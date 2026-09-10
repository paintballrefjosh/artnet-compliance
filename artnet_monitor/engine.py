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
import hashlib
import threading
import time

from .config import Config, matches_applies_to


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

    @staticmethod
    def _decode_fixture_values(fixture, universe_data: dict) -> dict:
        """channel role -> current DMX value for one fixture, given a
        universe -> 512-byte buffer map. Shared by snapshot() and the
        compliance report so both decode channels the same way."""
        values = {}
        data = universe_data.get(fixture.universe, b"\x00" * 512)
        for role, chan in fixture.channels.items():
            idx = chan - 1  # channel numbers in config.yaml are absolute, 1-based
            values[role] = data[idx] if 0 <= idx < len(data) else 0
        return values

    def owners_fingerprint(self) -> str:
        """Short hash of everything that determines the channel-owner map.

        The owner map is by far the biggest part of a snapshot (~51KB of a
        94KB payload with 80 fixtures patched) but only changes when the
        patch does -- so the websocket loop uses this to skip re-sending it
        on every 2 Hz tick. Hashing the fixtures' addresses is much cheaper
        than serialising the ~950 owner objects it would produce.
        """
        parts = [
            f"{f.name}|{f.universe}|" + ",".join(f"{r}:{c}" for r, c in sorted(f.channels.items()))
            for f in self.config.fixtures
        ]
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]

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

    def snapshot(self, include_owners: bool = True) -> dict:
        """
        Returns a JSON-serializable view of every fixture's current values plus
        any compliance violations, for the web dashboard.

        `include_owners=False` omits each universe's channel-owner map --
        the largest part of the payload, and one that only changes when the
        patch does. The websocket loop drops it on ticks where
        owners_fingerprint() is unchanged and the client reuses its cached
        copy; REST callers get it every time.
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
            values = self._decode_fixture_values(fixture, universe_data)
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
            "owners_version": self.owners_fingerprint(),
            "universes": {
                u: {
                    "last_update": last_update.get(u),
                    "channels": list(universe_data.get(u, b"\x00" * 512)[:512]),
                    **({"owners": {str(ch): info for ch, info in owners.get(u, {}).items()}}
                       if include_owners else {}),
                }
                for u in self.config.universes
            },
            "fixtures": fixtures_out,
            "any_violations": any_violations,
        }

    def _rule_bindings_for_cue(self, cue) -> list:
        """Every (rule, effective_applies_to) pair that would be enforced if
        `cue` were the active cue: every always-active rule (targeted by its
        own applies_to) plus every rule this cue assigns (targeted by that
        assignment's applies_to). Deduped by rule id, always-active rules
        taking precedence if a rule is somehow both -- mirrors _rules_for,
        generalized to an arbitrary cue instead of self.active_cue_name so
        the compliance report can judge every cue at once without touching
        which one is actually live."""
        seen_ids = set()
        bindings = []
        for rule in self.config.rules:
            if rule.always_active and rule.id not in seen_ids:
                bindings.append((rule, rule.applies_to))
                seen_ids.add(rule.id)
        for assignment in cue.rule_assignments:
            rule = self.config.get_rule(assignment.rule_id)
            if rule is None or rule.id in seen_ids:
                continue
            bindings.append((rule, assignment.applies_to))
            seen_ids.add(rule.id)
        return bindings

    def _evaluate_cue_compliance(self, cue, fixture_values: dict, group_membership: dict) -> dict:
        """Checks one cue's effective rules against already-decoded current
        fixture values (see _decode_fixture_values), as if this cue were
        active. `fixture_values` is {fixture_name: {role: value}}.

        Status is "unconfigured" only when this cue has *zero* applicable
        rules at all -- own assignments AND always-active ones combined --
        so a cue with no rules of its own can never have a real violation
        (from a global always-active rule) hidden behind a neutral badge."""
        bindings = self._rule_bindings_for_cue(cue)
        rule_reports = []
        any_violation = False

        for rule, applies_to in bindings:
            target_fixtures = [f for f in self.config.fixtures
                                if matches_applies_to(applies_to, f.name, group_membership)]
            seen_channels = set()
            violation_detail = []   # one row per (fixture, channel) violation
            for fixture in target_fixtures:
                values = fixture_values.get(fixture.name, {})
                for entry in rule.entries:
                    if entry.channel not in values:
                        continue
                    value = values[entry.channel]
                    if entry.check(value):
                        continue
                    seen_channels.add(entry.channel)
                    violation_detail.append({
                        "fixture": fixture.name, "channel": entry.channel,
                        "value": value, "min": entry.min, "max": entry.max,
                    })
            # Ordered to match the rule's own entries (e.g. always
            # "intensity, red, ...") rather than fixture-discovery order,
            # which would otherwise vary depending on which fixture happened
            # to violate first.
            bad_channels = [e.channel for e in rule.entries if e.channel in seen_channels]
            if bad_channels:
                any_violation = True
                rule_reports.append({
                    "rule_id": rule.id,
                    "rule_name": rule.name or "(unnamed)",
                    "channels": bad_channels,
                    "violations": violation_detail,
                })

        if not bindings:
            status = "unconfigured"
        elif any_violation:
            status = "violation"
        else:
            status = "compliant"

        return {
            "name": cue.name,
            "description": cue.description,
            "status": status,
            "rule_reports": rule_reports,
        }

    def generate_compliance_report(self) -> dict:
        """A frozen, timestamped snapshot judging *every* cue against
        current live DMX at once -- each as if it alone were active -- for
        the Report page. Unlike snapshot() (which only evaluates whichever
        cue is actually selected), this doesn't touch active_cue_name."""
        with self._lock:
            universe_data = dict(self._universe_data)
            active_cue_name = self.active_cue_name

        fixture_values = {
            f.name: self._decode_fixture_values(f, universe_data) for f in self.config.fixtures
        }
        group_membership = self.config.group_membership()

        return {
            "generated_at": time.time(),
            "active_cue": active_cue_name,
            "cues": [
                self._evaluate_cue_compliance(cue, fixture_values, group_membership)
                for cue in self.config.cues
            ],
        }
