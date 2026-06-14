"""Supplies admin form: create + Shopify-only fan-out."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import (
    Channel,
    InventoryUnit,
    OutboundChange,
    Product,
    ProductKind,
)


def test_supplies_index_empty(client):
    r = client.get("/admin/supplies/")
    assert r.status_code == 200
    assert "No supplies yet" in r.text


def test_create_supply_writes_product_and_unit(client, session):
    r = client.post(
        "/admin/supplies/",
        data={
            "name": "Dragon Shield Matte Black 100ct",
            "supply_category": "Sleeves",
            "unit_price": "12.99",
            "quantity": "10",
            "description": "100 sleeves",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    products = session.execute(
        select(Product).where(Product.kind == ProductKind.SUPPLY.value)
    ).scalars().all()
    assert len(products) == 1
    p = products[0]
    assert p.name == "Dragon Shield Matte Black 100ct"
    assert p.supply_category == "Sleeves"
    assert p.is_online_listable is False  # supplies never go online

    units = session.execute(
        select(InventoryUnit).where(InventoryUnit.product_id == p.id)
    ).scalars().all()
    assert len(units) == 1
    assert units[0].quantity_on_hand == 10


def test_create_supply_only_enqueues_shopify_outbound(client, session):
    client.post(
        "/admin/supplies/",
        data={
            "name": "Generic Dice Set",
            "supply_category": "Dice",
            "unit_price": "5.00",
            "quantity": "20",
        },
        follow_redirects=False,
    )
    rows = session.execute(select(OutboundChange)).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel == Channel.SHOPIFY_POS.value
    assert rows[0].action == "create"


def test_supplies_appear_on_supplies_page(client, session):
    client.post(
        "/admin/supplies/",
        data={
            "name": "Test Sleeve",
            "supply_category": "Sleeves",
            "unit_price": "8.50",
            "quantity": "5",
        },
        follow_redirects=False,
    )
    r = client.get("/admin/supplies/")
    assert "Test Sleeve" in r.text
    assert "8.50" in r.text


def test_supplies_appear_in_pos_browse(client, session):
    client.post(
        "/admin/supplies/",
        data={
            "name": "Big Playmat",
            "supply_category": "Playmat",
            "unit_price": "19.99",
            "quantity": "3",
        },
        follow_redirects=False,
    )
    r = client.get("/pos/?kind=supply")
    assert r.status_code == 200
    assert "Big Playmat" in r.text


def test_bad_price_redirects_with_error(client):
    r = client.post(
        "/admin/supplies/",
        data={
            "name": "Bad",
            "supply_category": "Sleeves",
            "unit_price": "not-a-number",
            "quantity": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=bad_price" in r.headers["location"]
