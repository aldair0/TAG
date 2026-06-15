# Holistic Code Review — Security & Fragility

Whole-codebase review (not just the recent diff), conducted across five
subsystems: secrets/subprocess, web surface/auth, data layer/concurrency,
external integrations/parsing, and the new reliability code. Each finding was
verified against the actual source. Severity is impact-if-exploited-or-hit;
**Origin** notes whether it's pre-existing code or code added this session.

**Headline:** two genuinely serious *security* issues in the Shopify OAuth
flow (SSRF + HMAC bypass), an SSRF + unbounded download in image fetching, and
a handful of *fragility* bugs — including four in the just-written reliability
code that are worth fixing before any soak test.

---

## Critical / High — Security

### S1. Shopify OAuth callback: SSRF + secret exfiltration via unvalidated `shop` (High)
`app/routes/shopify_auth.py:64-88,116-120` — the `shop` query param is
interpolated directly into both a browser redirect
(`https://{shop}/admin/oauth/authorize` → **open redirect**) and a
*server-side* `httpx.post("https://{shop}/admin/oauth/access_token", ...)`.
The server POSTs `client_id` + `client_secret` + `code` to an
attacker-controlled host → **SSRF that leaks the Shopify app secret and the
OAuth code**. No `*.myshopify.com` allow-list exists.
**Fix:** validate `shop` against `^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$`
in both routes; reject otherwise.

### S2. Shopify OAuth callback: HMAC verification bypass (High)
`app/routes/shopify_auth.py:107-111` — on HMAC mismatch it logs *"proceeding
anyway (dev mode)"* and continues to the token exchange; and if no secret is
configured the check is skipped entirely. Combined with S1 this lets a forged
callback drive the flow.
**Fix:** reject on mismatch; require the secret to be present; don't reuse the
`state` nonce across attempts.

### S3. Image fetch: SSRF + unbounded download (High)
`app/sync/tcgplayer/images.py:26,41,49,62` — `fetch_if_missing` fetches a URL
taken straight from the untrusted CSV `Photo URL`
(`parser.py:127 → IngestRow.image_url`), with `follow_redirects=True`, **no
scheme/host allow-list** (SSRF to `169.254.169.254`, internal hosts), and
`r.content` buffered with **no size cap** (memory exhaustion).
**Fix:** require `https` + allow-list the TCGPlayer CDN host; disable/re-validate
redirects; stream with a byte cap and reject non-image `Content-Type`.

---

## High — Fragility (new reliability code — fix before soak)

### F1. Receiver can permanently skip a real sale email (High)
`app/inbound_email/receiver.py:228-236` — `_drain_new` advances and persists
the UID watermark even for a UID whose `RFC822` body came back empty (a
transient fetch miss). That sale email is **never retried** — silent loss.
**Fix:** only advance/persist the watermark for UIDs actually fetched *and*
processed; re-fetch (or don't advance past) an empty body.

### F2. Backup uses a raw connection with no `busy_timeout` (Medium→High)
`app/backup.py:71` — the online backup opens its own `sqlite3.connect(src)`
that does **not** get the engine's `busy_timeout=5000` pragma (default is 0).
On a busy WAL DB the nightly backup can hit "database is locked" immediately
and fail — exactly when durability matters.
**Fix:** `src_con.execute("PRAGMA busy_timeout=5000")` before `.backup()`.

### F3. Malformed `BACKUP_TIME` aborts the *entire* scheduler (Medium→High)
`app/scheduler.py` (`_parse_hhmm` in `start_scheduler`) — a bad value like
`"25:00"` raises during startup and takes down **all** scheduler jobs
(auto-sync, WAL checkpoint, disk guard), not just the backup.
**Fix:** parse defensively; skip only the backup job on bad input.

### F4. Retention `0`/negative can delete the just-written backup (Medium)
`app/backup.py:97-105` — `prune_old` has no floor on `retention_days`; `0`
makes `cutoff = now` and can delete the backup created moments earlier in the
same call.
**Fix:** clamp `retention_days >= 1` and exclude the just-written file from prune.

---

## Medium — Fragility

### F5. Email-confirm vs CSV-apply double-decrement race (Medium)
`app/sync/tcgplayer/apply.py:132` + `app/inbound_email/flagger.py` — the
sold-online guard reads `unit.is_sold_online` in the scheduler thread's
transaction; the flag is written by the receiver thread. `busy_timeout`
serializes *writes*, not the read-decide-write, so the CSV path can decide
"not flagged" then decrement while the flag commits concurrently. The flag's
POS-block still works, but the just-added preservation guarantee has a TOCTOU
hole.
**Fix:** fold the flag check into the guarded UPDATE
(`... WHERE sold_online_until IS NULL`) or add an optimistic-version re-check.

### F6. `record_sale` idempotency raises instead of dedup under a race (Medium)
`app/sales/recorder.py:98-109` — **Correction to the raw finding:** a unique
constraint *does* exist (`Sale.external_order_id`, `unique=True`,
`sale.py:43`), so a concurrent duplicate insert is prevented at the DB level —
it won't double-decrement. But the code relies on a pre-read and does **not**
catch the resulting `IntegrityError`, so the second concurrent caller raises
uncaught.
**Fix:** catch `IntegrityError` and return the existing sale (treat the unique
constraint as the source of truth, not the pre-read).

### F7. eBay SKU cast crashes the whole poll (Medium)
`app/sync/ebay/inbound.py:91` — `int(li.sku)` on an external SKU isn't guarded;
a non-numeric SKU raises `ValueError` that isn't caught in the per-order loop,
aborting the batch and every later order. (Lower live risk: the real eBay
client is currently a `NotImplementedError` stub.)
**Fix:** try/except the cast → open a `listing_not_found` conflict or skip.

### F8. Email-parser ReDoS / unbounded body (Medium)
`app/sync/tcgplayer/email_parser.py:135-176` — lazy-dot regexes over an
unbounded body, plus `\d+`/`.*?` on attacker-influenceable HTML (From headers
are spoofable; no DKIM check), risk pathological backtracking pinning a thread.
**Fix:** cap body size before regex; bound the gap pattern and `\d{1,4}`.

### F9. Shopify client: weak retry + unclamped `Retry-After` (Medium)
`app/sync/shopify/client.py:121-134` — retries a 429 once, no backoff, no retry
on 5xx/network; `Retry-After` is `float()`-parsed with no cap, so a hostile
`Retry-After: 99999` `time.sleep`s the thread for ~27 hours.
**Fix:** bounded exponential backoff for 429/5xx/network; clamp `Retry-After`.

### F10. Sync service final commit outside try (Medium)
`app/sync/tcgplayer/service.py:173-184` — the final `run.ended_at` /
`session.commit()` is outside the try, so a commit error (lock/IntegrityError)
propagates uncaught.
**Fix:** move the final commit inside the guarded block.

### F11. Reaper `taskkill /IM` is system-wide (Medium)
`app/maintenance.py:36-45` — kills *all* `chromedriver.exe` etc. on the box, not
just this app's orphans. Safe on a dedicated laptop (and never touches
`chrome.exe`), but would kill another tool's driver.
**Fix:** scope by PID/parent-PID, or document the box must be dedicated.

