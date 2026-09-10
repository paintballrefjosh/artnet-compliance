"""Loads config/patch.yaml into simple, easy-to-query Python objects."""
from dataclasses import dataclass, field
import uuid

import yaml

from .paths import read_text_lenient


def new_rule_id() -> str:
    return "rule-" + uuid.uuid4().hex[:8]


@dataclass
class Fixture:
    name: str
    universe: int
    start_channel: int
    channels: dict            # role name -> absolute channel number (1-based)
    profile_id: str = None    # set if this fixture was patched from a library profile
    mode: str = None


@dataclass
class RuleEntry:
    """One channel-role threshold within a Rule package -- e.g. {channel:
    "dimmer", max: 166, min: 166}. Entries carry no name/target of their
    own; those live on the enclosing Rule and apply to every entry in it."""
    channel: str
    max: int | None = None
    min: int | None = None

    def check(self, value: int) -> bool:
        """Returns True if `value` is COMPLIANT (i.e. does not violate this entry)."""
        if self.max is not None and value > self.max:
            return False
        if self.min is not None and value < self.min:
            return False
        return True


@dataclass
class Rule:
    """
    A named package of one or more channel-role thresholds ("entries"),
    living in the shared rule pool (the top-level `rules:` list) and
    optionally scoped to a fixture profile -- e.g. "House Whites 50%" might
    set dimmer/red/green/blue/strobe thresholds all at once under one name,
    decoupled from which specific fixture or cue actually enforces it.

    `always_active=True` means every entry is enforced no matter what,
    against whatever this rule's own `applies_to` names (a baseline safety
    limit -- there's no cue to attach a target to, so the target lives on
    the rule itself). `always_active=False` (the common case) means the
    rule has no fixed target of its own; instead, each cue that selects it
    specifies its own target via that cue's rule_assignments, so the same
    reusable rule can be pointed at different fixtures/groups in different
    cues. Either way, the target applies to *all* of this rule's entries at
    once -- that's the point of grouping them.
    """
    id: str
    name: str
    profile_id: str | None = None  # which fixture profile this rule is designed for
    description: str = ""
    applies_to: object = "*"  # only meaningful when always_active=True
    always_active: bool = False
    entries: list = field(default_factory=list)  # list[RuleEntry]

    def applies(self, fixture_name: str, group_membership: dict | None = None) -> bool:
        return matches_applies_to(self.applies_to, fixture_name, group_membership)


def matches_applies_to(applies_to, fixture_name: str, group_membership: dict | None = None) -> bool:
    """Standalone version of the applies_to matching logic, usable both for
    a Rule's own applies_to (always-active rules) and for a cue
    assignment's applies_to (cue-specific rules), which are no longer
    necessarily the same object."""
    if applies_to == "*":
        return True
    group_membership = group_membership or {}
    for entry in applies_to:
        if isinstance(entry, str) and entry.startswith("group:"):
            group_name = entry[len("group:"):]
            if fixture_name in group_membership.get(group_name, ()):
                return True
        elif entry == fixture_name:
            return True
    return False


def _rules_matching(rules: list, fixture_name: str, group_membership: dict | None = None) -> list:
    return [r for r in rules if r.applies(fixture_name, group_membership)]


def group_ref(name: str) -> str:
    """The `applies_to` token that targets a whole group."""
    return f"group:{name}"


def retarget_group_reference(applies_to, old_name: str, new_name: str):
    """
    Rewrites any "group:<old_name>" entry in an `applies_to` value to point at
    `new_name`, returning (new_applies_to, changed). Used when a group is
    renamed so every rule and cue assignment that targeted it keeps working.

    "*" and any other non-list value passes through untouched — those don't
    name a group in the first place.
    """
    if not isinstance(applies_to, list):
        return applies_to, False

    old_ref, new_ref = group_ref(old_name), group_ref(new_name)
    changed = False
    rewritten = []
    for entry in applies_to:
        if entry == old_ref:
            rewritten.append(new_ref)
            changed = True
        else:
            rewritten.append(entry)
    return rewritten, changed


