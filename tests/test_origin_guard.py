"""A1: cross-origin state-changing requests are refused."""

from __future__ import annotations


def test_cross_origin_post_refused(client):
    r = client.post(
        "/admin/sold-online/dismiss/1",
        headers={"Origin": "http://evil.example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "cross-origin" in r.json()["error"]


def test_cross_origin_via_referer_refused(client):
    r = client.post(
        "/admin/sold-online/dismiss/1",
        headers={"Referer": "http://evil.example.com/page"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_same_origin_post_allowed(client):
    # TestClient's Host is "testserver"; a matching Origin is same-origin.
    r = client.post(
        "/admin/sold-online/dismiss/999999",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code != 403  # 303 redirect (unknown unit is a no-op)


def test_no_origin_header_allowed(client):
    # Server-side callers (and TestClient default) send no Origin/Referer.
    r = client.post("/admin/sold-online/dismiss/999999", follow_redirects=False)
    assert r.status_code != 403


def test_get_requests_never_blocked(client):
    r = client.get("/admin/sold-online/", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 200
