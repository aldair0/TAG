"""eBay inbound poller — orders → record_sale → fan-out."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db.models import (
    Channel,
    InventoryUnit,
    OutboundChange,
    Sale,
    SaleLine,
    SyncRun,
)
from app.sync.ebay import (
    EbayOrder,
    EbayOrderLine,
    LoggingMockEbayClient,
    run_ebay_inbound,
)
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(session):
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def _first_sellable_unit(session) -> InventoryUnit:
    return (
        session.execute(
            select(InventoryUnit).where(InventoryUnit.quantity_on_hand > 0).order_by(InventoryUnit.id)
        )
        .scalars()
        .first()
    )


def test_ebay_inbound_records_sale_and_decrements(session):
    _ingest(session)
    unit = _first_sellable_unit(session)
    initial_qty = unit.quantity_on_hand
    assert initial_qty > 0

    order = EbayOrder(
        order_id="EB-001",
        created_at=datetime.now(timezone.utc),
        lines=[EbayOrderLine(sku=str(unit.id), quantity=1, unit_price=Decimal("3.99"))],
        total=Decimal("3.99"),
    )
    result = run_ebay_inbound(session, LoggingMockEbayClient(orders=[order]))

    assert result.orders_pulled == 1
    assert result.orders_recorded == 1
    assert result.oversells == 0

    session.refresh(unit)
    assert unit.quantity_on_hand == initial_qty - 1

    sale = session.execute(select(Sale).where(Sale.external_order_id == "EB-001")).scalar_one()
    assert sale.channel == "ebay"
    lines = session.execute(select(SaleLine).where(SaleLine.sale_id == sale.id)).scalars().all()
    assert len(lines) == 1
    assert lines[0].quantity_sold == 1


def test_ebay_inbound_fans_out_to_tcgplayer_and_shopify(session):
    _ingest(session)
    unit = _first_sellable_unit(session)
    order = EbayOrder(
        order_id="EB-002",
        created_at=datetime.now(timezone.utc),
        lines=[EbayOrderLine(sku=str(unit.id), quantity=1)],
    )
    run_ebay_inbound(session, LoggingMockEbayClient(orders=[order]))

    rows = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
        )
    ).scalars().all()
    assert {r.channel for r in rows} == {Channel.TCGPLAYER.value, Channel.SHOPIFY_POS.value}


def test_ebay_inbound_idempotent_on_repeat_poll(session):
    _ingest(session)
    unit = _first_sellable_unit(session)
    initial = unit.quantity_on_hand

    order = EbayOrder(
        order_id="EB-DUPE",
        created_at=datetime.now(timezone.utc),
        lines=[EbayOrderLine(sku=str(unit.id), quantity=1)],
    )
    client = LoggingMockEbayClient(orders=[order])
    run_ebay_inbound(session, client)
    run_ebay_inbound(session, client)  # second poll, same order

    session.refresh(unit)
    assert unit.quantity_on_hand == initial - 1  # only one decrement
    sales = session.execute(select(Sale).where(Sale.external_order_id == "EB-DUPE")).scalars().all()
    assert len(sales) == 1


def test_ebay_inbound_records_sync_run(session):
    _ingest(session)
    unit = _first_sellable_unit(session)
    order = EbayOrder(
        order_id="EB-RUN",
        created_at=datetime.now(timezone.utc),
        lines=[EbayOrderLine(sku=str(unit.id), quantity=1)],
    )
    run_ebay_inbound(session, LoggingMockEbayClient(orders=[order]))
    runs = session.execute(
        select(SyncRun).where(SyncRun.worker == "ebay", SyncRun.direction == "inbound")
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].rows_seen == 1
    assert runs[0].rows_inserted == 1
    assert runs[0].error is None


def test_ebay_inbound_no_orders_is_a_no_op(session):
    result = run_ebay_inbound(session, LoggingMockEbayClient(orders=[]))
    assert result.orders_pulled == 0
    assert result.orders_recorded == 0
