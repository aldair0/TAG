"""Sold Online page actions: Confirm shipped (decrement) vs Dismiss (no-op)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Channel, InventoryUnit, Product, Sale, SaleLine


def _seed_flagged_unit(session, *, qty=1, name="Blue-Eyes White Dragon (UTR)") -> InventoryUnit:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = Product(
        tcgplayer_product_id=920_001,
        kind="single",
        name=name,
        is_online_listable=True,
    )
    session.add(p)
    session.flush()
    u = InventoryUnit(
        product_id=p.id,
        condition="Lightly Played Unlimited",
        quantity_on_hand=qty,
        unit_price=Decimal("4.50"),
        sold_online_at=now,
        sold_online_until=now + timedelta(days=2),
    )
    session.add(u)
    session.commit()
    return u


def test_confirm_decrements_records_sale_and_clears_flag(client, session):
    unit = _seed_flagged_unit(session, qty=1)

    r = client.post(f"/admin/sold-online/confirm/{unit.id}", follow_redirects=False)
    assert r.status_code == 303

    session.refresh(unit)
    # Removed from inventory.
    assert unit.quantity_on_hand == 0
    # Flag fully cleared → drops off the page.
    assert unit.sold_online_at is None
    assert unit.sold_online_until is None

    # Sale recorded on the TCGPlayer channel, one line for this unit.
    sale = session.execute(
        select(Sale).where(Sale.channel == Channel.TCGPLAYER.value)
    ).scalar_one()
    line = session.execute(
        select(SaleLine).where(SaleLine.sale_id == sale.id)
    ).scalar_one()
    assert line.inventory_unit_id == unit.id
    assert line.quantity_sold == 1


def test_confirm_only_removes_one_when_multiple_in_stock(client, session):
    unit = _seed_flagged_unit(session, qty=3)

    client.post(f"/admin/sold-online/confirm/{unit.id}", follow_redirects=False)

    session.refresh(unit)
    assert unit.quantity_on_hand == 2  # default qty=1 removed
    assert unit.sold_online_until is None


def test_dismiss_clears_flag_without_touching_inventory(client, session):
    unit = _seed_flagged_unit(session, qty=1)

    r = client.post(f"/admin/sold-online/dismiss/{unit.id}", follow_redirects=False)
    assert r.status_code == 303

    session.refresh(unit)
    assert unit.quantity_on_hand == 1  # unchanged — false alarm
    assert unit.sold_online_until is None
    # No sale recorded.
    assert session.execute(select(Sale)).first() is None


def test_confirm_unknown_unit_is_noop(client, session):
    r = client.post("/admin/sold-online/confirm/999999", follow_redirects=False)
    assert r.status_code == 303
    assert session.execute(select(Sale)).first() is None
