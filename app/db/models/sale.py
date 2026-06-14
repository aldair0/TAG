from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin, _now

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class Sale(Base, TimestampMixin):
    """One row per completed transaction across any channel.

    For TCGPlayer / eBay sales we record what we know from the inbound
    signal (CSV diff or order poll). For Shopify POS sales (Phase 5) we
    record the Shopify order id so the webhook is idempotent.
    """

    __tablename__ = "sale"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('tcgplayer','ebay','shopify_pos')",
            name="sale_channel_check",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    card_surcharge: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    payment_method: Mapped[str | None] = mapped_column(String(16))  # 'card' / 'cash' / null
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["SaleLine"]] = relationship(
        back_populates="sale",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<Sale id={self.id} channel={self.channel} "
            f"ext={self.external_order_id} total={self.total}>"
        )


class SaleLine(Base):
    """One row per inventory_unit decremented by a sale."""

    __tablename__ = "sale_line"

    id: Mapped[int] = mapped_column(primary_key=True)

    sale_id: Mapped[int] = mapped_column(
        ForeignKey("sale.id", ondelete="CASCADE"), nullable=False
    )
    inventory_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_unit.id", ondelete="SET NULL")
    )

    quantity_sold: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    condition_at_sale: Mapped[str | None] = mapped_column(String(16))
    title_at_sale: Mapped[str | None] = mapped_column(String(255))

    sale: Mapped["Sale"] = relationship(back_populates="lines")
    inventory_unit: Mapped["InventoryUnit | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<SaleLine id={self.id} sale={self.sale_id} "
            f"unit={self.inventory_unit_id} qty={self.quantity_sold}>"
        )
