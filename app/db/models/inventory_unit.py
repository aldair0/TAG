from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.channel_listing import ChannelListing
    from app.db.models.inventory_adjustment import InventoryAdjustment
    from app.db.models.product import Product


class InventoryUnit(Base, TimestampMixin):
    __tablename__ = "inventory_unit"
    __table_args__ = (
        UniqueConstraint("product_id", "condition", name="inventory_unit_product_condition_uk"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id", ondelete="CASCADE"))

    condition: Mapped[str | None] = mapped_column(String(16))

    quantity_on_hand: Mapped[int] = mapped_column(default=0, nullable=False)
    # TCGPlayer's "reserve" — the floor below which the marketplace
    # listing won't sell. Display-only here; the marketplace enforces it.
    reserve_quantity: Mapped[int] = mapped_column(default=0, nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    last_local_edit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # "Sold online" flag — set when we receive an out-of-band signal (e.g. email)
    # that this unit was sold on another channel before the CSV sync could
    # update inventory. Blocks POS sale until manually dismissed or auto-expired.
    # sold_online_at: when the flag was set (UTC).
    # sold_online_until: auto-expiry (end of the calendar day after flagging,
    #   store-timezone, stored as UTC). NULL = not currently flagged.
    sold_online_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sold_online_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    product: Mapped["Product"] = relationship(back_populates="inventory_units")
    channel_listings: Mapped[list["ChannelListing"]] = relationship(
        back_populates="inventory_unit",
        cascade="all, delete-orphan",
    )
    adjustments: Mapped[list["InventoryAdjustment"]] = relationship(
        back_populates="unit",
        cascade="all, delete-orphan",
        order_by="InventoryAdjustment.created_at.desc()",
    )

    @property
    def is_sold_online(self) -> bool:
        """True while the sold-online flag is active (not expired, not dismissed)."""
        if not self.sold_online_until:
            return False
        expiry = self.sold_online_until
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < expiry

    def __repr__(self) -> str:
        return (
            f"<InventoryUnit id={self.id} product_id={self.product_id} "
            f"condition={self.condition!r} qty={self.quantity_on_hand}>"
        )
