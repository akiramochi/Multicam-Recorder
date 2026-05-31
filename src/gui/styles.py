DARK_STYLESHEET = """
QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}

QMainWindow {
    background-color: #1e1e1e;
}

/* ── Toolbar ── */
QToolBar {
    background-color: #252525;
    border-bottom: 1px solid #3a3a3a;
    padding: 4px 8px;
    spacing: 6px;
}

QToolBar QLabel {
    color: #aaaaaa;
    font-size: 12px;
}

/* ── Buttons ── */
QPushButton {
    background-color: #3a3a3a;
    color: #e0e0e0;
    border: 1px solid #555;
    border-radius: 5px;
    padding: 6px 14px;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #484848;
    border-color: #777;
}
QPushButton:pressed {
    background-color: #2a2a2a;
}
QPushButton:disabled {
    color: #666;
    border-color: #3a3a3a;
}

QPushButton#btn_start_all {
    background-color: #1a6b2e;
    border-color: #27a045;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#btn_start_all:hover {
    background-color: #1e7d36;
}

QPushButton#btn_stop_all {
    background-color: #7a1a1a;
    border-color: #c0392b;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#btn_stop_all:hover {
    background-color: #8e2020;
}

QPushButton#btn_record {
    background-color: #1a6b2e;
    border-color: #27a045;
    color: #ffffff;
}
QPushButton#btn_record:hover {
    background-color: #1e7d36;
}

QPushButton#btn_stop_rec {
    background-color: #7a1a1a;
    border-color: #c0392b;
    color: #ffffff;
}
QPushButton#btn_stop_rec:hover {
    background-color: #8e2020;
}

QPushButton#btn_remove {
    background-color: transparent;
    border: none;
    color: #888;
    padding: 2px 6px;
    font-size: 11px;
}
QPushButton#btn_remove:hover {
    color: #c0392b;
}

/* ── Line edit / combo ── */
QLineEdit {
    background-color: #2d2d2d;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e0e0e0;
}
QLineEdit:focus {
    border-color: #4a9eff;
}

QComboBox {
    background-color: #2d2d2d;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e0e0e0;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #2d2d2d;
    border: 1px solid #555;
    selection-background-color: #4a4a4a;
}

/* ── Sliders ── */
QSlider::groove:horizontal {
    height: 4px;
    background: #3a3a3a;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #4a9eff;
    border: none;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
QSlider::sub-page:horizontal {
    background: #4a9eff;
    border-radius: 2px;
}

/* ── SpinBox ── */
QSpinBox {
    background-color: #2d2d2d;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 3px 6px;
    color: #e0e0e0;
}

/* ── Stream tile card ── */
QFrame#stream_tile {
    background-color: #2a2a2a;
    border: 1px solid #3a3a3a;
    border-radius: 8px;
}
QFrame#stream_tile:hover {
    border-color: #555;
}

QLabel#tile_source_name {
    font-size: 13px;
    font-weight: bold;
    color: #e0e0e0;
}
QLabel#tile_info {
    font-size: 11px;
    color: #888;
}
QLabel#tile_status {
    font-size: 12px;
    font-weight: bold;
}
QLabel#tile_duration {
    font-size: 12px;
    color: #e0e0e0;
    font-variant-numeric: tabular-nums;
}

/* ── Scroll area ── */
QScrollArea {
    border: none;
    background-color: #1e1e1e;
}
QScrollBar:vertical {
    background: #1e1e1e;
    width: 8px;
}
QScrollBar::handle:vertical {
    background: #444;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* ── Status bar ── */
QStatusBar {
    background-color: #252525;
    border-top: 1px solid #3a3a3a;
    color: #aaaaaa;
    font-size: 12px;
}

/* ── Dialog ── */
QDialog {
    background-color: #252525;
}

/* ── List widget ── */
QListWidget {
    background-color: #2d2d2d;
    border: 1px solid #555;
    border-radius: 4px;
    outline: none;
}
QListWidget::item {
    padding: 6px 10px;
}
QListWidget::item:selected {
    background-color: #3a5f8a;
    color: #ffffff;
}
QListWidget::item:hover {
    background-color: #3a3a3a;
}

/* ── Radio / Check buttons ── */
QRadioButton, QCheckBox {
    spacing: 8px;
}
QRadioButton::indicator, QCheckBox::indicator {
    width: 16px;
    height: 16px;
}

/* ── Tab widget ── */
QTabWidget::pane {
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    padding: 8px;
}
QTabBar::tab {
    background-color: #2a2a2a;
    color: #aaaaaa;
    border: 1px solid #3a3a3a;
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    padding: 6px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #3a3a3a;
    color: #e0e0e0;
}
QTabBar::tab:hover:!selected {
    background-color: #323232;
}

/* ── Group box ── */
QGroupBox {
    border: 1px solid #3a3a3a;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 4px;
    font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #aaaaaa;
}

/* ── Preview label ── */
QLabel#preview_label {
    background-color: #000000;
    border-radius: 4px;
}
"""
