"""Shopify outbound worker — same shape as eBay test, but checking that
shopify_product_id / variant_id are populated on the Product row."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.db.models import Channel, ChannelListing, Product
from app.sync.shopify import LoggingMockShopifyClient, run_shopify_outbound
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(session):
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def test_publishes_every_product_to_shopify(session):
    _ingest(session)
    client = LoggingMockShopifyClient()
    result = run_shopify_outbound(session, client)

    assert result.pulled == 15
    assert result.succeeded == 15
    assert result.failed == 0

    # Mock client got 15 publish_product calls (one per inventory_unit).
    pubs = [c for c in client.calls if c.op == "publish_product"]
    assert len(pubs) == 15

    # Every product now has shopify_product_id / variant_id populated.
    products = session.execute(select(Product)).scalars().all()
    # 12 distinct products in the fixture
    assert len(products) == 12
    for p in products:
        assert p.shopify_product_id is not None
        assert p.shopify_variant_id is not None

    # ChannelListing rows for shopify_pos all populated.
    listings = session.execute(
        select(ChannelListing).where(ChannelListing.channel == Channel.SHOPIFY_POS.value)
    ).scalars().all()
    assert len(listings) == 15
    for cl in listings:
        assert cl.sync_state == "ok"
        assert cl.external_listing_id is not None  # the variant id


def test_supply_publishes_to_shopify_only(session):
    """When a supply is enqueued, only Shopify gets a CREATE row.

    Phase 4 will add the Admin UI for entering supplies; this verifies the
    enqueue contract is set up correctly without it.
    """
    from decimal import Decimal

    from app.db.models import InventoryUnit, ProductKind
    from app.outbound import enqueue_for_new_unit

    p = Product(
        kind=ProductKind.SUPPLY.value,
        name="Dragon Shield Matte Black 100ct",
        supply_category="Sleeves",
        is_online_listable=False,
    )
    session.add(p)
    session.flush()
    u = InventoryUnit(product_id=p.id, condition=None, quantity_on_hand=10, unit_price=Decimal("12.99"))
    session.add(u)
    session.flush()
    enqueue_for_new_unit(session, u)
    session.flush()

    client = LoggingMockShopifyClient()
    result = run_shopify_outbound(session, client)
    assert result.succeeded == 1

    # Now p has shopify ids
    session.refresh(p)
    assert p.shopify_product_id is not None
    assert p.shopify_variant_id is not None

    # The publish_product call carried product_type="Supply / Sleeves"
    publish = next(c for c in client.calls if c.op == "publish_product")
    assert publish.kwargs["product_type"] == "Supply / Sleeves"
