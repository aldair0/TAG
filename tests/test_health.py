"""Structured health snapshot + /healthz route."""

from __future__ import annotations

from app.health import collect_health


def test_collect_health_shape_and_ok():
    h = collect_health()
    assert h["status"] in ("ok", "degraded", "down")
    # DB is reachable in tests → not down.
    assert h["db"]["ok"] is True
    assert h["status"] != "down"
    for key in ("db", "disk", "receiver", "scheduler", "backup", "version"):
        assert key in h


def test_healthz_route_returns_structured_body(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"  # db+disk+receiver healthy in tests
    assert "version" in body
    assert body["db"]["ok"] is True
    assert "free_gb" in body["disk"]


def test_healthz_simple_backcompat(client):
    r = client.get("/healthz/simple")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
