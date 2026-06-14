"""Live end-to-end demo of the marketplace-driven image fetcher.

Exercises ProductImageFetcher.ensure_image against two real products
already in the local DB: Dark Flareon (Team Rocket) and Hitmonchan
(Base Set). Asserts the marketplace_product_id gets resolved and
cached, and the JPEG lands on disk.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.db.models import Product
from app.db.session import SessionLocal
from app.sync.tcgplayer.product_images import ProductImageFetcher

TARGETS = [
    ("Dark Flareon", 1219950),
    ("Hitmonchan", 2999671),
]


def main() -> None:
    with SessionLocal() as session:
        with ProductImageFetcher(session, size_px=400) as fetcher:
            for name, sku in TARGETS:
                product = session.execute(
                    select(Product).where(Product.tcgplayer_product_id == sku)
                ).scalar_one_or_none()
                if product is None:
                    print(f"[{name}] not in DB — skipping")
                    continue

                before_id = product.marketplace_product_id
                print(f"[{name}] sku={sku} marketplace_product_id_before={before_id}")

                local_path = fetcher.ensure_image(product)
                session.commit()  # persist marketplace_product_id

                after_id = product.marketplace_product_id
                print(f"  marketplace_product_id_after = {after_id}")
                if local_path is None:
                    print(f"  ensure_image returned None (lookup or download failed)")
                    continue
                size_bytes = local_path.stat().st_size if local_path.exists() else 0
                print(f"  saved to: {local_path}")
                print(f"  file size: {size_bytes:,} bytes")
                print()


if __name__ == "__main__":
    main()
