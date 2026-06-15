"""The "Sign in to TCGPlayer" flow must persist the auth cookie in a
form the later "Get from TCGPlayer Portal" pull can inject — otherwise
the user is forced to log in a second time when pulling the CSV.

This pins the contract end-to-end, for both save points:

1. After login, the ``auth_status`` poll snags cookies and saves them
   (encrypted) under ``app_setting[tcgplayer_portal_cookies]``.
2. The download flow itself captures cookies from the live authenticated
   driver the moment auth succeeds (``_persist_driver_cookies``) — the
   race-free save point, guarded by the browser lock so a concurrent
   snag can't evict the download window.
3. ``download_pricing_csv``'s ``_inject_stored_cookies`` reads the saved
   blob back and injects ``TCGAuthTicket_Production`` into the driver —
   the thing that keeps the pull authenticated with no fresh login.
"""

from __future__ import annotations

from pathlib import Path

import app.sync.tcgplayer.auth_health as auth_health
import app.sync.tcgplayer.portal_auth as portal_auth
import app.sync.tcgplayer.portal_downloader as portal_downloader
from app.sync.tcgplayer.auth_health import AuthHealth, AuthStatus
from app.sync.tcgplayer.portal_auth import AUTH_COOKIE_NAME
from app.sync.tcgplayer.portal_downloader import _inject_stored_cookies

SELLER_COOKIES = {
    AUTH_COOKIE_NAME: "TICKET-abc123",
    "SellerKey": "1d1b3bf6",
    "AWSALB": "lb-token",
}


class _FakeDriver:
    """Records add_cookie calls; ignores navigation."""

    def __init__(self) -> None:
        self.added: list[dict] = []

    def get(self, url: str) -> None:  # noqa: D401 - test stub
        pass

    def add_cookie(self, cookie: dict) -> None:
        self.added.append(cookie)


def _stub_auth_status_deps(monkeypatch, *, snag_result):
    """Neutralise the live collaborators of the auth_status route so the
    test exercises only the snag→save logic, never a real browser/profile."""
    # ensure_healthy must NOT touch the real data/chrome_profile.
    monkeypatch.setattr(
        auth_health,
        "ensure_healthy",
        lambda *a, **k: AuthStatus(AuthHealth.NEEDS_LOGIN, "stubbed"),
    )
    # Login has "completed": the profile reports the auth cookie present.
    monkeypatch.setattr(portal_auth, "profile_has_auth_cookie", lambda *_: True)
    monkeypatch.setattr(portal_auth, "snag_auth_cookies", lambda **_: snag_result)
    # A browser is "installed".
    monkeypatch.setattr(
        portal_downloader, "find_browser_executable", lambda: Path("chrome.exe")
    )


def test_login_flow_saves_auth_cookie(client, session, monkeypatch):
    """After login the auth_status poll persists the snagged cookies
    (encrypted) and reports the connection as live."""
    from app.settings_store import get_secret_setting

    _stub_auth_status_deps(monkeypatch, snag_result=SELLER_COOKIES)

    r = client.get("/admin/sync/auth_status")
    assert r.status_code == 200
    assert "Connected to TCGPlayer" in r.text

    session.expire_all()
    saved = get_secret_setting(session, "tcgplayer_portal_cookies", default="") or ""
    assert f"{AUTH_COOKIE_NAME}=TICKET-abc123" in saved
    assert "SellerKey=1d1b3bf6" in saved


def test_saved_cookie_is_injected_on_pull_no_second_login(client, monkeypatch):
    """The cookie the login flow saved is the cookie the CSV pull injects
    — proving the pull stays authenticated without a fresh login."""
    _stub_auth_status_deps(monkeypatch, snag_result=SELLER_COOKIES)

    # Run the login-completion poll (saves the cookies).
    assert client.get("/admin/sync/auth_status").status_code == 200

    # Now simulate the pull's cookie-injection step against a fake driver.
    driver = _FakeDriver()
    n = _inject_stored_cookies(driver)

    assert n == len(SELLER_COOKIES)
    injected = {c["name"]: c["value"] for c in driver.added}
    assert injected[AUTH_COOKIE_NAME] == "TICKET-abc123"
    assert all(c["domain"] == ".tcgplayer.com" for c in driver.added)


