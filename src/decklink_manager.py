"""
DeckLink device discovery via Windows COM (comtypes).
Targets Desktop Video SDK 12.x – 16.x (April 2026).

GUIDs are sourced from DeckLinkAPI.h (SDK 12.x, 2021) and remain stable
through Desktop Video 16.0.  SDK 9.x had different GUIDs and a 16-method
IDeckLinkInput vtable (no GetDisplayMode); that era is not supported here.

CLSID note: Blackmagic uses the interface IID as the coclass CLSID for the
iterator in DeckLinkAPIDispatch.cpp, so CLSID == IID_IDeckLinkIterator.
"""

import ctypes
from typing import List, Optional

DECKLINK_AVAILABLE = False
_comtypes_ok = False

try:
    import comtypes
    import comtypes.client
    from comtypes import GUID, IUnknown, HRESULT, COMMETHOD, COMObject
    _comtypes_ok = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# COM interface definitions
# ---------------------------------------------------------------------------
if _comtypes_ok:
    # SDK 12.x–16.x — CLSID == IID for the iterator coclass (BMD convention).
    # SDK 9.x used {1F2E109A-8F4F-49E4-9203-135595CB6FA5}; listed for reference only.
    _CLSID_CDeckLinkIterator = GUID('{50FB36CD-3063-4B73-BDBB-958087F2D8BA}')

    # IDeckLink: IID not used for vtable calls; only needed for explicit QI.
    # Value is from DeckLinkAPI.h SDK 12.x (Windows).
    class IDeckLink(IUnknown):
        _iid_ = GUID('{8C2C45AC-EA40-4C3E-9735-DB2F4C76AF0B}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'GetDisplayName',
                (['out'], ctypes.POINTER(ctypes.c_wchar_p), 'displayName')),
        ]

    # IID stable across all Desktop Video releases since SDK 11.
    class IDeckLinkIterator(IUnknown):
        _iid_ = GUID('{50FB36CD-3063-4B73-BDBB-958087F2D8BA}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'Next',
                (['out'], ctypes.POINTER(ctypes.POINTER(IDeckLink)), 'deckLinkInstance')),
        ]

    # SDK 12.x–16.x IID (changed from SDK 9.x {DD04E5EC-…}).
    class IDeckLinkInputCallback(IUnknown):
        _iid_ = GUID('{C6FCE4C9-C4E4-4047-82FB-5D238232A902}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'VideoInputFormatChanged',
                (['in'], ctypes.c_uint32, 'notificationEvents'),
                (['in'], ctypes.c_void_p, 'newDisplayMode'),
                (['in'], ctypes.c_uint32, 'detectedSignalFlags')),
            COMMETHOD([], HRESULT, 'VideoInputFrameArrived',
                (['in'], ctypes.c_void_p, 'videoFrame'),
                (['in'], ctypes.c_void_p, 'audioPacket')),
        ]

    # IDeckLinkInput SDK 12.x-16.x: 16 methods (slots 3-18 after IUnknown's 3).
    # GetDisplayMode is NOT a member of IDeckLinkInput — it belongs to
    # IDeckLinkDisplayMode.  The previous definition included it erroneously,
    # which shifted every subsequent slot by 1 and caused E_INVALIDARG on
    # SetCallback / EnableVideoInput.
    # Placeholder c_void_p params are for interfaces we never call directly.
    class IDeckLinkInput(IUnknown):
        _iid_ = GUID('{C21CDB6E-F414-46E4-A636-80A566E0ED37}')  # SDK 12.x-16.x
        _methods_ = [
            # slot 3 — SDK 12.x: 5 in-params + 2 out-params
            COMMETHOD([], HRESULT, 'DoesSupportVideoMode',
                (['in'],  ctypes.c_uint32, 'connection'),
                (['in'],  ctypes.c_uint32, 'requestedMode'),
                (['in'],  ctypes.c_uint32, 'requestedPixelFormat'),
                (['in'],  ctypes.c_uint32, 'conversionMode'),
                (['in'],  ctypes.c_uint32, 'flags'),
                (['out'], ctypes.c_void_p, 'resultMode'),
                (['out'], ctypes.POINTER(ctypes.c_bool), 'supported')),
            # slot 4 — present in Desktop Video 16.x (confirmed by vtable probe)
            COMMETHOD([], HRESULT, 'GetDisplayMode',
                (['out'], ctypes.c_void_p, 'theMode')),
            # slot 5
            COMMETHOD([], HRESULT, 'GetDisplayModeIterator',
                (['out'], ctypes.c_void_p, 'iterator')),
            # slot 6
            COMMETHOD([], HRESULT, 'SetScreenPreviewCallback',
                (['in'],  ctypes.c_void_p, 'previewCallback')),
            # slot 6
            COMMETHOD([], HRESULT, 'EnableVideoInput',
                (['in'],  ctypes.c_uint32, 'displayMode'),
                (['in'],  ctypes.c_uint32, 'pixelFormat'),
                (['in'],  ctypes.c_uint32, 'flags')),
            # slot 7
            COMMETHOD([], HRESULT, 'DisableVideoInput'),
            # slot 8
            COMMETHOD([], HRESULT, 'GetAvailableVideoFrameCount',
                (['out'], ctypes.POINTER(ctypes.c_uint32), 'availableFrameCount')),
            # slot 9
            COMMETHOD([], HRESULT, 'SetVideoInputFrameMemoryAllocator',
                (['in'],  ctypes.c_void_p, 'theAllocator')),
            # slot 10
            COMMETHOD([], HRESULT, 'EnableAudioInput',
                (['in'],  ctypes.c_uint32, 'sampleRate'),
                (['in'],  ctypes.c_uint32, 'sampleType'),
                (['in'],  ctypes.c_uint32, 'channelCount')),
            # slot 11
            COMMETHOD([], HRESULT, 'DisableAudioInput'),
            # slot 12
            COMMETHOD([], HRESULT, 'GetAvailableAudioSampleFrameCount',
                (['out'], ctypes.POINTER(ctypes.c_uint32), 'availableSampleFrameCount')),
            # slot 13
            COMMETHOD([], HRESULT, 'StartStreams'),
            # slot 14
            COMMETHOD([], HRESULT, 'StopStreams'),
            # slot 15
            COMMETHOD([], HRESULT, 'PauseStreams'),
            # slot 16
            COMMETHOD([], HRESULT, 'FlushStreams'),
            # slot 17 — accept c_void_p so a raw COM pointer can be passed directly
            COMMETHOD([], HRESULT, 'SetCallback',
                (['in'],  ctypes.c_void_p, 'theCallback')),
            # slot 18 — present since SDK 10.x
            COMMETHOD([], HRESULT, 'GetHardwareReferenceClock',
                (['in'],  ctypes.c_longlong, 'desiredTimeScale'),
                (['out'], ctypes.POINTER(ctypes.c_longlong), 'hardwareTime'),
                (['out'], ctypes.POINTER(ctypes.c_longlong), 'timeInFrame'),
                (['out'], ctypes.POINTER(ctypes.c_longlong), 'ticksPerFrame')),
        ]

