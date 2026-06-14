"""Verbose diagnostic for marketplace search hits/misses.

Pulls a product from the local DB by SKU, then runs the marketplace
search BOTH ways:
  1. With the Tag Collects seller filter (current production behavior)
  2. Without any seller filter (public catalog)

Dumps the raw response shape, the parsed hits, and most importantly:
which Tag-Collects-tagged listings exist within the public results.
That's what tells us whether the seller-filter is silently dropping
listings our store actually has, vs. our store genuinely not having a
public marketplace listing for the SKU.

Usage::

    .venv\\Scripts\\python.exe tools\\diagnose_marketplace_search.py 1236114
    .venv\\Scripts\\python.exe tools\\diagnose_marketplace_search.py     # picks one
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import httpx

from app.config import settings
from app.sync.tcgplayer.marketplace_search import (
    SEARCH_URL,
    build_search_payload,
)

# Headers cribbed from the captured browser request — same as the client
# uses, plus full UA so we look like Edge.
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.tcgplayer.com",
    "referer": "https://www.tcgplayer.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
    ),
}


def _load_product_by_sku(sku: int) -> dict | None:
    con = sqlite3.connect("data/tag_inventory.db")
    con.row_factory = sqlite3.Row
    row = con.execute(
        'SELECT tcgplayer_product_id AS sku, name, "set" AS set_name, '
        "number, rarity, marketplace_product_id, has_image "
        "FROM product WHERE tcgplayer_product_id = ?",
        (sku,),
    ).fetchone()
    return dict(row) if row else None


def _do_search(q: str, *, seller_key: str | None) -> dict:
    """Run a single search and return the raw JSON envelope. We mirror
    MarketplaceSearchClient's request layout but make the seller filter
    optional via passing seller_key=None to build_search_payload."""
    params = {"q": q, "isList": "false", "mpfev": "5106"}
    payload = build_search_payload(
        q=q, seller_key=(seller_key or ""), page=1, page_size=24
    )
    # Strip the seller filter when we're doing a public search.
    if not seller_key:
        listing_term = payload["listingSearch"]["filters"]["term"]
        listing_term.pop("sellerKey", None)
    with httpx.Client(timeout=15.0, headers=_HEADERS) as c:
        r = c.post(SEARCH_URL, params=params, json=payload)
        r.raise_for_status()
        return r.json()


def _summarize(envelope: dict, *, target_sku: int) -> None:
    outer = (envelope.get("results") or [{}])[0]
    hits = outer.get("results") or []
    total = outer.get("totalResults") or 0
    print(f"  totalResults             : {total}")
    print(f"  hit count returned       : {len(hits)}")
    if not hits:
        print("  (no products in this response)")
        return
    sku_hits: list[tuple[int, str, str]] = []  # (productId, name, hit_label)
    for hit in hits:
        product_id = int(hit.get("productId") or 0)
        name = hit.get("productName") or ""
        set_name = hit.get("setName") or ""
        num = (hit.get("customAttributes") or {}).get("number") or ""
        listings = hit.get("listings") or []
        listing_skus = [int(li.get("productConditionId") or 0) for li in listings]
        seller_keys = sorted({li.get("sellerKey") or "" for li in listings})
        match = "<-- SKU MATCH" if target_sku in listing_skus else ""
        print(
            f"    productId={product_id} {name!r} set={set_name!r} "
            f"num={num!r} listings={len(listings)} "
            f"sellers={seller_keys} {match}"
        )
        if listings:
            for li in listings:
                marker = "***" if int(li.get("productConditionId") or 0) == target_sku else "   "
                print(
                    f"      {marker} productConditionId={li.get('productConditionId')} "
                    f"sellerKey={li.get('sellerKey')} "
                    f"sellerName={li.get('sellerName')!r} "
                    f"condition={li.get('condition')!r} printing={li.get('printing')!r} "
                    f"price={li.get('price')} qty={li.get('quantity')}"
                )
        if target_sku in listing_skus:
            sku_hits.append((product_id, name, "OK"))
    if not sku_hits:
        print(f"  *** No listing in any hit had productConditionId={target_sku}.")


def main() -> None:
    if len(sys.argv) > 1:
        sku = int(sys.argv[1])
    else:
        # Pick a random failing one
        con = sqlite3.connect("data/tag_inventory.db")
        row = con.execute(
            "SELECT tcgplayer_product_id FROM product "
            "WHERE has_image = 0 AND tcgplayer_product_id IS NOT NULL "
            'AND name != "" AND number != "" '
            "ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if not row:
            sys.exit("no failing product to pick")
        sku = row[0]

    product = _load_product_by_sku(sku)
    if product is None:
        sys.exit(f"sku {sku} not in DB")

    print(f"=== Product (from local DB) ===")
    print(f"  sku        : {product['sku']}")
    print(f"  name       : {product['name']}")
    print(f"  set        : {product['set_name']}")
    print(f"  number     : {product['number']}")
    print(f"  rarity     : {product['rarity']}")
    print(f"  has_image  : {bool(product['has_image'])}")
    print(f"  mp_id      : {product['marketplace_product_id']}")
    print()

    q = product["name"]
    print(f"=== Search 1/2: WITH seller filter (sellerKey={settings.tcgplayer_seller_key!r}) ===")
    print(f"  q={q!r}")
    env_filtered = _do_search(q, seller_key=settings.tcgplayer_seller_key)
    _summarize(env_filtered, target_sku=sku)
    print()

    print(f"=== Search 2/2: WITHOUT seller filter (public catalog) ===")
    print(f"  q={q!r}")
    env_public = _do_search(q, seller_key=None)
    _summarize(env_public, target_sku=sku)
    print()

    # Final question: is there ANY Tag Collects listing in the public
    # response? If so, our seller-filtered search is buggy. If not, the
    # seller genuinely has no marketplace listing for this card.
    public_outer = (env_public.get("results") or [{}])[0]
    tag_listings_in_public = []
    for hit in public_outer.get("results") or []:
        for li in hit.get("listings") or []:
            if li.get("sellerKey") == settings.tcgplayer_seller_key:
                tag_listings_in_public.append(
                    (
                        int(hit.get("productId") or 0),
                        hit.get("productName") or "",
                        int(li.get("productConditionId") or 0),
                        li.get("condition"),
                        li.get("printing"),
                    )
                )

    print("=== Verdict ===")
    if tag_listings_in_public:
        print(
            f"  Public search returned {len(tag_listings_in_public)} listing(s) "
            f"from Tag Collects:"
        )
        for productId, name, pci, cond, printing in tag_listings_in_public:
            mark = "<-- MATCHES OUR SKU" if pci == sku else ""
            print(
                f"    productId={productId} {name!r} "
                f"productConditionId={pci} {cond!r} {printing!r} {mark}"
            )
        # The diagnostic question: does the SELLER-FILTERED search also
        # return these? If not -> seller-filter is silently dropping our
        # own listings.
        filtered_outer = (env_filtered.get("results") or [{}])[0]
        filtered_pcis = {
            int(li.get("productConditionId") or 0)
            for hit in filtered_outer.get("results") or []
            for li in hit.get("listings") or []
        }
        public_tag_pcis = {pci for _, _, pci, _, _ in tag_listings_in_public}
        missing_from_filtered = public_tag_pcis - filtered_pcis
        if missing_from_filtered:
            print(
                f"  [WARN] {len(missing_from_filtered)} of our listing(s) appear in the "
                f"PUBLIC response but NOT in the seller-filtered response: "
                f"{missing_from_filtered}"
            )
            print("  -> seller filter is dropping our own listings. Bug in the search payload.")
        else:
            print(
                "  Both responses see the same Tag Collects listings -- search "
                "is consistent."
            )
    else:
        print(
            "  No Tag Collects listings found in the PUBLIC response either. "
            "The seller does not have an active marketplace listing for any "
            "variant of this product. The search isn't broken — there's just "
            "nothing to find."
        )
        print(
            "  This is an information gap, not a search bug. Decide: keep "
            "seller-only and accept missing images for unlisted inventory, "
            "OR add a fallback that resolves productId from the public "
            "catalog (image-only — no listing claim made)."
        )


if __name__ == "__main__":
    main()