def test_no_save_when_auth_ticket_missing(client, session, monkeypatch):
    """If snag comes back without the auth ticket (login not really done),
    nothing is persisted — we must not report a phantom connection that
    then fails at pull time."""
    from app.settings_store import get_secret_setting

    _stub_auth_status_deps(
        monkeypatch, snag_result={"AWSALB": "lb-only", "SellerKey": "x"}
    )

    r = client.get("/admin/sync/auth_status")
    assert "Connected to TCGPlayer" not in r.text

    session.expire_all()
    assert (get_secret_setting(session, "tcgplayer_portal_cookies", default="") or "") == ""


# ---- live-session capture (_persist_driver_cookies) -------------------
#
# The reliable save point: cookies captured from the live authenticated
# driver the moment auth succeeds, instead of a separate snag that races
# Chrome's SQLite flush. This is what makes "log in once in the portal
# window, never again" actually hold.


class _CookieDriver:
    def __init__(self, cookies):
        self._cookies = cookies

    def get_cookies(self):
        return self._cookies


def test_persist_driver_cookies_saves_when_auth_ticket_present(session):
    from app.settings_store import get_secret_setting
    from app.sync.tcgplayer.portal_downloader import _persist_driver_cookies

    d = _CookieDriver(
        [
            {"name": AUTH_COOKIE_NAME, "value": "LIVE-tok", "domain": ".tcgplayer.com"},
            {"name": "SellerKey", "value": "1d1b3bf6", "domain": ".tcgplayer.com"},
            {"name": "irrelevant", "value": "x", "domain": ".example.com"},  # dropped
        ]
    )

    n = _persist_driver_cookies(d)

    assert n == 2  # only the two tcgplayer cookies
    session.expire_all()
    saved = get_secret_setting(session, "tcgplayer_portal_cookies", default="") or ""
    assert f"{AUTH_COOKIE_NAME}=LIVE-tok" in saved
    assert "irrelevant" not in saved


def test_persist_driver_cookies_skips_when_no_auth_ticket(session):
    from app.settings_store import get_secret_setting
    from app.sync.tcgplayer.portal_downloader import _persist_driver_cookies

    d = _CookieDriver(
        [{"name": "AWSALB", "value": "lb", "domain": ".tcgplayer.com"}]
    )

    assert _persist_driver_cookies(d) == 0
    session.expire_all()
    # Nothing written — we must not clobber a real blob with a logged-out set.
    assert (get_secret_setting(session, "tcgplayer_portal_cookies", default="") or "") == ""


# ---- snag yields to an active download (the lock) ---------------------


def test_snag_skips_when_browser_lock_held(monkeypatch):
    """While a download owns the browser, snag must NOT launch a competing
    Chrome (which would evict the download window). It returns None without
    ever building a driver."""
    import app.sync.tcgplayer.portal_downloader as pd
    from app.sync.tcgplayer.portal_auth import snag_auth_cookies

    built = []
    monkeypatch.setattr(pd, "_build_driver", lambda *a, **k: built.append(1))

    pd._BROWSER_LOCK.acquire()
    try:
        result = snag_auth_cookies(chrome_binary=__import__("pathlib").Path("chrome.exe"), profile_dir=__import__("pathlib").Path("."))
    finally:
        pd._BROWSER_LOCK.release()

    assert result is None
    assert built == []  # never tried to launch a browser


# ---- "Sign in" now drives a live Selenium capture ---------------------
#
# The auth ticket is a session cookie, so the only way to save it is from
# the live driver the user logged into. The button must therefore spawn
# login_and_capture (Selenium), not a plain detached window.


