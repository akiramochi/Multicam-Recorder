"""
Per-device DeckLink capture worker.
Exposes the same signals as StreamWorker so StreamTile works unchanged.
"""
import ctypes
import queue
import time
from datetime import datetime
from fractions import Fraction

import numpy as np
from PyQt6.QtCore import QThread, QMutex, pyqtSignal
from PyQt6.QtGui import QImage

from .recording_settings import RecordingSettings
from .decklink_manager import (
    IDeckLinkInput, IDeckLinkIterator,
    BMD_MODE_DETECT, BMD_FORMAT_8BIT_YUV,
    BMD_AUDIO_SAMPLE_RATE_48K, BMD_AUDIO_SAMPLE_TYPE_32BIT_INT,
    BMD_VIDEO_INPUT_FLAG_DEFAULT, BMD_VIDEO_INPUT_ENABLE_FORMAT_DETECT,
    BMD_MODE_HD1080I5994, BMD_MODE_HD1080I50,
    BMD_MODE_HD1080P2997, BMD_MODE_HD1080P30, BMD_MODE_HD1080P25, BMD_MODE_HD1080P24,
    BMD_MODE_HD720P5994, BMD_MODE_HD720P60, BMD_MODE_HD720P50,
    BMD_MODE_NTSC, BMD_MODE_PAL,
    _vtbl_long, _vtbl_get_bytes,
    _VTI_GET_WIDTH, _VTI_GET_HEIGHT, _VTI_GET_ROW_BYTES, _VTI_GET_BYTES,
    _VTI_AUDIO_SAMPLE_COUNT, _VTI_AUDIO_GET_BYTES,
    DeckLinkSource,
)

try:
    import comtypes
    _COMTYPES_OK = True
except ImportError:
    _COMTYPES_OK = False

# ---------------------------------------------------------------------------
# WINFUNCTYPE signatures for the manually constructed COM callback vtable.
# Using WINFUNCTYPE (stdcall) matches the COM calling convention on Windows.
# ---------------------------------------------------------------------------
_CB_QI_T   = ctypes.WINFUNCTYPE(ctypes.c_long,  ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p)
_CB_REF_T  = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_CB_FCH_T  = ctypes.WINFUNCTYPE(ctypes.c_long,  ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
_CB_FAR_T  = ctypes.WINFUNCTYPE(ctypes.c_long,  ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p)

# ---------------------------------------------------------------------------
# WINFUNCTYPE signatures for raw IDeckLinkInput vtable calls.
# Direct vtable dispatch avoids all comtypes method-table lookup, so slot
# numbers here are exactly what the driver sees.  The authoritative slot
# table is in _DL_SLOT below.
# ---------------------------------------------------------------------------
_DL_VOID1_T = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
_DL_CB_T    = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
_DL_EVI_T   = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
_DL_EAI_T   = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)

_DL_SLOT = {
    # Slot numbers for IDeckLinkInput in Desktop Video 16.x.
    # GetDisplayMode is present at slot 4, shifting all later methods by +1
    # relative to SDK 12.x references that omit it.
    # slots 0-2: IUnknown (QI / AddRef / Release)
    # slot  3: DoesSupportVideoMode
    # slot  4: GetDisplayMode          ← new in 16.x
    # slot  5: GetDisplayModeIterator
    # slot  6: SetScreenPreviewCallback
    # slot  7: EnableVideoInput
    # slot  8: DisableVideoInput
    # slot  9: GetAvailableVideoFrameCount
    # slot 10: SetVideoInputFrameMemoryAllocator
    # slot 11: EnableAudioInput
    # slot 12: DisableAudioInput
    # slot 13: GetAvailableAudioSampleFrameCount
    # slot 14: StartStreams
    # slot 15: StopStreams
    # slot 16: PauseStreams
    # slot 17: FlushStreams
    # slot 18: SetCallback
    # slot 19: GetHardwareReferenceClock
    'EnableVideoInput':  7,
    'DisableVideoInput': 8,
    'EnableAudioInput':  11,
    'DisableAudioInput': 12,
    'StartStreams':       14,
    'StopStreams':        15,
    'FlushStreams':       17,
    'SetCallback':        18,
}


