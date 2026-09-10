"""Endpoint behaviour: collision checks, renames, backup round-trip, reports."""
import asyncio
import io
import json
import zipfile

from artnet_monitor.config import load_config
from artnet_monitor.web.app import (
    CueReorderRequest, CueUpdateRequest, PatchFixtureRequest,
    PatchFixtureUpdateRequest, SaveReportRequest,
)


def _body(response):
    return json.loads(response.body)


def test_overlapping_patch_is_rejected(routes):
    add = routes[("/api/patch/fixtures", "POST")]
    result = add(PatchFixtureRequest(name="Clash", universe=1, start_channel=2,
                                     channels={"intensity": 2}))
    assert "overlap" in _body(result)["error"]


def test_non_overlapping_patch_succeeds(routes):
    add = routes[("/api/patch/fixtures", "POST")]
    assert add(PatchFixtureRequest(name="Clean", universe=2, start_channel=1,
                                   channels={"intensity": 1}))["success"] is True


def test_conflicts_endpoint_reports_existing_overlaps(routes, engine):
    assert routes[("/api/patch/conflicts", "GET")]()["conflicts"] == []
    # Force an overlap the way a hand-edited config could contain one.
    engine.config.fixtures[1].channels = {"intensity": 1}
    conflicts = routes[("/api/patch/conflicts", "GET")]()["conflicts"]
    assert conflicts[0]["channel"] == 1
    assert set(conflicts[0]["fixtures"]) == {"Par 1", "Par 2"}


def test_fixture_rename_retargets_references(routes, config_path):
    update = routes[("/api/patch/fixtures/{name}", "PUT")]
    result = update("Par 1", PatchFixtureUpdateRequest(
        universe=1, start_channel=1, channels={"intensity": 1, "red": 2, "green": 3},
        name="Par One"))

    assert result["renamed"] is True
    assert result["retargeted"]["groups"] == 1
    assert load_config(config_path).groups[0].fixtures == ["Par One", "Par 2"]


def test_fixture_rename_to_an_existing_name_is_rejected(routes):
    update = routes[("/api/patch/fixtures/{name}", "PUT")]
    result = update("Par 1", PatchFixtureUpdateRequest(
        universe=1, start_channel=1, channels={"intensity": 1}, name="Par 2"))
    assert "already patched" in _body(result)["error"]


def test_readdressing_onto_itself_is_allowed(routes):
    update = routes[("/api/patch/fixtures/{name}", "PUT")]
    assert update("Par 1", PatchFixtureUpdateRequest(
        universe=1, start_channel=1,
        channels={"intensity": 1, "red": 2, "green": 3}))["success"] is True


def test_cue_rename_moves_the_active_cue_with_it(routes, engine):
    engine.set_active_cue("Blue Look")
    result = routes[("/api/cues/{name}", "PUT")]("Blue Look", CueUpdateRequest(
        name="Blue Look v2", description="", rule_assignments=[]))
    assert result["renamed"] is True
    assert engine.active_cue_name == "Blue Look v2"


def test_cue_reorder_persists(routes, config_path):
    result = routes[("/api/cues/reorder", "POST")](CueReorderRequest(names=["Empty Cue", "Blue Look"]))
    assert result["order"] == ["Empty Cue", "Blue Look"]
    assert [c.name for c in load_config(config_path).cues] == ["Empty Cue", "Blue Look"]


def test_cue_reorder_rejects_unknown_names(routes):
    result = routes[("/api/cues/reorder", "POST")](CueReorderRequest(names=["Ghost"]))
    assert "Unknown cue" in _body(result)["error"]


def test_reports_are_saved_listed_and_deletable(routes):
    saved = routes[("/api/reports", "POST")](SaveReportRequest(label="Pre-show"))
    assert saved["label"] == "Pre-show"

    listing = routes[("/api/reports", "GET")]()["reports"]
    assert listing[0]["id"] == saved["id"]
    assert set(listing[0]["counts"]) == {"compliant", "violation", "unconfigured"}

    assert routes[("/api/reports/{report_id}", "GET")](saved["id"])["id"] == saved["id"]
    assert routes[("/api/reports/{report_id}", "DELETE")](saved["id"])["success"] is True
    assert routes[("/api/reports", "GET")]()["reports"] == []


def test_unknown_report_is_404(routes):
    assert routes[("/api/reports/{report_id}", "GET")]("nope").status_code == 404


def test_backup_round_trip_preserves_the_destination_listen_ip(routes, engine, config_path):
    exported = routes[("/api/backup/export", "GET")]().body
    assert "patch.yaml" in zipfile.ZipFile(io.BytesIO(exported)).namelist()

    # Pretend this machine has its own address, then restore the backup.
    engine.config.listen_ip = "192.168.50.7"
    from artnet_monitor import patch_writer
    patch_writer.set_listen_ip(config_path, "192.168.50.7")

    class _Upload:
        async def read(self):
            return exported

    result = asyncio.run(routes[("/api/backup/import", "POST")](_Upload()))
    assert result["success"] is True
    assert load_config(config_path).listen_ip == "192.168.50.7", \
        "import must never restore the source machine's address"


def test_backup_import_rejects_a_non_backup_zip(routes):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("something.txt", "not a backup")

    class _Upload:
        async def read(self):
            return buf.getvalue()

    result = asyncio.run(routes[("/api/backup/import", "POST")](_Upload()))
    assert result.status_code == 400


def test_backup_import_rejects_a_non_zip(routes):
    class _Upload:
        async def read(self):
            return b"definitely not a zip"

    result = asyncio.run(routes[("/api/backup/import", "POST")](_Upload()))
    assert result.status_code == 400
