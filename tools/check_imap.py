"""Verify the IMAP credentials in .env without starting the live receiver.

Run after setting IMAP_USERNAME + GMAIL_APP_PASSWORD:

    .venv\\Scripts\\python.exe tools\\check_imap.py

It logs in, selects the configured mailbox, reports how many messages are
present and how many look like sale notifications, and logs out. It never
flags inventory and never marks mail as read — purely a connection check.
The app password is never printed.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.sync.tcgplayer.email_parser import is_sale_notification


def _fail(msg: str) -> None:
    print(f"  FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    print("IMAP credential check")
    print(f"  host     : {settings.imap_host}:{settings.imap_port}")
    print(f"  username : {settings.imap_username or '(unset)'}")
    print(f"  mailbox  : {settings.imap_mailbox}")
    print(f"  password : {'set (' + str(len(settings.imap_app_password)) + ' chars)' if settings.imap_app_password else '(unset)'}")
    print(f"  match    : From~'{settings.sale_email_from}' AND Subject~'{settings.sale_email_subject_contains}'")
    print()

    if not settings.imap_username:
        _fail("IMAP_USERNAME is empty in .env")
    if not settings.imap_app_password:
        _fail("GMAIL_APP_PASSWORD is empty in .env")
    if " " in settings.imap_app_password:
        print("  WARN: app password contains spaces — Google shows it spaced, but "
              "store it WITHOUT spaces. Trying anyway...")

    try:
        from imapclient import IMAPClient
    except ImportError:
        _fail("imapclient not installed — run: pip install -e .[dev]")

    try:
        with IMAPClient(settings.imap_host, port=settings.imap_port, ssl=True) as server:
            server.login(settings.imap_username, settings.imap_app_password)
            print("  OK: login succeeded")

            folder_info = server.select_folder(settings.imap_mailbox, readonly=True)
            total = folder_info.get(b"EXISTS", 0)
            print(f"  OK: selected '{settings.imap_mailbox}' ({total} message(s))")

            # Peek at the most recent messages and count sale notifications.
            uids = server.search(["ALL"])
            recent = sorted(uids)[-25:]
            sale_count = 0
            if recent:
                fetched = server.fetch(recent, ["ENVELOPE", "BODY.PEEK[HEADER]"])
                import email as _email
                for uid in recent:
                    raw = fetched[uid].get(b"BODY[HEADER]") or fetched[uid].get(b"RFC822.HEADER")
                    if not raw:
                        continue
                    msg = _email.message_from_bytes(raw)
                    if is_sale_notification(
                        msg,
                        from_contains=settings.sale_email_from,
                        subject_contains=settings.sale_email_subject_contains,
                    ):
                        sale_count += 1
            print(f"  OK: {sale_count} of the last {len(recent)} message(s) match the sale-notification filter")
    except Exception as e:  # noqa: BLE001 — surface any failure plainly
        name = type(e).__name__
        hint = ""
        text = str(e).lower()
        if "auth" in text or "credentials" in text or "login" in text:
            hint = (
                "\n  HINT: authentication failed. Confirm 2-Step Verification is ON "
                "and you pasted a 16-char App Password (not the account password), "
                "with spaces removed."
            )
        _fail(f"{name}: {e}{hint}")

    print("\nAll checks passed. Safe to set EMAIL_RECEIVER_ENABLED=true.")


if __name__ == "__main__":
    main()
