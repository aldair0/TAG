"""Tests for the TCGPlayer marketplace search wrapper.

We assert against the captured browser request shape (see the curl in the
project chat history). The server side is unstable enough that lots of
fields may be tolerated as no-ops, but matching the captured payload is
the safest starting point — fields can be trimmed later if confirmed
unnecessary.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.sync.tcgplayer.marketplace_search import (
    SEARCH_URL,
    MarketplaceSearchClient,
    build_image_url,
    build_search_payload,
)


# ---- payload builder ---------------------------------------------------


def test_payload_includes_seller_key_filter():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["listingSearch"]["filters"]["term"]["sellerKey"] == ["1d1b3bf6"]


def test_payload_seller_status_is_live():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["listingSearch"]["filters"]["term"]["sellerStatus"] == "Live"


def test_payload_only_returns_listings_with_quantity():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["listingSearch"]["filters"]["range"]["quantity"] == {"gte": 1}


def test_payload_default_pagination():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["from"] == 0
    assert body["size"] == 24


def test_payload_custom_pagination():
    body = build_search_payload(
        q="Hitmonchan", seller_key="1d1b3bf6", page=2, page_size=50
    )
    assert body["from"] == 50
    assert body["size"] == 50


def test_payload_first_page_is_one_indexed_externally():
    """page=1 should mean from=0 (first page), not from=size."""
    body = build_search_payload(q="x", seller_key="k", page=1, page_size=10)
    assert body["from"] == 0


def test_payload_uses_sales_dismax_algorithm():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["algorithm"] == "sales_dismax"


def test_payload_us_shipping_default():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    assert body["context"]["shippingCountry"] == "US"


def test_payload_serializes_to_json_cleanly():
    body = build_search_payload(q="Hitmonchan", seller_key="1d1b3bf6")
    # Round-trip — guards against non-JSON-serializable values sneaking in.
    s = json.dumps(body)
    assert "1d1b3bf6" in s
    assert "Hitmonchan" not in s  # q lives in the URL, NOT the body


# ---- client (httpx + MockTransport, no real HTTP) ----------------------


def _mock_response_envelope(seller_key: str = "1d1b3bf6"):
    """Mirror of the real response shape captured 2026-05-03 from
    https://mp-search-api.tcgplayer.com/v1/search/request?q=dark+flareon.
    Trimmed to just the fields the parser cares about; the float types
    on productId / productConditionId are deliberate (the API really
    returns floats here)."""
    return {
        "errors": [],
        "results": [
            {
                "totalResults": 2,
                "results": [
                    {
                        "productId": 84597.0,
                        "productName": "Dark Flareon",
                        "setName": "Team Rocket",
                        "setCode": "TR",
                        "productLineName": "Pokemon",
                        "rarityName": "Uncommon",
                        "marketPrice": 19.73,
                        "lowestPrice": 19.73,
                        "lowestPriceWithShipping": 1.0,
                        "imageCount": 1.0,
                        "totalListings": 1.0,
                        "customAttributes": {"number": "35/82"},
                        "listings": [
                            {
                                "productConditionId": 1219950.0,
                                "sellerKey": seller_key,
                                "sellerName": "Tag Collects",
                                "price": 1.0,
                                "quantity": 1.0,
                                "condition": "Damaged",
                                "printing": "Unlimited",
                            }
                        ],
                    },
                    {
                        "productId": 12345.0,
                        "productName": "Dark Charizard",
                        "setName": "Team Rocket",
                        "setCode": "TR",
                        "productLineName": "Pokemon",
                        "rarityName": "Holo Rare",
                        "marketPrice": 89.99,
                        "lowestPrice": 80.0,
                        "lowestPriceWithShipping": 81.31,
                        "customAttributes": {"number": "4/82"},
                        "listings": [],
                    },
                ],
            }
        ],
    }


def test_client_posts_to_correct_url_with_q_in_query_string():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_response_envelope())

    transport = httpx.MockTransport(handler)
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    client.search("Hitmonchan")

    assert captured["method"] == "POST"
    # q in query string, not body
    assert "q=Hitmonchan" in captured["url"]
    assert captured["url"].startswith(SEARCH_URL)
    # seller filter in body
    assert captured["body"]["listingSearch"]["filters"]["term"]["sellerKey"] == ["1d1b3bf6"]


def test_client_url_encodes_multi_word_q():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_mock_response_envelope())

    transport = httpx.MockTransport(handler)
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    client.search("Dark Flareon")

    # URL-encoded space, not raw
    assert "q=Dark%20Flareon" in captured["url"] or "q=Dark+Flareon" in captured["url"]


def test_client_returns_parsed_results():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)

    res = client.search("Dark")
    assert len(res.products) == 2
    hit = res.products[0]
    # Float-coerced ID rounds to int.
    assert hit.product_id == 84597
    assert isinstance(hit.product_id, int)
    assert hit.name == "Dark Flareon"
    assert hit.set_name == "Team Rocket"
    assert hit.set_code == "TR"
    assert hit.product_line == "Pokemon"
    assert hit.rarity == "Uncommon"
    assert hit.number == "35/82"
    assert hit.market_price == 19.73
    assert hit.lowest_price == 19.73
    assert hit.lowest_price_with_shipping == 1.0
    assert res.total_results == 2


def test_listings_carry_product_condition_id_for_sku_crosswalk():
    """Each listing's productConditionId is exactly the SKU ID we have
    in our local DB's tcgplayer_product_id column. Critical for
    backfilling product images."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)

    res = client.search("Dark Flareon")
    hit = res.products[0]
    assert len(hit.listings) == 1
    listing = hit.listings[0]
    assert listing.product_condition_id == 1219950
    assert listing.seller_key == "1d1b3bf6"
    assert listing.condition == "Damaged"
    assert listing.printing == "Unlimited"
    assert listing.price == 1.0
    assert listing.quantity == 1


