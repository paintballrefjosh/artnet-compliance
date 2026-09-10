"""
Structured edits to top-level list sections (`fixtures:`, `rules:`, `cues:`,
`groups:`) in patch.yaml, without dragging in a round-trip YAML library.
Approach: locate the exact text span covering a given key (from its line
through everything indented under it, up to the next top-level key), parse
*just that span* with PyYAML, modify the Python list, then re-serialize only
that span back into the file. Everything before and after the span --
including header comments and any other section -- is left completely
untouched.

Trade-off: any hand-written comments *inside* an edited block itself (e.g.
the commented-out example entries in the default template) get replaced the
first time that block is edited via the API/dashboard. Comments elsewhere in
the file are unaffected.
"""
import re

import yaml

from .config import retarget_group_reference

# Preferred key order when re-serializing entries, purely cosmetic.
_FIXTURE_KEY_ORDER = ["name", "universe", "start_channel", "profile", "mode", "channels"]
_CUE_KEY_ORDER = ["name", "description", "rule_assignments"]
_RULE_KEY_ORDER = ["id", "name", "profile_id", "description", "applies_to", "always_active", "entries"]
_RULE_ENTRY_KEY_ORDER = ["channel", "max", "min"]
_ASSIGNMENT_KEY_ORDER = ["rule_id", "applies_to"]
_GROUP_KEY_ORDER = ["name", "fixtures"]


def _span_re(key: str):
    return re.compile(rf"^{re.escape(key)}:.*?(?=\n[A-Za-z_][\w.-]*:|\Z)",
                       re.DOTALL | re.MULTILINE)


def _ordered(d: dict, key_order: list) -> dict:
    ordered = {k: d[k] for k in key_order if k in d}
    # keep any keys we didn't anticipate rather than silently dropping them
    for k, v in d.items():
        if k not in ordered:
            ordered[k] = v
    return ordered


def _ordered_rule(rule_dict: dict) -> dict:
    """Like _ordered, but also orders each item in a nested `entries` list --
    for a rule already in the current package shape. A legacy rule dict (no
    `entries` key at all -- channel/max/min sit at the top level instead) is
    passed straight to _ordered untouched: injecting an empty `entries: []`
    here would make build_rule() treat it as an already-migrated zero-entry
    package on the next load, silently discarding its channel/max/min. That
    matters because update_rule() re-serializes *every* rule in the file
    whenever any one of them changes, so this has to be a no-op for the
    untouched ones sitting next to the one actually being edited."""
    if "entries" not in rule_dict:
        return _ordered(rule_dict, _RULE_KEY_ORDER)
    entries = [_ordered(e, _RULE_ENTRY_KEY_ORDER) for e in rule_dict["entries"]]
    return _ordered({**rule_dict, "entries": entries}, _RULE_KEY_ORDER)


def _read_list_block(text: str, key: str) -> list:
    match = _span_re(key).search(text)
    if not match:
        return []
    parsed = yaml.safe_load(match.group(0)) or {}
    return parsed.get(key) or []


def _render_list_block(key: str, items: list) -> str:
    if not items:
        return f"{key}: []"
    dumped = yaml.safe_dump({key: items}, default_flow_style=False, sort_keys=False)
    return dumped.rstrip("\n")


def _replace_list_block(text: str, key: str, new_block: str) -> str:
    match = _span_re(key).search(text)
    if not match:
        return text.rstrip("\n") + "\n\n" + new_block + "\n"
    # match.end() sits right before the boundary newline leading into whatever
    # follows (another top-level key, or end of file) -- add one more newline
    # so the original blank-line separation between sections is preserved.
    return text[:match.start()] + new_block + "\n" + text[match.end():]


# ── fixtures ─────────────────────────────────────────────────────────────

def add_fixture(config_path: str, fixture_dict: dict):
    add_fixtures(config_path, [fixture_dict])