# ---------------------------------------------------------------------------
# BMD constants (FourCC encoded as big-endian uint32)
# ---------------------------------------------------------------------------
def _bmd(s: str) -> int:
    return int.from_bytes(s.encode('ascii'), 'big')

BMD_MODE_DETECT                        = _bmd('dtet')
BMD_FORMAT_8BIT_BGRA                   = _bmd('BGRA')
BMD_FORMAT_8BIT_YUV                    = _bmd('2vuy')
BMD_AUDIO_SAMPLE_RATE_48K              = 48000
BMD_AUDIO_SAMPLE_TYPE_16BIT_INT        = 16
BMD_AUDIO_SAMPLE_TYPE_32BIT_INT        = 32
BMD_VIDEO_INPUT_FLAG_DEFAULT           = 0
BMD_VIDEO_INPUT_ENABLE_FORMAT_DETECT   = 1 << 0

# Common display modes (FourCC) — used as fallbacks when auto-detect is
# not supported by the hardware.
BMD_MODE_HD1080I5994 = _bmd('Hi59')   # 1080i 59.94
BMD_MODE_HD1080I50   = _bmd('Hi50')   # 1080i 50
BMD_MODE_HD1080P2997 = _bmd('Hp29')   # 1080p 29.97
BMD_MODE_HD1080P30   = _bmd('Hp30')   # 1080p 30
BMD_MODE_HD1080P25   = _bmd('Hp25')   # 1080p 25
BMD_MODE_HD1080P24   = _bmd('Hp24')   # 1080p 24
BMD_MODE_HD720P5994  = _bmd('hp59')   # 720p 59.94
BMD_MODE_HD720P60    = _bmd('hp60')   # 720p 60
BMD_MODE_HD720P50    = _bmd('hp50')   # 720p 50
BMD_MODE_NTSC        = _bmd('ntsc')   # NTSC
BMD_MODE_PAL         = _bmd('pal ')   # PAL

