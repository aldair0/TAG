"""Parse the real .eml fixtures in "mail examples/" into structured sales."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import email

from app.sync.tcgplayer.email_parser import (
    EmailParseError,
    is_sale_notification,
    parse_bytes,
)

MAIL_DIR = Path(__file__).resolve().parent.parent / "mail examples"


def _load(filename: str):
    return parse_bytes((MAIL_DIR / filename).read_bytes())


def test_single_item_email():
    sale = _load(
        "Your TCGplayer.com items of Left Leg of the Forbidden One have sold!.eml"
    )
    assert sale.order_id == "1D1B3BF6-923C64-5ED56"
    assert sale.order_total == Decimal("5.99")
    assert sale.order_date == date(2026, 6, 13)
    assert len(sale.items) == 1
    item = sale.items[0]
    assert item.quantity == 1
    assert item.name == "Left Leg of the Forbidden One"
    assert item.condition == "Near Mint Unlimited"


def test_multi_item_email_lists_all_three():
    sale = _load(
        "Your TCGplayer.com items of Slifer the Sky Dragon and 2 more items have sold!.eml"
    )
    assert sale.order_id == "1D1B3BF6-145A2F-906A3"
    assert sale.order_total == Decimal("5.69")
    assert sale.order_date == date(2026, 6, 12)
    names = {(i.name, i.condition) for i in sale.items}
    assert names == {
        ("Obelisk the Tormentor", "Near Mint Limited"),
        ("Slifer the Sky Dragon", "Lightly Played Limited"),
        ("The Winged Dragon of Ra", "Moderately Played Limited"),
    }
    assert all(i.quantity == 1 for i in sale.items)


def test_two_item_email():
    sale = _load(
        "Your TCGplayer.com items of Blue-Eyes Twin Burst Dragon and 1 more items have sold!.eml"
    )
    assert sale.order_id == "1D1B3BF6-8F2F74-64629"
    assert sale.order_total == Decimal("2.13")
    names = {(i.name, i.condition) for i in sale.items}
    assert names == {
        ("Blue-Eyes Twin Burst Dragon", "Near Mint Unlimited"),
        ("Maiden of White", "Near Mint Unlimited"),
    }


def test_name_with_slash_splits_on_last_slash():
    """'Mamoswine ex - 174/159' has a slash in the name itself."""
    sale = _load(
        "Your TCGplayer.com items of Mamoswine ex - 174_159 have sold!.eml"
    )
    assert sale.order_id == "1D1B3BF6-15BC23-174E4"
    assert sale.order_total == Decimal("2.14")
    assert sale.order_date == date(2026, 6, 5)
    assert len(sale.items) == 1
    item = sale.items[0]
    assert item.name == "Mamoswine ex - 174/159"
    assert item.condition == "Near Mint Holofoil"


def test_garbage_email_raises():
    with pytest.raises(EmailParseError):
        parse_bytes(b"Subject: hello\r\n\r\nnot a sale email")


def _msg(from_: str, subject: str):
    return email.message_from_bytes(
        f"From: {from_}\r\nSubject: {subject}\r\n\r\nbody".encode()
    )


def test_is_sale_notification_defaults_match_tcgplayer():
    assert is_sale_notification(
        _msg("TCGplayer <sales@tcgplayer.com>", "Your items of X have sold!")
    )
    assert not is_sale_notification(_msg("eBay <noreply@ebay.com>", "You sold an item"))
    assert not is_sale_notification(_msg("TCGplayer <sales@tcgplayer.com>", "Newsletter"))


def test_is_sale_notification_honors_custom_criteria():
    msg = _msg("Marketplace <noreply@ebay.com>", "Order confirmed")
    assert is_sale_notification(
        msg, from_contains="ebay.com", subject_contains="order confirmed"
    )


def test_is_sale_notification_blank_criterion_matches_anything():
    msg = _msg("anyone@example.com", "whatever")
    assert is_sale_notification(msg, from_contains="", subject_contains="")
