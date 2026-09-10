"""Compliance evaluation: the live snapshot and the per-cue report."""
from conftest import frame


def test_snapshot_flags_violations_for_the_active_cue_only(engine):
    engine.ingest_frame(1, frame({1: 100, 2: 50}))  # Par 1 red=50 breaks rule-look

    par1 = next(f for f in engine.snapshot()["fixtures"] if f["name"] == "Par 1")
    assert par1["compliant"] is True, "cue rules shouldn't apply with no cue active"

    engine.set_active_cue("Blue Look")
    par1 = next(f for f in engine.snapshot()["fixtures"] if f["name"] == "Par 1")
    assert [v["channel"] for v in par1["violations"]] == ["red"]


def test_always_active_rules_apply_with_no_cue_selected(engine):
    engine.ingest_frame(1, frame({1: 255}))  # over the always-active cap of 200
    par1 = next(f for f in engine.snapshot()["fixtures"] if f["name"] == "Par 1")
    assert [v["channel"] for v in par1["violations"]] == ["intensity"]


def test_report_judges_every_cue_not_just_the_active_one(engine):
    # Both Pars are in the targeted group, so Par 2 has to pass too for
    # `red` to be the only failing channel.
    engine.ingest_frame(1, frame({1: 100, 2: 50, 4: 100, 5: 0}))
    report = engine.generate_compliance_report()
    by_name = {c["name"]: c for c in report["cues"]}

    # Blue Look isn't active, but the report still evaluates it.
    assert by_name["Blue Look"]["status"] == "violation"
    assert by_name["Blue Look"]["rule_reports"][0]["channels"] == ["red"]


def test_report_status_is_compliant_when_everything_passes(engine):
    engine.ingest_frame(1, frame({1: 100, 2: 0, 4: 100, 5: 0}))
    by_name = {c["name"]: c for c in engine.generate_compliance_report()["cues"]}
    assert by_name["Blue Look"]["status"] == "compliant"


def test_always_active_violation_surfaces_on_a_cue_with_no_rules(engine):
    """A cue with no assignments still inherits always-active rules, so it
    must not be reported as neutral/unconfigured while something is failing."""
    engine.ingest_frame(1, frame({1: 255}))
    by_name = {c["name"]: c for c in engine.generate_compliance_report()["cues"]}
    assert by_name["Empty Cue"]["status"] == "violation"


def test_unconfigured_only_when_nothing_applies_at_all(engine):
    engine.config.rules = []  # drop the always-active cap too
    by_name = {c["name"]: c for c in engine.generate_compliance_report()["cues"]}
    assert by_name["Empty Cue"]["status"] == "unconfigured"


def test_violation_channels_follow_rule_entry_order(engine):
    """Not fixture-discovery order, which would vary run to run."""
    # Par 2 breaks red first (by fixture order), Par 1 breaks intensity.
    engine.ingest_frame(1, frame({1: 5, 2: 0, 4: 100, 5: 90}))
    report = engine.generate_compliance_report()
    look = next(c for c in report["cues"] if c["name"] == "Blue Look")
    rule = next(r for r in look["rule_reports"] if r["rule_name"] == "House Blue 65%")
    assert rule["channels"] == ["intensity", "red"]  # entry order, not discovery order


def test_report_names_the_offending_fixture(engine):
    engine.ingest_frame(1, frame({1: 100, 2: 0, 4: 100, 5: 90}))  # only Par 2's red is off
    look = next(c for c in engine.generate_compliance_report()["cues"] if c["name"] == "Blue Look")
    violations = look["rule_reports"][0]["violations"]
    assert [v["fixture"] for v in violations] == ["Par 2"]


def test_owners_fingerprint_tracks_the_patch(engine):
    first = engine.owners_fingerprint()
    assert first == engine.owners_fingerprint()
    engine.config.fixtures[0].universe = 2
    assert engine.owners_fingerprint() != first


def test_snapshot_can_omit_owners(engine):
    with_owners = engine.snapshot(include_owners=True)
    without = engine.snapshot(include_owners=False)
    assert "owners" in with_owners["universes"][1]
    assert "owners" not in without["universes"][1]
    assert without["owners_version"] == with_owners["owners_version"]
