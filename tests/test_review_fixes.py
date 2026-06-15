"""Regression tests for the security + fragility review fixes."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

# --- S1: Shopify shop validation -------------------------------------------

from app.routes.shopify_auth import _valid_shop


def test_valid_shop_allows_only_myshopify():
    assert _valid_shop("acme.myshopify.com")
    assert _valid_shop("ACME.MyShopify.com")
    assert not _valid_shop("evil.com")
    assert not _valid_shop("acme.myshopify.com.evil.com")
    assert not _valid_shop("169.254.169.254")
    assert not _valid_shop("")


def test_install_rejects_bad_shop(client):
    r = client.get("/auth/shopify/install", params={"shop": "evil.com"}, follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "shopify_error=invalid_shop" in r.headers["location"]


def test_callback_rejects_bad_shop(client):
    r = client.get(
        "/auth/shopify/callback",
        params={"shop": "evil.com", "code": "x", "state": "y"},
        follow_redirects=False,
    )
    assert "shopify_error=invalid_shop" in r.headers["location"]


# --- S3: image fetch SSRF + size cap ---------------------------------------

from app.sync.tcgplayer.images import ImageCache, _MAX_IMAGE_BYTES, _is_allowed_image_url


def test_image_url_allowlist():
    assert _is_allowed_image_url("https://tcgplayer-cdn.tcgplayer.com/product/1.jpg")
    assert _is_allowed_image_url("https://tcgplayer.com/x.jpg")
    assert not _is_allowed_image_url("http://tcgplayer-cdn.tcgplayer.com/1.jpg")  # not https
    assert not _is_allowed_image_url("https://169.254.169.254/latest/meta-data")
    assert not _is_allowed_image_url("https://evil.com/x.jpg")
    assert not _is_allowed_image_url("https://tcgplayer.com.evil.com/x.jpg")


def test_fetch_refuses_disallowed_url(tmp_path):
    cache = ImageCache(tmp_path, client=httpx.Client())
    out = cache.fetch_to_path(tmp_path / "x.jpg", "https://evil.com/x.jpg")
    assert out is None
    assert not (tmp_path / "x.jpg").exists()


def test_fetch_allows_tcgplayer_image(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff data")

    cache = ImageCache(tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = cache.fetch_to_path(tmp_path / "ok.jpg", "https://tcgplayer-cdn.tcgplayer.com/p/1.jpg")
    assert out == tmp_path / "ok.jpg"
    assert out.read_bytes().startswith(b"\xff\xd8\xff")


def test_fetch_rejects_non_image_content_type(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")

    cache = ImageCache(tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.fetch_to_path(tmp_path / "x.jpg", "https://tcgplayer.com/x.jpg") is None


def test_fetch_aborts_oversize_stream(tmp_path):
    big = b"x" * (_MAX_IMAGE_BYTES + 10)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=big)

    cache = ImageCache(tmp_path, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.fetch_to_path(tmp_path / "x.jpg", "https://tcgplayer.com/x.jpg") is None


# --- F1: receiver watermark must not skip an unfetched UID ------------------

def test_drain_stops_at_empty_body(monkeypatch):
    from app.inbound_email.receiver import ImapIdleReceiver

    r = ImapIdleReceiver()
    processed: list[int] = []
    persisted: list[int] = []
    monkeypatch.setattr(r, "_process_one", lambda uid, raw: processed.append(uid))
    monkeypatch.setattr(r, "_set_last_uid", lambda uid: persisted.append(uid))

    class FakeClient:
        def search(self, _crit):
            return [11, 12, 13]

        def fetch(self, _uids, _parts):
            return {11: {b"RFC822": b"a"}, 12: {b"RFC822": b""}, 13: {b"RFC822": b"c"}}

    new_high = r._drain_new(FakeClient(), 10)
    # Stops at the empty-body uid 12 — does NOT process or skip past to 13.
    assert processed == [11]
    assert new_high == 11
    assert persisted == [11]


# --- F4: backup retention can't delete the just-written file ----------------

def test_backup_retention_zero_keeps_new_backup(tmp_path):
    import sqlite3

    from app.backup import run_backup

    src = tmp_path / "src.db"
    con = sqlite3.connect(str(src))
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()

    out = run_backup(src_path=src, dest_dir=tmp_path / "b", retention_days=0)
    assert out.exists()  # clamp to >=1 means the fresh backup survives


# --- A1: /signal shared-secret token ---------------------------------------

def test_signal_requires_token_when_configured(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signal_token", "s3cret")
    # Missing/wrong header → 401
    r = client.post("/admin/sold-online/signal", data={"card_name": "X"})
    assert r.status_code == 401
    # Correct header → proceeds (200, even if unmatched)
    r = client.post(
        "/admin/sold-online/signal",
        data={"card_name": "X"},
        headers={"X-Signal-Token": "s3cret"},
    )
    assert r.status_code == 200


# --- A6: Shopify order parse tolerates malformed money/id -------------------

def test_parse_shopify_order_handles_bad_money():
    from app.sync.shopify.client import _parse_shopify_order

    order = _parse_shopify_order(
        {"id": 5, "subtotal_price": "notanumber", "total_price": None, "line_items": []}
    )
    assert order.order_id == "5"
    assert order.subtotal is None
    assert order.total is None


def test_parse_shopify_order_missing_id_raises():
    from app.sync.shopify.client import _parse_shopify_order

    with pytest.raises(ValueError):
        _parse_shopify_order({"subtotal_price": "1.00", "line_items": []})
