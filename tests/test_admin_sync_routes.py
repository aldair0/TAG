from app.settings_store import get_setting


def test_sync_index_renders_status_panel(client):
    r = client.get("/admin/sync/")
    assert r.status_code == 200
    body = r.text
    assert "Auto-sync:" in body
    assert "Pull from TCGPlayer now" in body
    assert "Last successful update" in body
    assert "Status" in body


def test_toggle_auto_sync_flips_setting(client, session):
    # Default is "on" (no row); first toggle should produce "off".
    assert get_setting(session, "tcgplayer_auto_sync", default="on") == "on"

    r = client.post("/admin/sync/auto/toggle", follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert get_setting(session, "tcgplayer_auto_sync") == "off"

    r = client.post("/admin/sync/auto/toggle", follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert get_setting(session, "tcgplayer_auto_sync") == "on"


def test_manual_run_routes_through_coordinator(client):
    """Manual button posts to /admin/sync/run and returns 303 immediately,
    even with no fixture present (the coordinator handles the run async)."""
    r = client.post("/admin/sync/run", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/sync/"


def test_status_panel_shows_ON_badge_when_enabled(client):
    r = client.get("/admin/sync/")
    assert "Auto-sync: ON" in r.text


def test_status_panel_shows_OFF_after_toggle(client):
    client.post("/admin/sync/auto/toggle", follow_redirects=False)
    r = client.get("/admin/sync/")
    assert "Auto-sync: OFF" in r.text
