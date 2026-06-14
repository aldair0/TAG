# Test Account Setup Guide

This is the work you need to do (mostly web sign-ups and credential generation) so the Phase 1+ code has somewhere to talk to. Phase 0 doesn't need any of these — you can do this in parallel while the skeleton is being built.

When each section is done, paste the credentials into `c:\TAG_Inventory\.env` (the file will be set up in Phase 0). **Never commit `.env` to git** — the `.gitignore` will exclude it.

---

## 1. Shopify Partners account + Development Store + Custom App

Needed for: Phase 2 (publishing products to Shopify) and Phase 5 (Draft Orders + webhooks).

### 1a. Create a Partner account
1. Go to https://partners.shopify.com → "Become a partner"
2. Sign up (free). The account is separate from your live store.

### 1b. Create a development store
1. Partners dashboard → **Stores** → **Add store** → **Development store**
2. Purpose: **Build and test apps** (or **Test new features**)
3. Choose a store name (e.g., `tag-inventory-dev`); the URL will be `tag-inventory-dev.myshopify.com`
4. Set yourself as the store owner
5. Once created, click into the store admin

### 1c. Enable POS on the dev store
1. Dev store admin → **Settings** → **Apps and sales channels** → look for **Point of Sale**
2. If not installed: **Shopify App Store** → install **Shopify POS**
3. POS Lite is free; we don't need POS Pro for testing

### 1d. Create a custom app for Admin API access
1. Dev store admin → **Settings** → **Apps and sales channels** → **Develop apps**
   - First time: click **Allow custom app development**
2. **Create an app** → name it `TAG Inventory Bridge`
3. **Configuration** → **Admin API access scopes** — enable:
   - `read_products`, `write_products`
   - `read_inventory`, `write_inventory`
   - `read_locations`
   - `read_draft_orders`, `write_draft_orders`
   - `read_orders`
   - `read_publications`, `write_publications`
4. Click **Install app**
5. From the **API credentials** tab, copy:
   - **Admin API access token** (starts with `shpat_…`)
   - **API key** and **API secret key**
   - Your **shop URL** (e.g., `tag-inventory-dev.myshopify.com`)

### 1e. Configure webhook (we'll do this from code in Phase 5, but verify scope)
- The custom app needs `read_orders` scope to subscribe to `orders/create`. Confirm it's checked above.

### 1f. Drop credentials into `.env` (after Phase 0 sets up the file)
```
SHOPIFY_SHOP_DOMAIN=tag-inventory-dev.myshopify.com
SHOPIFY_ADMIN_API_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxx
SHOPIFY_API_KEY=xxxxxxxxxxxxxxxx
SHOPIFY_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SHOPIFY_WEBHOOK_SECRET=          # set later, when webhooks are configured
```

---

## 2. eBay Developer Program + Sandbox

Needed for: Phase 2 (eBay listings) and Phase 3 (eBay sale polling).

### 2a. Sign up
1. Go to https://developer.ebay.com → **Join the eBay Developers Program**
2. Sign in with your existing eBay account (or create one)
3. Accept the API License Agreement

### 2b. Create application keysets
1. Developer dashboard → **My Account** → **Application Keysets** → **Create a keyset**
2. Name: `TAG Inventory Bridge`
3. eBay generates **two** keysets automatically — Sandbox and Production. We'll use Sandbox for testing.
4. From the Sandbox keyset, copy:
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)**
   - **Dev ID**

### 2c. Create a sandbox seller test user
1. https://developer.ebay.com/DevZone/sandbox-test-users.aspx
2. Sign in, fill the form to create a test user — type **Seller**
3. Note the test user's username and password
4. Optionally seed it with mock listings to verify auth

### 2d. Get a User Access Token (OAuth)
For Phase 2, our app will run a one-time OAuth flow to get a refresh token for this sandbox seller. For now, just keep the App ID / Cert ID / Dev ID handy. The token-acquisition step is part of the Phase 2 plan.

### 2e. Drop credentials into `.env`
```
EBAY_ENV=sandbox
EBAY_APP_ID=YourApp-XXXXXXXX-SBX-XXXXXXXX-XXXXXXXX
EBAY_CERT_ID=SBX-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
EBAY_DEV_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
EBAY_USER_REFRESH_TOKEN=         # set after running OAuth flow in Phase 2
EBAY_SANDBOX_USERNAME=your_test_seller
```

---

## 3. TCGPlayer PRO Seller — sample CSV for fixture testing

Needed for: Phase 1 (CSV ingestion logic). We don't need API credentials — TCGPlayer doesn't have one — but we DO need a real CSV file to test the parser against.

### 3a. Export your current pricing
1. Log into the TCGPlayer Seller Portal at https://store.tcgplayer.com/admin/Login.aspx (or wherever your PRO Seller portal lives)
2. Navigate to **Pricing** → **Mass Pricing Tool** (or whatever the PRO Seller equivalent is called — the path may differ)
3. **Download** your current pricing as CSV
4. Save it to `c:\TAG_Inventory\test_data\tcgplayer_sample.csv` (the Phase 1 plan will create this folder; you can also create it now and drop the file in)

### 3b. Confirm what columns TCGPlayer's CSV actually has
Open the CSV in a text editor and check the header row. The expected columns are roughly:
- `TCGplayer Id`
- `Product Line` (Magic / Pokemon / Yu-Gi-Oh / etc.)
- `Set Name`
- `Product Name`
- `Title` (full descriptive title)
- `Number` (collector number)
- `Rarity`
- `Condition` (Near Mint / Lightly Played / …)
- `TCG Market Price`, `TCG Direct Low`, `TCG Low Price With Shipping`, `TCG Low Price`
- `Total Quantity`
- `Add to Quantity`
- `TCG Marketplace Price` (your listing price)
- `Photo URL`

The actual column names may differ slightly — drop a screenshot or paste the header row when you have the file.

### 3c. Shop login for session-cookie auth (Phase 1)
The CSV download/upload endpoints require an authenticated session. In Phase 1 we'll capture a session cookie via a one-time Playwright login. For now, just have the username/password ready — they'll go in `.env` (and stay encrypted at rest).

```
TCGPLAYER_PRO_USERNAME=
TCGPLAYER_PRO_PASSWORD=
```

---

## What we don't need yet

- **Live Shopify store credentials** — we work entirely against the dev store until Phase 6 polish.
- **eBay production keys** — we work in Sandbox until everything is verified.
- **Real-store TCGPlayer credentials** — for Phase 1 development we work off a sample CSV; only when we're ready to do round-trip auth testing do we touch the real account.

## Order of effort

1. **Today:** Shopify Partners signup → create dev store → install POS → create custom app. The Admin token unblocks Phase 2.
2. **This week:** eBay developer signup + sandbox keyset. The OAuth flow waits for Phase 2 code.
3. **Whenever:** export a TCGPlayer sample CSV and drop it in `test_data/`. Phase 1 needs it.

Hand each credential to me as you get it (paste into chat or into `.env` directly) and I'll wire it up.
