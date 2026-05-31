"""NDI Multicam Recorder — entry point."""
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.gui.styles import DARK_STYLESHEET
from src.gui.main_window import MainWindow
from src.ndi_manager import NDIManager, NDI_AVAILABLE
from src.decklink_manager import DeckLinkManager


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("NDI Multicam Recorder")
    app.setStyleSheet(DARK_STYLESHEET)

    if not NDI_AVAILABLE:
        QMessageBox.critical(
            None,
            "NDI Not Found",
            "Could not import ndi-python.\n\n"
            "Make sure you have run:  pip install ndi-python\n\n"
            "The application will now close.",
        )
        return 1

    ndi_mgr = NDIManager()
    if not ndi_mgr.initialize():
        QMessageBox.critical(None, "NDI Init Failed",
            "NDI library failed to start.")
        return 1

    # DeckLink is optional — silently unavailable if not installed
    dl_mgr = DeckLinkManager()
    dl_mgr.initialize()   # returns False quietly if not installed

    window = MainWindow(ndi_mgr, dl_mgr)
    window.show()

    ndi_mgr.start_discovery(callback=lambda _: None)

    exit_code = app.exec()
    ndi_mgr.destroy()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
