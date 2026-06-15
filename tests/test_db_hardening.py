"""SQLite hardening: busy_timeout pragma, integrity_check, WAL checkpoint."""

from __future__ import annotations

from sqlalchemy import text

from app.db.base import checkpoint_wal, engine, integrity_check


def test_busy_timeout_pragma_is_set():
    with engine.connect() as conn:
        val = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert val == 5000


def test_wal_journal_mode():
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_integrity_check_passes_on_healthy_db():
    assert integrity_check() is True


def test_checkpoint_wal_is_safe_noop():
    # Should not raise on a healthy DB.
    checkpoint_wal()
