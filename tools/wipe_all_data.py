"""Truncate every data row but keep schema + alembic state.

Different from ``app.demo.reset_database`` which drops the schema; we
just want a clean DB to re-import into. Used between the old TCGPlayer
CSV import and the new (corrected) one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("data/tag_inventory.db")

# Order matters: child tables before parents to satisfy FK constraints
# while foreign_keys=ON. ``alembic_version`` is left alone.
_ORDER = (
    "sale_line",
    "sale",
    "outbound_change",
    "channel_listing",
    "inventory_unit",
    "product",
    "conflict",
    "sync_run",
    "app_setting",
)


def main() -> None:
    con = sqlite3.connect(DB, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()

    print("=== BEFORE ===")
    for t in _ORDER:
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {n:>10,}")

    cur.execute("BEGIN")
    try:
        for t in _ORDER:
            cur.execute(f"DELETE FROM {t}")
        # sqlite_sequence only exists when AUTOINCREMENT columns are
        # declared. Our PKs are INTEGER PRIMARY KEY (rowid alias), so
        # the table may not be present at all — try to clear it but
        # don't fail if it isn't there.
        try:
            cur.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ("
                + ",".join("?" for _ in _ORDER)
                + ")",
                list(_ORDER),
            )
        except sqlite3.OperationalError:
            pass
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    cur.execute("VACUUM")  # reclaim disk

    print()
    print("=== AFTER ===")
    for t in _ORDER:
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {n:>10,}")

    con.close()


if __name__ == "__main__":
    main()
