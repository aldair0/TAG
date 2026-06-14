from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin, _now

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class OutboundAction(str, enum.Enum):
    CREATE = "create"
    UPDATE_QTY = "update_qty"
    UPDATE_PRICE = "update_price"
    END_LISTING = "end_listing"


class OutboundChange(Base, TimestampMixin):
    __tablename__ = "outbound_change"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('tcgplayer','ebay','shopify_pos')",
            name="outbound_change_channel_check",
        ),
        CheckConstraint(
            "action IN ('create','update_qty','update_price','end_listing')",
            name="outbound_change_action_check",
        ),
        Index(
            "outbound_change_pending_idx",
            "channel",
            "enqueued_at",
            sqlite_where=None,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    inventory_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_unit.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON)

    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    push_id: Mapped[str | None] = mapped_column(String(64))

    inventory_unit: Mapped["InventoryUnit | None"] = relationship()

    @property
    def is_pending(self) -> bool:
        return self.completed_at is None

    def __repr__(self) -> str:
        state = "done" if self.completed_at else ("error" if self.last_error else "pending")
        return (
            f"<OutboundChange id={self.id} {self.channel}/{self.action} "
            f"unit={self.inventory_unit_id} {state}>"
        )
