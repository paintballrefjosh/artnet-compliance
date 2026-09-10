"""
Imports fixture definitions from Open Fixture Library (open-fixture-library.org),
converting OFL's JSON schema into our internal FixtureProfile shape.

OFL is organized as one JSON file per fixture, at a URL like:
  https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/
    master/fixtures/<manufacturer-key>/<fixture-key>.json

Find the manufacturer-key/fixture-key pair from a fixture's URL on
open-fixture-library.org — e.g. the page
  https://open-fixture-library.org/chauvet-dj/slimpar-pro-h
has manufacturer_key="chauvet-dj", fixture_key="slimpar-pro-h".

Each OFL fixture defines `availableChannels` (channel name -> capability
info) and `modes` (ordered list of channel names per mode). This module
flattens that into our simpler {mode_name: [role, role, ...]} shape — one
role per DMX channel position, in order.

Limitation: channels with multiple DMX-range-dependent capabilities (e.g. a
channel that's "no function" for one range and "strobe" for another) are
reduced to a single representative role, taken from the first capability
listed. That's enough to validate intensity/color against a standard, but
loses OFL's finer per-range behavior — worth knowing if a rule ever needs to
key off exact DMX ranges rather than just role + value.
"""
import json
import urllib.request
import urllib.error

from .schema import FixtureProfile

OFL_RAW_BASE = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/fixtures"

# OFL capability "type" (+ "color" for ColorIntensity) -> our role vocabulary.
_CAPABILITY_ROLE_MAP = {
    "Intensity": "intensity",
    "ShutterStrobe": "strobe",
    "StrobeSpeed": "strobe",
    "StrobeDuration": "strobe",
    "Pan": "pan",
    "PanContinuous": "pan",
    "Tilt": "tilt",
    "TiltContinuous": "tilt",
    "Zoom": "zoom",
    "Focus": "focus",
    "Iris": "iris",
    "IrisEffect": "iris",
    "ColorWheelIndex": "colorwheel",
    "ColorWheelRotation": "colorwheel",
    "ColorTemperature": "colortemp",
    "WheelSlot": "gobo",
    "WheelRotation": "gobo",
    "WheelShake": "gobo",
    "Effect": "effect",
    "EffectSpeed": "speed",
    "Speed": "speed",
    "Maintenance": "maintenance",
    "NoFunction": "reserved",
    "Fog": "fog",
    "FogOutput": "fog",
    "Rotation": "rotation",
    "Prism": "prism",
    "PrismRotation": "prism",
}

_COLOR_ROLE_MAP = {
    "Red": "red", "Green": "green", "Blue": "blue", "White": "white",
    "Amber": "amber", "UV": "uv", "Lime": "lime", "Indigo": "indigo",
    "Cyan": "cyan", "Magenta": "magenta", "Yellow": "yellow",
    "Warm White": "warm_white", "Cold White": "cold_white",
}


def _channel_role(channel_def: dict) -> str:
    cap = channel_def.get("capability") or (channel_def.get("capabilities") or [{}])[0]
    cap_type = cap.get("type", "")
    if cap_type == "ColorIntensity":
        return _COLOR_ROLE_MAP.get(cap.get("color", ""), "color")
    return _CAPABILITY_ROLE_MAP.get(cap_type, cap_type.lower() or "reserved")


def convert_ofl_fixture(manufacturer_key: str, fixture_key: str, raw: dict) -> FixtureProfile:
    available = raw.get("availableChannels", {})
    # Template channels (used for repeated sub-fixtures like pixel matrices)
    # aren't resolved here — most standard fixtures don't need them, and
    # supporting matrices properly is a bigger feature than this needs yet.
    channel_roles_by_name = {
        name: _channel_role(defn) for name, defn in available.items()
    }

    modes = {}
    for mode in raw.get("modes", []):
        roles = []
        for chan_name in mode.get("channels", []):
            if chan_name is None:
                roles.append("reserved")
            else:
                roles.append(channel_roles_by_name.get(chan_name, "reserved"))
        modes[mode["name"]] = roles

    return FixtureProfile(
        id=f"ofl:{manufacturer_key}/{fixture_key}",
        manufacturer=manufacturer_key,
        model=raw.get("name", fixture_key),
        modes=modes,
        source="ofl",
        categories=raw.get("categories", []),
        notes=f"Imported from Open Fixture Library ({manufacturer_key}/{fixture_key})",
    )


def fetch_ofl_fixture(manufacturer_key: str, fixture_key: str) -> FixtureProfile:
    """
    Downloads and converts a single fixture definition from Open Fixture
    Library. Raises ValueError with a clear message on a 404 (wrong slug) or
    other fetch failure — the caller should surface that directly to the user
    rather than a raw stack trace, since a typo'd slug is the common case.
    """
    url = f"{OFL_RAW_BASE}/{manufacturer_key}/{fixture_key}.json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(
                f"No fixture found at {manufacturer_key}/{fixture_key} on Open "
                f"Fixture Library. Double check the slugs from the fixture's "
                f"URL on open-fixture-library.org."
            ) from e
        raise ValueError(f"Fetch failed ({e.code}): {url}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach Open Fixture Library: {e.reason}") from e

    return convert_ofl_fixture(manufacturer_key, fixture_key, raw)
