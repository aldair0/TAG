"""Pure cart-total math: subtotal + tax + optional card surcharge.

Kept side-effect-free so the cashier-side preview and the eventual
Shopify draft-order builder share the same arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def _money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LineSnapshot:
    inventory_unit_id: int
    title: str
    unit_price: Decimal
    quantity: int
    override_price: str | None = None  # set for custom items; used by cart template

    @property
    def line_total(self) -> Decimal:
        return _money(self.unit_price * self.quantity)


@dataclass(frozen=True)
class CartTotals:
    lines: list[LineSnapshot]
    subtotal: Decimal
    cash_discount: Decimal   # 0 for card
    tax: Decimal
    card_surcharge: Decimal  # 0 for cash
    total: Decimal
    payment_method: str  # 'card' | 'cash'


def compute_totals(
    lines: list[LineSnapshot],
    *,
    tax_rate: float,
    card_surcharge_rate: float,
    cash_discount_rate: float = 0.0,
    payment_method: str = "card",
) -> CartTotals:
    """Compute totals for a cart.

    Card order of operations:
      subtotal  = Σ line_total
      tax       = subtotal * tax_rate
      surcharge = (subtotal + tax) * card_surcharge_rate
      total     = subtotal + tax + surcharge

    Cash order of operations:
      subtotal       = Σ line_total
      cash_discount  = subtotal * cash_discount_rate
      taxable_base   = subtotal - cash_discount
      tax            = taxable_base * tax_rate
      total          = taxable_base + tax
    """
    subtotal = _money(sum((li.line_total for li in lines), Decimal("0.00")))

    if payment_method == "cash":
        cash_discount = _money(subtotal * Decimal(str(cash_discount_rate)))
        taxable = subtotal - cash_discount
        tax = _money(taxable * Decimal(str(tax_rate)))
        surcharge = Decimal("0.00")
        total = _money(taxable + tax)
    else:
        cash_discount = Decimal("0.00")
        tax = _money(subtotal * Decimal(str(tax_rate)))
        surcharge = _money((subtotal + tax) * Decimal(str(card_surcharge_rate)))
        total = _money(subtotal + tax + surcharge)

    return CartTotals(
        lines=lines,
        subtotal=subtotal,
        cash_discount=cash_discount,
        tax=tax,
        card_surcharge=surcharge,
        total=total,
        payment_method=payment_method,
    )
