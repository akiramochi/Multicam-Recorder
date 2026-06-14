"""
Background encode + mux thread.

Capture (NDI receive / DeckLink driver callback) and encoding+muxing used to run
on a single thread.  A periodic heavy operation on that thread — a keyframe being
generated every GOP, or the OS flushing the output file to disk — would stall the
loop long enough that incoming frames piled up and got dropped, producing the
characteristic "stutter every couple of seconds".

This thread separates the two concerns:

  * The capture thread does only the unavoidable work that must happen before the
    source buffer is released (copy the pixels / samples) plus the rate-limited
    preview, then hands the raw payload to this thread via a queue.
  * This thread does colour conversion, frame construction, encoding and muxing —
    all the work whose latency is bursty — and owns the PyAV container, so all
    muxing stays single-threaded.

Video frames are *droppable*: when the queue is full the newest frame is discarded
(drop-newest).  The wall-clock PTS scheme means a dropped frame just leaves a gap
in presentation time, so the remaining frames still play at the right moment and
audio stays in sync.  Audio frames and control messages are never dropped — a lost
audio frame would desync the running sample counter.

The worker supplies four callables so this class stays codec/source agnostic:

  open_container(path, w, h, fps) -> (container, v_stream, a_stream)
  close_container(container, v_stream, a_stream) -> (None, None, None)
  build_video(payload)           -> av.VideoFrame   (pts NOT set)
  build_audio(payload)           -> (av.AudioFrame, n_samples)   (pts NOT set)

plus on_status / on_error / on_stopped callbacks (which emit the worker's Qt
signals) and a log_tag for diagnostic prints.
"""
import os
import queue
import threading
import time


