from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class AdjustmentReason(str, enum.Enum):
    RESTOCK = "restock"
    DAMAGE = "damage"
    THEFT = "theft"
    CORRECTION = "correction"
    SHRINKAGE = "shrinkage"
    OTHER = "other"


class InventoryAdjustment(Base):
    __tablename__ = "inventory_adjustment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inventory_unit_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("inventory_unit.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Positive = stock added (restock), negative = stock removed (damage, shrinkage, etc.)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default="CURRENT_TIMESTAMP",
    )

    unit: Mapped["InventoryUnit"] = relationship("InventoryUnit", back_populates="adjustments")