# ---- image url builder -------------------------------------------------


def test_build_image_url_default_size():
    assert build_image_url(84597) == (
        "https://tcgplayer-cdn.tcgplayer.com/product/84597_in_400x400.jpg"
    )


def test_build_image_url_custom_size():
    assert build_image_url(84597, size_px=200) == (
        "https://tcgplayer-cdn.tcgplayer.com/product/84597_in_200x200.jpg"
    )
    assert build_image_url(84597, size_px=1000) == (
        "https://tcgplayer-cdn.tcgplayer.com/product/84597_in_1000x1000.jpg"
    )


def test_build_image_url_from_hit():
    """Convenience: ProductHit.image_url(size) → CDN URL using its productId."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    res = client.search("Dark Flareon")
    hit = res.products[0]
    assert hit.image_url(400) == "https://tcgplayer-cdn.tcgplayer.com/product/84597_in_400x400.jpg"


def test_build_image_url_rejects_non_positive_size():
    with pytest.raises(ValueError):
        build_image_url(84597, size_px=0)
    with pytest.raises(ValueError):
        build_image_url(84597, size_px=-100)


# ---- find_by_sku / find_by_name ---------------------------------------


def test_find_by_sku_filters_to_listing_with_matching_product_condition_id():
    """Crosswalk: given a CSV SKU id (e.g., 1219950), find the parent
    product hit by scanning each candidate's listings array."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)

    # search by name, narrow by SKU
    hit = client.find_by_sku("Dark", sku_id=1219950)
    assert hit is not None
    assert hit.product_id == 84597
    assert hit.name == "Dark Flareon"


def test_find_by_sku_returns_none_when_no_match():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    assert client.find_by_sku("Dark", sku_id=99999999) is None


def test_find_by_attributes_matches_set_and_number():
    """Slower fallback when we don't have a SKU id yet — match by
    name + setName + customAttributes.number."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    hit = client.find_by_attributes(
        name="Dark Flareon", set_name="Team Rocket", number="35/82"
    )
    assert hit is not None
    assert hit.product_id == 84597


def test_find_by_attributes_rejects_wrong_number():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_mock_response_envelope())
    )
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    # Same name + set, different number → no match
    assert client.find_by_attributes(
        name="Dark Flareon", set_name="Team Rocket", number="99/82"
    ) is None


def test_client_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="busy"))
    client = MarketplaceSearchClient(seller_key="1d1b3bf6", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        client.search("anything")


def test_client_uses_settings_seller_key_by_default(monkeypatch):
    """Constructor without explicit seller_key falls back to settings."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_response_envelope())

    transport = httpx.MockTransport(handler)
    client = MarketplaceSearchClient(transport=transport)  # no seller_key
    client.search("x")
    # Default from app.config.settings.tcgplayer_seller_key
    from app.config import settings as app_settings

    assert captured["body"]["listingSearch"]["filters"]["term"]["sellerKey"] == [
        app_settings.tcgplayer_seller_key
    ]