def add_fixtures(config_path: str, fixture_dicts: list):
    """Adds several fixtures in a single read-modify-write cycle -- used for
    'quantity > 1' patches so N fixtures don't trigger N separate file writes."""
    if not fixture_dicts:
        return

    with open(config_path, "r") as fh:
        text = fh.read()

    fixtures = _read_list_block(text, "fixtures")
    fixtures.extend(_ordered(f, _FIXTURE_KEY_ORDER) for f in fixture_dicts)
    new_text = _replace_list_block(text, "fixtures", _render_list_block("fixtures", fixtures))

    with open(config_path, "w") as fh:
        fh.write(new_text)


def update_fixture(config_path: str, name: str, raw_fields: dict) -> bool:
    """Replaces the patched-fixture entry named `name` with `raw_fields` (a
    full patch.yaml-shaped fixture dict, including its `name` key) --
    a wholesale replace rather than a merge, so switching a fixture between
    a library profile and an inline `channels` map doesn't leave a stale key
    from the shape it used before. Returns True if found."""
    with open(config_path, "r") as fh:
        text = fh.read()

    fixtures = _read_list_block(text, "fixtures")
    found = False
    for i, f in enumerate(fixtures):
        if f.get("name") == name:
            fixtures[i] = _ordered(raw_fields, _FIXTURE_KEY_ORDER)
            found = True
            break
    if not found:
        return False

    new_text = _replace_list_block(text, "fixtures", _render_list_block("fixtures", fixtures))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return True


def remove_fixture(config_path: str, name: str) -> bool:
    """Returns True if a fixture with this name was found and removed."""
    with open(config_path, "r") as fh:
        text = fh.read()

    fixtures = _read_list_block(text, "fixtures")
    remaining = [f for f in fixtures if f.get("name") != name]
    removed = len(remaining) != len(fixtures)

    new_text = _replace_list_block(text, "fixtures", _render_list_block("fixtures", remaining))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return removed


# ── rules (shared pool) ──────────────────────────────────────────────────

def add_rule(config_path: str, rule_dict: dict):
    with open(config_path, "r") as fh:
        text = fh.read()

    rules = _read_list_block(text, "rules")
    rules.append(_ordered_rule(rule_dict))
    new_text = _replace_list_block(text, "rules", _render_list_block("rules", rules))

    with open(config_path, "w") as fh:
        fh.write(new_text)


def update_rule(config_path: str, rule_id: str, updated_fields: dict) -> bool:
    """Merges updated_fields into the existing rule dict with this id.
    Returns True if found.

    updated_fields always carries the current (package) shape, including an
    `entries` list. A rule saved back before this shape existed has legacy
    top-level `channel`/`max`/`min` keys instead -- those are dropped here so
    the rewrite lands cleanly on the new shape instead of leaving stale
    fields alongside the new `entries` list (dict.update() alone wouldn't
    remove keys the new shape no longer has)."""
    with open(config_path, "r") as fh:
        text = fh.read()

    rules = _read_list_block(text, "rules")
    found = False
    for r in rules:
        if r.get("id") == rule_id:
            if "entries" in updated_fields:
                r.pop("channel", None)
                r.pop("max", None)
                r.pop("min", None)
            r.update(updated_fields)
            found = True
            break
    if not found:
        return False

    ordered = [_ordered_rule(r) for r in rules]
    new_text = _replace_list_block(text, "rules", _render_list_block("rules", ordered))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return True


def remove_rule(config_path: str, rule_id: str) -> bool:
    """Returns True if a rule with this id was found and removed. Does not
    touch cues -- see remove_rule_id_from_all_cues for reference cleanup."""
    with open(config_path, "r") as fh:
        text = fh.read()

    rules = _read_list_block(text, "rules")
    remaining = [r for r in rules if r.get("id") != rule_id]
    removed = len(remaining) != len(rules)

    new_text = _replace_list_block(text, "rules", _render_list_block("rules", remaining))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return removed


# ── cues ─────────────────────────────────────────────────────────────────

