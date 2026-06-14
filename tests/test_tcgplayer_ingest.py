"""End-to-end ingest happy-path against the synthetic fixture CSV.

Image fetching is replaced with a no-op cache so we don't hit the network.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import ChannelListing, InventoryUnit, Product, SyncRun
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from app.sync.tcgplayer.images import ImageCache

FIXTURE_V1 = Path("test_data/tcgplayer_fixture.csv")
FIXTURE_V2 = Path("test_data/tcgplayer_fixture_v2.csv")


class _NoopImageCache(ImageCache):
    """Image cache that pretends every fetch fails. Used in tests."""

    def __init__(self) -> None:
        # Bypass the parent __init__ — no httpx client, no disk.
        self.root = Path("/dev/null")
        self._owns_client = False
        self._client = None  # type: ignore[assignment]

    def fetch_if_missing(self, tcgplayer_id, source_url):
        return None

    def close(self) -> None:
        pass


@pytest.fixture
def noop_images() -> ImageCache:
    return _NoopImageCache()


def test_zero_qty_rows_are_skipped(tmp_path, session, noop_images):
    """User rule: 'If a total quantity is blank or 0, we do nothing
    with that row.' Verifies the ingest treats both blank and zero
    Total Quantity values as no-op rows — no Product, no InventoryUnit
    is created for them."""
    csv_path = tmp_path / "skip_test.csv"
    csv_path.write_text(
        "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,"
        "Condition,TCG Marketplace Price,Total Quantity,My Store Reserve Quantity,"
        "Photo URL\n"
        # row with qty=0 → must be skipped
        "9000001,Magic,TestSet,Zero Qty Card,,001,Common,Near Mint,1.00,0,0,\n"
        # row with blank qty → must be skipped
        "9000002,Magic,TestSet,Blank Qty Card,,002,Common,Near Mint,1.00,,0,\n"
        # row with qty>0 → must be ingested
        "9000003,Magic,TestSet,Real Qty Card,,003,Common,Near Mint,1.00,2,1,\n",
        encoding="utf-8",
    )
    run = run_ingest(FixtureTCGPlayerSource(csv_path), session, image_cache=noop_images)
    assert run.error is None

    products = session.execute(
        select(Product).where(Product.tcgplayer_product_id.in_([9000001, 9000002, 9000003]))
    ).scalars().all()
    assert {p.tcgplayer_product_id for p in products} == {9000003}, (
        "only the qty>0 row should land in the DB"
    )
    # The skip-counted "rows seen" should still reflect what was filtered out,
    # so admin can see the volume.
    assert run.rows_inserted == 1


def test_existing_sku_going_to_zero_qty_deletes_the_product(tmp_path, session, noop_images):
    """Bug: when an existing SKU's CSV row drops to qty=0/blank, the
    parser used to skip the row entirely and the inventory_unit kept
    its stale qty>0 forever. For a card store (one specific physical
    item per SKU), the right behavior is to delete — once that exact
    card is sold, it's gone."""
    from app.db.models import InventoryUnit, Product, ProductKind

    # Pre-seed: a product that's currently in stock locally.
    existing = Product(
        tcgplayer_product_id=5555,
        kind=ProductKind.SINGLE.value,
        name="Going Away Soon",
        set="TestSet",
        number="042",
    )
    session.add(existing)
    session.flush()
    session.add(
        InventoryUnit(
            product_id=existing.id,
            condition="Near Mint",
            quantity_on_hand=2,
            unit_price=Decimal("3.00"),
        )
    )
    session.commit()
    pid = existing.id  # capture before delete

    # New CSV: same SKU, but Total Quantity is 0.
    csv_path = tmp_path / "drain.csv"
    csv_path.write_text(
        "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,"
        "Condition,TCG Marketplace Price,Total Quantity,My Store Reserve Quantity,"
        "Photo URL\n"
        "5555,Magic,TestSet,Going Away Soon,,042,Common,Near Mint,3.00,0,0,\n",
        encoding="utf-8",
    )

    run_ingest(FixtureTCGPlayerSource(csv_path), session, image_cache=noop_images)
    session.expire_all()

    assert session.execute(
        select(Product).where(Product.id == pid)
    ).scalar_one_or_none() is None, (
        "Product should have been deleted when its SKU's CSV row went to qty=0"
    )
    # InventoryUnit should be cascade-deleted with the product.
    assert session.execute(
        select(InventoryUnit).where(InventoryUnit.product_id == pid)
    ).scalar_one_or_none() is None


