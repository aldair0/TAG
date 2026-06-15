"""Tests for the testable parts of the portal downloader.

The browser-automation entry point (``download_pricing_csv``) can't be
unit-tested without launching a real Chrome — that's exercised manually
via the admin "Get from TCGPlayer Portal" button. The supporting
helpers (browser detection, file rotation) are pure I/O and TDDable.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.sync.tcgplayer.portal_downloader import (
    _clear_profile_locks,
    _parse_cookies,
    _profile_lock_holders,
    archive_and_replace,
    ensure_profile_free,
    find_browser_executable,
)


# ---- archive_and_replace ----------------------------------------------


def test_archive_and_replace_when_target_does_not_exist(tmp_path: Path):
    """First-time call: source moves to target, no archive entry created."""
    src = tmp_path / "incoming" / "fresh.csv"
    src.parent.mkdir()
    src.write_bytes(b"fresh-data")
    target = tmp_path / "out" / "tcgplayer_pricing.csv"
    archive = tmp_path / "out" / "_archive"

    result = archive_and_replace(source=src, target=target, archive_dir=archive, keep=5)

    assert result == target
    assert target.read_bytes() == b"fresh-data"
    assert not src.exists()  # source consumed
    assert not archive.exists() or list(archive.iterdir()) == []


def test_archive_and_replace_moves_existing_target_into_archive(tmp_path: Path):
    src = tmp_path / "incoming" / "new.csv"
    src.parent.mkdir()
    src.write_bytes(b"new-data")
    target = tmp_path / "out" / "tcgplayer_pricing.csv"
    target.parent.mkdir()
    target.write_bytes(b"old-data")
    archive = tmp_path / "out" / "_archive"

    archive_and_replace(source=src, target=target, archive_dir=archive, keep=5)

    assert target.read_bytes() == b"new-data"
    archived = list(archive.iterdir())
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"old-data"
    # Archive filename has the same stem + suffix as target
    assert archived[0].stem.startswith("tcgplayer_pricing_")
    assert archived[0].suffix == ".csv"


def test_archive_and_replace_keeps_only_n_most_recent(tmp_path: Path):
    target = tmp_path / "out" / "tcgplayer_pricing.csv"
    target.parent.mkdir()
    archive = tmp_path / "out" / "_archive"
    archive.mkdir()

    # Pre-seed 6 archive files with monotonically increasing mtimes.
    for i in range(6):
        f = archive / f"tcgplayer_pricing_2026010{i}_120000.csv"
        f.write_bytes(f"v{i}".encode())
        # mtime ordered: i=0 oldest, i=5 newest
        ts = time.time() - (10 - i) * 60
        import os
        os.utime(f, (ts, ts))

    target.write_bytes(b"current")
    src = tmp_path / "incoming" / "new.csv"
    src.parent.mkdir()
    src.write_bytes(b"newest")

    archive_and_replace(source=src, target=target, archive_dir=archive, keep=5)

    # After: 6 pre-existing + 1 just-archived from the previous target = 7 candidates,
    # trimmed back to 5. The single oldest 2 should be deleted.
    remaining = sorted(archive.iterdir(), key=lambda p: p.stat().st_mtime)
    assert len(remaining) == 5
    # The one we archived now is the newest — present.
    contents = [f.read_bytes() for f in remaining]
    assert b"current" in contents


def test_archive_and_replace_only_trims_matching_pattern(tmp_path: Path):
    """Files in the archive dir that don't match the target stem are
    left alone — guards against deleting unrelated stuff if archive_dir
    was ever pointed somewhere wrong."""
    target = tmp_path / "out" / "tcgplayer_pricing.csv"
    target.parent.mkdir()
    archive = tmp_path / "out" / "_archive"
    archive.mkdir()

    unrelated = archive / "other_data.csv"
    unrelated.write_bytes(b"unrelated")
    for i in range(7):
        (archive / f"tcgplayer_pricing_x{i}.csv").write_bytes(f"v{i}".encode())

    src = tmp_path / "incoming" / "fresh.csv"
    src.parent.mkdir()
    src.write_bytes(b"fresh")
    archive_and_replace(source=src, target=target, archive_dir=archive, keep=5)

    assert unrelated.exists()
    assert unrelated.read_bytes() == b"unrelated"


# ---- find_browser_executable -----------------------------------------


def test_find_browser_returns_first_existing(tmp_path: Path):
    chrome = tmp_path / "chrome.exe"
    edge = tmp_path / "edge.exe"
    edge.write_bytes(b"edge")  # only edge exists

    with patch(
        "app.sync.tcgplayer.portal_downloader._KNOWN_BROWSERS",
        [chrome, edge],
    ):
        assert find_browser_executable() == edge


def test_find_browser_returns_none_when_nothing_installed(tmp_path: Path):
    with patch(
        "app.sync.tcgplayer.portal_downloader._KNOWN_BROWSERS",
        [tmp_path / "missing-1.exe", tmp_path / "missing-2.exe"],
    ):
        assert find_browser_executable() is None


def test_find_browser_prefers_earlier_entries(tmp_path: Path):
    """Both Chrome and Edge installed — first in the list wins (Chrome
    is preferred because undetected-chromedriver is Chrome-tuned)."""
    chrome = tmp_path / "chrome.exe"
    edge = tmp_path / "edge.exe"
    chrome.write_bytes(b"chrome")
    edge.write_bytes(b"edge")

    with patch(
        "app.sync.tcgplayer.portal_downloader._KNOWN_BROWSERS",
        [chrome, edge],
    ):
        assert find_browser_executable() == chrome


# ---- _parse_cookies ---------------------------------------------------


def test_parse_cookies_semicolon_separated():
    out = _parse_cookies("TCGAuthTicket_Production=abc; valid=xyz")
    assert [c["name"] for c in out] == ["TCGAuthTicket_Production", "valid"]
    assert [c["value"] for c in out] == ["abc", "xyz"]


def test_parse_cookies_newline_separated():
    out = _parse_cookies("k1=v1\nk2=v2\n\nk3=v3")
    assert [c["name"] for c in out] == ["k1", "k2", "k3"]


def test_parse_cookies_mixed_separators():
    out = _parse_cookies("a=1;\nb=2\n;c=3")
    assert [c["name"] for c in out] == ["a", "b", "c"]


def test_parse_cookies_skips_empty_and_malformed():
    out = _parse_cookies("  ;; junk; valid=ok ; ; \n  ;")
    assert [c["name"] for c in out] == ["valid"]
    assert out[0]["value"] == "ok"


def test_parse_cookies_default_domain_and_path():
    [c] = _parse_cookies("k=v")
    assert c["domain"] == ".tcgplayer.com"
    assert c["path"] == "/"


def test_parse_cookies_empty_string_returns_empty_list():
    assert _parse_cookies("") == []
    assert _parse_cookies("   \n  ") == []


def test_parse_cookies_handles_value_with_equals_sign():
    """Some auth cookies are base64-padded with '=' inside the value
    (e.g. JWT-ish things)."""
    [c] = _parse_cookies("token=abc==.def")
    assert c["name"] == "token"
    assert c["value"] == "abc==.def"



# ---- profile-lock self-healing ----------------------------------------
#
# Root cause of the "still can't log in" crash: a second browser launched
# against a --user-data-dir another browser already holds dies with
# "not connected to DevTools". ensure_profile_free() evicts the holder and
# clears stale locks before each driver launch. The live process-kill needs
# a real browser (manual smoke); the parsing / file / orchestration logic
# below is deterministic and CI-safe.


def test_clear_profile_locks_removes_lock_files(tmp_path: Path):
    for name in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        (tmp_path / name).write_text("x")
    (tmp_path / "keep.txt").write_text("keep")

    _clear_profile_locks(tmp_path)

    assert not (tmp_path / "lockfile").exists()
    assert not (tmp_path / "SingletonLock").exists()
    assert (tmp_path / "keep.txt").exists()  # untouched


def test_clear_profile_locks_noop_when_absent(tmp_path: Path):
    _clear_profile_locks(tmp_path)  # must not raise on a clean dir


def test_profile_lock_holders_matches_only_our_profile(tmp_path: Path, monkeypatch):
    """CIM output lists three browsers; only the one whose --user-data-dir
    resolves to OUR profile is returned — never the user's everyday Chrome."""
    import subprocess as sp

    ours = tmp_path / "chrome_profile"
    ours.mkdir()
    other = tmp_path / "someone_elses"
    other.mkdir()

    fake_out = "\n".join(
        [
            f'4242\tchrome.exe --user-data-dir={ours.resolve()} about:blank',
            f'9999\tchrome.exe --user-data-dir={other.resolve()} about:blank',
            "5555\tchrome.exe --type=renderer",  # no user-data-dir
        ]
    )

    class _R:
        stdout = fake_out

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(sp, "run", lambda *a, **k: _R())

    assert _profile_lock_holders(ours) == [4242]


