from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._common import _now


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)

    worker: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows_seen: Mapped[int] = mapped_column(default=0, nullable=False)
    rows_inserted: Mapped[int] = mapped_column(default=0, nullable=False)
    rows_updated: Mapped[int] = mapped_column(default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"<SyncRun id={self.id} worker={self.worker} dir={self.direction} "
            f"seen={self.rows_seen} ins={self.rows_inserted} upd={self.rows_updated}>"
        )
