"""Shopify Admin API client interface + real implementation.

The ABC defines the three operations the outbound pipeline needs.
RealShopifyClient drives the Shopify Admin REST API (version 2026-04).

Product lifecycle:
  publish_product  →  POST /products.json          (create + set initial stock)
  update_quantity  →  POST /inventory_levels/set   (stock change)
  update_price     →  PUT  /variants/{id}.json     (price change)

Inventory updates require a location_id — fetched automatically on first
use and cached for the lifetime of the client.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

_API_VERSION = "2026-04"
_RATE_LIMIT_RETRY_AFTER = 2.0   # seconds to wait after a 429


@dataclass(frozen=True)
class PublishedShopifyProduct:
    product_id: int
    variant_id: int


class ShopifyClient(ABC):
    @abstractmethod
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
        """Create a Shopify product published to POS. Returns product + variant IDs."""

    @abstractmethod
    def update_quantity(self, *, variant_id: int, sku: str, new_quantity: int) -> None: ...

    @abstractmethod
    def update_price(self, *, variant_id: int, sku: str, new_price: Decimal) -> None: ...

    def close(self) -> None:
        pass

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class RealShopifyClient(ShopifyClient):
    """Shopify Admin REST API client.

    Credentials are read from the DB-backed settings store (see
    ``app.routes.settings.get_shopify_creds``). Pass ``shop_domain``,
    ``admin_api_token``, and optionally ``location_id`` directly, or
    let ``from_settings()`` pull them from the DB.

    ``location_id`` is required for inventory updates. If not supplied,
    it is fetched automatically from the first active location on the
    first inventory call and cached.
    """

    def __init__(
        self,
        *,
        shop_domain: str,
        admin_api_token: str,
        location_id: str = "",
        timeout: float = 15.0,
    ) -> None:
        if not shop_domain or not admin_api_token:
            raise ValueError("shop_domain and admin_api_token are required")
        self._domain = shop_domain.strip().lower().removeprefix("https://").removesuffix("/")
        self._token = admin_api_token.strip()
        self._location_id: int | None = int(location_id) if location_id.strip() else None
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-Shopify-Access-Token": self._token,
                "Content-Type": "application/json",
            },
        )

    @classmethod
    def from_settings(cls, session) -> "RealShopifyClient":
        """Construct from DB-backed settings. Raises ValueError if not configured."""
        from app.routes.settings import get_shopify_creds
        creds = get_shopify_creds(session)
        return cls(
            shop_domain=creds["domain"],
            admin_api_token=creds["token"],
            location_id=creds["location_id"],
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ helpers

    def _url(self, path: str) -> str:
        return f"https://{self._domain}/admin/api/{_API_VERSION}/{path}"

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an API call with one automatic retry on 429 rate-limit."""
        r = self._client.request(method, self._url(path), **kwargs)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", _RATE_LIMIT_RETRY_AFTER))
            logger.warning("Shopify rate limit hit — waiting %.1fs", retry_after)
            time.sleep(retry_after)
            r = self._client.request(method, self._url(path), **kwargs)
        if r.status_code == 401:
            raise RuntimeError(
                "Shopify API: 401 Unauthorized — check the Admin API token in Settings."
            )
        r.raise_for_status()
        return r

    def _get_location_id(self) -> int:
        """Fetch and cache the first active location's ID."""
        if self._location_id is not None:
            return self._location_id
        r = self._request("GET", "locations.json")
        locations = r.json().get("locations", [])
        active = [l for l in locations if l.get("active")]
        if not active:
            raise RuntimeError("No active Shopify locations found. Configure a location in Settings.")
        self._location_id = int(active[0]["id"])
        logger.info("Auto-selected Shopify location: %s (%s)", active[0]["name"], self._location_id)
        return self._location_id

    # ------------------------------------------------------------------ ABC impl

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
        payload: dict = {
            "product": {
                "title": title,
                "body_html": body_html or "",
                "product_type": product_type,
                "status": "active",
                "published_scope": "global",
                "variants": [
                    {
                        "sku": sku,
                        "price": str(price) if price is not None else "0.00",
                        "inventory_management": "shopify",
                        "inventory_policy": "deny",
                        "fulfillment_service": "manual",
                        "requires_shipping": False,
                        "taxable": True,
                    }
                ],
            }
        }

        # Only attach image if it's an absolute public URL
        if image_url and image_url.startswith("http"):
            payload["product"]["images"] = [{"src": image_url, "alt": title}]

        r = self._request("POST", "products.json", json=payload)
        data = r.json()["product"]
        product_id = int(data["id"])
        variant = data["variants"][0]
        variant_id = int(variant["id"])
        inventory_item_id = int(variant["inventory_item_id"])

        logger.info(
            "Shopify: created product %d variant %d sku=%s title=%r",
            product_id, variant_id, sku, title,
        )

        # Set initial stock at the location
        try:
            location_id = self._get_location_id()
            self._request(
                "POST",
                "inventory_levels/set.json",
                json={
                    "location_id": location_id,
                    "inventory_item_id": inventory_item_id,
                    "available": quantity,
                },
            )
            logger.info(
                "Shopify: set inventory item=%d qty=%d location=%d",
                inventory_item_id, quantity, location_id,
            )
        except Exception:
            logger.warning(
                "Shopify: could not set initial inventory for variant %d — "
                "check location_id in Settings",
                variant_id, exc_info=True,
            )

        return PublishedShopifyProduct(product_id=product_id, variant_id=variant_id)

    def update_quantity(self, *, variant_id: int, sku: str, new_quantity: int) -> None:
        # Resolve inventory_item_id from the variant
        r = self._request("GET", f"variants/{variant_id}.json")
        inventory_item_id = int(r.json()["variant"]["inventory_item_id"])
        location_id = self._get_location_id()

        self._request(
            "POST",
            "inventory_levels/set.json",
            json={
                "location_id": location_id,
                "inventory_item_id": inventory_item_id,
                "available": new_quantity,
            },
        )
        logger.info(
            "Shopify: updated qty variant=%d sku=%s qty=%d location=%d",
            variant_id, sku, new_quantity, location_id,
        )

    def update_price(self, *, variant_id: int, sku: str, new_price: Decimal) -> None:
        self._request(
            "PUT",
            f"variants/{variant_id}.json",
            json={"variant": {"id": variant_id, "price": str(new_price)}},
        )
        logger.info("Shopify: updated price variant=%d sku=%s price=%s", variant_id, sku, new_price)

    def create_draft_order(
        self,
        *,
        line_items: list[dict],
        cash_discount_pct: float = 0.0,
        note: str = "",
    ) -> dict:
        """Create a Shopify Draft Order (card payment path).

        Returns the full draft_order dict including ``id`` and
        ``invoice_url`` so the cashier can open it in the POS app.
        """
        payload: dict = {
            "draft_order": {
                "line_items": line_items,
                "tags": "TAG-POS,card",
                "note": note or "TAG POS — card payment",
            }
        }
        if cash_discount_pct > 0:
            payload["draft_order"]["applied_discount"] = {
                "value_type": "percentage",
                "value": str(round(cash_discount_pct * 100, 4)),
                "title": "Cash discount",
            }
        r = self._request("POST", "draft_orders.json", json=payload)
        return r.json()["draft_order"]

    def _pos_line_items(self, line_items: list[dict]) -> list[dict]:
        """Stamp each product line item as non-shippable and non-taxable.

        taxable=False prevents Shopify from auto-computing its own tax on top
        of the products; we supply explicit tax and fee line items instead so
        the order total matches our computed total exactly.
        """
        return [{**li, "requires_shipping": False, "taxable": False} for li in line_items]

    def _extra_charge_item(self, title: str, amount: str) -> dict:
        return {
            "title": title,
            "quantity": 1,
            "price": amount,
            "requires_shipping": False,
            "taxable": False,
        }

    def create_card_order(
        self,
        *,
        line_items: list[dict],
        total_amount: str,
        tax_amount: str = "0.00",
        card_surcharge_amount: str = "0.00",
        note: str = "",
    ) -> dict:
        """Create a completed in-store Shopify order for a card payment.

        Tax and card fee are passed as explicit non-taxable line items so
        Shopify's order total equals our computed total and both charges are
        visible on the receipt.  Product items are marked taxable=false to
        prevent Shopify from auto-adding its own tax on top.
        """
        from decimal import Decimal as _D
        all_items = self._pos_line_items(line_items)
        if _D(tax_amount) > 0:
            all_items.append(self._extra_charge_item("Sales Tax", tax_amount))
        if _D(card_surcharge_amount) > 0:
            all_items.append(self._extra_charge_item("Credit Card Fee", card_surcharge_amount))

        payload: dict = {
            "order": {
                "line_items": all_items,
                "financial_status": "paid",
                "fulfillment_status": "fulfilled",
                "transactions": [{
                    "kind": "sale",
                    "status": "success",
                    "amount": total_amount,
                    "gateway": "card_reader",
                }],
                "tags": "TAG-POS,card",
                "note": note or "TAG POS — card payment",
            }
        }
        r = self._request("POST", "orders.json", json=payload)
        return r.json()["order"]

    def create_cash_order(
        self,
        *,
        line_items: list[dict],
        total_amount: str,
        cash_discount_pct: float = 0.0,
        note: str = "",
    ) -> dict:
        """Create a completed in-store Shopify order for a cash payment.

        Same as card_order but records the gateway as cash and applies
        an optional cash discount code.
        """
        payload: dict = {
            "order": {
                "line_items": self._pos_line_items(line_items),
                "financial_status": "paid",
                "fulfillment_status": "fulfilled",
                "transactions": [{
                    "kind": "sale",
                    "status": "success",
                    "amount": total_amount,
                    "gateway": "cash",
                }],
                "tags": "TAG-POS,cash",
                "note": note or "TAG POS — cash payment",
            }
        }
        if cash_discount_pct > 0:
            payload["order"]["discount_codes"] = [{
                "code": "CASH",
                "amount": str(round(cash_discount_pct * 100, 4)),
                "type": "percentage",
            }]
        r = self._request("POST", "orders.json", json=payload)
        return r.json()["order"]

    def fetch_orders_since(
        self,
        since: "datetime | None" = None,
        *,
        limit: int = 50,
    ) -> list["ShopifyOrder"]:
        """Return paid orders created after ``since`` (UTC), newest last."""
        from datetime import datetime, timezone

        params: dict = {
            "status": "any",
            "financial_status": "paid",
            "limit": limit,
            "order": "created_at asc",
        }
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            params["created_at_min"] = since.isoformat()

        r = self._request("GET", "orders.json", params=params)
        raw_orders = r.json().get("orders", [])
        return [_parse_shopify_order(o) for o in raw_orders]


