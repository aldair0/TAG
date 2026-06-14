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
    _parse_cookies,
    archive_and_replace,
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

