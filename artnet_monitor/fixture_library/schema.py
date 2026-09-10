"""
Internal fixture-profile schema. Every profile — whether bundled with this
app, imported from Open Fixture Library, or hand-written by a user — ends up
in this same shape once loaded, so nothing downstream (patch config,
compliance engine, dashboard) needs to know or care where it came from.

A profile doesn't describe *where* a fixture is patched (that's a "patched
fixture" entry in config/patch.yaml) — only what its channels *mean*, per
mode. Modes are just an ordered list of channel roles; the channel's DMX
offset within the fixture is simply its position in that list (offset 0 =
the fixture's start channel, offset 1 = start+1, etc.), which matches how
DMX modes actually work.

Role names are a loose, open vocabulary (not a strict enum) so importers and
custom profiles can introduce fixture-specific roles (e.g. "gobo_wheel")
without code changes — but a common set is used consistently so compliance
rules can reference them predictably: intensity, red, green, blue, white,
amber, uv, lime, indigo, pan, tilt, zoom, focus, iris, strobe, colorwheel,
gobo, macro, speed, reserved.
"""
from dataclasses import dataclass, field


@dataclass
class FixtureProfile:
    id: str                    # stable id, e.g. "ofl:chauvet-dj/slimpar-pro-h" or "custom:my-par"
    manufacturer: str
    model: str
    modes: dict                # mode name -> list of channel role strings, in DMX order
    source: str = "custom"     # "bundled" | "ofl" | "custom"
    categories: list = field(default_factory=list)
    notes: str = ""

    def channel_roles(self, mode: str) -> list:
        if mode not in self.modes:
            available = ", ".join(self.modes.keys())
            raise KeyError(f"Mode '{mode}' not defined for {self.id}. "
                            f"Available modes: {available}")
        return self.modes[mode]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "modes": self.modes,
            "source": self.source,
            "categories": self.categories,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FixtureProfile":
        return cls(
            id=data["id"],
            manufacturer=data["manufacturer"],
            model=data["model"],
            modes=data["modes"],
            source=data.get("source", "custom"),
            categories=data.get("categories", []),
            notes=data.get("notes", ""),
        )
