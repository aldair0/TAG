"""Drain pending eBay outbound_change rows.

Each call processes up to ``batch_size`` rows in enqueue order. Successful
rows are marked completed; failures bump ``attempts`` and record
``last_error`` but stay pending for retry.

Returns a small dataclass summary for logging / Admin UI display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import (
    Channel,
    ChannelListing,
    InventoryUnit,
    OutboundAction,
    OutboundChange,
    Product,
    SyncRun,
)
from app.sync.ebay.client import EbayClient

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


@dataclass
class OutboundResult:
    sync_run_id: int
    pulled: int = 0
    succeeded: int = 0
    failed: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_ebay_outbound(
    session: Session,
    client: EbayClient,
    *,
    batch_size: int = 50,
) -> OutboundResult:
    run = SyncRun(worker="ebay", direction="outbound", started_at=_now())
    session.add(run)
    session.flush()

    pending = (
        session.execute(
            select(OutboundChange)
            .where(
                OutboundChange.channel == Channel.EBAY.value,
                OutboundChange.completed_at.is_(None),
            )
            .order_by(OutboundChange.enqueued_at)
            .limit(batch_size)
        )
        .scalars()
        .all()
    )

    result = OutboundResult(sync_run_id=run.id, pulled=len(pending))

    for change in pending:
        change.attempted_at = _now()
        change.attempts += 1

        if change.attempts > MAX_ATTEMPTS:
            change.completed_at = _now()
            change.last_error = f"Gave up after {MAX_ATTEMPTS} failed attempts: {change.last_error}"
            logger.warning(
                "ebay outbound change %s gave up after %d attempts", change.id, MAX_ATTEMPTS
            )
            result.failed += 1
            continue

        try:
            unit = _load_unit_with_product(session, change.inventory_unit_id)
            if unit is None:
                raise RuntimeError(
                    f"InventoryUnit {change.inventory_unit_id} not found for "
                    f"OutboundChange {change.id}"
                )
            listing = _ensure_listing(session, unit.id)

            if change.action == OutboundAction.CREATE.value:
                external_id = client.publish_listing(
                    sku=str(unit.id),
                    title=_listing_title(unit),
                    price=unit.unit_price,
                    quantity=unit.quantity_on_hand,
                )
                listing.external_listing_id = external_id
                listing.last_pushed_quantity = unit.quantity_on_hand
                listing.last_pushed_price = unit.unit_price

            elif change.action == OutboundAction.UPDATE_QTY.value:
                new_qty = int(change.payload["quantity"])
                client.update_quantity(
                    external_listing_id=listing.external_listing_id or _placeholder_id(unit),
                    sku=str(unit.id),
                    new_quantity=new_qty,
                )
                listing.last_pushed_quantity = new_qty

            elif change.action == OutboundAction.UPDATE_PRICE.value:
                new_price = Decimal(change.payload["price"])
                client.update_price(
                    external_listing_id=listing.external_listing_id or _placeholder_id(unit),
                    sku=str(unit.id),
                    new_price=new_price,
                )
                listing.last_pushed_price = new_price

            elif change.action == OutboundAction.END_LISTING.value:
                if listing.external_listing_id:
                    client.end_listing(
                        external_listing_id=listing.external_listing_id,
                        sku=str(unit.id),
                    )

            else:
                raise RuntimeError(f"Unknown action {change.action!r}")

            listing.sync_state = "ok"
            listing.last_synced_at = _now()
            listing.last_push_id = change.push_id
            change.completed_at = _now()
            change.last_error = None
            result.succeeded += 1

        except Exception as e:
            logger.exception("ebay outbound failed for change %s", change.id)
            change.last_error = f"{type(e).__name__}: {e}"
            # Mark the channel_listing as errored if we have one.
            if change.inventory_unit_id is not None:
                listing = _ensure_listing(session, change.inventory_unit_id)
                listing.sync_state = "error"
            result.failed += 1

    run.rows_seen = result.pulled
    run.rows_inserted = result.succeeded
    run.rows_updated = 0
    run.error = f"{result.failed} failed" if result.failed else None
    run.ended_at = _now()
    session.commit()
    return result


def _load_unit_with_product(session: Session, unit_id: int | None) -> InventoryUnit | None:
    if unit_id is None:
        return None
    return session.execute(
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.id == unit_id)
    ).scalar_one_or_none()


def _ensure_listing(session: Session, unit_id: int) -> ChannelListing:
    listing = session.execute(
        select(ChannelListing).where(
            ChannelListing.inventory_unit_id == unit_id,
            ChannelListing.channel == Channel.EBAY.value,
        )
    ).scalar_one_or_none()
    if listing is None:
        listing = ChannelListing(
            inventory_unit_id=unit_id,
            channel=Channel.EBAY.value,
            sync_state="pending",
        )
        session.add(listing)
        session.flush()
    return listing


def _listing_title(unit: InventoryUnit) -> str:
    p: Product = unit.product
    parts = [p.name]
    if unit.condition:
        parts.append(f"({unit.condition})")
    if p.set:
        parts.append(f"— {p.set}")
    return " ".join(parts)


def _placeholder_id(unit: InventoryUnit) -> str:
    """If a quantity update is attempted before the create succeeded, we
    don't have an external_listing_id yet. Use a placeholder so the mock
    client still gets called and the worker can move on; the create will
    be retried on the next run."""
    return f"PENDING-{unit.id}"
