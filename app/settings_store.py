"""Get/set helpers for ``app_setting`` rows.

Single-row-by-key access used by the scheduler (auto-sync flag) and the
admin UI (toggle endpoint). String-typed by design — callers parse to
bool/int as needed.

Two flavors:

- ``get_setting`` / ``set_setting`` — plaintext. Used for non-sensitive
  flags like ``tcgplayer_auto_sync = "on"|"off"``.
- ``get_secret_setting`` / ``set_secret_setting`` — DPAPI-encrypted at
  rest on Windows. Used for the TCGPlayer auth cookie. Reads transparently
  upgrade legacy plaintext rows. Writes always encrypt.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.security.dpapi import (
    DpapiDecryptError,
    DpapiUnavailableError,
    decrypt_secret,
    encrypt_secret,
)


def get_setting(
    session: Session, key: str, *, default: str | None = None
) -> str | None:
    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def get_secret_setting(
    session: Session, key: str, *, default: str | None = None
) -> str | None:
    """Read a sensitive value, decrypting DPAPI envelopes transparently.
    Plaintext rows from before encryption was added are returned as-is
    so the migration is gradual (next ``set_secret_setting`` rewrites
    encrypted)."""
    raw = get_setting(session, key, default=default)
    if raw is None or raw == default:
        return raw
    try:
        return decrypt_secret(raw)
    except DpapiDecryptError:
        # Blob was encrypted by a different Windows user or machine.
        # Treat as absent so the UI renders with empty credentials
        # rather than crashing with a 500.
        import logging
        logging.getLogger(__name__).warning(
            "DPAPI decryption failed for key %r — treating as unset. "
            "Re-enter credentials in Settings to re-encrypt for this user.",
            key,
        )
        return default


def set_secret_setting(session: Session, key: str, value: str) -> None:
    """Write a sensitive value, encrypted at rest where DPAPI is
    available. Empty values bypass encryption (so ``""`` round-trips
    cleanly and shows as cleared in the admin UI)."""
    if not value:
        set_setting(session, key, "")
        return
    try:
        encrypted = encrypt_secret(value)
    except DpapiUnavailableError:
        # Non-Windows fallback (CI, dev box on Linux/macOS): store
        # plaintext. The DB doesn't leave that machine.
        encrypted = value
    set_setting(session, key, encrypted)
