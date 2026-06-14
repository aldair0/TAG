"""Pick a card with has_image=True and reconstruct the exact POST that
discovered its marketplace_product_id. Outputs the request as both a
plain dump and a copy-paste-able cURL for re-execution."""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from app.config import settings
from app.sync.tcgplayer.marketplace_search import (
    SEARCH_URL,
    build_search_payload,
)


def main() -> None:
    con = sqlite3.connect("data/tag_inventory.db")
    con.row_factory = sqlite3.Row
    row = con.execute(
        'SELECT tcgplayer_product_id AS sku, name, "set" AS set_name, '
        "number, marketplace_product_id "
        "FROM product "
        "WHERE has_image = 1 "
        "AND marketplace_product_id IS NOT NULL "
        "ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if row is None:
        sys.exit("No imaged product found.")

    print("=== Source product (from local DB) ===")
    print(f"  SKU (TCGplayer Id)       : {row['sku']}")
    print(f"  Name                     : {row['name']!r}")
    print(f"  Set                      : {row['set_name']!r}")
    print(f"  Number                   : {row['number']!r}")
    print(f"  marketplace_product_id   : {row['marketplace_product_id']}")
    print()

    q = row["name"]
    payload = build_search_payload(
        q=q, seller_key=settings.tcgplayer_seller_key, page=1, page_size=24
    )
    params = {"q": q, "isList": "false", "mpfev": "5106"}

    full_url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"

    print("=== HTTP request actually sent ===")
    print(f"  Method  : POST")
    print(f"  URL     : {full_url}")
    print()
    print("  Headers (the client sets these):")
    for k, v in {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.tcgplayer.com",
        "referer": "https://www.tcgplayer.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
        ),
    }.items():
        print(f"    {k}: {v}")
    print()
    print("  Body (JSON):")
    body_str = json.dumps(payload, indent=2)
    for line in body_str.splitlines():
        print(f"    {line}")
    print()

    # Compact-body curl (Bash-style; works in Git Bash/WSL/macOS/Linux)
    body_compact = json.dumps(payload, separators=(",", ":"))
    body_escaped = body_compact.replace("'", "'\\''")
    print("=== Copy-paste curl (Bash) ===")
    print(
        f"curl -sS -X POST '{full_url}' \\\n"
        "  -H 'accept: application/json, text/plain, */*' \\\n"
        "  -H 'content-type: application/json' \\\n"
        "  -H 'origin: https://www.tcgplayer.com' \\\n"
        "  -H 'referer: https://www.tcgplayer.com/' \\\n"
        "  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 "
        "Edg/147.0.0.0' \\\n"
        f"  --data '{body_escaped}'"
    )
    print()

    # PowerShell version
    body_ps = body_compact.replace("'", "''")
    print("=== Copy-paste PowerShell ===")
    print(
        f"$body = '{body_ps}'\n"
        "$headers = @{\n"
        "  'accept' = 'application/json, text/plain, */*'\n"
        "  'content-type' = 'application/json'\n"
        "  'origin' = 'https://www.tcgplayer.com'\n"
        "  'referer' = 'https://www.tcgplayer.com/'\n"
        "  'user-agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0'\n"
        "}\n"
        f"Invoke-RestMethod -Uri '{full_url}' -Method POST -Headers $headers -Body $body -ContentType 'application/json'"
    )
    print()
    print("=== Resulting CDN image URL (the GET that follows the POST) ===")
    mp_id = row["marketplace_product_id"]
    print(f"  https://tcgplayer-cdn.tcgplayer.com/product/{mp_id}_in_400x400.jpg")


if __name__ == "__main__":
    main()
