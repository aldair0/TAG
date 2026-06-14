# Phase 1 Implementation Plan — TCGPlayer Ingestion (mock-data mode)

**Goal:** A working CSV-diff ingestion path that reads from a TCGPlayer-shaped CSV, populates the local DB (products + inventory_units + channel_listing[tcgplayer]), and surfaces inventory in the Admin UI. Run manually via "Run sync now" — APScheduler timing is wired in a later step.

**Mock-data mode:** the live CSV download endpoint isn't reachable until the shop owner provides credentials. We abstract the source behind a `TCGPlayerSource` interface; the dev/test implementation reads a synthetic CSV from `test_data/`. The real implementation arrives when creds do — interface stays the same.

---

## Scope

### In
- DB schema for `product`, `inventory_unit`, `channel_listing` (other tables added in later phases)
- Real Alembic migration replacing the empty 0001
- Synthetic CSV fixture (12 cards across 2 sets + 3 sealed products, mirroring the documented TCGPlayer column layout)
- `TCGPlayerSource` abstract base + `FixtureTCGPlayerSource` (dev) + stub for `LiveTCGPlayerSource` (raises NotImplementedError until creds arrive)
- CSV parser that converts a row into a normalized `IngestRow` dataclass
- Diff engine: compares parsed rows against current DB state; produces `IngestPlan` (creates / qty-changes / price-changes)
- Apply step: runs the plan inside a single transaction
- Image fetcher with local cache under `data/images/` and an `httpx`-based stub that's mockable in tests
- Admin UI:
  - `/admin/inventory` — paginated table, image thumb, name, set, condition, qty, price
  - `/admin/sync` — list of recent sync_runs; "Run sync now" button (HTMX POST → kicks off ingest, returns updated row)
- Tests covering parser, diff engine, apply step, full happy path

### Out (Phase 2+)
- eBay / Shopify integration
- Outbound CSV upload to TCGPlayer
- Sale detection (qty decrease as a sale)
- Scheduled background runs (APScheduler) — manually triggered for now
- Conflict resolution UI

---

## Schema (this phase)

Three tables. Sale/sync_run/conflict/outbound_change come in Phase 2/3 — only `sync_run` is added here so the Admin UI can show sync history.

```
product
  id PK
  tcgplayer_product_id INTEGER UNIQUE NULL
  shopify_product_id BIGINT UNIQUE NULL          -- populated in Phase 2
  shopify_variant_id BIGINT NULL                  -- populated in Phase 2
  kind TEXT CHECK (kind IN ('single','sealed','supply'))
  name TEXT NOT NULL
  set TEXT NULL
  description TEXT NULL
  image_url TEXT NULL
  language TEXT DEFAULT 'English'
  is_foil BOOLEAN DEFAULT 0
  rarity TEXT NULL
  card_type TEXT NULL
  sealed_subtype TEXT NULL
  supply_category TEXT NULL
  is_online_listable BOOLEAN DEFAULT 1
  created_at, updated_at TIMESTAMP

inventory_unit
  id PK
  product_id FK product
  condition TEXT NULL                             -- NM/LP/MP/HP/DMG, NULL for sealed/supply
  quantity_on_hand INTEGER NOT NULL DEFAULT 0
  unit_price NUMERIC NULL
  last_local_edit_at TIMESTAMP NULL
  created_at, updated_at TIMESTAMP
  UNIQUE(product_id, condition)

channel_listing
  id PK
  inventory_unit_id FK inventory_unit
  channel TEXT CHECK (channel IN ('tcgplayer','ebay','shopify_pos'))
  external_listing_id TEXT NULL                    -- TCGplayer id, eBay sku, Shopify variant id
  last_pushed_quantity INTEGER NULL
  last_pushed_price NUMERIC NULL
  last_synced_at TIMESTAMP NULL
  sync_state TEXT DEFAULT 'pending'                -- 'ok' | 'pending' | 'error'
  last_push_id TEXT NULL                           -- echo-detection token (Phase 2)
  UNIQUE(inventory_unit_id, channel)

sync_run
  id PK
  worker TEXT                                       -- 'tcgplayer'
  direction TEXT                                    -- 'inbound'
  started_at, ended_at TIMESTAMP
  rows_seen INTEGER DEFAULT 0
  rows_inserted INTEGER DEFAULT 0
  rows_updated INTEGER DEFAULT 0
  error TEXT NULL
```

