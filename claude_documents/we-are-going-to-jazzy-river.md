# TAG Inventory — System Architecture Plan

**Status:** Architecture & system design only. No code tasks in this document. Per-subsystem implementation plans will be written in follow-up sessions.

**Date:** 2026-04-26

---

## 1. Context

The card shop currently sells across three independent channels with no shared inventory state:

- **TCGPlayer storefront** — primary place where new product is entered (PRO Seller account).
- **eBay** — secondary listing channel for the same physical inventory.
- **In-person walk-ins** — handled today by **Shopify POS on a tablet**, which has no awareness of card-level inventory at all.

Without integration, staff must re-enter or manually reconcile each item across channels, walk-ins can sell cards that were already sold online (oversell), and Shopify POS cannot show staff what's actually available to sell at the counter.

**Goal of this system:** one local source of truth for inventory; automatic outbound sync to eBay **and TCGPlayer** when state changes; a tablet-friendly browse/cart UI for in-person sales that hands the final total off to Shopify POS for payment processing (card or cash).

**Outcome we're solving for:** a sale on **any** channel — TCGPlayer, eBay, or in-person Shopify POS — decrements the local DB and pushes the new quantity out to **every other channel** within minutes. The cashier can ring up a walk-in customer in under a minute by tapping cards into a cart on the same tablet that runs Shopify POS, and that walk-in sale automatically removes/decrements the same card from both TCGPlayer and eBay.

### 1.1 Source of Truth (load-bearing)

This is the single most important design point for the system; everything else follows from it:

- **The local DB is the source of truth for inventory state** (quantities on hand, prices, conditions) once a product is in it.
- **TCGPlayer is the source of truth for *initial product data*** only — the place new items get cataloged when they first enter the shop, because TCGPlayer's storefront UI is the most ergonomic place to enter a card (TCG database lookup, condition picker, image, etc.). We pull `name / set / number / image / initial price` from TCGPlayer the first time a TCGplayer ID appears in the seller CSV.
- **After initial ingestion, TCGPlayer is treated as a sink, exactly like eBay.** It receives quantity updates from the DB. It does NOT drive inventory state going forward; the only reason we still *read* TCGPlayer's CSV is to detect sales that happened on the TCGPlayer marketplace.
- **eBay is purely a sink + sale-source.** No product data flows from eBay into the DB; we publish to it.
- **Shopify POS is a sale-source AND a sink for everything (cards, sealed, supplies).**
  - All products in the DB are pushed to Shopify as catalog products, **published only to the in-store POS sales channel** (not the Shopify Online Store). The Shopify in-store inventory count therefore always mirrors our DB.
  - When a card sells *anywhere* — eBay, TCGPlayer, or in-store — the new quantity is pushed to Shopify so the in-store inventory stays accurate and the next cashier-side browse reflects reality.
  - The cashier can ring up *any* item via Shopify POS's native catalog browse, or via our POS UI (which creates Draft Orders that reference Shopify variant IDs as cataloged line items, not custom line items).
- **Non-card supplies (sleeves, deck boxes, dice, etc.)** are entered **directly into the local DB via the Admin UI** (no TCGPlayer ingestion path). From there:
  - The Shopify Sync Worker pushes them to Shopify Admin API as POS-channel-published products — the same mechanism used for cards/sealed.
  - They are **never pushed to TCGPlayer or eBay** — they don't exist online.
  - When sold (via either Shopify POS native browse or our POS UI handoff), the `orders/create` webhook decrements the local DB just like any other in-person sale.

In one sentence: **DB is master; TCGPlayer is the front door for online product (cards + sealed) and one of three sales channels; eBay is online-only; Shopify POS mirrors the entire DB inventory and rings up walk-ins. Supplies are a Shopify-only subset; everything else exists on all three channels.**

---

## 2. Resolved Constraints & Decisions

These came from the planning conversation and are load-bearing for the design:

| Decision | Choice | Implication |
|---|---|---|
| Deployment | Local server on shop PC + LAN web UI | One always-on machine; staff devices are clients. |
| Dev / shop-PC split | **Code is developed on a separate dev machine; ships to the shop PC as a compiled Windows binary.** | Cannot assume Python interpreter exists on the shop PC. App must be packaged as a single distributable artifact (PyInstaller). Resource paths must be `sys._MEIPASS`-aware. |
| Python runtime | **Python 3.10** (already on dev box) | Locked to 3.10 because that's what's installed locally and all our libs work on it. Don't use 3.11+ syntax (`Self`, `LiteralString`, etc.) so packaging stays predictable. |
| Distribution | **PyInstaller (one-folder)** producing a Windows directory bundle (`.exe` + DLLs + bundled assets) | One-folder mode beats one-file for our case: faster startup, easier to inspect/debug, plays nicer with Playwright's bundled Chromium. Ship the bundle as a zip. |
| TCGPlayer access | **PRO Seller, CSV bulk upload/download** | No scraping needed. Integration is a periodic CSV diff, not browser automation. |
| eBay access | Developer account to be created | Modern REST Sell APIs (Inventory + Fulfillment). |
| Shopify POS | Production app on shop tablet, no sandbox | Integration via Admin API **Draft Orders** + `orders/create` webhook. No POS UI extension. |
| Inventory model | Mix of singles (qty-1 by condition) and bulk (qty-N) | Schema must support both. |
| Cash sales | Recorded through Shopify POS as cash payment | Shopify is the financial source of truth for all in-person sales. |
| Card fee | Surcharge passed to the customer (separate line) | Cashier picks payment method in our UI; total adjusts before draft order. |
| Tablet capability | Tablet must render our POS UI in-browser alongside Shopify POS app | UI must be tablet-friendly (large hit targets, no hover, works in mobile Safari/Chrome). |

