"""End-to-end smoke test of the demo seeder + admin/POS round-trip."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import (
    Channel,
    ChannelListing,
    Conflict,
    InventoryUnit,
    OutboundChange,
    Product,
    ProductKind,
    Sale,
)


def test_seed_button_populates_inventory_and_drains_queues(client, session):
    r = client.post("/admin/demo/seed", follow_redirects=False)
    assert r.status_code == 303

    # Cards + sealed + supplies all present.
    products = session.execute(select(Product)).scalars().all()
    cards = [p for p in products if p.kind == ProductKind.SINGLE.value]
    sealed = [p for p in products if p.kind == ProductKind.SEALED.value]
    supplies = [p for p in products if p.kind == ProductKind.SUPPLY.value]
    assert len(cards) >= 20
    assert len(sealed) >= 5
    assert len(supplies) == 10

    # Outbound queues drained: nothing pending, every channel_listing
    # for cards/sealed has sync_state=ok.
    pending = session.execute(
        select(OutboundChange).where(OutboundChange.completed_at.is_(None))
    ).scalars().all()
    assert pending == []

    # ChannelListings are per inventory_unit (one card in multiple
    # conditions = multiple units = multiple listings on each channel).
    units = session.execute(select(InventoryUnit)).scalars().all()
    online_units = [
        u for u in units
        if next((p for p in products if p.id == u.product_id)).is_online_listable
    ]
    supply_units = [u for u in units if u not in online_units]

    listings = session.execute(select(ChannelListing)).scalars().all()
    by_channel = {ch.value: 0 for ch in Channel}
    for cl in listings:
        by_channel[cl.channel] += 1
        assert cl.sync_state == "ok", f"non-ok listing: {cl}"
    assert by_channel[Channel.SHOPIFY_POS.value] == len(units)
    assert by_channel[Channel.EBAY.value] == len(online_units)
    assert by_channel[Channel.TCGPLAYER.value] == len(online_units)
    # Supplies never get an eBay or TCGPlayer listing.
    assert len(supply_units) == 10


def test_seed_then_v2_records_tcgplayer_sales(client, session):
    client.post("/admin/demo/seed", follow_redirects=False)
    pre_sales = session.execute(select(Sale)).scalars().all()
    assert pre_sales == []

    r = client.post("/admin/demo/seed_v2", follow_redirects=False)
    assert r.status_code == 303

    sales = session.execute(
        select(Sale).where(Sale.channel == Channel.TCGPLAYER.value)
    ).scalars().all()
    # v1 → v2 has 5 qty decreases (501001 NM, 602002, 710001, 800001, 800006).
    assert len(sales) == 5

    # The 5 sales fan out to eBay + Shopify (10 update_qty rows total).
    qty_updates = session.execute(
        select(OutboundChange).where(OutboundChange.action == "update_qty")
    ).scalars().all()
    # All should be on eBay or Shopify (never TCGPlayer — origin).
    assert {r.channel for r in qty_updates} <= {
        Channel.EBAY.value,
        Channel.SHOPIFY_POS.value,
    }
    assert len(qty_updates) >= 10


def test_seed_idempotent_supplies_not_duplicated(client, session):
    client.post("/admin/demo/seed", follow_redirects=False)
    first_supplies = session.execute(
        select(Product).where(Product.kind == ProductKind.SUPPLY.value)
    ).scalars().all()
    client.post("/admin/demo/seed", follow_redirects=False)
    second_supplies = session.execute(
        select(Product).where(Product.kind == ProductKind.SUPPLY.value)
    ).scalars().all()
    assert len(first_supplies) == len(second_supplies)


def test_reset_button_wipes_data(client, session):
    client.post("/admin/demo/seed", follow_redirects=False)
    assert session.execute(select(Product)).scalars().all()

    r = client.post("/admin/demo/reset", follow_redirects=False)
    assert r.status_code == 303

    # The conftest test session may need a refresh because reset_database
    # drops & recreates tables. Use a fresh query.
    session.expire_all()
    products = session.execute(select(Product)).scalars().all()
    assert products == []
    sales = session.execute(select(Sale)).scalars().all()
    assert sales == []
    conflicts = session.execute(select(Conflict)).scalars().all()
    assert conflicts == []


def test_seed_then_pos_checkout_decrements_and_fans_out(client, session):
    client.post("/admin/demo/seed", follow_redirects=False)
    # Find a sellable inventory_unit via the POS browse page so the test
    # exercises the same selection flow the cashier uses.
    r = client.get("/pos/")
    assert r.status_code == 200

    from app.db.models import InventoryUnit
    unit = session.execute(
        select(InventoryUnit).where(InventoryUnit.quantity_on_hand > 0).limit(1)
    ).scalar_one()
    initial_qty = unit.quantity_on_hand

    client.post("/pos/cart/add", data={"inventory_unit_id": unit.id}, follow_redirects=False)
    client.post("/pos/cart/checkout", data={"payment_method": "cash"}, follow_redirects=False)

    session.refresh(unit)
    assert unit.quantity_on_hand == initial_qty - 1

    fanout = session.execute(
        select(OutboundChange).where(
            OutboundChange.inventory_unit_id == unit.id,
            OutboundChange.action == "update_qty",
            OutboundChange.completed_at.is_(None),
        )
    ).scalars().all()
    # Walk-in sale fans out to eBay + TCGPlayer (Shopify is origin).
    assert {r.channel for r in fanout} == {Channel.EBAY.value, Channel.TCGPLAYER.value}