def add_cue(config_path: str, cue_dict: dict):
    with open(config_path, "r") as fh:
        text = fh.read()

    cues = _read_list_block(text, "cues")
    cues.append(_ordered(cue_dict, _CUE_KEY_ORDER))
    new_text = _replace_list_block(text, "cues", _render_list_block("cues", cues))

    with open(config_path, "w") as fh:
        fh.write(new_text)


def remove_cue(config_path: str, name: str) -> bool:
    """Returns True if a cue with this name was found and removed."""
    with open(config_path, "r") as fh:
        text = fh.read()

    cues = _read_list_block(text, "cues")
    remaining = [c for c in cues if c.get("name") != name]
    removed = len(remaining) != len(cues)

    new_text = _replace_list_block(text, "cues", _render_list_block("cues", remaining))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return removed


def update_cue(config_path: str, cue_name: str, description: str, rule_assignments: list,
                new_name: str = None) -> bool:
    """Replaces a cue's description and rule_assignments together, and
    renames it if `new_name` is given. `rule_assignments` is a list of
    {rule_id, applies_to} dicts. Returns True if found.

    Nothing outside the `cues:` block references a cue by name -- rules
    don't point at cues, and the active cue is runtime-only, never
    persisted -- so a rename here is self-contained and needs no
    cross-reference cleanup the way a group or fixture rename would."""
    with open(config_path, "r") as fh:
        text = fh.read()

    cues = _read_list_block(text, "cues")
    found = False
    for cue in cues:
        if cue.get("name") == cue_name:
            cue["description"] = description
            cue["rule_assignments"] = [_ordered(a, _ASSIGNMENT_KEY_ORDER) for a in rule_assignments]
            if new_name is not None:
                cue["name"] = new_name
            found = True
            break
    if not found:
        return False

    ordered = [_ordered(c, _CUE_KEY_ORDER) for c in cues]
    new_text = _replace_list_block(text, "cues", _render_list_block("cues", ordered))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return True


def remove_rule_id_from_all_cues(config_path: str, rule_id: str):
    """Strips any assignment referencing this rule id from every cue --
    used when that rule is deleted from the pool, so no cue is left
    pointing at a rule that no longer exists."""
    with open(config_path, "r") as fh:
        text = fh.read()

    cues = _read_list_block(text, "cues")
    changed = False
    for cue in cues:
        assignments = cue.get("rule_assignments") or []
        remaining = [a for a in assignments if a.get("rule_id") != rule_id]
        if len(remaining) != len(assignments):
            cue["rule_assignments"] = remaining
            changed = True
    if not changed:
        return

    ordered = [_ordered(c, _CUE_KEY_ORDER) for c in cues]
    new_text = _replace_list_block(text, "cues", _render_list_block("cues", ordered))
    with open(config_path, "w") as fh:
        fh.write(new_text)


# ── groups ───────────────────────────────────────────────────────────────

def add_group(config_path: str, group_dict: dict):
    with open(config_path, "r") as fh:
        text = fh.read()

    groups = _read_list_block(text, "groups")
    groups.append(_ordered(group_dict, _GROUP_KEY_ORDER))
    new_text = _replace_list_block(text, "groups", _render_list_block("groups", groups))

    with open(config_path, "w") as fh:
        fh.write(new_text)


def remove_group(config_path: str, name: str) -> bool:
    """Returns True if a group with this name was found and removed."""
    with open(config_path, "r") as fh:
        text = fh.read()

    groups = _read_list_block(text, "groups")
    remaining = [g for g in groups if g.get("name") != name]
    removed = len(remaining) != len(groups)

    new_text = _replace_list_block(text, "groups", _render_list_block("groups", remaining))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return removed