---

## 3. Recommended Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.10 | Locked to dev-machine version. Avoid 3.11+ syntax for packaging predictability. |
| Package manager | pip + venv (stdlib) | Simple; no extra tooling on the shop PC (which never sees Python anyway). |
| Distribution | PyInstaller (one-folder bundle) | Ship the shop PC a self-contained directory with `tag_inventory.exe`, no Python install required. |
| Web framework | FastAPI | Async, OpenAPI for free, easy webhook endpoints. |
| Templating / UI | Jinja2 + HTMX + Tailwind CSS | No SPA build step. Tablet-friendly. Matches the "single shop PC" deployment model. |
| Database | SQLite (WAL mode) via SQLAlchemy 2 + Alembic | Single-file, no DB server to run on the shop PC, fits expected volume (low thousands of cards). |
| Background work | APScheduler (in-process) | One process to deploy. No Redis/Celery for this scale. |
| Shopify | `ShopifyAPI` Python lib + raw httpx for webhooks | Admin API REST is well-supported. |
| eBay | `ebaysdk-python` or direct httpx calls to Sell APIs | SDK is dated; direct REST calls may end up cleaner. Decide in Phase 2. |
| TCGPlayer | `pandas` + `httpx` for CSV; **Playwright (Python)** only as a fallback if CSV endpoint requires browser-driven auth | Keep dependency optional. |
| Process supervisor | NSSM (Windows service) or Task Scheduler "always-running" | Shop PC is Windows. |
| Backups | Litestream → OneDrive/Dropbox folder, OR simple cron-like nightly copy | SQLite WAL streaming gives near-zero data loss. |
| Reverse proxy / TLS | Caddy on the shop PC, self-signed cert for LAN HTTPS | Tablet talks HTTPS even on LAN; needed for clipboard/camera access if we add scanning later. |

**Why not Node/Next.js or .NET:** Node adds an SPA build pipeline and hot-reload complexity that doesn't earn its keep here. .NET desktop locks us into one machine and makes the LAN web-UI requirement awkward.

---

## 4. High-Level Architecture

```
                        +------------------------------+
                        |     Shop PC (Windows)        |
                        |                              |
                        |  +------------------------+  |
   TCGPlayer  <-CSV---->|  | TCGPlayer Sync Worker  |  |
   Seller Portal        |  +-----------+------------+  |
                        |              |               |
                        |  +-----------v------------+  |
   eBay Sell API <-RST->|  | eBay Sync Worker       |  |
                        |  +-----------+------------+  |
                        |              |               |
                        |  +-----------v------------+  |
                        |  | Inventory Service      |  |
                        |  | (FastAPI + SQLite)     |  |
                        |  +-----+--------------+---+  |
                        |        ^              ^      |
                        |  +-----+----+   +-----+---+  |
                        |  | Webhook  |   | Web UI  |  |
                        |  | Receiver |   | (HTMX)  |  |
                        |  +-----+----+   +----+----+  |
                        +--------|-------------|-------+
                                 |             |
                  Shopify Admin  |             | LAN HTTPS
                       API +     |             |
                     webhooks    |             |
                                 v             v
                  +--------------+----+   +----+--------+
                  | Shopify Cloud     |   | Shop tablet |
                  | (POS, Orders)     |   | (browser    |
                  +-------------------+   |  for POS UI |
                                          |  + Shopify  |
                                          |  POS app)   |
                                          +-------------+
```

### Components

1. **TCGPlayer Sync Worker** — bidirectional CSV bridge to the PRO Seller portal. Periodic (every 3–5 min) **download** to detect TCGPlayer-side sales and to ingest *new products* the staff just cataloged on TCGPlayer; periodic **upload** to push the DB's quantity (and optionally price) for every channel_listing whose state has drifted. The download is the source for *initial product data + image* on first sighting of a TCGplayer ID. After that, the DB is master; the worker just keeps TCGPlayer in sync the same way the eBay worker keeps eBay in sync.
2. **eBay Sync Worker** — pushes new listings, updates quantities, polls Fulfillment API for new orders.
3. **Shopify POS Bridge** — bidirectional. (a) Push **all products** (cards, sealed, supplies) from local DB to Shopify Admin API as POS-channel-published products, with quantity kept in sync from the DB whenever inventory changes anywhere (new product, edit, eBay sale, TCGPlayer sale, walk-in sale). (b) Build Draft Orders from our POS UI cart, referencing Shopify variant IDs so they appear as catalog line items. (c) Receive the `orders/create` webhook for *all* in-store sales — whether the cashier built the cart in our POS UI or rang it up natively in the Shopify POS app — and decrement local inventory accordingly.
4. **Inventory Service** — central FastAPI app holding the DB and all business logic (the "decrement and propagate" engine). All sync workers and the UI talk only to this.
5. **POS UI (browser)** — tablet-friendly cart for walk-in sales. Lists available stock with images, condition, price; supports search; computes total + tax + optional card surcharge; sends final cart to Shopify POS Bridge.
6. **Admin UI (browser)** — settings, manual sync trigger, oversell/conflict resolution, sales log, channel diff inspection.

