"""Post-import verification probe — counts and a few spot checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("data/tag_inventory.db")


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()

    print("=== Counts ===")
    for tbl in ("product", "inventory_unit", "channel_listing", "outbound_change", "sync_run"):
        n = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl:20s} {n}")

    print()
    print("=== Latest sync_run ===")
    r = cur.execute(
        "SELECT id, worker, direction, started_at, ended_at, rows_seen, rows_inserted, rows_updated, error "
        "FROM sync_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    print(" ", r)

    print()
    print("=== Product kind breakdown ===")
    for kind, n in cur.execute(
        "SELECT kind, COUNT(*) FROM product GROUP BY kind ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {kind:10s} {n}")

    print()
    print("=== Top 10 conditions seen ===")
    for cond, n in cur.execute(
        "SELECT condition, COUNT(*) FROM inventory_unit GROUP BY condition ORDER BY 2 DESC LIMIT 10"
    ).fetchall():
        print(f"  {str(cond):40s} {n}")

    print()
    print("=== 5 sample products (oldest TCGplayer Ids) ===")
    for r in cur.execute(
        'SELECT tcgplayer_product_id, kind, name, "set" FROM product '
        "WHERE tcgplayer_product_id IS NOT NULL "
        "ORDER BY tcgplayer_product_id LIMIT 5"
    ).fetchall():
        print(" ", r)

    print()
    print("=== 5 sample inventory_units (joined) ===")
    for r in cur.execute(
        'SELECT p.tcgplayer_product_id, p.name, iu.condition, iu.quantity_on_hand, iu.unit_price '
        "FROM inventory_unit iu JOIN product p ON p.id = iu.product_id "
        "WHERE p.tcgplayer_product_id >= 3523831 LIMIT 5"
    ).fetchall():
        print(" ", r)


if __name__ == "__main__":
    main()
