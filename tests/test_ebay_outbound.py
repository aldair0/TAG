"""eBay outbound worker behavior end-to-end."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.db.models import Channel, ChannelListing, OutboundChange, SyncRun
from app.sync.ebay import LoggingMockEbayClient, run_ebay_outbound
from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(session) -> None:
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def test_drains_pending_creates(session):
    _ingest(session)
    client = LoggingMockEbayClient()
    result = run_ebay_outbound(session, client)

    assert result.pulled == 15
    assert result.succeeded == 15
    assert result.failed == 0

    # Every change is now completed.
    pending = session.execute(
        select(OutboundChange).where(
            OutboundChange.channel == Channel.EBAY.value,
            OutboundChange.completed_at.is_(None),
        )
    ).scalars().all()
    assert pending == []

    # Each ebay channel_listing has an external id and sync_state=ok
    listings = session.execute(
        select(ChannelListing).where(ChannelListing.channel == Channel.EBAY.value)
    ).scalars().all()
    assert len(listings) == 15
    for cl in listings:
        assert cl.sync_state == "ok"
        assert cl.external_listing_id is not None
        assert cl.external_listing_id.startswith("MOCK-EBAY-")
        assert cl.last_pushed_quantity is not None
        assert cl.last_synced_at is not None

    # Mock client recorded one publish_listing per unit.
    pubs = [c for c in client.calls if c.op == "publish_listing"]
    assert len(pubs) == 15


def test_failing_client_marks_pending_with_error(session):
    _ingest(session)
    client = LoggingMockEbayClient(fail_on={"publish_listing"})
    result = run_ebay_outbound(session, client)

    assert result.pulled == 15
    assert result.succeeded == 0
    assert result.failed == 15

    rows = session.execute(
        select(OutboundChange).where(OutboundChange.channel == Channel.EBAY.value)
    ).scalars().all()
    for r in rows:
        assert r.completed_at is None
        assert r.attempts == 1
        assert "simulated failure" in (r.last_error or "")


def test_retry_after_failure_succeeds_and_clears_error(session):
    _ingest(session)
    bad = LoggingMockEbayClient(fail_on={"publish_listing"})
    run_ebay_outbound(session, bad)

    good = LoggingMockEbayClient()
    result = run_ebay_outbound(session, good)
    assert result.succeeded == 15
    assert result.failed == 0

    rows = session.execute(
        select(OutboundChange).where(OutboundChange.channel == Channel.EBAY.value)
    ).scalars().all()
    for r in rows:
        assert r.completed_at is not None
        assert r.attempts == 2  # one failed, one succeeded
        assert r.last_error is None


def test_idempotent_when_no_pending(session):
    _ingest(session)
    run_ebay_outbound(session, LoggingMockEbayClient())
    second = run_ebay_outbound(session, LoggingMockEbayClient())
    assert second.pulled == 0
    assert second.succeeded == 0


def test_sync_run_records_outbound(session):
    _ingest(session)
    run_ebay_outbound(session, LoggingMockEbayClient())
    runs = session.execute(
        select(SyncRun).where(SyncRun.worker == "ebay", SyncRun.direction == "outbound")
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].rows_seen == 15
    assert runs[0].rows_inserted == 15
