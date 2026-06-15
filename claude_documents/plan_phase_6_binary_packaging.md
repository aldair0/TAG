# Phase 6 Plan — Binary Packaging & Shop-PC Distribution

**Goal:** Ship the finished app to the shop PC as a self-contained Windows
artifact that runs with **no Python install** — a PyInstaller one-folder
bundle, supervised as a Windows service, reachable over LAN HTTPS by the
tablet. Operators edit one `.env`, double-click (or the service starts it),
and it works.

**Status when this plan was written:** Development is **not** complete.
Features through the email receiver exist; eBay/Shopify/POS phases are still
landing. This plan therefore has two halves:

1. **Continuous (do now, every phase):** cheap hygiene that keeps the
   eventual build from becoming a multi-day archaeology dig. Most of it is
   "don't reintroduce CWD/`__file__` assumptions."
2. **Packaging sprint (do once features are frozen):** the actual spec,
   service, proxy, backups, runbook.

The single most important recommendation: **do a throwaway PyInstaller build
NOW**, mid-development, to surface the hard problems (chromedriver, hidden
imports) while there's still time to design around them. Do not discover them
the week you're trying to ship.

**Tech decisions (confirmed in architecture doc §"Distribution"):**
- PyInstaller **one-folder** (onedir), not one-file. Faster startup, easier
  to debug, and a one-file temp-extract dir is hostile to a long-running
  service and to a bundled browser driver.
- Process supervisor: **NSSM** (or Task Scheduler "always running").
- TLS: **direct uvicorn HTTPS** with a self-signed LAN cert (Caddy deferred —
  see Task 6.2). No reverse proxy in the default deployment.
- Public ingress for Shopify webhooks (Phase 5): **Cloudflare Tunnel** or
  **Tailscale Funnel** — only inbound-from-internet surface.
- Backups: **Litestream** streaming the SQLite DB.

**What's explicitly NOT in this plan:** feature work (that's Phases 1–5),
auto-update/installer (manual folder swap is acceptable for one PC),
code-signing the exe (optional; revisit if SmartScreen is a nuisance).

---

## Part 1 — Continuous hygiene (maintain every phase)

These are rules + small guards, not a sprint. They cost little now and save a
lot later.

### 1.1 All paths go through `app/paths.py`
Two anchors, already established:
- **`resource_root()`** → bundled, **read-only** assets (templates, static,
  and at build time `alembic/`). Frozen: `sys._MEIPASS`.
- **`app_dir()`** → user-editable, **persistent** files beside the exe
  (`.env`, `data/`, image cache). Frozen: the executable's directory.

Rule for all new code: **never** `open("data/...")`, `Path("./x")`,
`os.getcwd()`, or `Path(__file__).parent` to reach a runtime file. Use a
`paths.py` helper. Add new helpers there as new file kinds appear.

### 1.2 A regression guard
Add `tests/test_no_cwd_paths.py` that greps the `app/` tree for the banned
patterns above (allowing `paths.py` itself). Cheap insurance that a future
phase doesn't silently reintroduce a CWD assumption that only breaks once
frozen — the worst kind of bug to find late.

### 1.3 Vet every new dependency for freeze-friendliness when added
When a phase adds a dep, note in its plan whether PyInstaller needs a hook /
hidden import / `collect_data_files`. Known so far:
- `uvicorn[standard]` — needs `--collect-submodules uvicorn` and its loop/
  protocol impls as hidden imports.
- `apscheduler` — entrypoint-based; needs `collect_submodules`.
- `sqlalchemy` — dialects load lazily; the sqlite dialect must be a hidden
  import.
- `imapclient` — pure-ish, but ships a `.pem`; `collect_data_files`.
- `pydantic` / `pydantic-core` — generally OK on recent PyInstaller; pin both.
- ⚠️ `selenium` + `undetected-chromedriver` — the hard one. See Part 3.

