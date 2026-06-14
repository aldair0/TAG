"""Smoke tests for the new Phase 2 admin routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from tests.test_tcgplayer_ingest import _NoopImageCache


def _ingest(session):
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )


def test_outbound_index_empty(client):
    r = client.get("/admin/sync/outbound")
    assert r.status_code == 200
    assert "No outbound changes match" in r.text


def test_outbound_index_after_ingest(client, session):
    _ingest(session)
    r = client.get("/admin/sync/outbound")
    assert r.status_code == 200
    # 30 rows total (15 ebay + 15 shopify)
    assert "ebay" in r.text
    assert "shopify_pos" in r.text
    assert "create" in r.text


def test_outbound_filter_by_channel(client, session):
    _ingest(session)
    r = client.get("/admin/sync/outbound?channel=ebay")
    assert r.status_code == 200
    # shopify_pos shouldn't appear in any data row. The dropdowns contain it
    # but the <tbody> shouldn't.
    tbody = r.text.split("<tbody", 1)[-1].split("</tbody>", 1)[0]
    assert "ebay" in tbody
    assert "shopify_pos" not in tbody


def test_run_ebay_button(client, session):
    _ingest(session)
    r = client.post("/admin/sync/run_ebay", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/admin/sync/")


def test_run_shopify_button(client, session):
    _ingest(session)
    r = client.post("/admin/sync/run_shopify", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/admin/sync/")


def test_sync_index_shows_pending_counts(client, session):
    _ingest(session)
    r = client.get("/admin/sync/")
    assert r.status_code == 200
    # 15 pending each for ebay + shopify_pos
    assert "15" in r.text  # appears as the pending count
    assert "ebay" in r.text.lower()
    assert "shopify" in r.text.lower()
