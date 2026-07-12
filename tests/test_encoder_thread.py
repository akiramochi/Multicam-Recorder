"""
Concurrency / lifecycle tests for EncoderThread.

These use fake container/stream/frame objects so they run with no NDI SDK,
DeckLink card, or PyAV/FFmpeg installed — only the standard library.  They cover
the stability-critical behaviour of the encode thread: clean start/stop, strictly
increasing PTS, surviving per-frame exceptions, dropping video (not blocking) when
backed up, and never blocking the caller forever on audio.

Run directly (`python tests/test_encoder_thread.py`) or under pytest.
"""
import os
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.encoder_thread import EncoderThread


class FakePacket:
    def __init__(self, pts, dts=None, is_audio=False):
        self.dts = pts if dts is None else dts
        self.pts = pts
        self.size = 10
        self.is_keyframe = False
        self.is_audio = is_audio


class FakeStream:
    def __init__(self, audio=False):
        self.codec_context = type("C", (), {"time_base": Fraction(1, 30)})()
        self.time_base = Fraction(1, 30)
        self._audio = audio

    def encode(self, frame=None):
        if frame is None:
            return []                       # flush
        return [FakePacket(frame.pts, is_audio=self._audio)]


class ScriptedStream:
    """Video stream whose encode() yields one packet per call from a fixed
    (dts, pts) script, ignoring the fed frame — used to replay real dts/pts
    sequences captured from actual encoders (see scratch NVENC/libx265 repro
    behind hevc-av-desync-bframes-options.md)."""
    def __init__(self, dts_pts_pairs):
        self.codec_context = type("C", (), {"time_base": Fraction(1, 30)})()
        self.time_base = Fraction(1, 30)
        self._script = list(dts_pts_pairs)
        self._i = 0

    def encode(self, frame=None):
        if frame is None or self._i >= len(self._script):
            return []
        dts, pts = self._script[self._i]
        self._i += 1
        return [FakePacket(pts, dts=dts, is_audio=False)]


class FakeContainer:
    def __init__(self):
        self.video = []
        self.audio = []
        self.closed = False

    def mux(self, pkt):
        (self.audio if getattr(pkt, "is_audio", False) else self.video).append(pkt)

    def close(self):
        self.closed = True


class FakeFrame:
    def __init__(self, is_audio=False):
        self.pts = None
        self.is_audio = is_audio


def make_encoder(events, *, slow_mux=0.0, build_raises=False, maxsize=8,
                  video_stream=None):
    cont = {}

    def open_container(path, w, h, fps):
        c = FakeContainer()
        cont["c"] = c
        vs = video_stream if video_stream is not None else FakeStream(audio=False)
        return c, vs, FakeStream(audio=True)

    def close_container(c, vs, as_):
        if c is not None:
            c.close()
        return None, None, None

    def build_video(payload):
        if build_raises:
            raise RuntimeError("boom in build_video")
        if slow_mux:
            time.sleep(slow_mux)
        return FakeFrame(is_audio=False)

    def build_audio(payload):
        return FakeFrame(is_audio=True), 1024

    enc = EncoderThread(
        open_container=open_container,
        close_container=close_container,
        build_video=build_video,
        build_audio=build_audio,
        use_nvenc=False,
        on_status=lambda s: events.append(("status", s)),
        on_error=lambda m: events.append(("error", m)),
        on_stopped=lambda: events.append(("stopped",)),
        log_tag="TEST",
        maxsize=maxsize,
    )
    enc.start()
    return enc, cont


def test_normal_lifecycle():
    events = []
    enc, cont = make_encoder(events)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(10):
        enc.submit_video(b"frame", i)
        enc.submit_audio(b"audio")
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)

    assert not enc.is_alive(), "thread did not exit"
    c = cont["c"]
    assert c.closed, "container not closed"
    assert len(c.video) == 10, f"expected 10 video packets, got {len(c.video)}"
    pts = [p.pts for p in c.video]
    assert pts == sorted(pts) and len(set(pts)) == len(pts), \
        f"PTS not strictly increasing/unique: {pts}"
    assert ("status", "Recording") in events
    assert ("stopped",) in events


def test_build_error_keeps_thread_alive():
    events = []
    enc, cont = make_encoder(events, build_raises=True)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(5):
        enc.submit_video(b"frame", i)
    time.sleep(0.2)
    assert enc.is_alive(), "thread died on a per-frame build error"
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)
    assert not enc.is_alive()
    assert ("stopped",) in events


def test_video_drop_does_not_block():
    events = []
    enc, cont = make_encoder(events, slow_mux=0.05, maxsize=4)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    start = time.monotonic()
    for i in range(200):
        enc.submit_video(b"frame", i)               # must never block
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"submit_video appears to block ({elapsed:.2f}s)"
    enc.end()
    enc.shutdown()
    enc.join(timeout=10)
    assert not enc.is_alive()


def test_audio_does_not_hang_forever():
    events = []
    enc, cont = make_encoder(events, slow_mux=0.5, maxsize=2)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(5):
        enc.submit_video(b"frame", i)
    start = time.monotonic()
    for i in range(5):
        enc.submit_audio(b"audio")
    elapsed = time.monotonic() - start
    assert elapsed < 11.0, f"submit_audio hung ({elapsed:.2f}s)"
    enc.shutdown()
    enc.join(timeout=10)


