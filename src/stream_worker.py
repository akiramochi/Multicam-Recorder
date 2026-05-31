"""Per-stream worker thread: NDI receive → preview → encode."""
import time
from fractions import Fraction
from typing import Optional, Tuple

import numpy as np
from PyQt6.QtCore import QMutex, QThread, pyqtSignal
from PyQt6.QtGui import QImage

from .recording_settings import RecordingSettings

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

        container = None
        v_stream = None
        a_stream = None
        is_recording = False
        v_pts = 0
        a_pts = 0
        v_dts_offset = -1   # shift so first video DTS = 0; -1 = not yet seen
        audio_pkt_buf = []  # encoded audio pkts held until first video pkt muxed
        no_signal_streak = 0
        first_frame = True
        output_path = ""

        while self._running:
            # ---------- check flags from GUI thread ----------
            self._mutex.lock()
            start_req = self._start_flag
            stop_req = self._stop_flag
            pending = self._pending_path
            self._start_flag = False
            self._stop_flag = False
            self._mutex.unlock()

            if start_req and not is_recording:
                output_path = pending
                is_recording = True
                self.status_changed.emit("Waiting for frame…")
                self.recording_started.emit(output_path)

            if stop_req and is_recording:
                container, v_stream, a_stream = self._close_container(
                    container, v_stream, a_stream
                )
                is_recording = False
                v_pts = 0
                a_pts = 0
                v_dts_offset = -1
                audio_pkt_buf = []
                self.status_changed.emit("Connected")
                self.recording_stopped.emit()

            # ---------- receive frame ----------
            frame_type, v_frame, a_frame, _ = _ndi.recv_capture_v2(recv, 100)

            # ---------- video ----------
            if frame_type == _ndi.FRAME_TYPE_VIDEO and v_frame:
                no_signal_streak = 0
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
                        bgrx = raw[:expected].reshape((h, w, 4))

                        # preview at limited rate
                        now = time.monotonic()
                        if now - self._last_preview_ts >= self._preview_interval:
                            self._last_preview_ts = now
                            self._emit_preview(bgrx, w, h)

                        # encoding
                        if is_recording:
                            if container is None:
                                try:
                                    container, v_stream, a_stream = self._open_container(
                                        output_path, w, h, fps
                                    )
                                    v_pts = 0
                                    a_pts = 0
                                    self.status_changed.emit("Recording")
                                except Exception as exc:
                                    self.error_occurred.emit(
                                        f"Could not open output file: {exc}"
                                    )
                                    is_recording = False
                                    self.status_changed.emit("Connected")
                                    self.recording_stopped.emit()

                            if container is not None and v_stream is not None:
                                try:
                                    bgr = np.ascontiguousarray(bgrx[:, :, :3])
                                    raw_frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
                                    # NVENC does not accept bgr24; reformat to
                                    # yuv420p first (libswscale, software).
                                    if self._settings.use_nvenc:
                                        av_frame = raw_frame.reformat(format="yuv420p")
                                    else:
                                        av_frame = raw_frame
                                    is_first = (v_pts == 0)
                                    av_frame.pts = v_pts
                                    v_pts += 1
                                    pkt_list = list(v_stream.encode(av_frame))
                                    if is_first and self._settings.use_nvenc:
                                        _dts_info = (
                                            f' DTS={pkt_list[0].dts} PTS={pkt_list[0].pts}'
                                            if pkt_list else ' (no packets — look-ahead still active?)'
                                        )
                                        print(f'[NDI] NVENC first encode:'
                                              f' {len(pkt_list)} pkt(s){_dts_info}', flush=True)
                                    for pkt in pkt_list:
                                        if v_dts_offset < 0:
                                            raw_dts = pkt.dts if pkt.dts is not None else 0
                                            v_dts_offset = max(0, -raw_dts)
                                            if v_dts_offset:
                                                print(f'[NDI] DTS offset = {v_dts_offset}'
                                                      f' (NVENC pipeline delay)', flush=True)
                                        if v_dts_offset > 0:
                                            if pkt.dts is not None:
                                                pkt.dts += v_dts_offset
                                            if pkt.pts is not None:
                                                pkt.pts += v_dts_offset
                                        container.mux(pkt)
                                    # After first video mux triggers write_header with correct
                                    # NVENC SPS/PPS extradata, flush any buffered audio packets.
                                    if audio_pkt_buf and v_dts_offset >= 0:
                                        for _ap in audio_pkt_buf:
                                            try:
                                                container.mux(_ap)
                                            except Exception as _ae:
                                                print(f'[NDI] buffered audio mux error: {_ae}', flush=True)
                                        audio_pkt_buf.clear()
                                except Exception as exc:
                                    print(f'[NDI] video encode error: {exc}', flush=True)
                except Exception:
                    pass

                _ndi.recv_free_video_v2(recv, v_frame)

            # ---------- audio ----------
            elif frame_type == _ndi.FRAME_TYPE_AUDIO and a_frame:
                if is_recording and container is not None and a_stream is not None:
                    try:
                        n_ch = a_frame.no_channels
                        n_samp = a_frame.no_samples
                        # channel_stride_in_bytes may include padding; use it
                        # to index each channel rather than assuming tight packing
                        stride_f32 = a_frame.channel_stride_in_bytes // 4
                        raw = np.frombuffer(a_frame.data, dtype=np.float32)
                        out_ch = min(n_ch, 2)
                        layout = "stereo" if out_ch == 2 else "mono"
                        af = av.AudioFrame(format="fltp", layout=layout, samples=n_samp)
                        af.sample_rate = a_frame.sample_rate
                        # AAC-LC has a 1024-sample encoder delay; shift PTS so
                        # the first output packet has DTS = 0, not −1024.
                        # Without this the muxer returns EINVAL (errno 22) for
                        # the first audio packet and corrupts the interleave queue.
                        _AAC_DELAY = 1024
                        af.pts = a_pts + _AAC_DELAY
                        a_pts += n_samp
                        for i in range(out_ch):
                            start = i * stride_f32
                            af.planes[i].update(raw[start: start + n_samp].tobytes())
                        for pkt in a_stream.encode(af):
                            if v_dts_offset >= 0:
                                container.mux(pkt)
                            else:
                                audio_pkt_buf.append(pkt)
                    except Exception as exc:
                        print(f'[NDI] audio encode error: {exc}', flush=True)

                _ndi.recv_free_audio_v2(recv, a_frame)

            elif frame_type == _ndi.FRAME_TYPE_NONE:
                no_signal_streak += 1
                if no_signal_streak >= 50:   # ~5 s
                    self.status_changed.emit("No Signal")
                    no_signal_streak = 0

        # ------------------------------------------------------------------
        # Shutdown
        # ------------------------------------------------------------------
        if container is not None:
            self._close_container(container, v_stream, a_stream)

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

    def _emit_preview(self, bgrx: np.ndarray, w: int, h: int):
        try:
            rgb = np.ascontiguousarray(bgrx[:, :, [2, 1, 0]])
            qi = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
            self.frame_ready.emit(qi.copy())
        except Exception:
            pass

    def _open_container(
        self, path: str, w: int, h: int, fps: float
    ) -> Tuple:
        c = av.open(path, mode="w", options={"movflags": "+faststart"})

        codec   = self._settings.effective_video_codec
        profile = self._settings.active_video_profile
        nvenc   = self._settings.use_nvenc

        vs = c.add_stream(codec, rate=round(fps))
        vs.width     = w
        vs.height    = h
        vs.bit_rate  = self._settings.video_bitrate_bps
        vs.time_base = Fraction(1, round(fps))

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
            opts = {"preset": "fast", "profile": profile,
                    "x265-params": "log-level=none:keyint=60"}
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
