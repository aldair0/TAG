"""POS UI route smoke tests + end-to-end sale flow."""

from __future__ import annotations

from decimal import Decimal
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


def _ingest(session) -> None:
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def _first_unit(session) -> InventoryUnit:
    return session.execute(
        select(InventoryUnit)
        .order_by(InventoryUnit.id)
        .limit(1)
    ).scalar_one()


def test_pos_index_empty(client):
    r = client.get("/pos/")
    assert r.status_code == 200
    assert "Nothing matching that filter" in r.text


def test_pos_index_after_ingest(client, session):
    _ingest(session)
    r = client.get("/pos/")
    assert r.status_code == 200
    assert "Lightning Helix" in r.text
    assert "Add to cart" in r.text


def test_pos_search_filter(client, session):
    _ingest(session)
    r = client.get("/pos/?q=Pikachu")
    assert "Pikachu ex" in r.text
    assert "Lightning Helix" not in r.text


def test_pos_kind_filter_sealed(client, session):
    _ingest(session)
    r = client.get("/pos/?kind=sealed")
    assert "Booster Box" in r.text
    assert "Lightning Helix" not in r.text


def test_cart_empty_view(client):
    r = client.get("/pos/cart")
    assert r.status_code == 200
    assert "Cart is empty" in r.text


def test_add_to_cart_then_view_cart_shows_line(client, session):
    _ingest(session)
    unit = _first_unit(session)

    r = client.post(
        "/pos/cart/add",
        data={"inventory_unit_id": unit.id},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.get("/pos/cart")
    assert r.status_code == 200
    assert unit.product.name in r.text
    assert "Subtotal" in r.text


def test_cart_set_quantity_caps_at_qty_on_hand(client, session):
    _ingest(session)
    unit = _first_unit(session)
    initial_qty = unit.quantity_on_hand

    # Try to set qty beyond what's available — should cap.
    client.post(
        "/pos/cart/set",
        data={"inventory_unit_id": unit.id, "quantity": initial_qty + 999},
        follow_redirects=False,
    )
    r = client.get("/pos/cart")
    # Cap should land at initial_qty in the rendered input.
    assert f'value="{initial_qty}"' in r.text


def test_card_payment_adds_surcharge_line(client, session):
    _ingest(session)
    unit = _first_unit(session)
    client.post("/pos/cart/add", data={"inventory_unit_id": unit.id}, follow_redirects=False)

    r_card = client.get("/pos/cart?payment_method=card")
    assert "Card surcharge" in r_card.text

    r_cash = client.get("/pos/cart?payment_method=cash")
    assert "Card surcharge" not in r_cash.text


def test_checkout_records_shopify_sale_and_decrements(client, session):
    _ingest(session)
    unit = _first_unit(session)
    initial_qty = unit.quantity_on_hand

    client.post("/pos/cart/add", data={"inventory_unit_id": unit.id}, follow_redirects=False)
    r = client.post(
        "/pos/cart/checkout",
        data={"payment_method": "card"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/pos/checkout/done")

    session.refresh(unit)
    assert unit.quantity_on_hand == initial_qty - 1

    sales = session.execute(
        select(Sale).where(Sale.channel == Channel.SHOPIFY_POS.value)
    ).scalars().all()
    assert len(sales) == 1
    assert sales[0].payment_method == "card"
    assert sales[0].card_surcharge is not None and sales[0].card_surcharge > 0


def test_checkout_fans_out_to_ebay_and_tcgplayer(client, session):
    _ingest(session)
    unit = _first_unit(session)
    client.post("/pos/cart/add", data={"inventory_unit_id": unit.id}, follow_redirects=False)
    client.post("/pos/cart/checkout", data={"payment_method": "cash"}, follow_redirects=False)

    rows = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
        )
    ).scalars().all()
    assert {r.channel for r in rows} == {Channel.EBAY.value, Channel.TCGPLAYER.value}


def test_checkout_done_page_renders(client):
    r = client.get("/pos/checkout/done")
    assert r.status_code == 200
    assert "Sale recorded" in r.text


def test_cart_clear_empties(client, session):
    _ingest(session)
    unit = _first_unit(session)
    client.post("/pos/cart/add", data={"inventory_unit_id": unit.id}, follow_redirects=False)
    client.post("/pos/cart/clear", follow_redirects=False)
    r = client.get("/pos/cart")
    assert "Cart is empty" in r.text
