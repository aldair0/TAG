"""Self-healing TCGPlayer auth state — detect credentials that were
sealed to a *different* machine/user and recover automatically.

Why this exists
---------------
The portal auto-fetch authenticates with two stores, and **both are
bound to the original Windows machine+user by DPAPI**, so neither
survives copying ``data/`` to a new box:

1. The Chrome profile (``data/chrome_profile``). Its TCGPlayer cookies
   are ``v10`` AES-GCM blobs; the AES key lives in ``Local State`` →
   ``os_crypt.encrypted_key`` as a DPAPI blob sealed to the old machine.
   On a new machine ``CryptUnprotectData`` fails, Chrome can't recover
   the key, and every cookie is undecryptable — the seller session is
   effectively gone.
2. The encrypted backup ``app_setting[tcgplayer_portal_cookies]``, a
   ``dpapi:v1:`` envelope also sealed to the old machine. On a new
   machine it decrypts to nothing.

That's DPAPI working as designed (a security feature), but the symptom
is nasty: ``download_pricing_csv`` injects zero cookies, the pricing
page redirects to login, and the flow fails only after the full
login-wait timeout (90s headless / 15min interactive) with no signal
about *why*. The scheduler then silently serves a stale cached CSV.

What self-healing does
----------------------
``ensure_healthy()`` runs before any portal launch and:

- **Detects** the foreign state cheaply (probe the app_setting envelope
  and the Chrome ``os_crypt`` key with DPAPI — no browser needed).
- **Heals** it: clears the dead app_setting ciphertext (so it can't be
  re-injected as a stale seed and the UI stops lying about being
  "connected"), and quarantines the foreign Chrome profile to
  ``chrome_profile.foreign_<ts>`` so the next login starts clean and
  gets re-encrypted with *this* machine's key.
- **Reports** the outcome via ``app_setting[tcgplayer_auth_state]`` and
  the returned :class:`AuthStatus`, so the admin UI and logs say
  "re-authenticate on this machine" instead of timing out blind.

After healing, the existing "Sign in to TCGPlayer" flow
(``open_portal_login`` → ``auth_status`` snag) re-captures a fresh,
native session with no manual cleanup.
"""

from __future__ import annotations

import base64
import enum
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.security.dpapi import can_decrypt_envelope, can_unprotect

logger = logging.getLogger(__name__)

COOKIE_SETTINGS_KEY = "tcgplayer_portal_cookies"
AUTH_STATE_KEY = "tcgplayer_auth_state"

# Chrome prefixes the DPAPI-wrapped os_crypt key with these 5 bytes.
_DPAPI_KEY_PREFIX = b"DPAPI"


class AuthHealth(enum.Enum):
    OK = "ok"
    """A credential usable on this machine is present (decryptable
    envelope, or — after a fresh native login — a same-machine profile)."""

    NEEDS_LOGIN = "needs_login"
    """No usable credential. A normal first-run / logged-out state; the
    user just needs to sign in."""

    FOREIGN = "foreign"
    """Credentials exist but were sealed to a different machine/user
    (the post-move state). Triggers self-heal."""


@dataclass
class AuthStatus:
    health: AuthHealth
    reason: str
    healed: bool = False

    @property
    def usable(self) -> bool:
        return self.health is AuthHealth.OK


# ---- detection ---------------------------------------------------------


def chrome_profile_is_foreign(profile_dir: Path) -> bool | None:
    """Is ``profile_dir``'s ``Local State`` os_crypt key sealed to
    another machine?

    Returns ``True`` (foreign), ``False`` (native), or ``None`` when we
    can't tell — no ``Local State`` yet (fresh/empty profile), no key
    field, or unreadable JSON. ``None`` is deliberately distinct from
    ``False`` so a brand-new profile isn't mistaken for a healthy one.
    """
    local_state = profile_dir / "Local State"
    if not local_state.exists():
        return None
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read %s", local_state, exc_info=True)
        return None
    enc = (data.get("os_crypt") or {}).get("encrypted_key")
    if not enc:
        return None
    try:
        raw = base64.b64decode(enc)
    except (ValueError, TypeError):
        return None
    if raw[: len(_DPAPI_KEY_PREFIX)] == _DPAPI_KEY_PREFIX:
        raw = raw[len(_DPAPI_KEY_PREFIX):]
    return not can_unprotect(raw)


