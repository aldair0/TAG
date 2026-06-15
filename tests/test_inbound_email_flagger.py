"""flag_email_sale: match email line items to inventory + set sold-online flag."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.db.models import InventoryUnit, Product
from app.inbound_email import flag_email_sale
from app.sync.tcgplayer.email_parser import SoldItem, SoldOnlineEmail, parse_bytes

MAIL_DIR = Path(__file__).resolve().parent.parent / "mail examples"


def _seed(session, name, condition, qty=1, listable=True) -> InventoryUnit:
    p = Product(kind="single", name=name, is_online_listable=listable)
    session.add(p)
    session.flush()
    u = InventoryUnit(
        product_id=p.id,
        condition=condition,
        quantity_on_hand=qty,
        unit_price=Decimal("1.00"),
    )
    session.add(u)
    session.flush()
    return u


def _email(*items, order_id="ORD-1") -> SoldOnlineEmail:
    return SoldOnlineEmail(
        order_id=order_id,
        order_total=None,
        order_date=None,
        subject="test",
        items=list(items),
    )


def test_flags_matching_unit(session):
    unit = _seed(session, "Left Leg of the Forbidden One", "Near Mint Unlimited")
    parsed = _email(SoldItem(1, "Left Leg of the Forbidden One", "Near Mint Unlimited"))

    summary = flag_email_sale(session, parsed)

    assert summary.flagged_unit_ids == [unit.id]
    session.refresh(unit)
    assert unit.sold_online_until is not None
    assert unit.sold_online_until > datetime.now(timezone.utc).replace(tzinfo=None)


def test_condition_must_match(session):
    """Same name, different condition → not flagged (condition is precise)."""
    unit = _seed(session, "Slifer the Sky Dragon", "Near Mint Limited")
    parsed = _email(SoldItem(1, "Slifer the Sky Dragon", "Lightly Played Limited"))

    summary = flag_email_sale(session, parsed)

    assert summary.flagged_unit_ids == []
    assert summary.unmatched_items == parsed.items
    session.refresh(unit)
    assert unit.sold_online_until is None


def test_name_match_is_case_insensitive(session):
    unit = _seed(session, "Maiden of White", "Near Mint Unlimited")
    parsed = _email(SoldItem(1, "maiden of WHITE", "near mint unlimited"))

    summary = flag_email_sale(session, parsed)

    assert summary.flagged_unit_ids == [unit.id]


def test_zero_qty_unit_not_flagged(session):
    unit = _seed(session, "Obelisk the Tormentor", "Near Mint Limited", qty=0)
    parsed = _email(SoldItem(1, "Obelisk the Tormentor", "Near Mint Limited"))

    summary = flag_email_sale(session, parsed)

    assert summary.flagged_unit_ids == []
    session.refresh(unit)
    assert unit.sold_online_until is None


def test_slash_in_name_matches(session):
    """'Mamoswine ex - 174/159' — the name contains a slash."""
    unit = _seed(session, "Mamoswine ex - 174/159", "Near Mint Holofoil")
    parsed = parse_bytes(
        (MAIL_DIR / "Your TCGplayer.com items of Mamoswine ex - 174_159 have sold!.eml").read_bytes()
    )

    summary = flag_email_sale(session, parsed)

    assert summary.flagged_unit_ids == [unit.id]
    assert summary.fully_matched


def test_multi_item_partial_match(session):
    """Three sold; only two are in local inventory."""
    slifer = _seed(session, "Slifer the Sky Dragon", "Lightly Played Limited")
    obelisk = _seed(session, "Obelisk the Tormentor", "Near Mint Limited")
    # 'The Winged Dragon of Ra' intentionally absent.
    parsed = parse_bytes(
        (MAIL_DIR / "Your TCGplayer.com items of Slifer the Sky Dragon and 2 more items have sold!.eml").read_bytes()
    )

    summary = flag_email_sale(session, parsed)

    assert set(summary.flagged_unit_ids) == {slifer.id, obelisk.id}
    assert not summary.fully_matched
    assert [i.name for i in summary.unmatched_items] == ["The Winged Dragon of Ra"]


def test_multiple_units_same_name_condition_all_flagged(session):
    """Two physical copies of the same printing → both flagged; staff dismisses extra."""
    u1 = _seed(session, "Blue-Eyes Twin Burst Dragon", "Near Mint Unlimited")
    u2 = _seed(session, "Blue-Eyes Twin Burst Dragon", "Near Mint Unlimited")
    parsed = _email(SoldItem(1, "Blue-Eyes Twin Burst Dragon", "Near Mint Unlimited"))

    summary = flag_email_sale(session, parsed)

    assert set(summary.flagged_unit_ids) == {u1.id, u2.id}
