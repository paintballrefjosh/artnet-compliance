"""
FixtureLibraryStore: loads every profile — bundled reference fixtures shipped
with this app, ones imported from Open Fixture Library, and user-authored
custom ones — into a single in-memory catalog, keyed by profile id.

Layering (later wins on id collision, so your own edits always take priority):
  1. artnet_monitor/fixture_library/profiles/bundled/*.json  (shipped, read-only)
  2. config/fixture_profiles/imported/*.json                 (from OFL)
  3. config/fixture_profiles/custom/*.json                   (hand-written)
"""
import json
import logging
from pathlib import Path

from ..paths import resolve_within, safe_filename
from .schema import FixtureProfile

log = logging.getLogger("fixture_library")

BUNDLED_DIR = Path(__file__).parent / "profiles" / "bundled"


class FixtureLibraryStore:
    def __init__(self, config_dir: str):
        self.config_dir = Path(config_dir)
        self.imported_dir = self.config_dir / "fixture_profiles" / "imported"
        self.custom_dir = self.config_dir / "fixture_profiles" / "custom"
        self.imported_dir.mkdir(parents=True, exist_ok=True)
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self._profiles: dict[str, FixtureProfile] = {}
        self.reload()

    def reload(self):
        self._profiles = {}
        for directory in (BUNDLED_DIR, self.imported_dir, self.custom_dir):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    with open(path) as fh:
                        data = json.load(fh)
                    profile = FixtureProfile.from_dict(data)
                    self._profiles[profile.id] = profile
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning("Skipping invalid fixture profile %s: %s", path, e)
        log.info("Fixture library loaded: %d profile(s)", len(self._profiles))

    def list_profiles(self) -> list:
        return sorted(self._profiles.values(), key=lambda p: (p.manufacturer, p.model))

    def get(self, profile_id: str) -> FixtureProfile:
        if profile_id not in self._profiles:
            raise KeyError(f"Unknown fixture profile: {profile_id}")
        return self._profiles[profile_id]

    @staticmethod
    def _profile_path(directory: Path, profile_id: str) -> Path:
        """Where a profile id's JSON file lives. The id's source prefix
        ("custom:", "ofl:") is dropped and the rest sanitised into one
        filename component -- manufacturer/model reach this straight from
        the API as free text, so without that an id like
        "custom:..\\..\\evil" would write outside the profiles directory
        on Windows. resolve_within is the backstop if anything slips past."""
        safe_name = safe_filename(profile_id.split(":", 1)[-1])
        path = resolve_within(directory, f"{safe_name}.json")
        if path is None:
            raise ValueError(f"Refusing to use unsafe profile path for '{profile_id}'.")
        return path

    def save_custom(self, profile: FixtureProfile):
        """Writes a custom profile to disk and adds it to the live catalog."""
        profile.source = "custom"
        path = self._profile_path(self.custom_dir, profile.id)
        with open(path, "w") as fh:
            json.dump(profile.to_dict(), fh, indent=2)
        self._profiles[profile.id] = profile
        return path

    def save_imported(self, profile: FixtureProfile):
        """Writes an OFL-imported profile to disk and adds it to the live catalog."""
        profile.source = "ofl"
        path = self._profile_path(self.imported_dir, profile.id)
        with open(path, "w") as fh:
            json.dump(profile.to_dict(), fh, indent=2)
        self._profiles[profile.id] = profile
        return path

    def delete_custom(self, profile_id: str):
        """Removes a custom profile's file and drops it from the live catalog.
        Raises KeyError if it doesn't exist or isn't a custom profile."""
        profile = self.get(profile_id)
        if profile.source != "custom":
            raise ValueError(f"'{profile_id}' is not a custom profile.")
        self._profile_path(self.custom_dir, profile_id).unlink(missing_ok=True)
        del self._profiles[profile_id]
