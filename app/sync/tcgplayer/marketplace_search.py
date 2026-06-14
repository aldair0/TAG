"""Wrapper around TCGPlayer's public marketplace search API
(``mp-search-api.tcgplayer.com``), filtered to a specific seller.

Captured request shape lives in the project chat history (the curl from
2026-05-03). The body is reproduced faithfully — many fields look
optional, but matching what a real browser sends is the safest default
until we confirm what the server actually requires.

Auth: this endpoint serves the public buyer-facing search, so an
authenticated session cookie is **not** required for the seller-filter
case (anyone can browse a single seller's listings). The captured curl
sent cookies because they were in the browser jar — we don't.

Use cases:
- Per-card spot lookup (e.g., "is this still listed?")
- Backfilling ``Product.tcgplayer_url`` once we add that column —
  the response carries the product-level ID that the URL needs (the
  CSV's TCGplayer Id is a SKU-level ID; see chat for details).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings

SEARCH_URL = "https://mp-search-api.tcgplayer.com/v1/search/request"
IMAGE_CDN = "https://tcgplayer-cdn.tcgplayer.com/product"

# Opaque build/version identifier the browser sends. Stable enough to
# hard-code; adjust if requests start 4xxing.
_MPFEV = "5106"

_DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.tcgplayer.com",
    "referer": "https://www.tcgplayer.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
    ),
}


def build_search_payload(
    *,
    q: str,
    seller_key: str,
    page: int = 1,
    page_size: int = 24,
) -> dict:
    """Construct the JSON body posted to the search endpoint.

    The search query string ``q`` is **not** included here — it lives in
    the URL's query string per TCGPlayer's contract.

    ``page`` is one-indexed externally (page 1 → from=0).
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    return {
        "algorithm": "sales_dismax",
        "from": (page - 1) * page_size,
        "size": page_size,
        "filters": {"term": {}, "range": {}, "match": {}},
        "listingSearch": {
            "context": {"cart": {}},
            "filters": {
                "term": {
                    "sellerStatus": "Live",
                    "channelId": 0,
                    "sellerKey": [seller_key],
                },
                "range": {"quantity": {"gte": 1}},
                "exclude": {"channelExclusion": 0},
            },
        },
        "context": {
            "cart": {},
            "shippingCountry": "US",
        },
        "settings": {
            "useFuzzySearch": True,
            "didYouMean": {},
        },
        "sort": {},
    }


def build_image_url(product_id: int, *, size_px: int = 400) -> str:
    """Construct the CDN image URL for a product.

    TCGPlayer serves multiple sizes from the same path pattern:
    ``<id>_in_<S>x<S>.jpg``. Sizes seen in the wild: 200, 400, 600,
    800, 1000. The CDN appears to silently 200 even for arbitrary
    sizes, but stick to those if possible.
    """
    if size_px <= 0:
        raise ValueError(f"size_px must be positive, got {size_px}")
    return f"{IMAGE_CDN}/{product_id}_in_{size_px}x{size_px}.jpg"


@dataclass(frozen=True)
class Listing:
    """One seller's offer for a product (post seller-filter, this is
    almost always our store's listing). ``product_condition_id`` is
    exactly the SKU ID we have stored in ``Product.tcgplayer_product_id``
    — the bridge between marketplace search and our local CSV import."""

    product_condition_id: int
    seller_key: str | None
    seller_name: str | None
    price: float | None
    quantity: int
    condition: str | None
    printing: str | None


@dataclass(frozen=True)
class ProductHit:
    """One product in the search results. ``product_id`` is the
    public-facing product page ID — the one that appears in
    ``https://www.tcgplayer.com/product/<product_id>/...`` URLs and
    is the one image URLs use."""

    product_id: int
    name: str
    set_name: str | None
    set_code: str | None
    product_line: str | None
    rarity: str | None
    number: str | None  # collector number from customAttributes (e.g. "35/82")
    market_price: float | None
    lowest_price: float | None
    lowest_price_with_shipping: float | None
    listings: list[Listing]

    @property
    def listing_count(self) -> int:
        return len(self.listings)

    @property
    def tcgplayer_url(self) -> str:
        """Slug is decorative; TCGPlayer routes by ID."""
        return f"https://www.tcgplayer.com/product/{self.product_id}/"

    def image_url(self, size_px: int = 400) -> str:
        return build_image_url(self.product_id, size_px=size_px)


@dataclass(frozen=True)
class SearchResponse:
    products: list[ProductHit]
    total_results: int


def _parse_listing(raw: dict) -> Listing:
    return Listing(
        product_condition_id=int(raw.get("productConditionId") or 0),
        seller_key=raw.get("sellerKey"),
        seller_name=raw.get("sellerName"),
        price=raw.get("price"),
        quantity=int(raw.get("quantity") or 0),
        condition=raw.get("condition"),
        printing=raw.get("printing"),
    )