def test_portal_login_spawns_selenium_capture(client, monkeypatch):
    import threading

    import app.sync.tcgplayer.auth_health as ah
    import app.sync.tcgplayer.portal_downloader as pd
    from app.sync.tcgplayer.auth_health import AuthHealth, AuthStatus

    called = threading.Event()
    monkeypatch.setattr(pd, "find_browser_executable", lambda: Path("chrome.exe"))
    monkeypatch.setattr(
        ah, "ensure_healthy", lambda *a, **k: AuthStatus(AuthHealth.NEEDS_LOGIN, "x")
    )
    monkeypatch.setattr(pd, "login_and_capture", lambda *a, **k: called.set())

    r = client.post("/admin/sync/portal_login", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/admin/sync/?login_opened=1"
    assert called.wait(timeout=3)  # background thread actually invoked capture


def test_portal_login_no_browser_returns_error(client, monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    monkeypatch.setattr(pd, "find_browser_executable", lambda: None)
    r = client.post("/admin/sync/portal_login", follow_redirects=False)
    assert r.status_code == 303
    assert "login_error=no_browser" in r.headers["location"]


def test_login_and_capture_returns_false_without_browser(monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    monkeypatch.setattr(pd, "find_browser_executable", lambda: None)
    assert pd.login_and_capture() is False


# ---- CDP capture: plain login window + DevTools cookie read -----------
#
# The auth ticket is a session cookie AND driving the login with Selenium
# trips the captcha. So login_and_capture launches a PLAIN Chrome (captcha
# behaves normally) and reads cookies over DevTools (invisible to the
# page). These verify the orchestration without a real browser.


def test_login_and_capture_saves_when_cdp_returns_ticket(session, monkeypatch):
    from app.settings_store import get_secret_setting
    import app.sync.tcgplayer.portal_downloader as pd
    import app.sync.tcgplayer.portal_auth as pa

    monkeypatch.setattr(pd, "find_browser_executable", lambda: Path("chrome.exe"))
    monkeypatch.setattr(pd, "ensure_profile_free", lambda *a, **k: 0)
    monkeypatch.setattr(pd, "_find_free_port", lambda: 9333)
    launched = {}
    monkeypatch.setattr(
        pa, "launch_login_window",
        lambda **k: launched.update(k) or None,
    )
    # CDP "sees" the user complete login: ticket shows up.
    monkeypatch.setattr(
        pd, "_read_cookies_via_cdp",
        lambda port: {AUTH_COOKIE_NAME: "CDP-tok", "SellerKey": "1d1b3bf6"},
    )

    ok = pd.login_and_capture(login_wait_sec=5, poll_interval=0.01)

    assert ok is True
    assert launched["remote_debugging_port"] == 9333  # plain window, debug port
    session.expire_all()
    saved = get_secret_setting(session, "tcgplayer_portal_cookies", default="") or ""
    assert f"{AUTH_COOKIE_NAME}=CDP-tok" in saved


def test_login_and_capture_times_out_without_ticket(session, monkeypatch):
    from app.settings_store import get_secret_setting
    import app.sync.tcgplayer.portal_downloader as pd
    import app.sync.tcgplayer.portal_auth as pa

    monkeypatch.setattr(pd, "find_browser_executable", lambda: Path("chrome.exe"))
    monkeypatch.setattr(pd, "ensure_profile_free", lambda *a, **k: 0)
    monkeypatch.setattr(pd, "_find_free_port", lambda: 9444)
    monkeypatch.setattr(pa, "launch_login_window", lambda **k: None)
    # User never logs in — CDP only ever returns anonymous cookies.
    monkeypatch.setattr(pd, "_read_cookies_via_cdp", lambda port: {"AWSALB": "x"})

    ok = pd.login_and_capture(login_wait_sec=0.05, poll_interval=0.01)

    assert ok is False
    session.expire_all()
    assert (get_secret_setting(session, "tcgplayer_portal_cookies", default="") or "") == ""


def test_read_cookies_via_cdp_filters_tcgplayer(monkeypatch):
    """Storage.getCookies returns all domains; we keep only tcgplayer."""
    import app.sync.tcgplayer.portal_downloader as pd

    # _tcg_cookie_map is the filter _read_cookies_via_cdp applies.
    raw = [
        {"name": AUTH_COOKIE_NAME, "value": "t", "domain": ".tcgplayer.com"},
        {"name": "NID", "value": "g", "domain": ".google.com"},
        {"name": "hc", "value": "h", "domain": ".hcaptcha.com"},
    ]
    assert pd._tcg_cookie_map(raw) == {AUTH_COOKIE_NAME: "t"}
