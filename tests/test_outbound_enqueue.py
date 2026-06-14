"""Outbound enqueue helpers + integration with Phase 1 ingest."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    Channel,
    InventoryUnit,
    OutboundAction,
    OutboundChange,
    Product,
    ProductKind,
)
from app.outbound import (
    enqueue_for_new_unit,
    enqueue_for_price_change,
    enqueue_for_qty_change,
)
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _seed_unit(session, *, kind="single", is_online_listable=True, qty=1, price="1.00") -> InventoryUnit:
    p = Product(
        tcgplayer_product_id=999_001 if kind != "supply" else None,
        kind=kind,
        name="Test",
        is_online_listable=is_online_listable,
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


def _outbound_channels(session) -> dict[str, list[OutboundChange]]:
    rows = session.execute(select(OutboundChange)).scalars().all()
    out: dict[str, list[OutboundChange]] = {}
    for r in rows:
        out.setdefault(r.channel, []).append(r)
    return out


def test_enqueue_new_unit_for_single_fans_out_to_ebay_and_shopify(session):
    unit = _seed_unit(session, kind="single")
    rows = enqueue_for_new_unit(session, unit, origin_channel=Channel.TCGPLAYER.value)
    by_channel = {r.channel for r in rows}
    assert by_channel == {Channel.EBAY.value, Channel.SHOPIFY_POS.value}
    assert all(r.action == OutboundAction.CREATE.value for r in rows)


def test_enqueue_new_unit_for_supply_only_shopify(session):
    unit = _seed_unit(session, kind="supply", is_online_listable=False)
    rows = enqueue_for_new_unit(session, unit)
    assert {r.channel for r in rows} == {Channel.SHOPIFY_POS.value}


def test_enqueue_qty_change_excludes_origin_channel(session):
    unit = _seed_unit(session, kind="single")
    rows = enqueue_for_qty_change(session, unit, 5, origin_channel=Channel.TCGPLAYER.value)
    assert {r.channel for r in rows} == {Channel.EBAY.value, Channel.SHOPIFY_POS.value}
    for r in rows:
        assert r.payload == {"quantity": 5}


def test_enqueue_qty_change_from_shopify_excludes_shopify(session):
    unit = _seed_unit(session, kind="single")
    rows = enqueue_for_qty_change(session, unit, 0, origin_channel=Channel.SHOPIFY_POS.value)
    assert {r.channel for r in rows} == {Channel.EBAY.value, Channel.TCGPLAYER.value}


def test_enqueue_price_change_carries_decimal(session):
    unit = _seed_unit(session, kind="single", price="2.00")
    rows = enqueue_for_price_change(session, unit, Decimal("3.50"), origin_channel=Channel.TCGPLAYER.value)
    for r in rows:
        assert r.payload == {"price": "3.50"}
        assert r.action == OutboundAction.UPDATE_PRICE.value


def test_phase1_ingest_fans_out_to_ebay_and_shopify_only(session):
    """After ingesting the fixture, every new unit has ebay+shopify_pos
    outbound rows but NOT tcgplayer (origin)."""
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )

    grouped = _outbound_channels(session)
    # 15 units → 15 ebay create + 15 shopify_pos create
    assert len(grouped.get(Channel.EBAY.value, [])) == 15
    assert len(grouped.get(Channel.SHOPIFY_POS.value, [])) == 15
    assert grouped.get(Channel.TCGPLAYER.value, []) == []  # never push back to origin


def test_phase1_qty_change_enqueues_updates(session):
    """v1 → v2 fixture diff yields qty changes; outbound queue picks them up."""
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )
    pre = session.execute(select(OutboundChange).where(OutboundChange.action == "update_qty")).scalars().all()
    assert pre == []

    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture_v2.csv")),
        session,
        image_cache=_NoopImageCache(),
    )

    qty_changes = session.execute(
        select(OutboundChange).where(OutboundChange.action == "update_qty")
    ).scalars().all()
    # 2 qty changes (501001 LP, 700001 sealed) × 2 channels (ebay + shopify) = 4
    assert len(qty_changes) == 4
    assert {r.channel for r in qty_changes} == {Channel.EBAY.value, Channel.SHOPIFY_POS.value}
