from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin, _now

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class ConflictKind:
    OVERSELL = "oversell"
    LISTING_NOT_FOUND = "listing_not_found"
    SYNC_FAILED = "sync_failed"


class Conflict(Base, TimestampMixin):
    """A human-resolvable problem surfaced from automated sync work.

    Phase 3 writes oversell rows when an atomic decrement returns 0 rows
    affected. Later phases will surface listing-not-found from the
    webhook receiver, etc.
    """

    __tablename__ = "conflict"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('oversell','listing_not_found','sync_failed')",
            name="conflict_kind_check",
        ),
        CheckConstraint(
            "status IN ('open','resolved','ignored')",
            name="conflict_status_check",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)

    channel: Mapped[str | None] = mapped_column(String(16))
    inventory_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_unit.id", ondelete="SET NULL")
    )
    external_order_id: Mapped[str | None] = mapped_column(String(64))

    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_notes: Mapped[str | None] = mapped_column(Text)

    inventory_unit: Mapped["InventoryUnit | None"] = relationship()

    def __repr__(self) -> str:
        return f"<Conflict id={self.id} {self.kind}/{self.status} {self.summary!r}>"
