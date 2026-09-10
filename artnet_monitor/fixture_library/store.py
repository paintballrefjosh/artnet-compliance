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

    def save_custom(self, profile: FixtureProfile):
        """Writes a custom profile to disk and adds it to the live catalog."""
        profile.source = "custom"
        safe_name = profile.id.split(":", 1)[-1].replace("/", "_")
        path = self.custom_dir / f"{safe_name}.json"
        with open(path, "w") as fh:
            json.dump(profile.to_dict(), fh, indent=2)
        self._profiles[profile.id] = profile
        return path

    def save_imported(self, profile: FixtureProfile):
        """Writes an OFL-imported profile to disk and adds it to the live catalog."""
        profile.source = "ofl"
        safe_name = profile.id.split(":", 1)[-1].replace("/", "_")
        path = self.imported_dir / f"{safe_name}.json"
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
        safe_name = profile_id.split(":", 1)[-1].replace("/", "_")
        path = self.custom_dir / f"{safe_name}.json"
        path.unlink(missing_ok=True)
        del self._profiles[profile_id]
