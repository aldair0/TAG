"""Drain pending Shopify outbound_change rows.

Mirrors the eBay outbound worker's structure. Differences:

- ``CREATE`` actions call ``publish_product`` and store the returned
  ``shopify_product_id`` / ``shopify_variant_id`` on the ``Product`` row
  (not just the channel_listing — the variant id is reused across
  channel_listings for the same product).
- Every kind (single, sealed, supply) is published — Shopify mirrors the
  full DB.
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
from app.sync.shopify.client import ShopifyClient

logger = logging.getLogger(__name__)


@dataclass
class OutboundResult:
    sync_run_id: int
    pulled: int = 0
    succeeded: int = 0
    failed: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_shopify_outbound(
    session: Session,
    client: ShopifyClient,
    *,
    batch_size: int = 50,
) -> OutboundResult:
    run = SyncRun(worker="shopify", direction="outbound", started_at=_now())
    session.add(run)
    session.flush()

    pending = (
        session.execute(
            select(OutboundChange)
            .where(
                OutboundChange.channel == Channel.SHOPIFY_POS.value,
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

        try:
            unit = _load_unit_with_product(session, change.inventory_unit_id)
            if unit is None:
                raise RuntimeError(
                    f"InventoryUnit {change.inventory_unit_id} not found for "
                    f"OutboundChange {change.id}"
                )
            product: Product = unit.product
            listing = _ensure_listing(session, unit.id)

            if change.action == OutboundAction.CREATE.value:
                published = client.publish_product(
                    sku=str(unit.id),
                    title=_listing_title(unit),
                    body_html=product.description,
                    # Local URL path under /images/. When real Shopify
                    # integration lands (Phase 5) this needs to be a
                    # publicly-reachable absolute URL — Shopify fetches
                    # the image during product creation.
                    image_url=product.image_url_path,
                    price=unit.unit_price,
                    quantity=unit.quantity_on_hand,
                    product_type=_product_type(product),
                )
                product.shopify_product_id = published.product_id
                product.shopify_variant_id = published.variant_id
                listing.external_listing_id = str(published.variant_id)
                listing.last_pushed_quantity = unit.quantity_on_hand
                listing.last_pushed_price = unit.unit_price

            elif change.action == OutboundAction.UPDATE_QTY.value:
                variant_id = product.shopify_variant_id
                if variant_id is None:
                    raise RuntimeError(
                        "Cannot update quantity on Shopify before the product "
                        "is published. Run create first."
                    )
                new_qty = int(change.payload["quantity"])
                client.update_quantity(
                    variant_id=variant_id, sku=str(unit.id), new_quantity=new_qty
                )
                listing.last_pushed_quantity = new_qty

            elif change.action == OutboundAction.UPDATE_PRICE.value:
                variant_id = product.shopify_variant_id
                if variant_id is None:
                    raise RuntimeError(
                        "Cannot update price on Shopify before the product is published."
                    )
                new_price = Decimal(change.payload["price"])
                client.update_price(
                    variant_id=variant_id, sku=str(unit.id), new_price=new_price
                )
                listing.last_pushed_price = new_price

            elif change.action == OutboundAction.END_LISTING.value:
                # Shopify "end listing" is "set quantity to 0" (the product
                # stays in the catalog as out-of-stock; the cashier sees that).
                if product.shopify_variant_id is not None:
                    client.update_quantity(
                        variant_id=product.shopify_variant_id,
                        sku=str(unit.id),
                        new_quantity=0,
                    )
                    listing.last_pushed_quantity = 0

            else:
                raise RuntimeError(f"Unknown action {change.action!r}")

            listing.sync_state = "ok"
            listing.last_synced_at = _now()
            listing.last_push_id = change.push_id
            change.completed_at = _now()
            change.last_error = None
            result.succeeded += 1

        except Exception as e:
            logger.exception("shopify outbound failed for change %s", change.id)
            change.last_error = f"{type(e).__name__}: {e}"
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
            ChannelListing.channel == Channel.SHOPIFY_POS.value,
        )
    ).scalar_one_or_none()
    if listing is None:
        listing = ChannelListing(
            inventory_unit_id=unit_id,
            channel=Channel.SHOPIFY_POS.value,
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


def _product_type(product: Product) -> str:
    if product.kind == "supply":
        return f"Supply / {product.supply_category or 'Other'}"
    if product.kind == "sealed":
        return f"Sealed / {product.sealed_subtype or 'Other'}"
    return "Single"