def test_blank_qty_on_existing_sku_also_deletes(tmp_path, session, noop_images):
    """Blank Total Quantity should be treated identically to literal 0."""
    from app.db.models import InventoryUnit, Product, ProductKind

    existing = Product(
        tcgplayer_product_id=5556,
        kind=ProductKind.SINGLE.value,
        name="Blank Going",
        set="TestSet",
        number="043",
    )
    session.add(existing)
    session.flush()
    session.add(
        InventoryUnit(
            product_id=existing.id,
            condition="Near Mint",
            quantity_on_hand=1,
        )
    )
    session.commit()
    pid = existing.id

    csv_path = tmp_path / "blank.csv"
    csv_path.write_text(
        "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,"
        "Condition,TCG Marketplace Price,Total Quantity,My Store Reserve Quantity,"
        "Photo URL\n"
        "5556,Magic,TestSet,Blank Going,,043,Common,Near Mint,3.00,,0,\n",
        encoding="utf-8",
    )
    run_ingest(FixtureTCGPlayerSource(csv_path), session, image_cache=noop_images)
    session.expire_all()
    assert session.execute(
        select(Product).where(Product.id == pid)
    ).scalar_one_or_none() is None


def test_zero_qty_for_unknown_sku_is_still_a_noop(tmp_path, session, noop_images):
    """Brand-new SKU at qty=0 — don't insert and don't crash trying to
    delete a row that doesn't exist."""
    from app.db.models import Product

    csv_path = tmp_path / "new_zero.csv"
    csv_path.write_text(
        "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,"
        "Condition,TCG Marketplace Price,Total Quantity,My Store Reserve Quantity,"
        "Photo URL\n"
        "8888,Magic,TestSet,Brand New Zero,,099,Common,Near Mint,1.00,0,0,\n",
        encoding="utf-8",
    )
    run_ingest(FixtureTCGPlayerSource(csv_path), session, image_cache=noop_images)
    session.expire_all()
    assert session.execute(
        select(Product).where(Product.tcgplayer_product_id == 8888)
    ).scalar_one_or_none() is None


def test_ingest_retries_image_fetch_for_existing_products_without_image(
    tmp_path, session, monkeypatch
):
    """Regression: image-fetch wasn't being retried for products that
    already exist in the DB but have ``has_image=False`` (e.g., a
    previous marketplace search returned no hit for them). Each ingest
    should give every product touched by the CSV another chance."""
    monkeypatch.delenv("TAG_SKIP_IMAGE_FETCH", raising=False)

    # Pre-seed the DB with a product that ALREADY exists but is missing
    # its image. Mimics the post-backfill state where a marketplace
    # search miss left has_image=False.
    from app.db.models import InventoryUnit, Product, ProductKind

    existing = Product(
        tcgplayer_product_id=4444,
        kind=ProductKind.SINGLE.value,
        name="Pre-existing No Image",
        set="TestSet",
        number="001",
        has_image=False,
    )
    session.add(existing)
    session.flush()
    session.add(
        InventoryUnit(
            product_id=existing.id,
            condition="Near Mint",
            quantity_on_hand=1,
        )
    )
    session.commit()

    # Build a CSV that includes that same SKU. The diff engine will
    # treat it as a price-update or unchanged row — NOT a "new product".
    csv_path = tmp_path / "retry.csv"
    csv_path.write_text(
        "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,"
        "Condition,TCG Marketplace Price,Total Quantity,My Store Reserve Quantity,"
        "Photo URL\n"
        "4444,Magic,TestSet,Pre-existing No Image,,001,Common,Near Mint,1.00,1,0,\n",
        encoding="utf-8",
    )

    # Capture which products ensure_image gets called with.
    seen_products: list[int] = []

    class _CapturingFetcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def ensure_image(self, product):
            seen_products.append(product.tcgplayer_product_id)
            # Pretend we couldn't fetch — has_image stays False.
            return None

    monkeypatch.setattr(
        "app.sync.tcgplayer.product_images.ProductImageFetcher",
        _CapturingFetcher,
    )

    run_ingest(FixtureTCGPlayerSource(csv_path), session)

    assert 4444 in seen_products, (
        "ensure_image should be called for existing products that lack "
        f"an image, not just new ones. Saw: {seen_products}"
    )