### 1.4 Config & data location anchoring (partly done)
- ✅ `.env` resolved via `paths.env_file()` (absolute, beside exe).
- ☐ **DB path** still defaults to CWD-relative `sqlite:///./data/...`. Anchor
  it to `app_dir()/data` (Part 2). Because `alembic/env.py` reads the URL
  from `app.config.settings` (per `alembic.ini`), fixing it in settings fixes
  migrations too — one change, both paths.
- ☐ **Image cache** (`IMAGES_ROOT`) → `app_dir()/data/images`.

---

## Part 2 — Path & config anchoring (small code sprint, can do now)

Concrete edits; low risk, unblocks a clean build.

### Task 2.1 — Anchor the SQLite DB to `app_dir()`
- In `config.py`, change `database_url` default to a value computed from
  `app_dir()/data/tag_inventory.db` (instead of `./data/...`). Keep the
  `DATABASE_URL` env override (tests rely on it).
- Ensure `app_dir()/data/` is created at startup (db engine factory or
  lifespan) before the engine connects.
- Verify `alembic/env.py` picks up the same settings URL (it already imports
  `app.config.settings`).

### Task 2.2 — Anchor the image cache
- `IMAGES_ROOT` → `app_dir()/data/images`. It's already `mkdir`'d at app
  startup in `main.py`; just move the root.

