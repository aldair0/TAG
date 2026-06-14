from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class Channel(str, enum.Enum):
    TCGPLAYER = "tcgplayer"
    EBAY = "ebay"
    SHOPIFY_POS = "shopify_pos"


class SyncState(str, enum.Enum):
    PENDING = "pending"
    OK = "ok"
    ERROR = "error"


class ChannelListing(Base, TimestampMixin):
    __tablename__ = "channel_listing"
    __table_args__ = (
        UniqueConstraint(
            "inventory_unit_id",
            "channel",
            name="channel_listing_unit_channel_uk",
        ),
        CheckConstraint(
            "channel IN ('tcgplayer','ebay','shopify_pos')",
            name="channel_listing_channel_check",
        ),
        CheckConstraint(
            "sync_state IN ('pending','ok','error')",
            name="channel_listing_sync_state_check",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_unit_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_unit.id", ondelete="CASCADE")
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    external_listing_id: Mapped[str | None] = mapped_column(String(64))

    last_pushed_quantity: Mapped[int | None] = mapped_column()
    last_pushed_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sync_state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    last_push_id: Mapped[str | None] = mapped_column(String(64))

    inventory_unit: Mapped["InventoryUnit"] = relationship(back_populates="channel_listings")

    def __repr__(self) -> str:
        return (
            f"<ChannelListing id={self.id} unit={self.inventory_unit_id} "
            f"channel={self.channel} qty={self.last_pushed_quantity}>"
        )
