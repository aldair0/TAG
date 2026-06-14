"""Round-trip tests for the encrypted-at-rest settings flavor.

We don't assert on the on-disk format here (that's covered in
test_dpapi.py); we just confirm the wrapper reads what it writes and
that legacy plaintext rows still resolve."""

from __future__ import annotations

from app.settings_store import (
    get_secret_setting,
    get_setting,
    set_secret_setting,
    set_setting,
)


def test_secret_round_trip(session):
    set_secret_setting(session, "k.cookie", "TCGAuthTicket_Production=ABC123")
    assert (
        get_secret_setting(session, "k.cookie")
        == "TCGAuthTicket_Production=ABC123"
    )


def test_legacy_plaintext_still_readable_via_get_secret(session):
    """Rows written by the old plaintext path keep working."""
    set_setting(session, "k.legacy", "legacy-cookie-value=xyz")
    assert get_secret_setting(session, "k.legacy") == "legacy-cookie-value=xyz"


def test_set_secret_then_get_setting_returns_encrypted_blob(session):
    """Reading via the plain getter shows the encrypted shape (when
    DPAPI is available)."""
    import sys

    set_secret_setting(session, "k.x", "secret-value")
    raw = get_setting(session, "k.x")
    if sys.platform == "win32":
        assert raw is not None and raw.startswith("dpapi:v1:")
    else:
        # Non-Windows fallback: plaintext.
        assert raw == "secret-value"


def test_empty_value_round_trips(session):
    set_secret_setting(session, "k.empty", "")
    assert get_secret_setting(session, "k.empty") == ""
    # And the on-disk form is plain empty, not "dpapi:v1:".
    assert get_setting(session, "k.empty") == ""


def test_overwrite_re_encrypts(session):
    set_secret_setting(session, "k.rolling", "v1")
    set_secret_setting(session, "k.rolling", "v2")
    assert get_secret_setting(session, "k.rolling") == "v2"


def test_default_returned_for_missing(session):
    assert get_secret_setting(session, "nope") is None
    assert get_secret_setting(session, "nope", default="x") == "x"
