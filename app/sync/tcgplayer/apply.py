"""Apply an IngestPlan to the database in a single transaction.

The diff engine produced data; this module mutates state. Splitting them
keeps the diff layer pure and easy to test, and concentrates all
write-side logic here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete as sql_delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    ChannelListing,
    InventoryUnit,
    OutboundChange,
    Product,
    ProductKind,
)
from app.outbound import (
    enqueue_for_new_unit,
    enqueue_for_price_change,
    enqueue_for_qty_change,
)
from app.sales import SaleLineInput, record_sale
from app.sync.tcgplayer.diff import IngestPlan
from app.sync.tcgplayer.parser import IngestRow

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    rows_inserted: int = 0
    rows_updated: int = 0
    sales_recorded: int = 0
    # CSV-detected sales/removals skipped because the unit carries an active
    # "sold online" flag — the manual Confirm/dismiss flow (or auto-expiry)
    # owns that resolution, so the CSV rotation must not clear it first.
    flags_preserved: int = 0


def apply_plan(plan: IngestPlan, session: Session) -> ApplyResult:
    """Apply an IngestPlan within the caller's transaction context.

    The session is flushed but **not committed**; the caller commits after
    any post-apply work (image fetching, sync_run finalization, etc.).
    """
    result = ApplyResult()

    # --- New products: create product + inventory_unit + channel_listing ---
    for row in plan.new_products:
        product = Product(
            tcgplayer_product_id=row.tcgplayer_product_id,
            kind=ProductKind.SEALED.value if row.kind == "sealed" else ProductKind.SINGLE.value,
            name=row.name,
            set=row.set,
            number=row.number,
            rarity=row.rarity,
            sealed_subtype=row.sealed_subtype,
            card_type=None,  # TCGPlayer CSV doesn't expose this directly; left for later.
            language="English",
            is_foil=False,
            is_online_listable=True,
            has_image=False,  # ProductImageFetcher flips this on success.
        )
        session.add(product)
        session.flush()  # need product.id for the FK

        unit = InventoryUnit(
            product_id=product.id,
            condition=row.condition,
            quantity_on_hand=row.quantity,
            reserve_quantity=row.reserve_quantity,
            unit_price=row.unit_price,
        )
        session.add(unit)
        session.flush()

        listing = ChannelListing(
            inventory_unit_id=unit.id,
            channel=Channel.TCGPLAYER.value,
            external_listing_id=str(row.tcgplayer_product_id),
            last_pushed_quantity=row.quantity,
            last_pushed_price=row.unit_price,
            sync_state="ok",
        )
        session.add(listing)

        # Fan out to other channels. TCGPlayer is excluded — we just learned
        # about the listing FROM TCGPlayer's CSV; pushing back would echo.
        enqueue_for_new_unit(session, unit, origin_channel=Channel.TCGPLAYER.value)

        result.rows_inserted += 1

    # --- New variants on an existing product (different condition) ---
    for row in plan.new_variants:
        product = _load_product_by_tcg_id(session, row.tcgplayer_product_id)
        if product is None:
            # Shouldn't happen — diff engine guarantees the product exists.
            continue
        unit = InventoryUnit(
            product_id=product.id,
            condition=row.condition,
            quantity_on_hand=row.quantity,
            reserve_quantity=row.reserve_quantity,
            unit_price=row.unit_price,
        )
        session.add(unit)
        session.flush()
        listing = ChannelListing(
            inventory_unit_id=unit.id,
            channel=Channel.TCGPLAYER.value,
            external_listing_id=str(row.tcgplayer_product_id),
            last_pushed_quantity=row.quantity,
            last_pushed_price=row.unit_price,
            sync_state="ok",
        )
        session.add(listing)
        enqueue_for_new_unit(session, unit, origin_channel=Channel.TCGPLAYER.value)
        result.rows_inserted += 1

    # --- Quantity changes on existing inventory_units ---
    # A *decrease* on the TCGPlayer side means a TCGPlayer-marketplace
    # sale; record it as a Sale so the audit trail is complete and let
    # record_sale handle the atomic decrement + fan-out. An *increase*
    # means the owner restocked on TCGPlayer; treat it as a plain update.
    for row, new_qty in plan.qty_changes:
        unit = _load_unit(session, row.tcgplayer_product_id, row.condition)
        if unit is None:
            continue
        old_qty = unit.quantity_on_hand
        listing = _load_listing(session, unit.id, Channel.TCGPLAYER.value)

        # For a decrease (a detected sale), re-read the unit so a sold-online
        # flag the receiver thread committed moments ago is visible — this
        # narrows the cross-thread TOCTOU on the preservation guard below.
        if new_qty < old_qty:
            session.refresh(unit)

        if new_qty < old_qty and unit.is_sold_online:
            # This unit is held by an active "sold online" flag. The manual
            # Confirm/dismiss flow (or auto-expiry) owns this sale — don't let
            # the CSV rotation record it and wipe the flag. Leave qty and the
            # channel_listing untouched so the discrepancy is re-evaluated on
            # the next sync once the flag has cleared.
            logger.info(
                "apply: preserved sold-online unit %d (%s): CSV qty %d < local %d "
                "— awaiting manual confirm/dismiss or auto-expiry",
                unit.id, unit.condition, new_qty, old_qty,
            )
            result.flags_preserved += 1
            continue

        if new_qty < old_qty:
            sold = old_qty - new_qty
            recorded = record_sale(
                session,
                channel=Channel.TCGPLAYER.value,
                lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=sold)],
            )
            result.sales_recorded += 1
            # record_sale already decremented + fanned out to other
            # channels. We still need to keep the TCGPlayer side's
            # channel_listing in sync (TCGPlayer told us the new qty,
            # so no outbound push back to TCGPlayer).
            if listing is not None:
                listing.last_pushed_quantity = new_qty
                listing.sync_state = "ok"
            # If a recorded oversell left the unit qty above new_qty
            # (because we couldn't decrement enough), force-align: the
            # TCGPlayer CSV is the ground truth for what was sold there.
            if recorded.had_oversell:
                unit.quantity_on_hand = new_qty
        else:
            unit.quantity_on_hand = new_qty
            if listing is not None:
                listing.last_pushed_quantity = new_qty
                listing.sync_state = "ok"
            enqueue_for_qty_change(
                session, unit, new_qty, origin_channel=Channel.TCGPLAYER.value
            )
        result.rows_updated += 1

    # --- Reserve-quantity changes on existing inventory_units ---
    # TCGPlayer's "Reserve" is a marketplace-side floor; it doesn't drive
    # sales math. Just keep our copy in sync. No outbound fan-out needed.
    for row, new_reserve in plan.reserve_changes:
        unit = _load_unit(session, row.tcgplayer_product_id, row.condition)
        if unit is None:
            continue
        unit.reserve_quantity = new_reserve
        # Don't double-count: a row with both qty and reserve change would
        # appear in both lists.
        if not any(r is row for r, _ in plan.qty_changes) and not any(
            r is row for r, _ in plan.price_changes
        ):
            result.rows_updated += 1

    # --- Price changes on existing inventory_units ---
    for row, new_price in plan.price_changes:
        unit = _load_unit(session, row.tcgplayer_product_id, row.condition)
        if unit is None:
            continue
        unit.unit_price = new_price
        listing = _load_listing(session, unit.id, Channel.TCGPLAYER.value)
        if listing is not None:
            listing.last_pushed_price = new_price
        enqueue_for_price_change(session, unit, new_price, origin_channel=Channel.TCGPLAYER.value)
        # Don't double-count: a row that had both qty AND price change is one
        # row in the source CSV but appears in two plan lists. Track uniquely.
        if not any(r is row for r, _ in plan.qty_changes):
            result.rows_updated += 1

    # --- Units removed from TCGPlayer CSV — record sale then delete ---
    # A listing absent from the CSV was de-listed on TCGPlayer (sold out or
    # manually removed). Record the remaining qty as a sale for the audit
    # trail, then purge every pending outbound_change for this unit so
    # nothing propagates to Shopify or eBay — TCGPlayer is the origin and
    # no fan-out is wanted. Delete the unit; orphaned products go too.
    for unit in plan.removed_units:
        if unit.is_sold_online:
            # Held by an active "sold online" flag — don't sell+delete it out
            # from under the manual Confirm/dismiss flow. It stays absent from
            # the CSV, so it reappears here each sync and is skipped until the
            # flag clears, after which a later sync reconciles it normally.
            logger.info(
                "apply: preserved sold-online unit %d (absent from CSV) "
                "— awaiting manual confirm/dismiss or auto-expiry",
                unit.id,
            )
            result.flags_preserved += 1
            continue

        if unit.quantity_on_hand > 0:
            record_sale(
                session,
                channel=Channel.TCGPLAYER.value,
                lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=unit.quantity_on_hand)],
            )
            result.sales_recorded += 1

        # Purge all pending outbound work for this unit — both pre-existing
        # rows and any just created by record_sale. CSV removals must not
        # fan out to other channels.
        session.execute(
            sql_delete(OutboundChange).where(
                OutboundChange.inventory_unit_id == unit.id,
                OutboundChange.completed_at.is_(None),
            )
        )

        product_id = unit.product_id
        session.delete(unit)
        session.flush()

        remaining = session.execute(
            select(func.count()).where(InventoryUnit.product_id == product_id)
        ).scalar_one()
        if remaining == 0:
            product = session.get(Product, product_id)
            if product is not None:
                session.delete(product)

        result.rows_updated += 1

    session.flush()
    return result


def _load_product_by_tcg_id(session: Session, tcg_id: int) -> Product | None:
    return session.execute(
        select(Product).where(Product.tcgplayer_product_id == tcg_id)
    ).scalar_one_or_none()


def _load_unit(
    session: Session, tcg_id: int, condition: str | None
) -> InventoryUnit | None:
    return session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(
            Product.tcgplayer_product_id == tcg_id,
            InventoryUnit.condition.is_(condition)
            if condition is None
            else InventoryUnit.condition == condition,
        )
    ).scalar_one_or_none()


def _load_listing(
    session: Session, inventory_unit_id: int, channel: str
) -> ChannelListing | None:
    return session.execute(
        select(ChannelListing).where(
            ChannelListing.inventory_unit_id == inventory_unit_id,
            ChannelListing.channel == channel,
        )
    ).scalar_one_or_none()
