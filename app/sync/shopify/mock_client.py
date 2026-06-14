"""Logging mock Shopify client. Records calls and produces deterministic
product/variant IDs derived from the SKU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.sync.shopify.client import PublishedShopifyProduct, ShopifyClient

logger = logging.getLogger(__name__)


@dataclass
class MockCall:
    op: str
    kwargs: dict[str, Any]


class LoggingMockShopifyClient(ShopifyClient):
    """Mock Shopify Admin API.

    publish_product returns ``(product_id=80000000+sku, variant_id=90000000+sku)``
    where ``sku`` is the inventory_unit id.
    """

    PRODUCT_BASE = 80_000_000
    VARIANT_BASE = 90_000_000

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[MockCall] = []
        self.fail_on = fail_on or set()

    def _record(self, op: str, **kwargs: Any) -> None:
        self.calls.append(MockCall(op=op, kwargs=kwargs))
        logger.info("[mock-shopify] %s %s", op, kwargs)

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise RuntimeError(f"[mock-shopify] simulated failure on {op}")

    def publish_product(
        self,
        *,
        sku: str,
        title: str,
        body_html: str | None,
        image_url: str | None,
        price: Decimal | None,
        quantity: int,
        product_type: str,
    ) -> PublishedShopifyProduct:
        self._record(
            "publish_product",
            sku=sku,
            title=title,
            price=price,
            quantity=quantity,
            product_type=product_type,
            body_html_len=len(body_html) if body_html else 0,
            has_image=bool(image_url),
        )
        self._maybe_fail("publish_product")
        n = int(sku)
        return PublishedShopifyProduct(
            product_id=self.PRODUCT_BASE + n,
            variant_id=self.VARIANT_BASE + n,
        )

    def update_quantity(self, *, variant_id: int, sku: str, new_quantity: int) -> None:
        self._record("update_quantity", variant_id=variant_id, sku=sku, new_quantity=new_quantity)
        self._maybe_fail("update_quantity")

    def update_price(self, *, variant_id: int, sku: str, new_price: Decimal) -> None:
        self._record("update_price", variant_id=variant_id, sku=sku, new_price=new_price)
        self._maybe_fail("update_price")
