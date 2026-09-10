"""patch_writer's surgical YAML edits, atomic writes, and the reference
retargeting that rename depends on."""
import threading

import yaml

from artnet_monitor import patch_writer
from artnet_monitor.config import load_config


def _raw(config_path):
    return yaml.safe_load(open(config_path, encoding="utf-8"))


def test_header_comments_survive_every_writer(config_path):
    patch_writer.add_group(config_path, {"name": "New", "fixtures": []})
    patch_writer.add_rule(config_path, {"id": "r1", "name": "x", "entries": [{"channel": "c"}]})
    patch_writer.add_cue(config_path, {"name": "C", "description": "", "rule_assignments": []})
    text = open(config_path, encoding="utf-8").read()
    assert text.startswith("# header comment that must survive every write")


def test_editing_one_rule_leaves_legacy_siblings_intact(tmp_path):
    """update_rule re-serialises *every* rule; a legacy-shaped sibling must
    not be turned into a phantom zero-entry package (this regressed once)."""
    path = tmp_path / "patch.yaml"
    path.write_text(
        'listen_ip: "1.2.3.4"\nuniverses: [1]\n'
        "rules:\n"
        "- id: rule-legacy\n  channel: dimmer\n  max: 166\n  always_active: true\n"
        "- id: rule-new\n  name: New\n  always_active: true\n  entries:\n  - channel: red\n    max: 10\n",
        encoding="utf-8",
    )
    patch_writer.update_rule(str(path), "rule-new",
                             {"name": "Renamed", "entries": [{"channel": "red", "max": 20}]})

    legacy = load_config(str(path)).get_rule("rule-legacy")
    assert [(e.channel, e.max) for e in legacy.entries] == [("dimmer", 166)]


def test_updating_a_legacy_rule_drops_its_stale_flat_keys(tmp_path):
    path = tmp_path / "patch.yaml"
    path.write_text(
        'listen_ip: "1.2.3.4"\nuniverses: [1]\n'
        "rules:\n- id: rule-legacy\n  channel: dimmer\n  max: 166\n  min: 100\n  always_active: true\n",
        encoding="utf-8",
    )
    patch_writer.update_rule(str(path), "rule-legacy",
                             {"entries": [{"channel": "dimmer", "max": 200}]})
    stored = _raw(str(path))["rules"][0]
    assert "channel" not in stored and "max" not in stored and "min" not in stored
    assert stored["entries"] == [{"channel": "dimmer", "max": 200}]


def test_group_rename_retargets_rule_and_cue_references(config_path):
    patch_writer.update_group(config_path, "Pars", ["Par 1", "Par 2"], new_name="All Pars")
    counts = patch_writer.retarget_group_references(config_path, "Pars", "All Pars")

    config = load_config(config_path)
    assert counts["rules"] == 1
    assert config.get_rule("rule-look").applies_to == ["group:All Pars"]
    assert config.cues[0].rule_assignments[0].applies_to == ["group:All Pars"]


def test_fixture_rename_retargets_groups_rules_and_cues(config_path):
    counts = patch_writer.retarget_fixture_references(config_path, "Par 1", "Par One")
    config = load_config(config_path)
    assert counts["groups"] == 1
    assert config.groups[0].fixtures == ["Par One", "Par 2"]


def test_reorder_cues_persists_and_keeps_unnamed_cues(config_path):
    assert patch_writer.reorder_cues(config_path, ["Empty Cue", "Blue Look"]) is True
    assert [c.name for c in load_config(config_path).cues] == ["Empty Cue", "Blue Look"]

    # A stale ordering that omits a cue must not drop it.
    patch_writer.reorder_cues(config_path, ["Blue Look"])
    assert {c.name for c in load_config(config_path).cues} == {"Blue Look", "Empty Cue"}


def test_writes_are_serialized_across_threads(config_path):
    """Concurrent read-modify-write cycles must not lose updates."""
    def add(i):
        patch_writer.add_group(config_path, {"name": f"G{i}", "fixtures": []})

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    names = {g.name for g in load_config(config_path).groups}
    assert {f"G{i}" for i in range(12)} <= names


def test_write_leaves_no_temp_files_behind(config_path, tmp_path):
    patch_writer.add_group(config_path, {"name": "X", "fixtures": []})
    assert [p.name for p in tmp_path.glob(".artnet-*")] == []


def test_set_listen_ip_touches_only_that_line(config_path):
    before = open(config_path, encoding="utf-8").read().splitlines()
    assert patch_writer.set_listen_ip(config_path, "192.168.1.99") is True
    after = open(config_path, encoding="utf-8").read().splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(changed) == 1 and "192.168.1.99" in changed[0][1]
