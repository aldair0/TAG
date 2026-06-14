"""Pick a random product where has_image=False so the user can manually
verify whether a public marketplace search would find it (validating
the proposed seller-filter fallback)."""

from __future__ import annotations

import random
import sqlite3
import urllib.parse

con = sqlite3.connect("data/tag_inventory.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    'SELECT tcgplayer_product_id AS sku, name, "set" AS set_name, number, '
    "rarity, marketplace_product_id "
    "FROM product "
    "WHERE has_image = 0 "
    "AND tcgplayer_product_id IS NOT NULL "
    'AND name IS NOT NULL AND name != "" '
    'AND "set" IS NOT NULL AND "set" != "" '
    'AND number IS NOT NULL AND number != "" '
    "ORDER BY RANDOM() LIMIT 1"
).fetchall()

if not rows:
    print("No matching rows.")
    raise SystemExit(0)

r = rows[0]

print(f"=== Random failing product ===")
print(f"  TCGPlayer Id (SKU)        : {r['sku']}")
print(f"  Name                      : {r['name']}")
print(f"  Set                       : {r['set_name']}")
print(f"  Number                    : {r['number']}")
print(f"  Rarity                    : {r['rarity']}")
print(f"  marketplace_product_id    : {r['marketplace_product_id']}")
print()

q = urllib.parse.quote_plus(r["name"])
print("=== Manual check links ===")
print(f"  Public marketplace search :  https://www.tcgplayer.com/search/all/product?q={q}")
print(f"  Seller-filtered search    :  Same URL filtered to Tag Collects in TCGPlayer's UI")
print()
print("In the public search, look for a result whose:")
print(f"  Set matches    : {r['set_name']!r}")
print(f"  Number matches : {r['number']!r}")
print(f"If you find one, the URL pattern is /product/<id>/<slug>?... — that <id>")
print("is the marketplace_product_id we want for the image URL.")