def _dl_fn(raw_ptr: int, slot: int, fn_type):
    """
    Return a callable for vtable slot *slot* of the COM object at *raw_ptr*.
    Raises RuntimeError if the slot address is NULL (prevents silent AV).
    """
    vtbl = ctypes.cast(raw_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    addr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[slot]
    if not addr:
        raise RuntimeError(f'IDeckLinkInput vtable slot {slot} is NULL')
    return ctypes.cast(ctypes.c_void_p(addr), fn_type)


try:
    import av
    _AV_OK = True
except ImportError:
    _AV_OK = False

# Preview display size — must match stream_tile._PREVIEW_W / _PREVIEW_H.
# The worker subsamples to this resolution before colour conversion so that
# the worker loop stays fast enough to handle format_change events without
# blocking the DeckLink driver thread.
_PREVIEW_MAX_W = 320
_PREVIEW_MAX_H = 180


# ---------------------------------------------------------------------------
# UYVY ↔ YUV/RGB helpers (numpy-only — PyAV from_ndarray rejects uyvy422)
# ---------------------------------------------------------------------------

def _uyvy_to_rgb(uyvy: np.ndarray, w: int, h: int) -> np.ndarray:
    """
    Convert a UYVY (8-bit YUV 4:2:2 packed) array of shape (h, w*2) to
    an RGB24 array of shape (h, w, 3).  No PyAV required; pure numpy.

    UYVY byte layout per 4-byte macro-pixel: [U, Y0, V, Y1]
    BT.601 full-range YCbCr → RGB coefficients.
    """
    macro = uyvy.reshape(h, w // 2, 4).astype(np.float32)
    Y = np.empty((h, w), dtype=np.float32)
    Y[:, 0::2] = macro[:, :, 1]          # Y0
    Y[:, 1::2] = macro[:, :, 3]          # Y1
    U = np.repeat(macro[:, :, 0], 2, axis=1) - 128.0   # (h, w)
    V = np.repeat(macro[:, :, 2], 2, axis=1) - 128.0   # (h, w)
    R = np.clip(Y + 1.40200 * V,                   0.0, 255.0)
    G = np.clip(Y - 0.34414 * U - 0.71414 * V,    0.0, 255.0)
    B = np.clip(Y + 1.77200 * U,                   0.0, 255.0)
    return np.stack([R, G, B], axis=2).astype(np.uint8)


def _uyvy_to_yuv420p(uyvy: np.ndarray, w: int, h: int):
    """
    Convert a UYVY array (h, w*2) to three numpy planes ready for a
    yuv420p av.VideoFrame: Y (h×w), U (h//2 × w//2), V (h//2 × w//2).
    Vertical chroma downsampling averages adjacent row pairs.
    """
    macro = uyvy.reshape(h, w // 2, 4)
    Y = np.empty((h, w), dtype=np.uint8)
    Y[:, 0::2] = macro[:, :, 1]          # Y0
    Y[:, 1::2] = macro[:, :, 3]          # Y1
    U = macro[:, :, 0].astype(np.uint16)  # (h, w//2)
    V = macro[:, :, 2].astype(np.uint16)  # (h, w//2)
    # Vertical 2× downsample
    U_420 = ((U[0::2] + U[1::2]) >> 1).astype(np.uint8)   # (h//2, w//2)
    V_420 = ((V[0::2] + V[1::2]) >> 1).astype(np.uint8)   # (h//2, w//2)
    return Y, U_420, V_420


# ---------------------------------------------------------------------------
# Display-mode inference from geometry + frame rate
# ---------------------------------------------------------------------------

def _infer_display_mode(w: int, h: int, is_prog: bool,
                         dur: int, scale: int) -> int:
    """
    Return a BMDDisplayMode FourCC inferred from frame geometry and rate.
    dur/scale come from IDeckLinkDisplayMode::GetFrameRate().
    fps_k = fps × 1000 (integer, avoids float rounding).
    Returns 0 if the combination is not recognised.
    """
    if dur <= 0 or scale <= 0:
        return 0
    fps_k = (scale * 1000) // dur

    if w == 1920 and h == 1080:
        if is_prog:
            if 23960 <= fps_k < 23990:  return 0x32337073  # '23ps' 1080p23.98
            if fps_k == 24000:          return 0x32347073  # '24ps' 1080p24
            if fps_k == 25000:          return 0x48703235  # 'Hp25' 1080p25
            if 29950 <= fps_k < 29990:  return 0x48703239  # 'Hp29' 1080p29.97
            if fps_k == 30000:          return 0x48703330  # 'Hp30' 1080p30
            if fps_k == 50000:          return 0x48703530  # 'Hp50' 1080p50
            if 59900 <= fps_k < 59980:  return 0x48703539  # 'Hp59' 1080p59.94
            if fps_k == 60000:          return 0x48703630  # 'Hp60' 1080p60
        else:
            if 29950 <= fps_k < 29990:  return 0x48693539  # 'Hi59' 1080i59.94
            if fps_k == 25000:          return 0x48693530  # 'Hi50' 1080i50
            if fps_k == 30000:          return 0x48693630  # 'Hi60' 1080i60
    elif w == 1280 and h == 720:
        if fps_k == 50000:              return 0x68703530  # 'hp50' 720p50
        if 59900 <= fps_k < 59980:      return 0x68703539  # 'hp59' 720p59.94
        if fps_k == 60000:              return 0x68703630  # 'hp60' 720p60
    elif w == 720 and 480 <= h <= 490:
        return 0x6E747363  # 'ntsc'
    elif w == 720 and h == 576:
        return 0x70616C20  # 'pal '
    return 0


# ---------------------------------------------------------------------------
# COM callback — fires on the DeckLink driver thread
# ---------------------------------------------------------------------------

class _RawCallback:
    """
    IDeckLinkInputCallback implemented via a manually constructed COM vtable.

    comtypes COMObject has an ambiguous 'this' convention for non-IUnknown
    methods: depending on comtypes version and COMMETHOD flags it may or may
    not strip 'this' before calling the Python method.  When it does NOT strip
    it, the first explicit parameter receives the raw COM 'this' pointer rather
    than the video frame, causing an access violation in _vtbl_long.

    Building the vtable by hand with WINFUNCTYPE gives us full control: 'this'
    is always the first explicit parameter in our C-callable, exactly as the
    DeckLink driver passes it, with no comtypes magic in between.
    """

    def __init__(self, frame_q: queue.Queue):
        self._q = frame_q
        # One-shot guard: set True after queuing a format_change so that the
        # driver's "signal locked" re-fire of VideoInputFormatChanged (which
        # happens even with FLAG_DEFAULT) doesn't trigger a second restart.
        # Reset to False by the worker loop when the first video frame arrives.
        self._format_change_sent = False
        self._first_frame_logged = False   # diagnostic: log the first callback

        # Wrap each method as a C callable.  Keep strong references on self so
        # Python's GC never frees the underlying function pointers.
        self._fns = (
            _CB_QI_T(self._qi),
            _CB_REF_T(self._addref),
            _CB_REF_T(self._release),
            _CB_FCH_T(self._format_changed),
            _CB_FAR_T(self._frame_arrived),
        )

        # vtable: 5 consecutive function pointers (QI, AddRef, Release,
        # VideoInputFormatChanged, VideoInputFrameArrived).
        self._vtable = (ctypes.c_void_p * 5)(
            *(ctypes.cast(f, ctypes.c_void_p).value for f in self._fns)
        )

        # A COM object is a pointer to a vtable pointer.
        # _obj holds { vtable_ptr }; self.ptr is the address of _obj.
        self._obj = (ctypes.c_void_p * 1)(
            ctypes.cast(self._vtable, ctypes.c_void_p).value
        )
        self.ptr = ctypes.cast(self._obj, ctypes.c_void_p).value

    # IUnknown
    def _qi(self, this, riid, ppv):
        if ppv:
            ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p)).contents.value = this
        return 0  # S_OK — return ourselves for any QI

    def _addref(self, this):
        return 1

    def _release(self, this):
        return 1

    # IDeckLinkInputCallback
    def _format_changed(self, this, events, display_mode, signal_flags):
        # events bitmask: 0x01 = bmdVideoInputDisplayModeChanged
        # display_mode: raw IDeckLinkDisplayMode* pointer
        # signal_flags: BMDDetectedVideoInputFormatFlags
        #   bit 3 (0x08) = bmdDetectedVideoInputHighFrameRate (fps >= 50)
        #
        # IDeckLinkDisplayMode vtable confirmed for Desktop Video 16.x:
        #   slot 3: NEW (HRESULT, purpose unknown)
        #   slot 4: GetDisplayMode() → BMDDisplayMode FourCC  ← USE THIS
        #   slot 5: GetWidth()       → long
        #   slot 6: GetHeight()      → long
        #   slot 7: GetFrameRate(BMDTimeValue*, BMDTimeScale*) → HRESULT
        #   slot 8: GetFieldDominance() → BMDFieldDominance  (confirmed)
        #   slot 9: GetFlags()          → BMDDisplayModeFlags (confirmed)
        if not (display_mode and (events & 0x01)):
            return 0

        if self._format_change_sent:
            # Suppress the driver's "signal locked" re-fire that occurs even
            # with FLAG_DEFAULT.  The flag is cleared when the first video
            # frame arrives, so legitimate future signal changes are detected.
            print('[DeckLink] VideoInputFormatChanged suppressed '
                  '(restart already in progress)', flush=True)
            return 0

        try:
            # Read the BMDDisplayMode FourCC directly from slot 4.
            mode = _vtbl_long(display_mode, 4) & 0xFFFFFFFF

            # Sanity-check: all 4 bytes must be printable ASCII (0x20–0x7E).
            # If not, the slot layout differs; fall back to signal_flags hints.
            if not all(0x20 <= ((mode >> (8 * i)) & 0xFF) <= 0x7E
                       for i in range(4)):
                mode = 0

            if not mode:
                # slot 4 didn't yield a valid FourCC — use field dominance +
                # high-rate flag to pick the most common matching mode.
                is_prog  = (_vtbl_long(display_mode, 8) == 0x70726F67)
                high_fps = bool(signal_flags & 0x08)
                if is_prog:
                    mode = 0x48703539 if high_fps else 0x48703239  # Hp59 / Hp29
                else:
                    mode = 0x48693539 if high_fps else 0x48693530  # Hi59 / Hi50
                print(f'[DeckLink] slot 4 invalid — '
                      f'guessing from field_dom+high_fps', flush=True)

            try:
                lbl = mode.to_bytes(4, 'big').decode('ascii', errors='replace')
            except Exception:
                lbl = '????'
            print(f'[DeckLink] VideoInputFormatChanged → '
                  f'0x{mode:08X} ({lbl})', flush=True)
            self._format_change_sent = True
            self._q.put(('format_change', mode))

        except Exception as exc:
            print(f'[DeckLink] _format_changed error: {exc}', flush=True)
        return 0  # S_OK

    def _frame_arrived(self, this, video_ptr, audio_ptr):
        if not self._first_frame_logged:
            self._first_frame_logged = True
            if video_ptr:
                try:
                    _w  = _vtbl_long(video_ptr, _VTI_GET_WIDTH)
                    _h  = _vtbl_long(video_ptr, _VTI_GET_HEIGHT)
                    _rb = _vtbl_long(video_ptr, _VTI_GET_ROW_BYTES)
                    _b  = _vtbl_get_bytes(video_ptr, _VTI_GET_BYTES)
                    _pix = ''
                    if _b:
                        # Sample the first macro-pixel: layout = [U, Y0, V, Y1]
                        _s = (ctypes.c_ubyte * 8).from_address(_b)
                        _pix = (f' | macro0 U={_s[0]} Y0={_s[1]} V={_s[2]} Y1={_s[3]}'
                                f'  macro1 U={_s[4]} Y0={_s[5]} V={_s[6]} Y1={_s[7]}')
                    print(f'[DeckLink] _frame_arrived: '
                          f'video_ptr={video_ptr} w={_w} h={_h} rb={_rb} buf={_b}'
                          f'{_pix}', flush=True)
                except Exception as _de:
                    print(f'[DeckLink] _frame_arrived diag error: {_de}', flush=True)
            else:
                print(f'[DeckLink] _frame_arrived: video_ptr=NULL '
                      f'audio_ptr={audio_ptr}', flush=True)
        try:
            if video_ptr:
                w  = _vtbl_long(video_ptr, _VTI_GET_WIDTH)
                h  = _vtbl_long(video_ptr, _VTI_GET_HEIGHT)
                rb = _vtbl_long(video_ptr, _VTI_GET_ROW_BYTES)
                buf = _vtbl_get_bytes(video_ptr, _VTI_GET_BYTES)
                if buf and w > 0 and h > 0 and rb > 0:
                    # Capture monotonic timestamp before the memcpy so that
                    # PTS can be computed from actual arrival time even when
                    # frames are dropped by put_nowait.
                    _vts = time.monotonic()
                    raw  = (ctypes.c_ubyte * (h * rb)).from_address(buf)
                    arr  = np.frombuffer(raw, dtype=np.uint8).reshape((h, rb))
                    # UYVY (8-bit YUV 4:2:2): 2 bytes per pixel.
                    # Rows may be padded; trim to the active pixel columns.
                    uyvy = arr[:, : w * 2].copy()
                    # Use put_nowait: drop the frame if the queue is full rather
                    # than blocking the DeckLink driver thread.  A blocked
                    # driver thread cannot deliver the VideoInputFormatChanged
                    # callback, so format-change handling would stall forever.
                    try:
                        self._q.put_nowait(('video', uyvy, w, h, _vts))
                    except queue.Full:
                        pass

            if audio_ptr:
                n_samp = _vtbl_long(audio_ptr, _VTI_AUDIO_SAMPLE_COUNT)
                buf    = _vtbl_get_bytes(audio_ptr, _VTI_AUDIO_GET_BYTES)
                if buf and n_samp > 0:
                    _ats = time.monotonic()
                    # 2-channel 32-bit int PCM
                    raw = (ctypes.c_int32 * (n_samp * 2)).from_address(buf)
                    pcm = np.frombuffer(raw, dtype=np.int32).reshape(
                        (2, n_samp), order='F').copy()
                    try:
                        self._q.put_nowait(('audio', pcm, n_samp, _ats))
                    except queue.Full:
                        pass
        except Exception as _e:
            print(f'[DeckLink] _frame_arrived exception: {_e}', flush=True)
        return 0  # S_OK


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class DeckLinkWorker(QThread):
    """
    Captures one DeckLink device, emits preview frames, and optionally
    records to disk.  Identical signal set to StreamWorker.
    """

    frame_ready       = pyqtSignal(QImage)
    recording_started = pyqtSignal(str)
    recording_stopped = pyqtSignal()
    status_changed    = pyqtSignal(str)
    error_occurred    = pyqtSignal(str)
    stream_info       = pyqtSignal(int, int, float)

    _PREVIEW_FPS = 15

    def __init__(self, source: DeckLinkSource, parent=None):
        super().__init__(parent)
        self._source     = source
        self._settings   = RecordingSettings()
        self._mutex      = QMutex()
        self._running    = False
        self._start_flag = False
        self._stop_flag  = False
        self._pending_path = ''
        self._preview_interval = 1.0 / self._PREVIEW_FPS
        self._last_preview_ts  = 0.0

    # ------------------------------------------------------------------
    # Public API (GUI thread)
    # ------------------------------------------------------------------

    @property
    def source_name(self) -> str:
        return str(self._source)

    def configure(self, settings: RecordingSettings):
        self._settings = settings.copy()

    def start_recording(self, path: str):
        self._mutex.lock()
        self._pending_path = path
        self._start_flag   = True
        self._mutex.unlock()

    def stop_recording(self):
        self._mutex.lock()
        self._stop_flag = True
        self._mutex.unlock()

    def stop(self):
        self._mutex.lock()
        self._running   = False
        self._stop_flag = True
        self._mutex.unlock()

    def _err(self, msg: str):
        """Emit error to the UI tile and also print to terminal for diagnostics."""
        print(f'[DeckLink:{self._source.name}] {msg}', flush=True)
        self.error_occurred.emit(msg)

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def run(self):
        if not _COMTYPES_OK or not _AV_OK:
            self._err('comtypes or av not available')
            return

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass

        # Enumerate on THIS thread so all COM objects live in the same MTA.
        # Never reuse a COM pointer created on the main STA thread — that
        # causes a message-pump deadlock when Qt's click handler is on the call stack.
        try:
            iterator = comtypes.client.CreateObject(
                self._source._clsid, interface=IDeckLinkIterator
            )
            device = None
            for _ in range(self._source.index + 1):
                try:
                    device = iterator.Next()
                except comtypes.COMError:
                    device = None
                    break

            # comtypes wraps a NULL COM pointer (S_FALSE end-of-list) as a
            # non-None Python object, so `is None` alone is not reliable.
            # Cast to c_void_p for an unambiguous integer null check.
            _dev_addr = None
            if device is not None:
                try:
                    _dev_addr = ctypes.cast(device, ctypes.c_void_p).value
                except Exception:
                    pass
            if not _dev_addr:
                self._err(
                    f'DeckLink device "{self._source.name}" not found during capture init.'
                )
                return
            dl_input = device.QueryInterface(IDeckLinkInput)

            # comtypes can return a non-None but NULL-wrapped pointer when QI
            # returns S_OK with a zero ppv.  Cast to integer for a safe check.
            _dl_ptr = ctypes.cast(dl_input, ctypes.c_void_p).value
            if not _dl_ptr:
                raise RuntimeError(
                    'IDeckLinkInput QueryInterface returned NULL '
                    '(IID may not match Desktop Video 16.x).'
                )
        except Exception as e:
            self._err(f'DeckLink init failed: {e}')
            return

        frame_q: queue.Queue = queue.Queue(maxsize=8)
        callback = _RawCallback(frame_q)

        # Use raw vtable calls so slot numbers are explicit — comtypes method
        # dispatch can land on the wrong function when the SDK vtable layout
        # differs from our interface definition (e.g. Desktop Video 16.x).
        try:
            hr = _dl_fn(_dl_ptr, _DL_SLOT['SetCallback'], _DL_CB_T)(
                _dl_ptr, callback.ptr)
            if hr != 0:
                raise RuntimeError(f'SetCallback: 0x{hr & 0xFFFFFFFF:08X}')

            # Quad 2 delivers native 8-bit YUV (UYVY / '2vuy').
            # Strategy:
            #   1. Try 'dtet' (bmdModeDetectFormat) with detection flag — works on
            #      many cards but Quad 2 often rejects it (E_INVALIDARG).
            #   2. Try each common specific mode WITH bmdVideoInputEnableFormatDetection.
            #      Even if the mode doesn't match the live signal, the driver calls
            #      VideoInputFormatChanged with the actual format; our callback then
            #      does a Stop→Disable→Enable(correct_mode)→Start restart.
            #   3. Last resort: try without detection flag — mode must exactly match
            #      the signal or no frames will arrive.
            _EVI = _dl_fn(_dl_ptr, _DL_SLOT['EnableVideoInput'], _DL_EVI_T)
            _D = BMD_VIDEO_INPUT_ENABLE_FORMAT_DETECT
            _N = BMD_VIDEO_INPUT_FLAG_DEFAULT
            _MODES = [
                # Pass 1: auto-detect mode (shortcut, may be unsupported on Quad 2)
                (BMD_MODE_DETECT,      _D, 'auto-detect'),
                # Pass 2: specific modes + format detection flag
                # (driver fires VideoInputFormatChanged when signal differs from hint)
                (BMD_MODE_HD1080I5994, _D, '1080i59.94+det'),
                (BMD_MODE_HD1080I50,   _D, '1080i50+det'),
                (BMD_MODE_HD1080P2997, _D, '1080p29.97+det'),
                (BMD_MODE_HD1080P30,   _D, '1080p30+det'),
                (BMD_MODE_HD1080P25,   _D, '1080p25+det'),
                (BMD_MODE_HD1080P24,   _D, '1080p24+det'),
                (BMD_MODE_HD720P5994,  _D, '720p59.94+det'),
                (BMD_MODE_HD720P60,    _D, '720p60+det'),
                (BMD_MODE_HD720P50,    _D, '720p50+det'),
                (BMD_MODE_NTSC,        _D, 'NTSC+det'),
                (BMD_MODE_PAL,         _D, 'PAL+det'),
                # Pass 3: specific modes without detection (last resort)
                (BMD_MODE_HD1080I5994, _N, '1080i59.94'),
                (BMD_MODE_HD1080I50,   _N, '1080i50'),
                (BMD_MODE_HD1080P2997, _N, '1080p29.97'),
                (BMD_MODE_HD1080P30,   _N, '1080p30'),
                (BMD_MODE_HD1080P25,   _N, '1080p25'),
                (BMD_MODE_HD1080P24,   _N, '1080p24'),
                (BMD_MODE_HD720P5994,  _N, '720p59.94'),
                (BMD_MODE_HD720P60,    _N, '720p60'),
                (BMD_MODE_HD720P50,    _N, '720p50'),
                (BMD_MODE_NTSC,        _N, 'NTSC'),
                (BMD_MODE_PAL,         _N, 'PAL'),
            ]
            hr = -1
            for _mode, _flags, _label in _MODES:
                hr = _EVI(_dl_ptr, _mode, BMD_FORMAT_8BIT_YUV, _flags)
                print(f'[DeckLink] EnableVideoInput({_label}): '
                      f'0x{hr & 0xFFFFFFFF:08X}', flush=True)
                if hr == 0:
                    break
            if hr != 0:
                raise RuntimeError('EnableVideoInput: all modes failed')

            hr = _dl_fn(_dl_ptr, _DL_SLOT['EnableAudioInput'], _DL_EAI_T)(
                _dl_ptr, BMD_AUDIO_SAMPLE_RATE_48K,
                BMD_AUDIO_SAMPLE_TYPE_32BIT_INT, 2)
            if hr != 0:
                raise RuntimeError(f'EnableAudioInput: 0x{hr & 0xFFFFFFFF:08X}')

            hr = _dl_fn(_dl_ptr, _DL_SLOT['StartStreams'], _DL_VOID1_T)(_dl_ptr)
            if hr != 0:
                raise RuntimeError(f'StartStreams: 0x{hr & 0xFFFFFFFF:08X}')
        except Exception as e:
            self._err(f'DeckLink start failed: {e}')
            return

        self._running = True
        self.status_changed.emit('Connected')

        container    = None
        v_stream     = None
        a_stream     = None
        is_recording = False
        # v_rec_start  : monotonic timestamp of the first recorded video frame.
        # v_pts_mult   : 1 / codec_context.time_base  (set after the codec
        #                opens on the first encode call).  Deriving it from the
        #                codec — not the container stream — handles B-frame
        #                time-base doubling (HIGH profile libx264 uses 1/60
        #                instead of 1/30, causing 2× fast-forward if ignored).
        v_rec_start: float = 0.0
        v_pts_mult:  float = 0.0   # populated after first encode
        v_last_pts:  int   = -1    # last input PTS used; enforces strict monotonicity
        a_pts:       int   = 0     # monotonic audio sample counter (reset each recording)
        v_dts_offset: int  = -1    # shift so first video DTS = 0; -1 = not yet seen
        audio_pkt_buf      = []    # encoded audio pkts held until first video pkt muxed
        first_frame  = True
        output_path  = ''
        # Diagnostic counters (limit log spam to first few events per recording).
        v_dbg_pkt_count   = 0      # video packets logged so far
        v_err_count       = 0      # video encode errors logged so far

        while self._running:
            # flags from GUI thread
            self._mutex.lock()
            s_req = self._start_flag;  self._start_flag = False
            x_req = self._stop_flag;   self._stop_flag  = False
            ppath = self._pending_path
            self._mutex.unlock()

            if s_req and not is_recording:
                output_path  = ppath
                is_recording = True
                self.status_changed.emit('Waiting for frame…')
                self.recording_started.emit(output_path)

            if x_req and is_recording:
                container, v_stream, a_stream = self._close_container(
                    container, v_stream, a_stream)
                is_recording = False
                v_rec_start  = 0.0
                v_pts_mult   = 0.0
                v_last_pts   = -1
                a_pts        = 0
                v_dts_offset = -1
                audio_pkt_buf = []
                self.status_changed.emit('Connected')
                self.recording_stopped.emit()

            # drain the frame queue
            try:
                item = frame_q.get(timeout=0.1)
            except queue.Empty:
                continue

            kind = item[0]

            if kind == 'video':
                _, uyvy, w, h, frame_ts = item
                if first_frame:
                    first_frame = False
                    # First frame after a restart: allow VideoInputFormatChanged
                    # to fire again so genuine signal changes are handled.
                    callback._format_change_sent = False
                    self.stream_info.emit(w, h, 29.97)  # fps updated on format change

                now = time.monotonic()
                if now - self._last_preview_ts >= self._preview_interval:
                    self._last_preview_ts = now
                    self._emit_preview(uyvy, w, h)

                if is_recording:
                    if container is None:
                        try:
                            container, v_stream, a_stream = self._open_container(
                                output_path, w, h, 29.97)
                            v_rec_start = frame_ts   # anchor for PTS calculation
                            # For NVENC, _open_container pre-opens the codec,
                            # so codec_context.time_base is valid NOW.  Discover
                            # v_pts_mult here, BEFORE the first encode, so every
                            # frame (including frame 1) gets a unique PTS.
                            # Without this, NVENC's 1-frame pipeline delay means
                            # frames 1 AND 2 both go in with vf.pts=0 (the
                            # "anchor" branch below), NVENC emits two output
                            # packets with PTS=0, and the MOV muxer rejects the
                            # duplicate with EINVAL on the second video mux.
                            if v_pts_mult == 0.0:
                                try:
                                    ctb = v_stream.codec_context.time_base
                                    if ctb and float(ctb) > 0.0:
                                        v_pts_mult = 1.0 / float(ctb)
                                        print(f'[DeckLink] post-open '
                                              f'codec_context.time_base={ctb} '
                                              f'v_pts_mult={v_pts_mult:.1f}',
                                              flush=True)
                                except Exception:
                                    pass
                            self.status_changed.emit('Recording')
                        except Exception as exc:
                            self._err(f'Cannot open output: {exc}')
                            is_recording = False
                            self.status_changed.emit('Connected')
                            self.recording_stopped.emit()

                    if container and v_stream:
                        try:
                            Y, U, V = _uyvy_to_yuv420p(uyvy, w, h)
                            vf = av.VideoFrame(w, h, 'yuv420p')
                            vf.planes[0].update(Y.tobytes())
                            vf.planes[1].update(U.tobytes())
                            vf.planes[2].update(V.tobytes())
                            # PTS must be in codec_context.time_base units.
                            # libx264 HIGH profile doubles its internal time_base
                            # (1/30 → 1/60) to express B-frame display order; using
                            # the container stream time_base instead would give PTS
                            # values 2× too small, causing 2× fast-forward playback.
                            # v_pts_mult = 1 / codec_context.time_base is read after
                            # the first encode (when the codec is fully open).
                            if v_pts_mult > 0.0:
                                # Compute PTS from the wall-clock timestamp, but
                                # enforce strict monotonicity.  At 29.97 fps with
                                # a 1/30 codec time-base, two consecutive frames
                                # whose wall-clock timestamps are within ~16 ms of
                                # each other (driver jitter, startup bursts) round
                                # to the same integer tick.  libx264 silently
                                # coalesces such inputs; NVENC emits one packet per
                                # input frame and the duplicate DTS makes the MOV
                                # muxer return EINVAL.  Clamping new_pts to
                                # last_pts + 1 keeps every PTS unique and the
                                # cumulative drift bounded by total frame count.
                                new_pts = max(0, round(
                                    (frame_ts - v_rec_start) * v_pts_mult))
                                if new_pts <= v_last_pts:
                                    new_pts = v_last_pts + 1
                                v_last_pts = new_pts
                                vf.pts = new_pts
                            else:
                                vf.pts = 0  # first frame — anchor point
                                v_last_pts = 0
                            pkt_list = list(v_stream.encode(vf))
                            if v_dbg_pkt_count < 5 and self._settings.use_nvenc:
                                if pkt_list:
                                    _p0 = pkt_list[0]
                                    print(f'[DeckLink] DBG encode→{len(pkt_list)}pkt '
                                          f'in.pts={vf.pts} '
                                          f'out.dts={_p0.dts} out.pts={_p0.pts}',
                                          flush=True)
                                else:
                                    print(f'[DeckLink] DBG encode→0pkt '
                                          f'in.pts={vf.pts} (pipeline buffer)',
                                          flush=True)
                            for pkt in pkt_list:
                                # NVENC has an internal pipeline delay that
                                # makes the first output packet's DTS negative
                                # (DTS = PTS − delay).  On the very first
                                # packet we compute a fixed shift to bring
                                # DTS to 0; the same shift is applied to every
                                # subsequent packet so DTS stays monotonically
                                # ≥ 0.  For no-B-frame software codecs the
                                # first DTS is already 0 so v_dts_offset = 0
                                # and nothing changes.
                                if v_dts_offset < 0:   # first packet ever seen
                                    raw_dts = pkt.dts if pkt.dts is not None else 0
                                    v_dts_offset = max(0, -raw_dts)
                                    if v_dts_offset:
                                        print(f'[DeckLink] DTS offset = {v_dts_offset}'
                                              f' (NVENC pipeline delay)', flush=True)
                                if v_dts_offset > 0:
                                    if pkt.dts is not None:
                                        pkt.dts += v_dts_offset
                                    if pkt.pts is not None:
                                        pkt.pts += v_dts_offset
                                # Diagnostic: log pkt timing & mux result for
                                # the first few packets, so we can see whether
                                # write_header succeeded and what DTS/PTS values
                                # the muxer is being given.
                                _dbg = v_dbg_pkt_count < 5
                                if _dbg:
                                    print(f'[DeckLink] DBG pre-mux #{v_dbg_pkt_count} '
                                          f'dts={pkt.dts} pts={pkt.pts} '
                                          f'size={pkt.size} key={pkt.is_keyframe}',
                                          flush=True)
                                try:
                                    container.mux(pkt)
                                    if _dbg:
                                        print(f'[DeckLink] DBG muxed #{v_dbg_pkt_count} OK',
                                              flush=True)
                                    v_dbg_pkt_count += 1
                                except Exception as _me:
                                    if v_err_count < 3:
                                        print(f'[DeckLink] DBG mux FAIL #{v_dbg_pkt_count} '
                                              f'dts={pkt.dts} pts={pkt.pts}: {_me}',
                                              flush=True)
                                    v_err_count += 1
                                    raise
                            # Once the first video packet has been muxed it
                            # triggered avformat_write_header, so NVENC's SPS/PPS
                            # extradata is now committed to the container header.
                            # Flush any audio packets that were buffered while we
                            # waited — if they had triggered write_header first the
                            # video codecpar (extradata) might not have been synced
                            # yet, producing a broken video track and EINVAL on
                            # every subsequent video mux.
                            if audio_pkt_buf and v_dts_offset >= 0:
                                for _ap in audio_pkt_buf:
                                    try:
                                        container.mux(_ap)
                                    except Exception as _ae:
                                        print(f'[DeckLink] buffered audio mux'
                                              f' error: {_ae}', flush=True)
                                audio_pkt_buf.clear()
                            # Codec opens on first encode; read its actual time_base
                            # now so all subsequent PTS values use the right scale.
                            if v_pts_mult == 0.0:
                                try:
                                    ctb = v_stream.codec_context.time_base
                                    if ctb and float(ctb) > 0.0:
                                        v_pts_mult = 1.0 / float(ctb)
                                    else:
                                        raise ValueError(f'invalid codec tb: {ctb}')
                                    print(f'[DeckLink] codec_context.time_base={ctb} '
                                          f'v_pts_mult={v_pts_mult:.1f}', flush=True)
                                except Exception as _te:
                                    # Fallback: use whatever stream time_base reports
                                    try:
                                        stb = v_stream.time_base
                                        if stb and float(stb) > 0.0:
                                            v_pts_mult = 1.0 / float(stb)
                                    except Exception:
                                        v_pts_mult = 30.0  # last-resort: 30 fps
                                    print(f'[DeckLink] codec_context.time_base '
                                          f'unavailable ({_te}); '
                                          f'v_pts_mult={v_pts_mult:.1f}', flush=True)
                        except Exception as exc:
                            if v_err_count < 3:
                                print(f'[DeckLink] video encode error: {exc}', flush=True)
                            v_err_count += 1

            elif kind == 'audio':
                _, pcm, n_samp, audio_ts = item
                if is_recording and container and a_stream:
                    try:
                        # pcm is int32, shape (2, n_samp); convert to float32 planar
                        flt = pcm.astype(np.float32) / 2**31
                        af  = av.AudioFrame(format='fltp', layout='stereo', samples=n_samp)
                        af.sample_rate = BMD_AUDIO_SAMPLE_RATE_48K
                        # Use a monotonic sample counter for audio PTS.
                        # The previous timestamp-based approach
                        # (audio_ts − v_rec_start, clamped to ≥0) produced
                        # duplicate PTS values when several audio frames in
                        # the queue had timestamps before v_rec_start — they
                        # all mapped to PTS=0.  The AAC encoder passed them
                        # through but the resulting output packets had
                        # identical DTS, which the MOV/MP4 muxer rejected
                        # with EINVAL (errno 22), corrupting the interleave
                        # queue and causing all subsequent mux calls to fail.
                        #
                        # The +1024 shift compensates for AAC-LC's built-in
                        # 1024-sample encoder delay so the first output packet
                        # has DTS=0 (not −1024).  The encoder writes
                        # initial_padding=1024; the muxer records an edit list
                        # (elst) at write_trailer for correct player AV sync.
                        _AAC_DELAY = 1024
                        af.pts  = a_pts + _AAC_DELAY
                        a_pts  += n_samp
                        af.planes[0].update(flt[0].tobytes())
                        af.planes[1].update(flt[1].tobytes())
                        for pkt in a_stream.encode(af):
                            # Hold audio packets until the first video packet
                            # has been muxed (and write_header triggered with
                            # correct video codecpar).  After that, mux directly.
                            if v_dts_offset >= 0:
                                container.mux(pkt)
                            else:
                                audio_pkt_buf.append(pkt)
                    except Exception as exc:
                        print(f'[DeckLink] audio encode error: {exc}', flush=True)

            elif kind == 'format_change':
                # VideoInputFormatChanged fired — restart with the correct mode.
                # SDK-recommended sequence: StopStreams → EnableVideoInput(new)
                #   → FlushStreams → StartStreams.
                # Do NOT call DisableVideoInput here — that unregisters the
                # callback set by SetCallback, so no frames arrive after restart.
                new_mode = item[1]
                # Close any active recording first (resolution/fps may change).
                if is_recording:
                    container, v_stream, a_stream = self._close_container(
                        container, v_stream, a_stream)
                    is_recording = False
                    v_rec_start  = 0.0
                    v_pts_mult   = 0.0
                    v_last_pts   = -1
                    a_pts        = 0
                    v_dts_offset = -1
                    audio_pkt_buf = []
                    self.recording_stopped.emit()
                try:
                    _dl_fn(_dl_ptr, _DL_SLOT['StopStreams'], _DL_VOID1_T)(_dl_ptr)
                    # Use FLAG_DEFAULT (no format detection) on restart to
                    # prevent the driver re-firing VideoInputFormatChanged and
                    # causing an infinite restart loop.
                    hr = _dl_fn(_dl_ptr, _DL_SLOT['EnableVideoInput'], _DL_EVI_T)(
                        _dl_ptr, new_mode, BMD_FORMAT_8BIT_YUV,
                        BMD_VIDEO_INPUT_FLAG_DEFAULT)
                    print(f'[DeckLink] Re-EnableVideoInput(0x{new_mode:08X}): '
                          f'0x{hr & 0xFFFFFFFF:08X}', flush=True)
                    if hr == 0:
                        # Re-register the callback — EnableVideoInput (or
                        # StopStreams) clears it, so frames go nowhere without
                        # this call.
                        hr_cb = _dl_fn(_dl_ptr, _DL_SLOT['SetCallback'], _DL_CB_T)(
                            _dl_ptr, callback.ptr)
                        print(f'[DeckLink] SetCallback (format change): '
                              f'0x{hr_cb & 0xFFFFFFFF:08X}', flush=True)
                        _dl_fn(_dl_ptr, _DL_SLOT['FlushStreams'],  _DL_VOID1_T)(_dl_ptr)
                        hr2 = _dl_fn(_dl_ptr, _DL_SLOT['StartStreams'], _DL_VOID1_T)(_dl_ptr)
                        print(f'[DeckLink] StartStreams (format change): '
                              f'0x{hr2 & 0xFFFFFFFF:08X}', flush=True)
                        callback._first_frame_logged = False  # re-arm diagnostic
                        first_frame = True
                        self.status_changed.emit('Connected')
                    else:
                        self._err(
                            f'Format-change re-enable failed: 0x{hr & 0xFFFFFFFF:08X}')
                except Exception as exc:
                    print(f'[DeckLink] Format-change restart failed: {exc}', flush=True)

        # shutdown — raw vtable calls (same reason as startup)
        try:
            _dl_fn(_dl_ptr, _DL_SLOT['StopStreams'],        _DL_VOID1_T)(_dl_ptr)
            _dl_fn(_dl_ptr, _DL_SLOT['DisableVideoInput'],  _DL_VOID1_T)(_dl_ptr)
            _dl_fn(_dl_ptr, _DL_SLOT['DisableAudioInput'],  _DL_VOID1_T)(_dl_ptr)
        except Exception:
            pass

        if container:
            self._close_container(container, v_stream, a_stream)

    # ------------------------------------------------------------------

    def _emit_preview(self, uyvy: np.ndarray, w: int, h: int):
        try:
            # Subsample to preview resolution BEFORE colour conversion.
            # For 1920×1080 → 320×180 this is a 36× reduction, keeping the
            # worker fast enough that the queue never backs up and stalls the
            # DeckLink driver thread (which delivers format_change callbacks).
            rs    = max(1, h // _PREVIEW_MAX_H)          # row step
            cs    = max(1, (w // 2) // (_PREVIEW_MAX_W // 2))  # macro-pixel step
            small = uyvy[::rs, :]                         # (hs, w*2)
            hs    = small.shape[0]
            macro = (small.reshape(hs, w // 2, 4)[:, ::cs, :]
                     .astype(np.float32))                 # (hs, ws//2, 4)
            ws    = macro.shape[1] * 2
            Y     = np.empty((hs, ws), dtype=np.float32)
            Y[:, 0::2] = macro[:, :, 1]                  # Y0
            Y[:, 1::2] = macro[:, :, 3]                  # Y1
            U     = np.repeat(macro[:, :, 0], 2, axis=1) - 128.0
            V     = np.repeat(macro[:, :, 2], 2, axis=1) - 128.0
            R     = np.clip(Y + 1.40200 * V,                 0.0, 255.0)
            G     = np.clip(Y - 0.34414 * U - 0.71414 * V,  0.0, 255.0)
            B     = np.clip(Y + 1.77200 * U,                 0.0, 255.0)
            rgb   = np.stack([R, G, B], axis=2).astype(np.uint8)
            qi    = QImage(rgb.data, ws, hs, ws * 3,
                           QImage.Format.Format_RGB888)
            self.frame_ready.emit(qi.copy())
        except Exception as _e:
            print(f'[DeckLink] _emit_preview error: {type(_e).__name__}: {_e}',
                  flush=True)

    def _open_container(self, path, w, h, fps):
        c       = av.open(path, mode='w', options={'movflags': '+faststart'})
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
        if not nvenc and codec == 'libx265' and profile == 'main10':
            vs.pix_fmt = 'yuv420p10le'
        else:
            vs.pix_fmt = 'yuv420p'

        if nvenc:
            # bf=0 → no B-frames (frameIntervalP=1); rc-lookahead=0 → no
            # look-ahead buffering.  Both together ensure every encode() call
            # yields exactly one output packet with DTS == PTS.
            vs.codec_context.max_b_frames = 0
            opts = {'bf': '0', 'rc-lookahead': '0'}
            # H.264 NVENC accepts an explicit profile option.
            # H.265 NVENC infers the profile from pix_fmt (yuv420p → main).
            if self._settings.video_codec.value == 'libx264':
                opts['profile'] = profile
        elif codec == 'libx264':
            opts = {'preset': 'fast', 'profile': profile, 'tune': 'zerolatency'}
        else:  # libx265
            opts = {'preset': 'fast', 'profile': profile,
                    'x265-params': 'log-level=none:keyint=60'}
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
                # close→reopen on multiple streams can leave the session
                # counter transiently above the cap.  Give the driver up to
                # ~1.2s to release before giving up.
                _open_err = None
                for _attempt in range(3):
                    try:
                        vs.codec_context.open()
                        _open_err = None
                        break
                    except Exception as _re:
                        _open_err = _re
                        if _attempt < 2:
                            print(f'[DeckLink] NVENC open attempt '
                                  f'{_attempt + 1} failed ({_re}); '
                                  f'retrying in 400ms…', flush=True)
                            time.sleep(0.4)
                if _open_err is not None:
                    raise _open_err
                _ed = vs.codec_context.extradata
                print(f'[DeckLink] NVENC codec pre-opened; '
                      f'extradata={len(_ed) if _ed else 0}B', flush=True)
            except Exception as _e:
                # NVENC failed to initialise (common causes: concurrent-session
                # cap reached on consumer GeForce GPUs — ~3 HEVC / ~5 H.264 —
                # or transient driver state after a previous session).  The
                # codec_context is now in a partially-initialised state where
                # every subsequent encode() call will raise the same error and
                # the audio buffer would grow without bound while waiting for a
                # video packet that never arrives, eventually crashing the
                # process.  Tear down the half-built container and re-raise so
                # the caller aborts this recording cleanly with a visible
                # error and the GPU resources are released.
                print(f'[DeckLink] NVENC pre-open FAILED: {_e}', flush=True)
                print(f'[DeckLink] Aborting recording.  If this is HEVC with '
                      f'multiple streams, your GPU may have hit its concurrent '
                      f'NVENC session cap — try H.264 or fewer streams.',
                      flush=True)
                try:
                    c.close()
                except Exception:
                    pass
                raise

        astr = c.add_stream('aac', rate=BMD_AUDIO_SAMPLE_RATE_48K)
        astr.bit_rate  = self._settings.audio_bitrate_bps
        astr.layout    = 'stereo'
        astr.time_base = Fraction(1, BMD_AUDIO_SAMPLE_RATE_48K)
        return c, vs, astr

    def _close_container(self, container, v_stream, a_stream):
        if container is None:
            return None, None, None
        try:
            if v_stream:
                for pkt in v_stream.encode(): container.mux(pkt)
            if a_stream:
                for pkt in a_stream.encode(): container.mux(pkt)
        except Exception:
            pass
        try:
            container.close()
        except Exception:
            pass
        return None, None, None
