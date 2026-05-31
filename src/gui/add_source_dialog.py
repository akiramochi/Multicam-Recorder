"""Dialog for selecting NDI sources to add."""
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from ..ndi_manager import NDIManager, NDISource


class AddSourceDialog(QDialog):
    def __init__(self, manager: NDIManager, already_added: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add NDI Source")
        self.setMinimumSize(400, 320)
        self._manager = manager
        self._already_added = set(already_added)
        self._selected: Optional[NDISource] = None
        self._build_ui()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(2000)

    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel("Select an NDI source to add:")
        header.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        layout.addWidget(header)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_double_click)
        self._list.itemSelectionChanged.connect(self._on_selection_change)
        layout.addWidget(self._list)

        refresh_row = QHBoxLayout()
        self._status_label = QLabel("Scanning…")
        self._status_label.setStyleSheet("color: #888; font-size: 11px;")
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        refresh_row.addWidget(self._status_label)
        refresh_row.addStretch()
        refresh_row.addWidget(refresh_btn)
        layout.addLayout(refresh_row)

        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._btn_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._btn_box.accepted.connect(self._accept)
        self._btn_box.rejected.connect(self.reject)
        layout.addWidget(self._btn_box)

    def _refresh(self):
        sources = self._manager.get_current_sources()
        available = [s for s in sources if s.name not in self._already_added]

        self._list.clear()
        for src in available:
            item = QListWidgetItem(src.name)
            item.setData(Qt.ItemDataRole.UserRole, src)
            self._list.addItem(item)

        n = len(available)
        total = len(sources)
        self._status_label.setText(
            f"{n} available  ({total} total found on network)"
        )

    def _on_selection_change(self):
        has_sel = bool(self._list.selectedItems())
        self._btn_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(has_sel)

    def _on_double_click(self, _item: QListWidgetItem):
        self._accept()

    def _accept(self):
        items = self._list.selectedItems()
        if items:
            self._selected = items[0].data(Qt.ItemDataRole.UserRole)
            self._timer.stop()
            self.accept()

    def get_selected_source(self) -> Optional[NDISource]:
        return self._selected
