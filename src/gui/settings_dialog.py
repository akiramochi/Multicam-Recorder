"""Global recording settings dialog."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..recording_settings import (
    Container, H264Profile, H265Profile, RecordingSettings, VideoCodec,
)
from ..osc_manager import OSC_AVAILABLE


class SettingsDialog(QDialog):
    def __init__(self, settings: RecordingSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self._settings = settings.copy()
        self._build_ui()
        self._load(settings)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._build_recording_tab(), "Recording")
        tabs.addTab(self._build_osc_tab(),       "OSC / Bitfocus")
        root.addWidget(tabs)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

    # ── Recording tab ─────────────────────────────────────────────────

    def _build_recording_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        # Output directory
        dir_group = QGroupBox("Output Directory")
        dir_row = QHBoxLayout(dir_group)
        self._dir_edit = QLineEdit()
        self._dir_edit.setPlaceholderText("Select output folder…")
        dir_browse = QPushButton("Browse…")
        dir_browse.clicked.connect(self._browse_dir)
        dir_row.addWidget(self._dir_edit)
        dir_row.addWidget(dir_browse)
        layout.addWidget(dir_group)

        # Container
        cont_group = QGroupBox("Container")
        cont_row = QHBoxLayout(cont_group)
        self._rb_mp4 = QRadioButton("MP4  (.mp4)")
        self._rb_mov = QRadioButton("MOV  (.mov)")
        cont_row.addWidget(self._rb_mp4)
        cont_row.addWidget(self._rb_mov)
        cont_row.addStretch()
        layout.addWidget(cont_group)

        # Codec + Profile (side by side)
        cp_row = QHBoxLayout()

        codec_group = QGroupBox("Video Codec")
        codec_inner = QHBoxLayout(codec_group)
        self._rb_h264 = QRadioButton("H.264  (AVC)")
        self._rb_h265 = QRadioButton("H.265  (HEVC)")
        self._rb_h264.toggled.connect(self._on_codec_changed)
        codec_inner.addWidget(self._rb_h264)
        codec_inner.addWidget(self._rb_h265)

        profile_group = QGroupBox("Profile")
        profile_inner = QHBoxLayout(profile_group)
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(110)
        profile_inner.addWidget(self._profile_combo)
        profile_inner.addStretch()

        cp_row.addWidget(codec_group, stretch=3)
        cp_row.addWidget(profile_group, stretch=2)
        layout.addLayout(cp_row)

        # Hardware acceleration
        hw_group = QGroupBox("Hardware Acceleration")
        hw_inner = QVBoxLayout(hw_group)
        self._nvenc_cb = QCheckBox("Use NVIDIA GPU encoder  (NVENC)")
        hw_note = QLabel(
            "Requires an NVIDIA GPU with NVENC support (GTX 600 series or newer).\n"
            "If unavailable, recording will fail — check the terminal for errors."
        )
        hw_note.setStyleSheet("color: #888; font-size: 11px;")
        hw_inner.addWidget(self._nvenc_cb)
        hw_inner.addWidget(hw_note)
        layout.addWidget(hw_group)

        # Video bitrate
        vbr_group = QGroupBox("Video Bitrate")
        vbr_inner = QVBoxLayout(vbr_group)
        self._vbr_slider, self._vbr_spin, vbr_row = self._bitrate_row(
            1_000, 100_000, 8_000, "kbps", step=500
        )
        vbr_inner.addLayout(vbr_row)
        layout.addWidget(vbr_group)

        # Audio bitrate
        abr_group = QGroupBox("Audio Bitrate")
        abr_inner = QVBoxLayout(abr_group)
        self._abr_slider, self._abr_spin, abr_row = self._bitrate_row(
            64, 320, 192, "kbps", step=8
        )
        abr_inner.addLayout(abr_row)
        layout.addWidget(abr_group)

        layout.addStretch()
        return w

    # ── OSC tab ───────────────────────────────────────────────────────

    def _build_osc_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        if not OSC_AVAILABLE:
            warn = QLabel(
                "python-osc is not installed.\n\n"
                "Run:  pip install python-osc\n\n"
                "then restart the application."
            )
            warn.setStyleSheet("color: #e67e22; font-size: 13px;")
            warn.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(warn)
            layout.addStretch()
            self._osc_enable_cb    = QCheckBox()
            self._osc_listen_spin  = QSpinBox()
            self._osc_ip_edit      = QLineEdit()
            self._osc_fb_port_spin = QSpinBox()
            return w

        self._osc_enable_cb = QCheckBox("Enable OSC  (requires restart of OSC listener)")
        self._osc_enable_cb.toggled.connect(self._on_osc_toggled)
        layout.addWidget(self._osc_enable_cb)

        # Listen port
        listen_group = QGroupBox("Incoming  (Companion → App)")
        listen_inner = QHBoxLayout(listen_group)
        listen_inner.addWidget(QLabel("Listen port:"))
        self._osc_listen_spin = QSpinBox()
        self._osc_listen_spin.setRange(1024, 65535)
        self._osc_listen_spin.setValue(9000)
        self._osc_listen_spin.setFixedWidth(90)
        listen_inner.addWidget(self._osc_listen_spin)
        listen_inner.addStretch()
        paths_label = QLabel(
            "Supported paths:\n"
            "  /start_all   — start all recordings\n"
            "  /stop_all    — stop all recordings"
        )
        paths_label.setStyleSheet("color: #888; font-size: 11px;")
        listen_inner_v = QVBoxLayout()
        listen_inner_v.addLayout(listen_inner)
        listen_inner_v.addWidget(paths_label)
        listen_group.setLayout(listen_inner_v)
        layout.addWidget(listen_group)

        # Feedback
        fb_group = QGroupBox("Outgoing feedback  (App → Companion)")
        fb_inner = QVBoxLayout(fb_group)

        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel("Companion IP:"))
        self._osc_ip_edit = QLineEdit()
        self._osc_ip_edit.setPlaceholderText("127.0.0.1")
        self._osc_ip_edit.setFixedWidth(140)
        ip_row.addWidget(self._osc_ip_edit)
        ip_row.addStretch()
        fb_inner.addLayout(ip_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Companion OSC port:"))
        self._osc_fb_port_spin = QSpinBox()
        self._osc_fb_port_spin.setRange(1024, 65535)
        self._osc_fb_port_spin.setValue(12321)
        self._osc_fb_port_spin.setFixedWidth(90)
        port_row.addWidget(self._osc_fb_port_spin)
        port_row.addStretch()
        fb_inner.addLayout(port_row)

        fb_paths = QLabel(
            "Paths sent to Companion:\n"
            "  /multicam/recording        (0 or 1)\n"
            "  /multicam/recording_count  (integer)\n"
            "  /multicam/source_count     (integer)"
        )
        fb_paths.setStyleSheet("color: #888; font-size: 11px;")
        fb_inner.addWidget(fb_paths)
        layout.addWidget(fb_group)

        layout.addStretch()
        return w

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bitrate_row(self, lo, hi, default, unit, step=1):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setSingleStep(step)
        slider.setPageStep(step * 10)
        slider.setValue(default)

        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setSuffix(f" {unit}")
        spin.setValue(default)
        spin.setFixedWidth(110)

        slider.valueChanged.connect(lambda v: spin.setValue(v))
        spin.valueChanged.connect(lambda v: slider.setValue(v))

        row = QHBoxLayout()
        row.addWidget(slider)
        row.addWidget(spin)
        return slider, spin, row

    def _populate_profiles(self, codec: VideoCodec, current_profile: str):
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        if codec == VideoCodec.H264:
            items = [("Baseline", H264Profile.BASELINE.value),
                     ("Main",     H264Profile.MAIN.value),
                     ("High",     H264Profile.HIGH.value)]
        else:
            items = [("Main",    H265Profile.MAIN.value),
                     ("Main 10", H265Profile.MAIN10.value)]
        for label, value in items:
            self._profile_combo.addItem(label, userData=value)
        idx = next((i for i, (_, v) in enumerate(items) if v == current_profile), 0)
        self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _on_codec_changed(self, h264_checked: bool):
        codec = VideoCodec.H264 if h264_checked else VideoCodec.H265
        current = (self._settings.h264_profile.value if h264_checked
                   else self._settings.h265_profile.value)
        self._populate_profiles(codec, current)

    def _on_osc_toggled(self, enabled: bool):
        for w in (self._osc_listen_spin, self._osc_ip_edit, self._osc_fb_port_spin):
            w.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load(self, s: RecordingSettings):
        self._dir_edit.setText(s.output_directory)
        (self._rb_mp4 if s.container is Container.MP4 else self._rb_mov).setChecked(True)

        self._rb_h264.setChecked(s.video_codec is VideoCodec.H264)
        self._rb_h265.setChecked(s.video_codec is VideoCodec.H265)
        self._populate_profiles(s.video_codec, s.active_video_profile)

        self._nvenc_cb.setChecked(s.use_nvenc)

        self._vbr_slider.setValue(s.video_bitrate_kbps)
        self._vbr_spin.setValue(s.video_bitrate_kbps)
        self._abr_slider.setValue(s.audio_bitrate_kbps)
        self._abr_spin.setValue(s.audio_bitrate_kbps)

        self._osc_enable_cb.setChecked(s.osc_enabled)
        self._osc_listen_spin.setValue(s.osc_listen_port)
        self._osc_ip_edit.setText(s.osc_feedback_ip)
        self._osc_fb_port_spin.setValue(s.osc_feedback_port)
        self._on_osc_toggled(s.osc_enabled)

    def _accept(self):
        s = self._settings
        s.output_directory   = self._dir_edit.text().strip()
        s.container          = Container.MP4 if self._rb_mp4.isChecked() else Container.MOV
        s.video_codec        = VideoCodec.H264 if self._rb_h264.isChecked() else VideoCodec.H265

        profile_val = self._profile_combo.currentData()
        if s.video_codec == VideoCodec.H264:
            s.h264_profile = H264Profile(profile_val)
        else:
            s.h265_profile = H265Profile(profile_val)

        s.use_nvenc          = self._nvenc_cb.isChecked()
        s.video_bitrate_kbps = self._vbr_spin.value()
        s.audio_bitrate_kbps = self._abr_spin.value()

        s.osc_enabled        = self._osc_enable_cb.isChecked()
        s.osc_listen_port    = self._osc_listen_spin.value()
        s.osc_feedback_ip    = self._osc_ip_edit.text().strip() or "127.0.0.1"
        s.osc_feedback_port  = self._osc_fb_port_spin.value()
        self.accept()

    def _browse_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self._dir_edit.text()
        )
        if path:
            self._dir_edit.setText(path)

    # ------------------------------------------------------------------

    def get_settings(self) -> RecordingSettings:
        return self._settings
