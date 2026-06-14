# Phase 2 Implementation Plan — eBay + Shopify Outbound (mock clients)

**Goal:** When inventory changes locally (Phase 1 ingest, future sales), changes are enqueued for the channels that need them and a worker drains the queue. Real network clients are stubbed; mock clients log every call so we can verify behavior end-to-end without credentials. Swapping in real clients later is a constructor injection.

## Scope

### In
- `outbound_change` table + migration
- `EbayClient` / `ShopifyClient` abstract bases
- `LoggingMockEbayClient` / `LoggingMockShopifyClient` (log + return deterministic IDs)
- `RealEbayClient` / `RealShopifyClient` stubs that raise NotImplementedError until creds wire up
- Outbound enqueue helpers (`enqueue_for_new_unit`, `enqueue_for_qty_change`, `enqueue_for_price_change`)
- Phase 1 `apply.py` modified to call enqueue helpers (so ingest naturally fans out)
- Outbound workers: `run_ebay_outbound(session, client)`, `run_shopify_outbound(session, client)`
- Admin UI:
  - `/admin/sync` adds "Run eBay outbound" + "Run Shopify outbound" buttons
  - `/admin/sync/outbound` page showing recent outbound_change rows with state
- Tests covering enqueue, workers, retry-on-failure, mock client recording

### Out (later phases)
- Real eBay/Shopify network calls (await credentials)
- Sale detection (Phase 3 inbound polling for eBay; CSV diff for tcgplayer sales already partially in place)
- Shopify draft orders + webhooks (Phase 5)
- TCGPlayer outbound CSV upload (Phase 3 — needs creds too)
- Echo-detection / push-id correlation (Phase 3 — only relevant once tcgplayer outbound exists)

## New schema

```
outbound_change
  id PK
  channel TEXT CHECK (channel IN ('tcgplayer','ebay','shopify_pos'))
  inventory_unit_id FK inventory_unit (nullable for end-of-life events on a deleted unit)
  action TEXT CHECK (action IN ('create','update_qty','update_price','end_listing'))
  payload JSON  -- {new_quantity, new_price, ...}
  enqueued_at, attempted_at NULLABLE, completed_at NULLABLE
  attempts INT DEFAULT 0
  last_error TEXT NULLABLE
  push_id TEXT  -- UUID for future echo detection
  created_at, updated_at
```

## Layout

```
app/
  sync/
    ebay/
      __init__.py
      client.py            # EbayClient ABC + RealEbayClient stub
      mock_client.py       # LoggingMockEbayClient
      outbound.py          # run_ebay_outbound(session, client)
    shopify/
      __init__.py
      client.py            # ShopifyClient ABC + RealShopifyClient stub
      mock_client.py       # LoggingMockShopifyClient
      outbound.py          # run_shopify_outbound(session, client)
  outbound/
    __init__.py
    enqueue.py             # enqueue_* helpers, called from Phase 1 apply.py
  db/models/
    outbound_change.py
  routes/
    sync.py                # add POST /admin/sync/run_ebay, /admin/sync/run_shopify, GET /admin/sync/outbound
  templates/admin/
    outbound.html
```

## Tests

- `test_outbound_enqueue.py` — Phase 1 ingest fans out; new units get create rows for ebay+shopify; supplies skip ebay; qty changes from tcg side don't enqueue tcg
- `test_ebay_mock_client.py` / `test_shopify_mock_client.py` — recording behavior
- `test_ebay_outbound.py` / `test_shopify_outbound.py` — worker happy-path + retry on failing client
- `test_admin_outbound_routes.py` — UI smokes

## Definition of done

- [ ] Migration adds `outbound_change`; existing tests still pass after migrate
- [ ] After Phase 1 ingest of the fixture: outbound_change has 12 ebay create rows (cards/sealed only) and 15 shopify_pos create rows (everything)
- [ ] Running ebay outbound updates each card/sealed channel_listing[ebay] with a deterministic mock listing id and sets sync_state=ok
- [ ] Running shopify outbound assigns mock shopify_product_id + variant_id, populates channel_listing[shopify_pos]
- [ ] Failing mock client → outbound_change row left with attempts++, last_error set, NOT marked completed
- [ ] Admin /sync page has both Run-now buttons; /sync/outbound lists pending/done/errored rows
- [ ] Tests pass
- [ ] First post-Phase-2 commit