def retarget_fixture_reference(applies_to, old_name: str, new_name: str):
    """
    Rewrites a bare fixture-name entry in an `applies_to` value from
    `old_name` to `new_name`, returning (new_applies_to, changed).

    The fixture-name counterpart to retarget_group_reference. Only exact
    plain-name entries match -- a "group:<name>" token naming a *group*
    that happens to share this fixture's name is left alone.
    """
    if not isinstance(applies_to, list):
        return applies_to, False

    changed = False
    rewritten = []
    for entry in applies_to:
        if entry == old_name:
            rewritten.append(new_name)
            changed = True
        else:
            rewritten.append(entry)
    return rewritten, changed


@dataclass
class Group:
    """A named set of fixtures (e.g. "House Whites", "Stage Front"), so a
    single rule's `applies_to` can target the whole set via "group:<name>"
    instead of listing every fixture individually. Shown to the user as
    "Fixture Groups"."""
    name: str
    fixtures: list = field(default_factory=list)


@dataclass
class CueRuleAssignment:
    """Points a cue at one rule from the pool, plus the specific target
    (fixture name(s) and/or "group:<name>" entries, or "*") that rule
    should be checked against *for this cue*. The same rule can be assigned
    to different targets in different cues."""
    rule_id: str
    applies_to: object = "*"


@dataclass
class Cue:
    """
    A named, selectable set of rule assignments — e.g. "Cue 12 - Blackout"
    might assign a "must be 0" rule to a specific blinder. Only one cue is
    "active" at a time (chosen from the dashboard); its assigned rules are
    checked *in addition to* whatever rules in the pool are flagged
    `always_active`, which apply no matter which cue (if any) is active.
    """
    name: str
    description: str = ""
    rule_assignments: list = field(default_factory=list)  # list of CueRuleAssignment


@dataclass
class Config:
    listen_ip: str
    universes: list
    fixtures: list = field(default_factory=list)
    rules: list = field(default_factory=list)   # the shared rule pool
    cues: list = field(default_factory=list)
    groups: list = field(default_factory=list)

    def fixtures_in_universe(self, universe: int):
        return [f for f in self.fixtures if f.universe == universe]

    def group_membership(self) -> dict:
        return {g.name: set(g.fixtures) for g in self.groups}

    def get_rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def always_active_rules_for(self, fixture_name: str, group_membership: dict | None = None):
        """Baseline rules: everything in the pool flagged always_active,
        filtered to ones that apply to this fixture."""
        if group_membership is None:
            group_membership = self.group_membership()
        return _rules_matching([r for r in self.rules if r.always_active], fixture_name, group_membership)

    def cue_rules_for(self, cue: "Cue", fixture_name: str, group_membership: dict | None = None):
        """Resolves a cue's rule assignments against the pool, filtered to
        ones whose *assignment* target (not the rule's own applies_to)
        matches this fixture. Unknown rule ids (e.g. a rule deleted out
        from under a cue) are silently skipped."""
        if group_membership is None:
            group_membership = self.group_membership()
        matched = []
        for assignment in cue.rule_assignments:
            rule = self.get_rule(assignment.rule_id)
            if rule is None:
                continue
            if matches_applies_to(assignment.applies_to, fixture_name, group_membership):
                matched.append(rule)
        return matched

    def get_cue(self, name: str) -> Cue | None:
        return next((c for c in self.cues if c.name == name), None)

    def get_group(self, name: str) -> Group | None:
        return next((g for g in self.groups if g.name == name), None)


