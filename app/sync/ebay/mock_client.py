"""Logging mock eBay client. Records every call (so tests can assert on
behavior) and returns deterministic external listing ids derived from the SKU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.sync.ebay.client import EbayClient, EbayOrder

logger = logging.getLogger(__name__)


@dataclass
class MockCall:
    op: str
    kwargs: dict[str, Any]


class LoggingMockEbayClient(EbayClient):
    """Records calls; returns ``"MOCK-EBAY-{sku}"`` from publish_listing.

    Optionally fails: pass ``fail_on={"publish_listing"}`` to make a method
    raise :class:`RuntimeError`. Useful for retry tests.

    Pass ``orders=[…]`` to seed orders for ``fetch_orders_since`` polls
    (used by inbound sale tests).
    """

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        orders: list[EbayOrder] | None = None,
    ) -> None:
        self.calls: list[MockCall] = []
        self.fail_on = fail_on or set()
        self._orders: list[EbayOrder] = list(orders or [])

    def add_order(self, order: EbayOrder) -> None:
        """Append an order to the mock's internal list. The next
        ``fetch_orders_since`` call will return it (subject to the
        ``since`` filter)."""
        self._orders.append(order)

    def _record(self, op: str, **kwargs: Any) -> None:
        self.calls.append(MockCall(op=op, kwargs=kwargs))
        logger.info("[mock-ebay] %s %s", op, kwargs)

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise RuntimeError(f"[mock-ebay] simulated failure on {op}")

    def publish_listing(self, *, sku: str, title: str, price: Decimal | None, quantity: int) -> str:
        self._record("publish_listing", sku=sku, title=title, price=price, quantity=quantity)
        self._maybe_fail("publish_listing")
        return f"MOCK-EBAY-{sku}"

    def update_quantity(self, *, external_listing_id: str, sku: str, new_quantity: int) -> None:
        self._record(
            "update_quantity",
            external_listing_id=external_listing_id,
            sku=sku,
            new_quantity=new_quantity,
        )
        self._maybe_fail("update_quantity")

    def update_price(self, *, external_listing_id: str, sku: str, new_price: Decimal) -> None:
        self._record(
            "update_price",
            external_listing_id=external_listing_id,
            sku=sku,
            new_price=new_price,
        )
        self._maybe_fail("update_price")

    def end_listing(self, *, external_listing_id: str, sku: str) -> None:
        self._record("end_listing", external_listing_id=external_listing_id, sku=sku)
        self._maybe_fail("end_listing")

    def fetch_orders_since(self, since: datetime | None) -> list[EbayOrder]:
        self._record("fetch_orders_since", since=since)
        self._maybe_fail("fetch_orders_since")
        if since is None:
            return list(self._orders)
        return [o for o in self._orders if o.created_at > since]
