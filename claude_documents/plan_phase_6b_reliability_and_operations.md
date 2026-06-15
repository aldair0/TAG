# Phase 6b Plan — Reliability, Self-Healing & Operations

**Companion to** [plan_phase_6_binary_packaging.md](plan_phase_6_binary_packaging.md). That
doc covers *how to build and ship the binary* (PyInstaller spec, paths,
migrations, NSSM, direct-uvicorn HTTPS, Litestream). **This** doc covers *how to keep it
alive, recoverable, and debuggable* once it's running unattended on the shop
PC — the operational half.

**Framing:** the shop PC will run this **24/7 for weeks at a time**, attended
only by non-technical staff. The failures that matter here are not crashes on
startup (you'd catch those) — they're the **slow ones that only surface after
days of uptime**: leaked connections, unbounded files, expired tokens,
zombie browser processes, a silently-dead socket. The centerpiece of this
plan is the **soak failure-mode register** (§2), written against this app's
actual components.

**A hard constraint that shapes everything:** the app has **no outbound
email** (the receiver is inbound-only). *It cannot notify anyone when it
breaks.* Every failure path must therefore degrade to either (a) a visible
on-screen status the staff/owner can see, or (b) automatic self-recovery.
"It'll email me" is not available.

---

## 1. How the five asks map out

| Ask | Where addressed |
|---|---|
| Turn into a binary | Packaging plan Parts 3–5 (PyInstaller one-folder, spec, migrations-at-startup) |
| Auto-start on boot | §5 here + packaging plan 6.1 (NSSM auto-start service) |
| Self-healing | §3 here (five recovery layers) |
| Durability | §4 here (SQLite hardening, backups, config survival) |
| Logs for debugging | §6 here (rotation, levels, health surface) |
| 2-week soak fail points | §2 here (the register) + §7 (soak test) |

---

## 2. Soak failure-mode register (left on 2 weeks straight)

Grouped by subsystem. **Sev** = impact if unhandled. **Today** = current
behavior. **Plan** = the mitigation to build.

### 2.1 The IMAP receiver — our biggest new long-uptime risk

It holds one long-lived socket and is the component most exposed to "works
for days, then silently stops."

| # | Failure | Sev | Today | Plan |
|---|---|---|---|---|
| R1 | **Silently-dead IDLE socket** — NAT/router drops the connection without a FIN; `idle_check` keeps returning empty, no exception ever raised. Receiver looks alive but receives nothing. | **High** | Reconnects only on *exception*; a half-open socket throws none → silent stall | Add an **active liveness probe**: bounded IDLE cycles, and every N minutes drop IDLE and issue a `NOOP`/re-`SELECT`; track `last_server_contact`; if it exceeds a threshold, force a full reconnect. Don't trust "no exception" as "healthy." |
| R2 | **App password revoked** (owner changes Google password, security event) | High | Infinite reconnect-with-backoff loop, no visible signal | Surface receiver auth state on the health page (§6); after K failed auths, set a prominent "Email receiver DOWN — re-enter app password" banner |
| R3 | **Connection/fd leak** across hundreds of reconnects over 2 weeks | Med | `logout()` in a `finally`, but a wedged socket may not close cleanly | Ensure hard socket close on every reconnect path; cap and log reconnect count; periodic self-restart of the receiver thread as a backstop |
| R4 | **Gmail simultaneous-connection limit** (≤15) hit by leaked/duplicate connections | Med | One connection by design; a leak (R3) or a stray dev watcher could trip it | Single-owner connection invariant; on "too many connections" error, back off long and reconnect once |
| R5 | **Poison email** (malformed body) crashes processing repeatedly | Low | `_process_one` catches parse errors and **advances the watermark anyway** → no repro loop ✅ | Already safe; keep the per-message watermark advance. Add a "skipped/unparseable" counter to the health page |

### 2.2 The TCGplayer scheduler + headless browser

| # | Failure | Sev | Today | Plan |
|---|---|---|---|---|
| B1 | **Zombie `chrome.exe` processes** accumulate from undetected-chromedriver over many runs → memory exhaustion | **High** | No explicit reaper | Guarantee driver `quit()` in `finally`; add a periodic orphaned-Chrome reaper (kill chrome procs older than X with our marker); cap concurrency to 1 (already) |
| B2 | **Chrome auto-updates** on the shop PC → chromedriver version skew → portal download silently fails | Med | Falls back to cached CSV ✅ (degrades, doesn't crash) | Detect the failure, surface "TCGplayer auto-download failing" on health page; pin/refresh driver (ties to packaging Part 3 decision) |
| B3 | **TCGplayer portal session expires** → headless login fails | Med | Falls back to cached CSV ✅ | Surface "re-auth needed" status; the manual portal button stays the recovery path |
| B4 | **Selenium memory growth** over a 2-week run | Low | Process is short-lived per run (spawned per tick) → naturally bounded ✅ | Confirm the per-run spawn model; no persistent browser |

### 2.3 SQLite & durability

| # | Failure | Sev | Today | Plan |
|---|---|---|---|---|
| D1 | **`database is locked`** — receiver thread + scheduler thread + web requests all write; SQLite is single-writer | **High** | WAL on, but **no `busy_timeout`** → a contended write fails immediately instead of waiting | Add `PRAGMA busy_timeout=5000` in the connect hook (one line, do now — §8) |
| D2 | **WAL file grows unbounded** if a long-lived connection blocks checkpointing → disk bloat | Med | `synchronous=NORMAL`, auto-checkpoint default; long-lived threads may starve it | Periodic `PRAGMA wal_checkpoint(TRUNCATE)` on a timer; keep connections short-lived |
| D3 | **Disk full** (WAL + logs + image cache + Litestream) → all writes fail | **High** | No disk monitoring; image cache uncapped | Disk-space check on the health page + log warning under a threshold; cap/prune the image cache; rotate logs (§6) |
| D4 | **Corruption from unclean shutdown** (power loss) | Med | WAL + `synchronous=NORMAL` gives crash-consistency (loses at most the last txn) | Acceptable for this workload; pair with Litestream (§4) for point-in-time restore; run `PRAGMA integrity_check` at startup, log result |
| D5 | **Backup absent** → a disk failure loses all inventory/sales history | **High** | None yet | Litestream continuous replication to a 2nd disk/cloud (packaging 6.4); **test the restore** |

### 2.4 Process / OS / Windows

| # | Failure | Sev | Today | Plan |
|---|---|---|---|---|
| O1 | **Windows Update forced reboot** mid-operation | **High** | — | NSSM auto-start (§5); boot-time **catch-up** (migrations + receiver resumes from watermark → missed sale emails processed) |
| O2 | **PC sleeps/hibernates** → IDLE dies, scheduler pauses, **system clock jumps** on wake | **High** | — | Set the shop PC to **never sleep**, high-performance power plan, disable USB selective suspend; document as a setup step |
| O3 | **Service crash / unhandled exception kills the process** | High | uvicorn + threads; an uncaught error in a worker thread could kill it | NSSM restart-on-failure with throttled backoff; wrap each worker loop so a thread crash restarts the *thread*, not the process (§3) |
| O4 | **Unsigned exe** flagged by SmartScreen/Defender; AV quarantines the exe or locks the DB file mid-scan | Med | — | Add AV exclusion for the install dir + DB; consider code-signing if SmartScreen nags; document |
| O5 | **DPAPI decrypt fails** if the Windows user/profile changes (secrets at rest) | Low | `settings_store` already degrades gracefully (treats as unset, logs) ✅ | Keep; document the "re-enter credentials" recovery |

### 2.5 Credentials / tokens expiring over calendar time

These don't care about uptime — they expire on wall-clock schedules and will
bite a long-running install eventually.

| # | Failure | Sev | Plan |
|---|---|---|---|
| C1 | **eBay refresh token** expires (~18 months) or access-token refresh fails | Med | Surface token health; document re-auth. Verify the refresh flow runs unattended |
| C2 | **Shopify token** revoked / app uninstalled | Med | Health-page indicator; re-auth runbook |
| C3 | **Gmail app password** revoked (C-level: same as R2) | High | Health banner + re-enter |
| C4 | **TCGplayer portal cookie** expires (frequent) | Low | Already degrades to cached CSV; manual re-auth button |

---

## 3. Self-healing — five recovery layers

Defense in depth: each layer catches what the one below it missed.

1. **OS / service** — NSSM runs the exe as an auto-start service with
   **restart-on-failure** (e.g. restart after 5s, throttle so a crash-loop
   doesn't spin). Survives reboots (O1) and hard crashes (O3).
2. **Process supervisor within the app** — the FastAPI lifespan owns the
   scheduler + receiver. Wrap each background worker's top loop so an
   unexpected exception **restarts that worker** (with backoff) instead of
   propagating and killing the process. (Receiver already reconnect-loops;
   generalize the pattern; ensure the scheduler thread is likewise guarded.)
3. **Subsystem liveness probes** — active health checks, not passive trust:
   - Receiver: `last_server_contact` heartbeat; force reconnect if stale (R1).
   - Scheduler: `last_tick_at`; if a tick hasn't run in 2× the interval during
     open hours, log + attempt a re-schedule.
   - DB: a cheap `SELECT 1` on the health check.
4. **Resource guards** (periodic timer jobs): WAL checkpoint (D2), orphaned-
   Chrome reaper (B1), log rotation (§6), disk-space check (D3), image-cache
   prune (D3).
5. **Idempotent boot catch-up** — on every start: run migrations, run
   `integrity_check`, receiver resumes from `imap_last_uid` (catches every
   sale email that arrived while down — already implemented), scheduler
   reconciles inventory. Downtime never loses data, it just defers it.

**Watchdog question (open decision):** layers 1–4 are *internal* — if the
whole process wedges (deadlock, not crash), nothing restarts it. Options: an
external tiny watchdog that polls `/healthz` and bounces the NSSM service on
repeated failure, or rely on NSSM's own health-check. Decide in §9.

---

## 4. Durability

- **SQLite hardening:** WAL ✅, `synchronous=NORMAL` ✅, **add `busy_timeout`**
  (D1), periodic checkpoint (D2), startup `integrity_check` (D4).
- **Continuous backup:** Litestream replicating the DB to a second local disk
  and/or a cloud bucket → point-in-time restore. **The restore must be tested
  once**, not assumed.
- **Config survival:** `.env` + `data/` live under `TAG_HOME`, *outside* the
  versioned bundle dir, so an app upgrade (folder swap) can't wipe credentials
  or the database (packaging Task 2.3). This is the single most important
  durability decision and must land before first deploy.
- **What's intentionally NOT durable:** the image cache (re-fetchable) and
  logs (rotated). Don't back those up.

---

## 5. Auto-start on boot

- NSSM service, **Automatic (Delayed Start)** so networking is up first.
- Service account with rights to the `TAG_HOME` dir and the install dir.
- Boot sequence inside the app: migrations → `integrity_check` → start
  scheduler + receiver → serve. Migrations-at-startup (packaging Task 4.2)
  makes a fresh boot self-sufficient with no shell.
- **HTTPS is served directly by uvicorn** (no Caddy) — `SSL_ENABLED` default,
  self-signed cert auto-generated under `TAG_HOME/certs` ([app/tls.py](../app/tls.py)). The
  existing firewall-rule helper ([settings.py](../app/routes/settings.py)) covers the port-8000 opening.
  The watchdog must poll `https://127.0.0.1:8000/healthz` with cert
  verification disabled.
- Verify the **catch-up path** (O1): kill power mid-run, reboot, confirm the
  receiver resumes from the watermark and processes the gap.

---

## 6. Logging & observability

Today: `logging.basicConfig(level=...)` → **stderr only, no file, no
rotation** ([main.py](../app/main.py)). For an unattended 2-week run this is the
biggest *debuggability* gap — when staff say "it stopped working Tuesday,"
there must be a log to read.

- **Rotating file logs** under `TAG_HOME/logs/` (size- or time-based,
  capped total size so they can't fill the disk — ties to D3). Keep stderr
  too (NSSM captures it).
- **Levels with intent:** INFO for the audit trail we already emit (receiver
  uid/order/flag actions, sales, sync runs), WARN for degraded-but-recovered
  (cached-CSV fallback, reconnects), ERROR for needs-a-human.
- **A human status page** (extend the existing Settings/health page): green/red
  for receiver connected + `last email seen`, last CSV sync time, scheduler
  alive, open conflicts count, DB size, free disk, token health (§2.5). This
  is the substitute for the alerting email the app can't send. One glance =
  "is it healthy?"
- **`/healthz` returns structured subsystem status** (not just `ok`) so an
  external watchdog (§3) or a phone bookmark can poll it.

---

## 7. The 2-week soak test (validate before shipping)

Don't ship and hope. Before the real deploy, run an **accelerated soak** on a
test box:
- Run the binary as the service for a continuous stretch (ideally days).
- **Inject the failures**: pull the network cable (R1/R3), kill `chrome.exe`
  mid-sync (B1), revoke the app password (R2), fill the disk to near-full
  (D3), force a reboot (O1), let it sleep then wake (O2), expire a token (C*).
- **Assert recovery** each time: does it reconnect, restart the thread, fall
  back, surface the right status, and resume from the watermark?
- Watch for **slow leaks**: chart process memory, fd/handle count, Chrome
  process count, WAL size, log dir size over the run. Flat = good; sawtooth-up
  = a leak to fix.

This is the test that actually answers "can it run 2 weeks?"

---

## 8. No-regret hardening to land NOW (small, during dev)

Cheap changes that de-risk the eventual deploy and cost little today:
1. **`PRAGMA busy_timeout=5000`** in the connect hook (D1) — one line in
   [base.py](../app/db/base.py).
2. **Rotating file log handler** under `TAG_HOME/logs/` (§6) — replaces the
   bare `basicConfig`.
3. **`TAG_HOME` anchor** in `paths.app_dir()` (packaging Task 2.3) so data/
   config never live inside the swappable bundle.
4. **Worker-loop crash guards** (§3 layer 2) — wrap the scheduler tick + the
   receiver loop so a thread error self-restarts.
5. **Structured `/healthz`** + a couple of `last_*` timestamps (receiver,
   scheduler) — the foundation the status page and watchdog build on.

Items 1–2 and 5 are tiny and worth doing in the current dev cycle, not at
packaging time, because they also make *development* debugging better.

---

## 9. Open decisions

1. **External watchdog or not?** (Whole-process wedge recovery — §3.) Tiny
   poller that bounces the service, or trust NSSM health checks?
2. **Backup target** for Litestream: second local disk, a NAS, or a cloud
   bucket? (Determines restore speed + offsite safety.)
3. **Status visibility:** is the on-screen health page enough, or do we want a
   *second* alert channel (e.g. a webhook to the owner's phone / a Telegram
   bot) given the app can't email? Recommended at least to consider for C3/R2.
4. **Soak duration** acceptable before sign-off: a real 14-day run, or an
   accelerated few-day run with injected faults?

---

## Definition of done (operational readiness)

- [ ] `busy_timeout`, periodic WAL checkpoint, startup `integrity_check` in place.
- [ ] Rotating file logs under `TAG_HOME/logs`, size-capped.
- [ ] Receiver active-liveness probe (catches the silent-dead socket).
- [ ] Worker loops self-restart on crash; NSSM restarts the process on crash.
- [ ] Orphaned-Chrome reaper + disk-space + image-cache guards running on timers.
- [ ] Structured `/healthz` + human status page (receiver, sync, DB, disk, tokens).
- [ ] Litestream backup configured and a **restore rehearsed once**.
- [ ] Shop PC set to never-sleep; AV exclusions; service auto-start verified.
- [ ] Soak test passed with injected faults; no memory/fd/Chrome/WAL leak.
- [ ] All §9 decisions resolved.
```