### Task 2.3 — Persistence-location decision (IMPORTANT — see Open Decisions)
A one-folder upgrade = replace the bundle directory. If `.env` and `data/`
live *inside* that directory, an upgrade **wipes the operator's credentials
and database**. Decide the persistence root before shipping:
- **Recommended:** introduce a `TAG_HOME` env override in `paths.app_dir()`.
  Default to exe dir for dev simplicity, but on the shop PC point `TAG_HOME`
  at a stable folder *outside* the versioned bundle (e.g.
  `C:\TAGData\` or `%PROGRAMDATA%\TAG\`). Upgrades swap the bundle; data
  survives.
- This is a one-line change in `app_dir()` now; retrofitting it after data
  exists is a migration headache. Do it in this task.

---

## Part 3 — The chromedriver problem (de-risk EARLY, not at ship time)

`undetected-chromedriver` is the highest-risk item in the whole bundle. It
patches a chromedriver binary **at runtime**, which assumes network access, a
writable working dir, and a layout PyInstaller's frozen environment doesn't
naturally provide. This can fail in ways that are invisible until the bundle
runs on a machine without dev tooling.

### Task 3.1 — Spike: attempt to freeze the portal downloader NOW
Build a minimal one-folder bundle of *just* `app/sync/tcgplayer/portal_*`
plus its deps and run it. Determine which failure mode we're in before
committing to an approach.

### Task 3.2 — Pick a strategy based on the spike
Options, roughly in order of preference:
1. **Require a system browser, pin & bundle a matching chromedriver, bypass
   UC's auto-patch.** The code already has `find_browser_executable()`
   (Chrome/Edge). Ship a known-good `chromedriver`/`msedgedriver` in the
   bundle and point Selenium at it directly, skipping UC's download/patch
   step in frozen mode.
2. **Keep UC but give it a writable, persistent driver cache** under
   `app_dir()/data/drivers` and pre-seed it so first run doesn't need to
   download.
3. **Drop automated portal download from the shipped build.** The scheduler
   already falls back to a manually-placed CSV (`data/csv/...`), and the
   Admin UI has a manual "Get from TCGPlayer Portal" button intended for a
   dev/attended context. If headless portal download proves un-freezable, the
   shipped binary can rely on the manual CSV drop + the email receiver, and
   portal automation stays a dev-only convenience. **Lowest risk; confirm
   with stakeholder whether unattended CSV refresh is a hard requirement.**

The choice may influence other decisions (driver cache location, whether
Cloudflare's bot-gate even matters at ship time), which is why it's early.

---

## Part 4 — Database & migrations in frozen mode

### Task 4.1 — Bundle migration assets
`alembic/` (the `versions/` chain + `env.py`) and `alembic.ini` must be added
to the spec's `datas`. They are read-only → resolve via `resource_root()`.

### Task 4.2 — Run migrations at startup (replace the manual README step)
There's no shell on the shop PC to run `alembic upgrade head`. Add a startup
step (in `run` / lifespan, before serving) that calls Alembic's Python API
(`alembic.command.upgrade(cfg, "head")`) with a `Config` whose
`script_location` points at the **bundled** `alembic/` via `resource_root()`
and whose URL comes from settings. Idempotent: on an up-to-date DB it's a
no-op; on a fresh install it creates and migrates.

### Task 4.3 — First-run vs upgrade
- Fresh install: empty `data/` → migrations create the DB.
- Upgrade: existing DB in the persistent `TAG_HOME` → migrations apply new
  revisions only. (Reinforces why Task 2.3 matters.)

---

## Part 5 — The PyInstaller spec & entry point

### Task 5.1 — Dedicated entry script
Add `run_app.py` at repo root that: (1) runs migrations (Task 4.2), (2) calls
`uvicorn.run(app, ...)`. PyInstaller targets this, not `app/main.py`, so the
frozen entry path is explicit and migration-before-serve is guaranteed.

### Task 5.2 — `tag-inventory.spec`
- `datas`: `app/templates`, `app/static`, `alembic/`, `alembic.ini`, the
  pinned chromedriver (if Part 3 option 1/2), `imapclient` PEM.
- `hiddenimports` + `collect_submodules`: uvicorn (+ loops/protocols),
  apscheduler, sqlalchemy sqlite dialect, imapclient, pydantic. Expand from
  the spike's failures.
- `console=True` initially (we want logs while shaking it out); revisit.
- One-folder (`COLLECT`), name `tag-inventory`.
- Pin the **PyInstaller version** in dev deps; build on a Windows machine
  whose arch matches the shop PC (no cross-build).

### Task 5.3 — Build script
`tools/build_bundle.py` (or a `.ps1`): clean `build/`+`dist/`, run
PyInstaller against the spec, then run the **bundle smoke test** (5.4), then
zip `dist/tag-inventory/`. Stamp the version (from `app.__version__`) into the
zip name.

### Task 5.4 — Bundle smoke test
Automated post-build check that launches the **frozen exe** (not the source),
waits for `/healthz` to return 200, hits `/admin`, confirms the DB file
appeared in the persistent dir, and shuts it down. This is the gate that
proves the *bundle* works, distinct from `pytest` proving the *source* works.

---

## Part 6 — Runtime & ops stack (at/after feature-complete)

### Task 6.1 — Windows service (NSSM)
Wrap `tag-inventory.exe` as an auto-start service. Capture stdout/stderr to a
rotating log under the persistent dir. Document install/uninstall/restart.

### Task 6.2 — HTTPS (direct uvicorn TLS — Caddy DEFERRED)
**Decided 2026-06-14:** no reverse proxy. uvicorn serves TLS directly
(`SSL_ENABLED=true`, default), auto-generating a self-signed cert under
`TAG_HOME/certs` on first run (`app/tls.py`). One process, no Caddy binary,
no Caddyfile. The tablet hits `https://<host>:8000`.
- **One-time tablet step:** install `TAG_HOME/certs/tag-cert.pem` as a trusted
  cert on the staff tablet → clean padlock. Skip it → a browser warning each
  visit (still encrypted, just untrusted).
- **Firewall** opens the HTTPS port (8000) — the existing rule already does.
- **Watchdog/health** (§3 of the reliability plan) must poll
  `https://127.0.0.1:8000/healthz` with cert verification **disabled** (the
  self-signed cert isn't in the machine trust store).
- **Caddy is the escalation path**, not the default: add it later only if you
  need a public hostname, real (Let's Encrypt) certs, or Basic Auth — none of
  which apply to the current isolated-LAN deployment.

### Task 6.3 — Public ingress for Shopify webhooks (only if Phase 5 ships)
Cloudflare Tunnel or Tailscale Funnel exposing just the webhook path. Coordin-
ate with the Shopify webhook secret already in settings. Skip entirely if the
shipped build doesn't include Shopify POS handoff.

### Task 6.4 — Backups (Litestream)
Litestream replicating the SQLite DB to a second disk / cloud bucket.
Configure against the persistent DB path. Document restore.

### Task 6.5 — Secrets at rest (decision)
Today `.env` sits in plaintext beside the exe — including
`GMAIL_APP_PASSWORD` and channel tokens. The repo already has DPAPI
(`app/security/dpapi.py`) and `set_secret_setting`/`get_secret_setting`.
Decide:
- **Keep `.env` plaintext** (single-operator PC, physical security) — simplest.
- **Move secrets into the DPAPI-encrypted settings store** via the Admin
  Settings UI, leaving `.env` for non-secret config only. Better at rest;
  more UI work. Recommended if the PC isn't physically controlled.

---

## Part 7 — Release & upgrade procedure

1. Bump `app.__version__`; commit.
2. Run `tools/build_bundle.py` → versioned zip (gated by bundle smoke test).
3. On shop PC: stop the service → unzip the new bundle over the **program**
   dir → leave `TAG_HOME` (`.env` + `data/`) untouched → start the service →
   confirm `/healthz`.
4. Rollback = stop service, restore previous bundle dir, restart. Data is
   forward/backward-safe only across migration-compatible versions; note any
   non-reversible migration in release notes.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `undetected-chromedriver` won't freeze | High | Med | Part 3 spike NOW; fallback = manual CSV + email receiver |
| Upgrade wipes `.env`/DB (data inside bundle) | High if unaddressed | High | `TAG_HOME` outside bundle (Task 2.3) |
| Hidden-import gaps (uvicorn/apscheduler/sqlalchemy) | Med | Med | Early build + bundle smoke test; expand spec iteratively |
| CWD path reintroduced by a later phase | Med | Med | `paths.py` rule + grep guard (1.1–1.2) |
| Migrations don't run on fresh shop PC | Med | High | Programmatic `upgrade head` at startup (4.2) |
| Plaintext secrets beside exe | Low–Med | Med | DPAPI settings store option (6.5) |

---

## Open decisions (need answers before the packaging sprint)

1. **Persistence root:** ship with `TAG_HOME` pointed where? (`C:\TAGData`,
   `%PROGRAMDATA%\TAG`, or accept exe-dir + a careful upgrade ritual.)
2. **Unattended TCGPlayer CSV refresh:** hard requirement in the shipped
   build? If no, we can sidestep the chromedriver-freeze risk entirely.
3. **Secrets at rest:** plaintext `.env` acceptable, or move to DPAPI store?
4. **Which phases are in the *first* shipped binary?** (Determines whether
   Caddy public ingress / Shopify webhook / eBay polling must work in the
   bundle, or whether v1 ships TCGPlayer + email + POS only.)

---

## Definition of done

- [ ] `paths.py` rule documented; grep guard test green.
- [ ] DB + image cache anchored to `app_dir()`/`TAG_HOME`; `.env` already is.
- [ ] Chromedriver strategy chosen and proven by spike.
- [ ] Migrations run automatically at startup in frozen mode.
- [ ] `tag-inventory.spec` builds a one-folder bundle on a clean Windows box.
- [ ] Bundle smoke test passes against the **frozen exe**.
- [ ] NSSM service + direct-uvicorn HTTPS working on the shop PC; tablet cert installed.
- [ ] Litestream backup configured; restore tested once.
- [ ] Release + upgrade runbook written; one practice upgrade performed.
- [ ] All four open decisions resolved and recorded here.

---

## Suggested sequencing given "development not done"

- **Now, alongside features:** Part 1 (hygiene + guard), Part 2 (anchoring),
  and the **Part 3 spike**. These are cheap, and the spike de-risks the scary
  part while there's slack.
- **Mid-development checkpoint:** one throwaway end-to-end build (Parts 4–5)
  to flush out hidden-import gaps early. Throw the artifact away; keep the
  spec.
- **At feature-complete:** finalize Parts 4–5, then Part 6 ops stack and
  Part 7 runbook. Resolve open decisions before this sprint starts.