---

## Low — Defense-in-depth & hardening

- **A1 (Med-ish, by deployment):** No auth + no CSRF on any state-changing POST
  (`/admin/sold-online/signal`, `/admin/settings/network/firewall` which
  RunAs-elevates PowerShell, sync triggers, checkout, inventory delete). By
  design (single-user LAN), but any LAN device or drive-by form can trigger
  sales/inventory deletion/firewall changes. `app/routes/sold_online.py`,
  `settings.py`, `sync.py`, `main.py`. **Fix:** shared-secret/`X-Signal-Token`
  on `/signal`; CSRF tokens on forms; consider binding admin to loopback +
  reverse proxy if the LAN is untrusted.
- **A2:** Hardcoded TCGPlayer seller key/id default in `config.py:66-68` (public
  marketplace identifier, low value) — move to `.env`.
- **A3:** Non-Windows secret persistence falls back to **plaintext** in the DB
  (`settings_store.py:84-87`) for the TCGPlayer auth ticket — fine on the
  Windows target; refuse-or-warn elsewhere.
- **A4:** Alert throttle dict (`alerts.py:_last_sent`) never evicts — unbounded
  if a caller passes a dynamic `subject` with no stable `key`. All current
  callers pass stable keys. **Fix:** require `key` or cap/evict.
- **A5:** `_hot_copy` `.part` temp name collides if two backups run in the same
  second (manual + scheduled). **Fix:** unique temp name (`mkstemp`).
- **A6:** Shopify order parse (`client.py:419-444`) trusts `Decimal(...)`/`id`
  without guards; one malformed order crashes the page. **Fix:** guard casts.
- **A7:** Info disclosure: `sync.py` returns raw exception text to the client
  (`run_ebay`), writes tracebacks to `data/diagnostics/`. **Fix:** generic
  client messages; scrub diagnostics.
- **A8:** Receiver/scheduler status fields read cross-thread without a lock
  (`receiver.py`, `scheduler.py`) — GIL-atomic, benign; snapshot under a lock
  for correctness.
- **A9:** `images.py:64` / `marketplace_search.py:185` broad `except` and
  `int(...)` casts can swallow/abort on one malformed API hit. **Fix:** narrow.

---

## Verified SAFE (checked, not vulnerable)

- **No SQL injection** — SQLAlchemy ORM with bound params throughout; `ilike(f"%{q}%")` is parameterized.
- **No XSS** — Jinja2 autoescaping intact; **no** `|safe`/`Markup`/disabled-autoescape anywhere; external card names render escaped.
- **No `shell=True`** anywhere; all `subprocess`/PowerShell use list-argv and interpolate only constants (the firewall rule name + port 8000 are module constants — **not** user input), so no command injection.
- **No secret leakage** in `/healthz`, logs, or alert bodies — the app password / tokens never appear in the payload or alert text.
- **No Shopify webhook receiver exists yet** — the missing-HMAC-on-webhook risk is moot today (only the OAuth callback HMAC, flagged S2).
- **Sessions are never shared across threads** — receiver, scheduler, and web each open their own `SessionLocal()`; `check_same_thread=False` is correct usage.
- **`_atomic_decrement`** guarded `UPDATE ... WHERE qty >= :n` is genuinely atomic and prevents single-row oversell.
- **All live HTTP clients set timeouts** (10–15s); the CSV parser wraps every cast in typed `ParseError`.
- **Scheduler maintenance jobs** all wrap bodies in try/except — a job failure can't kill the APScheduler thread.

---

## Suggested fix order

1. **S1–S3** (security: Shopify SSRF/HMAC, image SSRF) — real external risk.
2. **F1–F4** (new reliability-code bugs) — fix before the soak, since the soak
   validates exactly this code.
3. **F5–F11** (fragility) — as a batch.
4. **A1** (auth/CSRF) — decide based on how trusted the shop LAN is.
5. **A2–A9** — opportunistic hardening.
