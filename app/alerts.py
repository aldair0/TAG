"""Outbound failure alerting to the tech-support inbox.

The app is otherwise inbound-only (the IMAP receiver *reads* mail) and so
cannot tell anyone when it breaks. This is the single outbound channel: a
throttled SMTP send to ``SUPPORT_EMAIL`` for the failures a human must know
about — the receiver losing auth, a failed backup, low disk, DB corruption.

By default it reuses the store Gmail account already configured for the
receiver (Gmail app passwords work for SMTP too), so no extra credentials
are needed. Throttled per-key so a persistent fault emails once an hour,
not once a minute. Never raises — alerting must not become a failure source.
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
import time
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_sent: dict[str, float] = {}

DEFAULT_MIN_INTERVAL_SEC = 3600  # one email per fault per hour


def _smtp_credentials() -> tuple[str, str]:
    """SMTP login — falls back to the receiver's Gmail creds (the chosen
    'reuse store Gmail' path) when dedicated alert creds aren't set."""
    user = settings.alert_smtp_username or settings.imap_username
    password = settings.alert_smtp_password or settings.imap_app_password
    return user, password


def send_alert(
    subject: str,
    body: str,
    *,
    key: str | None = None,
    min_interval_sec: int = DEFAULT_MIN_INTERVAL_SEC,
) -> str:
    """Email a support alert. Returns 'sent' | 'logged' | 'throttled' | 'error'.

    ``key`` (defaults to ``subject``) is the throttle bucket.
    """
    bucket = key or subject
    now = time.time()
    with _lock:
        last = _last_sent.get(bucket)
        if last is not None and now - last < min_interval_sec:
            return "throttled"
        # Evict entries we no longer need (older than a day) so the throttle
        # map can't grow unbounded if a caller passes dynamic subjects.
        stale_before = now - 86400
        for k in [k for k, t in _last_sent.items() if t < stale_before]:
            del _last_sent[k]
        _last_sent[bucket] = now

    return _deliver(subject, body)


def send_email(
    subject: str,
    body: str,
    *,
    attachments: list[tuple[str, bytes | str]] | None = None,
) -> str:
    """Send an email to the support address (no throttling). Used for the
    on-demand / on-failure diagnostic report, which carries a log attachment.
    Returns 'sent' | 'logged' | 'error'.
    """
    return _deliver(subject, body, attachments=attachments)


def _deliver(
    subject: str,
    body: str,
    *,
    attachments: list[tuple[str, bytes | str]] | None = None,
) -> str:
    # Tests / opt-out: never actually send.
    if os.environ.get("TAG_DISABLE_ALERTS") == "1":
        logger.warning("EMAIL (suppressed): %s", subject)
        return "logged"

    user, password = _smtp_credentials()
    to_addr = settings.support_email
    if not (user and password and to_addr):
        logger.warning("EMAIL (not sent — SMTP unconfigured): %s", subject)
        return "logged"

    try:
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = to_addr
        msg["Subject"] = f"[TAG Inventory] {subject}"
        msg.set_content(body)
        for filename, data in attachments or []:
            payload = data.encode("utf-8", "replace") if isinstance(data, str) else data
            msg.add_attachment(payload, maintype="text", subtype="plain", filename=filename)
        with smtplib.SMTP(settings.alert_smtp_host, settings.alert_smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        logger.info("email sent to %s: %s", to_addr, subject)
        return "sent"
    except Exception:
        logger.exception("email send failed: %s", subject)
        return "error"


def reset_throttle() -> None:
    """Clear throttle state (tests)."""
    with _lock:
        _last_sent.clear()