def test_tag_skip_image_fetch_env_var_skips_fetcher(session, monkeypatch):
    """Setting TAG_SKIP_IMAGE_FETCH=1 must mean run_ingest never calls
    fetch_if_missing on any cache — including the default one — so the
    initial bulk import doesn't fan out to 219K HTTP calls."""

    calls: list[tuple[int, str]] = []

    class _SpyCache(ImageCache):
        def __init__(self) -> None:
            self.root = Path("/dev/null")
            self._owns_client = False
            self._client = None  # type: ignore[assignment]

        def fetch_if_missing(self, tcgplayer_id, source_url):
            calls.append((tcgplayer_id, source_url))
            return None

        def close(self) -> None:
            pass

    monkeypatch.setenv("TAG_SKIP_IMAGE_FETCH", "1")
    spy = _SpyCache()
    run = run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=spy)
    assert run.error is None
    assert run.rows_inserted > 0  # confirms the run actually did work
    assert calls == []  # no image fetches attempted


def test_first_run_ingests_full_fixture(session, noop_images):
    run = run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=noop_images)

    assert run.error is None
    assert run.rows_seen == 15  # 9 cards (3 with two conditions = 12 rows) + 3 sealed
    assert run.rows_inserted == 15

    # 9 unique TCGplayer IDs for cards (501001..501005, 602001..602004) + 3 sealed = 12 distinct products
    products = session.execute(select(Product)).scalars().all()
    assert len(products) == 12

    # 15 inventory_unit rows (one per CSV row)
    units = session.execute(select(InventoryUnit)).scalars().all()
    assert len(units) == 15

    # Every unit has a TCGPlayer channel_listing
    listings = session.execute(
        select(ChannelListing).where(ChannelListing.channel == "tcgplayer")
    ).scalars().all()
    assert len(listings) == 15

    # Spot-check: Lightning Helix has 2 conditions
    lh = session.execute(
        select(Product).where(Product.tcgplayer_product_id == 501001)
    ).scalar_one()
    conditions = sorted(u.condition for u in lh.inventory_units)
    assert conditions == ["Lightly Played", "Near Mint"]

    # Spot-check sealed: kind=sealed, sealed_subtype set, condition NULL
    booster = session.execute(
        select(Product).where(Product.tcgplayer_product_id == 700001)
    ).scalar_one()
    assert booster.kind == "sealed"
    assert booster.sealed_subtype == "Booster Box"
    assert booster.inventory_units[0].condition is None
    assert booster.inventory_units[0].quantity_on_hand == 4


def test_second_run_is_idempotent(session, noop_images):
    run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=noop_images)
    second = run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=noop_images)

    assert second.rows_seen == 15
    assert second.rows_inserted == 0
    assert second.rows_updated == 0


def test_v2_detects_qty_and_price_changes_and_one_new_card(session, noop_images):
    run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=noop_images)
    second = run_ingest(FixtureTCGPlayerSource(FIXTURE_V2), session, image_cache=noop_images)

    # v2 vs v1:
    #   - 501001 LP: qty 2 → 1 (qty change)
    #   - 501002 NM: price 12.00 → 14.00 (price change)
    #   - 700001 sealed: qty 4 → 3 (qty change)
    #   - 501006 NM: new card (insertion)
    assert second.rows_inserted == 1
    assert second.rows_updated == 3

    new_card = session.execute(
        select(Product).where(Product.tcgplayer_product_id == 501006)
    ).scalar_one()
    assert new_card.name == "Helping Hand"
    assert new_card.inventory_units[0].quantity_on_hand == 12


def test_sync_run_recorded_with_timestamps(session, noop_images):
    run = run_ingest(FixtureTCGPlayerSource(FIXTURE_V1), session, image_cache=noop_images)
    assert run.id is not None
    assert run.started_at is not None
    assert run.ended_at is not None
    assert run.ended_at >= run.started_at
    persisted = session.execute(select(SyncRun)).scalars().all()
    assert len(persisted) == 1
