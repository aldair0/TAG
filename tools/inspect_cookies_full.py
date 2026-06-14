"""Re-probe the Cookies SQLite db looking at encrypted_value length
(Chromium stores cookie values encrypted; the plaintext `value` column
is empty by default since ~2018). Specifically searches for any auth-
shaped cookies."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROFILE_DIR = Path("data/chrome_profile")
COOKIES_DB = PROFILE_DIR / "Default" / "Network" / "Cookies"


def main() -> None:
    if not COOKIES_DB.exists():
        sys.exit(f"missing: {COOKIES_DB}")

    print(f"Cookies DB: {COOKIES_DB}  ({COOKIES_DB.stat().st_size:,} bytes)")
    print()

    con = sqlite3.connect(f"file:{COOKIES_DB}?mode=ro", uri=True)
    cur = con.cursor()

    # Show the schema we care about
    cols = [r[1] for r in cur.execute("PRAGMA table_info(cookies)").fetchall()]
    print(f"cookies table columns: {cols}")
    print()

    # Total row count + tcgplayer-only count
    total = cur.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
    tcg = cur.execute(
        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%tcgplayer%'"
    ).fetchone()[0]
    print(f"total cookies: {total}    tcgplayer cookies: {tcg}")
    print()

    print("=== ALL tcgplayer cookies (with encrypted_value length) ===")
    rows = cur.execute(
        "SELECT host_key, name, "
        "length(value) AS plain_len, "
        "length(encrypted_value) AS enc_len, "
        "is_secure, is_httponly, "
        "expires_utc, has_expires "
        "FROM cookies WHERE host_key LIKE '%tcgplayer%' "
        "ORDER BY host_key, name"
    ).fetchall()
    for host, name, plain, enc, sec, http, exp, has_exp in rows:
        print(
            f"  {host:30s} {name:38s} plain={plain or 0:>4} enc={enc or 0:>4} "
            f"secure={sec} httponly={http}"
        )
    print()

    # Specifically TCGAuthTicket_Production?
    auth = cur.execute(
        "SELECT host_key, name, length(encrypted_value), is_secure, is_httponly "
        "FROM cookies WHERE name = 'TCGAuthTicket_Production'"
    ).fetchall()
    print("=== TCGAuthTicket_Production rows ===")
    if auth:
        for r in auth:
            print(f"  FOUND: {r}")
    else:
        print("  NOT PRESENT in this Cookies db.")
    print()

    # And anything that looks auth-y
    print("=== Any cookies with 'auth' or 'ticket' or 'session' in name ===")
    likely = cur.execute(
        "SELECT host_key, name, length(encrypted_value) "
        "FROM cookies "
        "WHERE name LIKE '%auth%' OR name LIKE '%ticket%' OR name LIKE '%session%' "
        "ORDER BY host_key, name"
    ).fetchall()
    for host, name, enc in likely:
        print(f"  {host:30s} {name:38s} enc={enc or 0}")
    if not likely:
        print("  (none)")

    con.close()


if __name__ == "__main__":
    main()
