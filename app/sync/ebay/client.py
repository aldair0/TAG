"""eBay Sell API client interface.

The outbound worker depends only on :class:`EbayClient`. Two implementations:

- :class:`LoggingMockEbayClient` (in mock_client.py) — records calls and
  returns deterministic IDs. Used until eBay developer credentials are
  available.
- :class:`RealEbayClient` (here, stubbed) — will hit the real Sell APIs.
  Raises :class:`NotImplementedError` until Phase 2b wires it up.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class EbayOrderLine:
    sku: str
    quantity: int
    unit_price: Decimal | None = None


@dataclass(frozen=True)
class EbayOrder:
    """Minimal projection of an eBay Fulfillment-API order, just enough
    for the inbound poller to drive ``record_sale``."""

    order_id: str
    created_at: datetime
    lines: list[EbayOrderLine] = field(default_factory=list)
    total: Decimal | None = None


class EbayClient(ABC):
    @abstractmethod
    def publish_listing(self, *, sku: str, title: str, price: Decimal | None, quantity: int) -> str:
        """Create a listing for ``sku`` and return the external listing id
        (e.g., the eBay offer id).
        """

    @abstractmethod
    def update_quantity(self, *, external_listing_id: str, sku: str, new_quantity: int) -> None:
        ...

    @abstractmethod
    def update_price(self, *, external_listing_id: str, sku: str, new_price: Decimal) -> None:
        ...

    @abstractmethod
    def end_listing(self, *, external_listing_id: str, sku: str) -> None:
        ...

    @abstractmethod
    def fetch_orders_since(self, since: datetime | None) -> list[EbayOrder]:
        """Return all orders created since ``since`` (exclusive).

        ``since=None`` means "all known orders" — the very first poll.
        Implementations should return orders in creation-time order.
        """


class RealEbayClient(EbayClient):
    """The real Sell API client. Stubbed until eBay sandbox credentials are
    in ``.env`` and Phase 2b builds the request layer."""

    def __init__(self, *, app_id: str, cert_id: str, dev_id: str, refresh_token: str, env: str = "sandbox"):
        if not all([app_id, cert_id, dev_id, refresh_token]):
            raise RuntimeError(
                "RealEbayClient requires EBAY_APP_ID / EBAY_CERT_ID / EBAY_DEV_ID / "
                "EBAY_USER_REFRESH_TOKEN in .env. See claude_documents/test_accounts_setup.md §2."
            )
        self.app_id = app_id
        self.cert_id = cert_id
        self.dev_id = dev_id
        self.refresh_token = refresh_token
        self.env = env

    def _not_yet(self, op: str) -> "NotImplementedError":
        return NotImplementedError(
            f"RealEbayClient.{op} is not implemented yet. "
            "Use LoggingMockEbayClient until Phase 2b wires up the Sell API requests."
        )

    def publish_listing(self, **_kw) -> str:
        raise self._not_yet("publish_listing")

    def update_quantity(self, **_kw) -> None:
        raise self._not_yet("update_quantity")

    def update_price(self, **_kw) -> None:
        raise self._not_yet("update_price")

    def end_listing(self, **_kw) -> None:
        raise self._not_yet("end_listing")

    def fetch_orders_since(self, since: datetime | None) -> list[EbayOrder]:
        raise self._not_yet("fetch_orders_since")
