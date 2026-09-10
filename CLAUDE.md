# CLAUDE.md

Notes for Claude Code sessions working in this repo. README.md is the
user-facing doc (setup, how to operate the dashboard); this file covers
architecture, invariants, and the traps that aren't obvious from reading a
single file.

## What this is

A read-only Art-Net compliance monitor. It advertises this machine to a
lighting console (Vista) as an Art-Net node, captures the ArtDmx frames the
console unicasts back, decodes them into per-fixture channel values, and
flags values that violate configured rules. It **never transmits DMX** — it
only listens and reports. Nothing here should ever gain the ability to drive
lights.

## Tests

```sh
python -m pytest        # 53 tests, ~1s
```

`tests/` covers the things that regress silently: the three cue schema
generations and the legacy flat-rule migration, patch_writer's surgical
edits (comment preservation, legacy-key cleanup, sibling rules surviving an
edit), reference retargeting on group/fixture rename, per-cue report
evaluation, path-traversal/zip-slip rejection, and the backup round-trip.
Every test works on a throwaway copy in `tmp_path` — none of them can touch
a real `config/patch.yaml`. Endpoints are called as plain functions via the
`routes` fixture (httpx isn't a dependency).

## Security posture

This is a LAN tool with real authority: the dashboard rewrites the patch,
restarts the Art-Net node, and can replace the entire configuration.

- **Binds `127.0.0.1` by default** (`--web-host`). `0.0.0.0` is opt-in and
  logs a warning when used without a token.
- **`--auth-token` / `ARTNET_AUTH_TOKEN`** — when set, every request needs
  it (`X-Auth-Token` header, `?token=` once, or the cookie that sets).
- **Cross-origin writes are refused** by middleware in `create_app`. The
  JSON endpoints were already incidentally safe (cross-origin JSON POSTs
  trigger a preflight nothing answers), but `multipart/form-data` is a CORS
  *simple* request — without this, any page could silently POST a crafted
  backup zip to localhost and wipe the config.
- **Path safety lives in `paths.py`.** `safe_filename()` + `resolve_within()`
  guard every place a user-supplied string becomes a filename (profile ids,
  report ids, zip entries). **Windows treats `\` as a separator too**, so
  sanitising only `/` is not enough — that gap was a live zip-slip in the
  backup importer and a traversal via manufacturer/model in profile saves.

## Encoding & atomic writes (paths.py)

Config reads go through `read_text_lenient()` and writes through
`atomic_write_text()`. Both matter more than they look:

- **Always UTF-8, with a cp1252 fallback.** Bare `open()` on Windows means
  cp1252; the header comments' em-dashes only survived because those bytes
  round-trip identically between the two encodings. A fixture named with any
  character outside cp1252 would have raised UnicodeEncodeError and failed
  the write.
- **`read_text_lenient` normalises newlines to `\n`.** It reads binary (to
  control decoding), so without this the CRLFs come through literally and
  `atomic_write_text` (newline=None) translates them *again* — turning every
  line ending into `\r\r\n`, a little worse on every write. There's a test
  for this; it was caught that way, not by inspection.
- **Writes are atomic** (temp file + fsync + `os.replace`) and **serialized**
  by `_serialized`/`_config_lock` in patch_writer, since every writer is a
  read-modify-write cycle and FastAPI runs sync endpoints on a threadpool.

## Running it

```sh
run.bat          # Windows: activates venv, python -m artnet_monitor.main --config config\patch.yaml
./run.sh         # macOS/Linux equivalent
```

Dashboard at `http://localhost:8000/`. There's **no build step** — the
frontend is one hand-written static HTML file, served as-is. Backend logic
has tests (above); frontend changes are still verified by running the app
and clicking through.

Requires a real LAN IP (not loopback) to be useful, since Vista unicasts
ArtDmx back to whatever address the node advertises. Starting the app on a
dev machine with no console on the network works fine — universes just show
"NO DATA".

## Architecture

```text
main.py            CLI entrypoint; builds library → config → engine → node manager → FastAPI app
config.py          patch.yaml → dataclasses (Fixture/Rule/Group/Cue/Config) + schema migrations
config_bootstrap.py First-run copy of patch.yaml.default → patch.yaml
engine.py          ComplianceEngine: holds latest DMX per universe, evaluates rules, builds snapshots
manager.py         NodeManager: owns the ArtNetNode, swaps advertised IP without a process restart
patch_writer.py    Surgical YAML edits to patch.yaml (see "The patch writer" below)
netiface.py        Lists IPv4 interfaces for the "Advertise as" dropdown
artnet/protocol.py ArtPoll / ArtPollReply / ArtDmx packet parse + build
artnet/node.py     Socket loop: discovery responder + DMX capture thread
fixture_library/   Profile catalog: bundled JSON + OFL import + user custom profiles
web/app.py         FastAPI: REST endpoints + /ws WebSocket push
web/static/index.html  The entire frontend — markup, CSS, and JS in one file
```

Data flow: Art-Net receive thread → `engine.ingest_frame(universe, data)` →
stored under a lock → web thread calls `engine.snapshot()` → JSON pushed over
`/ws` at 2 Hz → `render()` in index.html repaints the universe grids.

**Threading:** `ingest_frame` runs on the Art-Net receive thread while
`snapshot` and `set_active_cue` run on the web server thread. `ComplianceEngine._lock`
guards the shared state. Anything new that touches `_universe_data`,
`_last_update`, or `active_cue_name` must take the lock.

## The config model (the part worth understanding)

`config/patch.yaml` is the single source of truth for everything except the
active cue (runtime-only, deliberately not persisted).

- **fixtures** — a patched fixture: name, universe, start_channel, plus
  either `profile` + `mode` (resolved via the fixture library) or an inline
  `channels: {role: absolute_channel}` map. Channel numbers in the config are
  absolute and 1-based. Roles named `reserved` are skipped when a profile is
  expanded.
- **groups** — named sets of fixture names. Referenced elsewhere as the
  string `"group:<name>"`.
- **rules** — one shared pool. A rule is a *named package* (`id`, `name`,
  `profile_id`, `description`, `applies_to`, `always_active`) holding one or
  more `entries` — `{channel, max, min}` thresholds. All entries in a rule
  share the package's target and always-active flag; that's the point of
  grouping them — "House Whites 50%" can set dimmer/red/green/blue/strobe
  thresholds all at once as one named unit instead of five separate rules.
  `always_active: true` means every entry is enforced constantly against the
  rule's own `applies_to`. `always_active: false` (the common case) means the
  rule carries no target of its own — each cue that selects it supplies the
  target, applying to every entry at once.
- **cues** — named sets of `rule_assignments` (`{rule_id, applies_to}` pairs).
  Only one cue is active at a time. Its rules are checked *in addition to*
  the always-active ones; duplicates are de-duped by rule id.

`applies_to` is either the string `"*"` or a **list** whose entries are
fixture names and/or `"group:<name>"` strings. `matches_applies_to()` in
config.py is the single matcher for both a rule's own `applies_to` and a cue
assignment's — use it rather than reimplementing the comparison.

### Schema migrations live in `load_config`

Cues have been through three generations, and all three still load:

1. oldest — full rule objects embedded under the cue (`cue.rules`)
2. middle — `rule_ids:` flat list, target baked into the rule itself
3. current — `rule_assignments:` `{rule_id, applies_to}` pairs

`config/patch.yaml` in this repo currently contains a **live gen-2 cue**
("Pre Service" with `rule_ids`), so don't assume every cue on disk has
`rule_assignments`. Migration happens on load and is only written back to
disk if that cue is later edited through the API. Don't delete these
migration paths.

Similarly, a top-level rule with no `always_active` key at all predates the
flag and is defaulted to `True` on load (in `load_config`, deliberately not
in `build_rule`, since `build_rule` is also used for cue-migrated rules that
should default to `False`).

### Rules were single-channel before they were packages

Every rule used to check exactly one `channel`/`max`/`min` triple sitting
directly on the rule. Rules became named packages holding an `entries` list
so one rule can cover several channels at once (`config.py`'s `RuleEntry` +
`Rule.entries`). `build_rule()` in config.py migrates the legacy shape
transparently: if a raw rule dict has no `entries` key, its top-level
`channel`/`max`/`min` become a single-entry package on load. **This repo's
`config/patch.yaml` currently has 20 such legacy single-entry rules** (the
"imported from House Whites"/"House Colors" ones) — don't assume every rule
on disk has an `entries` key.

As with cues, migration only writes back to disk when that specific rule is
next edited through the API — `patch_writer.update_rule()` explicitly pops
the stale `channel`/`max`/`min` keys before merging in the new `entries` key,
so the file doesn't end up with both shapes at once. **Be careful here**:
`update_rule()` re-serializes *every* rule in the file whenever any one of
them is edited (not just the edited one), so `_ordered_rule()` in
patch_writer.py deliberately leaves a legacy dict's shape untouched (no
`entries` key) unless it already has one — this is what stops editing rule A
from silently truncating rule B's legacy `channel`/`max`/`min` into a
phantom zero-entry package the next time the file loads. This exact bug
happened once during development; if you touch `_ordered_rule` or
`update_rule`, re-verify with a two-rule fixture (one legacy, one edited)
that the untouched one still has its channel/max/min, not `entries: []`.

The engine (`engine.py: ComplianceEngine.snapshot`) iterates
`rule.entries` per applicable rule rather than checking one channel per
rule; violations still carry the *package's* `name`/`description`, since
entries don't have their own.

## The patch writer — read this before touching persistence

`patch_writer.py` edits `patch.yaml` **without** a round-trip YAML library.
For a given top-level key it regex-locates that key's exact text span, parses
only that span with PyYAML, mutates the Python list, re-serializes just that
span, and splices it back. Everything outside the edited block — including
the large explanatory comment header — survives untouched.

Consequences to keep in mind:

- Comments *inside* an edited block are lost the first time that block is
  written through the API. Comments elsewhere are safe. This is why the
  header comments in patch.yaml still exist after months of dashboard edits.
- Every writer function is its own read-modify-write cycle. Multi-block
  operations (e.g. deleting a rule, then stripping it from every cue) are
  two separate writes and are **not atomic**. This is accepted — it's a
  single-user local tool.
- Writers are keyed by `name` (fixtures, cues, groups) or `id` (rules). Any
  operation that changes a key must also rewrite everything referencing it.
- API mutations must update **both** the in-memory `engine.config` object
  **and** call the corresponding `patch_writer` function. Updating only one
  is the most common bug shape here: the UI looks right until restart, or
  the file is right but the live engine isn't.

### Cross-references that must be maintained

| Changing | Also update |
| --- | --- |
| rule id / deleting a rule | every cue's `rule_assignments` (`remove_rule_id_from_all_cues`) |
| group name | every rule's `applies_to` and every cue assignment's `applies_to` (`retarget_group_references`) |
| fixture name | group membership lists, and any `applies_to` naming that fixture (`retarget_fixture_references`) |

Fixture rename **is** implemented now: `PUT /api/patch/fixtures/{name}`
accepts a `name`, and `patch_writer.retarget_fixture_references()` follows it
into group membership lists, rule `applies_to`, and cue-assignment
`applies_to`. `unpatch_fixture` (delete) still leaves the name behind in any
group that listed it — that's the remaining gap.

Both add and update also reject **address collisions** now
(`config.conflicts_for`): a fixture patched over another's channels used to
be silently accepted, with the second one quietly taking the owner map and
vanishing from the grids while still being compliance-checked.
`GET /api/patch/conflicts` (`config.channel_conflicts`) reports overlaps that
already exist in a config from before that check.

The Edit menu also only supports fixtures patched via a library profile +
mode. A fixture using an inline `channels: {role: absolute_channel}` map
(never actually reachable from the current "From library" add form, but
possible by hand-editing patch.yaml) shows a "not editable here yet" message
instead — its `channels` aren't derived from `start_channel` the way a
profile's are, so there's no well-defined "move it" or "reprofile it"
operation for that shape without designing one.

## Fixture library

Three layered sources, later wins on id collision:

1. `artnet_monitor/fixture_library/profiles/bundled/*.json` — shipped, read-only
2. `config/fixture_profiles/imported/*.json` — fetched from Open Fixture Library
3. `config/fixture_profiles/custom/*.json` — user-authored

Profile ids encode the source: `custom:<slug>`, `ofl:<mfr>/<fixture>`. Only
`source == "custom"` profiles can be edited or deleted through the API, and
deletion is refused while any fixture is still patched to them.

A mode is just an ordered list of channel role strings; a channel's DMX
offset is its index in that list. Role names are an open vocabulary, not an
enum — importers can introduce fixture-specific roles freely. Rules match
roles by exact string, so a profile calling it `dimmer` and a rule saying
`intensity` simply won't match. (Note the live config uses `dimmer`, not
`intensity`.)

## Compliance report (judging every cue at once)

The Report tab (`GET /api/report`, `ComplianceEngine.generate_compliance_report`
in engine.py) is a frozen, timestamped snapshot that judges **every** cue
against current live DMX at once, each as if it alone were active — unlike
`snapshot()` (and the Monitor page), which only ever evaluates whichever
cue is actually selected via `active_cue_name`. It's generated on demand,
not live-updating, and doesn't touch `active_cue_name` or any other state —
purely a read.

**Per-cue evaluation** (`_rule_bindings_for_cue` + `_evaluate_cue_compliance`):
every always-active rule (targeted by its own `applies_to`) plus every rule
*that specific cue* assigns (targeted by that assignment's `applies_to`),
deduped by rule id — the exact same combination `_rules_for` already builds
for the real active cue, just generalized to an arbitrary cue.

**Status is one of three values, not two:**

- `"unconfigured"` — this cue has **zero** applicable rules at all, its own
  assignments *and* always-active ones combined. This is deliberately
  narrower than "this cue has no rule_assignments of its own": every cue
  automatically inherits all always-active rules regardless of its own
  configuration, so if any always-active rule is currently violated, that
  has to surface as a real `"violation"` — marking the cue neutral/gray
  would hide a genuine problem behind a "nothing to see here" badge. In
  practice `"unconfigured"` only fires for a project with no always-active
  rules defined anywhere yet.
- `"violation"` — at least one applicable rule's entry is failing somewhere
  among its target fixtures.
- `"compliant"` — otherwise.

**Violation reporting is two-tier, on purpose:** the headline
(`rule_reports[].channels`) lists the *distinct channel roles* failing
anywhere among a rule's target fixtures — e.g. a rule named "House Blue 65%"
targeting a group renders as "House Blue 65% — intensity out of range, red
out of range", not one line per fixture. `channels` is explicitly reordered
to match the rule's own `entries` sequence rather than fixture-discovery
order (which fixture happens to violate first would otherwise make the same
rule print its channels in a different order between report runs). Which
*specific* fixture(s) and their actual values live in `rule_reports[].violations`,
rendered as expandable detail in the frontend (`.report-rule-toggle`) rather
than inline — deliberately not shown up front, per how this was scoped.

`engine.py`'s per-fixture DMX decode (`data[chan-1]` from the raw universe
buffer) was factored into `_decode_fixture_values()` specifically so the
report and `snapshot()` share it rather than drifting into two copies of
the same channel-indexing logic — if you touch one, check the other still
agrees (verified behavior-identical when this was split out).

## Backup & restore (moving setup between computers)

The Backup nav tab (`GET /api/backup/export`, `POST /api/backup/import` in
web/app.py) round-trips the whole setup as one `.zip`: `manifest.json`
(app id + format version, so an unrelated zip gets rejected with a clear
error instead of a confusing parse failure), `patch.yaml` verbatim, and
every file under `fixture_profiles/custom/` and `fixture_profiles/imported/`
(bundled profiles aren't included — they ship with the app itself and exist
on every install already).

Two things are **deliberately never carried over** on import:

- **`listen_ip`** — restoring the source machine's LAN address onto a
  different machine would be actively wrong, not just stale. The
  destination machine's current `listen_ip` is always kept; only every
  other field of `patch.yaml` comes from the backup. This is done by
  `patch_writer.restore_full_config()`: write the backup's `patch.yaml`
  wholesale (unlike every other `patch_writer` function, this one is a full
  replace, not a surgical per-block edit — "restore a backup" means
  replace), then immediately rewrite just the `listen_ip:` line back to the
  destination's own value via `patch_writer.set_listen_ip()`.
- **`universes`** — this *would* be reasonable to restore (it describes your
  patch structure, not the machine), but `ComplianceEngine.__init__` sizes
  its per-universe buffers once and `NodeManager` binds the Art-Net node
  once; neither has a hot-reload path. So the imported value is written to
  patch.yaml but deliberately left untouched in the live `engine.config` —
  the response's `restart_required` flag tells the frontend to say so, and
  fixtures/groups/rules/cues take effect immediately regardless.

**Fixture profiles are additive, not a wholesale replace** — importing
writes/overwrites files by name into `custom_dir`/`imported_dir` and calls
`library.reload()`, but never deletes a profile that only exists on the
destination machine. This has to happen *before* `patch.yaml` is validated:
a restored fixture can reference `profile: custom:something` that only
exists in this same backup, and `build_fixture()` resolves that against the
live library, not the zip's contents. Validating patch.yaml first (as you'd
expect from a "check everything before touching anything" approach) would
reject a perfectly good backup that references its own bundled profiles.
The order that actually works: write profiles → `library.reload()` →
`parse_config()` (this is what makes fixture-profile references resolve) →
only if that succeeds, `restore_full_config()` and mutate `engine.config`.
A `patch.yaml` that fails to validate at that point leaves the newly-written
profile files in place (low-risk — they're additive, static files) but never
touches `patch.yaml` or the live engine.

`config.py`'s `load_config(path, library=None)` is split into that plus
`parse_config(raw: dict, library=None)` specifically so the import endpoint
can run the *exact* same parsing/migration path (all three cue generations,
the always-active default, the rule-package legacy-shape fallback) against
an in-memory dict from the uploaded zip, without needing a temp file on
disk. Keep them in sync if you touch either.

`engine.config` is mutated in place on import (`engine.config.fixtures =
new_config.fixtures`, etc.) rather than rebinding `engine.config =
new_config` — `NodeManager` holds a reference to the exact same `Config`
object, so an in-place mutation is what both see without extra wiring; a
rebind would silently leave `manager.config` pointing at the stale object.
This is the same pattern every other mutating endpoint in app.py already
uses (`engine.config.groups.append(...)`, etc.) — backup import is not a
special case here, just the highest-cardinality one.

## Universe numbering

Universes are **1-based everywhere** — config, callbacks, logs, dashboard —
matching what Vista displays. The 0-based Art-Net wire value exists only
inside `artnet/node.py` and `artnet/protocol.py`. Don't leak wire numbers
outward.

## Frontend conventions

`web/static/index.html` is the whole UI: ~1800 lines of markup + CSS + JS,
no framework, no bundler, no dependencies. Edit it directly.

- Top-level nav is `.nav-tab` → `.page` (Monitor / Patch / Fixture Groups /
  Rules / Cues). Sub-tabs inside the Patch panel are `.tab` → `.tabpage`.
- Reopening a nav tab re-fetches that page's data (the handler near the end
  of the script) — the pages don't share a reactive store, so any mutation
  should call the relevant `loadX()` afterward.
- **DMX vs percent:** everything is stored, sent, and compared as raw DMX
  (0-255). The percent setting on the Rules page is display-only —
  `dmxToDisplay` / `displayToDmx` convert at the edges, and inputs stash the
  authoritative DMX value in a `data-dmx` attribute. Never send display units
  to the API.
- **Modals:** `openFormModal({title, bodyHtml, buttons})` is the shared
  add/edit/delete dialog helper. Buttons are declared per call with `variant`
  (`secondary` / `danger`); the modal stays open after a click so a failed
  save can report into `formModalMsg` in place, and the handler calls
  `closeFormModal()` on success. `#detailModal` is separate and belongs to
  the Monitor page's channel inspector.
- **Page pattern (Fixture Groups, Cues, and Rules all follow it):** a
  `.page-header` with the title and a primary "+ Add …" button on the right,
  then a `.data-table` listing records with action buttons in a
  right-aligned `.col-actions` column. All create/edit/delete happens in
  modals, never inline on the page, and every action confirms via
  `showToast()` (see below), not a page-level message strip. Operational
  (non-CRUD) actions live in the table too — that's why "Set Active / Clear"
  sits in the Cues row rather than in a dialog. The Rules table groups rows
  by profile using full-width `tr.group-row` header rows.
- **The Patch page is a hybrid, deliberately.** Adding a fixture still uses
  the older inline tabbed form (`.tabs`/`.tabpage`: "From library" / "Import
  from OFL" / "Custom Profiles") — that flow spans a library search, an
  external OFL fetch, and profile *authoring*, which doesn't compress into a
  single add-dialog the way a group/rule/cue's flat field list does. But the
  list of already-patched fixtures below it is not a `.data-table` — it's a
  512-cell grid per universe built from the live `owners` map. Clicking a
  patched cell opens `openFixtureMenuModal(name)` — a small Edit/Delete menu.
- **The universe grid itself (`.chan-grid`/`.chan-cell`) is shared between
  Monitor and Patch**, not just visually similar — `buildUniverseGridCells(grid)`
  creates the 512 numbered cells, and `applyFixtureGrouping(cells, owners)`
  groups them by fixture: the lowest channel a fixture owns gets its name
  overlaid (`.fixture-label`, anchored via `.label-anchor`'s elevated
  z-index so the overflow paints over later sibling cells rather than under
  them), and hovering any of a fixture's cells highlights all of them
  (`.hl`). Both pages call this after building/fetching their own snapshot;
  only cell *background* meaning, click handlers, and title text stay
  page-specific (Monitor: live compliance color + `openChannelDetail`;
  Patch: a static blue "patched" fill + `openFixtureMenuModal`). Monitor
  guards the (re)build behind `universeOwnersSignature` — a per-universe
  `JSON.stringify(owners)` fingerprint — so the label/hover-group DOM isn't
  torn down and rebuilt on every 2Hz websocket tick, only when the actual
  patch changes; Patch has no such guard since it only re-renders on demand.
- **Dialog code shape:** each modal is a pair — an `xHtml(prefill)` builder
  returning a string, and a `wireX(bodyEl, prefill)` that attaches listeners
  and populates selects after insertion (passed as `onReady`). Keep new
  dialogs in that shape.
- **The Add/Edit Rule dialog nests this same pair inside itself.** The rule
  form (`ruleFormHtml`/`wireRuleForm`) covers the package-level fields; each
  channel entry row is its own `ruleEntryRowHtml(entry)` +
  `wireRuleEntryRow(row, entry)` pair, appended into `#ruleEntries` by a
  closure inside `wireRuleForm`. `collectRuleForm(bodyEl)` reads all
  `.rule-entry-row` elements back into an `entries` array on submit. If you
  add another package-shaped concept (a set of children sharing one parent
  form), follow this nesting rather than flattening it into one giant form.
- **Filterable checklists:** `wireChecklistFilter(bodyEl)` powers the
  case-insensitive search in the group and cue dialogs. Markup contract —
  container `[data-filter-list="<noun>"]`, rows `[data-search="<lowercased
  haystack>"]` with optional `[data-group]`, headers
  `[data-group-header="<same value>"]` (a header hides once all its rows
  filter out). Rows are hidden, never re-rendered, so checkbox state and
  per-row selects survive typing. Filtering narrows the view only — it never
  deselects, and submit handlers read all checked rows regardless of
  visibility. Row/header lookups use `:scope > [data-search]` (not a deep
  `querySelectorAll`) specifically so a checklist can be **nested** inside
  another checklist's row without the outer filter pass sweeping up the
  inner one's rows too — see the target picker below.
- **The "applies to" target picker** (`targetPickerHtml`/`wireTargetPicker`/
  `collectTargetPicker`) is the shared searchable, multi-select widget for an
  `applies_to` value — used by the Cues dialog's per-rule target (nested one
  per row) and the Rules dialog's "Applies to" field. Groups are always
  listed before fixtures (`targetPickerHtml`'s two blocks, in that order) so
  targeting a whole group doesn't require scrolling past every fixture
  first. An "All fixtures (*)" checkbox toggles the list's visibility;
  `collectTargetPicker` returns the literal string `"*"` when it's checked
  *or* when nothing else is checked, and otherwise an array mixing fixture
  names and `"group:<name>"` tokens — the same shape `matches_applies_to()`
  in config.py already expected, so no backend change was needed to support
  multi-target selection once the picker existed.
- **Toast notifications:** `showToast(text, {variant})` (variant is
  `"success"` (default) or `"error"`) is the one place action confirmations
  belong — "Created X", "Deleted Y". It renders into the fixed `#toastStack`
  container (bottom-right), auto-dismisses after `TOAST_LIFETIME_MS` (5s), or
  dismisses immediately on click anywhere on the toast. Older page-specific
  notice functions (`groupsNotice`/`cuesNotice`/`rulesNotice`, each writing
  into a page-level `.msg` strip) were replaced by this — don't reintroduce
  that pattern for a new page; call `showToast` directly instead. This is
  separate from `formModalMsg`, which stays a static `.msg` line inside an
  *open* dialog for blocking validation errors ("Name is required") — those
  need to stay visible next to the field until fixed, not time out.
- **Beware `.field input`:** it sets `min-width: 140px` plus form-field
  chrome, and it matches checkboxes too. `.checklist` and `.cue-rule-row`
  override it; the Rules page's "Always active" checkbox is still affected.
  Fixing it properly means `.field input:not([type=checkbox])`.
- Use the `escapeHtml()` helper when interpolating user-supplied names into
  `innerHTML`. Older code in this file interpolates raw — that's a latent bug,
  not a convention to copy.

## Gotchas

- The Monitor grid deliberately shows green **only** when a rule actually
  checks that channel. "Active but unchecked" is grey, so green never
  falsely implies verification. Preserve that distinction.
- `snapshot()` rebuilds the channel-owner reverse lookup on every call rather
  than caching it — intentional, so a patch edit is never stale.
- Fixture profile deletion uses a `{profile_id:path}` route because ids
  contain `/`.
- `psutil` is only used by `netiface.py` for the interface dropdown.
- The venv lives in `venv/` at the repo root and is not tracked.