Everything runs as one Python process (FastAPI + APScheduler) on the shop PC, supervised as a Windows service. Webhook endpoints are exposed via Caddy reverse-proxy with a public hostname (Cloudflare Tunnel or Tailscale Funnel) so Shopify can reach them — this is the only inbound-from-internet surface.

---

## 5. Data Model

User-supplied required fields (2026-04-26):

| Field | Singles | Sealed | Supplies *(in-store only)* |
|---|---|---|---|
| Image | ✓ | ✓ | ✓ |
| Name | ✓ | (uses Set + subtype as name) | ✓ |
| Condition | ✓ | — (always "Sealed") | — |
| Price | ✓ | ✓ | ✓ |
| Quantity | ✓ | ✓ | ✓ |
| Rarity | ✓ | — | — |
| Set | ✓ | ✓ | — |
| Type | ✓ | — | (category — Sleeves / Deck Box / Dice / etc.) |
| Description | ✓ | ✓ | ✓ |

**Supplies are physical-store-only**: they are *never* pushed to TCGPlayer or eBay, and they are *not* sourced from the TCGPlayer CSV. They are entered directly via the Admin UI and exist only in the local DB and the POS UI for walk-in sales. The supply field list above is provisional — see §10 Phase-1 open questions to confirm.

These map to a two-table inventory schema (one `product` row per TCGPlayer product ID *or* per locally-entered supply, one `inventory_unit` row per (product, condition)):

### `product` — canonical card/sealed-product record

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | local |
| `tcgplayer_product_id` | int UNIQUE NULL | from CSV; the join key with TCGPlayer. **NULL for supplies** (no TCGPlayer counterpart). |
| `shopify_product_id` | bigint UNIQUE NULL | join key with Shopify. NULL until the Shopify Sync Worker first publishes the product; populated thereafter. **All kinds (cards, sealed, supplies) are published**. |
| `shopify_variant_id` | bigint NULL | the variant ID Shopify assigned; used as the `variant_id` for Draft Order line items so cards appear as cataloged items rather than custom lines. |
| `kind` | enum('single', 'sealed', 'supply') | drives which fields below are required and whether the product participates in cross-channel sync |
| `name` | text | "Lightning Bolt" / "Bloomburrow Bundle" / "Dragon Shield Matte Black 100ct" |
| `set` | text NULL | TCGPlayer set name; NULL for supplies |
| `description` | text | rules text for singles, marketing copy for sealed, blurb for supplies |
| `image_url` | text | local cache path; for cards/sealed the original is `https://product-images.tcgplayer.com/fit-in/<size>/<id>.jpg`; for supplies, an image uploaded via Admin UI |
| `language` | text | "English" default; null OK for sealed/supplies |
| `is_foil` | bool | singles only; default false |
| `rarity` | text NULL | singles only ("Common", "Mythic Rare", "Holo Rare", …) |
| `card_type` | text NULL | singles only — primary type ("Creature", "Spell", "Trainer", …); see open Q in §10 |
| `sealed_subtype` | text NULL | sealed only ("Booster Box", "Bundle", "Theme Deck", …) |
| `supply_category` | text NULL | supplies only ("Sleeves", "Deck Box", "Playmat", "Dice", …) |
| `is_online_listable` | bool | true for kind ∈ {single, sealed}; **false for kind=supply**. The TCGPlayer and eBay sync workers skip rows where false. The Shopify Sync Worker pushes **everything** to Shopify regardless (Shopify mirrors the entire DB). |
| `created_at`, `updated_at` | timestamps | |

One `product` per TCGPlayer ID for cards/sealed. For supplies, one `product` per locally-entered SKU (no TCGPlayer ID). Foil and non-foil of the same card are *different* TCGPlayer IDs and therefore different `product` rows — this matches how TCGPlayer issues IDs.

### `inventory_unit` — what's actually for sale

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | also the SKU we send to eBay |
| `product_id` | FK product | |
| `condition` | text NULL | "NM"/"LP"/"MP"/"HP"/"DMG" for singles; NULL (or "Sealed") for sealed |
| `quantity_on_hand` | int | the decrement target |
| `unit_price` | numeric | local price; channel-specific overrides go on `channel_listing` |
| `last_local_edit_at` | timestamp | so the TCGPlayer outbound sync knows when to push price changes |

UNIQUE(`product_id`, `condition`). Singles typically have 1–4 inventory_unit rows per product (one per condition stocked). Sealed has a single row with quantity ≥ 1.

### Supporting tables