def channel_conflicts(fixtures: list) -> list:
    """
    Finds fixtures whose patched channels overlap. Returns one entry per
    contested channel: {universe, channel, fixtures: [name, ...]}.

    Overlapping addresses were silently allowed before this existed -- the
    second fixture just quietly took over those channels in the snapshot's
    owner map, so it vanished from the grids while still being compliance-
    checked invisibly. Console patches genuinely do get typed wrong, so
    this reports rather than guesses.
    """
    occupied = {}  # (universe, channel) -> [fixture names, in patch order]
    for fixture in fixtures:
        for chan in fixture.channels.values():
            occupied.setdefault((fixture.universe, chan), []).append(fixture.name)

    return [
        {"universe": universe, "channel": chan, "fixtures": names}
        for (universe, chan), names in sorted(occupied.items())
        if len(names) > 1
    ]


def conflicts_for(fixtures: list, candidate, ignore_name: str = None) -> list:
    """Channels `candidate` would collide with among `fixtures`, ignoring
    the fixture named `ignore_name` (so re-addressing a fixture doesn't
    report a conflict with its own current position). Returns
    [{channel, universe, fixture}, ...]."""
    taken = {}
    for fixture in fixtures:
        if ignore_name is not None and fixture.name == ignore_name:
            continue
        for chan in fixture.channels.values():
            taken[(fixture.universe, chan)] = fixture.name

    clashes = []
    for chan in sorted(candidate.channels.values()):
        owner = taken.get((candidate.universe, chan))
        if owner is not None:
            clashes.append({"universe": candidate.universe, "channel": chan, "fixture": owner})
    return clashes


def sequential_addresses(universe: int, start_channel: int, channels_per_unit: int,
                          quantity: int) -> list:
    """
    Returns `quantity` (universe, start_channel) pairs, each `channels_per_unit`
    DMX channels wide, packed sequentially starting at (universe, start_channel).
    Rolls over to the next universe (resetting to channel 1) whenever a unit
    would otherwise spill past channel 512 — used for the dashboard's
    "add N of this fixture" quantity feature so a run of fixtures never
    silently produces overlapping or out-of-range addresses.
    """
    if channels_per_unit < 1:
        raise ValueError("channels_per_unit must be at least 1")
    addresses = []
    u, ch = universe, start_channel
    for _ in range(quantity):
        if ch + channels_per_unit - 1 > 512:
            u += 1
            ch = 1
        addresses.append((u, ch))
        ch += channels_per_unit
    return addresses


def build_fixture(raw: dict, library=None) -> Fixture:
    """
    Builds a Fixture from a raw dict — either a patch.yaml entry or a JSON
    body from the dashboard's "add fixture" form. Two ways to specify
    channels:
      - `profile` + `mode`: resolved against the fixture library. Channel
        offsets come from the profile's channel-role order, starting at
        `start_channel`. Roles the profile marks "reserved" are skipped.
      - `channels`: an inline {role: absolute_channel_number} map, for
        one-off fixtures that don't warrant a library profile.
    """
    universe = raw["universe"]
    start_channel = raw["start_channel"]

    if raw.get("profile"):
        if library is None:
            raise ValueError(
                f"Fixture '{raw['name']}' references profile '{raw['profile']}' "
                f"but no fixture library is available to resolve it."
            )
        profile = library.get(raw["profile"])
        mode = raw["mode"]
        roles = profile.channel_roles(mode)
        channels = {
            role: start_channel + i
            for i, role in enumerate(roles)
            if role != "reserved"
        }
        return Fixture(
            name=raw["name"], universe=universe, start_channel=start_channel,
            channels=channels, profile_id=raw["profile"], mode=mode,
        )

    if "channels" in raw:
        return Fixture(
            name=raw["name"], universe=universe, start_channel=start_channel,
            channels=raw["channels"],
        )

    raise ValueError(f"Fixture '{raw.get('name', '?')}' needs either "
                      f"'profile'+'mode' or an inline 'channels' map.")


def build_rule_entry(raw: dict) -> RuleEntry:
    return RuleEntry(channel=raw["channel"], max=raw.get("max"), min=raw.get("min"))


