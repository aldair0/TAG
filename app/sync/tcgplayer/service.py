"""Top-level run_ingest entry point — what the Admin "Run sync now" button calls.

Wires together: source → parser → diff → apply → image fetch → SyncRun record.
The image fetch runs after the DB is updated so a network blip doesn't undo
the inventory work; we accept image-fetch failures and reconcile them later.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Product, SyncRun
from app.sync.tcgplayer.apply import apply_plan
from app.sync.tcgplayer.diff import build_plan
from app.sync.tcgplayer.images import ImageCache
from app.sync.tcgplayer.parser import IngestRow, ParseError, parse_row
from app.sync.tcgplayer.source import TCGPlayerSource

logger = logging.getLogger(__name__)


def run_ingest(
    source: TCGPlayerSource,
    session: Session,
    image_cache: ImageCache | None = None,
) -> SyncRun:
    """Run a full TCGPlayer ingest cycle. Returns the persisted SyncRun row.

    The session is committed by this function — caller doesn't need to.
    """
    run = SyncRun(
        worker="tcgplayer",
        direction="inbound",
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()

    parsed: list[IngestRow] = []
    parse_errors: list[str] = []
    # SKUs whose Total Quantity is blank or 0 in the new CSV. We DON'T
    # ingest them (no insert / no update of incoming data), but if any
    # of them already exist in the DB they get deleted — for a card
    # store, one specific physical card per SKU; once it's gone from
    # the seller's inventory it's gone for good.
    zero_qty_skus: set[int] = set()
    try:
        for raw in source.fetch_rows():
            try:
                row = parse_row(raw)
            except ParseError as e:
                parse_errors.append(str(e))
                logger.warning("Skipping unparseable row: %s", e)
                continue
            if row.quantity <= 0:
                if row.tcgplayer_product_id is not None:
                    zero_qty_skus.add(row.tcgplayer_product_id)
                continue
            parsed.append(row)

        run.rows_seen = len(parsed)
        if zero_qty_skus:
            logger.info(
                "Found %d SKUs with Total Quantity blank or 0 — "
                "checking for existing rows to drain",
                len(zero_qty_skus),
            )

        plan = build_plan(parsed, session)
        result = apply_plan(plan, session)
        run.rows_inserted = result.rows_inserted
        run.rows_updated = result.rows_updated

        # Drain step: any SKU now at 0 in the CSV that ALSO exists in
        # the DB gets removed. Cascade rules clean up inventory_units
        # and channel_listings; outbound_change / sale_line keep their
        # row but the inventory_unit_id FK goes NULL.
        if zero_qty_skus:
            deleted = _prune_zero_qty_products(zero_qty_skus, session)
            if deleted:
                logger.info(
                    "Deleted %d existing product(s) whose CSV row "
                    "went to 0/blank quantity",
                    deleted,
                )

        # Fetch images for products in this CSV that don't have one
        # yet — covers both freshly-inserted products AND existing rows
        # whose previous marketplace lookup missed (e.g., the seller
        # didn't have an active marketplace listing at the time, so the
        # seller-filtered search returned no hit). Each ingest gives
        # those another shot. ``ensure_image`` is a no-op for products
        # whose image is already on disk, so the cost is bounded by
        # the count of has_image=False rows seen here.
        #
        # TAG_SKIP_IMAGE_FETCH=1 short-circuits the whole block — used
        # for bulk imports of 100K+ rows where we don't want to issue
        # that many HTTPS calls. Images can be backfilled later via
        # tools/backfill_all_images.py.
        skip_images = os.environ.get("TAG_SKIP_IMAGE_FETCH") == "1"
        seen_ids = {
            r.tcgplayer_product_id
            for r in parsed
            if r.tcgplayer_product_id is not None
        }
        if seen_ids and not skip_images:
            from sqlalchemy import select

            from app.sync.tcgplayer.product_images import ProductImageFetcher

            # SQLite has a 32766-parameter cap on a single IN clause.
            # Real CSVs can blow past that; chunk to be safe.
            _IN_CHUNK = 500
            ids_list = list(seen_ids)
            products_needing_image: list[Product] = []
            for start in range(0, len(ids_list), _IN_CHUNK):
                chunk = ids_list[start : start + _IN_CHUNK]
                stmt = select(Product).where(
                    Product.tcgplayer_product_id.in_(chunk),
                    Product.has_image.is_(False),
                )
                products_needing_image.extend(
                    session.execute(stmt).scalars().all()
                )

            if products_needing_image:
                cache = image_cache or _default_image_cache()
                try:
                    with ProductImageFetcher(
                        session, image_cache=cache, size_px=400
                    ) as fetcher:
                        ok = miss = 0
                        for i, product in enumerate(products_needing_image, 1):
                            if fetcher.ensure_image(product) is not None:
                                ok += 1
                            else:
                                miss += 1
                            # Commit every 25 products so a late failure
                            # doesn't undo earlier image-flag flips.
                            if i % 25 == 0:
                                session.commit()
                        session.commit()
                        logger.info(
                            "Image fetch: %d products lacked images; "
                            "%d fetched, %d still missing",
                            len(products_needing_image),
                            ok,
                            miss,
                        )
                finally:
                    if image_cache is None:
                        cache.close()
            else:
                logger.info(
                    "Image fetch: all %d touched products already have images",
                    len(seen_ids),
                )
        elif seen_ids and skip_images:
            logger.info(
                "TAG_SKIP_IMAGE_FETCH=1 — skipped image checks for %d touched products",
                len(seen_ids),
            )

        if parse_errors:
            run.error = "\n".join(parse_errors[:10])

    except Exception as e:
        logger.exception("Ingest failed")
        session.rollback()
        run.error = f"{type(e).__name__}: {e}"
        run.ended_at = datetime.now(timezone.utc)
        # Re-attach + persist the failure record on a fresh transaction.
        session.add(run)
        session.commit()
        raise

    run.ended_at = datetime.now(timezone.utc)
    session.commit()
    return run


def _default_image_cache() -> ImageCache:
    """Default image cache rooted at ``<data_dir>/images``."""
    sqlite_path = settings.sqlite_path
    if sqlite_path is not None:
        root = sqlite_path.parent / "images"
    else:
        root = Path("data") / "images"
    return ImageCache(root)


def _prune_zero_qty_products(skus: set[int], session: Session) -> int:
    """Delete Product rows whose ``tcgplayer_product_id`` is in
    ``skus``. Schema cascade cleans up child inventory_unit and
    channel_listing rows; outbound_change.inventory_unit_id and
    sale_line.inventory_unit_id are ON DELETE SET NULL so historical
    queue + sales rows survive (with the FK going to NULL).

    SKUs in the set that don't exist in the DB are silently no-op'd.
    Returns the number of Product rows actually deleted.
    """
    if not skus:
        return 0
    from sqlalchemy import delete as sql_delete

    # SQLite caps a single IN list around 32K params; chunk to be safe.
    _CHUNK = 500
    sku_list = list(skus)
    total = 0
    for start in range(0, len(sku_list), _CHUNK):
        chunk = sku_list[start : start + _CHUNK]
        stmt = sql_delete(Product).where(
            Product.tcgplayer_product_id.in_(chunk)
        )
        result = session.execute(stmt)
        total += result.rowcount or 0
    session.flush()
    return total
