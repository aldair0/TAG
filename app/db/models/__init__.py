"""Importing this package registers all model classes on Base.metadata.

Alembic's ``env.py`` does ``import app.db.models`` to make autogenerate
discover them. Add new model modules below.
"""

from app.db.models.app_setting import AppSetting
from app.db.models.product import Product, ProductKind
from app.db.models.inventory_unit import InventoryUnit
from app.db.models.inventory_adjustment import AdjustmentReason, InventoryAdjustment
from app.db.models.channel_listing import Channel, ChannelListing, SyncState
from app.db.models.sync_run import SyncRun
from app.db.models.outbound_change import OutboundAction, OutboundChange
from app.db.models.sale import Sale, SaleLine
from app.db.models.conflict import Conflict, ConflictKind

__all__ = [
    "AdjustmentReason",
    "AppSetting",
    "Channel",
    "ChannelListing",
    "Conflict",
    "ConflictKind",
    "InventoryAdjustment",
    "InventoryUnit",
    "OutboundAction",
    "OutboundChange",
    "Product",
    "ProductKind",
    "Sale",
    "SaleLine",
    "SyncRun",
    "SyncState",
]
