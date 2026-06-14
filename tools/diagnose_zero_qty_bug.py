"""Cross-reference the local DB against the current CSV: find any
inventory_units where the DB still says qty>0 but the latest CSV row
for that SKU has Total Quantity blank or 0.

If we find any, the diagnosis is confirmed: ``service.run_ingest``
filters those rows out before the diff engine sees them, so existing
DB rows never get their quantity decremented to match. The system
keeps showing stock for products the seller's CSV says are now empty.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

CSV_PATH = Path("data/csv/tcgplayer_pricing.csv")
DB_PATH = Path("data/tag_inventory.db")


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"missing CSV: {CSV_PATH}")

    # Build sku -> (raw_total_qty_str) for every row in the CSV.
    csv_qty: dict[int, str] = {}
    with CSV_PATH.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = 0
        for row in reader:
            rows += 1
            raw_id = (row.get("TCGplayer Id") or "").strip()
            if not raw_id:
                continue
            try:
                sku = int(raw_id)
            except ValueError:
                continue
            csv_qty[sku] = (row.get("Total Quantity") or "").strip()
    print(f"CSV rows scanned: {rows:,}    unique SKUs: {len(csv_qty):,}")

    # Bin CSV rows by quantity status.
    csv_zero = sum(1 for v in csv_qty.values() if not v or v == "0")
    csv_pos = sum(1 for v in csv_qty.values() if v and v != "0")
    print(f"  CSV SKUs with Total Quantity blank/0 : {csv_zero:,}")
    print(f"  CSV SKUs with Total Quantity > 0     : {csv_pos:,}")
    print()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    db_units = con.execute(
        'SELECT iu.id AS unit_id, iu.quantity_on_hand AS qty, '
        'iu.condition AS condition, '
        'p.tcgplayer_product_id AS sku, p.name AS name, p."set" AS set_name, '
        'p.number AS number '
        "FROM inventory_unit iu "
        "JOIN product p ON p.id = iu.product_id "
        "WHERE iu.quantity_on_hand > 0"
    ).fetchall()

    print(f"DB inventory_units with qty > 0: {len(db_units):,}")

    mismatches: list[dict] = []
    not_in_csv: list[dict] = []
    for u in db_units:
        sku = u["sku"]
        if sku is None:
            continue
        if sku not in csv_qty:
            not_in_csv.append(dict(u))
            continue
        csv_v = csv_qty[sku]
        if not csv_v or csv_v == "0":
            mismatches.append({**dict(u), "csv_qty": csv_v or "(blank)"})

    print()
    print("=== MISMATCHES (DB says in stock, CSV says zero/blank) ===")
    print(f"  count: {len(mismatches):,}")
    if mismatches:
        print("  first 10 examples:")
        for m in mismatches[:10]:
            print(
                f"    sku={m['sku']:>10} db_qty={m['qty']} "
                f"csv_qty={m['csv_qty']:<8} "
                f"name={m['name']!r} set={m['set_name']!r} "
                f"#{m['number']} {m['condition']!r}"
            )
    print()
    print("=== DB SKUs NOT in current CSV at all ===")
    print(f"  count: {len(not_in_csv):,}")
    if not_in_csv:
        print("  first 5 examples:")
        for n in not_in_csv[:5]:
            print(
                f"    sku={n['sku']:>10} qty={n['qty']} "
                f"name={n['name']!r} set={n['set_name']!r}"
            )

    print()
    print("=== Verdict ===")
    if mismatches:
        print(
            f"  CONFIRMED: {len(mismatches):,} product(s) currently show "
            "in-stock locally but the latest CSV says they're zero/blank. "
            "The 'do nothing with row if Total Quantity is blank/0' "
            "filter prevents the existing DB rows from being updated; "
            "they keep their old quantity_on_hand value forever."
        )
    else:
        print(
            "  No mismatches. Every DB unit with qty>0 maps to a CSV row "
            "with Total Quantity > 0."
        )


if __name__ == "__main__":
    main()
