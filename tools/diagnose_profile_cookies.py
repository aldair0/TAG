"""Probe what cookies Selenium-Edge actually sees when it loads our
profile dir, and whether the seller pricing URL still 302s to login.

Run with the production server STOPPED (or after closing it for a sec)
so the profile dir isn't locked. Tells us in 30s whether we have a
profile-loading problem or a fingerprint problem.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROFILE_DIR = Path("data/chrome_profile")
COOKIES_DB = PROFILE_DIR / "Default" / "Network" / "Cookies"


def main() -> None:
    if not PROFILE_DIR.exists():
        sys.exit(f"profile dir not found: {PROFILE_DIR}")

    # ---- Step 1: peek directly at the SQLite cookies file -------------
    print(f"[1] Cookie DB: {COOKIES_DB}")
    if COOKIES_DB.exists():
        try:
            con = sqlite3.connect(f"file:{COOKIES_DB}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT host_key, name, length(value), is_secure, is_httponly "
                "FROM cookies "
                "WHERE host_key LIKE '%tcgplayer%' "
                "ORDER BY host_key, name"
            ).fetchall()
            con.close()
            print(f"    {len(rows)} tcgplayer cookies in the SQLite db:")
            for host, name, vlen, sec, http in rows:
                print(
                    f"      {host:35s} {name:35s} value_bytes={vlen} "
                    f"secure={sec} httponly={http}"
                )
        except sqlite3.OperationalError as e:
            print(f"    [error] could not read cookie db: {e}")
            print("    likely: Edge is currently running with this profile.")
    else:
        print("    [error] cookie DB does not exist — manual login didn't persist")
        sys.exit(2)

    # ---- Step 2: launch Selenium-Edge and ask IT what it sees ---------
    print()
    print("[2] Launching Selenium-Edge against the same profile dir...")
    from app.sync.tcgplayer.portal_downloader import _build_driver, find_browser_executable

    browser = find_browser_executable()
    if browser is None:
        sys.exit("no browser found")
    download_dir = Path("data/csv/_incoming")
    download_dir.mkdir(parents=True, exist_ok=True)

    driver = _build_driver(
        browser, profile_dir=PROFILE_DIR, download_dir=download_dir
    )
    try:
        # Visit the bare domain first so we can read its cookies.
        print("    GET https://store.tcgplayer.com/")
        driver.get("https://store.tcgplayer.com/")
        # Selenium's get_cookies returns cookies for the current page's
        # origin. Should match what's in the SQLite db for that host.
        cookies = driver.get_cookies()
        print(f"    {len(cookies)} cookies visible to selenium for store.tcgplayer.com:")
        for c in cookies:
            print(
                f"      {c.get('domain'):35s} {c.get('name'):35s} "
                f"value_bytes={len(c.get('value', ''))} secure={c.get('secure')}"
            )

        print()
        print("[3] Navigating to /admin/pricing — does it stay or redirect to login?")
        driver.get("https://store.tcgplayer.com/admin/pricing")
        # Brief settle
        import time
        time.sleep(3)
        print(f"    final URL: {driver.current_url}")
        print(f"    title    : {driver.title!r}")
        if "/admin/Login" in driver.current_url or "login" in driver.current_url.lower():
            print("    >>> bounced to LOGIN. The cookie isn't authenticating us.")
        elif "/admin/pricing" in driver.current_url:
            print("    >>> already authenticated! The Get button should now work.")
        else:
            print("    >>> unexpected URL — could be Cloudflare interstitial.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
