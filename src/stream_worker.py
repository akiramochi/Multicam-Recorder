"""Per-stream worker thread: NDI receive → preview → encode."""
import time
from fractions import Fraction
from typing import Tuple

import numpy as np
from PyQt6.QtCore import QMutex, QThread, pyqtSignal
from PyQt6.QtGui import QImage

from .recording_settings import RecordingSettings
from .encoder_thread import EncoderThread

try:
    import NDIlib as _ndi
    NDI_AVAILABLE = True
except ImportError:
    _ndi = None
    NDI_AVAILABLE = False

try:
    import av
    AV_AVAILABLE = True
except ImportError:
    av = None
    AV_AVAILABLE = False


class StreamWorker(QThread):
    """
    Owns one NDI receiver.  Emits QImages for the preview widget and writes
    H.264/H.265 audio+video to disk while recording is active.
    """

    frame_ready = pyqtSignal(QImage)
    recording_started = pyqtSignal(str)   # output file path
    recording_stopped = pyqtSignal()
    status_changed = pyqtSignal(str)      # human-readable state string
    error_occurred = pyqtSignal(str)
    stream_info = pyqtSignal(int, int, float)  # width, height, fps
    # Emits every audio frame as (planes, n_samp, layout, sample_rate) so
    # other streams can route this source's audio into their recording.
    audio_captured = pyqtSignal(object)

    _PREVIEW_FPS = 15

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self._source = source
        self._settings = RecordingSettings()
        self._mutex = QMutex()

        # Flags written from the GUI thread, read in run()
        self._running = False
        self._start_flag = False
        self._stop_flag = False
        self._pending_path = ""
        # Authoritative "is a recording session active" state, guarded by the
        # mutex.  Set True/False by the capture loop on start/stop, and reset to
        # False by the encoder thread (via _on_enc_stopped) when a session ends
        # or aborts, so the next Record press is honoured.
        self._rec_active = False

        self._encoder = None        # set while run() holds the EncoderThread
        self._use_own_audio = True  # False when audio is routed from another source

        self._preview_interval = 1.0 / self._PREVIEW_FPS
        self._last_preview_ts = 0.0

    # ------------------------------------------------------------------
    # Public interface (called from GUI thread)
    # ------------------------------------------------------------------

    @property
    def source_name(self) -> str:
        return str(self._source)

    def configure(self, settings: RecordingSettings):
        self._settings = settings.copy()

    def set_use_own_audio(self, val: bool):
        """GUI thread: switch between own audio and externally-routed audio."""
        self._use_own_audio = val

    def submit_external_audio(self, payload):
        """Receive audio routed from another source and feed it to this encoder.

        Called via DirectConnection from the source's capture thread, so this
        must not block.  Drops silently if our encoder is not recording yet.
        """
        enc = self._encoder
        if enc:
            enc.submit_audio_nonblocking(payload)

    def start_recording(self, output_path: str):
        self._mutex.lock()
        self._pending_path = output_path
        self._start_flag = True
        self._mutex.unlock()

    def stop_recording(self):
        self._mutex.lock()
        self._stop_flag = True
        self._mutex.unlock()

    def stop(self):
        self._mutex.lock()
        self._running = False
        self._stop_flag = True
        self._mutex.unlock()

    # ------------------------------------------------------------------
    # Worker thread main loop
    # ------------------------------------------------------------------

    def run(self):
        if not NDI_AVAILABLE or not AV_AVAILABLE:
            self.error_occurred.emit(
                "Missing dependencies: install ndi-python and av"
            )
            return

        recv = self._create_receiver()
        if recv is None:
            return

        self._running = True
        self.status_changed.emit("Connected")

        # Encode + mux runs on its own thread so a keyframe or disk flush can
        # never stall this capture loop and force dropped frames (the periodic
        # stutter).  This loop only copies pixels/samples and feeds the encoder.
        encoder = EncoderThread(
            open_container=self._open_container,
            close_container=self._close_container,
            build_video=self._build_video,
            build_audio=self._build_audio,
            use_nvenc=self._settings.use_nvenc,
            on_status=self.status_changed.emit,
            on_error=self._on_enc_error,
            on_stopped=self._on_enc_stopped,
            log_tag=f"NDI:{self.source_name}",
        )
        encoder.start()
        self._encoder = encoder

        no_signal_streak = 0
        first_frame = True
        output_path = ""
        enc_started = False   # has begin() been sent for the current session?
        vseq = 0              # source frame index within the current recording

        while self._running:
            # ---------- check flags from GUI thread ----------
            self._mutex.lock()
            start_req = self._start_flag
            stop_req = self._stop_flag
            pending = self._pending_path
            self._start_flag = False
            self._stop_flag = False
            prev_active = self._rec_active
            if start_req:
                self._rec_active = True
            if stop_req:
                self._rec_active = False
            rec_active = self._rec_active
            self._mutex.unlock()

            if start_req and not prev_active:
                output_path = pending
                enc_started = False
                self.status_changed.emit("Waiting for frame…")
                self.recording_started.emit(output_path)

            if stop_req and prev_active:
                # Hand the close off to the encoder thread; it drains any queued
                # frames, writes the trailer, then emits recording_stopped.
                encoder.end()
                enc_started = False

            # ---------- receive frame ----------
            frame_type, v_frame, a_frame, _ = _ndi.recv_capture_v2(recv, 100)

            # ---------- video ----------
            if frame_type == _ndi.FRAME_TYPE_VIDEO and v_frame:
                no_signal_streak = 0
                frame_ts = time.monotonic()   # arrival time → drives video PTS
                w = v_frame.xres
                h = v_frame.yres
                fps_n = v_frame.frame_rate_N or 30000
                fps_d = v_frame.frame_rate_D or 1001
                fps = fps_n / fps_d

                if first_frame:
                    first_frame = False
                    self.stream_info.emit(w, h, fps)

                try:
                    raw = np.frombuffer(v_frame.data, dtype=np.uint8)
                    expected = w * h * 4
                    if raw.size >= expected:
                        # Copy the pixels (dropping the unused alpha) before the
                        # NDI frame is freed below; the encoder thread does the
                        # colour conversion and encoding off this loop.
                        bgr = np.ascontiguousarray(
                            raw[:expected].reshape((h, w, 4))[:, :, :3])

                        # preview at limited rate
                        now = time.monotonic()
                        if now - self._last_preview_ts >= self._preview_interval:
                            self._last_preview_ts = now
                            self._emit_preview(bgr, w, h)

                        if rec_active:
                            if not enc_started:
                                # NDI frames carry the exact rational rate; pass it
                                # so the file is true CFR at the real source rate.
                                enc_started = encoder.begin(
                                    output_path, w, h, Fraction(fps_n, fps_d))
                                if enc_started:
                                    vseq = 0
                            if enc_started:
                                # vseq = source frame index (incremented for every
                                # frame); a frame dropped at the queue leaves a PTS
                                # gap so the timeline and audio sync are preserved.
                                encoder.submit_video(bgr, vseq)
                                vseq += 1
                except Exception:
                    pass

                _ndi.recv_free_video_v2(recv, v_frame)

            # ---------- audio ----------
            elif frame_type == _ndi.FRAME_TYPE_AUDIO and a_frame:
                try:
                    n_ch = a_frame.no_channels
                    n_samp = a_frame.no_samples
                    # channel_stride_in_bytes may include padding; use it
                    # to index each channel rather than assuming tight packing
                    stride_f32 = a_frame.channel_stride_in_bytes // 4
                    raw = np.frombuffer(a_frame.data, dtype=np.float32)
                    out_ch = min(n_ch, 2)
                    layout = "stereo" if out_ch == 2 else "mono"
                    # Copy each channel's samples before the frame is freed;
                    # the encoder thread builds and encodes the AudioFrame.
                    planes = [
                        raw[i * stride_f32: i * stride_f32 + n_samp].copy()
                        for i in range(out_ch)
                    ]
                    payload = (planes, n_samp, layout, a_frame.sample_rate)
                    # Always broadcast so listeners routing this audio can receive it.
                    self.audio_captured.emit(payload)
                    if self._use_own_audio and rec_active and enc_started:
                        encoder.submit_audio(payload)
                except Exception as exc:
                    print(f'[NDI] audio capture error: {exc}', flush=True)

                _ndi.recv_free_audio_v2(recv, a_frame)

            elif frame_type == _ndi.FRAME_TYPE_NONE:
                no_signal_streak += 1
                if no_signal_streak >= 50:   # ~5 s
                    self.status_changed.emit("No Signal")
                    no_signal_streak = 0

        # ------------------------------------------------------------------
        # Shutdown — let the encoder finish any queued frames and close the file.
        # ------------------------------------------------------------------
        encoder.shutdown()
        encoder.join(timeout=10.0)
        self._encoder = None

        _ndi.recv_destroy(recv)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_receiver(self):
        desc = _ndi.RecvCreateV3()
        desc.color_format = _ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        desc.bandwidth = _ndi.RECV_BANDWIDTH_HIGHEST
        desc.allow_video_fields = False
        recv = _ndi.recv_create_v3(desc)
        if not recv:
            self.error_occurred.emit(
                f"Could not create NDI receiver for '{self.source_name}'"
            )
            return None
        _ndi.recv_connect(recv, self._source.raw)
        return recv

    def _emit_preview(self, bgr: np.ndarray, w: int, h: int):
        try:
            rgb = np.ascontiguousarray(bgr[:, :, ::-1])
            qi = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
            self.frame_ready.emit(qi.copy())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Encoder-thread bridge
    # ------------------------------------------------------------------

    def _build_video(self, payload):
        """Build an av.VideoFrame from a contiguous BGR array (encoder thread)."""
        bgr = payload
        frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
        # NVENC does not accept bgr24; convert to yuv420p (libswscale) here, on
        # the encoder thread, so the capture loop is never charged for it.
        if self._settings.use_nvenc:
            frame = frame.reformat(format="yuv420p")
        return frame

    def _build_audio(self, payload):
        """Build an av.AudioFrame from copied planar float samples."""
        planes, n_samp, layout, sample_rate = payload
        af = av.AudioFrame(format="fltp", layout=layout, samples=n_samp)
        af.sample_rate = sample_rate
        for i, plane in enumerate(planes):
            af.planes[i].update(plane.tobytes())
        return af, n_samp

    def _on_enc_error(self, msg: str):
        print(f'[NDI] {msg}', flush=True)
        self.error_occurred.emit(msg)

    def _on_enc_stopped(self):
        self._mutex.lock()
        self._rec_active = False
        self._mutex.unlock()
        self.status_changed.emit("Connected")
        self.recording_stopped.emit()

    def _open_container(
        self, path: str, w: int, h: int, fps: float
    ) -> Tuple:
        # NOTE: do NOT use movflags=+faststart here.  faststart performs a
        # second pass at container close that rewrites the ENTIRE file to move
        # the moov atom to the front — for a long recording that means waiting
        # while gigabytes are re-copied before stop() returns.  Local recordings
        # play back fine with moov at the end; if web/progressive streaming is
        # ever needed, run a faststart remux as a separate post-process step.
        c = av.open(path, mode="w")

        codec   = self._settings.effective_video_codec
        profile = self._settings.active_video_profile
        nvenc   = self._settings.use_nvenc

        # Use the true rational frame rate (e.g. 30000/1001 for 29.97) for the
        # stream rate and time base, so the recorded file is genuine
        # constant-frame-rate at the real source rate rather than rounded to 30.
        rate = fps if isinstance(fps, Fraction) else Fraction(fps).limit_denominator(1001000)
        tb   = Fraction(rate.denominator, rate.numerator)   # 1 / rate
        vs = c.add_stream(codec, rate=rate)
        if codec in ('libx265', 'hevc_nvenc'):
            # FFmpeg's MP4/MOV muxer defaults HEVC to the 'hev1' codec tag
            # (out-of-band parameter sets).  QuickTime/AVFoundation only
            # recognises 'hvc1' (in-band parameter sets); without this the
            # file plays in VLC/ffplay but QuickTime Player shows nothing.
            vs.codec_context.codec_tag = 'hvc1'
        vs.width     = w
        vs.height    = h
        vs.bit_rate  = self._settings.video_bitrate_bps
        vs.time_base = tb
        try:
            vs.codec_context.time_base = tb
        except Exception:
            pass

        # NVENC accepts yuv420p directly.
        # Software HEVC main10 needs a 10-bit pixel format.
        if not nvenc and codec == "libx265" and profile == "main10":
            vs.pix_fmt = "yuv420p10le"
        else:
            vs.pix_fmt = "yuv420p"

        if nvenc:
            # bf=0 → no B-frames (frameIntervalP=1); rc-lookahead=0 → no
            # look-ahead buffering.  Both together ensure every encode() call
            # yields exactly one output packet with DTS == PTS.
            vs.codec_context.max_b_frames = 0
            opts = {"bf": "0", "rc-lookahead": "0"}
            if self._settings.video_codec.value == "libx264":
                opts["profile"] = profile
        elif codec == "libx264":
            opts = {"preset": "fast", "profile": profile, "tune": "zerolatency"}
        else:  # libx265
            # bframes=0 keeps DTS == PTS (matches the nvenc/libx264 paths
            # above) so encoder_thread's NVENC-pipeline-delay DTS-offset
            # logic — which shifts every packet's PTS along with DTS — never
            # fires here.  With B-frames on, libx265's frame reordering makes
            # the first packet's DTS go negative for a different reason (true
            # reordering, not pipeline delay), and shifting PTS to compensate
            # desyncs video against audio, which isn't shifted the same way.
            opts = {"preset": "fast", "profile": profile,
                    "x265-params": "log-level=none:keyint=60:bframes=0"}
        vs.options = opts

        if nvenc:
            # NVENC populates avctx→extradata (the SPS/PPS parameter sets
            # needed for the MP4 avcC/hvcC box) only after avcodec_open2 runs.
            # PyAV opens the codec lazily on the first encode() call, which is
            # AFTER avformat_write_header — so the box ends up empty and every
            # decoder except VLC (which parses in-band parameters) shows black.
            # Fix: force the codec open now so extradata is ready before the
            # container header is written on the first mux() call.
            try:
                # vs.options = opts above is the standard way to pass options
                # to the eventual avcodec_open2 call.  Some PyAV versions also
                # accept setting options directly on the CodecContext.
                try:
                    vs.codec_context.options = opts
                except Exception:
                    pass
                # Retry loop: NVENC session release is asynchronous in the
                # NVIDIA driver, so a feed dropout that triggered a quick
                # close→reopen can leave the session counter transiently
                # above the cap.  Give the driver up to ~1.2s to release.
                _open_err = None
                for _attempt in range(3):
                    try:
                        vs.codec_context.open()
                        _open_err = None
                        break
                    except Exception as _re:
                        _open_err = _re
                        if _attempt < 2:
                            print(f'[NDI] NVENC open attempt '
                                  f'{_attempt + 1} failed ({_re}); '
                                  f'retrying in 400ms…', flush=True)
                            time.sleep(0.4)
                if _open_err is not None:
                    raise _open_err
                _ed = vs.codec_context.extradata
                print(f'[NDI] NVENC codec pre-opened; '
                      f'extradata={len(_ed) if _ed else 0}B', flush=True)
            except Exception as _e:
                # NVENC failed to initialise (common causes: concurrent-session
                # cap reached on consumer GeForce GPUs — ~3 HEVC / ~5 H.264 —
                # or transient driver state after a previous session).  Tear
                # down the half-built container and re-raise so the caller
                # aborts this recording cleanly with a visible error.
                print(f'[NDI] NVENC pre-open FAILED: {_e}', flush=True)
                print(f'[NDI] Aborting recording.  If this is HEVC with '
                      f'multiple streams, your GPU may have hit its concurrent '
                      f'NVENC session cap — try H.264 or fewer streams.',
                      flush=True)
                try:
                    c.close()
                except Exception:
                    pass
                raise

        as_ = c.add_stream("aac", rate=48000)
        as_.bit_rate = self._settings.audio_bitrate_bps
        as_.layout   = "stereo"
        as_.time_base = Fraction(1, 48000)

        return c, vs, as_

    def _close_container(self, container, v_stream, a_stream) -> Tuple:
        if container is None:
            return None, None, None
        try:
            if v_stream:
                for pkt in v_stream.encode():
                    container.mux(pkt)
            if a_stream:
                for pkt in a_stream.encode():
                    container.mux(pkt)
        except Exception:
            pass
        try:
            container.close()
        except Exception:
            pass
        return None, None, None