# IDeckLinkVideoFrame vtable indices (0-2 = IUnknown, then IDeckLinkVideoFrame)
_VTI_GET_WIDTH      = 3
_VTI_GET_HEIGHT     = 4
_VTI_GET_ROW_BYTES  = 5
_VTI_GET_BYTES      = 8   # GetBytes(void**) — index 8 in vtable

# IDeckLinkAudioInputPacket vtable indices
_VTI_AUDIO_SAMPLE_COUNT = 3   # GetSampleFrameCount() -> long
_VTI_AUDIO_GET_BYTES    = 4   # GetBytes(void**) -> HRESULT


# ---------------------------------------------------------------------------
# Raw vtable helpers (for frame/audio interfaces — avoids defining full IIDs)
# ---------------------------------------------------------------------------

def _vtbl_long(obj_ptr: int, idx: int) -> int:
    """Call a vtable method that takes no args and returns a C long."""
    vtbl = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn_ptr = ctypes.cast(ctypes.c_void_p(
        ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[idx]
    ), ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p))
    return fn_ptr(obj_ptr)


def _vtbl_get_bytes(obj_ptr: int, idx: int) -> Optional[int]:
    """Call a vtable GetBytes(void**) method, return raw buffer pointer or None."""
    vtbl = ctypes.cast(obj_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn_ptr = ctypes.cast(ctypes.c_void_p(
        ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[idx]
    ), ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p)))
    buf = ctypes.c_void_p()
    hr = fn_ptr(obj_ptr, ctypes.byref(buf))
    return buf.value if hr == 0 else None


# ---------------------------------------------------------------------------
# Public source class
# ---------------------------------------------------------------------------

