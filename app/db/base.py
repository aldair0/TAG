from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Single declarative base for all ORM models. Phase 1 fills app/db/models/."""


def _ensure_sqlite_dir() -> None:
    p = settings.sqlite_path
    if p is not None:
        p.parent.mkdir(parents=True, exist_ok=True)


def make_engine() -> Engine:
    _ensure_sqlite_dir()
    eng = create_engine(
        settings.database_url,
        future=True,
        connect_args={"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {},
    )
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            # Wait up to 5s for a competing writer instead of failing
            # immediately with "database is locked". Critical now that the
            # email-receiver thread, the scheduler thread, and web requests
            # all write to a single-writer SQLite DB.
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return eng


engine: Engine = make_engine()


def integrity_check(eng: Engine | None = None) -> bool:
    """Run ``PRAGMA integrity_check`` at startup. Returns True if OK.

    Logs the result either way. A failure here means the DB is corrupt (e.g.
    after an unclean shutdown that WAL couldn't fully recover) and a restore
    from backup is warranted — surfaced rather than silently soldiered past.
    """
    eng = eng or engine
    if not settings.database_url.startswith("sqlite"):
        return True
    with eng.connect() as conn:
        result = conn.execute(text("PRAGMA integrity_check")).scalar()
    ok = result == "ok"
    if ok:
        logger.info("DB integrity_check: ok")
    else:
        logger.error("DB integrity_check FAILED: %s — restore from backup advised", result)
        try:
            from app.alerts import send_alert

            send_alert(
                "DATABASE corruption detected",
                f"PRAGMA integrity_check reported: {result}\n"
                "The SQLite database may be corrupt (e.g. after an unclean "
                "shutdown). Restore the most recent good backup from TAG_HOME/backups.",
                key="db_integrity",
            )
        except Exception:
            logger.exception("failed to send DB-integrity alert")
    return ok


def checkpoint_wal(eng: Engine | None = None) -> None:
    """Truncate the WAL so it can't grow unbounded over a long uptime.

    Safe to call periodically; a no-op when there's nothing to checkpoint.
    """
    eng = eng or engine
    if not settings.database_url.startswith("sqlite"):
        return
    with eng.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
