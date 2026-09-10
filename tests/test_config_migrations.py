"""The schema migrations in load_config/build_rule. These run against real
user data on every startup, and a regression here silently loses rules or
cue assignments rather than erroring -- which is exactly why they're tested.
"""
import textwrap

import pytest

from artnet_monitor.config import load_config, parse_config

import yaml


def _write(tmp_path, body):
    path = tmp_path / "patch.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def test_legacy_flat_rule_becomes_single_entry_package(tmp_path):
    """Rules predate being packages: channel/max/min sat on the rule itself."""
    path = _write(tmp_path, """\
        listen_ip: "1.2.3.4"
        universes: [1]
        rules:
        - id: rule-old
          channel: dimmer
          max: 166
          min: 100
          always_active: true
        """)
    rule = load_config(path).rules[0]
    assert [(e.channel, e.min, e.max) for e in rule.entries] == [("dimmer", 100, 166)]


def test_rule_without_always_active_key_defaults_to_true(tmp_path):
    """Pre-dates the flag, when every top-level rule always applied."""
    path = _write(tmp_path, """\
        listen_ip: "1.2.3.4"
        universes: [1]
        rules:
        - id: rule-old
          channel: dimmer
          max: 200
        """)
    assert load_config(path).rules[0].always_active is True


def test_cue_gen1_embedded_rules_are_migrated(tmp_path):
    """Oldest cue shape: full rule objects nested under the cue."""
    path = _write(tmp_path, """\
        listen_ip: "1.2.3.4"
        universes: [1]
        rules: []
        cues:
        - name: Old Cue
          rules:
          - id: rule-embedded
            channel: dimmer
            max: 10
            applies_to: ['Par 1']
        """)
    config = load_config(path)
    assert [a.rule_id for a in config.cues[0].rule_assignments] == ["rule-embedded"]
    # the embedded rule joins the shared pool, not always-active
    assert config.get_rule("rule-embedded").always_active is False


def test_cue_gen2_rule_ids_inherit_the_rules_own_target(tmp_path):
    """Middle shape: flat rule_ids, with the target baked into the rule."""
    path = _write(tmp_path, """\
        listen_ip: "1.2.3.4"
        universes: [1]
        rules:
        - id: rule-a
          channel: dimmer
          max: 10
          applies_to: ['Par 7']
          always_active: false
        cues:
        - name: Gen2 Cue
          rule_ids: [rule-a]
        """)
    assignment = load_config(path).cues[0].rule_assignments[0]
    assert (assignment.rule_id, assignment.applies_to) == ("rule-a", ["Par 7"])


def test_parse_config_matches_load_config(config_path):
    """The backup importer validates via parse_config on an in-memory dict;
    it has to stay equivalent to the on-disk path."""
    from_file = load_config(config_path)
    from_dict = parse_config(yaml.safe_load(open(config_path, encoding="utf-8")))
    assert len(from_dict.rules) == len(from_file.rules)
    assert len(from_dict.cues) == len(from_file.cues)
    assert [f.name for f in from_dict.fixtures] == [f.name for f in from_file.fixtures]


def test_missing_required_keys_raises(tmp_path):
    path = _write(tmp_path, "fixtures: []\n")
    with pytest.raises(KeyError):
        load_config(path)
