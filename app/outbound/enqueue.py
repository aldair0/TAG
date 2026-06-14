"""Enqueue helpers — called when local state changes to put work on channel
workers' queues.

Channel selection rules:

- ``ebay``: only when ``product.is_online_listable`` (skips supplies).
- ``shopify_pos``: NOT included. Shopify is a payment terminal only —
  it does not manage inventory. Nothing is pushed to Shopify.
- ``tcgplayer``: only when the change did NOT originate from TCGPlayer
  (anti-echo rule).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    InventoryUnit,
    OutboundAction,
    OutboundChange,
    Product,
)


def _new_push_id() -> str:
    return uuid.uuid4().hex


def _channels_for(product: Product, *, exclude: str | None = None) -> list[str]:
    """Return the channels that should receive an outbound notification.

    Shopify is intentionally excluded — it is a payment processor, not
    an inventory system. Only eBay and TCGPlayer receive pushes.
    """
    channels: list[str] = []
    if product.is_online_listable and exclude != Channel.EBAY.value:
        channels.append(Channel.EBAY.value)
    if product.is_online_listable and exclude != Channel.TCGPLAYER.value:
        channels.append(Channel.TCGPLAYER.value)
    return channels


def enqueue_for_new_unit(
    session: Session,
    unit: InventoryUnit,
    *,
    origin_channel: str | None = None,
) -> list[OutboundChange]:
    product = unit.product or session.get(Product, unit.product_id)
    if product is None:
        return []
    candidate_channels = _channels_for(product, exclude=origin_channel)

    rows = []
    for ch in candidate_channels:
        row = OutboundChange(
            channel=ch,
            inventory_unit_id=unit.id,
            action=OutboundAction.CREATE.value,
            payload={
                "quantity": unit.quantity_on_hand,
                "price": str(unit.unit_price) if unit.unit_price is not None else None,
                "condition": unit.condition,
            },
            push_id=_new_push_id(),
        )
        session.add(row)
        rows.append(row)
    return rows


def enqueue_for_qty_change(
    session: Session,
    unit: InventoryUnit,
    new_quantity: int,
    *,
    origin_channel: str | None = None,
) -> list[OutboundChange]:
    product = unit.product or session.get(Product, unit.product_id)
    if product is None:
        return []
    candidate_channels = _channels_for(product, exclude=origin_channel)

    rows = []
    for ch in candidate_channels:
        row = OutboundChange(
            channel=ch,
            inventory_unit_id=unit.id,
            action=OutboundAction.UPDATE_QTY.value,
            payload={"quantity": new_quantity},
            push_id=_new_push_id(),
        )
        session.add(row)
        rows.append(row)
    return rows


def enqueue_for_price_change(
    session: Session,
    unit: InventoryUnit,
    new_price: Decimal,
    *,
    origin_channel: str | None = None,
) -> list[OutboundChange]:
    product = unit.product or session.get(Product, unit.product_id)
    if product is None:
        return []
    candidate_channels = _channels_for(product, exclude=origin_channel)

    rows = []
    for ch in candidate_channels:
        row = OutboundChange(
            channel=ch,
            inventory_unit_id=unit.id,
            action=OutboundAction.UPDATE_PRICE.value,
            payload={"price": str(new_price)},
            push_id=_new_push_id(),
        )
        session.add(row)
        rows.append(row)
    return rows