- **`channel_listing`** — one row per (inventory_unit, channel) where channel ∈ {tcgplayer, ebay}. Holds the channel-specific listing ID, last-pushed quantity, last-pushed price, last-sync timestamp, sync state, and the *push-id* used to ignore our own outbound changes when they reflect back in the next inbound CSV diff.
- **`sale`** — one per completed transaction. Channel ∈ {tcgplayer, ebay, shopify_pos}. Holds Shopify order ID where applicable, payment method, totals, tax, card-surcharge.
- **`sale_line`** — one per inventory_unit sold within a sale. Holds quantity_sold, unit_price, condition snapshot.
- **`sync_run`** — audit log of every sync attempt (worker, channel, started_at, ended_at, items_changed, errors).
- **`conflict`** — open issues needing human resolution: oversells, image-fetch failures, channel-listing-not-found, etc. Drives the Admin UI's queue view.
- **`outbound_change`** — queue of pending updates to push to TCGPlayer or eBay (decoupling decrement from push so transient API failures don't lose data).

**Concurrency:** every inventory mutation is a SQLite transaction wrapping `UPDATE inventory_unit SET quantity_on_hand = quantity_on_hand - :n WHERE id = :id AND quantity_on_hand >= :n`. Zero rows affected ⇒ oversell, write a `conflict` row, refund/cancel flow downstream.

### What the POS UI shows

The cart/browse UI maps directly to these fields. Three layouts driven by `product.kind`:
- **Single:** `image_url`, `name`, `condition`, `unit_price`, `quantity_on_hand`, `rarity`, `set`, `card_type`, `description`
- **Sealed:** `image_url`, `name`, `unit_price`, `description`, `set`, `quantity_on_hand`
- **Supply:** `image_url`, `name`, `supply_category`, `unit_price`, `quantity_on_hand`, `description`

Supplies appear in the POS UI for walk-in sales but are filtered out of the online-listing browse views (and obviously out of the TCGPlayer/eBay sync workers via `is_online_listable=false`).

---

## 6. Integration Surfaces

### 6.1 TCGPlayer (PRO Seller bulk CSV)

**TCGPlayer plays two distinct roles** and the worker must keep them straight:
- *Front door* for newly-cataloged product (read-only ingestion, only on first sighting of a TCGplayer ID).
- *Sales channel* like eBay — receives quantity updates pushed from the DB; surfaces TCGPlayer-marketplace sales for the DB to consume.

- **Inbound (CSV download, every 3–5 min):** PRO Seller portal exposes a "Download Pricing" CSV containing all current listings (TCGplayer ID, name, set, condition, qty, price, …). Worker downloads, diffs against `channel_listing` to detect:
  - **new product** (TCGplayer ID never seen before) → create `product` + `inventory_unit` + `channel_listing`; fetch image from `https://product-images.tcgplayer.com/fit-in/<size>/<product_id>.jpg`. *This is the only path by which product data and images enter the DB.*
  - **quantity decrease without a matching outbound push** → it's a TCGPlayer-marketplace sale: record `sale` with channel=tcgplayer, decrement local, enqueue eBay update.
  - **quantity decrease that matches a recent outbound push** → ignore (it's our own update being reflected back).
  - **price change** → record but do not auto-propagate; surface for staff review.
- **Outbound (CSV upload, every N min):** any `inventory_unit` whose `channel_listing[tcgplayer].last_pushed_quantity` ≠ current `quantity_on_hand` (or whose price has been edited locally) emits a row into the next outbound batch. The trigger does not care *why* the local quantity changed — eBay sale, Shopify POS sale, manual edit — TCGPlayer just gets the new state. Outbound rows are tagged with a push-id so the next inbound diff can ignore them (see above). **Supply products are skipped** (filtered by `product.is_online_listable=false`); supplies never have a TCGPlayer `channel_listing` row in the first place.
- **Auth:** PRO Seller portal session cookie. Capture once via headed Playwright login, persist; refresh when expired. Document a "re-auth" admin page.
- **Risk note:** CSV is batch, not realtime. Window of risk: a card sold on TCGPlayer at minute 0 won't be reflected in our DB until the next sync (≤5 min). Same window applies in reverse. **Accept this; surface it in Admin UI.**

### 6.2 eBay (Sell APIs)

- **Scope:** singles + sealed only. Supplies are skipped via `product.is_online_listable=false` (no eBay listing, no offer, ever).
- **Listing creation:** `POST /sell/inventory/v1/inventory_item/{sku}` then `/offer` then `/offer/{id}/publish`. SKU = our `inventory_unit.id`.
- **Quantity sync:** `PUT /sell/inventory/v1/inventory_item/{sku}` with new available quantity. End-of-life listing when quantity hits zero.
- **Sales detection:** poll `/sell/fulfillment/v1/order?filter=creationdate:[…]` every 2–5 minutes. Webhooks via eBay's notification framework are an option but require more setup; **polling is good enough at this volume** and avoids the public-webhook compliance burden.
- **Auth:** OAuth client credentials + refresh token. Store encrypted at rest.

### 6.3 Shopify POS (Admin API + webhooks)

Shopify mirrors the **entire** DB inventory (cards + sealed + supplies). It is touched in four different ways, each with its own contract.

**6.3.a Product publishing (DB → Shopify catalog) — applies to all kinds:**
  - On `product` insert (new TCGPlayer card, new sealed product, or new supply via Admin UI), enqueue an `outbound_change` row for the Shopify channel.
  - Shopify Sync Worker drains the queue, calling `POST /admin/api/{ver}/products.json` (first time) or `PUT .../products/{id}.json` (updates). Required fields: title, body_html (description), images, variant price, variant inventory_quantity, plus the publication graph set so the product is **published only to the POS sales channel** (not Online Store, not Buy Button) — done via the GraphQL `publishablePublish` mutation against the POS publication ID.
  - On success, store the returned Shopify product ID + variant ID on `product.shopify_product_id` / `shopify_variant_id` and create `channel_listing[shopify_pos]` with `last_pushed_quantity = 1`.
  - Bulk initial sync: cards arrive in batches via the TCGPlayer CSV; the worker rate-limits Shopify pushes (Shopify REST = 2 req/sec per app) and uses `bulkOperationRunMutation` for the first-time backfill if the catalog has thousands of products.

**6.3.b Quantity sync (DB → Shopify) — keeps the in-store count accurate:**
  - Whenever `inventory_unit.quantity_on_hand` changes for *any* reason — eBay sale, TCGPlayer sale, walk-in sale, manual edit — an outbound_change row is enqueued for the Shopify channel along with the analogous rows for eBay and TCGPlayer (where applicable).
  - Shopify Sync Worker calls `inventoryItemAdjustQuantity` (GraphQL) or the equivalent REST endpoint to set the new quantity on the variant.
  - **Exception (no-op optimization):** when the quantity change *originates* from Shopify itself (a walk-in sale via 6.3.c or 6.3.d), Shopify has already decremented internally; we still push the new value back, which is a harmless no-op on Shopify's side and keeps the channel_listing row in sync.

**6.3.c Cart handoff flow (POS UI → Shopify POS app, walk-in sale):**
  1. Cashier builds cart in our POS UI on the tablet.
  2. Cashier picks "Card" or "Cash". Our app computes line items + tax + optional card surcharge line, creates a **Draft Order** via Admin API (`POST /admin/api/{ver}/draft_orders.json`) with `use_customer_default_address: false`, line items referencing Shopify variant IDs (every product is in the Shopify catalog now, so all lines are referenced — no custom line items).
  3. Our app shows the cashier "Open draft #1234 in Shopify POS." Cashier swipes to the Shopify POS app, opens "Drafts," taps the order, processes payment on the card reader (or selects "Cash").
  4. Shopify fires `orders/create` webhook to our server.
  5. Webhook receiver matches each line item by `shopify_variant_id` to our `inventory_unit`, decrements `quantity_on_hand`, enqueues outbound_change rows for the *other* channels (TCGPlayer + eBay, where applicable per `is_online_listable`).

**6.3.d Native-POS-rang-up sale (Shopify catalog → orders/create webhook):**
  - Cashier opens Shopify POS app directly, browses the catalog, adds items, processes payment. This works for any product (card, sealed, supply) since they're all published.
  - Same `orders/create` webhook fires — but the order has no `draft_order_id` matching one we created.
  - Webhook receiver detects this case, matches each line item by `shopify_variant_id` → `inventory_unit`, decrements `quantity_on_hand`, enqueues outbound_change rows for TCGPlayer + eBay (where `is_online_listable=true`).

**Cross-cutting:**
- **Card fee (surcharge):** added as a line item on the draft order only when the cashier picked "Card" in our POS UI (6.3.c). Native-POS-rang-up sales (6.3.d) don't get this surcharge — Shopify POS handles its own tax/payment math, and we accept it as-is.
- **Tax:** rely on Shopify's tax engine; pass line items at base price, let Shopify add taxes from the store's tax registration.
- **Webhook auth:** verify the `X-Shopify-Hmac-Sha256` header on every request.
- **Why not POS UI Extensions?** Shopify supports custom tiles inside the POS app, but they're tied to a Public app review process and the user has no test instance. Draft Orders + native catalog browse cover both flows without it.

---

## 7. Key Flows

### 7.1 Ingestion (TCGPlayer → DB)
TCGPlayer Sync Worker downloads CSV → diffs against DB → inserts new products, updates quantities/prices, queues image fetches, writes `sync_run`. Runs every 3-5 minutes by default (configurable). On each new product insert it enqueues outbound_change rows for **both eBay and Shopify** so the new card is mirrored to both online catalogs.

### 7.2 Outbound listing push (DB → eBay + Shopify)
On `inventory_unit` insert or quantity change, enqueue `outbound_change` rows for every channel the product participates in:
- **eBay** — only if `is_online_listable=true` (cards/sealed; not supplies).
- **Shopify** — always. Shopify mirrors the entire DB.
- **TCGPlayer** — only when the change *originated* somewhere other than TCGPlayer (avoids echo loops).

The eBay Sync Worker and Shopify Sync Worker drain their respective queues every 60s, calling their APIs. Failures retry with exponential backoff and surface in `conflict` table after N attempts.

### 7.3 Online sale on eBay → cross-channel decrement
eBay Sync Worker poll detects new order → matches SKU to `inventory_unit` → atomic decrement → enqueue outbound_change rows for **TCGPlayer AND Shopify** → next TCGPlayer sync uploads qty=0 to TCGPlayer's bulk endpoint; next Shopify sync push updates the in-store inventory count so the cashier doesn't try to ring it up.

### 7.4 Online sale on TCGPlayer → cross-channel decrement
TCGPlayer CSV sync detects qty decrease → record `sale` with channel=tcgplayer → atomic decrement (already reflected on TCGPlayer side) → enqueue outbound_change rows for **eBay AND Shopify** → eBay quantity update + Shopify in-store inventory update.

### 7.4b Product published to Shopify (DB → Shopify catalog)
Applies on first appearance of any `product` (cards/sealed first sighted via TCGPlayer CSV, or supply created in Admin UI):
- outbound_change enqueued for the Shopify channel
- Shopify Sync Worker calls Admin API to create the Shopify Product (POS sales channel only via the publication graph)
- `shopify_product_id` and `shopify_variant_id` stored on `product`; `channel_listing[shopify_pos]` row created with `last_pushed_quantity = qty_on_hand`

Subsequent quantity changes use the same outbound_change → Shopify Sync Worker path.

### 7.5 In-person sale (walk-in via our POS UI handoff)
Cashier searches in POS UI → adds inventory_units to cart → picks "Card"/"Cash" → our app shows total breakdown (subtotal, tax, card surcharge if applicable) → "Send to POS" → Draft Order created with **referenced** line items (every product is in the Shopify catalog now) → cashier opens it in Shopify POS app on the same tablet → payment processed (card via reader, or cash) → Shopify fires `orders/create` webhook → our webhook receiver matches each line by `shopify_variant_id` → atomic decrement on each `inventory_unit` → enqueues outbound updates **for eBay and TCGPlayer (where applicable per `is_online_listable`)**.

Shopify itself doesn't need an outbound push for this flow — Shopify already decremented its own count when it processed the order, and our DB is now in sync. (The next routine consistency check just confirms no drift.)

**Supplies in a walk-in cart:** if the cart contains supply rows, those lines decrement local inventory only — no TCGPlayer or eBay outbound updates (because `is_online_listable=false`). Shopify is already up to date from the order itself.

**This is the symmetric case of §7.3 and §7.4** — a sale on any channel triggers updates on the others. Walk-in cash and walk-in card behave identically from the inventory standpoint; the only difference is the card-surcharge line on the draft order.

### 7.5b In-person sale via Shopify POS native catalog (any product)
Cashier opens the Shopify POS app directly → browses the catalog (cards, sealed, supplies are all there because §7.4b publishes everything) → adds items → processes payment → Shopify fires `orders/create` webhook with **no** matching draft_order_id → webhook receiver matches each line by `shopify_variant_id` → atomic decrement on the corresponding `inventory_unit` → enqueues outbound_change rows for eBay + TCGPlayer (where `is_online_listable=true`). For pure supply rings, no fan-out happens. This is the path for "I just want this real quick" sales and is also the fallback when our POS UI is down.

### 7.6 Oversell handling
Atomic decrement returns 0 rows ⇒ row in `conflict` table with channel/sku/customer info ⇒ Admin UI shows it ⇒ staff manually refund/notify per channel's process. Not automated in v1.

### 7.7 Cash drop / reconciliation
Daily Admin UI report cross-checks `sale` rows for the day against Shopify's day-end report. Discrepancies logged as conflicts.

---

## 8. Hard Problems & Risks

1. **CSV is batch, not realtime.** Same card can sell on TCGPlayer and eBay within a 10-minute window. Mitigation: shorten sync interval to 3-5 min for low-quantity items, accept oversell risk on the rest, document the refund flow as part of operations.
2. **Shopify Draft Order requires a manual "open the draft" step** in the POS app. Mitigation: keep our POS UI's "Send to POS" button big and the draft-order numbering predictable (e.g., last 4 digits big and visible).
3. **eBay listing creation is finicky** (category + aspects + condition mapping). Mitigation: Phase 2 includes a seed step to learn-and-cache eBay's category/aspect data per game (Magic, Pokemon, etc.).
4. **TCGPlayer auth via session cookie can break silently** on password change or token expiry. Mitigation: an Admin UI "Reauth" button that opens a Playwright-driven login flow; alerting when CSV download fails.
5. **No Shopify test instance** means we'll be testing Draft Orders against the live store. Mitigation: build a "dry-run" mode in the Shopify Bridge that prints the draft-order payload instead of POSTing. Use it for the first end-to-end tests; flip to live only once the structure is verified.
6. **Tablet running our UI + Shopify POS app simultaneously** may stutter. Mitigation: keep the POS UI dependency surface tiny (no SPA, server-rendered HTMX). Confirm performance on the actual tablet in Phase 0.
7. **Backups.** SQLite is one file — corruption or accidental delete = data loss. Mitigation: Litestream replication to cloud storage from day one.
8. **Two cashier paths for supplies** (our POS UI handoff vs. Shopify POS native browse) means staff must learn when each applies. Mitigation: cashier runbook lays out the rule simply — "use our POS UI for any sale that includes a card; use Shopify POS native for supply-only quick rings or fall back to it if our app is down." Webhook receiver handles both paths transparently so there's no inventory drift either way.
9. **Initial Shopify catalog backfill** can be large. With thousands of cards in the DB, pushing each as a Shopify product hits the 2 req/sec REST limit hard — a 10k-product backfill takes ~85 minutes serially. Mitigation: use Shopify's `bulkOperationRunMutation` (GraphQL) for the first-time backfill; afterwards the steady-state delta is small enough that the standard rate limit is fine. Also: rate-limited retry with backoff in the Shopify Sync Worker.
10. **Shopify in-store inventory drift** if our Shopify Sync Worker falls behind. A card sold on eBay but not yet pushed to Shopify means the cashier sees stock that isn't really there. Mitigation: webhook receiver on `inventory_levels/update` can sanity-check; failed Shopify pushes surface in `conflict` table within 5 minutes; cashier runbook says "if Shopify POS shows stock but our POS UI doesn't, trust our POS UI."

---

## 9. Phasing Recommendation

Each phase produces working, demoable, testable software. Subsequent phases will get their own detailed implementation plans (file lists, task breakdowns, tests).

| Phase | Deliverable | Approx scope |
|---|---|---|
| **0. Skeleton** | FastAPI app running on shop PC as a Windows service, Caddy + LAN HTTPS, SQLite + Alembic baseline, empty Admin UI shell, tablet can load it. | 1–2 sessions |
| **1. TCGPlayer ingestion** | CSV download/diff/import working. Products + inventory_units + channel_listings populated. Admin UI shows current inventory. Read-only. | 2–3 sessions |
| **2. eBay + Shopify outbound** | Push DB inventory to **both** eBay listings and Shopify products (POS-channel-only). New listings/products on insert; qty updates on change. No sale-detection yet. Includes the Shopify catalog backfill via bulkOperationRunMutation. | 3 sessions |
| **3. Cross-channel sale sync** | eBay sale polling; TCGPlayer CSV-detected sales; outbound decrement queue fans out to **all three** channels (eBay + Shopify + TCGPlayer); oversell conflict surface. | 2 sessions |
| **4. POS UI (browse + cart) + Supply admin** | Tablet-friendly browse/search across singles + sealed + supplies, with images, conditions, price; cart; subtotal/tax/card-fee preview. Admin form for adding/editing supplies (the only entry point for supply products). No payment integration yet. | 2–3 sessions |
| **5. Shopify POS handoff** | Draft Order creation referencing Shopify variant IDs, cashier flow, `orders/create` webhook receiver, decrement on order create, native-POS-rang-up handling. | 2 sessions |
| **6. Polish & ops** | Backups (Litestream), monitoring, conflict resolution UI, daily reconciliation report, runbook for re-auth and outages. | 1–2 sessions |

**Out of scope for v1** (called out so we don't grow scope mid-build):
- Customer accounts / loyalty
- Buy-list / trade-in workflow
- Multi-store / multi-cashier permissions
- Price-update automation (matching market prices)
- Barcode/scanner hardware integration
- Mobile-app version (browser-on-tablet is good enough)

---

## 10. Open Questions (resolve before each phase begins)

These are not blockers for the architecture but should be answered before the relevant phase's implementation plan.

**Phase 0:**
- What is the shop PC's specs (will it run Caddy + Python comfortably 24/7)?
- Is there an existing public hostname, or do we need Cloudflare Tunnel / Tailscale Funnel for the Shopify webhook?
- Tablet model & browser?

**Phase 1:**
- Sync cadence: 10 min default OK? Or do we need 3 min for hot inventory?
- **"Type" semantics for singles**: is this the card's primary type ("Creature", "Instant", "Trainer"…), the supertype ("Legendary Creature"…), or game family ("Magic", "Pokemon"…)? TCGPlayer's CSV exposes several type-like columns; the right mapping depends on how the POS UI will display/filter on it.
- **Condition vocabulary**: TCGPlayer uses NM/LP/MP/HP/DMG (and "Unopened" for sealed). Confirm we use TCGPlayer's exact strings vs our own normalized values.
- **Foil flag**: does the shop want foil/non-foil shown as the same product with a flag, or as fully separate products in browse? (TCGPlayer's CSV will give them separate IDs either way; this is a UI question.)
- **Supply schema confirmation**: §5 currently assumes supplies have Image, Name, Category (Sleeves/Deck Box/Dice/…), Price, Quantity, Description. Confirm this matches the shop's mental model, or supply a different field list.

**Phase 4 (POS UI / Supply entry):**
- Where does the supply Admin UI live and who uses it? (Cashier on the tablet, or owner on the shop PC?)
- Do supplies need barcodes/SKUs for receiving inventory, or is qty entered manually?

**Phase 2:**
- Per-game eBay category mapping (Magic, Pokemon, Yu-Gi-Oh, Lorcana, …) — does the user have a preference list or should we infer from TCGPlayer's category field?
- eBay return/shipping/handling-time defaults?
- **Shopify product type / vendor** — what should we set on `product.product_type` and `product.vendor` in Shopify so the in-store catalog browse is well-organized? (Card vs Sealed vs Supply, plus game family?)
- **Initial backfill window** — when we first turn on the Shopify Sync Worker and the DB already has thousands of cards, when do we run the backfill? Off-hours, presumably; need to confirm the shop's quiet window.

**Phase 5:**
- Card-surcharge percentage — fixed (e.g., 2.9%) or configurable per-method?
- Default sales-tax behavior — rely on Shopify's tax engine entirely, or override?
- Receipt expectations — Shopify POS prints its own; do we need to print anything?
- **Publication scope** — confirm "POS sales channel only" is the desired default in Shopify for *all* products (cards, sealed, supplies) — not Online Store, not Buy Button. The Shopify Online Store, if it exists at all, would only show whatever the owner curates separately.
- **Shopify inventory tracking** — set `track_inventory=true` on every product so Shopify POS shows stock-out and refuses oversell at the counter (with our DB as source of truth). Confirm.
- **Native-POS-rang-up cards** — now supported because every card is in the Shopify catalog. Should the cashier prefer it or our POS UI? Recommendation: our POS UI for any cart with multiple cards or condition variants (its search is much faster than Shopify's catalog browse for thousands of SKUs); native POS for "single item, customer is in a hurry."

---

## 11. Verification (at architecture level)

Before starting Phase 0, confirm these end-to-end stories are answerable on paper:

1. *"A card is added to TCGPlayer; how does it appear in our DB and the in-store Shopify POS catalog?"* → CSV sync detects new TCGplayer ID → product/inventory_unit/channel_listing inserted → image fetched from TCGPlayer CDN → outbound_change rows enqueued for **eBay AND Shopify** → eBay listing published, Shopify product created (POS-channel only) → cashier can now see the card both in our POS UI and in Shopify POS native browse.
2. *"An eBay buyer purchases card X; how do TCGPlayer and Shopify (in-store) learn it's gone?"* → eBay poll → DB decrement → outbound_change rows for **TCGPlayer AND Shopify** → next CSV cycle uploads qty=0 to TCGPlayer; next Shopify push sets variant inventory to 0 so the in-store cashier doesn't see it as available.
3. *"A walk-in customer wants to pay cash for two cards; what does the cashier do, and what happens everywhere else?"* → POS UI → add to cart → "Cash" → draft order with referenced variant_ids → open in Shopify POS app → ring up cash → Shopify decrements its own inventory, fires webhook → DB decrement → outbound updates for **eBay AND TCGPlayer** (Shopify is already up to date from the order itself) → card removed from eBay within ~60 s and from TCGPlayer within the next outbound CSV batch.
4. *"The same card sells on eBay and TCGPlayer in the same minute; what happens?"* → first decrement wins, second creates a `conflict` row, Admin UI shows it, staff issues a manual refund per the loser's channel.
5. *"TCGPlayer auth expires overnight; what does the cashier see?"* → Admin UI banner, in-store inventory still works (DB is local), eBay still works, only cross-channel TCGPlayer sync is paused until "Reauth" is clicked.
6. *"A new shipment of sleeves arrives; how do they get into inventory, how are they sold, and where do they show up?"* → Owner opens Admin UI → "New supply" → fills name/category/price/qty/image → product created with `kind='supply'`, `is_online_listable=false` → Shopify Sync Worker publishes it to Shopify as a POS-channel-only product, stores `shopify_product_id` → supply now visible in *both* our POS UI and the Shopify POS app's native catalog → cashier rings up either way → `orders/create` webhook fires → DB decrements → no fan-out to eBay or TCGPlayer (because `is_online_listable=false`).

Any story we can't trace on the diagram is an architecture gap — fix it in this document before writing code.

---

## 12. Files to Modify / Create

**None yet** — this is a greenfield project. Phase-0 plan (the next document) will define the initial repo layout. As a placeholder, the expected top-level structure will be:

```
TAG_Inventory/
  app/
    main.py            # FastAPI entrypoint
    db/                # SQLAlchemy models + Alembic migrations
    sync/              # tcgplayer.py, ebay.py, shopify.py workers
    webhooks/          # Shopify webhook handlers
    pos/               # POS UI routes + templates
    admin/             # Admin UI routes + templates
    settings.py
  templates/
  static/
  tests/
  alembic.ini
  pyproject.toml
  Caddyfile
  service.xml          # NSSM Windows service config
```

This is illustrative — the Phase 0 plan will commit to it.

---

## 13. Worksheet Status

The user clarified on 2026-04-26 that there is no separate worksheet document — the required-fields list (now incorporated into §5) is the worksheet.

If additional spec material surfaces later (e.g., specific game-family attributes, eBay category preferences, tax rules, receipt formatting), drop it into `c:\TAG_Inventory\claude_documents\` and request a revision pass on the relevant section of this plan.
