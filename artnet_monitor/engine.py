"""
ComplianceEngine: owns current state (latest DMX per universe), decodes it into
per-fixture channel values, and evaluates rules to flag violations.

Rules live in a single shared pool (Config.rules). Rules flagged
always_active apply no matter what. When a cue is selected (via
set_active_cue), whatever rules that cue's rule_ids point to are checked
*in addition to* the always-active ones -- so e.g. an audience blinder can
have an always-active "never above 200" cap, plus a cue-specific "must be 0
during Cue 5" rule that only applies while that cue is selected. A rule
could technically be both always_active and selected by a cue; duplicates
are removed by rule id so it doesn't produce a doubled-up violation entry.

Thread-safety note: `ingest_frame` is called from the Art-Net receive thread;
`snapshot` (and the active-cue setter) are called from the web server
thread. A simple lock keeps state consistent between them.
"""
import threading
import time

from .config import Config


class ComplianceEngine:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._universe_data = {u: bytes(512) for u in config.universes}
        self._last_update = {u: None for u in config.universes}
        self.active_cue_name = None

    def ingest_frame(self, universe: int, data: bytes):
        padded = data.ljust(512, b"\x00")[:512]
        with self._lock:
            self._universe_data[universe] = padded
            self._last_update[universe] = time.time()

    def set_active_cue(self, name: str | None):
        with self._lock:
            self.active_cue_name = name

    def _rules_for(self, fixture_name: str, group_membership: dict) -> list:
        seen_ids = set()
        combined = []
        for rule in self.config.always_active_rules_for(fixture_name, group_membership):
            if rule.id not in seen_ids:
                combined.append(rule)
                seen_ids.add(rule.id)
        if self.active_cue_name:
            cue = self.config.get_cue(self.active_cue_name)
            if cue is not None:
                for rule in self.config.cue_rules_for(cue, fixture_name, group_membership):
                    if rule.id not in seen_ids:
                        combined.append(rule)
                        seen_ids.add(rule.id)
        return combined

    def _channel_owners(self) -> dict:
        """Universe -> {channel_number: {'fixture': name, 'role': role}} reverse
        lookup, built fresh each snapshot from the current fixture patch --
        cheap unless you have an enormous number of fixtures, and always
        correct even right after a patch add/remove with no cache to bust."""
        owners = {u: {} for u in self.config.universes}
        for fixture in self.config.fixtures:
            bucket = owners.setdefault(fixture.universe, {})
            for role, chan in fixture.channels.items():
                bucket[chan] = {"fixture": fixture.name, "role": role}
        return owners

    def snapshot(self) -> dict:
        """
        Returns a JSON-serializable view of every fixture's current values plus
        any compliance violations, for the web dashboard.
        """
        with self._lock:
            universe_data = dict(self._universe_data)
            last_update = dict(self._last_update)
            active_cue_name = self.active_cue_name

        owners = self._channel_owners()
        group_membership = self.config.group_membership()
        fixtures_out = []
        any_violations = False

        for fixture in self.config.fixtures:
            values = {}
            data = universe_data.get(fixture.universe, b"\x00" * 512)
            for role, chan in fixture.channels.items():
                idx = chan - 1  # channel numbers in config.yaml are absolute, 1-based
                values[role] = data[idx] if 0 <= idx < len(data) else 0

            applicable_rules = self._rules_for(fixture.name, group_membership)

            checked_roles = set()
            violations = []
            for rule in applicable_rules:
                for entry in rule.entries:
                    if entry.channel not in values:
                        continue
                    checked_roles.add(entry.channel)
                    value = values[entry.channel]
                    if not entry.check(value):
                        violations.append({
                            "channel": entry.channel,
                            "value": value,
                            "max": entry.max,
                            "min": entry.min,
                            "name": rule.name,
                            "description": rule.description,
                        })
            checked_roles = sorted(checked_roles)
            if violations:
                any_violations = True

            fixtures_out.append({
                "name": fixture.name,
                "universe": fixture.universe,
                "start_channel": fixture.start_channel,
                "profile": fixture.profile_id,
                "mode": fixture.mode,
                "values": values,
                "checked_roles": checked_roles,
                "violations": violations,
                "compliant": len(violations) == 0,
            })

        return {
            "generated_at": time.time(),
            "active_cue": active_cue_name,
            "universes": {
                u: {
                    "last_update": last_update.get(u),
                    "channels": list(universe_data.get(u, b"\x00" * 512)[:512]),
                    "owners": {str(ch): info for ch, info in owners.get(u, {}).items()},
                }
                for u in self.config.universes
            },
            "fixtures": fixtures_out,
            "any_violations": any_violations,
        }
