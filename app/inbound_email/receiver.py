"""Always-on IMAP IDLE receiver for sale-notification emails.

A daemon thread holds an IMAP IDLE connection to the store inbox and
reacts within seconds of a new email. Each message is parsed and, if it's
a TCGPlayer sale notification, the matching inventory units are flagged
sold-online (see :func:`app.inbound_email.flagger.flag_email_sale`).

Design notes mirroring ``app.scheduler``:
- ``imapclient`` is imported lazily so unit tests that never start the
  receiver don't pull it into the import graph.
- ``start_email_receiver`` / ``stop_email_receiver`` are module-level and
  idempotent, wired into the FastAPI lifespan.
- ``TAG_DISABLE_EMAIL_RECEIVER=1`` short-circuits startup (tests).

State:
- The highest processed message UID is persisted in ``app_setting`` under
  ``imap_last_uid`` so a restart resumes where it left off rather than
  re-flagging history. On the very first run (no stored UID) we record the
  current max UID *without* processing the backlog — only mail arriving
  after startup is acted on.

Robustness:
- Any connection/parse error is logged and the loop reconnects with
  capped exponential backoff; one bad email never kills the receiver.
- ``idle_check`` uses a short poll window so ``stop`` is responsive while
  still refreshing IDLE comfortably under Gmail's ~30-min idle timeout.
"""

from __future__ import annotations

import email
import logging
import os
import threading

from app.config import settings
from app.inbound_email.flagger import flag_email_sale
from app.sync.tcgplayer.email_parser import (
    EmailParseError,
    is_sale_notification,
    parse_message,
)

logger = logging.getLogger(__name__)

_LAST_UID_KEY = "imap_last_uid"
# Re-enter IDLE at least this often, and check for a stop signal at least
# this often (whichever is smaller bounds stop latency).
_IDLE_POLL_SEC = 60


