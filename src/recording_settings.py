from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VideoCodec(Enum):
    H264 = "libx264"
    H265 = "libx265"


class Container(Enum):
    MP4 = "mp4"
    MOV = "mov"


class H264Profile(Enum):
    BASELINE = "baseline"
    MAIN     = "main"
    HIGH     = "high"


class H265Profile(Enum):
    MAIN   = "main"
    MAIN10 = "main10"


@dataclass
class RecordingSettings:
    video_codec:        VideoCodec  = VideoCodec.H264
    container:          Container   = Container.MP4
    video_bitrate_kbps: int         = 8000
    audio_bitrate_kbps: int         = 192
    output_directory:   str         = ""
    h264_profile:       H264Profile = H264Profile.HIGH
    h265_profile:       H265Profile = H265Profile.MAIN

    # Hardware encoding
    use_nvenc:          bool        = False   # True → h264_nvenc / hevc_nvenc

    # OSC / Bitfocus Companion
    osc_enabled:        bool        = False
    osc_listen_port:    int         = 9000
    osc_feedback_ip:    str         = "127.0.0.1"
    osc_feedback_port:  int         = 12321

    @property
    def video_bitrate_bps(self) -> int:
        return self.video_bitrate_kbps * 1000

    @property
    def audio_bitrate_bps(self) -> int:
        return self.audio_bitrate_kbps * 1000

    @property
    def file_extension(self) -> str:
        return self.container.value

    @property
    def active_video_profile(self) -> str:
        if self.video_codec == VideoCodec.H264:
            return self.h264_profile.value
        return self.h265_profile.value

    @property
    def effective_video_codec(self) -> str:
        """
        FFmpeg codec string to pass to av.add_stream().
        Substitutes NVENC variants (h264_nvenc / hevc_nvenc) when use_nvenc
        is True; otherwise returns the software codec (libx264 / libx265).
        """
        if self.use_nvenc:
            return "hevc_nvenc" if self.video_codec == VideoCodec.H265 else "h264_nvenc"
        return self.video_codec.value

    # ------------------------------------------------------------------
    # Serialisation (used by ProfileManager)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "video_codec":        self.video_codec.value,
            "container":          self.container.value,
            "video_bitrate_kbps": self.video_bitrate_kbps,
            "audio_bitrate_kbps": self.audio_bitrate_kbps,
            "output_directory":   self.output_directory,
            "h264_profile":       self.h264_profile.value,
            "h265_profile":       self.h265_profile.value,
            "use_nvenc":          self.use_nvenc,
            "osc_enabled":        self.osc_enabled,
            "osc_listen_port":    self.osc_listen_port,
            "osc_feedback_ip":    self.osc_feedback_ip,
            "osc_feedback_port":  self.osc_feedback_port,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecordingSettings":
        """
        Build a RecordingSettings from a plain dict.
        Unknown or invalid keys are silently ignored; defaults fill the rest.
        """
        s = cls()
        try:
            s.video_codec        = VideoCodec(d.get("video_codec",        s.video_codec.value))
            s.container          = Container(d.get("container",            s.container.value))
            s.h264_profile       = H264Profile(d.get("h264_profile",      s.h264_profile.value))
            s.h265_profile       = H265Profile(d.get("h265_profile",      s.h265_profile.value))
            s.video_bitrate_kbps = int(d.get("video_bitrate_kbps",        s.video_bitrate_kbps))
            s.audio_bitrate_kbps = int(d.get("audio_bitrate_kbps",        s.audio_bitrate_kbps))
            s.output_directory   = str(d.get("output_directory",          s.output_directory))
            s.use_nvenc          = bool(d.get("use_nvenc",                 s.use_nvenc))
            s.osc_enabled        = bool(d.get("osc_enabled",              s.osc_enabled))
            s.osc_listen_port    = int(d.get("osc_listen_port",           s.osc_listen_port))
            s.osc_feedback_ip    = str(d.get("osc_feedback_ip",           s.osc_feedback_ip)) or "127.0.0.1"
            s.osc_feedback_port  = int(d.get("osc_feedback_port",         s.osc_feedback_port))
        except Exception:
            pass
        return s

    def copy(self) -> "RecordingSettings":
        return RecordingSettings.from_dict(self.to_dict())


@dataclass
class SourceOverrides:
    """Per-source overrides that supersede the global RecordingSettings.

    Any field left as None means "use the global value".
    """
    video_codec:        Optional[VideoCodec]  = None
    container:          Optional[Container]   = None
    video_bitrate_kbps: Optional[int]         = None
    audio_bitrate_kbps: Optional[int]         = None
    h264_profile:       Optional[H264Profile] = None
    h265_profile:       Optional[H265Profile] = None
    use_nvenc:          Optional[bool]        = None

    def apply_to(self, base: RecordingSettings) -> RecordingSettings:
        """Return a copy of *base* with non-None overrides applied."""
        s = base.copy()
        if self.video_codec        is not None: s.video_codec        = self.video_codec
        if self.container          is not None: s.container          = self.container
        if self.video_bitrate_kbps is not None: s.video_bitrate_kbps = self.video_bitrate_kbps
        if self.audio_bitrate_kbps is not None: s.audio_bitrate_kbps = self.audio_bitrate_kbps
        if self.h264_profile       is not None: s.h264_profile       = self.h264_profile
        if self.h265_profile       is not None: s.h265_profile       = self.h265_profile
        if self.use_nvenc          is not None: s.use_nvenc          = self.use_nvenc
        return s

    def has_any(self) -> bool:
        return any(v is not None for v in (
            self.video_codec, self.container,
            self.video_bitrate_kbps, self.audio_bitrate_kbps,
            self.h264_profile, self.h265_profile, self.use_nvenc,
        ))

    def to_dict(self) -> dict:
        d: dict = {}
        if self.video_codec        is not None: d["video_codec"]        = self.video_codec.value
        if self.container          is not None: d["container"]          = self.container.value
        if self.video_bitrate_kbps is not None: d["video_bitrate_kbps"] = self.video_bitrate_kbps
        if self.audio_bitrate_kbps is not None: d["audio_bitrate_kbps"] = self.audio_bitrate_kbps
        if self.h264_profile       is not None: d["h264_profile"]       = self.h264_profile.value
        if self.h265_profile       is not None: d["h265_profile"]       = self.h265_profile.value
        if self.use_nvenc          is not None: d["use_nvenc"]          = self.use_nvenc
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SourceOverrides":
        o = cls()
        try:
            if "video_codec"        in d: o.video_codec        = VideoCodec(d["video_codec"])
            if "container"          in d: o.container          = Container(d["container"])
            if "video_bitrate_kbps" in d: o.video_bitrate_kbps = int(d["video_bitrate_kbps"])
            if "audio_bitrate_kbps" in d: o.audio_bitrate_kbps = int(d["audio_bitrate_kbps"])
            if "h264_profile"       in d: o.h264_profile       = H264Profile(d["h264_profile"])
            if "h265_profile"       in d: o.h265_profile       = H265Profile(d["h265_profile"])
            if "use_nvenc"          in d: o.use_nvenc          = bool(d["use_nvenc"])
        except Exception:
            pass
        return o
