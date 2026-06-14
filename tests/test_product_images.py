"""Tests for the marketplace-driven image fetcher.

The fetcher writes to deterministic per-set/per-card paths under
``data/images/<set-slug>/<name-slug>__<number-slug>.jpg`` (or whatever
``images_root`` the test passes). On success it flips
``Product.has_image`` to True. The marketplace_product_id is cached on
the Product the first time it's resolved so subsequent ensures don't
re-search.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.db.models import Product, ProductKind
from app.sync.tcgplayer.images import ImageCache
from app.sync.tcgplayer.marketplace_search import (
    Listing,
    MarketplaceSearchClient,
    ProductHit,
)
from app.sync.tcgplayer.product_images import ProductImageFetcher


def _make_product(
    session,
    *,
    sku: int,
    name: str,
    set_name: str = "Team Rocket",
    number: str = "35/82",
    marketplace_id: int | None = None,
    has_image: bool = False,
):
    p = Product(
        tcgplayer_product_id=sku,
        marketplace_product_id=marketplace_id,
        kind=ProductKind.SINGLE.value,
        name=name,
        set=set_name,
        number=number,
        has_image=has_image,
    )
    session.add(p)
    session.flush()
    return p


def _hit(*, product_id: int, name: str, sku: int) -> ProductHit:
    return ProductHit(
        product_id=product_id,
        name=name,
        set_name="Team Rocket",
        set_code="TR",
        product_line="Pokemon",
        rarity="Uncommon",
        number="35/82",
        market_price=10.0,
        lowest_price=8.0,
        lowest_price_with_shipping=9.0,
        listings=[
            Listing(
                product_condition_id=sku,
                seller_key="1d1b3bf6",
                seller_name="Tag Collects",
                price=8.0,
                quantity=1,
                condition="Near Mint",
                printing="Unlimited",
            )
        ],
    )


# ---- ProductImageFetcher: skip-when-cached path -----------------------


def test_skips_download_and_search_when_local_file_exists(session, tmp_path):
    """If the JPEG is already on disk at the deterministic path, no
    HTTP fires — neither the search call nor the image download. Also
    flips has_image=True if it wasn't already."""
    p = _make_product(
        session,
        sku=1219950,
        name="Dark Flareon",
        marketplace_id=84597,
        has_image=False,
    )
    expected = tmp_path / "team-rocket" / "dark-flareon__35-82.jpg"
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b"already-cached-bytes")

    search = MagicMock(spec=MarketplaceSearchClient)
    cache = MagicMock(spec=ImageCache)
    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    result = fetcher.ensure_image(p)

    assert result == expected
    assert p.has_image is True
    search.find_by_sku.assert_not_called()
    cache.fetch_to_path.assert_not_called()


# ---- ProductImageFetcher: download + flag-flip path -------------------


def test_downloads_to_set_and_name_path_then_sets_has_image(session, tmp_path):
    p = _make_product(
        session,
        sku=1219950,
        name="Dark Flareon",
        marketplace_id=84597,
        has_image=False,
    )

    expected = tmp_path / "team-rocket" / "dark-flareon__35-82.jpg"
    search = MagicMock(spec=MarketplaceSearchClient)
    cache = MagicMock(spec=ImageCache)
    cache.fetch_to_path.return_value = expected

    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    fetcher.ensure_image(p)

    cache.fetch_to_path.assert_called_once()
    args, _ = cache.fetch_to_path.call_args
    local_path, url = args[0], args[1]
    assert local_path == expected
    assert url == "https://tcgplayer-cdn.tcgplayer.com/product/84597_in_400x400.jpg"
    assert p.has_image is True


# ---- ProductImageFetcher: marketplace lookup path ---------------------


def test_resolves_marketplace_id_when_missing_then_caches(session, tmp_path):
    p = _make_product(session, sku=1219950, name="Dark Flareon")
    assert p.marketplace_product_id is None

    search = MagicMock(spec=MarketplaceSearchClient)
    search.find_by_sku.return_value = _hit(
        product_id=84597, name="Dark Flareon", sku=1219950
    )
    cache = MagicMock(spec=ImageCache)
    cache.fetch_to_path.return_value = (
        tmp_path / "team-rocket" / "dark-flareon__35-82.jpg"
    )

    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    fetcher.ensure_image(p)

    assert p.marketplace_product_id == 84597
    search.find_by_sku.assert_called_once_with("Dark Flareon", sku_id=1219950)


def test_returns_none_when_marketplace_lookup_fails(session, tmp_path):
    p = _make_product(session, sku=99999999, name="Nonexistent Card")

    search = MagicMock(spec=MarketplaceSearchClient)
    search.find_by_sku.return_value = None
    cache = MagicMock(spec=ImageCache)

    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    result = fetcher.ensure_image(p)

    assert result is None
    cache.fetch_to_path.assert_not_called()
    assert p.marketplace_product_id is None
    assert p.has_image is False


def test_does_not_set_has_image_when_download_fails(session, tmp_path):
    p = _make_product(
        session, sku=1219950, name="Dark Flareon", marketplace_id=84597
    )

    search = MagicMock(spec=MarketplaceSearchClient)
    cache = MagicMock(spec=ImageCache)
    cache.fetch_to_path.return_value = None  # download failed

    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    fetcher.ensure_image(p)

    assert p.has_image is False


# ---- ProductImageFetcher: supplies are bypassed ----------------------


def test_fetches_skipped_for_supplies(session, tmp_path):
    p = Product(
        tcgplayer_product_id=None,
        kind=ProductKind.SUPPLY.value,
        name="Dragon Shield Sleeves",
        is_online_listable=False,
        has_image=False,
    )
    session.add(p)
    session.flush()

    search = MagicMock(spec=MarketplaceSearchClient)
    cache = MagicMock(spec=ImageCache)

    fetcher = ProductImageFetcher(
        session,
        search_client=search,
        image_cache=cache,
        size_px=400,
        images_root=tmp_path,
    )
    result = fetcher.ensure_image(p)

    assert result is None
    assert p.has_image is False
    search.find_by_sku.assert_not_called()
    cache.fetch_to_path.assert_not_called()