def _parse_response(envelope: dict) -> SearchResponse:
    # Top-level shape: { "errors": [...], "results": [ { "totalResults": N, "results": [hit, ...] } ] }
    outer = (envelope.get("results") or [{}])[0]
    total = int(outer.get("totalResults") or 0)
    products: list[ProductHit] = []
    for hit in outer.get("results") or []:
        attrs = hit.get("customAttributes") or {}
        listings = [_parse_listing(li) for li in (hit.get("listings") or [])]
        products.append(
            ProductHit(
                product_id=int(hit.get("productId")),
                name=str(hit.get("productName") or ""),
                set_name=hit.get("setName"),
                set_code=hit.get("setCode"),
                product_line=hit.get("productLineName"),
                rarity=hit.get("rarityName"),
                number=attrs.get("number"),
                market_price=hit.get("marketPrice"),
                lowest_price=hit.get("lowestPrice"),
                lowest_price_with_shipping=hit.get("lowestPriceWithShipping"),
                listings=listings,
            )
        )
    return SearchResponse(products=products, total_results=total)


class MarketplaceSearchClient:
    """Thin httpx wrapper. Pass ``transport`` to inject ``MockTransport``
    in tests."""

    def __init__(
        self,
        *,
        seller_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._seller_key = seller_key or settings.tcgplayer_seller_key
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers=_DEFAULT_HEADERS,
        )

    def __enter__(self) -> "MarketplaceSearchClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search(
        self, q: str, *, page: int = 1, page_size: int = 24
    ) -> SearchResponse:
        params = {"q": q, "isList": "false", "mpfev": _MPFEV}
        payload = build_search_payload(
            q=q,
            seller_key=self._seller_key,
            page=page,
            page_size=page_size,
        )
        r = self._client.post(SEARCH_URL, params=params, json=payload)
        r.raise_for_status()
        return _parse_response(r.json())

    def find_by_sku(self, q: str, *, sku_id: int) -> ProductHit | None:
        """Cross-walk a CSV SKU id (TCGplayer Id from the seller export)
        to its parent product hit. Searches by ``q`` and returns the
        first hit whose listings include the given productConditionId.

        First tries a seller-filtered search (fast, works when our listing
        is active). If that misses — e.g. the listing is out of stock or
        inactive — falls back to a global search across all sellers so we
        can still resolve the marketplace_product_id for image fetching.
        """
        res = self.search(q)
        for hit in res.products:
            if any(li.product_condition_id == sku_id for li in hit.listings):
                return hit
        # Seller-filtered miss — retry without the seller/quantity gate.
        res_global = self._search_global(q)
        for hit in res_global.products:
            if any(li.product_condition_id == sku_id for li in hit.listings):
                return hit
        return None

    def _search_global(self, q: str, *, page_size: int = 24) -> SearchResponse:
        """Like ``search`` but without the seller/quantity filter — returns
        results from all sellers. Used as fallback when our own listing is
        inactive or out of stock."""
        payload = {
            "algorithm": "sales_dismax",
            "from": 0,
            "size": page_size,
            "filters": {"term": {}, "range": {}, "match": {}},
            "listingSearch": {
                "context": {"cart": {}},
                "filters": {
                    "term": {"sellerStatus": "Live", "channelId": 0},
                    "range": {},
                    "exclude": {"channelExclusion": 0},
                },
            },
            "context": {"cart": {}, "shippingCountry": "US"},
            "settings": {"useFuzzySearch": True, "didYouMean": {}},
            "sort": {},
        }
        params = {"q": q, "isList": "false", "mpfev": _MPFEV}
        r = self._client.post(SEARCH_URL, params=params, json=payload)
        r.raise_for_status()
        return _parse_response(r.json())

    def find_by_attributes(
        self, *, name: str, set_name: str | None = None, number: str | None = None
    ) -> ProductHit | None:
        """Fallback lookup when a SKU id isn't available. Matches by
        product name + (optional) set name + (optional) collector
        number. Returns the first hit where every supplied attribute
        matches case-insensitively. ``None`` if nothing matches.
        """
        res = self.search(name)
        return self._match_by_attributes(res.products, name=name, set_name=set_name, number=number)

    def find_global_by_attributes(
        self, *, name: str, set_name: str | None = None, number: str | None = None
    ) -> ProductHit | None:
        """Like ``find_by_attributes`` but searches all sellers — used
        when no active listing exists for our store. Useful for resolving
        a marketplace_product_id for image fetching even on delisted cards.
        """
        res = self._search_global(name)
        return self._match_by_attributes(res.products, name=name, set_name=set_name, number=number)

    @staticmethod
    def _match_by_attributes(
        products: list["ProductHit"],
        *,
        name: str,
        set_name: str | None,
        number: str | None,
    ) -> "ProductHit | None":
        target_name = name.casefold()
        target_set = set_name.casefold() if set_name else None
        for hit in products:
            if hit.name.casefold() != target_name:
                continue
            if target_set and (hit.set_name or "").casefold() != target_set:
                continue
            if number and (hit.number or "") != number:
                continue
            return hit
        return None
