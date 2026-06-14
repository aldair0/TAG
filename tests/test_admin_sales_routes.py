"""Smoke tests for the Phase 3 admin pages: sales log + conflicts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.db.models import Channel
from app.sales import SaleLineInput, record_sale
from app.sync.ebay import EbayOrder, EbayOrderLine, LoggingMockEbayClient, run_ebay_inbound
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from sqlalchemy import select

from app.db.models import InventoryUnit
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(session) -> None:
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def _first_unit(session) -> InventoryUnit:
    return session.execute(
        select(InventoryUnit).order_by(InventoryUnit.id).limit(1)
    ).scalar_one()


def test_sales_index_empty(client):
    r = client.get("/admin/sales/")
    assert r.status_code == 200
    assert "No sales recorded yet" in r.text


def test_sales_index_after_ebay_inbound(client, session):
    _ingest(session)
    unit = _first_unit(session)
    order = EbayOrder(
        order_id="EB-T1",
        created_at=datetime.now(timezone.utc),
        lines=[EbayOrderLine(sku=str(unit.id), quantity=1, unit_price=Decimal("4.99"))],
        total=Decimal("4.99"),
    )
    run_ebay_inbound(session, LoggingMockEbayClient(orders=[order]))

    r = client.get("/admin/sales/")
    assert r.status_code == 200
    assert "EB-T1" in r.text
    assert "ebay" in r.text


def test_sales_filter_by_channel(client, session):
    _ingest(session)
    unit = _first_unit(session)
    record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        external_order_id="SHOP-T1",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=1)],
    )
    session.commit()

    r = client.get("/admin/sales/?channel=ebay")
    assert r.status_code == 200
    assert "SHOP-T1" not in r.text  # filtered out

    r = client.get("/admin/sales/?channel=shopify_pos")
    assert "SHOP-T1" in r.text


def test_conflicts_index_empty(client):
    r = client.get("/admin/sales/conflicts")
    assert r.status_code == 200
    assert "No conflicts" in r.text


def test_conflicts_index_after_oversell(client, session):
    _ingest(session)
    unit = _first_unit(session)
    # Force an oversell.
    record_sale(
        session,
        channel=Channel.EBAY.value,
        external_order_id="EB-OVER",
        lines=[SaleLineInput(inventory_unit_id=unit.id, quantity=999)],
    )
    session.commit()

    r = client.get("/admin/sales/conflicts")
    assert r.status_code == 200
    assert "oversell" in r.text
    assert "EB-OVER" in r.text


def test_run_ebay_inbound_button_redirects_to_sales(client, session):
    _ingest(session)
    r = client.post("/admin/sync/run_ebay_inbound", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/admin/sales/")