---

## Component layout

```
app/
  db/models/
    __init__.py            # imports submodules so Alembic autogenerate sees them
    product.py
    inventory_unit.py
    channel_listing.py
    sync_run.py
  sync/
    __init__.py
    tcgplayer/
      __init__.py
      source.py            # TCGPlayerSource ABC + FixtureTCGPlayerSource + LiveTCGPlayerSource stub
      parser.py            # CSV row → IngestRow
      diff.py              # current DB state vs incoming rows → IngestPlan
      apply.py             # applies an IngestPlan in a transaction
      service.py           # high-level run_ingest() entry point
      images.py            # image fetcher + cache
  routes/
    inventory.py           # /admin/inventory
    sync.py                # /admin/sync (+ POST /admin/sync/run)
  templates/admin/
    inventory.html
    sync.html
test_data/
  tcgplayer_fixture.csv     # the synthetic dataset
tests/
  test_tcgplayer_parser.py
  test_tcgplayer_diff.py
  test_tcgplayer_ingest.py  # full happy-path
  test_inventory_routes.py
```

---

## Tasks (compact)

1. **Models + migration** — write the four model classes; `alembic revision --autogenerate -m "phase 1: products + inventory + channel_listing + sync_run"`; verify SQL; `upgrade head`.
2. **Synthetic fixture** — hand-write `test_data/tcgplayer_fixture.csv` (committed; `.gitignore` excludes `test_data/`, but we'll allow this synthetic one explicitly).
3. **Source abstraction** — `TCGPlayerSource.fetch() -> Iterable[dict]`; `FixtureTCGPlayerSource(path)` reads CSV; `LiveTCGPlayerSource` raises NotImplementedError with a clear message about credentials.
4. **Parser** — CSV row → `IngestRow` dataclass with normalized fields.
5. **Diff engine** — pure function: `(current_rows, incoming_rows) → IngestPlan(creates, qty_updates, price_updates)`.
6. **Apply step** — takes a session + plan; runs creates/updates atomically; records sync_run.
7. **Image fetcher** — given a TCGplayer id, fetches image to `data/images/<id>.jpg` if not present; returns the local path. In tests, use an `httpx.MockTransport`.
8. **Admin routes** — inventory list (paginated), sync history list, "Run sync now" button (POST → calls `service.run_ingest()` → redirects back).
9. **Templates** — `inventory.html` (table with thumbnails), `sync.html` (history table + run button).
10. **Tests** — parser, diff, ingest happy-path, route smoke.
11. **Verify end-to-end** — `pytest`, `alembic upgrade head`, manual run-sync from the UI, see fixture data appear in `/admin/inventory`.
12. **Commit.**

---

## Definition of done

- [ ] DB has product / inventory_unit / channel_listing / sync_run tables (verified via `alembic upgrade head`)
- [ ] `pytest` passes (existing 3 + new tests)
- [ ] After running sync via the UI, `/admin/inventory` shows the 12 cards + 3 sealed from the fixture, with thumbnails
- [ ] `/admin/sync` shows a sync_run record for the run
- [ ] Re-running sync produces no changes (idempotent)
- [ ] Editing the fixture CSV (e.g., dropping qty on one row) and re-running shows the qty change in the inventory list
- [ ] Adding a new row to the fixture and re-running shows the new product
- [ ] Live source raises a clear "credentials not configured" error if invoked
