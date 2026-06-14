"""Diff incoming CSV rows against the current DB state.

Produces an :class:`IngestPlan` describing exactly which products need to be
created, which inventory_units are new, and which need quantity/price updates.
The plan is data-only — application is in apply.py — so this layer is easy
to test in isolation without a session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import Channel, ChannelListing, InventoryUnit, Product
from app.sync.tcgplayer.parser import IngestRow


@dataclass
class IngestPlan:
    new_products: list[IngestRow] = field(default_factory=list)
    new_variants: list[IngestRow] = field(default_factory=list)
    qty_changes: list[tuple[IngestRow, int]] = field(default_factory=list)
    price_changes: list[tuple[IngestRow, Decimal]] = field(default_factory=list)
    reserve_changes: list[tuple[IngestRow, int]] = field(default_factory=list)
    unchanged: list[IngestRow] = field(default_factory=list)
    # Units that had a TCGPlayer listing and qty > 0 in our DB but are
    # absent from the incoming CSV.  TCGPlayer removes a listing from the
    # export once its quantity reaches zero, so a disappearing row almost
    # always means the item sold completely on that channel.
    removed_units: list[InventoryUnit] = field(default_factory=list)

    def total_changes(self) -> int:
        return (
            len(self.new_products)
            + len(self.new_variants)
            + len(self.qty_changes)
            + len(self.price_changes)
            + len(self.reserve_changes)
            + len(self.removed_units)
        )

    def is_empty(self) -> bool:
        return self.total_changes() == 0


# SQLite caps a single bound-parameter list at 32766 (since 3.32). Real
# CSV imports easily blow past that, so we chunk WHERE...IN queries.
# Tests can monkeypatch this to exercise the boundary cheaply.
_IN_CHUNK_SIZE = 500


def build_plan(rows: Iterable[IngestRow], session: Session) -> IngestPlan:
    incoming = list(rows)

    # Pull every (tcgplayer_id → product) and existing inventory units in one round-trip.
    tcg_ids = {r.tcgplayer_product_id for r in incoming}
    existing_products: dict[int, Product] = {}
    existing_units: dict[tuple[int, str | None], InventoryUnit] = {}

    if tcg_ids:
        tcg_id_list = list(tcg_ids)
        for start in range(0, len(tcg_id_list), _IN_CHUNK_SIZE):
            chunk = tcg_id_list[start : start + _IN_CHUNK_SIZE]
            stmt = (
                select(Product)
                .where(Product.tcgplayer_product_id.in_(chunk))
                .options(joinedload(Product.inventory_units))
            )
            for product in session.execute(stmt).unique().scalars().all():
                existing_products[product.tcgplayer_product_id] = product
                for unit in product.inventory_units:
                    existing_units[
                        (product.tcgplayer_product_id, unit.condition)
                    ] = unit

    plan = IngestPlan()

    # When the same tcg_id appears multiple times in one CSV (e.g., same card
    # in two conditions), the FIRST occurrence creates the product and its
    # first variant; subsequent occurrences just add variants.
    seen_new_product_for: set[int] = set()

    for row in incoming:
        product = existing_products.get(row.tcgplayer_product_id)
        if product is None:
            if row.tcgplayer_product_id in seen_new_product_for:
                plan.new_variants.append(row)
            else:
                plan.new_products.append(row)
                seen_new_product_for.add(row.tcgplayer_product_id)
            continue

        unit = existing_units.get((row.tcgplayer_product_id, row.condition))
        if unit is None:
            plan.new_variants.append(row)
            continue

        changed = False
        if unit.quantity_on_hand != row.quantity:
            plan.qty_changes.append((row, row.quantity))
            changed = True
        if row.unit_price is not None and unit.unit_price != row.unit_price:
            plan.price_changes.append((row, row.unit_price))
            changed = True
        if unit.reserve_quantity != row.reserve_quantity:
            plan.reserve_changes.append((row, row.reserve_quantity))
            changed = True
        if not changed:
            plan.unchanged.append(row)

    # Identify TCGPlayer-linked units absent from the incoming CSV. A missing
    # row means the listing was de-listed (sold out or removed). These units
    # will be deleted from inventory — any remaining qty is recorded as a sale
    # first. Units already at qty=0 are included so stale ghost rows are also
    # cleaned up.
    incoming_keys: set[tuple[int, str | None]] = {
        (r.tcgplayer_product_id, r.condition) for r in incoming
    }
    all_tcg_units = session.execute(
        select(InventoryUnit)
        .join(ChannelListing, ChannelListing.inventory_unit_id == InventoryUnit.id)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(
            ChannelListing.channel == Channel.TCGPLAYER.value,
            Product.tcgplayer_product_id.isnot(None),
        )
        .options(joinedload(InventoryUnit.product))
    ).unique().scalars().all()
    for unit in all_tcg_units:
        key = (unit.product.tcgplayer_product_id, unit.condition)
        if key not in incoming_keys:
            plan.removed_units.append(unit)

    return plan