def update_group(config_path: str, name: str, fixtures: list, new_name: str = None) -> bool:
    """
    Replaces a group's fixture membership and, when `new_name` is given,
    renames it -- both in a single read-modify-write so a rename can't land
    without its membership (or vice versa). Returns True if found.

    Renaming only rewrites the `groups:` block; callers must also call
    retarget_group_references() so rules and cues pointing at the old
    "group:<name>" token follow it.
    """
    with open(config_path, "r") as fh:
        text = fh.read()

    groups = _read_list_block(text, "groups")
    found = False
    for group in groups:
        if group.get("name") == name:
            group["fixtures"] = fixtures
            if new_name is not None:
                group["name"] = new_name
            found = True
            break
    if not found:
        return False

    ordered = [_ordered(g, _GROUP_KEY_ORDER) for g in groups]
    new_text = _replace_list_block(text, "groups", _render_list_block("groups", ordered))
    with open(config_path, "w") as fh:
        fh.write(new_text)

    return True


# ── listen_ip / full-config backup & restore ────────────────────────────

def set_listen_ip(config_path: str, ip: str) -> bool:
    """Rewrites just the `listen_ip:` line in place, leaving the rest of the
    file (comments, fixtures, rules, ...) untouched -- the same targeted
    edit manager.py's NodeManager used to do inline. Returns True if the
    line was found and updated."""
    with open(config_path, "r") as fh:
        text = fh.read()

    new_line = f'listen_ip: "{ip}"'
    updated, count = re.subn(r'^listen_ip:.*$', new_line, text, count=1, flags=re.MULTILINE)
    if count == 0:
        return False

    with open(config_path, "w") as fh:
        fh.write(updated)
    return True


def restore_full_config(config_path: str, new_text: str, keep_listen_ip: str) -> None:
    """
    Wholesale-replaces patch.yaml with `new_text` -- used to restore an
    imported backup, where "replace everything" is the actually-intended
    behavior rather than the surgical per-block edits every other function
    in this module does. `keep_listen_ip` is written back over whatever
    listen_ip the backup contained: a backup restored onto a *different*
    machine should never make it advertise the *original* machine's
    address, so the destination machine's own address always wins here.
    """
    with open(config_path, "w") as fh:
        fh.write(new_text)
    set_listen_ip(config_path, keep_listen_ip)


def update_group_fixtures(config_path: str, name: str, fixtures: list) -> bool:
    """Back-compat shim: membership-only update."""
    return update_group(config_path, name, fixtures)


def retarget_group_references(config_path: str, old_name: str, new_name: str) -> dict:
    """
    Points every "group:<old_name>" reference at <new_name> instead, across
    both the shared rule pool's `applies_to` and every cue assignment's
    `applies_to`. Returns {"rules": n, "cue_assignments": n} counts.

    Rules and cues are separate top-level blocks, so this is two writes; the
    rules block is only rewritten if something in it actually changed, and
    likewise for cues.
    """
    counts = {"rules": 0, "cue_assignments": 0}

    with open(config_path, "r") as fh:
        text = fh.read()

    rules = _read_list_block(text, "rules")
    for rule in rules:
        rewritten, changed = retarget_group_reference(
            rule.get("applies_to", "*"), old_name, new_name
        )
        if changed:
            rule["applies_to"] = rewritten
            counts["rules"] += 1
    if counts["rules"]:
        ordered = [_ordered(r, _RULE_KEY_ORDER) for r in rules]
        text = _replace_list_block(text, "rules", _render_list_block("rules", ordered))

    cues = _read_list_block(text, "cues")
    cues_changed = False
    for cue in cues:
        for assignment in cue.get("rule_assignments") or []:
            rewritten, changed = retarget_group_reference(
                assignment.get("applies_to", "*"), old_name, new_name
            )
            if changed:
                assignment["applies_to"] = rewritten
                counts["cue_assignments"] += 1
                cues_changed = True
    if cues_changed:
        ordered = [_ordered(c, _CUE_KEY_ORDER) for c in cues]
        text = _replace_list_block(text, "cues", _render_list_block("cues", ordered))

    if counts["rules"] or cues_changed:
        with open(config_path, "w") as fh:
            fh.write(text)

    return counts
