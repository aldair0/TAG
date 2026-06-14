"""Central sale-recording engine.

Every channel that detects a sale (eBay poll, TCGPlayer CSV diff, Shopify
webhook in Phase 5) calls :func:`record_sale`. The engine:

1. Atomically decrements ``inventory_unit.quantity_on_hand`` per line,
   using a guarded UPDATE (``WHERE qty >= :n``). Zero rows affected ⇒
   oversell ⇒ a :class:`Conflict` row, but the sale itself is still
   recorded for audit.
2. Writes the :class:`Sale` + :class:`SaleLine` rows.
3. Fans out :class:`OutboundChange` rows to every channel **except** the
   originating one — that's the "DB is master, propagate to others" rule.

Channels themselves are responsible for skipping listings that don't
exist there (supplies have no eBay/TCGPlayer listing); that filtering
lives in ``app.outbound.enqueue``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.db.models import (
    Conflict,
    ConflictKind,
    InventoryUnit,
    Product,
    ProductKind,
    Sale,
    SaleLine,
)
from app.outbound import enqueue_for_qty_change

logger = logging.getLogger(__name__)


@dataclass
class SaleLineInput:
    inventory_unit_id: int
    quantity: int
    unit_price: Decimal | None = None  # falls back to unit's current price


@dataclass
class RecordedSale:
    sale: Sale
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def had_oversell(self) -> bool:
        return any(c.kind == ConflictKind.OVERSELL for c in self.conflicts)

    @property
    def had_conflict(self) -> bool:
        return bool(self.conflicts)

    # Back-compat alias for callers expecting the old name.
    @property
    def oversell_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.kind == ConflictKind.OVERSELL]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_sale(
    session: Session,
    *,
    channel: str,
    lines: list[SaleLineInput],
    external_order_id: str | None = None,
    occurred_at: datetime | None = None,
    payment_method: str | None = None,
    subtotal: Decimal | None = None,
    tax: Decimal | None = None,
    card_surcharge: Decimal | None = None,
    total: Decimal | None = None,
    notes: str | None = None,
) -> RecordedSale:
    """Record a sale that already happened on ``channel``.

    The session is **flushed** but not committed — caller wraps the
    transaction. This lets a poller record many sales in one commit
    while still being able to roll back on a downstream error.
    """
    if not lines:
        raise ValueError("record_sale: lines must be non-empty")

    # Idempotency: if an external_order_id was provided and we already
    # have a sale for it, return the existing record without re-decrementing.
    if external_order_id is not None:
        existing = session.execute(
            select(Sale).where(
                Sale.channel == channel,
                Sale.external_order_id == external_order_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "record_sale: skipping duplicate %s/%s", channel, external_order_id
            )
            return RecordedSale(sale=existing)

    sale = Sale(
        channel=channel,
        external_order_id=external_order_id,
        occurred_at=occurred_at or _now(),
        payment_method=payment_method,
        subtotal=subtotal,
        tax=tax,
        card_surcharge=card_surcharge,
        total=total,
        notes=notes,
    )
    session.add(sale)
    session.flush()

    conflicts: list[Conflict] = []

    for li in lines:
        unit = session.execute(
            select(InventoryUnit)
            .options(joinedload(InventoryUnit.product))
            .where(InventoryUnit.id == li.inventory_unit_id)
        ).scalar_one_or_none()

        if unit is None:
            conflicts.append(
                _conflict(
                    session,
                    sale=sale,
                    channel=channel,
                    inventory_unit_id=None,  # FK would fail — unit doesn't exist
                    summary=(
                        f"Sale references unknown inventory_unit "
                        f"id={li.inventory_unit_id}"
                    ),
                    details={
                        "channel": channel,
                        "quantity": li.quantity,
                        "missing_unit_id": li.inventory_unit_id,
                    },
                    kind=ConflictKind.LISTING_NOT_FOUND,
                )
            )
            continue

        product: Product = unit.product
        title = _line_title(unit, product)

        # Supplies always "in stock" — record the sale but leave qty alone.
        if product.kind == ProductKind.SUPPLY.value:
            decremented = True
        else:
            decremented = _atomic_decrement(session, unit.id, li.quantity)
        sale_line = SaleLine(
            sale_id=sale.id,
            inventory_unit_id=unit.id,
            quantity_sold=li.quantity,
            unit_price=li.unit_price if li.unit_price is not None else unit.unit_price,
            condition_at_sale=unit.condition,
            title_at_sale=title,
        )
        session.add(sale_line)

        if not decremented:
            # Oversell: someone else already drained this. Record the
            # sale_line for audit, but the inventory_unit qty stays
            # untouched and we open a conflict so staff can refund.
            conflicts.append(
                _conflict(
                    session,
                    sale=sale,
                    channel=channel,
                    inventory_unit_id=unit.id,
                    summary=(
                        f"Oversell: {channel} sold {li.quantity}× "
                        f"{title!r} but local qty was "
                        f"{unit.quantity_on_hand}"
                    ),
                    details={
                        "channel": channel,
                        "external_order_id": external_order_id,
                        "requested_qty": li.quantity,
                        "available_qty": unit.quantity_on_hand,
                    },
                    kind=ConflictKind.OVERSELL,
                )
            )
            continue

        # Successful decrement: refresh the in-memory unit qty and fan
        # out to every other channel.
        session.refresh(unit)
        enqueue_for_qty_change(
            session, unit, unit.quantity_on_hand, origin_channel=channel
        )

    session.flush()
    return RecordedSale(sale=sale, conflicts=conflicts)


def _atomic_decrement(session: Session, unit_id: int, quantity: int) -> bool:
    """Guarded ``UPDATE inventory_unit SET qty=qty-:n WHERE qty >= :n``.

    Returns True if the row was decremented, False on oversell.
    """
    if quantity <= 0:
        raise ValueError(f"record_sale: line quantity must be positive, got {quantity}")
    stmt = (
        update(InventoryUnit)
        .where(
            InventoryUnit.id == unit_id,
            InventoryUnit.quantity_on_hand >= quantity,
        )
        .values(quantity_on_hand=InventoryUnit.quantity_on_hand - quantity)
    )
    result = session.execute(stmt)
    return result.rowcount == 1


def _conflict(
    session: Session,
    *,
    sale: Sale,
    channel: str,
    inventory_unit_id: int | None,
    summary: str,
    details: dict,
    kind: str,
) -> Conflict:
    c = Conflict(
        kind=kind,
        status="open",
        channel=channel,
        inventory_unit_id=inventory_unit_id,
        external_order_id=sale.external_order_id,
        summary=summary,
        details=details,
    )
    session.add(c)
    session.flush()
    return c


def _line_title(unit: InventoryUnit, product: Product) -> str:
    parts = [product.name]
    if unit.condition:
        parts.append(f"({unit.condition})")
    if product.set:
        parts.append(f"— {product.set}")
    return " ".join(parts)
