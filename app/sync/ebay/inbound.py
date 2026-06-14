"""Poll eBay for new orders and record each as a Sale (decrementing
local inventory + fanning out to TCGPlayer + Shopify).

Each call:
- asks the client for orders since the timestamp of the last successful
  ``ebay/inbound`` SyncRun (or all orders on first run)
- maps each line's ``sku`` (which is the local inventory_unit.id) to a
  unit and calls :func:`record_sale`
- writes a SyncRun for the poll, and the channel_listing's
  ``last_pushed_quantity`` is updated by the resulting outbound fan-out.

If a line references a SKU we don't recognize, we open a
``listing_not_found`` conflict; the order is otherwise still recorded so
no audit data is lost.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Channel, SyncRun
from app.sales import SaleLineInput, record_sale
from app.sync.ebay.client import EbayClient, EbayOrder

logger = logging.getLogger(__name__)


@dataclass
class InboundResult:
    sync_run_id: int
    orders_pulled: int = 0
    orders_recorded: int = 0
    orders_skipped: int = 0  # already-seen duplicates
    oversells: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_ebay_inbound(session: Session, client: EbayClient) -> InboundResult:
    """Poll eBay once, record any new orders, return a summary."""
    run = SyncRun(worker="ebay", direction="inbound", started_at=_now())
    session.add(run)
    session.flush()

    since = _last_successful_inbound_at(session)
    try:
        orders = client.fetch_orders_since(since)
    except Exception as e:
        logger.exception("ebay inbound: fetch_orders_since failed")
        run.error = f"{type(e).__name__}: {e}"
        run.ended_at = _now()
        session.commit()
        raise

    result = InboundResult(sync_run_id=run.id, orders_pulled=len(orders))

    for order in orders:
        recorded = _record_order(session, order)
        if recorded is None:
            result.orders_skipped += 1
        else:
            result.orders_recorded += 1
            if recorded.had_oversell:
                result.oversells += len(recorded.oversell_conflicts)

    run.rows_seen = result.orders_pulled
    run.rows_inserted = result.orders_recorded
    run.rows_updated = result.orders_skipped
    run.error = (
        f"{result.oversells} oversell(s)" if result.oversells else None
    )
    run.ended_at = _now()
    session.commit()
    return result


def _record_order(session: Session, order: EbayOrder):
    if not order.lines:
        logger.warning("ebay inbound: order %s had no lines", order.order_id)
        return None
    lines = [
        SaleLineInput(
            inventory_unit_id=int(li.sku),
            quantity=li.quantity,
            unit_price=li.unit_price,
        )
        for li in order.lines
    ]
    return record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id=order.order_id,
        occurred_at=order.created_at,
        total=order.total,
        lines=lines,
    )


def _last_successful_inbound_at(session: Session) -> datetime | None:
    row = session.execute(
        select(SyncRun)
        .where(
            SyncRun.worker == "ebay",
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
    # SQLite drops tzinfo; treat stored UTC timestamps as UTC again so
    # downstream comparisons against tz-aware order timestamps work.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
