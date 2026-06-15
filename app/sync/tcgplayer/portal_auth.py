"""Customer-facing TCGPlayer auth: spawn a real Chrome window pointed
at our profile dir, let the customer log in normally, and detect when
their session has been captured into the profile's cookie DB.

Three pieces:

- ``launch_login_window`` — spawn ``chrome.exe`` *detached* so the
  HTTP request that triggered it returns immediately. Customer sees
  a normal Chrome window pop up, logs in like any other website.
- ``profile_has_auth_cookie`` — read-only peek at Chrome's cookies
  SQLite DB to detect whether ``TCGAuthTicket_Production`` has landed.
  Polled by the admin UI every few seconds.
- ``snag_auth_cookies`` — once a successful auth is observed, briefly
  drive Selenium against the same profile to extract the cookie value
  (Chrome decrypts in-memory for Selenium, so we don't need to fight
  App-Bound Encryption ourselves) and persist it as a DPAPI-encrypted
  ``app_setting`` for redundancy.

Per-``user-data-dir`` Chrome isolation handles the "customer also uses
Chrome day-to-day" case: their everyday profile lives elsewhere, our
profile has its own SingletonLock, so the two never share a process.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PRICING_URL = "https://store.tcgplayer.com/admin/pricing"
LOGIN_URL = "https://store.tcgplayer.com/oauth/login?returnUrl=/admin/pricing"
AUTH_COOKIE_NAME = "TCGAuthTicket_Production"
TCGPLAYER_HOST_PATTERN = "%tcgplayer%"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def profile_has_auth_cookie(profile_dir: Path) -> bool:
    """True iff the Chrome profile at ``profile_dir`` has a
    ``TCGAuthTicket_Production`` cookie row with a non-empty
    ``encrypted_value``. Read-only; safe to call while Chrome is using
    the profile (returns False on lock failures rather than blocking).
    """
    db = profile_dir / "Default" / "Network" / "Cookies"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.OperationalError:
        return False
    try:
        try:
            row = con.execute(
                "SELECT length(encrypted_value) FROM cookies "
                "WHERE name = ? AND host_key LIKE ? "
                "ORDER BY length(encrypted_value) DESC LIMIT 1",
                (AUTH_COOKIE_NAME, TCGPLAYER_HOST_PATTERN),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    finally:
        con.close()
    return bool(row and row[0] and row[0] > 0)


def launch_login_window(
    *,
    chrome_binary: Path,
    profile_dir: Path,
    target_url: str = LOGIN_URL,
    remote_debugging_port: int | None = None,
) -> subprocess.Popen:
    """Spawn ``chrome.exe`` detached, pointed at our profile dir and
    the TCGPlayer login URL. Returns the Popen so callers can poll
    ``.poll()`` if they want; we don't read its stdout.

    Detached so the HTTP request that triggered this returns
    immediately. The Chrome window outlives the request.

    This is a **plain** Chrome — no Selenium, no automation flags — so
    the login page sees an ordinary browser and the hCaptcha/Cloudflare
    challenge behaves normally for a human. When ``remote_debugging_port``
    is given, we add ``--remote-debugging-port`` so a caller can read the
    live (in-memory) cookies over DevTools without driving the page; the
    port is invisible to page JS, so it doesn't change captcha behaviour.
    A non-default ``--user-data-dir`` (which we always pass) is required
    for Chrome 136+ to honour the debugging port.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome_binary),
        f"--user-data-dir={profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--profile-directory=Default",
    ]
    if remote_debugging_port is not None:
        args.append(f"--remote-debugging-port={remote_debugging_port}")
        args.append("--remote-allow-origins=*")
    args.append(target_url)
    logger.info("Launching login window: %s", " ".join(args))

    creationflags = 0
    if sys.platform == "win32":
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(
        args,
        creationflags=creationflags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def snag_auth_cookies(
    *,
    chrome_binary: Path,
    profile_dir: Path,
    seed_blob: str = "",
) -> dict[str, str] | None:
    """Drive Selenium against the profile to extract auth cookies in
    plaintext (Chrome decrypts in-memory so we sidestep App-Bound
    Encryption). Returns ``{name: value}`` or ``None`` on failure.

    ``seed_blob`` — semicolon-separated ``name=value`` pairs from the
    encrypted setting. When supplied, these are injected before
    navigating to the pricing page so an existing valid auth ticket
    survives even if the profile has no active seller session.

    Best-effort: failure here doesn't break anything — the encrypted
    setting copy is just redundancy / a keep-fresh mechanism.
    """
    from app.sync.tcgplayer.portal_downloader import (
        _BROWSER_LOCK,
        _build_driver,
        _parse_cookies,
    )

    # Don't launch a competing browser while a CSV download owns the
    # profile — that would evict the download's window (via
    # ensure_profile_free) and kill it mid-flight. Skip this snag cycle;
    # the next poll retries once the download has released the lock.
    if not _BROWSER_LOCK.acquire(blocking=False):
        logger.info("snag_auth_cookies skipped: a portal browser session is active")
        return None

    download_dir = Path("data/csv/_incoming")
    download_dir.mkdir(parents=True, exist_ok=True)

    driver = None
    try:
        driver = _build_driver(
            chrome_binary, profile_dir=profile_dir, download_dir=download_dir
        )
        # Must land on the domain before add_cookie will accept anything.
        driver.get("https://store.tcgplayer.com/")
        if seed_blob:
            for ck in _parse_cookies(seed_blob):
                try:
                    driver.add_cookie(ck)
                except Exception:
                    pass
        # Now navigate to the seller admin so TCGPlayer issues / refreshes
        # TCGAuthTicket_Production into the live session.
        driver.get(PRICING_URL)
        cookies = driver.get_cookies()
    except Exception:
        logger.warning("snag_auth_cookies failed", exc_info=True)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        _BROWSER_LOCK.release()

    out: dict[str, str] = {}
    for c in cookies:
        if "tcgplayer" not in (c.get("domain") or ""):
            continue
        name = c.get("name")
        value = c.get("value")
        if name and value:
            out[name] = value
    return out
