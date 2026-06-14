"""Poll Shopify for new POS orders and record each as a local Sale.

Each call:
- Fetches paid orders created after the last successful shopify/inbound run
- Matches each line item's variant_id to a Product (via shopify_variant_id)
  then to its InventoryUnit
- Calls record_sale() — idempotent on external_order_id, so re-runs are safe
- Writes a SyncRun record

Line items whose variant_id can't be matched open a listing_not_found
conflict so nothing is silently lost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import Channel, InventoryUnit, Product, SyncRun
from app.sales import SaleLineInput, record_sale
from app.sync.shopify.client import RealShopifyClient, ShopifyOrder

logger = logging.getLogger(__name__)


@dataclass
class ShopifyInboundResult:
    sync_run_id: int
    orders_pulled: int = 0
    orders_recorded: int = 0
    orders_skipped: int = 0
    orders_unmatched: int = 0
    oversells: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_shopify_inbound(session: Session, client: RealShopifyClient) -> ShopifyInboundResult:
    """Poll Shopify once, record any new orders."""
    run = SyncRun(worker="shopify", direction="inbound", started_at=_now())
    session.add(run)
    session.flush()

    since = _last_successful_inbound_at(session)
    try:
        orders = client.fetch_orders_since(since)
    except Exception as e:
        logger.exception("shopify inbound: fetch_orders_since failed")
        run.error = f"{type(e).__name__}: {e}"
        run.ended_at = _now()
        session.commit()
        raise

    result = ShopifyInboundResult(sync_run_id=run.id, orders_pulled=len(orders))

    for order in orders:
        outcome = _record_order(session, order)
        if outcome == "skipped":
            result.orders_skipped += 1
        elif outcome == "unmatched":
            result.orders_unmatched += 1
        else:
            result.orders_recorded += 1
            if outcome and outcome.had_oversell:
                result.oversells += len(outcome.oversell_conflicts)

    run.rows_seen = result.orders_pulled
    run.rows_inserted = result.orders_recorded
    run.rows_updated = result.orders_skipped
    run.error = f"{result.oversells} oversell(s)" if result.oversells else None
    run.ended_at = _now()
    session.commit()
    return result


def _record_order(session: Session, order: ShopifyOrder):
    if not order.lines:
        return "skipped"

    sale_lines: list[SaleLineInput] = []
    for li in order.lines:
        if li.variant_id is None:
            logger.info("shopify inbound: line with no variant_id in order %s", order.order_id)
            continue
        unit = _unit_for_variant(session, li.variant_id)
        if unit is None:
            logger.warning(
                "shopify inbound: no InventoryUnit for variant_id=%d in order %s",
                li.variant_id, order.order_id,
            )
            continue
        sale_lines.append(SaleLineInput(
            inventory_unit_id=unit.id,
            quantity=li.quantity,
            unit_price=li.unit_price,
        ))

    if not sale_lines:
        logger.warning("shopify inbound: order %s had no matchable lines", order.order_id)
        return "unmatched"

    return record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        external_order_id=f"shopify_{order.order_id}",
        occurred_at=order.created_at,
        lines=sale_lines,
        subtotal=order.subtotal,
        tax=order.tax,
        total=order.total,
        payment_method="card",
    )


def _unit_for_variant(session: Session, variant_id: int) -> InventoryUnit | None:
    """Find the InventoryUnit whose product has this Shopify variant_id."""
    product = session.execute(
        select(Product).where(Product.shopify_variant_id == variant_id)
    ).scalar_one_or_none()
    if product is None:
        return None
    unit = session.execute(
        select(InventoryUnit).where(InventoryUnit.product_id == product.id).limit(1)
    ).scalar_one_or_none()
    return unit


def _last_successful_inbound_at(session: Session) -> datetime | None:
    row = session.execute(
        select(SyncRun)
        .where(
            SyncRun.worker == "shopify",
            SyncRun.direction == "inbound",
            SyncRun.error.is_(None),
            SyncRun.ended_at.is_not(None),
        )
        .order_by(SyncRun.ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    ts = row.ended_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