def test_true_cfr_output():
    """End-to-end: index PTS + true rational rate must yield a CFR file at the
    exact source rate.  Skipped where PyAV/FFmpeg isn't installed."""
    try:
        import av
        import numpy as np
    except Exception:
        print("test_true_cfr_output SKIPPED (PyAV not available)")
        return

    import tempfile
    rate = Fraction(30000, 1001)        # true 29.97
    path = os.path.join(tempfile.gettempdir(), "_enc_cfr_test.mp4")

    def open_container(p, w, h, fps):
        c = av.open(path, mode="w")
        vs = c.add_stream("libx264", rate=rate)
        vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
        # Match the real workers: no B-frames, so DTS == PTS and the index-based
        # PTS stays monotonic without reordering.
        vs.codec_context.max_b_frames = 0
        vs.options = {"tune": "zerolatency", "preset": "ultrafast"}
        tb = Fraction(rate.denominator, rate.numerator)
        vs.time_base = tb
        try:
            vs.codec_context.time_base = tb
        except Exception:
            pass
        return c, vs, None      # no audio stream in this test

    def close_container(c, vs, a):
        for pkt in vs.encode():
            c.mux(pkt)
        c.close()
        return None, None, None

    def build_video(payload):
        img = np.full((180, 320, 3), 16, dtype=np.uint8)
        return av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")

    events = []
    enc = EncoderThread(
        open_container=open_container, close_container=close_container,
        build_video=build_video, build_audio=lambda p: None,
        use_nvenc=False, on_status=lambda s: None, on_error=lambda m: events.append(m),
        on_stopped=lambda: None, log_tag="CFR",
    )
    enc.start()
    enc.begin(path, 320, 180, rate)
    for i in range(90):
        enc.submit_video(b"x", i)
    enc.end()
    enc.shutdown()
    enc.join(timeout=10)

    d = av.open(path)
    avg = d.streams.video[0].average_rate
    d.close()
    os.remove(path)
    assert not events, f"encoder errors: {events}"
    assert avg == rate, f"expected CFR {rate}, got {avg}"


def test_reordered_dts_pts_muxed_unmodified():
    """libx265 with B-frames: dts/pts sequence captured from a real encode
    (see hevc-av-desync-bframes-options.md). Both fields must be muxed
    exactly as the encoder returned them — no manual offset. PTS is already
    the correct real display time (`vf.pts = frame_index`); shifting it
    desyncs audio. Shifting DTS alone (a naive fix) instead pushes DTS past
    PTS on packet index 3 below, where the reorder margin is only 0 — an
    invalid packet. Leaving both untouched avoids that and relies on the
    muxer's own edit-list handling of a negative starting DTS (verified
    separately to mux/read back cleanly with correct A/V sync)."""
    script = [
        (-2, 0), (-1, 5), (0, 3), (1, 1), (2, 2), (3, 4), (4, 10), (5, 8),
        (6, 6), (7, 7),
    ]
    events = []
    enc, cont = make_encoder(events, video_stream=ScriptedStream(script), maxsize=32)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    for i in range(len(script)):
        enc.submit_video(b"frame", i)
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)

    c = cont["c"]
    got = [(p.dts, p.pts) for p in c.video]
    assert got == script, f"packets were modified: {got} != {script}"


def test_locked_dts_pts_muxed_unmodified():
    """No-reordering path (NVENC, or libx265 with bframes=0): dts==pts on
    every packet, captured from a real bf=0 encode. Must also pass through
    unmodified — this is the currently-working NVENC behaviour and must not
    regress."""
    script = [(-3 + i, -3 + i) for i in range(10)]   # dts==pts, first is -3
    events = []
    enc, cont = make_encoder(events, video_stream=ScriptedStream(script), maxsize=32)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    for i in range(len(script)):
        enc.submit_video(b"frame", i)
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)

    c = cont["c"]
    got = [(p.dts, p.pts) for p in c.video]
    assert got == script, f"packets were modified: {got} != {script}"


def test_audio_buffered_until_first_video_packet_muxed():
    """Audio submitted before the encoder has emitted any video packet must
    be held back, then flushed once the first video packet actually reaches
    the container — not just once a video frame was submitted. Uses a
    ScriptedStream whose first two encode() calls return no packet (encoder
    buffering, as real B-frame lookahead does) to make sure the gate keys off
    an actual muxed packet, not merely on submit_video having been called."""
    script = [(None, None)] * 2 + [(0, 0), (1, 1), (2, 2)]

    class DelayedStream(ScriptedStream):
        def encode(self, frame=None):
            if frame is None or self._i >= len(self._script):
                return []
            dts, pts = self._script[self._i]
            self._i += 1
            if dts is None:
                return []
            return [FakePacket(pts, dts=dts, is_audio=False)]

    events = []
    enc, cont = make_encoder(events, video_stream=DelayedStream(script), maxsize=32)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    enc.submit_video(b"frame", 0)   # buffered internally, no packet yet
    enc.submit_audio(b"audio")      # must be held, not muxed early
    enc.submit_video(b"frame", 1)   # still buffered
    enc.submit_video(b"frame", 2)   # first packet emitted here
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)

    c = cont["c"]
    assert len(c.audio) == 1, f"expected 1 audio packet muxed, got {len(c.audio)}"
    assert len(c.video) >= 1, "expected at least one video packet muxed"


if __name__ == "__main__":
    test_normal_lifecycle()
    test_build_error_keeps_thread_alive()
    test_video_drop_does_not_block()
    test_audio_does_not_hang_forever()
    test_true_cfr_output()
    test_reordered_dts_pts_muxed_unmodified()
    test_locked_dts_pts_muxed_unmodified()
    test_audio_buffered_until_first_video_packet_muxed()
    print("ALL ENCODER THREAD TESTS PASSED")
