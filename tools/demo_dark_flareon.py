"""End-to-end live demo of the marketplace-search lookup flow.

Walks: SKU 1219950 (Dark Flareon, Damaged Unlimited, from our CSV) →
search by name → match by productConditionId → product page URL +
image URLs at each size.
"""

from __future__ import annotations

from app.sync.tcgplayer.marketplace_search import (
    MarketplaceSearchClient,
    build_image_url,
)

# From the local CSV: 1219950 is "Dark Flareon - Damaged Unlimited"
EXPECTED_SKU_ID = 1219950
EXPECTED_NAME = "Dark Flareon"
EXPECTED_SET = "Team Rocket"
EXPECTED_NUMBER = "35/82"


def main() -> None:
    with MarketplaceSearchClient() as c:
        # --- Path 1: SKU-id-based lookup (the strongest match) ---
        hit = c.find_by_sku("Dark Flareon", sku_id=EXPECTED_SKU_ID)
        assert hit is not None, "find_by_sku returned None"
        print("=== find_by_sku(SKU 1219950) ===")
        _print_hit(hit)
        print()

        # --- Path 2: attribute-based lookup (fallback if no SKU id) ---
        hit2 = c.find_by_attributes(
            name=EXPECTED_NAME, set_name=EXPECTED_SET, number=EXPECTED_NUMBER
        )
        assert hit2 is not None
        assert hit2.product_id == hit.product_id, (
            "Attribute and SKU lookups disagree on the product id"
        )
        print("=== find_by_attributes(name+set+number) — same product? ===")
        print(f"  product_id matches: {hit2.product_id == hit.product_id}")
        print()

        # --- Image URLs at all five canonical sizes ---
        print("=== Image URLs ===")
        for size in (200, 400, 600, 800, 1000):
            print(f"  {size}x{size}: {build_image_url(hit.product_id, size_px=size)}")
        print()

        # --- Verification (the rule the user spelled out) ---
        ok = (
            hit.name.casefold() == EXPECTED_NAME.casefold()
            and (hit.set_name or "").casefold() == EXPECTED_SET.casefold()
            and (hit.number or "") == EXPECTED_NUMBER
        )
        print(f"Verification (name+set+number): {'PASS' if ok else 'FAIL'}")


def _print_hit(hit) -> None:
    print(f"  product_id     : {hit.product_id}")
    print(f"  name           : {hit.name}")
    print(f"  set            : {hit.set_name}  ({hit.set_code})")
    print(f"  product_line   : {hit.product_line}")
    print(f"  rarity         : {hit.rarity}")
    print(f"  number         : {hit.number}")
    print(f"  market_price   : ${hit.market_price}")
    print(f"  lowest_price   : ${hit.lowest_price}")
    print(f"  listings       : {len(hit.listings)}")
    for li in hit.listings:
        print(
            f"    - SKU {li.product_condition_id} | "
            f"{li.condition} {li.printing} | qty={li.quantity} | ${li.price} | "
            f"seller={li.seller_name} ({li.seller_key})"
        )
    print(f"  product_url    : {hit.tcgplayer_url}")
    print(f"  image_url(400) : {hit.image_url(400)}")


if __name__ == "__main__":
    main()
