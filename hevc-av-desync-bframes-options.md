# Software HEVC AV-desync fix — alternatives to disabling B-frames

## Background

Branch `fix/hevc-av-desync` (was open as PR #6) fixes an AV-desync bug in
software libx265 recordings by disabling B-frames (`bframes=0`) in
[`src/decklink_worker.py`](src/decklink_worker.py) and
[`src/stream_worker.py`](src/stream_worker.py). This only affects the narrow
path where H265 is explicitly selected **and** NVENC is off (default codec is
H264; NVENC HEVC already forces `bf=0` separately). Disabling B-frames trades
away some compression efficiency (roughly 10-20% bitrate efficiency loss at a
fixed bitrate, content-dependent) to fix the bug.

Root cause: the shared per-packet timestamp-fixup logic in
[`src/encoder_thread.py:301-317`](src/encoder_thread.py) computes a one-time
`v_dts_offset` from the first packet's negative DTS, then applies that same
offset to **both** `pkt.dts` and `pkt.pts` for every subsequent packet. For
NVENC (`bf=0`, `rc-lookahead=0`, no reordering) this is a valid fix — a
constant pipeline-startup latency shifts both dts and pts together. For
libx265 with B-frames enabled, the negative first DTS is due to genuine
frame-reordering, not a constant pipeline delay — the fed-in PTS values
(`vf.pts = frame_index`, see comment at
[`src/encoder_thread.py:282`](src/encoder_thread.py:282)) are already correct
real display-time values and shouldn't be shifted. Shifting them anyway
delays the whole video timeline relative to audio (which isn't shifted to
compensate), causing the desync.

The question: can this be fixed **without** disabling B-frames?

## Option A — offset DTS only, leave PTS untouched

Change the fixup block in `encoder_thread.py` to add `v_dts_offset` to
`pkt.dts` only, not `pkt.pts`.

**Pros**
- Minimal, localized change — remove one line
  (`pkt.pts += v_dts_offset`) in the one shared block used by both
  DeckLink and NDI paths.
- No new state to compute or thread through — reuses the existing reactive
  `v_dts_offset` detection, just narrows what it's applied to.
- Keeps the current mental model intact (offset computed from the first
  packet, applied per-packet as data arrives).
- Matches the actual constraint: MP4 muxers need DTS ≥ 0 and monotonic; PTS
  has no such constraint and is already correct as fed in.

**Cons**
- Relies on an **unverified assumption** that NVENC's returned `pkt.pts` is
  not itself internally shifted by the encoder's pipeline delay. If it is,
  decoupling dts/pts would break the currently-working NVENC path. Needs a
  repro test on the NVENC branch specifically before trusting it.
- Still reactive/first-packet-derived rather than principled — a single
  one-time offset could still be wrong if some encoder/setting produces a
  non-constant DTS/PTS relationship.
- Doesn't address the audio side — can't express a case where video's *true*
  start time actually needs to shift, not just its DTS bookkeeping.

## Option B — precompute reorder delay, shift audio and video uniformly

Compute the B-frame reorder delay up front (bounded, determined by
`bframes`/lookahead settings — not variable per frame) and apply it as a
fixed startup offset to **both** the video and audio timelines, the same way
`_AAC_DELAY` already compensates for the AAC encoder's own priming delay
(see [`src/encoder_thread.py:352`](src/encoder_thread.py:352)).

**Pros**
- Principled: reorder delay is a known, bounded property of encoder
  configuration, not inferred reactively from whatever the first packet
  happens to report.
- Symmetric with the existing `_AAC_DELAY` handling — generalizes an
  approach already trusted in this codebase.
- More robust across encoders/settings — works regardless of whether
  PTS/DTS reporting quirks differ between NVENC and libx265, since it aligns
  actual track start times rather than picking which field to patch.

**Cons**
- More invasive — touches both video and audio timing logic, and needs a
  way to determine the delay constant up front (from `bframes`/lookahead
  settings, or by querying the opened codec context).
- Requires the video and audio encode paths to coordinate on a shared
  constant before either starts muxing, which the current
  threaded/queued structure doesn't cleanly support yet — likely needs
  restructuring of the startup sequence.
- Harder to verify by inspection; needs a repro test comparing actual A/V
  offset in the output file, not just "DTS is non-negative."
- Larger surface for regression — changes behavior for the already-working
  NVENC and default-H264 paths too, not just the narrow software-libx265
  case.

## Recommendation

Try Option A first (cheaper, more surgical) — but verify the NVENC-side PTS
assumption with a repro test before landing it. Fall back to Option B if
Option A turns out to break the NVENC path or otherwise doesn't hold up.

## Status — resolved on `fix/hevc-av-desync-option-a`

Investigated on a machine with a real NVIDIA GPU (hevc_nvenc available) and
PyAV/ffmpeg installed, which made it possible to actually test the two
"unverified assumptions" flagged above instead of reasoning about them in the
abstract. Both original options turned out to be wrong in instructive ways:

- **Option A as originally described (offset DTS only, leave PTS) is
  unsafe.** Replaying real dts/pts pairs captured from a libx265 B-frame
  encode showed a fixed additive DTS offset can push `dts > pts` on later
  packets — the reorder margin (`pts - dts`) varies per packet, and isn't
  always ≥ the offset needed to zero out the first packet's negative DTS.
  That's an invalid packet, not just a cosmetic issue.
- **The premise "MP4 muxers need DTS ≥ 0" doesn't hold for PyAV/ffmpeg's mp4
  muxer.** Muxing raw libx265 B-frame output with *no* manual timestamp
  patching at all — negative starting DTS included — produced no error and
  read back with `start_time == 0` on both streams (the muxer normalizes it,
  apparently via an edit list).

Given that, the actual fix is simpler than either option: **mux every
packet's DTS/PTS exactly as the encoder returns them, no offset at all.**
Implemented in [`src/encoder_thread.py`](src/encoder_thread.py) — the whole
`v_dts_offset`/pipeline-delay-shift mechanism was removed and replaced with a
plain `video_muxed` boolean, used only to gate holding audio packets until
the first video packet is actually muxed (unrelated to timestamp math).
`bframes=0` was never applied on this branch (it forked from `main` before
`fix/hevc-av-desync` merged), so B-frames stay on by default — no
compression-efficiency tradeoff.

Verified end-to-end (not just unit tests) on this machine:
- Real `EncoderThread` + real `libx265` (B-frames on) + real AAC audio →
  decoded file has video and audio both starting at pts 0.0s.
- Real `EncoderThread` + real `hevc_nvenc` (bf=0) + real AAC audio → same,
  0.0s A/V offset, confirming no regression on the previously-working path.
- Added regression tests to `tests/test_encoder_thread.py` asserting muxed
  packets are byte-for-byte unmodified from what the stream's `encode()`
  returns, for both a reordered (B-frame) and locked (dts==pts) dts/pts
  sequence captured from real encodes, plus a test for the audio-buffering
  gate.

**Update: verified with a real, independent player (not just PyAV reading
back its own muxer's output).** Generated a 3s test clip via the standalone
`ffmpeg` CLI — `libx264 -bf 3` (real B-frame reordering, confirmed via
`ffprobe` to produce a negative starting DTS: `-1024` in the file's
timebase, ≈ −2 frames) + AAC audio, with a distinct white flash frame and an
audible tone burst at t=0/1/2s. Served it over local HTTP (with Range
support, which Chromium needs for real seeking — Python's default
`http.server` doesn't send `Accept-Ranges` and silently breaks seeking) and
loaded it in the browser. Independently measured, using the browser's own
decoders (not ffmpeg's read-back):
- Video: stepped through every frame via `video.currentTime` seeks and
  measured canvas brightness to find the flash frames.
- Audio: decoded the full track via `AudioContext.decodeAudioData` (the
  browser's own AAC decoder) and measured RMS to find the beep bursts.

Result: flash and beep events landed at 0.000s, 1.000s, 2.000s with **zero**
offset between them. As a negative control (to confirm the test would
actually catch a real desync), the same measurement was run against a copy
with the video track deliberately shifted +0.5s via `ffmpeg -itsoffset`
(mimicking what the original bug did) — it correctly reported a 0.5s
flash/beep offset. So the "0.000s" result on the real fix isn't a test
artifact.

**Still not verified in this sandbox:** playback in actual consumer
player/NLE software (QuickTime, Premiere, VLC — none installed here) and
behavior on real DeckLink capture hardware rather than synthetic frames. The
original `v_dts_offset` code's "NVENC pipeline delay" comment implied someone
had observed a real negative first-packet DTS on NVENC in practice; this
session's repro never reproduced that under the current
`bf=0`/`rc-lookahead=0`/`max_b_frames=0` settings (dts stayed at 0 and
dts==pts on every packet), so that defensive code appears to have been dead
already — but if it does still occur on some driver/hardware combination,
passing DTS/PTS through unmodified should be safe there too by the same
edit-list argument, just untested on that specific path.
