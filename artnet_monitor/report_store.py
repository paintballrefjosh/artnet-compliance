"""
Saved compliance reports: one timestamped JSON file per report under
`config/reports/`.

A report is evidence -- "every cue was checked at 18:42 before doors, here's
what passed" -- so unlike the live snapshot it's written down and kept rather
than recomputed. Files are plain JSON so they survive this app entirely; the
backup export picks them up alongside everything else under config/.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .paths import atomic_write_text, resolve_within, safe_filename

log = logging.getLogger("report_store")

# Reports are small (a few KB) but generated freely; cap the kept history so
# a long-running install doesn't accumulate them without bound.
MAX_REPORTS = 200


class ReportStore:
    def __init__(self, config_dir: str):
        self.reports_dir = Path(config_dir) / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, report_id: str) -> Path:
        path = resolve_within(self.reports_dir, f"{safe_filename(report_id)}.json")
        if path is None:
            raise ValueError(f"Unsafe report id: {report_id!r}")
        return path

    def save(self, report: dict, label: str = "") -> dict:
        """Persists a generated report, returning it with `id` and `label`
        attached. The id doubles as the filename and sorts chronologically."""
        stamp = datetime.fromtimestamp(report.get("generated_at", time.time()))
        report_id = stamp.strftime("%Y%m%d-%H%M%S")
        path = self._path_for(report_id)
        # Same second twice: disambiguate rather than silently overwrite.
        suffix = 2
        while path.exists():
            report_id = f"{stamp.strftime('%Y%m%d-%H%M%S')}-{suffix}"
            path = self._path_for(report_id)
            suffix += 1

        stored = {**report, "id": report_id, "label": label}
        atomic_write_text(str(path), json.dumps(stored, indent=2))
        self._prune()
        return stored

    def list(self) -> list:
        """Newest first. Summary only -- id, timestamp, label, and per-status
        counts -- so the history list doesn't load every full report."""
        summaries = []
        for path in sorted(self.reports_dir.glob("*.json"), reverse=True):
            try:
                with open(path, encoding="utf-8") as fh:
                    report = json.load(fh)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Skipping unreadable report %s: %s", path.name, e)
                continue
            cues = report.get("cues", [])
            summaries.append({
                "id": report.get("id", path.stem),
                "generated_at": report.get("generated_at"),
                "label": report.get("label", ""),
                "active_cue": report.get("active_cue"),
                "counts": {
                    status: sum(1 for c in cues if c.get("status") == status)
                    for status in ("compliant", "violation", "unconfigured")
                },
            })
        return summaries

    def get(self, report_id: str) -> dict | None:
        path = self._path_for(report_id)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def delete(self, report_id: str) -> bool:
        path = self._path_for(report_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _prune(self):
        reports = sorted(self.reports_dir.glob("*.json"), reverse=True)
        for stale in reports[MAX_REPORTS:]:
            try:
                stale.unlink()
            except OSError:
                pass
