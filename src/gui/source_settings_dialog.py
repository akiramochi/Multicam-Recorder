"""Per-source recording settings dialog."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QLabel, QRadioButton, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..recording_settings import (
    Container, H264Profile, H265Profile, RecordingSettings,
    SourceOverrides, VideoCodec,
)


class SourceSettingsDialog(QDialog):
    """
    Configure per-source overrides on top of the global RecordingSettings.

    Each section has an "Override" checkbox.  When unchecked the global value
    is shown (greyed out) and will not be stored as an override.
    """

    def __init__(
        self,
        source_name: str,
        global_settings: RecordingSettings,
        overrides: SourceOverrides,
        available_audio_sources: list | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Source Settings — {source_name}")
        self.setMinimumWidth(500)
        self._global = global_settings
        self._overrides = overrides.to_dict()   # working copy as plain dict
        self._available_audio_sources = available_audio_sources or []
        self._result: SourceOverrides | None = None
        self._build_ui()
        self._load(global_settings, overrides)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        note = QLabel(
            "Settings checked here override the global defaults for this source only."
        )
        note.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(note)

        # ── Container ─────────────────────────────────────────────────
        self._cont_cb, cont_group = self._override_group("Container")
        cont_inner = QHBoxLayout()
        self._rb_mp4 = QRadioButton("MP4  (.mp4)")
        self._rb_mov = QRadioButton("MOV  (.mov)")
        cont_inner.addWidget(self._rb_mp4)
        cont_inner.addWidget(self._rb_mov)
        cont_inner.addStretch()
        cont_group.layout().addLayout(cont_inner)
        self._cont_cb.toggled.connect(
            lambda on: self._set_enabled(on, self._rb_mp4, self._rb_mov)
        )
        root.addWidget(self._make_section(self._cont_cb, cont_group))

        # ── Codec + Profile ───────────────────────────────────────────
        self._codec_cb, codec_group = self._override_group("Video Codec & Profile")
        cp_row = QHBoxLayout()

        codec_inner = QHBoxLayout()
        self._rb_h264 = QRadioButton("H.264  (AVC)")
        self._rb_h265 = QRadioButton("H.265  (HEVC)")
        self._rb_h264.toggled.connect(self._on_codec_changed)
        codec_inner.addWidget(self._rb_h264)
        codec_inner.addWidget(self._rb_h265)

        prof_inner = QHBoxLayout()
        prof_inner.addWidget(QLabel("Profile:"))
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(110)
        prof_inner.addWidget(self._profile_combo)
        prof_inner.addStretch()

        cp_row.addLayout(codec_inner, stretch=3)
        cp_row.addLayout(prof_inner, stretch=2)
        codec_group.layout().addLayout(cp_row)
        self._codec_cb.toggled.connect(
            lambda on: self._set_enabled(
                on, self._rb_h264, self._rb_h265, self._profile_combo
            )
        )
        root.addWidget(self._make_section(self._codec_cb, codec_group))

        # ── Hardware acceleration ─────────────────────────────────────
        self._hw_cb, hw_group = self._override_group("Hardware Acceleration")
        self._nvenc_cb = QCheckBox("Use NVIDIA GPU encoder  (NVENC)")
        hw_note = QLabel(
            "Requires an NVIDIA GPU with NVENC support.\n"
            "If unavailable the recording for this source will fail."
        )
        hw_note.setStyleSheet("color: #888; font-size: 11px;")
        hw_group.layout().addWidget(self._nvenc_cb)
        hw_group.layout().addWidget(hw_note)
        self._hw_cb.toggled.connect(
            lambda on: self._set_enabled(on, self._nvenc_cb)
        )
        root.addWidget(self._make_section(self._hw_cb, hw_group))

        # ── Video bitrate ─────────────────────────────────────────────
        self._vbr_cb, vbr_group = self._override_group("Video Bitrate")
        self._vbr_slider, self._vbr_spin, vbr_row = self._bitrate_row(
            1_000, 100_000, 8_000, "kbps", step=500
        )
        vbr_group.layout().addLayout(vbr_row)
        self._vbr_cb.toggled.connect(
            lambda on: self._set_enabled(on, self._vbr_slider, self._vbr_spin)
        )
        root.addWidget(self._make_section(self._vbr_cb, vbr_group))

        # ── Audio bitrate ─────────────────────────────────────────────
        self._abr_cb, abr_group = self._override_group("Audio Bitrate")
        self._abr_slider, self._abr_spin, abr_row = self._bitrate_row(
            64, 320, 192, "kbps", step=8
        )
        abr_group.layout().addLayout(abr_row)
        self._abr_cb.toggled.connect(
            lambda on: self._set_enabled(on, self._abr_slider, self._abr_spin)
        )
        root.addWidget(self._make_section(self._abr_cb, abr_group))

        # ── Audio Source ──────────────────────────────────────────────
        audio_src_group = QGroupBox("Audio Source")
        audio_src_group.setLayout(QVBoxLayout())
        audio_src_group.layout().setContentsMargins(8, 4, 8, 4)

        self._audio_src_combo = QComboBox()
        self._audio_src_combo.addItem("Own Audio  (default)", userData=None)
        for name in self._available_audio_sources:
            self._audio_src_combo.addItem(name, userData=name)
        self._audio_src_combo.setEnabled(bool(self._available_audio_sources))

        audio_src_note = QLabel(
            "Record audio from a different source alongside this video."
        )
        audio_src_note.setStyleSheet("color: #888; font-size: 11px;")
        audio_src_group.layout().addWidget(self._audio_src_combo)
        audio_src_group.layout().addWidget(audio_src_note)
        root.addWidget(audio_src_group)

        root.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _override_group(self, label: str):
        """Return (checkbox, groupbox) pair wired together."""
        cb = QCheckBox(f"Override: {label}")
        group = QGroupBox()
        group.setLayout(QVBoxLayout())
        group.layout().setContentsMargins(8, 4, 8, 4)
        return cb, group

    def _make_section(self, cb: QCheckBox, group: QGroupBox) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(cb)
        layout.addWidget(group)
        return w

    @staticmethod
    def _set_enabled(enabled: bool, *widgets):
        for w in widgets:
            w.setEnabled(enabled)

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
        # Keep current profile selection if possible, else default for new codec
        cur = self._profile_combo.currentData() or ""
        self._populate_profiles(codec, cur)

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load(self, g: RecordingSettings, ov: SourceOverrides):
        # Container
        has_cont = ov.container is not None
        self._cont_cb.setChecked(has_cont)
        cont = ov.container if has_cont else g.container
        (self._rb_mp4 if cont is Container.MP4 else self._rb_mov).setChecked(True)
        self._set_enabled(has_cont, self._rb_mp4, self._rb_mov)

        # Codec + Profile
        has_codec = ov.video_codec is not None
        self._codec_cb.setChecked(has_codec)
        codec = ov.video_codec if has_codec else g.video_codec
        self._rb_h264.setChecked(codec is VideoCodec.H264)
        self._rb_h265.setChecked(codec is VideoCodec.H265)
        if codec is VideoCodec.H264:
            profile = (ov.h264_profile.value if ov.h264_profile else g.h264_profile.value)
        else:
            profile = (ov.h265_profile.value if ov.h265_profile else g.h265_profile.value)
        self._populate_profiles(codec, profile)
        self._set_enabled(has_codec, self._rb_h264, self._rb_h265, self._profile_combo)

        # HW accel
        has_hw = ov.use_nvenc is not None
        self._hw_cb.setChecked(has_hw)
        self._nvenc_cb.setChecked(ov.use_nvenc if has_hw else g.use_nvenc)
        self._set_enabled(has_hw, self._nvenc_cb)

        # Video bitrate
        has_vbr = ov.video_bitrate_kbps is not None
        self._vbr_cb.setChecked(has_vbr)
        vbr = ov.video_bitrate_kbps if has_vbr else g.video_bitrate_kbps
        self._vbr_slider.setValue(vbr)
        self._vbr_spin.setValue(vbr)
        self._set_enabled(has_vbr, self._vbr_slider, self._vbr_spin)

        # Audio bitrate
        has_abr = ov.audio_bitrate_kbps is not None
        self._abr_cb.setChecked(has_abr)
        abr = ov.audio_bitrate_kbps if has_abr else g.audio_bitrate_kbps
        self._abr_slider.setValue(abr)
        self._abr_spin.setValue(abr)
        self._set_enabled(has_abr, self._abr_slider, self._abr_spin)

        # Audio source
        if ov.audio_source_name is not None:
            idx = self._audio_src_combo.findData(ov.audio_source_name)
            if idx >= 0:
                self._audio_src_combo.setCurrentIndex(idx)
            else:
                # Previously-saved source no longer available; reset to own audio
                self._audio_src_combo.setCurrentIndex(0)
        else:
            self._audio_src_combo.setCurrentIndex(0)

    def _accept(self):
        o = SourceOverrides()

        if self._cont_cb.isChecked():
            o.container = Container.MP4 if self._rb_mp4.isChecked() else Container.MOV

        if self._codec_cb.isChecked():
            o.video_codec = VideoCodec.H264 if self._rb_h264.isChecked() else VideoCodec.H265
            profile_val = self._profile_combo.currentData()
            if o.video_codec is VideoCodec.H264:
                o.h264_profile = H264Profile(profile_val)
            else:
                o.h265_profile = H265Profile(profile_val)

        if self._hw_cb.isChecked():
            o.use_nvenc = self._nvenc_cb.isChecked()

        if self._vbr_cb.isChecked():
            o.video_bitrate_kbps = self._vbr_spin.value()

        if self._abr_cb.isChecked():
            o.audio_bitrate_kbps = self._abr_spin.value()

        o.audio_source_name = self._audio_src_combo.currentData()

        self._result = o
        self.accept()

    # ------------------------------------------------------------------

    def get_overrides(self) -> SourceOverrides:
        return self._result if self._result is not None else SourceOverrides()
