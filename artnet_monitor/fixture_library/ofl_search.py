"""
Lightweight local search index over Open Fixture Library, so the dashboard
can offer a manufacturer+fixture search box instead of requiring exact slugs.

OFL's own search index (fixtures/register.json) is generated at build time
and gitignored — it isn't available via raw.githubusercontent.com. Instead,
this builds an index from two things that ARE always fetchable:
  - The repository's full file tree (one GitHub API call), which gives every
    manufacturer-key/fixture-key pair from the fixture JSON file paths.
  - fixtures/manufacturers.json, which maps manufacturer keys to their real
    display names.

Fixture *display names* aren't in either of those — getting the true product
name requires fetching the individual fixture JSON. Rather than fetch
thousands of files just to build a search index, this derives a readable
title from the fixture's slug (e.g. "slimpar-pro-h" -> "Slimpar Pro H"),
which is a close approximation since OFL slugs are generated from the real
name. The exact name is confirmed once a fixture is actually imported.

The index is cached to disk and only rebuilt when missing or stale, so
normal use — searching, selecting, importing — costs at most one GitHub
request per search session, not one per keystroke.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("ofl_search")

TREE_URL = ("https://api.github.com/repos/OpenLightingProject/"
            "open-fixture-library/git/trees/master?recursive=1")
MANUFACTURERS_URL = ("https://raw.githubusercontent.com/OpenLightingProject/"
                      "open-fixture-library/master/fixtures/manufacturers.json")

CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # rebuild weekly — OFL adds fixtures often enough


def _slug_to_title(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "artnet-compliance-monitor"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_index() -> list:
    tree = _fetch_json(TREE_URL)
    manufacturers = _fetch_json(MANUFACTURERS_URL)

    entries = []
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if not path.startswith("fixtures/") or not path.endswith(".json"):
            continue
        parts = path[len("fixtures/"):].rsplit(".json", 1)[0].split("/")
        if len(parts) != 2:
            continue  # skip manufacturers.json itself and any top-level files
        manufacturer_key, fixture_key = parts
        manufacturer_name = manufacturers.get(manufacturer_key, {}).get(
            "name", _slug_to_title(manufacturer_key)
        )
        entries.append({
            "manufacturer_key": manufacturer_key,
            "manufacturer_name": manufacturer_name,
            "fixture_key": fixture_key,
            "display_name": _slug_to_title(fixture_key),
        })
    return entries


def get_index(cache_path: Path, force_refresh: bool = False) -> list:
    if not force_refresh and cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_MAX_AGE_SECONDS:
            try:
                with open(cache_path) as fh:
                    return json.load(fh)["entries"]
            except (json.JSONDecodeError, KeyError):
                pass  # fall through and rebuild

    try:
        entries = _build_index()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        if cache_path.exists():
            log.warning("Couldn't refresh OFL index (%s); using stale cache.", e)
            with open(cache_path) as fh:
                return json.load(fh)["entries"]
        raise ValueError(f"Couldn't reach Open Fixture Library to build the "
                          f"search index: {e}") from e

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as fh:
        json.dump({"built_at": time.time(), "entries": entries}, fh)
    return entries


def search(entries: list, query: str, limit: int = 40) -> list:
    q = query.strip().lower()
    if not q:
        return []
    words = q.split()

    def matches(entry) -> bool:
        haystack = f"{entry['manufacturer_name']} {entry['manufacturer_key']} " \
                   f"{entry['display_name']} {entry['fixture_key']}".lower()
        return all(word in haystack for word in words)

    def score(entry) -> tuple:
        # Prefer matches where the query appears at the start of the
        # manufacturer or fixture name, then fall back to match position.
        haystack = f"{entry['manufacturer_name']} {entry['display_name']}".lower()
        starts_with = 0 if haystack.startswith(q) else 1
        pos = haystack.find(q) if q in haystack else 999
        return (starts_with, pos)

    results = [e for e in entries if matches(e)]
    results.sort(key=score)
    return results[:limit]
