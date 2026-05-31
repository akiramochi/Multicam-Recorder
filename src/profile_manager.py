r"""
Named-profile persistence for RecordingSettings + open source list.

Profiles are stored in a single JSON file:
  %APPDATA%\MulticamRecorder\profiles.json   (Windows)
  ~/.MulticamRecorder/profiles.json           (fallback)

File structure:
  {
    "active_profile": "Default",
    "profiles": {
      "Default": {
        "settings": { ...RecordingSettings fields... },
        "sources":  [
          {"type": "decklink", "name": "DeckLink Quad 2 (5)"},
          {"type": "ndi",      "name": "CAMERA1 (stream1)"}
        ]
      },
      "High Quality": { ... }
    }
  }
"""
import json
import os
from typing import Dict, List, Optional

from .recording_settings import RecordingSettings

_DATA_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "MulticamRecorder",
)
_PROFILES_FILE = os.path.join(_DATA_DIR, "profiles.json")
_DEFAULT_NAME  = "Default"


class ProfileManager:
    """
    Manages a collection of named profiles, each holding RecordingSettings
    plus the list of input sources that were open when the profile was saved.
    All mutating operations write through to disk immediately.
    """

    def __init__(self):
        self._profiles: Dict[str, dict] = {}   # name → {"settings": {}, "sources": []}
        self._active:   str             = _DEFAULT_NAME
        self._load()

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(_PROFILES_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._profiles = data.get("profiles", {})
            self._active   = data.get("active_profile", _DEFAULT_NAME)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        # Migrate old flat format (settings stored directly at profile root)
        for name, body in list(self._profiles.items()):
            if "settings" not in body:
                self._profiles[name] = {"settings": body, "sources": []}

        if not self._profiles:
            self._profiles[_DEFAULT_NAME] = {
                "settings": RecordingSettings().to_dict(),
                "sources":  [],
            }

        if self._active not in self._profiles:
            self._active = next(iter(self._profiles))

    def _flush(self) -> None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _PROFILES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"active_profile": self._active, "profiles": self._profiles},
                fh,
                indent=2,
                ensure_ascii=False,
            )
        os.replace(tmp, _PROFILES_FILE)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def profile_names(self) -> List[str]:
        return list(self._profiles.keys())

    @property
    def active_name(self) -> str:
        return self._active

    def get_settings(self, name: str) -> Optional[RecordingSettings]:
        """Return RecordingSettings for *name*, or None if not found."""
        body = self._profiles.get(name)
        if body is None:
            return None
        return RecordingSettings.from_dict(body.get("settings", {}))

    def get_sources(self, name: str) -> List[dict]:
        """Return the saved source list for *name* (may be empty)."""
        body = self._profiles.get(name, {})
        return list(body.get("sources", []))

    def get_active_settings(self) -> RecordingSettings:
        return self.get_settings(self._active) or RecordingSettings()

    def get_active_sources(self) -> List[dict]:
        return self.get_sources(self._active)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_profile(self, name: str,
                     settings: RecordingSettings,
                     sources:  List[dict]) -> None:
        """Overwrite (or create) a profile with *settings* and *sources*."""
        self._profiles[name] = {
            "settings": settings.to_dict(),
            "sources":  list(sources),
        }
        self._active = name
        self._flush()

    def new_profile(self, name: str,
                    settings: RecordingSettings,
                    sources:  List[dict]) -> bool:
        """
        Create a new profile.
        Returns False (and does nothing) if the name already exists.
        """
        if name in self._profiles:
            return False
        self._profiles[name] = {
            "settings": settings.to_dict(),
            "sources":  list(sources),
        }
        self._active = name
        self._flush()
        return True

    def rename_profile(self, old_name: str, new_name: str) -> bool:
        if old_name not in self._profiles or new_name in self._profiles:
            return False
        self._profiles[new_name] = self._profiles.pop(old_name)
        if self._active == old_name:
            self._active = new_name
        self._flush()
        return True

    def delete_profile(self, name: str) -> bool:
        if name not in self._profiles or len(self._profiles) <= 1:
            return False
        del self._profiles[name]
        if self._active == name:
            self._active = next(iter(self._profiles))
        self._flush()
        return True

    def set_active(self, name: str) -> None:
        """Record which profile is active without changing its contents."""
        if name in self._profiles:
            self._active = name
            self._flush()
