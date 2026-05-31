"""
OSC server / client for Bitfocus Companion integration.

Incoming paths  (Companion → app):
  /start_all   — start all recordings
  /stop_all    — stop all recordings

Outgoing feedback  (app → Companion):
  /multicam/recording        int  1 = any stream recording, 0 = none
  /multicam/recording_count  int  number of streams currently recording
  /multicam/source_count     int  total number of sources
"""
import threading
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

OSC_AVAILABLE = False
try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
    OSC_AVAILABLE = True
except ImportError:
    pass


class OSCManager(QObject):
    start_all_requested = pyqtSignal()
    stop_all_requested  = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[object] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def start(self, listen_port: int, feedback_ip: str, feedback_port: int):
        if not OSC_AVAILABLE:
            return False
        self.stop()
        try:
            self._client = SimpleUDPClient(feedback_ip, feedback_port)
        except Exception:
            self._client = None

        try:
            disp = Dispatcher()
            disp.map("/start_all", self._on_start_all)
            disp.map("/stop_all",  self._on_stop_all)
            disp.set_default_handler(lambda *_: None)
            self._server = ThreadingOSCUDPServer(("0.0.0.0", listen_port), disp)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            self._thread.start()
            return True
        except Exception:
            self._server = None
            return False

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        self._thread = None

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def send_feedback(self, any_recording: bool, recording_count: int, source_count: int):
        if not self._client:
            return
        try:
            self._client.send_message("/multicam/recording",       int(any_recording))
            self._client.send_message("/multicam/recording_count", recording_count)
            self._client.send_message("/multicam/source_count",    source_count)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Handlers  (called on the OSC server thread — Qt signals are thread-safe)
    # ------------------------------------------------------------------

    def _on_start_all(self, addr, *args):
        self.start_all_requested.emit()

    def _on_stop_all(self, addr, *args):
        self.stop_all_requested.emit()
