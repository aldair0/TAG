"""Backfill ``marketplace_product_id`` + the on-disk image for every
product with ``has_image=False``.

For each product:
  1. ``ProductImageFetcher.ensure_image`` searches the marketplace API
     (1 HTTP call) — caches the resolved ``marketplace_product_id`` on
     the row.
  2. Downloads the image from TCGPlayer's CDN (1 HTTP call) — saves to
     ``data/images/<set-slug>/<name-slug>__<number-slug>.jpg``.
  3. Flips ``Product.has_image=True``.

Re-runnable: rows that already have ``has_image=True`` (or whose JPEG
exists at the deterministic path) are skipped with no HTTP fired.

CLI:
    --sleep <sec>   Pause between products (default 0.5s).
    --limit <n>     Stop after ``n`` products (for smoke runs).
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import select

from app.db.models import Product, ProductKind
from app.db.session import SessionLocal
from app.sync.tcgplayer.product_images import ProductImageFetcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("backfill")

    with SessionLocal() as session:
        stmt = (
            select(Product)
            .where(Product.has_image.is_(False))
            .where(Product.kind != ProductKind.SUPPLY.value)
            .where(Product.tcgplayer_product_id.is_not(None))
            .order_by(Product.id)
        )
        if args.limit:
            stmt = stmt.limit(args.limit)

        products = session.execute(stmt).scalars().all()
        total = len(products)
        log.info("Backfilling %d products", total)

        if total == 0:
            log.info("Nothing to do.")
            return

        successes = failures = 0
        t0 = time.perf_counter()

        with ProductImageFetcher(session, size_px=400) as fetcher:
            for i, product in enumerate(products, start=1):
                pname = product.name
                psku = product.tcgplayer_product_id
                try:
                    result = fetcher.ensure_image(product)
                    session.commit()
                    if result is not None:
                        successes += 1
                        log.info(
                            "[%d/%d] ok %s (sku=%s) -> %s",
                            i, total, pname, psku, result.name,
                        )
                    else:
                        failures += 1
                        log.warning(
                            "[%d/%d] miss %s (sku=%s) — no marketplace hit",
                            i, total, pname, psku,
                        )
                except Exception:
                    session.rollback()
                    failures += 1
                    log.exception(
                        "[%d/%d] error %s (sku=%s)", i, total, pname, psku
                    )

                if i < total and args.sleep > 0:
                    time.sleep(args.sleep)

        elapsed = time.perf_counter() - t0
        log.info(
            "Done in %.1fs — %d ok, %d miss/error (rate ~%.2f/s)",
            elapsed,
            successes,
            failures,
            total / elapsed if elapsed > 0 else 0,
        )


if __name__ == "__main__":
    main()
