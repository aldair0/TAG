"""Tiny key/value table for runtime-toggleable application settings.

Used for flags that need to be editable without a server restart — currently
just the TCGPlayer auto-sync on/off switch. If we end up with more than ~5
keys, revisit and split into purpose-specific tables.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_setting"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
