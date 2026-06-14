"""Smoke tests for the new Phase 1 admin routes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.sync.tcgplayer import FixtureTCGPlayerSource, run_ingest
from app.sync.tcgplayer.images import ImageCache


class _NoopImageCache(ImageCache):
    def __init__(self) -> None:
        self.root = Path("/dev/null")
        self._owns_client = False
        self._client = None  # type: ignore[assignment]

    def fetch_if_missing(self, *_args, **_kwargs):
        return None

    def close(self) -> None:
        pass


def test_inventory_empty(client):
    r = client.get("/admin/inventory/")
    assert r.status_code == 200
    assert "No inventory yet" in r.text


def test_inventory_after_ingest(client, session):
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )
    r = client.get("/admin/inventory/")
    assert r.status_code == 200
    assert "Lightning Helix" in r.text
    assert "Pikachu ex" in r.text
    assert "Bloomburrow Booster Box" in r.text


def test_inventory_search(client, session):
    run_ingest(
        FixtureTCGPlayerSource(Path("test_data/tcgplayer_fixture.csv")),
        session,
        image_cache=_NoopImageCache(),
    )
    r = client.get("/admin/inventory/?q=Lightning")
    assert r.status_code == 200
    assert "Lightning Helix" in r.text
    assert "Pikachu ex" not in r.text


def test_sync_index(client):
    r = client.get("/admin/sync/")
    assert r.status_code == 200
    # New status panel: TCGPlayer + the manual-pull copy.
    body = r.text
    assert "TCGPlayer" in body
    assert "Pull from TCGPlayer now" in body


def test_sync_run_button_returns_303(client):
    """POST /admin/sync/run hands off to the coordinator and redirects.

    The actual ingest now runs on a coordinator-owned thread; route-level
    behavior is just the redirect. Coordinator behavior is covered in
    tests/test_scheduler.py and ingest end-to-end is covered by
    test_inventory_after_ingest above.
    """
    r = client.post("/admin/sync/run", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/admin/sync/")
