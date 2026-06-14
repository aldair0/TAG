"""TCGPlayer CSV qty-decrease ⇒ Sale row + cross-channel fan-out.

The v1 → v2 fixture diff has two qty decreases:
- 501001 (Lightning Helix LP): 2 → 1 (one sold)
- 700001 (Bloomburrow Booster Box): 4 → 3 (one sold)
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    Channel,
    InventoryUnit,
    OutboundChange,
    Product,
    Sale,
    SaleLine,
)
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(path: str, session) -> None:
    run_ingest(
        FixtureTCGPlayerSource(Path(path)),
        session,
        image_cache=_NoopImageCache(),
    )


def test_v1_to_v2_records_two_tcgplayer_sales(session):
    _ingest("test_data/tcgplayer_fixture.csv", session)
    # Phase 1 ingest produces no sales — only initial stocking.
    pre = session.execute(select(Sale)).scalars().all()
    assert pre == []

    _ingest("test_data/tcgplayer_fixture_v2.csv", session)

    sales = session.execute(
        select(Sale).where(Sale.channel == Channel.TCGPLAYER.value)
    ).scalars().all()
    assert len(sales) == 2
    # Each sale has exactly one line (we record one sale per qty-changed
    # CSV row).
    for s in sales:
        assert len(s.lines) == 1
        assert s.lines[0].quantity_sold == 1


def test_v1_to_v2_decrements_inventory(session):
    _ingest("test_data/tcgplayer_fixture.csv", session)

    helix_lp = session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.tcgplayer_product_id == 501001, InventoryUnit.condition == "Lightly Played")
    ).scalar_one()
    box = session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.tcgplayer_product_id == 700001)
    ).scalar_one()
    assert helix_lp.quantity_on_hand == 2
    assert box.quantity_on_hand == 4

    _ingest("test_data/tcgplayer_fixture_v2.csv", session)

    session.refresh(helix_lp)
    session.refresh(box)
    assert helix_lp.quantity_on_hand == 1
    assert box.quantity_on_hand == 3


def test_v1_to_v2_fans_out_qty_updates_to_ebay_and_shopify_only(session):
    """A TCGPlayer-detected sale must NOT echo back to TCGPlayer."""
    _ingest("test_data/tcgplayer_fixture.csv", session)
    _ingest("test_data/tcgplayer_fixture_v2.csv", session)

    # Get the unit ids for the two qty changes
    helix_lp = session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.tcgplayer_product_id == 501001, InventoryUnit.condition == "Lightly Played")
    ).scalar_one()
    box = session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.tcgplayer_product_id == 700001)
    ).scalar_one()

    qty_updates = session.execute(
        select(OutboundChange).where(
            OutboundChange.action == "update_qty",
            OutboundChange.inventory_unit_id.in_([helix_lp.id, box.id]),
        )
    ).scalars().all()
    # 2 units × 2 channels (eBay + Shopify) = 4 update_qty rows. None for TCGPlayer.
    assert len(qty_updates) == 4
    assert {r.channel for r in qty_updates} == {Channel.EBAY.value, Channel.SHOPIFY_POS.value}


def test_v1_to_v2_increase_is_treated_as_restock_not_sale(session):
    """Build an artificial fixture in-memory: the same row but with a
    HIGHER quantity than the DB. Should NOT create a Sale."""
    _ingest("test_data/tcgplayer_fixture.csv", session)

    # Manually nudge the qty downward so v2's value is now an increase.
    box = session.execute(
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.tcgplayer_product_id == 700001)
    ).scalar_one()
    box.quantity_on_hand = 1
    session.commit()

    pre_sales = session.execute(select(Sale)).scalars().all()

    _ingest("test_data/tcgplayer_fixture.csv", session)  # v1: 700001 = 4 (now an increase)

    # The 700001 increase did NOT create a new sale (501001 LP
    # increase from 1 back to 2 also doesn't, since v1's csv has
    # 501001 LP=2 already in the DB).
    post_sales = session.execute(
        select(Sale).where(Sale.channel == "tcgplayer")
    ).scalars().all()
    assert len(post_sales) == len(pre_sales)
