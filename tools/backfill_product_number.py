"""One-shot backfill: copy the CSV's Number column into Product.number
for every product imported before the column existed."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

CSV = Path("data/csv/tcgplayer_pricing.csv")
DB = Path("data/tag_inventory.db")


def main() -> None:
    sku_to_number: dict[int, str] = {}
    with CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sku_raw = (row.get("TCGplayer Id") or "").strip()
            number = (row.get("Number") or "").strip()
            if not sku_raw or not number:
                continue
            try:
                sku_to_number[int(sku_raw)] = number
            except ValueError:
                pass

    print(f"CSV rows with non-empty Number: {len(sku_to_number):,}")

    con = sqlite3.connect(DB, isolation_level=None)
    cur = con.cursor()

    before_null = cur.execute(
        "SELECT COUNT(*) FROM product WHERE number IS NULL"
    ).fetchone()[0]
    print(f"Products with NULL number before: {before_null:,}")

    updated = 0
    cur.execute("BEGIN")
    try:
        for sku, number in sku_to_number.items():
            cur.execute(
                "UPDATE product SET number = ? WHERE tcgplayer_product_id = ?",
                (number, sku),
            )
            updated += cur.rowcount
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    after_null = cur.execute(
        "SELECT COUNT(*) FROM product WHERE number IS NULL"
    ).fetchone()[0]
    print(f"Rows updated: {updated:,}")
    print(f"Products with NULL number after: {after_null:,}")

    con.close()


if __name__ == "__main__":
    main()