class DeckLinkSource:
    """
    Represents one DeckLink capture device.

    Intentionally stores NO COM pointer.  COM objects are bound to the
    apartment that created them; sharing them across threads (main STA →
    worker MTA) requires message-pump marshalling that deadlocks during a
    Qt click handler.  DeckLinkWorker enumerates the device again on its
    own thread using _clsid + index instead.
    """

    def __init__(self, name: str, index: int, clsid):
        self.name   = name
        self.index  = index
        self._clsid = clsid   # CLSID to pass to worker for re-enumeration

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other) -> bool:
        return isinstance(other, DeckLinkSource) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class DeckLinkManager:
    """
    Enumerate DeckLink devices via COM.  Call initialize() first.

    Device enumeration is performed once during initialize() and cached.
    Re-doing it on demand (e.g. on a button click) causes COM STA deadlocks
    on multi-input cards such as the DeckLink Quad 2 because the driver's
    internal initialisation requires a Windows message pump that background
    threads do not have.  initialize() runs before app.exec() on the main
    thread, which is the only safe place to call DeckLink COM enumeration.
    """

    def __init__(self):
        self._available = False
        self._active_clsid = None
        self._cached_sources: List[DeckLinkSource] = []

    # Legacy SDK 9.x CLSID — tried as a fallback if the primary CLSID fails.
    _CLSID_LEGACY = '{1F2E109A-8F4F-49E4-9203-135595CB6FA5}'

    def initialize(self) -> bool:
        if not _comtypes_ok:
            return False
        # Note: do NOT call CoInitializeEx here.  QApplication.__init__ calls
        # OleInitialize which initialises the main thread as STA; calling
        # CoInitializeEx(COINIT_MULTITHREADED) afterwards returns
        # RPC_E_CHANGED_MODE and we'd silently stay STA anyway.
        for clsid in (_CLSID_CDeckLinkIterator, GUID(self._CLSID_LEGACY)):
            try:
                comtypes.client.CreateObject(clsid, interface=IDeckLinkIterator)
                self._active_clsid = clsid
                self._available = True
                # Enumerate now, on the main thread, before app.exec().
                # This is the only thread/time that can safely do DeckLink
                # COM enumeration without a message-pump deadlock.
                self._cached_sources = self._enumerate_sources()
                return True
            except Exception:
                continue
        return False

    @property
    def available(self) -> bool:
        return self._available

    def get_sources(self) -> List[DeckLinkSource]:
        """Return the cached device list (populated during initialize)."""
        return list(self._cached_sources)

    def _enumerate_sources(self) -> List[DeckLinkSource]:
        """
        Enumerate DeckLink devices using raw vtable calls.

        comtypes wraps a NULL COM pointer (returned by Next() on end-of-list)
        as a non-None Python object, so the normal `if device is None` check
        never fires and the loop runs until RAM is exhausted.  Calling vtable
        slots directly gives us a plain integer we can null-check with `if not
        dev_ptr.value`, which is unambiguous.
        """
        if not self._available or self._active_clsid is None:
            return []
        sources: List[DeckLinkSource] = []
        try:
            iterator = comtypes.client.CreateObject(
                self._active_clsid, interface=IDeckLinkIterator
            )
            # Extract the raw COM interface pointer (integer address).
            iter_ptr = ctypes.cast(iterator, ctypes.c_void_p).value
            if not iter_ptr:
                return []

            # CFUNCTYPE signatures for the three vtable slots we need.
            # slot 2 = Release(this) → ULONG
            # slot 3 = Next(this, IDeckLink** out) → HRESULT  (on iterator)
            # slot 3 = GetDisplayName(this, BSTR* out) → HRESULT  (on device)
            _RelFn  = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            _NextFn = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p))
            _NameFn = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p))

            def _fn(ptr, slot, ftype):
                vtbl = ctypes.cast(
                    ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
                addr = ctypes.cast(
                    vtbl, ctypes.POINTER(ctypes.c_void_p))[slot]
                return ctypes.cast(ctypes.c_void_p(addr), ftype)

            next_fn = _fn(iter_ptr, 3, _NextFn)

            for idx in range(64):          # Quad 2 has 8 inputs; 64 = hard cap
                dev_ptr = ctypes.c_void_p(0)
                hr = next_fn(iter_ptr, ctypes.byref(dev_ptr))

                # S_OK (0) + non-null pointer → valid device.
                # S_FALSE (1), error, or NULL pointer → end of list.
                if hr != 0 or not dev_ptr.value:
                    break

                dev = dev_ptr.value
                name = None
                try:
                    name_fn  = _fn(dev, 3, _NameFn)
                    out_str  = ctypes.c_wchar_p()
                    if name_fn(dev, ctypes.byref(out_str)) == 0:
                        name = out_str.value or None
                except Exception:
                    pass

                # Release the AddRef from Next() before moving on.
                try:
                    _fn(dev, 2, _RelFn)(dev)
                except Exception:
                    pass

                sources.append(
                    DeckLinkSource(name or f"DeckLink {idx}", idx, self._active_clsid))

        except Exception:
            pass

        # Deduplicate names — multi-input cards (e.g. DeckLink Quad 2) often
        # report the identical display name for every sub-device.  Append " (N)"
        # so each entry is unique and independently selectable in the UI.
        _cnt: dict = {}
        for s in sources:
            _cnt[s.name] = _cnt.get(s.name, 0) + 1
        _seen: dict = {}
        deduped: List[DeckLinkSource] = []
        for s in sources:
            if _cnt[s.name] > 1:
                _seen[s.name] = _seen.get(s.name, 0) + 1
                deduped.append(DeckLinkSource(
                    f"{s.name} ({_seen[s.name]})", s.index, s._clsid))
            else:
                deduped.append(s)
        return deduped
