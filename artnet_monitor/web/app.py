import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import patch_writer
from ..config import (
    build_cue, build_cue_assignment, build_fixture, build_group, build_rule,
    build_rule_entry, parse_config, retarget_group_reference, sequential_addresses,
)
from ..fixture_library import ofl_search
from ..fixture_library.ofl_import import fetch_ofl_fixture
from ..fixture_library.schema import FixtureProfile
from ..netiface import list_ipv4_interfaces

BACKUP_APP_ID = "artnet-compliance-monitor"
BACKUP_FORMAT_VERSION = 1

STATIC_DIR = Path(__file__).parent / "static"


class InterfaceSelection(BaseModel):
    ip: str


class OflImportRequest(BaseModel):
    manufacturer_key: str
    fixture_key: str


class CustomProfileRequest(BaseModel):
    manufacturer: str
    model: str
    modes: dict  # mode name -> [role, role, ...] in DMX order
    profile_id: str | None = None  # set to edit an existing custom profile in place


class PatchFixtureRequest(BaseModel):
    name: str
    universe: int
    start_channel: int
    profile: str | None = None
    mode: str | None = None
    channels: dict | None = None  # inline alternative to profile+mode
    quantity: int = 1  # >1 requires profile+mode; sequentially named/addressed


class PatchFixtureUpdateRequest(BaseModel):
    """Re-addresses or re-profiles an already-patched fixture. The name
    can't be changed here -- renaming would need to retarget every group
    membership and rule/cue applies_to naming this fixture (the same
    reference-cleanup group renaming already does for "group:<name>"
    tokens), which isn't implemented for fixture names yet."""
    universe: int
    start_channel: int
    profile: str | None = None
    mode: str | None = None
    channels: dict | None = None


class RuleEntryRequest(BaseModel):
    channel: str
    max: int | None = None
    min: int | None = None


class RuleRequest(BaseModel):
    name: str = ""
    profile_id: str | None = None
    description: str = ""
    applies_to: object = "*"  # only meaningful when always_active=True
    always_active: bool = False
    entries: list[RuleEntryRequest] = []


class CueRuleAssignmentRequest(BaseModel):
    rule_id: str
    applies_to: object = "*"


class CueRequest(BaseModel):
    name: str
    description: str = ""
    rule_assignments: list[CueRuleAssignmentRequest] = []


class CueUpdateRequest(BaseModel):
    description: str = ""
    rule_assignments: list[CueRuleAssignmentRequest] = []
    name: str | None = None  # set to rename; omit to leave the name alone


def _validate_cue_name(name: str) -> str | None:
    """Returns an error message, or None if the name is usable."""
    if not name.strip():
        return "A cue needs a name."
    return None


class ActiveCueRequest(BaseModel):
    name: str | None = None


class GroupRequest(BaseModel):
    name: str
    fixtures: list[str] = []


class GroupUpdateRequest(BaseModel):
    fixtures: list[str]
    name: str | None = None  # set to rename; omit to leave the name alone


def _validate_group_name(name: str) -> str | None:
    """Returns an error message, or None if the name is usable. Commas are
    rejected because the Rules page's `applies_to` field is a comma-separated
    list -- a group whose name contains one could never be targeted."""
    if not name.strip():
        return "A fixture group needs a name."
    if "," in name:
        return "A fixture group name can't contain a comma."
    return None


def _validate_rule_entries(entries: list) -> str | None:
    """Returns an error message, or None if the entry list is usable."""
    if not entries:
        return "A rule needs at least one channel entry."
    if any(not e.channel.strip() for e in entries):
        return "Every channel entry needs a channel role."
    return None


def rule_to_dict(rule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "profile_id": rule.profile_id,
        "description": rule.description,
        "applies_to": rule.applies_to,
        "always_active": rule.always_active,
        "entries": [{"channel": e.channel, "max": e.max, "min": e.min} for e in rule.entries],
    }