class EncoderThread(threading.Thread):
    # AAC-LC has a built-in 1024-sample encoder delay; shifting the first audio
    # PTS by +1024 makes the first output packet's DTS land on 0 instead of
    # −1024 (which the MP4/MOV muxer rejects with EINVAL).
    _AAC_DELAY = 1024

    def __init__(self, *, open_container, close_container,
                 build_video, build_audio, use_nvenc,
                 on_status, on_error, on_stopped,
                 log_tag="ENC", maxsize=120):
        super().__init__(daemon=True, name=f"{log_tag}-encoder")
        # Queue depth = how long a system-wide hiccup (AV scan, disk flush, OS
        # task) the buffer can ride out before dropping.  Sized in frames, so the
        # default 120 is ~2 s at 60 fps (and ~4 s at 30 fps) — i.e. the time
        # cushion holds up even for high-frame-rate sources.  Each slot holds one
        # raw frame (~4 MB UYVY / ~6 MB BGR), so the memory ceiling is
        # maxsize × frame_size × streams, reached only while actually backed up
        # (normally the queue sits near empty).  Override with env MCR_ENC_QUEUE
        # to trade RAM for more/less absorption.
        _env_q = os.environ.get("MCR_ENC_QUEUE")
        if _env_q:
            try:
                maxsize = max(4, int(_env_q))
            except ValueError:
                pass
        self._open_container  = open_container
        self._close_container = close_container
        self._build_video     = build_video
        self._build_audio     = build_audio
        self._use_nvenc       = use_nvenc
        self._on_status       = on_status
        self._on_error        = on_error
        self._on_stopped      = on_stopped
        self._tag             = log_tag

        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._dropped = 0          # video frames discarded since last log
        self._dropped_total = 0
        self._submitted = 0        # total video frames handed in (for stats)
        self._audio_drop_warned = False
        # Opt-in per-stream timing diagnostics: set env MCR_ENC_STATS=1 to get a
        # breakdown of where each frame's time goes (conversion vs encode vs mux)
        # plus in/out frame rates, so a throughput deficit can be pinpointed.
        self._stats = bool(os.environ.get("MCR_ENC_STATS"))

    # ------------------------------------------------------------------
    # Producer API — called from the capture thread
    # ------------------------------------------------------------------

    # Bounded wait for control messages.  These must not be dropped lightly, but
    # an unbounded block would hang the capture thread if the encoder ever wedged
    # (e.g. a disk stall inside mux).  Since video is dropped when the queue is
    # full, a slot frees every encode cycle, so this only ever waits that long in
    # practice; the timeout is purely a safety valve against a stuck encoder.
    _CONTROL_PUT_TIMEOUT = 10.0

    def _put_control(self, item) -> bool:
        try:
            self._q.put(item, timeout=self._CONTROL_PUT_TIMEOUT)
            return True
        except queue.Full:
            print(f"[{self._tag}] encoder not draining — control message "
                  f"{item[0]!r} dropped after {self._CONTROL_PUT_TIMEOUT}s",
                  flush=True)
            return False

    def begin(self, path, w, h, fps) -> bool:
        """Open a new recording.  Returns False if the message couldn't be
        queued (encoder wedged) so the caller can retry on the next frame."""
        return self._put_control(("start", path, w, h, fps))

    def end(self) -> bool:
        """Finish the current recording and close the file."""
        return self._put_control(("stop",))

    def submit_video(self, payload, frame_index):
        """Hand a raw video payload to the encoder.  Droppable when backed up.

        frame_index is the source frame's sequential position within the current
        recording (assigned by the capture loop for *every* frame, including ones
        that get dropped here).  Using it as the PTS means a dropped frame leaves
        a gap in the timeline rather than compressing it, so video stays aligned
        with audio and the file is true constant-frame-rate.
        """
        self._submitted += 1
        try:
            self._q.put_nowait(("video", payload, frame_index))
        except queue.Full:
            self._dropped += 1
            self._dropped_total += 1
            if self._dropped >= 30:
                print(f"[{self._tag}] encoder behind — dropped "
                      f"{self._dropped_total} video frame(s) so far", flush=True)
                self._dropped = 0

    def submit_audio(self, payload):
        """Hand a raw audio payload to the encoder.

        Audio is not dropped under normal back-pressure: because video is dropped
        when the queue is full the queue keeps draining, so this only waits for
        roughly one encode cycle.  But we use a bounded wait, not an unbounded
        block — if the encoder ever wedges or dies, an infinite block here would
        hang the capture thread (NDI frames never freed, Stop never honoured).
        On timeout we drop the audio and warn instead.
        """
        try:
            self._q.put(("audio", payload), timeout=2.0)
            self._audio_drop_warned = False
        except queue.Full:
            if not self._audio_drop_warned:
                print(f"[{self._tag}] encoder not draining — dropping audio; "
                      f"recording may be incomplete", flush=True)
                self._audio_drop_warned = True

    def shutdown(self):
        """Ask the thread to close any open file and exit."""
        self._put_control(("quit",))

    # ------------------------------------------------------------------
    # Consumer — runs on this thread
    # ------------------------------------------------------------------

    def run(self):
        container = v_stream = a_stream = None
        path = ""
        w = h = 0
        fps = 30.0

        v_idx0       = None   # source frame index of this recording's 1st frame
        v_last_pts   = -1     # last PTS used; enforces strict monotonicity
        a_pts        = 0      # running audio sample counter
        v_dts_offset = -1     # shift so first video DTS = 0; -1 = not seen yet
        audio_pkt_buf = []    # encoded audio held until first video pkt muxed
        session_failed = False
        v_err_count  = 0

        def _reset_session():
            nonlocal v_idx0, v_last_pts, a_pts
            nonlocal v_dts_offset, audio_pkt_buf, session_failed, v_err_count
            v_idx0       = None
            v_last_pts   = -1
            a_pts        = 0
            v_dts_offset = -1
            audio_pkt_buf = []
            session_failed = False
            v_err_count  = 0

        # Per-interval timing accumulators (only used when self._stats).
        st_t0 = time.monotonic()
        st_n = 0                      # frames encoded since last report
        st_build = st_enc = st_mux = 0.0
        st_last_sub = st_last_drop = 0

        while True:
            item = self._q.get()
            kind = item[0]

            if self._stats:
                _now = time.monotonic()
                if _now - st_t0 >= 3.0:
                    dt = _now - st_t0
                    sub = self._submitted
                    drop = self._dropped_total
                    nb = max(1, st_n)
                    # in/out: frames submitted vs encoded per second.  build/enc/
                    # mux: avg wall-clock ms per frame in each stage (wall-clock,
                    # so time blocked on the GIL by sibling streams shows up too).
                    print(f"[{self._tag}] stats {dt:.1f}s: "
                          f"in={(sub - st_last_sub) / dt:5.1f}/s "
                          f"out={st_n / dt:5.1f}/s "
                          f"drop+={drop - st_last_drop} "
                          f"q={self._q.qsize()}/{self._maxsize} "
                          f"build={1000 * st_build / nb:5.1f}ms "
                          f"enc={1000 * st_enc / nb:5.1f}ms "
                          f"mux={1000 * st_mux / nb:5.1f}ms", flush=True)
                    st_t0 = _now
                    st_n = 0
                    st_build = st_enc = st_mux = 0.0
                    st_last_sub = sub
                    st_last_drop = drop

            if kind == "quit":
                break

            # A failure while handling one item must never kill this thread: if
            # it died, submit_audio()'s bounded wait would start dropping and the
            # recording would silently break (and a wedged thread could stall
            # capture).  Guard the whole dispatch and keep going.
            try:
                if kind == "start":
                    _, path, w, h, fps = item
                    # Container is opened lazily on the first video frame (its
                    # dimensions/fps come from the frame, and NVENC SPS/PPS must
                    # be committed by a video mux before any audio is written).
                    _reset_session()
                    continue

                if kind == "stop":
                    if container is not None:
                        container, v_stream, a_stream = self._close_container(
                            container, v_stream, a_stream)
                    _reset_session()
                    self._on_stopped()
                    continue

                if kind == "video":
                    _, payload, frame_index = item
                    if session_failed:
                        continue
                    if v_idx0 is None:
                        v_idx0 = frame_index   # anchor this recording's PTS at 0

                    if container is None:
                        try:
                            container, v_stream, a_stream = self._open_container(
                                path, w, h, fps)
                            self._on_status("Recording")
                        except Exception as exc:
                            self._on_error(f"Could not open output file: {exc}")
                            container = v_stream = a_stream = None
                            session_failed = True
                            self._on_stopped()
                            continue

                    try:
                        _ta = time.perf_counter() if self._stats else 0.0
                        vf = self._build_video(payload)
                        if vf is None:
                            continue
                        # PTS = source frame index (anchored to 0) in the stream's
                        # true rational time base → constant-frame-rate output at
                        # the real source rate.  A dropped frame leaves a gap
                        # (the index skips), so the timeline and audio sync are
                        # preserved instead of the video being compressed shorter.
                        # Clamp strictly increasing as a safety net.
                        new_pts = max(0, frame_index - v_idx0)
                        if new_pts <= v_last_pts:
                            new_pts = v_last_pts + 1
                        v_last_pts = new_pts
                        vf.pts = new_pts

                        if self._stats:
                            _tb = time.perf_counter()
                            st_build += _tb - _ta
                        pkts = list(v_stream.encode(vf))
                        if self._stats:
                            _tc = time.perf_counter()
                            st_enc += _tc - _tb
                        for pkt in pkts:
                            # NVENC's pipeline delay makes the first packet's DTS
                            # negative; compute a one-time shift to bring it to 0
                            # and apply it to every packet so DTS stays ≥ 0.
                            if v_dts_offset < 0:
                                raw_dts = pkt.dts if pkt.dts is not None else 0
                                v_dts_offset = max(0, -raw_dts)
                                if v_dts_offset:
                                    print(f"[{self._tag}] DTS offset = "
                                          f"{v_dts_offset} (NVENC pipeline "
                                          f"delay)", flush=True)
                            if v_dts_offset > 0:
                                if pkt.dts is not None:
                                    pkt.dts += v_dts_offset
                                if pkt.pts is not None:
                                    pkt.pts += v_dts_offset
                            container.mux(pkt)
                        if self._stats:
                            st_mux += time.perf_counter() - _tc

                        # The first video mux triggers write_header with the
                        # correct video codecpar/extradata; now flush any audio
                        # held back.
                        if audio_pkt_buf and v_dts_offset >= 0:
                            for _ap in audio_pkt_buf:
                                try:
                                    container.mux(_ap)
                                except Exception as _ae:
                                    print(f"[{self._tag}] buffered audio mux "
                                          f"error: {_ae}", flush=True)
                            audio_pkt_buf.clear()
                        st_n += 1
                    except Exception as exc:
                        if v_err_count < 3:
                            print(f"[{self._tag}] video encode error: {exc}",
                                  flush=True)
                        v_err_count += 1
                    continue

                if kind == "audio":
                    _, payload = item
                    if container is None or a_stream is None:
                        # No container yet (audio before first video) — drop,
                        # exactly as the inline path did; a_pts only advances
                        # once recording.
                        continue
                    try:
                        built = self._build_audio(payload)
                        if built is None:
                            continue
                        af, n_samp = built
                        af.pts = a_pts + self._AAC_DELAY
                        a_pts += n_samp
                        for pkt in a_stream.encode(af):
                            if v_dts_offset >= 0:
                                container.mux(pkt)
                            else:
                                audio_pkt_buf.append(pkt)
                    except Exception as exc:
                        print(f"[{self._tag}] audio encode error: {exc}",
                              flush=True)
                    continue

            except Exception as exc:
                # Last-resort guard so one bad item can't take down the thread.
                print(f"[{self._tag}] unexpected encoder error "
                      f"({kind}): {exc}", flush=True)

        # Loop exited ('quit') — make sure the output file is closed cleanly.
        if container is not None:
            try:
                self._close_container(container, v_stream, a_stream)
            except Exception:
                pass
