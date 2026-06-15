"""Update check: version parse/compare, GitHub query, cache, no-repo no-op."""

from __future__ import annotations

import httpx
import pytest

from app.updater import _parse_version


def test_parse_version():
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("0.1.0") == (0, 1, 0)
    assert _parse_version("v2.0") == (2, 0)
    assert _parse_version("garbage") == (0,)
    assert _parse_version("v1.2.3") > _parse_version("v1.2.2")
    assert _parse_version("0.1.0") < _parse_version("0.2.0")


def test_no_repo_is_noop(monkeypatch):
    from app.config import settings
    import app.updater as up

    monkeypatch.setattr(settings, "github_repo", "")
    r = up.check_for_update()
    assert r["enabled"] is False


def _client(tag: str) -> httpx.Client:
    def handler(_req):
        return httpx.Response(
            200,
            json={"tag_name": tag, "html_url": "https://github.com/x/releases/" + tag,
                  "published_at": "2026-06-01T00:00:00Z"},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_detects_newer_release(monkeypatch):
    from app.config import settings
    import app.updater as up

    monkeypatch.setattr(settings, "github_repo", "owner/tag-inventory")
    r = up.check_for_update(client=_client("v9.9.9"))
    assert r["enabled"] is True
    assert r["latest"] == "v9.9.9"
    assert r["update_available"] is True
    # Cached for /healthz.
    assert up.update_status()["latest"] == "v9.9.9"


def test_same_version_no_update(monkeypatch):
    from app.config import settings
    import app.updater as up

    monkeypatch.setattr(settings, "github_repo", "owner/tag-inventory")
    r = up.check_for_update(client=_client("v0.0.1"))  # older than current 0.1.0
    assert r["update_available"] is False


def test_github_error_is_caught(monkeypatch):
    from app.config import settings
    import app.updater as up

    monkeypatch.setattr(settings, "github_repo", "owner/tag-inventory")

    def handler(_req):
        return httpx.Response(404)

    r = up.check_for_update(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "error" in r
    assert r.get("update_available") is None