def create_app(engine, manager, library, config_path: str) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico")
    def favicon():
        # Browsers probe this path directly regardless of the <link rel="icon">
        # tag in index.html; serve the same SVG so that request doesn't 404.
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/api/snapshot")
    def snapshot():
        return engine.snapshot()

    @app.get("/api/interfaces")
    def interfaces():
        return {
            "interfaces": list_ipv4_interfaces(),
            "current": manager.config.listen_ip,
        }

    @app.post("/api/interface")
    def set_interface(payload: InterfaceSelection):
        manager.set_listen_ip(payload.ip)
        return {"success": True, "listen_ip": payload.ip}

    @app.get("/api/fixture-library")
    def fixture_library_list():
        return {"profiles": [p.to_dict() for p in library.list_profiles()]}

    @app.get("/api/fixture-library/search-ofl")
    def search_ofl(q: str = ""):
        cache_path = Path(library.config_dir) / "fixture_profiles" / "ofl_index_cache.json"
        try:
            entries = ofl_search.get_index(cache_path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return {"results": ofl_search.search(entries, q)}

    @app.post("/api/fixture-library/import-ofl")
    def import_ofl(payload: OflImportRequest):
        profile_id = f"ofl:{payload.manufacturer_key}/{payload.fixture_key}"
        try:
            profile = library.get(profile_id)  # already imported previously — skip refetching
        except KeyError:
            try:
                profile = fetch_ofl_fixture(payload.manufacturer_key, payload.fixture_key)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            library.save_imported(profile)
        return profile.to_dict()

    @app.post("/api/fixture-library/custom")
    def create_or_update_custom_profile(payload: CustomProfileRequest):
        if payload.profile_id:
            try:
                existing = library.get(payload.profile_id)
            except KeyError:
                return JSONResponse(
                    {"error": f"No profile '{payload.profile_id}' to edit."}, status_code=404
                )
            if existing.source != "custom":
                return JSONResponse(
                    {"error": "Only custom profiles can be edited here — "
                              "bundled and OFL-imported profiles are read-only."},
                    status_code=400,
                )
            # Keep the same id (so this overwrites the same file rather than
            # creating an orphan), merge the submitted mode into whatever
            # modes already existed rather than discarding the others.
            merged_modes = dict(existing.modes)
            merged_modes.update(payload.modes)
            profile = FixtureProfile(
                id=existing.id, manufacturer=payload.manufacturer,
                model=payload.model, modes=merged_modes,
            )
        else:
            slug = f"{payload.manufacturer}-{payload.model}".lower().replace(" ", "-")
            profile = FixtureProfile(
                id=f"custom:{slug}", manufacturer=payload.manufacturer,
                model=payload.model, modes=payload.modes,
            )

        library.save_custom(profile)
        return profile.to_dict()

    @app.delete("/api/fixture-library/{profile_id:path}")
    def delete_custom_profile(profile_id: str):
        try:
            profile = library.get(profile_id)
        except KeyError:
            return JSONResponse({"error": f"No profile '{profile_id}'."}, status_code=404)
        if profile.source != "custom":
            return JSONResponse(
                {"error": "Only custom profiles can be deleted — "
                          "bundled and OFL-imported profiles are read-only."},
                status_code=400,
            )
        in_use = [f.name for f in engine.config.fixtures if f.profile_id == profile_id]
        if in_use:
            return JSONResponse(
                {"error": f"Still patched to {', '.join(in_use)} — remove those "
                          f"fixtures first."},
                status_code=400,
            )
        library.delete_custom(profile_id)
        return {"success": True}

    @app.post("/api/patch/fixtures")
    def patch_fixture(payload: PatchFixtureRequest):
        raw = {k: v for k, v in payload.model_dump().items()
               if v is not None and k != "quantity"}
        quantity = max(1, payload.quantity)

        if quantity > 1:
            if not raw.get("profile") or not raw.get("mode"):
                return JSONResponse(
                    {"error": "Adding more than one at a time requires a "
                              "library profile and mode (so the channel span "
                              "per fixture is known)."},
                    status_code=400,
                )
            try:
                profile = library.get(raw["profile"])
                channels_per_unit = len(profile.channel_roles(raw["mode"]))
            except KeyError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

            addresses = sequential_addresses(
                raw["universe"], raw["start_channel"], channels_per_unit, quantity
            )
            base_name = raw["name"]
            raw_entries = []
            for i, (universe, start_channel) in enumerate(addresses, start=1):
                fixture_name = f"{base_name} {i}"
                if any(f.name == fixture_name for f in engine.config.fixtures):
                    return JSONResponse(
                        {"error": f"A fixture named '{fixture_name}' is already "
                                  f"patched — remove it or pick a different base name."},
                        status_code=400,
                    )
                raw_entries.append({**raw, "name": fixture_name,
                                     "universe": universe, "start_channel": start_channel})
        else:
            if any(f.name == raw["name"] for f in engine.config.fixtures):
                return JSONResponse(
                    {"error": f"A fixture named '{raw['name']}' is already patched."},
                    status_code=400,
                )
            raw_entries = [raw]

        built = []
        for entry in raw_entries:
            try:
                built.append(build_fixture(entry, library))
            except (KeyError, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        engine.config.fixtures.extend(built)
        patch_writer.add_fixtures(config_path, raw_entries)
        return {"success": True, "added": [f.name for f in built]}

    @app.put("/api/patch/fixtures/{name}")
    def update_patch_fixture(name: str, payload: PatchFixtureUpdateRequest):
        existing = next((f for f in engine.config.fixtures if f.name == name), None)
        if existing is None:
            return JSONResponse({"error": f"No fixture named '{name}' is patched."}, status_code=404)

        raw = {k: v for k, v in payload.model_dump().items() if v is not None}
        raw["name"] = name
        try:
            updated = build_fixture(raw, library)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        engine.config.fixtures = [updated if f.name == name else f for f in engine.config.fixtures]
        patch_writer.update_fixture(config_path, name, raw)
        return {"success": True, "name": updated.name}

    @app.delete("/api/patch/fixtures/{name}")
    def unpatch_fixture(name: str):
        engine.config.fixtures = [f for f in engine.config.fixtures if f.name != name]
        removed = patch_writer.remove_fixture(config_path, name)
        return {"success": removed}

    @app.get("/api/rules")
    def get_rules():
        return {"rules": [rule_to_dict(r) for r in engine.config.rules]}

    @app.post("/api/rules")
    def create_rule(payload: RuleRequest):
        entry_error = _validate_rule_entries(payload.entries)
        if entry_error:
            return JSONResponse({"error": entry_error}, status_code=400)
        rule = build_rule(payload.model_dump())
        engine.config.rules.append(rule)
        # write the *built* rule (with its generated id), not the raw payload
        # dict -- RuleRequest has no id field of its own.
        patch_writer.add_rule(config_path, rule_to_dict(rule))
        return rule_to_dict(rule)

    @app.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, payload: RuleRequest):
        rule = engine.config.get_rule(rule_id)
        if rule is None:
            return JSONResponse({"error": f"No rule '{rule_id}'."}, status_code=404)
        entry_error = _validate_rule_entries(payload.entries)
        if entry_error:
            return JSONResponse({"error": entry_error}, status_code=400)

        updated_fields = payload.model_dump()
        rule.name = updated_fields["name"]
        rule.profile_id = updated_fields["profile_id"]
        rule.description = updated_fields["description"]
        rule.applies_to = updated_fields["applies_to"]
        rule.always_active = updated_fields["always_active"]
        rule.entries = [build_rule_entry(e) for e in updated_fields["entries"]]

        patch_writer.update_rule(config_path, rule_id, updated_fields)
        return rule_to_dict(rule)

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str):
        rule = engine.config.get_rule(rule_id)
        if rule is None:
            return JSONResponse({"error": f"No rule '{rule_id}'."}, status_code=404)

        affected_cues = [c.name for c in engine.config.cues
                         if any(a.rule_id == rule_id for a in c.rule_assignments)]
        engine.config.rules = [r for r in engine.config.rules if r.id != rule_id]
        for cue in engine.config.cues:
            cue.rule_assignments = [a for a in cue.rule_assignments if a.rule_id != rule_id]

        patch_writer.remove_rule(config_path, rule_id)
        if affected_cues:
            patch_writer.remove_rule_id_from_all_cues(config_path, rule_id)

        return {"success": True, "removed_from_cues": affected_cues}

    @app.get("/api/cues")
    def list_cues():
        return {
            "cues": [
                {"name": c.name, "description": c.description,
                 "rule_assignments": [{"rule_id": a.rule_id, "applies_to": a.applies_to}
                                       for a in c.rule_assignments]}
                for c in engine.config.cues
            ],
            "active_cue": engine.active_cue_name,
        }

    @app.post("/api/cues")
    def create_cue(payload: CueRequest):
        name_error = _validate_cue_name(payload.name)
        if name_error:
            return JSONResponse({"error": name_error}, status_code=400)
        if any(c.name == payload.name for c in engine.config.cues):
            return JSONResponse(
                {"error": f"A cue named '{payload.name}' already exists."},
                status_code=400,
            )
        unknown = [a.rule_id for a in payload.rule_assignments if engine.config.get_rule(a.rule_id) is None]
        if unknown:
            return JSONResponse({"error": f"Unknown rule id(s): {', '.join(unknown)}"}, status_code=400)

        raw = payload.model_dump()
        cue = build_cue(raw)
        engine.config.cues.append(cue)
        patch_writer.add_cue(config_path, raw)
        return {"success": True, "name": cue.name}

    @app.put("/api/cues/{name}")
    def update_cue(name: str, payload: CueUpdateRequest):
        cue = engine.config.get_cue(name)
        if cue is None:
            return JSONResponse({"error": f"No cue named '{name}'."}, status_code=404)
        unknown = [a.rule_id for a in payload.rule_assignments if engine.config.get_rule(a.rule_id) is None]
        if unknown:
            return JSONResponse({"error": f"Unknown rule id(s): {', '.join(unknown)}"}, status_code=400)

        new_name = payload.name.strip() if payload.name is not None else None
        renaming = new_name is not None and new_name != name
        if renaming:
            name_error = _validate_cue_name(new_name)
            if name_error:
                return JSONResponse({"error": name_error}, status_code=400)
            if any(c.name == new_name for c in engine.config.cues):
                return JSONResponse(
                    {"error": f"A cue named '{new_name}' already exists."}, status_code=400
                )

        cue.description = payload.description
        cue.rule_assignments = [build_cue_assignment(a.model_dump()) for a in payload.rule_assignments]
        patch_writer.update_cue(
            config_path, name, payload.description,
            [a.model_dump() for a in payload.rule_assignments],
            new_name=new_name if renaming else None,
        )

        if renaming:
            cue.name = new_name
            # active_cue_name is the only thing outside the cues list itself
            # that references a cue by name (it's runtime-only, never
            # persisted) -- keep it pointing at the same cue under its new name.
            if engine.active_cue_name == name:
                engine.set_active_cue(new_name)

        return {"success": True, "name": cue.name, "renamed": renaming}

    @app.delete("/api/cues/{name}")
    def delete_cue(name: str):
        engine.config.cues = [c for c in engine.config.cues if c.name != name]
        removed = patch_writer.remove_cue(config_path, name)
        if engine.active_cue_name == name:
            engine.set_active_cue(None)
        return {"success": removed}

    @app.post("/api/active-cue")
    def set_active_cue(payload: ActiveCueRequest):
        if payload.name is not None and engine.config.get_cue(payload.name) is None:
            return JSONResponse(
                {"error": f"No cue named '{payload.name}' exists."}, status_code=400
            )
        engine.set_active_cue(payload.name)
        return {"success": True, "active_cue": payload.name}

    @app.get("/api/groups")
    def list_groups():
        return {"groups": [{"name": g.name, "fixtures": g.fixtures} for g in engine.config.groups]}

    @app.post("/api/groups")
    def create_group(payload: GroupRequest):
        name_error = _validate_group_name(payload.name)
        if name_error:
            return JSONResponse({"error": name_error}, status_code=400)
        if any(g.name == payload.name for g in engine.config.groups):
            return JSONResponse(
                {"error": f"A fixture group named '{payload.name}' already exists."},
                status_code=400,
            )
        unknown = [n for n in payload.fixtures if not any(f.name == n for f in engine.config.fixtures)]
        if unknown:
            return JSONResponse(
                {"error": f"Unknown fixture(s): {', '.join(unknown)}"}, status_code=400
            )
        raw = payload.model_dump()
        group = build_group(raw)
        engine.config.groups.append(group)
        patch_writer.add_group(config_path, raw)
        return {"success": True, "name": group.name}

    @app.put("/api/groups/{name}")
    def update_group(name: str, payload: GroupUpdateRequest):
        group = engine.config.get_group(name)
        if group is None:
            return JSONResponse({"error": f"No fixture group named '{name}'."}, status_code=404)
        unknown = [n for n in payload.fixtures if not any(f.name == n for f in engine.config.fixtures)]
        if unknown:
            return JSONResponse(
                {"error": f"Unknown fixture(s): {', '.join(unknown)}"}, status_code=400
            )

        new_name = payload.name.strip() if payload.name is not None else None
        renaming = new_name is not None and new_name != name
        if renaming:
            name_error = _validate_group_name(new_name)
            if name_error:
                return JSONResponse({"error": name_error}, status_code=400)
            if any(g.name == new_name for g in engine.config.groups):
                return JSONResponse(
                    {"error": f"A fixture group named '{new_name}' already exists."},
                    status_code=400,
                )

        group.fixtures = payload.fixtures
        patch_writer.update_group(
            config_path, name, payload.fixtures, new_name if renaming else None
        )

        if not renaming:
            return {"success": True, "name": name, "renamed": False}

        # A group is referenced by the token "group:<name>", so the rename has
        # to follow through into every rule and cue assignment targeting it --
        # otherwise those silently stop matching any fixture.
        group.name = new_name
        retargeted = {"rules": 0, "cue_assignments": 0}
        for rule in engine.config.rules:
            rewritten, changed = retarget_group_reference(rule.applies_to, name, new_name)
            if changed:
                rule.applies_to = rewritten
                retargeted["rules"] += 1
        for cue in engine.config.cues:
            for assignment in cue.rule_assignments:
                rewritten, changed = retarget_group_reference(
                    assignment.applies_to, name, new_name
                )
                if changed:
                    assignment.applies_to = rewritten
                    retargeted["cue_assignments"] += 1

        patch_writer.retarget_group_references(config_path, name, new_name)
        return {"success": True, "name": new_name, "renamed": True, "retargeted": retargeted}

    @app.delete("/api/groups/{name}")
    def delete_group(name: str):
        engine.config.groups = [g for g in engine.config.groups if g.name != name]
        removed = patch_writer.remove_group(config_path, name)
        return {"success": removed}

    @app.get("/api/rule-targets")
    def rule_targets():
        fixtures = [{"name": f.name, "roles": list(f.channels.keys()), "profile": f.profile_id}
                    for f in engine.config.fixtures]
        groups = []
        for g in engine.config.groups:
            roles = set()
            for fname in g.fixtures:
                fx = next((f for f in engine.config.fixtures if f.name == fname), None)
                if fx:
                    roles.update(fx.channels.keys())
            groups.append({"name": g.name, "members": g.fixtures, "roles": sorted(roles)})
        return {"fixtures": fixtures, "groups": groups}

    @app.get("/api/rule-targets/values")
    def rule_target_values(target_type: str, name: str):
        snap = engine.snapshot()

        if target_type == "fixture":
            fx = next((f for f in snap["fixtures"] if f["name"] == name), None)
            if fx is None:
                return JSONResponse({"error": f"No fixture named '{name}'."}, status_code=404)
            return {"values": fx["values"]}

        if target_type == "group":
            group = engine.config.get_group(name)
            if group is None:
                return JSONResponse({"error": f"No fixture group named '{name}'."}, status_code=404)
            per_role = {}
            for fname in group.fixtures:
                fx = next((f for f in snap["fixtures"] if f["name"] == fname), None)
                if fx is None:
                    continue
                for role, v in fx["values"].items():
                    per_role.setdefault(role, []).append(v)
            ranges = {role: {"min": min(vs), "max": max(vs)} for role, vs in per_role.items()}
            return {"ranges": ranges, "detail": per_role}

        return JSONResponse({"error": "target_type must be 'fixture' or 'group'."}, status_code=400)

    @app.get("/api/backup/export")
    def export_backup():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({
                "app": BACKUP_APP_ID,
                "format_version": BACKUP_FORMAT_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
            with open(config_path, "r") as fh:
                zf.writestr("patch.yaml", fh.read())
            # Bundled profiles ship with the app itself and don't need
            # backing up; imported/custom ones only exist on this machine.
            for subdir, dir_path in (("custom", library.custom_dir), ("imported", library.imported_dir)):
                if not dir_path.exists():
                    continue
                for path in sorted(dir_path.glob("*.json")):
                    zf.write(path, arcname=f"fixture_profiles/{subdir}/{path.name}")

        filename = f"artnet-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/backup/import")
    async def import_backup(file: UploadFile = File(...)):
        content = await file.read()
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = set(zf.namelist())
                if "patch.yaml" not in names:
                    return JSONResponse(
                        {"error": "That .zip doesn't contain a patch.yaml -- "
                                  "it doesn't look like a backup from this app."},
                        status_code=400,
                    )
                if "manifest.json" in names:
                    try:
                        manifest = json.loads(zf.read("manifest.json"))
                    except json.JSONDecodeError:
                        manifest = {}
                    if manifest.get("app") != BACKUP_APP_ID:
                        return JSONResponse(
                            {"error": "That .zip's manifest doesn't identify it as a "
                                      "compliance monitor backup."},
                            status_code=400,
                        )

                patch_yaml_text = zf.read("patch.yaml").decode("utf-8")
                try:
                    raw = yaml.safe_load(patch_yaml_text)
                    if not isinstance(raw, dict):
                        raise ValueError("patch.yaml must be a YAML mapping")
                except yaml.YAMLError as e:
                    return JSONResponse(
                        {"error": f"This backup's patch.yaml is invalid: {e}"}, status_code=400
                    )

                # (archive path, raw bytes) for every bundled fixture profile.
                profile_entries = [
                    (n, zf.read(n)) for n in names
                    if n.startswith("fixture_profiles/") and n.endswith(".json")
                ]
        except zipfile.BadZipFile:
            return JSONResponse({"error": "That file isn't a valid .zip archive."}, status_code=400)

        # Fixture profiles have to land (and the library reload) *before*
        # validating patch.yaml below: a restored fixture can reference a
        # profile that only exists in this same backup, and build_fixture()
        # resolves `profile:` against the live library, not the zip. This is
        # additive only -- existing profiles under other filenames are left
        # alone, so restoring a backup can't delete profiles that only exist
        # on the destination machine. If patch.yaml then fails to validate,
        # these are the only thing that landed; patch.yaml itself and the
        # live engine are untouched below until validation succeeds.
        for rel_name, data in profile_entries:
            parts = rel_name.split("/")
            if len(parts) != 3 or parts[1] not in ("custom", "imported"):
                continue
            target_dir = library.custom_dir if parts[1] == "custom" else library.imported_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            with open(target_dir / parts[2], "wb") as fh:
                fh.write(data)
        library.reload()

        # Now build and fully validate the restored config, using the exact
        # same parsing/migration path a normal startup load uses -- a
        # malformed backup should fail loudly here, not leave patch.yaml or
        # the live engine half-restored.
        try:
            new_config = parse_config(raw, library=library)
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse(
                {"error": f"This backup's patch.yaml is invalid: {e}"}, status_code=400
            )

        # Universes/listen_ip only take effect at startup (ComplianceEngine
        # sizes its per-universe buffers once, in __init__, and the Art-Net
        # node binds once too) -- flag it rather than trying to hot-apply.
        restart_required = new_config.universes != engine.config.universes

        # listen_ip is deliberately never restored: it's this *machine's*
        # LAN address, meaningless (or actively wrong) on whatever machine
        # the backup lands on next.
        patch_writer.restore_full_config(config_path, patch_yaml_text,
                                          keep_listen_ip=engine.config.listen_ip)

        # engine.config is the exact object NodeManager also holds a
        # reference to, so mutating its fields in place (rather than
        # rebinding engine.config = new_config) is what makes this visible
        # to both without any extra wiring. universes/listen_ip are left
        # alone here for the same reason they're not persisted above.
        engine.config.fixtures = new_config.fixtures
        engine.config.groups = new_config.groups
        engine.config.rules = new_config.rules
        engine.config.cues = new_config.cues
        engine.set_active_cue(None)  # the old active cue may not exist in the restored set

        return {
            "success": True,
            "restart_required": restart_required,
            "fixtures": len(new_config.fixtures),
            "groups": len(new_config.groups),
            "rules": len(new_config.rules),
            "cues": len(new_config.cues),
        }

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(json.dumps(engine.snapshot()))
                await asyncio.sleep(0.5)  # 2 Hz push — plenty for a compliance dashboard
        except WebSocketDisconnect:
            pass

    return app