def build_rule(raw: dict) -> Rule:
    """
    Builds a Rule (package) from a raw dict. Two shapes on the way in:
      - current: an `entries:` list, each {channel, max, min}.
      - legacy (pre-package): channel/max/min directly on the rule itself,
        from before a rule could hold more than one threshold. Folded into a
        single-entry package here so old patch.yaml data and cue references
        keep working untouched -- only rewritten to the new shape on disk if
        that rule is later edited through the API (see patch_writer.update_rule).
    """
    entries = raw.get("entries")
    if entries is None:
        entries = [{"channel": raw["channel"], "max": raw.get("max"), "min": raw.get("min")}] \
            if "channel" in raw else []

    return Rule(
        id=raw.get("id") or new_rule_id(),
        name=raw.get("name", ""),
        profile_id=raw.get("profile_id"),
        description=raw.get("description", ""),
        applies_to=raw.get("applies_to", "*"),
        always_active=raw.get("always_active", False),
        entries=[build_rule_entry(e) for e in entries],
    )


def build_group(raw: dict) -> Group:
    return Group(name=raw["name"], fixtures=raw.get("fixtures", []) or [])


def build_cue_assignment(raw: dict) -> CueRuleAssignment:
    return CueRuleAssignment(rule_id=raw["rule_id"], applies_to=raw.get("applies_to", "*"))


def build_cue(raw: dict) -> Cue:
    return Cue(
        name=raw["name"],
        description=raw.get("description", ""),
        rule_assignments=[build_cue_assignment(a) for a in raw.get("rule_assignments", []) or []],
    )


def load_config(path: str, library=None) -> Config:
    return parse_config(yaml.safe_load(read_text_lenient(path)), library=library)


def parse_config(raw: dict, library=None) -> Config:
    """The actual patch.yaml -> Config logic, split out from load_config so
    it can run against an already-parsed dict -- e.g. one read from an
    uploaded backup .zip's patch.yaml rather than a file on disk (see
    web/app.py's /api/backup/import). Every schema migration below applies
    equally either way."""
    fixtures = [build_fixture(f, library) for f in raw.get("fixtures", []) or []]
    groups = [build_group(g) for g in raw.get("groups", []) or []]

    # Top-level `rules:` entries are the shared pool. Anything from before
    # the always_active flag existed was implicitly "always applies", so
    # default it to True here rather than in build_rule (which is also used
    # for cue-migrated rules below, which should default to False instead).
    rules = []
    for r in raw.get("rules", []) or []:
        rule = build_rule(r)
        if "always_active" not in r:
            rule.always_active = True
        rules.append(rule)

    # Cues have gone through two schema generations before this one:
    #   1. (oldest) embedded full rule objects directly under the cue
    #   2. rule_ids: a flat list of pool rule ids, target baked into the rule
    #   3. (current) rule_assignments: {rule_id, applies_to} pairs, so the
    #      same reusable rule can target different fixtures/groups per cue
    # Both older shapes are migrated transparently here so nothing is lost.
    cues = []
    for c in raw.get("cues", []) or []:
        if "rule_assignments" in c:
            cues.append(build_cue(c))
            continue

        if "rule_ids" in c:
            assignments = []
            for rid in c.get("rule_ids", []) or []:
                matching = next((r for r in rules if r.id == rid), None)
                applies_to = matching.applies_to if matching else "*"
                assignments.append(CueRuleAssignment(rule_id=rid, applies_to=applies_to))
            cues.append(Cue(name=c["name"], description=c.get("description", ""),
                             rule_assignments=assignments))
            continue

        assignments = []
        for embedded_rule in c.get("rules", []) or []:
            rule = build_rule(embedded_rule)
            rule.always_active = False
            rules.append(rule)
            assignments.append(CueRuleAssignment(rule_id=rule.id, applies_to=rule.applies_to))
        cues.append(Cue(name=c["name"], description=c.get("description", ""),
                         rule_assignments=assignments))

    return Config(
        listen_ip=raw["listen_ip"],
        universes=raw["universes"],
        fixtures=fixtures,
        rules=rules,
        cues=cues,
        groups=groups,
    )

