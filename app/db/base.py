from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


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
            cur.close()
    return eng


engine: Engine = make_engine()
