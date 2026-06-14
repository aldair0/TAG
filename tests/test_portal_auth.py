"""Tests for the testable parts of portal_auth.

The cookie-DB-peek helper is unit-testable against a synthetic SQLite
file shaped like Chrome's. The Chrome-launch path requires a live
binary so it lives outside this file (manual smoke).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.sync.tcgplayer.portal_auth import (
    AUTH_COOKIE_NAME,
    profile_has_auth_cookie,
)


def _make_chrome_cookies_db(profile_dir: Path, cookie_rows: list[tuple]) -> Path:
    """Build a minimal Chrome-shaped cookies db at the right path."""
    network_dir = profile_dir / "Default" / "Network"
    network_dir.mkdir(parents=True, exist_ok=True)
    db_path = network_dir / "Cookies"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)"
    )
    for host, name, enc in cookie_rows:
        con.execute(
            "INSERT INTO cookies (host_key, name, encrypted_value) VALUES (?, ?, ?)",
            (host, name, enc),
        )
    con.commit()
    con.close()
    return db_path


# ---- profile_has_auth_cookie ------------------------------------------


def test_returns_false_when_profile_dir_doesnt_exist(tmp_path: Path):
    assert profile_has_auth_cookie(tmp_path / "nope") is False


def test_returns_false_when_cookies_db_missing(tmp_path: Path):
    (tmp_path / "Default" / "Network").mkdir(parents=True)
    assert profile_has_auth_cookie(tmp_path) is False


def test_returns_false_when_db_has_no_auth_cookie(tmp_path: Path):
    _make_chrome_cookies_db(
        tmp_path,
        [
            (".tcgplayer.com", "ajs_anonymous_id", b"\x00\x01"),
            (".tcgplayer.com", "tracking-preferences", b"\x00\x02"),
        ],
    )
    assert profile_has_auth_cookie(tmp_path) is False


def test_returns_true_when_auth_cookie_present(tmp_path: Path):
    _make_chrome_cookies_db(
        tmp_path,
        [
            (".tcgplayer.com", "ajs_anonymous_id", b"\x00\x01"),
            (".tcgplayer.com", AUTH_COOKIE_NAME, b"\xde\xad\xbe\xef"),
        ],
    )
    assert profile_has_auth_cookie(tmp_path) is True


def test_returns_false_on_empty_encrypted_value(tmp_path: Path):
    """Defensive: a row exists but its encrypted_value is empty (zero
    bytes) — that's not a usable cookie. Don't claim "connected" on
    just a bookkeeping row."""
    _make_chrome_cookies_db(
        tmp_path,
        [(".tcgplayer.com", AUTH_COOKIE_NAME, b"")],
    )
    assert profile_has_auth_cookie(tmp_path) is False


def test_returns_false_when_db_locked_or_unreadable(tmp_path: Path, monkeypatch):
    """If the DB file is held open by Chrome, sqlite3 raises
    OperationalError. Helper should swallow that and return False —
    "we can't tell" is presented as "not connected"."""
    _make_chrome_cookies_db(
        tmp_path,
        [(".tcgplayer.com", AUTH_COOKIE_NAME, b"\xde\xad\xbe\xef")],
    )

    real_connect = sqlite3.connect

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", boom)
    assert profile_has_auth_cookie(tmp_path) is False
    monkeypatch.setattr(sqlite3, "connect", real_connect)
