"""record_sale: atomic decrement, oversell, idempotency, fan-out."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.db.models import (
    Channel,
    Conflict,
    InventoryUnit,
    OutboundChange,
    Product,
    ProductKind,
    Sale,
    SaleLine,
)
from app.sales import SaleLineInput, record_sale


def _seed_unit(session, *, kind="single", qty=3, price="5.00", listable=True) -> InventoryUnit:
    p = Product(
        tcgplayer_product_id=900_001 if kind != "supply" else None,
        kind=kind,
        name="Test Card",
        is_online_listable=listable,
    )
    session.add(p)
    session.flush()
    u = InventoryUnit(
        product_id=p.id,
        condition="NM" if kind == "single" else None,
        quantity_on_hand=qty,
        unit_price=Decimal(price),
    )
    session.add(u)
    session.flush()
    return u


def test_record_sale_decrements_atomically(session):
    unit = _seed_unit(session, qty=3)
    record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-1",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=2)],
    )
    session.refresh(unit)
    assert unit.quantity_on_hand == 1


def test_record_sale_writes_sale_and_sale_line_rows(session):
    unit = _seed_unit(session, qty=3)
    recorded = record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-2",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1, unit_price=Decimal("9.99"))],
    )
    assert recorded.sale.id is not None
    assert recorded.sale.channel == "ebay"

    lines = session.execute(select(SaleLine).where(SaleLine.sale_id == recorded.sale.id)).scalars().all()
    assert len(lines) == 1
    assert lines[0].quantity_sold == 1
    assert lines[0].unit_price == Decimal("9.99")
    assert lines[0].title_at_sale  # populated


def test_oversell_creates_conflict_and_does_not_decrement(session):
    unit = _seed_unit(session, qty=1)
    recorded = record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-OVERSOLD",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=5)],
    )
    session.refresh(unit)
    assert unit.quantity_on_hand == 1  # NOT decremented
    assert recorded.had_oversell
    assert len(recorded.oversell_conflicts) == 1
    c = recorded.oversell_conflicts[0]
    assert c.kind == "oversell"
    assert c.status == "open"
    assert c.channel == "ebay"
    assert c.external_order_id == "ORDER-OVERSOLD"


def test_idempotent_on_duplicate_external_order_id(session):
    unit = _seed_unit(session, qty=5)
    record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-DEDUP",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-DEDUP",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    session.refresh(unit)
    assert unit.quantity_on_hand == 4  # Decremented exactly once

    sales = session.execute(select(Sale).where(Sale.external_order_id == "ORDER-DEDUP")).scalars().all()
    assert len(sales) == 1


def test_unknown_inventory_unit_creates_listing_not_found_conflict(session):
    recorded = record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-GHOST",
        lines=[SaleLineInput(inventory_unit_id=999_999, quantity=1)],
    )
    conflicts = session.execute(select(Conflict)).scalars().all()
    assert len(conflicts) == 1
    assert conflicts[0].kind == "listing_not_found"
    assert recorded.had_conflict
    assert not recorded.had_oversell  # this kind is listing_not_found, not oversell


def test_ebay_sale_fans_out_to_tcgplayer_and_shopify_only(session):
    unit = _seed_unit(session, qty=3)
    record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="ORDER-FAN",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    rows = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
        )
    ).scalars().all()
    channels = {r.channel for r in rows}
    assert channels == {Channel.TCGPLAYER.value, Channel.SHOPIFY_POS.value}
    for r in rows:
        assert r.payload == {"quantity": 2}  # post-decrement value


def test_tcgplayer_sale_fans_out_to_ebay_and_shopify_only(session):
    unit = _seed_unit(session, qty=3)
    record_sale(
        session,
        channel=Channel.TCGPLAYER.value,
        external_order_id=None,
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=2)],
    )
    rows = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
        )
    ).scalars().all()
    assert {r.channel for r in rows} == {Channel.EBAY.value, Channel.SHOPIFY_POS.value}


def test_shopify_pos_sale_fans_out_to_ebay_and_tcgplayer_only(session):
    unit = _seed_unit(session, qty=3)
    record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        external_order_id="SHOP-1",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    rows = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
        )
    ).scalars().all()
    assert {r.channel for r in rows} == {Channel.EBAY.value, Channel.TCGPLAYER.value}


def test_supply_walkin_sale_does_not_fan_out_to_online_channels(session):
    """A supply (kind=supply, not online listable) sold in-store should
    NOT enqueue eBay or TCGPlayer outbound rows — those channels never
    have a listing for it. Shopify is excluded because it's the origin."""
    unit = _seed_unit(session, kind="supply", qty=10, listable=False)
    record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        external_order_id="SHOP-SUPPLY-1",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    rows = session.execute(
        select(OutboundChange).where(OutboundChange.inventory_unit_id == unit.id)
    ).scalars().all()
    # No fan-out: supply isn't on eBay/TCGPlayer, and origin is Shopify.
    assert rows == []
