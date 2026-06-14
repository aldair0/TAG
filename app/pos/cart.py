"""Cart state — kept in a single cookie as JSON.

The cart is intentionally stateless on the server: each request
encodes/decodes the cookie. This keeps the POS UI deployable as one
process and avoids the complexity of server-side sessions for a flow
that only spans the cashier's tap session.

CartItem stores ``inventory_unit.id`` + ``quantity``; everything else
(name, price, image) is re-resolved from the DB on each render so a
price/qty edit by the back office shows up immediately.

Custom items use an ``override_price`` (string, to avoid JSON/Decimal
issues) that takes precedence over the DB price when building cart lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

CART_COOKIE = "tag_pos_cart"


@dataclass
class CartItem:
    inventory_unit_id: int
    quantity: int
    override_price: str | None = None  # e.g. "5.00" for custom items


@dataclass
class Cart:
    items: list[CartItem] = field(default_factory=list)

    def _find(self, unit_id: int, override_price: str | None) -> CartItem | None:
        for it in self.items:
            if it.inventory_unit_id == unit_id and it.override_price == override_price:
                return it
        return None

    def add(self, unit_id: int, qty: int = 1, override_price: str | None = None) -> None:
        existing = self._find(unit_id, override_price)
        if existing:
            existing.quantity += qty
        else:
            self.items.append(CartItem(inventory_unit_id=unit_id, quantity=qty, override_price=override_price))

    def set_quantity(self, unit_id: int, qty: int, override_price: str | None = None) -> None:
        if qty <= 0:
            self.remove(unit_id, override_price)
            return
        existing = self._find(unit_id, override_price)
        if existing:
            existing.quantity = qty
        else:
            self.items.append(CartItem(inventory_unit_id=unit_id, quantity=qty, override_price=override_price))

    def remove(self, unit_id: int, override_price: str | None = None) -> None:
        self.items = [
            it for it in self.items
            if not (it.inventory_unit_id == unit_id and it.override_price == override_price)
        ]

    def clear(self) -> None:
        self.items.clear()

    def is_empty(self) -> bool:
        return not self.items

    def quantity_for(self, unit_id: int) -> int:
        return sum(it.quantity for it in self.items if it.inventory_unit_id == unit_id)


def encode_cart(cart: Cart) -> str:
    rows = []
    for it in cart.items:
        row: dict = {"u": it.inventory_unit_id, "q": it.quantity}
        if it.override_price is not None:
            row["p"] = it.override_price
        rows.append(row)
    return json.dumps(rows, separators=(",", ":"))


def decode_cart(raw: str | None) -> Cart:
    if not raw:
        return Cart()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Cart()
    cart = Cart()
    for entry in data or []:
        try:
            uid = int(entry["u"])
            q = int(entry["q"])
        except (KeyError, TypeError, ValueError):
            continue
        if q > 0:
            override_price = str(entry["p"]) if entry.get("p") else None
            cart.items.append(CartItem(inventory_unit_id=uid, quantity=q, override_price=override_price))
    return cart