class ImapIdleReceiver:
    """Owns the IMAP connection + background listen thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client = None  # lazily-created IMAPClient
        # Observability / self-healing state (read by health + alerting).
        self._connected = False
        self._last_contact: float | None = None  # monotonic-ish wall time of last server confirmation
        self._reconnects = 0
        self._consecutive_auth_failures = 0

    def status(self) -> dict:
        import time

        last = self._last_contact
        return {
            "enabled": True,
            "connected": self._connected,
            "seconds_since_contact": (time.time() - last) if last else None,
            "reconnects": self._reconnects,
            "auth_failures": self._consecutive_auth_failures,
        }

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ImapIdleReceiver", daemon=True
        )
        self._thread.start()
        logger.info(
            "Email receiver started: %s@%s/%s",
            settings.imap_username,
            settings.imap_host,
            settings.imap_mailbox,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            # Break a blocking idle and tear down the socket from this thread.
            try:
                client.logout()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # -- main loop ------------------------------------------------------

    def _mark_contact(self) -> None:
        import time

        self._last_contact = time.time()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
                backoff = 1.0  # clean exit (stop requested)
            except Exception:
                self._connected = False
                if self._stop.is_set():
                    break
                self._reconnects += 1
                logger.exception(
                    "Email receiver: connection error (reconnect #%d) — retrying in %.0fs",
                    self._reconnects, backoff,
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300.0)

    def _connect_and_listen(self) -> None:
        from imapclient import IMAPClient

        # A socket timeout is essential: without it a half-open ("silently
        # dead") connection blocks forever on the next op instead of raising,
        # and the receiver looks alive while receiving nothing. With it, a dead
        # socket surfaces within `timeout` and triggers a reconnect.
        client = IMAPClient(
            settings.imap_host, port=settings.imap_port, ssl=True, timeout=30
        )
        self._client = client
        try:
            try:
                client.login(settings.imap_username, settings.imap_app_password)
            except Exception as e:
                from imapclient.exceptions import LoginError

                if isinstance(e, LoginError):
                    self._consecutive_auth_failures += 1
                    logger.error(
                        "Email receiver: LOGIN FAILED (auth failure #%d) — the Gmail "
                        "app password is likely wrong or revoked",
                        self._consecutive_auth_failures,
                    )
                    # Alert once we're confident it's not a transient blip.
                    if self._consecutive_auth_failures == 3:
                        try:
                            from app.alerts import send_alert

                            send_alert(
                                "Email receiver DOWN — auth failing",
                                "The sale-notification email receiver can't log in to "
                                f"{settings.imap_username}. The Gmail app password is "
                                "likely wrong or revoked. Online sales are NOT being "
                                "flagged until this is fixed (Settings → re-enter the "
                                "app password).",
                                key="receiver_auth",
                            )
                        except Exception:
                            logger.exception("failed to send receiver-auth alert")
                raise
            self._consecutive_auth_failures = 0
            self._connected = True
            self._mark_contact()
            client.select_folder(settings.imap_mailbox)

            last_uid = self._initial_uid(client)

            while not self._stop.is_set():
                last_uid = self._drain_new(client, last_uid)
                if self._stop.is_set():
                    break
                client.idle()
                try:
                    # Returns as soon as the server reports activity, or
                    # after the poll window — whichever comes first.
                    client.idle_check(timeout=min(_IDLE_POLL_SEC, settings.imap_idle_refresh_sec))
                finally:
                    client.idle_done()
                # Active liveness probe: a NOOP confirms the socket is really
                # alive (idle_check returning empty does not). On a dead
                # connection this raises (within the socket timeout) and we
                # reconnect. On success, refresh the heartbeat.
                client.noop()
                self._mark_contact()
        finally:
            self._connected = False
            self._client = None
            try:
                client.logout()
            except Exception:
                pass

    # -- per-message processing ----------------------------------------

    def _initial_uid(self, client) -> int:
        """Stored high-water UID, or the current max (skip backlog) on first run."""
        stored = self._get_last_uid()
        if stored is not None:
            return stored
        existing = client.search(["ALL"])
        start = max(existing) if existing else 0
        self._set_last_uid(start)
        logger.info(
            "Email receiver: first run — starting after UID %d (backlog skipped)",
            start,
        )
        return start

    def _drain_new(self, client, last_uid: int) -> int:
        """Process every message with UID > ``last_uid``; return new high UID."""
        # 'n:*' can return the latest message even when its UID <= n, so
        # filter in code rather than trusting the server-side range.
        uids = [u for u in client.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid]
        if not uids:
            return last_uid
        fetched = client.fetch(sorted(uids), ["RFC822"])
        new_high = last_uid
        # Advance the watermark ONLY through UIDs we actually fetched and
        # processed, in order. If a UID is missing from the fetch or has an
        # empty body (a transient miss), stop — do NOT skip past it, or that
        # sale email is lost forever. The next drain retries from this point.
        for uid in sorted(uids):
            data = fetched.get(uid)
            raw = data.get(b"RFC822") if data else None
            if not raw:
                logger.warning(
                    "Email receiver: uid=%d not fetched/empty — stopping drain to retry next cycle",
                    uid,
                )
                break
            self._process_one(uid, raw)
            new_high = uid
            self._set_last_uid(new_high)
        return new_high

    def _process_one(self, uid: int, raw: bytes) -> None:
        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            logger.exception("Email receiver: undecodable message uid=%d", uid)
            return
        if not is_sale_notification(
            msg,
            from_contains=settings.sale_email_from,
            subject_contains=settings.sale_email_subject_contains,
        ):
            logger.debug("Email receiver: uid=%d not a sale notification — skipping", uid)
            return
        try:
            parsed = parse_message(msg)
        except EmailParseError as e:
            logger.warning("Email receiver: uid=%d parse failed: %s", uid, e)
            return

        from app.db.session import SessionLocal

        with SessionLocal() as session:
            summary = flag_email_sale(session, parsed, source="email")
            session.commit()
        logger.info(
            "Email receiver: uid=%d order=%s flagged %d unit(s), %d unmatched item(s)",
            uid,
            parsed.order_id or "—",
            len(summary.flagged_unit_ids),
            len(summary.unmatched_items),
        )

    # -- UID persistence ------------------------------------------------

    def _get_last_uid(self) -> int | None:
        from app.db.session import SessionLocal
        from app.settings_store import get_setting

        with SessionLocal() as session:
            raw = get_setting(session, _LAST_UID_KEY)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _set_last_uid(self, uid: int) -> None:
        from app.db.session import SessionLocal
        from app.settings_store import set_setting

        with SessionLocal() as session:
            set_setting(session, _LAST_UID_KEY, str(uid))
            session.commit()


# ---- module-level lifecycle (wired into FastAPI lifespan) --------------

_receiver: ImapIdleReceiver | None = None


def start_email_receiver() -> ImapIdleReceiver | None:
    """Start the receiver if enabled and configured. Idempotent."""
    global _receiver
    if _receiver is not None:
        return _receiver
    if os.environ.get("TAG_DISABLE_EMAIL_RECEIVER") == "1":
        return None
    if not settings.email_receiver_enabled:
        logger.info("Email receiver disabled (EMAIL_RECEIVER_ENABLED=false) — not starting")
        return None
    if not settings.imap_username or not settings.imap_app_password:
        logger.warning(
            "Email receiver enabled but IMAP_USERNAME / GMAIL_APP_PASSWORD unset — not starting"
        )
        return None

    _receiver = ImapIdleReceiver()
    _receiver.start()
    return _receiver


def stop_email_receiver() -> None:
    """Stop the receiver if running. Idempotent."""
    global _receiver
    if _receiver is not None:
        _receiver.stop()
        _receiver = None


def receiver_status() -> dict:
    """Snapshot of receiver health for the /healthz endpoint and alerting."""
    if _receiver is None:
        disabled_by_env = os.environ.get("TAG_DISABLE_EMAIL_RECEIVER") == "1"
        enabled = (
            not disabled_by_env
            and settings.email_receiver_enabled
            and bool(settings.imap_username and settings.imap_app_password)
        )
        return {"enabled": enabled, "connected": False, "running": False}
    s = _receiver.status()
    s["running"] = True
    return s
