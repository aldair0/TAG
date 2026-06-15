"""Local SQLite backup: hot copy, retention prune, atomic publish."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.backup import BackupError, prune_old, run_backup


def _make_db(path: Path, rows: int = 3) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    con.commit()
    con.close()


def test_run_backup_creates_consistent_copy(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=5)
    dest_dir = tmp_path / "backups"

    out = run_backup(src_path=src, dest_dir=dest_dir, retention_days=14)

    assert out.exists()
    assert out.parent == dest_dir
    # The backup is a real, queryable DB with the same data.
    con = sqlite3.connect(str(out))
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 5
    con.close()
    # No leftover .part temp file.
    assert list(dest_dir.glob("*.part")) == []


def test_run_backup_missing_source_raises(tmp_path: Path):
    with pytest.raises(BackupError):
        run_backup(src_path=tmp_path / "nope.db", dest_dir=tmp_path / "b")


def test_prune_removes_only_old_backups(tmp_path: Path):
    dest = tmp_path / "backups"
    dest.mkdir()
    now = datetime(2026, 6, 14, 23, 30, 0)
    # 16 days old (should go), 2 days old (should stay)
    old = dest / f"tag_inventory-{(now - timedelta(days=16)).strftime('%Y%m%d-%H%M%S')}.db"
    new = dest / f"tag_inventory-{(now - timedelta(days=2)).strftime('%Y%m%d-%H%M%S')}.db"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    # An unrelated file must be left alone.
    keep = dest / "notes.txt"
    keep.write_text("hi")

    removed = prune_old(dest, retention_days=14, now=now)

    assert removed == 1
    assert not old.exists()
    assert new.exists()
    assert keep.exists()


def test_retention_window_keeps_two_weeks(tmp_path: Path):
    dest = tmp_path / "backups"
    dest.mkdir()
    now = datetime(2026, 6, 14, 23, 30, 0)
    # One backup per day for 20 days; only the last 14 days should remain.
    for d in range(20):
        ts = (now - timedelta(days=d)).strftime("%Y%m%d-%H%M%S")
        (dest / f"tag_inventory-{ts}.db").write_bytes(b"x")

    prune_old(dest, retention_days=14, now=now)

    remaining = list(dest.glob("tag_inventory-*.db"))
    # Keep days 0..14 (≤ 14 days old); days 15..19 are pruned.
    assert len(remaining) == 15
