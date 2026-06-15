"""Windows DPAPI encryption-at-rest for sensitive ``app_setting`` values.

DPAPI's ``CryptProtectData`` ties the encrypted blob to the **current
Windows user account on the current machine**. A backup of
``data/tag_inventory.db`` copied off-machine — or to a different OS
user — yields ciphertext that nobody can decrypt. The legitimate app
running as the original user reads it transparently with no key
management on our side.

On-disk format::

    dpapi:v1:<base64(ciphertext)>

Plain strings (no prefix) are returned untouched by ``decrypt_secret``,
which makes the migration from plaintext gradual: old rows continue
working, and the next ``set_secret_setting`` call upgrades them.

This module is Windows-only. On other platforms ``encrypt_secret``
raises (we never silently store plaintext where the caller asked for
encryption); ``decrypt_secret`` still passes plaintext through so the
non-encrypted fallback works for tests/CI.
"""

from __future__ import annotations

import base64
import sys

PREFIX = "dpapi:v1:"


class DpapiUnavailableError(RuntimeError):
    """Raised when DPAPI is requested on a non-Windows platform."""


class DpapiDecryptError(RuntimeError):
    """Raised when a dpapi:v1: blob can't be decrypted (corrupted,
    different user, different machine)."""


def is_encrypted_blob(value: str | None) -> bool:
    """True iff ``value`` looks like one of our DPAPI envelopes."""
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt ``plaintext`` for the current Windows user. Returns the
    ``dpapi:v1:<base64>`` envelope. Raises ``DpapiUnavailableError`` on
    non-Windows platforms — callers should not silently fall back to
    plaintext when they asked for encryption.
    """
    if sys.platform != "win32":
        raise DpapiUnavailableError(
            "DPAPI encryption is only available on Windows."
        )
    if plaintext is None:
        raise TypeError("encrypt_secret() requires a string, got None")

    raw = plaintext.encode("utf-8")
    cipher = _crypt_protect(raw)
    return PREFIX + base64.b64encode(cipher).decode("ascii")


def can_decrypt_envelope(stored: str | None) -> bool:
    """True iff ``stored`` is usable on *this* machine+user.

    Plaintext (no ``dpapi:v1:`` prefix) round-trips anywhere, so it
    counts as decryptable. A ``dpapi:v1:`` envelope counts only if
    ``CryptUnprotectData`` succeeds here — the whole point of the
    machine-move detection. ``None``/empty are not decryptable (nothing
    to decrypt). Never raises.
    """
    if not stored:
        return False
    if not is_encrypted_blob(stored):
        return True
    try:
        decrypt_secret(stored)
        return True
    except (DpapiDecryptError, DpapiUnavailableError):
        return False


def can_unprotect(raw: bytes) -> bool:
    """True iff ``raw`` (a bare DPAPI ciphertext blob, no ``dpapi:v1:``
    envelope) decrypts on the current machine+user.

    Used to probe Chrome's ``Local State`` ``os_crypt.encrypted_key``,
    which is a DPAPI blob — not our envelope format — so we can tell a
    foreign (copied-from-another-machine) Chrome profile from a native
    one without launching the browser. Returns False off-Windows or on
    any failure; never raises.
    """
    if sys.platform != "win32" or not raw:
        return False
    try:
        _crypt_unprotect(raw)
        return True
    except OSError:
        return False


def decrypt_secret(stored: str | None) -> str | None:
    """Decrypt a ``dpapi:v1:`` blob; pass plaintext (or None) through
    unchanged. Raises ``DpapiDecryptError`` if the blob has the prefix
    but can't be decrypted (corrupted, wrong user, etc.).
    """
    if stored is None:
        return None
    if not is_encrypted_blob(stored):
        return stored

    if sys.platform != "win32":
        raise DpapiUnavailableError(
            "Stored value is DPAPI-encrypted but DPAPI is unavailable here."
        )
    body = stored[len(PREFIX):]
    try:
        cipher = base64.b64decode(body, validate=True)
    except Exception as e:
        raise DpapiDecryptError(f"Bad base64 in DPAPI blob: {e}") from e
    try:
        plain = _crypt_unprotect(cipher)
    except OSError as e:
        raise DpapiDecryptError(
            "DPAPI decryption failed (corrupted blob, different user, "
            "or different machine)."
        ) from e
    return plain.decode("utf-8")


# ---- low-level Win32 calls (lazy ctypes) -----------------------------


def _crypt_protect(data: bytes) -> bytes:
    """CryptProtectData → user-scoped ciphertext."""
    blob = _DataBlob(data)
    out = _DataBlobOut()
    ok = _CryptProtectData(blob.byref(), None, None, None, None, 0, out.byref())
    if not ok:
        import ctypes

        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return out.read_bytes()
    finally:
        out.free()


def _crypt_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData → plaintext (or raises OSError on mismatch)."""
    blob = _DataBlob(data)
    out = _DataBlobOut()
    ok = _CryptUnprotectData(
        blob.byref(), None, None, None, None, 0, out.byref()
    )
    if not ok:
        import ctypes

        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return out.read_bytes()
    finally:
        out.free()


# ---- ctypes plumbing — only resolved at call time on Windows ----------


def _ctypes_setup():
    """Lazy import + structure setup so this module is import-safe on
    non-Windows."""
    if sys.platform != "win32":
        raise DpapiUnavailableError("ctypes setup requires Windows.")
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = crypt32.CryptProtectData.argtypes
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return ctypes, DATA_BLOB, crypt32, kernel32


# Resolved on first call; cheap to do lazily.
_ctypes_state = None


def _ensure_state():
    global _ctypes_state
    if _ctypes_state is None:
        _ctypes_state = _ctypes_setup()
    return _ctypes_state


class _DataBlob:
    """Input blob — owns the bytes via a Python buffer, gives
    CryptProtectData a pointer it won't outlive."""

    def __init__(self, data: bytes):
        ctypes, DATA_BLOB, _, _ = _ensure_state()
        self._buf = ctypes.create_string_buffer(data, len(data))
        self._blob = DATA_BLOB(
            len(data), ctypes.cast(self._buf, ctypes.POINTER(ctypes.c_byte))
        )

    def byref(self):
        import ctypes

        return ctypes.byref(self._blob)


class _DataBlobOut:
    """Output blob — CryptProtectData fills its pbData with a pointer
    we have to LocalFree when done."""

    def __init__(self):
        _, DATA_BLOB, _, _ = _ensure_state()
        self._blob = DATA_BLOB(0, None)
        self._freed = False

    def byref(self):
        import ctypes

        return ctypes.byref(self._blob)

    def read_bytes(self) -> bytes:
        import ctypes

        if not self._blob.pbData:
            return b""
        return ctypes.string_at(self._blob.pbData, self._blob.cbData)

    def free(self):
        if self._freed or not self._blob.pbData:
            return
        _, _, _, kernel32 = _ensure_state()
        kernel32.LocalFree(self._blob.pbData)
        self._freed = True


def _CryptProtectData(*args):
    _, _, crypt32, _ = _ensure_state()
    return crypt32.CryptProtectData(*args)


def _CryptUnprotectData(*args):
    _, _, crypt32, _ = _ensure_state()
    return crypt32.CryptUnprotectData(*args)