def assess(session, profile_dir: Path) -> AuthStatus:
    """Classify current auth state without touching anything.

    Precedence: a decryptable backup → OK; otherwise any foreign signal
    (undecryptable backup OR foreign Chrome key) → FOREIGN; otherwise
    NEEDS_LOGIN.
    """
    from app.settings_store import get_setting

    raw_setting = get_setting(session, COOKIE_SETTINGS_KEY, default="") or ""
    if raw_setting and can_decrypt_envelope(raw_setting):
        return AuthStatus(AuthHealth.OK, "decryptable credential backup present")

    setting_foreign = bool(raw_setting) and not can_decrypt_envelope(raw_setting)
    profile_foreign = chrome_profile_is_foreign(profile_dir) is True

    if setting_foreign or profile_foreign:
        bits = []
        if setting_foreign:
            bits.append("encrypted cookie backup")
        if profile_foreign:
            bits.append("Chrome profile")
        return AuthStatus(
            AuthHealth.FOREIGN,
            f"{' and '.join(bits)} sealed to a different machine/user "
            "(credentials were copied from another computer)",
        )

    return AuthStatus(AuthHealth.NEEDS_LOGIN, "no stored credentials")


# ---- healing -----------------------------------------------------------


def _quarantine_foreign_profile(profile_dir: Path, *, keep: int = 1) -> Path | None:
    """Move a foreign profile aside so the next login starts clean.

    Renamed (not deleted) to ``<name>.foreign_<ts>`` for forensics;
    older quarantines beyond ``keep`` are pruned. Best-effort: if Chrome
    still holds the dir we log and bail (a foreign Chrome regenerates its
    own os_crypt key on next launch anyway).
    """
    if not profile_dir.exists():
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = profile_dir.with_name(f"{profile_dir.name}.foreign_{ts}")
    suffix = 1
    while dest.exists():
        dest = profile_dir.with_name(f"{profile_dir.name}.foreign_{ts}_{suffix}")
        suffix += 1
    try:
        shutil.move(str(profile_dir), str(dest))
    except OSError:
        logger.warning(
            "Could not quarantine foreign profile %s (in use?)",
            profile_dir,
            exc_info=True,
        )
        return None

    logger.info("Quarantined foreign Chrome profile → %s", dest)
    quarantines = sorted(
        profile_dir.parent.glob(f"{profile_dir.name}.foreign_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in quarantines[keep:]:
        try:
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            logger.debug("Could not prune old quarantine %s", old, exc_info=True)
    return dest


def self_heal(session, profile_dir: Path, status: AuthStatus) -> AuthStatus:
    """Recover from a FOREIGN state. No-op for OK/NEEDS_LOGIN.

    Commits its own changes to ``session`` (clearing the dead backup and
    stamping the auth-state). Returns the re-assessed status with
    ``healed=True``.
    """
    if status.health is not AuthHealth.FOREIGN:
        return status

    from app.settings_store import get_setting, set_secret_setting, set_setting

    healed_any = False

    raw_setting = get_setting(session, COOKIE_SETTINGS_KEY, default="") or ""
    if raw_setting and not can_decrypt_envelope(raw_setting):
        set_secret_setting(session, COOKIE_SETTINGS_KEY, "")
        healed_any = True
        logger.info("Cleared undecryptable cookie backup (foreign machine)")

    if chrome_profile_is_foreign(profile_dir) is True:
        if _quarantine_foreign_profile(profile_dir) is not None:
            healed_any = True

    after = assess(session, profile_dir)
    set_setting(session, AUTH_STATE_KEY, after.health.value)
    session.commit()

    after.healed = healed_any
    logger.warning(
        "Self-heal ran for TCGPlayer auth: %s → %s (%s). "
        "Re-authenticate via 'Sign in to TCGPlayer' on the sync page.",
        status.health.value,
        after.health.value,
        after.reason,
    )
    return after


# ---- convenience wrapper ----------------------------------------------


def ensure_healthy(profile_dir: Path, session=None) -> AuthStatus:
    """Assess auth, auto-heal a FOREIGN state, return the final status.

    Opens its own DB session when none is supplied. Fully guarded — a DB
    or import failure degrades to ``NEEDS_LOGIN`` rather than blowing up
    the caller's download flow.
    """
    own_session = False
    try:
        if session is None:
            from app.db.session import SessionLocal

            session = SessionLocal()
            own_session = True

        status = assess(session, profile_dir)
        if status.health is AuthHealth.FOREIGN:
            status = self_heal(session, profile_dir, status)
        else:
            # Keep the reported state fresh even when nothing needed fixing.
            from app.settings_store import set_setting

            set_setting(session, AUTH_STATE_KEY, status.health.value)
            session.commit()
        return status
    except Exception:
        logger.warning("ensure_healthy failed; assuming NEEDS_LOGIN", exc_info=True)
        return AuthStatus(AuthHealth.NEEDS_LOGIN, "auth health check errored")
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass
