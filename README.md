# Art-Net Compliance Monitor

Registers itself as a unicast-assignable Art-Net node so consoles like Vista
can patch specific universes to it, then checks incoming DMX values against
configurable compliance rules and shows live status on a local web dashboard.

## Why this isn't Dockerized on macOS

Docker Desktop on macOS runs containers inside a lightweight VM, so
`--network host` doesn't give a container real access to your Mac's LAN
interface — it's a known, still-open limitation (broadcast packets in
particular don't reliably pass through, even with the newer "host
networking" beta flag). Since the ArtPoll discovery handshake requires
receiving real broadcast traffic, running the Art-Net side of this in Docker
on a Mac is unreliable.

Instead: **run it in a Python virtual environment** (`./setup.sh`). A venv
gives you the same core benefit people want from Docker — dependencies fully
isolated, nothing touches system Python, trivial to wipe and redo — without
fighting the network virtualization layer.

If you ever move this onto a Linux box (e.g. a dedicated always-on machine
instead of the Vista Mac), `docker-compose.yml` is included and will work
correctly there, since Docker's host networking is native on Linux.

## Fixture library

Fixture *profiles* (what a fixture model's channels mean) are kept separate
from *patched fixtures* (which universe/address a profile is assigned to in
your actual rig) — the same split real lighting consoles use. This means the
same profile can be reused across as many patched fixtures as you have of
that model.

Three ways to get a profile, all from the dashboard's "Fixture Patch" panel:

1. **From library** — pick anything already available (a few generic bundled
   profiles ship with the app) and a mode, give it a name/universe/start
   channel, and add it to the patch. A **quantity** field lets you add
   several at once — e.g. quantity 10 with name "House Light" patches
   "House Light 1" through "House Light 10", each assigned the next free
   block of channels after the last, automatically rolling into the next
   universe if a run would spill past channel 512.
2. **Import from OFL** — search [Open Fixture Library](https://open-fixture-library.org)
   by manufacturer or fixture name right from the dashboard (e.g. "chauvet
   slimpar" or "martin mac viper"), then hit **Select** on the result you
   want. That imports it in the background and switches you straight to the
   "From library" tab with it already chosen — just fill in the
   name/address/quantity and add it to the patch. Requires this machine to
   have internet access at the moment you search/import; once imported, a
   profile is cached locally and works offline from then on. The search
   itself uses a small local index built from OFL's public repository
   listing, refreshed automatically about once a week — no OFL account or
   API key needed.
3. **Custom Profiles** — for anything not on OFL (or your own in-house/DIY
   fixtures): name it, list its channels in DMX order, and give each one a
   role (`intensity`, `red`, `green`, `blue`, `white`, `amber`, `uv`, `pan`,
   `tilt`, `strobe`, etc. — freeform, but stick to consistent names so
   compliance rules can reference them). Saved the same way as an OFL
   import, just without needing the network. The same tab also lets you
   **edit** any profile you've created — pick it from the "Edit existing
   profile" dropdown, load one of its modes to modify, or type a new mode
   name to add another mode to it without disturbing the existing ones. A
   "Delete profile" button is available once you've selected one, blocked
   if any patched fixture still depends on it. Bundled and OFL-imported
   profiles aren't editable or deletable here — they're not yours to
   overwrite, though nothing stops you from defining a custom profile with
   similar channels if you need a variant.

Profiles live as plain JSON under `config/fixture_profiles/imported/` and
`config/fixture_profiles/custom/` — back those up along with `patch.yaml` if
you migrate to a new machine. A few bundled reference profiles ship inside
the app itself and don't need backing up. The OFL search index itself is
cached at `config/fixture_profiles/ofl_index_cache.json` — safe to delete
any time to force a fresh rebuild sooner than the automatic weekly refresh.

**On GDTF:** the newer, XML-based [GDTF](https://gdtf-share.com) format is
increasingly the professional standard and many consoles are moving toward
it, but its schema is substantially more complex than OFL's (geometry, DMX
personality wheels, etc.) and wasn't worth the added complexity for what
this tool needs. The profile system here is decoupled enough that a GDTF
importer could be added later as a third source, alongside OFL, without
changing anything else.

## Universe numbering

Universes are numbered the way Vista's UI shows them — starting at **1**, not
0. Art-Net's wire protocol is 0-based internally, but that conversion happens
only inside `artnet/node.py`, right at the packet boundary; the config file,
the callback that delivers DMX frames, logs, and the dashboard all use the
same number you'd see in Vista. If you ever need to cross-check against a
packet sniffer (Wireshark, etc.) showing raw Art-Net universes, subtract 1.

## Dashboard layout

The dashboard is organized into six tabs, plus a persistent interface
selector at the top:

- **Monitor** — an active-cue selector, then one panel per universe showing
  a 512-channel grid (32×16, matching how consoles like Vista lay out a
  patch view). Green means compliant, red means violating, grey means
  unpatched (shaded by live value so activity is still visible before
  patching). Click any cell for details — patched fixtures show their name,
  role, all sibling channel values, and current compliance status;
  unpatched channels just show the raw value.
- **Patch** — the fixture library / OFL search / custom-fixture panel for
  adding new fixtures, plus a 512-channel grid per universe (like Monitor's,
  but showing what's patched rather than live compliance) for everything
  already patched. Each fixture's name labels its lowest channel and
  overlays across the rest of its span; hover any of its cells to highlight
  the whole fixture, click one to edit its address/profile or delete it.
- **Fixture Groups** — named sets of fixtures (e.g. "House Whites", "Stage
  Front") so a rule can be assigned to a whole set at once instead of
  listing fixtures individually.
- **Rules** — the shared pool of compliance thresholds. A rule is a named
  package that can hold several channel-role entries at once (e.g. "House
  Whites 50%" setting dimmer/red/green/blue together), each with its own
  min/max slider and a shared DMX/percent display toggle, plus an import
  wizard for turning a fixture or group's *current live values* into a
  ready-to-edit rule in one step.
- **Cues** — named sets of rule *assignments*: for each cue, check which
  rules apply and pick one or more targets (fixtures and/or fixture groups,
  searchable) for each one. Create a cue with nothing assigned and come back
  later, or set it all up right away. Only one cue is active at a time —
  switch it here or from the Monitor tab's dropdown — and switching is
  instant, no restart needed.
- **Backup** — export everything patched, grouped, and ruled here (plus any
  imported/custom fixture profiles) as one `.zip`, and import one back in —
  see [Where your data lives, and updating safely](#where-your-data-lives-and-updating-safely)
  below for what does and doesn't carry over between machines.

## Fixture Groups

A fixture group is just a named set of fixtures, managed from the Fixture
Groups tab. A group can be picked as the target for a rule assignment
(below) or referenced directly as `group:<name>` in a rule's own
`applies_to` (only relevant for always-active rules — see below) — either
way, adding or removing a fixture from the group changes what's covered
without touching the rule itself.

The tab lists your groups in a table; **Add Fixture Group**, **Edit**, and
**Delete** each open a dialog rather than editing in place. Renaming a group
from the Edit dialog automatically repoints every rule and cue assignment
that targeted it, so nothing silently stops matching — the dialog reports how
many were updated. Group names can't contain a comma, since the Rules page's
`applies_to` field is a comma-separated list.

## Rules and Cues

Rules describe what's valid for a *type* of fixture, not a specific
instance of one. Each rule is scoped to a fixture **profile** (e.g. "SlimPAR
Pro H intensity cap") — so the same rule definition can be reused across
every fixture that shares that profile, and the Rules tab groups the list
by profile so related rules stay together. Every rule also gets a **name**
you choose, shown everywhere the rule appears (the pool list, cue
checklists, violation details) instead of a generic auto-generated label.

Each rule has an **"Always active"** flag that changes how its target
works:

- **Checked** — enforced all the time, against the target you set directly
  on the rule (`applies_to` — "*", specific fixture names, or
  `group:<name>`). Use this for a hard safety limit that should never be
  optional, e.g. an audience-facing blinder that must never exceed a
  certain intensity no matter what's happening in the show.
- **Unchecked** (the default) — has no fixed target of its own. Instead,
  each **cue** that wants to enforce this rule picks its own target when
  assigning it — so the same reusable "must be off" rule for a given
  profile can be pointed at one specific fixture in one cue and an entire
  fixture group in another, without redefining the rule each time.

A cue, on the **Cues** tab, shows a checklist of every non-always-active
rule (grouped by profile, same as the Rules tab). Checking one reveals a
target dropdown — populated with fixtures that use that rule's profile,
plus every fixture group — where you pick who this cue should apply that
rule to. Always-active rules aren't listed here since they enforce
regardless of any cue; there's nothing to assign. Rule assignment is
entirely optional at cue-creation time: get all your cues named and
organized first, then come back and assign rules whenever you're ready.
Editing a cue's assignments or deleting a rule outright both take effect
immediately with no restart — deleting a rule that's currently assigned to
one or more cues removes it from all of them automatically.

**Importing rules from current values:** rather than typing thresholds from
scratch, the Rules tab's "Import channel values" panel lets you pick a
fixture or group, load its live current values, and turn selected channels
into a rule. For a single fixture, it starts as an exact snapshot (`min =
max = current value`) that you then widen into a real range, or leave as a
hard requirement. For a group, if its members currently differ on a
channel, the import shows that spread as a range (e.g. "50–200") and sets
the new rule's min/max to those exact observed bounds — so the rule starts
already covering what the group is actually doing, rather than an arbitrary
average that might not match any real fixture. If every member happens to
agree, it's shown and imported as a single value, same as a fixture.

**DMX vs. percent:** every min/max field — on the Rules tab, in cue
checklists, in imports — can be viewed and typed in either raw DMX (0-255)
or percent (0-100) via the toggle at the top of the Rules tab. Purely a
display convenience; rules are always stored and sent to the server as DMX
values regardless of which unit is currently shown.

**Upgrading from an older version:** `patch.yaml` files have gone through a
few schema generations for rules and cues, and all of them load and convert
automatically the first time you run the updated app — nothing is lost no
matter how old your file is:

1. The oldest format embedded full rule definitions directly inside each
   cue. These get moved into the shared pool with a fresh id, marked
   `always_active: false`, and the cue gets an assignment pointing at the
   new pool rule with whatever target the old rule used to have.
2. The next format used a flat `rule_ids:` list per cue with no per-cue
   target — each id becomes an assignment carrying over that rule's own
   `applies_to` as the initial target.
3. Rules from before the `always_active` flag existed are treated as
   `always_active: true`, matching how they used to behave unconditionally.

The file on disk stays in whatever format it was in until you actually edit
something through the dashboard, at which point that section is rewritten
in the current shape.

Cues are created from the dashboard and persist to `patch.yaml` under a
`cues:` key the same way fixtures do; groups persist under `groups:`. Which
cue is *currently active* is runtime state, not saved to disk — it resets
to "none" on restart, so a show doesn't silently start already treating some
cue as active.

## Where your data lives, and updating safely

Everything you configure — universes, fixtures, fixture groups, rules,
cues, and the advertised network IP — lives in plain files under `config/`,
right next to wherever you run the app. No database.

- `config/patch.yaml` — your live configuration. Every add/remove from the
  dashboard rewrites this file.
- `config/fixture_profiles/imported/` and `.../custom/` — fixture profiles
  you've pulled from OFL or defined yourself.
- `config/fixture_profiles/ofl_index_cache.json` — the OFL search index
  cache, safe to delete any time to force a refresh.

**Updating to a new version of this app:** extract the new files into the
*same* folder rather than deleting it and starting fresh. Releases ship a
`config/patch.yaml.default` template, never `config/patch.yaml` itself — the
app creates your real `patch.yaml` from that template automatically the
very first time it runs, and never touches it again after that. Since an
update only ever replaces `patch.yaml.default`, your actual live config is
never in the set of files being overwritten, no matter how many times you
update. If you ever want to see what changed in the default template
between versions, `config/patch.yaml.default` itself is safe to diff or
delete — deleting it just means the next fresh install starts from
scratch, it has no effect on an existing `patch.yaml`.

Back up the whole `config/` folder and you have everything needed to
restore or move this to another machine — or use the dashboard's **Backup**
tab, which packages the same data (`patch.yaml` plus your imported/custom
fixture profiles) into one `.zip` you can download and hand to another
computer's Import button, without needing file/folder access to either
machine.

A couple of things are handled deliberately on import, not just copied
verbatim:

- **The network address (`listen_ip`) is never restored** — it's specific to
  the machine you exported from, and would be wrong (or actively harmful)
  on a different one. The destination machine keeps its own; use the
  "Advertise as" dropdown there as usual.
- **Fixture profiles are added, not swapped in wholesale** — importing a
  backup won't delete a custom or imported profile that only exists on the
  machine you're restoring onto.
- **The universe list only takes effect after a restart.** Fixtures,
  groups, rules, and cues from the backup are live immediately; if the
  backup's universes differ from what's currently running, restart the app
  once to pick that part up too.

## Setup (macOS or Linux, via venv)

```bash
./setup.sh
```

Then edit `config/patch.yaml`:
- Leave `listen_ip` as-is for now — you'll set the real value from the
  dashboard's network interface dropdown once it's running (see below),
  which is easier than hunting down the right IP by hand and editing YAML.
- Set `universes` to the Art-Net universe numbers you want to capture
  (already set to 8 as a starting point — add more any time, numbered the
  way Vista shows them, starting at 1).
- Add `fixtures` entries mapping DMX addresses to names and channel roles
  (intensity, red/green/blue, etc).
- Add `rules` for whatever your compliance standard requires.

Run it:

```bash
./run.sh
```

Open the dashboard at `http://<this-machine's-ip>:8000/` and use the
**"Advertise as"** dropdown at the top to pick the correct network interface
— this is what actually sets `listen_ip`. Hitting Apply restarts just the
Art-Net node (not the whole process) and saves the choice back into
`config/patch.yaml`, so it's remembered next time you start the app.

> If you're updating an existing install rather than starting fresh, this
> version added `psutil` as a dependency (used to list network interfaces).
> Re-run `./setup.sh` / `setup.bat`, or `pip install -r requirements.txt`
> inside the existing venv, before running.

## Setup (Windows, via venv)

Same idea, different scripts — double-click, or run from a command prompt:

```
setup.bat
```

Edit `config\patch.yaml` the same way as above (find the LAN IP with
`ipconfig` and look for the IPv4 Address on your active adapter).

**Open the firewall for inbound Art-Net traffic** — Windows Firewall blocks
unsolicited inbound UDP by default, so Vista's ArtPoll packets won't reach
the process otherwise. Run this once, from an elevated (Administrator)
PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Art-Net Compliance Monitor" -Direction Inbound -Protocol UDP -LocalPort 6454 -Action Allow
```

Then run it:

```
run.bat
```

Note: Docker Desktop's host-networking limitation described above applies
to Windows too (it's a Linux-only driver, not a Mac-specific gap), so use
the venv path on a Windows deployment machine as well — reach for Docker
only if the dedicated machine ends up running Linux.

Then in Vista:
1. Open the universe/patch connection screen.
2. Trigger node discovery (Vista sends ArtPoll automatically) — this
   machine should appear as **"ComplianceMon"** in the node list.
3. Assign whichever Vista universes you want monitored to this node's ports.

Open the dashboard at `http://<this-machine's-ip>:8000/`.

## Project layout

```
artnet_monitor/
  artnet/
    protocol.py   Art-Net packet parsing/building (ArtPoll, ArtPollReply, ArtDmx)
    node.py       Socket handling: discovery responder + DMX capture loop
  config.py       Loads config/patch.yaml into Fixture/Rule objects
  engine.py       Decodes raw DMX into per-fixture values, evaluates rules
  web/
    app.py        FastAPI app: REST snapshot + WebSocket live push
    static/       Dashboard frontend
  main.py         Wires it all together, CLI entrypoint
config/patch.yaml Your patch + compliance rules — edit this, not the code
```

## Scaling beyond 8 universes

Nothing else to change — just add universe numbers to `config/patch.yaml`.
Art-Net limits each ArtPollReply packet to 4 ports, so the node automatically
splits your universe list into groups of 4 and sends one ArtPollReply per
group, all under the same node identity, so they all show up as ports on the
same node in Vista.

## Known simplifications worth knowing about

- `node.py` binds `0.0.0.0:6454` with `SO_REUSEPORT` where available (Linux/
  macOS only — Windows doesn't define this option, so the code skips it
  automatically via `hasattr`). This only matters if something else on the
  same machine also wants port 6454; on a dedicated monitoring machine
  nothing else should be competing for it, so this is now mostly moot.
- The ArtPollReply builder implements the fields Vista needs for discovery
  and port assignment; a couple of rarely-used fields (RDM UID, video/macro
  controls) are left zeroed since they don't affect DMX capture.
- Compliance rules currently support `max`/`min` per channel role. Extend
  `Rule` in `config.py` if you need e.g. rate-of-change or strobe-frequency
  checks later.
