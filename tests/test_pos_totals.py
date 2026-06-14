"""compute_totals: pure cart-total math."""

from __future__ import annotations

from decimal import Decimal

from app.pos.totals import LineSnapshot, compute_totals


def _line(price: str, qty: int = 1) -> LineSnapshot:
    return LineSnapshot(
        inventory_unit_id=1,
        title="Test",
        unit_price=Decimal(price),
        quantity=qty,
    )


def test_subtotal_sums_lines():
    t = compute_totals(
        [_line("3.00", 2), _line("4.99", 1)],
        tax_rate=0.0,
        card_surcharge_rate=0.0,
        payment_method="cash",
    )
    assert t.subtotal == Decimal("10.99")
    assert t.tax == Decimal("0.00")
    assert t.card_surcharge == Decimal("0.00")
    assert t.total == Decimal("10.99")


def test_tax_applied_then_surcharge_on_post_tax():
    t = compute_totals(
        [_line("100.00", 1)],
        tax_rate=0.10,  # tax = 10.00
        card_surcharge_rate=0.029,  # surcharge = 110.00 * 2.9% = 3.19
        payment_method="card",
    )
    assert t.subtotal == Decimal("100.00")
    assert t.tax == Decimal("10.00")
    assert t.card_surcharge == Decimal("3.19")
    assert t.total == Decimal("113.19")


def test_cash_skips_surcharge():
    t = compute_totals(
        [_line("10.00", 1)],
        tax_rate=0.10,
        card_surcharge_rate=0.029,
        payment_method="cash",
    )
    assert t.card_surcharge == Decimal("0.00")
    assert t.total == Decimal("11.00")


def test_empty_cart_zero_totals():
    t = compute_totals(
        [],
        tax_rate=0.10,
        card_surcharge_rate=0.029,
        payment_method="card",
    )
    assert t.subtotal == Decimal("0.00")
    assert t.total == Decimal("0.00")
