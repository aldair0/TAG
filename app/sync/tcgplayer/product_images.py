"""Marketplace-driven product image cache.

The seller-export CSV doesn't carry usable Photo URLs (the column is
mostly blank for our seller's listings). So we resolve each product's
public-facing **marketplace product id** via the search API, cache it on
``Product.marketplace_product_id``, then fetch the image from the
predictable CDN URL ``tcgplayer-cdn.tcgplayer.com/product/<id>_in_<S>x<S>.jpg``.

One image per product (not per SKU) — all conditions of the same card
share the same product page and image. Storing keyed on
marketplace_product_id avoids fetching the same JPEG once per condition.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Product, ProductKind
from app.sync.tcgplayer.image_paths import image_local_path
from app.sync.tcgplayer.images import ImageCache
from app.sync.tcgplayer.marketplace_search import (
    IMAGE_CDN,
    MarketplaceSearchClient,
)

logger = logging.getLogger(__name__)


def cdn_filename(marketplace_product_id: int, size_px: int) -> str:
    """The CDN's own filename for this marketplace_product_id at a given
    size. We download from this URL but save to a different (set/name/
    number-derived) local path — see ``image_paths.image_local_path``."""
    return f"{marketplace_product_id}_in_{size_px}x{size_px}.jpg"


# Backward-compat: existing tests import ``image_filename``.
image_filename = cdn_filename


class ProductImageFetcher:
    """Glues the marketplace search lookup, the on-disk cache, and the
    Product table together so a single ``ensure_image(product)`` call is
    all the ingest pipeline needs to call.

    Designed for dependency injection — pass ``search_client`` and
    ``image_cache`` so tests can mock cleanly. Defaults wire up real
    httpx-backed clients.
    """

    def __init__(
        self,
        session: Session,
        *,
        search_client: MarketplaceSearchClient | None = None,
        image_cache: ImageCache | None = None,
        size_px: int = 400,
        images_root: Path | None = None,
    ) -> None:
        self._session = session
        self._search = search_client
        self._cache = image_cache
        self._size = size_px
        self._images_root = images_root  # None → image_paths.IMAGES_ROOT
        self._owns_search = search_client is None
        self._owns_cache = image_cache is None

    def __enter__(self) -> "ProductImageFetcher":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_search and self._search is not None:
            self._search.close()
        if self._owns_cache and self._cache is not None:
            self._cache.close()

    def _ensure_search(self) -> MarketplaceSearchClient:
        if self._search is None:
            self._search = MarketplaceSearchClient()
        return self._search

    def _ensure_cache(self) -> ImageCache:
        if self._cache is None:
            self._cache = ImageCache(Path("data/images"))
        return self._cache

    def ensure_image(self, product: Product) -> Path | None:
        """Returns the local path to the image (existing or newly
        downloaded), or ``None`` on any failure (lookup miss, network
        error, supply product). Failures are logged, not raised — the
        ingest run shouldn't abort over a missing thumbnail.

        The on-disk path is derived from the product's set/name/number
        (see ``image_paths.image_local_path``), NOT from the
        marketplace_product_id. This makes images discoverable by any
        subsystem with just a Product row.
        """
        if product.kind == ProductKind.SUPPLY.value:
            return None
        if product.tcgplayer_product_id is None:
            return None

        # Output path: deterministic from set/name/number.
        local_path = image_local_path(
            set_name=product.set,
            name=product.name,
            number=product.number,
            root=self._images_root,
        )

        # Detect-then-fetch: if the JPEG is already on disk, just flip
        # has_image=True (covers re-imports / earlier successful fetches
        # that didn't propagate the flag) and return.
        if local_path.exists() and local_path.stat().st_size > 0:
            if not product.has_image:
                product.has_image = True
                self._session.flush()
            return local_path

        # We need the marketplace_product_id to construct the CDN URL.
        # Cached on the Product row to avoid a search on every retry.
        marketplace_id = product.marketplace_product_id
        if marketplace_id is None:
            marketplace_id = self._resolve_marketplace_id(product)
            if marketplace_id is None:
                return None

        url = f"{IMAGE_CDN}/{cdn_filename(marketplace_id, self._size)}"
        result = self._ensure_cache().fetch_to_path(local_path, url)
        if result is not None:
            product.has_image = True
            self._session.flush()
        return result

    def _resolve_marketplace_id(self, product: Product) -> int | None:
        """Resolve marketplace_product_id via a three-stage search cascade.

        1. Seller-filtered SKU match — fast, works when our listing is active.
        2. Global SKU match — works when our listing is inactive/out-of-stock
           but another seller has the same condition listed.
        3. Global name+set match — last resort for cards no one currently
           lists; uses product attributes instead of a live listing.

        Result is cached on the Product row so re-runs skip the lookup.
        """
        search = self._ensure_search()
        try:
            # Stages 1 & 2 are both inside find_by_sku (seller → global fallback).
            hit = search.find_by_sku(product.name, sku_id=product.tcgplayer_product_id)
            if hit is None:
                hit = search.find_global_by_attributes(
                    name=product.name,
                    set_name=product.set,
                    number=product.number,
                )
        except Exception:
            logger.warning(
                "Marketplace search failed for product=%s (sku=%s)",
                product.name,
                product.tcgplayer_product_id,
                exc_info=True,
            )
            return None
        if hit is None:
            logger.info(
                "No marketplace hit for product=%s (sku=%s)",
                product.name,
                product.tcgplayer_product_id,
            )
            return None
        product.marketplace_product_id = hit.product_id
        self._session.flush()
        return hit.product_id
