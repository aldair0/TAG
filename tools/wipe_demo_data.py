"""Surgical demo-only wipe.

Demo data was created during phases 0-3 (April 26-27 2026). The real
TCGPlayer CSV import happened 2026-05-03. We split on ``product.created_at``
and remove anything older — products + their cascade (inventory_unit,
channel_listing) plus their non-cascading dependents (outbound_change,
sale_line, and now-empty sale rows).

Run with:
    .\\.venv\\Scripts\\python.exe tools\\wipe_demo_data.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("data/tag_inventory.db")
CUTOFF = "2026-05-03"  # anything strictly older is demo-era

_TABLES = (
    "product",
    "inventory_unit",
    "channel_listing",
    "outbound_change",
    "sale",
    "sale_line",
    "sync_run",
)


def _counts(cur: sqlite3.Cursor) -> dict[str, int]:
    return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLES}


def _print_counts(label: str, c: dict[str, int]) -> None:
    print(f"=== {label} ===")
    for t in _TABLES:
        print(f"  {t:20s} {c[t]:>10,}")
    print()


def main() -> None:
    con = sqlite3.connect(DB, isolation_level=None)  # autocommit; we manage txn
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    before = _counts(cur)
    _print_counts("BEFORE", before)

    demo_pids = [
        r[0]
        for r in cur.execute(
            "SELECT id FROM product WHERE created_at < ?", (CUTOFF,)
        )
    ]
    print(f"Demo products to delete (created_at < {CUTOFF}): {len(demo_pids):,}")

    if not demo_pids:
        print("Nothing to do.")
        con.close()
        return

    cur.execute("BEGIN")
    try:
        # Build placeholder list. Demo set is small (~45) so no chunking needed.
        ph = ",".join(["?"] * len(demo_pids))

        demo_iuids = [
            r[0]
            for r in cur.execute(
                f"SELECT id FROM inventory_unit WHERE product_id IN ({ph})",
                demo_pids,
            )
        ]
        print(f"Demo inventory_units in scope: {len(demo_iuids):,}")

        if demo_iuids:
            ph2 = ",".join(["?"] * len(demo_iuids))
            cur.execute(
                f"DELETE FROM sale_line WHERE inventory_unit_id IN ({ph2})",
                demo_iuids,
            )
            print(f"  sale_line rows deleted: {cur.rowcount:,}")

            cur.execute(
                f"DELETE FROM outbound_change WHERE inventory_unit_id IN ({ph2})",
                demo_iuids,
            )
            print(f"  outbound_change rows deleted: {cur.rowcount:,}")

        # Sales whose every line just got removed are orphans now.
        cur.execute(
            "DELETE FROM sale WHERE id NOT IN (SELECT DISTINCT sale_id FROM sale_line)"
        )
        print(f"  orphan sale rows deleted: {cur.rowcount:,}")

        # Cascade does inventory_unit and channel_listing for us.
        cur.execute(f"DELETE FROM product WHERE id IN ({ph})", demo_pids)
        print(f"  product rows deleted: {cur.rowcount:,}")

        # Demo-era sync_run rows (pre-cutoff) are pure history; remove them
        # so the admin sync history doesn't show stale demo entries.
        cur.execute("DELETE FROM sync_run WHERE started_at < ?", (CUTOFF,))
        print(f"  demo-era sync_run rows deleted: {cur.rowcount:,}")

        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    after = _counts(cur)
    print()
    _print_counts("AFTER", after)
    print("=== Net change ===")
    for t in _TABLES:
        delta = after[t] - before[t]
        print(f"  {t:20s} {delta:>+10,}")

    con.close()


if __name__ == "__main__":
    main()
