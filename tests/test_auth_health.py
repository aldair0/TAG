"""Tests for the self-healing TCGPlayer auth state machine.

The platform-independent classification (assess / heal / quarantine) is
exercised everywhere. The DPAPI-backed foreign-vs-native detection is
Windows-only and skips elsewhere.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from app.sync.tcgplayer.auth_health import (
    AUTH_STATE_KEY,
    COOKIE_SETTINGS_KEY,
    AuthHealth,
    assess,
    chrome_profile_is_foreign,
    ensure_healthy,
    self_heal,
)

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is Windows-only"
)


def _write_local_state(profile_dir: Path, encrypted_key: bytes | None) -> None:
    """Write a Chrome-shaped Local State with the given (already
    DPAPI-prefixed) os_crypt key, or omit the key when None."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    os_crypt = {}
    if encrypted_key is not None:
        os_crypt["encrypted_key"] = base64.b64encode(encrypted_key).decode("ascii")
    (profile_dir / "Local State").write_text(
        json.dumps({"os_crypt": os_crypt}), encoding="utf-8"
    )


def _native_key_blob() -> bytes:
    """A real os_crypt key this machine CAN unprotect, with Chrome's
    'DPAPI' prefix."""
    from app.security.dpapi import _crypt_protect

    return b"DPAPI" + _crypt_protect(b"a-real-aes-key-32-bytes-long!!!!")


# ---- chrome_profile_is_foreign ----------------------------------------


def test_profile_foreign_none_when_no_local_state(tmp_path: Path):
    assert chrome_profile_is_foreign(tmp_path) is None


def test_profile_foreign_none_when_no_key_field(tmp_path: Path):
    _write_local_state(tmp_path, None)
    assert chrome_profile_is_foreign(tmp_path) is None


@windows_only
def test_profile_foreign_false_for_native_key(tmp_path: Path):
    _write_local_state(tmp_path, _native_key_blob())
    assert chrome_profile_is_foreign(tmp_path) is False


@windows_only
def test_profile_foreign_true_for_copied_key(tmp_path: Path):
    # A key blob from "another machine" — DPAPI prefix but ciphertext we
    # can't unprotect here.
    _write_local_state(tmp_path, b"DPAPI" + b"\x01\x02\x03 not our key")
    assert chrome_profile_is_foreign(tmp_path) is True


# ---- assess -----------------------------------------------------------


def test_assess_needs_login_when_empty(session, tmp_path: Path):
    status = assess(session, tmp_path)
    assert status.health is AuthHealth.NEEDS_LOGIN


def test_assess_ok_for_plaintext_backup(session, tmp_path: Path):
    # Plaintext (non-Windows fallback shape) round-trips anywhere.
    from app.settings_store import set_setting

    set_setting(session, COOKIE_SETTINGS_KEY, "TCGAuthTicket_Production=abc; foo=bar")
    status = assess(session, tmp_path)
    assert status.health is AuthHealth.OK


def test_assess_foreign_for_undecryptable_backup(session, tmp_path: Path):
    from app.security.dpapi import PREFIX
    from app.settings_store import set_setting

    foreign = PREFIX + base64.b64encode(b"\x01\x02 sealed elsewhere").decode("ascii")
    set_setting(session, COOKIE_SETTINGS_KEY, foreign)
    status = assess(session, tmp_path)
    # On non-Windows a dpapi blob is "unavailable" → also not decryptable.
    assert status.health is AuthHealth.FOREIGN


@windows_only
def test_assess_foreign_for_copied_profile(session, tmp_path: Path):
    _write_local_state(tmp_path, b"DPAPI" + b"\x09 copied key")
    status = assess(session, tmp_path)
    assert status.health is AuthHealth.FOREIGN


# ---- self_heal / ensure_healthy ---------------------------------------


def test_self_heal_clears_foreign_backup(session, tmp_path: Path):
    from app.security.dpapi import PREFIX
    from app.settings_store import get_setting, set_setting

    foreign = PREFIX + base64.b64encode(b"sealed-elsewhere").decode("ascii")
    set_setting(session, COOKIE_SETTINGS_KEY, foreign)

    status = self_heal(session, tmp_path, assess(session, tmp_path))

    assert status.healed is True
    assert status.health is AuthHealth.NEEDS_LOGIN
    assert (get_setting(session, COOKIE_SETTINGS_KEY) or "") == ""
    assert get_setting(session, AUTH_STATE_KEY) == "needs_login"


def test_self_heal_noop_for_healthy(session, tmp_path: Path):
    from app.settings_store import set_setting

    set_setting(session, COOKIE_SETTINGS_KEY, "TCGAuthTicket_Production=ok")
    before = assess(session, tmp_path)
    after = self_heal(session, tmp_path, before)
    assert after.healed is False
    assert after.health is AuthHealth.OK


@windows_only
def test_self_heal_quarantines_foreign_profile(session, tmp_path: Path):
    profile = tmp_path / "chrome_profile"
    _write_local_state(profile, b"DPAPI" + b"\x09 copied key")
    (profile / "Default").mkdir(parents=True, exist_ok=True)

    status = self_heal(session, profile, assess(session, profile))

    assert status.healed is True
    assert not profile.exists()  # moved aside
    quarantined = list(tmp_path.glob("chrome_profile.foreign_*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "Local State").exists()


def test_ensure_healthy_heals_and_reports(session, tmp_path: Path):
    from app.security.dpapi import PREFIX
    from app.settings_store import get_setting, set_setting

    foreign = PREFIX + base64.b64encode(b"sealed-elsewhere").decode("ascii")
    set_setting(session, COOKIE_SETTINGS_KEY, foreign)

    status = ensure_healthy(profile_dir=tmp_path, session=session)

    assert status.health is AuthHealth.NEEDS_LOGIN
    assert status.healed is True
    assert (get_setting(session, COOKIE_SETTINGS_KEY) or "") == ""
