"""Path-traversal and archive-entry safety.

Windows is the sharp edge here: pathlib treats "\\" as a separator too, so
sanitising only "/" (as the original code did) still let entries walk out.
"""
import io
import json
import zipfile

import pytest

from artnet_monitor.paths import resolve_within, safe_filename
from artnet_monitor.report_store import ReportStore


@pytest.mark.parametrize("hostile", [
    "../../evil",
    "..\\..\\evil",
    "/etc/passwd",
    "C:\\Windows\\evil",
    "..",
    ".",
    "",
])
def test_safe_filename_never_escapes(tmp_path, hostile):
    resolved = resolve_within(tmp_path, safe_filename(hostile))
    assert resolved is not None
    assert tmp_path.resolve() in resolved.parents


def test_safe_filename_preserves_existing_profile_names():
    """Profiles already on disk use characters like "+" -- rewriting the
    scheme would orphan their files."""
    assert safe_filename("elation-six+-bar-l") == "elation-six+-bar-l"
    assert safe_filename("american-dj_mega-bar-50rgb") == "american-dj_mega-bar-50rgb"


@pytest.mark.parametrize("hostile", ["../../evil", "..\\..\\evil", "/abs/path"])
def test_resolve_within_rejects_raw_traversal(tmp_path, hostile):
    assert resolve_within(tmp_path, hostile) is None


def test_profile_path_stays_inside_the_library(tmp_path):
    from artnet_monitor.fixture_library.store import FixtureLibraryStore
    store = FixtureLibraryStore(str(tmp_path))
    path = store._profile_path(store.custom_dir, "custom:..\\..\\..\\evil")
    assert store.custom_dir.resolve() in path.resolve().parents


def test_report_ids_cannot_traverse(tmp_path):
    store = ReportStore(str(tmp_path))
    assert store.get("../../../../evil") is None
    assert store.delete("..\\..\\evil") is False
    path = store._path_for("../../evil")
    assert store.reports_dir.resolve() in path.resolve().parents


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_backup_import_rejects_zip_slip(routes, tmp_path, config_path):
    """A backup entry naming "..\\..\\evil.json" must not write outside the
    profiles directory -- splitting on "/" alone let this through."""
    import asyncio

    manifest = json.dumps({"app": "artnet-compliance-monitor", "format_version": 1})
    patch_yaml = open(config_path, encoding="utf-8").read()
    payload = _zip_bytes({
        "manifest.json": manifest,
        "patch.yaml": patch_yaml,
        "fixture_profiles/custom/..\\..\\..\\evil.json": '{"pwned": true}',
        "fixture_profiles/custom/../../../evil2.json": '{"pwned": true}',
    })

    class _Upload:
        async def read(self):
            return payload

    result = asyncio.run(routes[("/api/backup/import", "POST")](_Upload()))
    assert result["success"] is True  # the good parts still import

    escaped = list(tmp_path.parent.glob("evil*.json")) + list(tmp_path.glob("evil*.json"))
    assert escaped == [], f"zip entry escaped the profiles directory: {escaped}"
