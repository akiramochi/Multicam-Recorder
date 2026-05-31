import threading
from typing import Callable, List, Optional

try:
    import NDIlib as _ndi
    NDI_AVAILABLE = True
except ImportError:
    _ndi = None
    NDI_AVAILABLE = False


class NDISource:
    """Thin wrapper around a raw NDI source object."""

    def __init__(self, raw_source):
        self._raw = raw_source
        self.name: str = getattr(raw_source, "ndi_name", str(raw_source))
        self.url: str = getattr(raw_source, "url_address", "")

    @property
    def raw(self):
        return self._raw

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other) -> bool:
        return isinstance(other, NDISource) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)


class NDIManager:
    """Handles NDI initialisation and continuous source discovery."""

    def __init__(self):
        self._finder = None
        self._initialized = False
        self._running = False
        self._lock = threading.Lock()
        self._sources: List[NDISource] = []
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[List[NDISource]], None]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        if not NDI_AVAILABLE:
            return False
        if not _ndi.initialize():
            return False
        self._finder = _ndi.find_create_v2()
        if not self._finder:
            _ndi.destroy()
            return False
        self._initialized = True
        return True

    def destroy(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self._finder:
            _ndi.find_destroy(self._finder)
            self._finder = None
        if self._initialized:
            _ndi.destroy()
            self._initialized = False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def start_discovery(self, callback: Callable[[List[NDISource]], None]):
        """Start a background thread that continuously refreshes the source list."""
        self._callback = callback
        self._running = True
        self._thread = threading.Thread(target=self._discovery_loop, daemon=True)
        self._thread.start()

    def _discovery_loop(self):
        while self._running and self._initialized:
            _ndi.find_wait_for_sources(self._finder, 1000)
            raw = _ndi.find_get_current_sources(self._finder)
            sources = [NDISource(s) for s in raw] if raw else []
            with self._lock:
                self._sources = sources
            if self._callback:
                self._callback(sources)

    def get_current_sources(self) -> List[NDISource]:
        with self._lock:
            return list(self._sources)

    def get_source_by_name(self, name: str) -> Optional[NDISource]:
        with self._lock:
            for s in self._sources:
                if s.name == name:
                    return s
        return None