@dataclass(frozen=True)
class ShopifyOrderLine:
    variant_id: int | None
    quantity: int
    unit_price: Decimal
    title: str


@dataclass(frozen=True)
class ShopifyOrder:
    order_id: str
    created_at: "datetime"
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    lines: list[ShopifyOrderLine]


def _parse_shopify_order(raw: dict) -> "ShopifyOrder":
    from datetime import datetime, timezone

    created_raw = raw.get("created_at", "")
    try:
        created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
    except Exception:
        created_at = datetime.now(timezone.utc)

    lines = []
    for li in raw.get("line_items", []):
        vid = li.get("variant_id")
        lines.append(ShopifyOrderLine(
            variant_id=int(vid) if vid else None,
            quantity=int(li.get("quantity") or 1),
            unit_price=Decimal(str(li.get("price") or "0.00")),
            title=str(li.get("title") or ""),
        ))

    return ShopifyOrder(
        order_id=str(raw["id"]),
        created_at=created_at,
        subtotal=Decimal(str(raw["subtotal_price"])) if raw.get("subtotal_price") else None,
        tax=Decimal(str(raw["total_tax"])) if raw.get("total_tax") else None,
        total=Decimal(str(raw["total_price"])) if raw.get("total_price") else None,
        lines=lines,
    )
