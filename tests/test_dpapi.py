"""Tests for the DPAPI-encrypted-at-rest helper.

DPAPI is Windows-only. Tests skip on other platforms; the helpers
themselves provide a fallback (no-op) shape so CI on non-Windows
machines doesn't blow up at import time.
"""

from __future__ import annotations

import sys

import pytest

from app.security.dpapi import (
    decrypt_secret,
    encrypt_secret,
    is_encrypted_blob,
)

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is Windows-only"
)


@windows_only
def test_round_trip_short_string():
    blob = encrypt_secret("hello-world")
    assert is_encrypted_blob(blob)
    assert decrypt_secret(blob) == "hello-world"


@windows_only
def test_round_trip_long_realistic_cookie():
    # Roughly the shape of a TCGAuthTicket — long hex.
    cookie = "TCGAuthTicket_Production=" + "A1B2C3D4E5F6" * 30
    blob = encrypt_secret(cookie)
    assert decrypt_secret(blob) == cookie


@windows_only
def test_round_trip_unicode():
    s = "Pokémon — €éñ test 🎴"
    assert decrypt_secret(encrypt_secret(s)) == s


@windows_only
def test_round_trip_empty_string():
    blob = encrypt_secret("")
    assert is_encrypted_blob(blob)
    assert decrypt_secret(blob) == ""


def test_decrypt_passes_through_plaintext():
    """Backward compat: rows that pre-date encryption should decrypt
    cleanly (i.e., return themselves) so we can migrate gradually."""
    assert decrypt_secret("plain-old-value") == "plain-old-value"
    assert decrypt_secret("") == ""


def test_decrypt_passes_through_none():
    assert decrypt_secret(None) is None


def test_is_encrypted_blob_recognizes_format():
    assert not is_encrypted_blob("plaintext")
    assert not is_encrypted_blob("")
    assert not is_encrypted_blob(None)
    # Anything starting with the prefix is at least claimed-encrypted.
    assert is_encrypted_blob("dpapi:v1:abc==")


@windows_only
def test_decrypt_corrupted_blob_raises():
    """A corrupted dpapi:v1: blob should raise rather than silently
    return garbage."""
    from app.security.dpapi import DpapiDecryptError

    with pytest.raises(DpapiDecryptError):
        decrypt_secret("dpapi:v1:not-actually-base64!@#$")


# ---- can_decrypt_envelope / can_unprotect (machine-move probes) --------


def test_can_decrypt_envelope_plaintext_and_empty():
    from app.security.dpapi import can_decrypt_envelope

    assert can_decrypt_envelope("plain-old-value") is True
    assert can_decrypt_envelope("") is False
    assert can_decrypt_envelope(None) is False


@windows_only
def test_can_decrypt_envelope_native_blob_true():
    from app.security.dpapi import can_decrypt_envelope

    assert can_decrypt_envelope(encrypt_secret("session-cookie")) is True


@windows_only
def test_can_decrypt_envelope_foreign_blob_false():
    """A well-formed dpapi:v1: envelope whose ciphertext this machine
    can't unprotect (the post-copy state) reads as not-decryptable —
    without raising."""
    import base64

    from app.security.dpapi import PREFIX, can_decrypt_envelope

    foreign = PREFIX + base64.b64encode(b"\x01\x02\x03not-our-key").decode("ascii")
    assert can_decrypt_envelope(foreign) is False


@windows_only
def test_can_unprotect_round_trip_and_foreign():
    from app.security.dpapi import _crypt_protect, can_unprotect

    native = _crypt_protect(b"chrome-os_crypt-key")
    assert can_unprotect(native) is True
    assert can_unprotect(b"\x00\x01\x02 garbage from another machine") is False
    assert can_unprotect(b"") is False