def test_profile_lock_holders_handles_quoted_path(tmp_path: Path, monkeypatch):
    import subprocess as sp

    ours = tmp_path / "chrome profile with spaces"
    ours.mkdir()

    class _R:
        stdout = f'7000\tchrome.exe "--user-data-dir={ours.resolve()}" about:blank'

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(sp, "run", lambda *a, **k: _R())

    assert _profile_lock_holders(ours) == [7000]


def test_profile_lock_holders_empty_off_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert _profile_lock_holders(tmp_path) == []


def test_ensure_profile_free_evicts_then_clears(tmp_path: Path, monkeypatch):
    """Holders are terminated, then lock files cleared; returns the count."""
    import app.sync.tcgplayer.portal_downloader as pd

    (tmp_path / "lockfile").write_text("x")
    killed: list[int] = []

    monkeypatch.setattr(pd, "_profile_lock_holders", lambda p: [111, 222])
    monkeypatch.setattr(
        pd.subprocess,
        "run",
        lambda args, **k: killed.append(int(args[2])),
    )
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    n = ensure_profile_free(tmp_path)

    assert n == 2
    assert killed == [111, 222]
    assert not (tmp_path / "lockfile").exists()


def test_ensure_profile_free_noop_when_free(tmp_path: Path, monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    monkeypatch.setattr(pd, "_profile_lock_holders", lambda p: [])
    assert ensure_profile_free(tmp_path) == 0


# ---- headless UA cloaking (anti-bot-detection) ------------------------
#
# Headless Chrome/Edge advertise "HeadlessChrome" in the UA, which trips
# Cloudflare's captcha on the seller portal. UC 3.5.5 can't cloak it for
# Chrome 149, so _cloak_headless_ua overrides it via CDP. Verified live
# against a real browser in the audit; these pin the pure logic.


class _UaDriver:
    def __init__(self, ua: str):
        self._ua = ua
        self.override = None

    def execute_script(self, _):
        return self._ua

    def execute_cdp_cmd(self, cmd, params):
        assert cmd == "Network.setUserAgentOverride"
        self.override = params["userAgent"]


def test_cloak_headless_ua_strips_headless():
    from app.sync.tcgplayer.portal_downloader import _cloak_headless_ua

    d = _UaDriver(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/149.0.0.0 Safari/537.36"
    )
    _cloak_headless_ua(d)
    assert d.override is not None
    assert "Headless" not in d.override
    assert "Chrome/149.0.0.0" in d.override


def test_cloak_headless_ua_noop_when_clean():
    from app.sync.tcgplayer.portal_downloader import _cloak_headless_ua

    d = _UaDriver(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    )
    _cloak_headless_ua(d)
    assert d.override is None  # already clean — no override issued


def test_cloak_headless_ua_swallows_cdp_errors():
    from app.sync.tcgplayer.portal_downloader import _cloak_headless_ua

    class _Boom(_UaDriver):
        def execute_cdp_cmd(self, *a, **k):
            raise RuntimeError("cdp down")

    # Must not raise — UA cloaking is best-effort.
    _cloak_headless_ua(_Boom("HeadlessChrome/149.0.0.0"))


# ---- _chrome_major_version --------------------------------------------


def test_chrome_major_version_parses_product_version(tmp_path, monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    class _R:
        stdout = "149.0.7827.115\n"

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: _R())
    assert pd._chrome_major_version(tmp_path / "chrome.exe") == 149


def test_chrome_major_version_none_off_windows(tmp_path, monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    monkeypatch.setattr("sys.platform", "linux")
    assert pd._chrome_major_version(tmp_path / "chrome.exe") is None


def test_chrome_major_version_none_on_garbage(tmp_path, monkeypatch):
    import app.sync.tcgplayer.portal_downloader as pd

    class _R:
        stdout = "not-a-version\n"

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: _R())
    assert pd._chrome_major_version(tmp_path / "chrome.exe") is None
