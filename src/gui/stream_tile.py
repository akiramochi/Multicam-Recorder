"""Per-stream preview card widget."""
import os
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ..recording_settings import RecordingSettings, SourceOverrides
from ..stream_worker import StreamWorker
from ..decklink_manager import DeckLinkSource
from ..decklink_worker import DeckLinkWorker

_PREVIEW_W = 320
_PREVIEW_H = 180
_NO_SIGNAL_STYLE = "color: #888888;"
_RECORDING_STYLE = "color: #e74c3c; font-weight: bold;"
_CONNECTED_STYLE = "color: #2ecc71;"
_WAITING_STYLE = "color: #f39c12;"


def _gear_icon(color: str = "#cfcfcf", size: int = 18) -> QIcon:
    """Draw a settings/gear icon as a vector pixmap.

    Drawn with QPainter rather than relying on an emoji/font glyph so it
    always renders (no blank 'tofu' boxes) and stays crisp on any background.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))

    cx = cy = size / 2.0
    r_teeth = size * 0.46     # how far teeth extend from centre
    r_body = size * 0.30      # radius of the round cog body
    tooth_w = size * 0.18
    tooth_h = size * 0.20

    p.translate(cx, cy)
    for i in range(8):
        p.save()
        p.rotate(i * 45.0)
        p.drawRoundedRect(
            QRectF(-tooth_w / 2.0, -r_teeth, tooth_w, tooth_h), 1.5, 1.5
        )
        p.restore()
    p.drawEllipse(QPointF(0, 0), r_body, r_body)

    # Punch a transparent centre hole so the cog reads correctly on any bg.
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.setBrush(QColor(0, 0, 0))
    p.drawEllipse(QPointF(0, 0), size * 0.13, size * 0.13)
    p.end()
    return QIcon(pm)


class StreamTile(QFrame):
    """
    Card that owns a StreamWorker, renders its preview, and exposes
    per-stream record / stop controls.
    """

    remove_requested  = pyqtSignal(str)   # emits source name
    overrides_changed = pyqtSignal(str)   # emits source name when overrides updated

    def __init__(
        self,
        source,
        settings: RecordingSettings,
        overrides: SourceOverrides | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("stream_tile")
        self.setFixedWidth(_PREVIEW_W + 24)

        self._source = source
        self._settings = settings.copy()
        self._overrides: SourceOverrides = overrides or SourceOverrides()
        self._is_recording = False
        self._rec_start_time: datetime | None = None

        self._build_ui()
        self._start_worker()
        self._refresh_cfg_indicator()

        self._duration_timer = QTimer(self)
        self._duration_timer.timeout.connect(self._update_duration)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        # Preview area
        self._preview = QLabel()
        self._preview.setObjectName("preview_label")
        self._preview.setFixedSize(_PREVIEW_W, _PREVIEW_H)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setText("No Signal")
        self._preview.setStyleSheet("background-color:#000; color:#444; border-radius:4px;")
        outer.addWidget(self._preview)

        # Source name
        self._name_label = QLabel(str(self._source))
        self._name_label.setObjectName("tile_source_name")
        self._name_label.setWordWrap(True)
        outer.addWidget(self._name_label)

        # Stream info (resolution, fps)
        self._info_label = QLabel("Connecting…")
        self._info_label.setObjectName("tile_info")
        outer.addWidget(self._info_label)

        # Status row
        status_row = QHBoxLayout()
        self._dot = QLabel("●")
        self._dot.setStyleSheet(_NO_SIGNAL_STYLE)
        self._status_label = QLabel("Connecting…")
        self._status_label.setObjectName("tile_status")
        self._status_label.setStyleSheet(_NO_SIGNAL_STYLE)
        self._duration_label = QLabel("")
        self._duration_label.setObjectName("tile_duration")
        status_row.addWidget(self._dot)
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        status_row.addWidget(self._duration_label)
        outer.addLayout(status_row)

        # Buttons row
        btn_row = QHBoxLayout()
        self._rec_btn = QPushButton("⏺  Record")
        self._rec_btn.setObjectName("btn_record")
        self._rec_btn.clicked.connect(self._toggle_recording)

        self._settings_btn = QPushButton()
        self._settings_btn.setObjectName("btn_tile_settings")
        self._settings_btn.setIcon(_gear_icon())
        self._settings_btn.setIconSize(QSize(18, 18))
        self._settings_btn.setToolTip("Per-source recording settings")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.clicked.connect(self._open_source_settings)

        self._remove_btn = QPushButton("✕ Remove")
        self._remove_btn.setObjectName("btn_remove")
        self._remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(str(self._source))
        )

        btn_row.addWidget(self._rec_btn)
        btn_row.addWidget(self._settings_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._remove_btn)
        outer.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _effective_settings(self) -> RecordingSettings:
        return self._overrides.apply_to(self._settings)

    def _start_worker(self):
        cls = DeckLinkWorker if isinstance(self._source, DeckLinkSource) else StreamWorker
        self._worker = cls(self._source)
        self._worker.configure(self._effective_settings())
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.recording_started.connect(self._on_rec_started)
        self._worker.recording_stopped.connect(self._on_rec_stopped)
        self._worker.status_changed.connect(self._on_status)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.stream_info.connect(self._on_stream_info)
        self._worker.start()

    # ------------------------------------------------------------------
    # Public API (called from MainWindow)
    # ------------------------------------------------------------------

    @property
    def source_name(self) -> str:
        return str(self._source)

    @property
    def source_type(self) -> str:
        """'decklink' or 'ndi' — used by the profile system."""
        return "decklink" if isinstance(self._source, DeckLinkSource) else "ndi"

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def overrides(self) -> SourceOverrides:
        return self._overrides

    def apply_settings(self, settings: RecordingSettings):
        self._settings = settings.copy()
        self._worker.configure(self._effective_settings())

    def start_recording(self):
        if self._is_recording:
            return
        out_dir = self._settings.output_directory
        if not out_dir:
            self._on_error("No output directory set — open Settings first.")
            return
        os.makedirs(out_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in str(self._source)
        )
        effective = self._effective_settings()
        filename = f"{ts}_{safe}.{effective.file_extension}"
        path = os.path.join(out_dir, filename)
        self._worker.start_recording(path)

    def stop_recording(self):
        if self._is_recording:
            self._worker.stop_recording()

    def shutdown(self):
        self._duration_timer.stop()
        self._worker.stop()
        # The worker's run() finalises the file and joins its encoder thread
        # (bounded to ~10 s) before returning, so this wait is guaranteed to end.
        # It must outlast that join: deleting a QThread that is still running
        # aborts the process, so wait long enough that run() has actually exited.
        if not self._worker.wait(15000):
            print(f"[StreamTile] worker for {self.source_name} did not stop "
                  f"within 15s; leaving it to finish in the background",
                  flush=True)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _refresh_cfg_indicator(self):
        if self._overrides.has_any():
            self._settings_btn.setToolTip("Per-source settings (overrides active)")
            self._settings_btn.setIcon(_gear_icon("#f39c12"))
        else:
            self._settings_btn.setToolTip("Per-source recording settings")
            self._settings_btn.setIcon(_gear_icon())

    def _open_source_settings(self):
        from .source_settings_dialog import SourceSettingsDialog
        dlg = SourceSettingsDialog(
            str(self._source), self._settings, self._overrides, parent=self
        )
        if dlg.exec():
            self._overrides = dlg.get_overrides()
            self._worker.configure(self._effective_settings())
            # Indicate overrides are active via tooltip on the gear button
            self._refresh_cfg_indicator()
            self.overrides_changed.emit(str(self._source))

    def _toggle_recording(self):
        if self._is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _on_frame(self, img: QImage):
        scaled = img.scaled(
            _PREVIEW_W, _PREVIEW_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview.setPixmap(QPixmap.fromImage(scaled))

    def _on_rec_started(self, path: str):
        self._is_recording = True
        self._rec_start_time = datetime.now()
        self._rec_btn.setText("⏹  Stop")
        self._rec_btn.setObjectName("btn_stop_rec")
        self._rec_btn.style().unpolish(self._rec_btn)
        self._rec_btn.style().polish(self._rec_btn)
        self._duration_timer.start(1000)
        self._update_duration()

    def _on_rec_stopped(self):
        self._is_recording = False
        self._rec_start_time = None
        self._duration_timer.stop()
        self._duration_label.setText("")
        self._rec_btn.setText("⏺  Record")
        self._rec_btn.setObjectName("btn_record")
        self._rec_btn.style().unpolish(self._rec_btn)
        self._rec_btn.style().polish(self._rec_btn)

    def _on_status(self, text: str):
        self._status_label.setText(text)
        if "Recording" in text:
            self._dot.setStyleSheet(_RECORDING_STYLE)
            self._status_label.setStyleSheet(_RECORDING_STYLE)
        elif text in ("Connected",):
            self._dot.setStyleSheet(_CONNECTED_STYLE)
            self._status_label.setStyleSheet(_CONNECTED_STYLE)
        elif "Wait" in text:
            self._dot.setStyleSheet(_WAITING_STYLE)
            self._status_label.setStyleSheet(_WAITING_STYLE)
        else:
            self._dot.setStyleSheet(_NO_SIGNAL_STYLE)
            self._status_label.setStyleSheet(_NO_SIGNAL_STYLE)

    def _on_error(self, msg: str):
        self._info_label.setText(f"⚠ {msg}")

    def _on_stream_info(self, w: int, h: int, fps: float):
        self._info_label.setText(f"{w}×{h}  {fps:.2f} fps")

    def _update_duration(self):
        if self._rec_start_time is None:
            return
        delta = datetime.now() - self._rec_start_time
        total_s = int(delta.total_seconds())
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        self._duration_label.setText(f"{h:02d}:{m:02d}:{s:02d}")
