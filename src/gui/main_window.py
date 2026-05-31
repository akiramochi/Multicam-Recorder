"""Main application window."""
import os
from typing import Dict, List

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QStatusBar,
    QToolBar, QVBoxLayout, QWidget, QMessageBox, QLineEdit,
)

from ..ndi_manager import NDIManager, NDISource
from ..decklink_manager import DeckLinkManager, DeckLinkSource
from ..decklink_worker import DeckLinkWorker
from ..recording_settings import RecordingSettings
from ..profile_manager import ProfileManager
from ..osc_manager import OSCManager, OSC_AVAILABLE
from .add_source_dialog import AddSourceDialog
from .settings_dialog import SettingsDialog
from .stream_tile import StreamTile

_TILE_COLUMNS = 3


class MainWindow(QMainWindow):
    def __init__(self, ndi_manager: NDIManager, decklink_manager: DeckLinkManager):
        super().__init__()
        self.setWindowTitle("NDI Multicam Recorder")
        self.resize(1200, 780)

        self._manager  = ndi_manager
        self._dl       = decklink_manager
        self._profiles = ProfileManager()
        self._settings = self._profiles.get_active_settings()
        self._tiles: Dict[str, StreamTile] = {}   # source_name → tile

        self._osc = OSCManager(self)
        self._osc.start_all_requested.connect(self._start_all)
        self._osc.stop_all_requested.connect(self._stop_all)

        self._build_ui()
        self._apply_initial_output_dir()
        self._start_status_refresh()
        # Re-open sources saved in the active profile (DeckLink sources are
        # already enumerated; NDI sources may not be visible yet so missing
        # ones are silently skipped at startup rather than shown as warnings).
        self._restore_sources_silent(self._profiles.get_active_sources())

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Toolbar ──────────────────────────────────────────────────
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(toolbar)

        add_btn = QPushButton("＋  Add NDI")
        add_btn.clicked.connect(self._add_source)
        toolbar.addWidget(add_btn)

        self._dl_btn = QPushButton("＋  Add DeckLink")
        self._dl_btn.clicked.connect(self._add_decklink_source)
        self._dl_btn.setVisible(self._dl.available)
        toolbar.addWidget(self._dl_btn)

        toolbar.addSeparator()

        self._start_all_btn = QPushButton("▶  Start All")
        self._start_all_btn.setObjectName("btn_start_all")
        self._start_all_btn.clicked.connect(self._start_all)
        toolbar.addWidget(self._start_all_btn)

        self._stop_all_btn = QPushButton("■  Stop All")
        self._stop_all_btn.setObjectName("btn_stop_all")
        self._stop_all_btn.clicked.connect(self._stop_all)
        toolbar.addWidget(self._stop_all_btn)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel("Output: "))
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("No output directory set…")
        self._dir_edit.setMinimumWidth(260)
        self._dir_edit.setReadOnly(True)
        toolbar.addWidget(self._dir_edit)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_output)
        toolbar.addWidget(browse_btn)

        toolbar.addSeparator()

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

        # ── Profile toolbar ───────────────────────────────────────────
        prof_bar = QToolBar()
        prof_bar.setMovable(False)
        prof_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.addToolBar(prof_bar)

        prof_bar.addWidget(QLabel("Profile: "))

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(160)
        self._profile_combo.setToolTip("Select a profile to load its settings")
        prof_bar.addWidget(self._profile_combo)

        save_prof_btn = QPushButton("💾  Save")
        save_prof_btn.setToolTip("Save current settings to the selected profile")
        save_prof_btn.clicked.connect(self._save_profile)
        prof_bar.addWidget(save_prof_btn)

        new_prof_btn = QPushButton("＋  New…")
        new_prof_btn.setToolTip("Create a new profile from current settings")
        new_prof_btn.clicked.connect(self._new_profile)
        prof_bar.addWidget(new_prof_btn)

        rename_prof_btn = QPushButton("✎  Rename…")
        rename_prof_btn.setToolTip("Rename the selected profile")
        rename_prof_btn.clicked.connect(self._rename_profile)
        prof_bar.addWidget(rename_prof_btn)

        self._del_prof_btn = QPushButton("🗑  Delete")
        self._del_prof_btn.setToolTip("Delete the selected profile")
        self._del_prof_btn.clicked.connect(self._delete_profile)
        prof_bar.addWidget(self._del_prof_btn)

        # Populate after all widgets exist
        self._refresh_profile_combo()
        # Connect AFTER initial populate so loading doesn't fire on setup
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)

        # ── Central area ──────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        main_layout.addWidget(scroll)

        self._grid_widget = QWidget()
        self._grid_layout = _FlowLayout(self._grid_widget, h_spacing=12, v_spacing=12)
        scroll.setWidget(self._grid_widget)

        # Empty-state label
        self._empty_label = QLabel(
            "No sources added.\n\nClick  ＋ Add NDI  or  ＋ Add DeckLink  to get started."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #555; font-size: 15px;")
        self._empty_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        main_layout.addWidget(self._empty_label)
        self._empty_label.setVisible(True)

        # ── Status bar ────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._update_status()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _add_source(self):
        already = list(self._tiles.keys())
        dlg = AddSourceDialog(self._manager, already, parent=self)
        if dlg.exec() and (src := dlg.get_selected_source()):
            self._add_tile(src)

    def _add_decklink_source(self):
        # get_sources() returns the list cached by DeckLinkManager.initialize(),
        # so this is instant — no COM calls happen here.
        sources = self._dl.get_sources()
        available = [s for s in sources if s.name not in self._tiles]
        if not available:
            QMessageBox.information(self, 'DeckLink',
                'No DeckLink devices found, or all devices already added.')
            return
        if len(available) == 1:
            self._add_tile(available[0])
            return
        from PyQt6.QtWidgets import QInputDialog
        names = [s.name for s in available]
        name, ok = QInputDialog.getItem(self, 'Add DeckLink Source',
                                        'Select device:', names, 0, False)
        if ok and name:
            src = next((s for s in available if s.name == name), None)
            if src:
                self._add_tile(src)

    def _add_tile(self, source: NDISource):
        name = source.name
        if name in self._tiles:
            return
        tile = StreamTile(source, self._settings, parent=self._grid_widget)
        tile.remove_requested.connect(self._remove_tile)
        self._tiles[name] = tile
        self._grid_layout.addWidget(tile)
        self._empty_label.setVisible(False)
        self._update_status()

    @pyqtSlot(str)
    def _remove_tile(self, source_name: str):
        tile = self._tiles.pop(source_name, None)
        if tile is None:
            return
        tile.shutdown()
        self._grid_layout.removeWidget(tile)
        tile.setParent(None)
        tile.deleteLater()
        if not self._tiles:
            self._empty_label.setVisible(True)
        self._update_status()

    def _start_all(self):
        if not self._settings.output_directory:
            QMessageBox.warning(
                self, "No Output Directory",
                "Please set an output directory in Settings before recording."
            )
            return
        for tile in self._tiles.values():
            tile.start_recording()

    def _stop_all(self):
        for tile in self._tiles.values():
            tile.stop_recording()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self._settings.output_directory
        )
        if path:
            self._settings.output_directory = path
            self._dir_edit.setText(path)
            for tile in self._tiles.values():
                tile.apply_settings(self._settings)
            self._profiles.save_profile(
                self._profiles.active_name, self._settings, self._current_sources()
            )

    def _open_settings(self):
        dlg = SettingsDialog(self._settings, parent=self)
        if dlg.exec():
            self._settings = dlg.get_settings()
            self._dir_edit.setText(self._settings.output_directory)
            for tile in self._tiles.values():
                tile.apply_settings(self._settings)
            self._apply_osc_settings()
            self._profiles.save_profile(
                self._profiles.active_name, self._settings, self._current_sources()
            )

    # ------------------------------------------------------------------
    # Status refresh
    # ------------------------------------------------------------------

    def _start_status_refresh(self):
        t = QTimer(self)
        t.timeout.connect(self._update_status)
        t.start(1000)

    def _update_status(self):
        n = len(self._tiles)
        rec = sum(1 for t in self._tiles.values() if t.is_recording)
        msg = f"{n} source{'s' if n != 1 else ''}  |  {rec} recording"
        if not n:
            msg = "No sources — click ＋ Add Source"
        if self._osc.is_running:
            msg += f"  |  OSC :{self._settings.osc_listen_port}"
        self._status_bar.showMessage(msg)
        self._osc.send_feedback(rec > 0, rec, n)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_initial_output_dir(self):
        # Use the saved directory if one exists, otherwise fall back to default.
        if not self._settings.output_directory:
            self._settings.output_directory = os.path.join(
                os.path.expanduser("~"), "Videos", "NDI Recordings"
            )
        self._dir_edit.setText(self._settings.output_directory)

    # ------------------------------------------------------------------
    # Profile actions
    # ------------------------------------------------------------------

    def _restore_sources_silent(self, sources: list) -> None:
        """
        Like _restore_sources but without tearing down existing tiles and
        without a warning dialog.  Used at startup so missing NDI sources
        don't produce a popup before the window is fully visible.
        """
        for entry in sources:
            src_type = entry.get("type", "")
            src_name = entry.get("name", "")
            if src_type == "decklink":
                src = next(
                    (s for s in self._dl.get_sources() if s.name == src_name), None
                )
            else:
                src = next(
                    (s for s in self._manager.get_sources() if s.name == src_name), None
                )
            if src and src_name not in self._tiles:
                self._add_tile(src)

    def _current_sources(self) -> list:
        """Serialisable snapshot of the tiles that are currently open."""
        return [
            {"type": tile.source_type, "name": tile.source_name}
            for tile in self._tiles.values()
        ]

    def _restore_sources(self, sources: list) -> None:
        """
        Shut down every current tile then reopen the sources saved in the
        profile.  Sources that cannot be found are reported in a warning.
        """
        for name in list(self._tiles.keys()):
            self._remove_tile(name)

        not_found = []
        for entry in sources:
            src_type = entry.get("type", "")
            src_name = entry.get("name", "")
            if src_type == "decklink":
                src = next(
                    (s for s in self._dl.get_sources() if s.name == src_name),
                    None,
                )
            else:
                src = next(
                    (s for s in self._manager.get_sources() if s.name == src_name),
                    None,
                )
            if src:
                self._add_tile(src)
            else:
                not_found.append(src_name)

        if not_found:
            QMessageBox.warning(
                self, "Sources Not Found",
                "The following sources from this profile could not be found:\n"
                + "\n".join(f"  •  {n}" for n in not_found)
                + "\n\nNDI sources may not be discoverable yet — "
                "try re-adding them manually once they appear.",
            )

    def _refresh_profile_combo(self):
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in self._profiles.profile_names:
            self._profile_combo.addItem(name)
        idx = self._profile_combo.findText(self._profiles.active_name)
        self._profile_combo.setCurrentIndex(max(idx, 0))
        self._del_prof_btn.setEnabled(len(self._profiles.profile_names) > 1)
        self._profile_combo.blockSignals(False)

    def _on_profile_selected(self, name: str):
        if not name:
            return
        settings = self._profiles.get_settings(name)
        if settings is None:
            return
        self._settings = settings
        self._profiles.set_active(name)
        self._dir_edit.setText(self._settings.output_directory)
        self._apply_osc_settings()
        self._del_prof_btn.setEnabled(len(self._profiles.profile_names) > 1)
        # Restore the saved source tiles for this profile
        self._restore_sources(self._profiles.get_sources(name))

    def _save_profile(self):
        name = self._profile_combo.currentText()
        if not name:
            return
        self._profiles.save_profile(name, self._settings, self._current_sources())

    def _new_profile(self):
        name, ok = QInputDialog.getText(
            self, "New Profile", "Profile name:", text="New Profile"
        )
        name = name.strip()
        if not ok or not name:
            return
        if not self._profiles.new_profile(name, self._settings, self._current_sources()):
            QMessageBox.warning(
                self, "Profile Exists",
                f'A profile named "{name}" already exists.\n'
                "Choose a different name or use Save to overwrite the current profile."
            )
            return
        self._refresh_profile_combo()
        idx = self._profile_combo.findText(name)
        if idx >= 0:
            self._profile_combo.blockSignals(True)
            self._profile_combo.setCurrentIndex(idx)
            self._profile_combo.blockSignals(False)

    def _rename_profile(self):
        old = self._profile_combo.currentText()
        if not old:
            return
        new, ok = QInputDialog.getText(
            self, "Rename Profile", "New name:", text=old
        )
        new = new.strip()
        if not ok or not new or new == old:
            return
        if not self._profiles.rename_profile(old, new):
            QMessageBox.warning(
                self, "Rename Failed",
                f'Could not rename "{old}" to "{new}".\n'
                "The new name may already be in use."
            )
            return
        self._refresh_profile_combo()
        idx = self._profile_combo.findText(new)
        if idx >= 0:
            self._profile_combo.blockSignals(True)
            self._profile_combo.setCurrentIndex(idx)
            self._profile_combo.blockSignals(False)

    def _delete_profile(self):
        name = self._profile_combo.currentText()
        if not name:
            return
        if len(self._profiles.profile_names) <= 1:
            QMessageBox.information(
                self, "Delete Profile", "Cannot delete the last profile."
            )
            return
        reply = QMessageBox.question(
            self, "Delete Profile",
            f'Delete the profile "{name}"?\nThis cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._profiles.delete_profile(name)
            self._refresh_profile_combo()
            self._on_profile_selected(self._profile_combo.currentText())

    def _apply_osc_settings(self):
        self._osc.stop()
        if self._settings.osc_enabled:
            self._osc.start(
                self._settings.osc_listen_port,
                self._settings.osc_feedback_ip,
                self._settings.osc_feedback_port,
            )

    def closeEvent(self, event):
        self._osc.stop()
        for tile in list(self._tiles.values()):
            tile.shutdown()
        event.accept()


# ---------------------------------------------------------------------------
# Simple flow layout (tiles wrap to new rows automatically)
# ---------------------------------------------------------------------------

from PyQt6.QtCore import QPoint, QRect, QSize
from PyQt6.QtWidgets import QLayout, QLayoutItem, QSizePolicy


class _FlowLayout(QLayout):
    def __init__(self, parent=None, h_spacing=6, v_spacing=6):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items: List[QLayoutItem] = []

    def addItem(self, item: QLayoutItem):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def removeWidget(self, widget):
        for i, item in enumerate(self._items):
            if item.widget() is widget:
                self._items.pop(i)
                break

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        row_height = 0

        for item in self._items:
            widget = item.widget()
            if widget and not widget.isVisible():
                continue
            item_size = item.sizeHint()
            next_x = x + item_size.width()
            if next_x > right and row_height > 0:
                x = rect.x() + margins.left()
                y += row_height + self._v_spacing
                row_height = 0
                next_x = x + item_size.width()
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))
            x = next_x + self._h_spacing
            row_height = max(row_height, item_size.height())

        return y + row_height - rect.y() + margins.bottom()
