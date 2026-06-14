"""Cart cookie encoding + add/set/remove/clear behavior."""

from __future__ import annotations

from app.pos.cart import Cart, decode_cart, encode_cart


def test_decode_empty_cookie_is_empty_cart():
    assert decode_cart(None).is_empty()
    assert decode_cart("").is_empty()
    assert decode_cart("garbage{[").is_empty()


def test_encode_round_trips():
    c = Cart()
    c.add(1, 2)
    c.add(7, 1)
    c2 = decode_cart(encode_cart(c))
    assert {it.inventory_unit_id: it.quantity for it in c2.items} == {1: 2, 7: 1}


def test_add_merges_duplicate_unit_id():
    c = Cart()
    c.add(5, 1)
    c.add(5, 2)
    assert len(c.items) == 1
    assert c.items[0].quantity == 3


def test_set_quantity_overwrites():
    c = Cart()
    c.add(5, 3)
    c.set_quantity(5, 1)
    assert c.quantity_for(5) == 1


def test_set_quantity_zero_removes():
    c = Cart()
    c.add(5, 3)
    c.set_quantity(5, 0)
    assert c.is_empty()


def test_remove_drops_only_target():
    c = Cart()
    c.add(1, 1)
    c.add(2, 1)
    c.remove(1)
    assert {it.inventory_unit_id for it in c.items} == {2}


def test_decode_rejects_zero_or_negative_qty_entries():
    encoded = '[{"u":5,"q":0},{"u":6,"q":-1},{"u":7,"q":2}]'
    cart = decode_cart(encoded)
    assert {it.inventory_unit_id for it in cart.items} == {7}
