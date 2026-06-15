"""TCGPlayer ingest scheduler.

Pieces:
- ``is_open_hours`` — pure predicate, store-hours math (DST-aware).
- ``SyncCoordinator`` — single-slot run gate with one queued follow-up
  shared by the manual button and the scheduled tick.
- ``start_scheduler`` / ``stop_scheduler`` — APScheduler lifecycle wired
  into the FastAPI lifespan.
- ``_tick`` — one scheduler iteration; gates on auto-sync flag + open hours.

The source resolution (fixture vs live) is intentionally one function so
that swapping to ``LiveTCGPlayerSource`` later is a single-line change.
"""

from __future__ import annotations

import enum
import logging
import threading
from datetime import datetime, time as _time
from typing import Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hhmm(s: str) -> _time:
    h, m = s.split(":")
    return _time(int(h), int(m))


def is_open_hours(
    now: datetime, *, tz: str, open_t: str, close_t: str, days: str
) -> bool:
    """True iff ``now``, projected into ``tz``, falls inside the store-open
    window ``[open_t, close_t)`` on a permitted weekday.

    Naive ``now`` values are treated as already in ``tz``.
    """
    z = ZoneInfo(tz)
    now = now.replace(tzinfo=z) if now.tzinfo is None else now.astimezone(z)
    allowed = {d.strip().lower() for d in days.split(",") if d.strip()}
    if _DAYS[now.weekday()] not in allowed:
        return False
    return _hhmm(open_t) <= now.time() < _hhmm(close_t)


class RunStatus(enum.Enum):
    STARTED = "started"
    QUEUED = "queued"
    REJECTED = "rejected"


class SyncCoordinator:
    """Single-slot run coordinator with one queued follow-up.

    Both the manual button and the APScheduler tick call ``request()``.
    The first request runs immediately on a background thread. A second
    request while the first is in flight claims the single queued slot
    and runs once the current run completes. A third (or further)
    request is rejected — there's nothing useful to gain from queueing
    three deep when each run already re-reads the whole source.
    """

    def __init__(self, run_fn: Callable[[], None]) -> None:
        self._run_fn = run_fn
        self._lock = threading.Lock()
        self._running = False
        self._queued = False
        self._idle = threading.Event()
        self._idle.set()
        self._last_diag = 0.0  # throttle for auto diagnostic reports

    def request(self) -> RunStatus:
        with self._lock:
            if not self._running:
                self._running = True
                self._idle.clear()
                threading.Thread(
                    target=self._loop, name="SyncCoordinator", daemon=True
                ).start()
                return RunStatus.STARTED
            if not self._queued:
                self._queued = True
                return RunStatus.QUEUED
            return RunStatus.REJECTED

    def state(self) -> dict:
        with self._lock:
            return {"running": self._running, "queued": self._queued}

    def _maybe_send_diagnostic(self) -> None:
        """Email a diagnostic report when a scheduled sync crashes unattended —
        this is the prime place a code bug surfaces with no one watching.
        Throttled so a persistent failure doesn't email every run."""
        import time
        import traceback

        now = time.time()
        if now - self._last_diag < 3600:
            return
        self._last_diag = now
        try:
            from app.diagnostics import send_diagnostic_report

            send_diagnostic_report(error=traceback.format_exc(), reason="sync_failure")
        except Exception:
            logger.exception("failed to send sync-failure diagnostic")

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout=timeout)

    def _loop(self) -> None:
        try:
            while True:
                try:
                    self._run_fn()
                except Exception:
                    logger.exception("Sync run failed inside coordinator")
                    self._maybe_send_diagnostic()
                with self._lock:
                    if not self._queued:
                        self._running = False
                        self._idle.set()
                        return
                    self._queued = False  # consume queued slot, loop
        except Exception:
            logger.exception("Coordinator loop crashed")
            with self._lock:
                self._running = False
                self._queued = False
                self._idle.set()


# ---- APScheduler lifecycle + tick --------------------------------------

# Imported lazily inside functions where possible to keep test discovery
# fast and to avoid pulling APScheduler into the import graph for unit
# tests of the predicate or the coordinator.

_scheduler = None  # type: ignore[var-annotated]
_coordinator: SyncCoordinator | None = None
_last_tick_at: float | None = None  # wall time of the most recent _tick()


def last_tick_at() -> float | None:
    """Wall-clock time (time.time()) of the last scheduler tick, or None."""
    return _last_tick_at


LIVE_CSV_PATH = "data/csv/tcgplayer_pricing.csv"


def _resolve_source():
    """Single chokepoint for source resolution. Order of preference:

    1. ``data/csv/tcgplayer_pricing.csv`` — manually placed real export from
       the TCGPlayer PRO Seller portal (current production-ish path until
       Selenium-driven downloads land).
    2. ``test_data/tcgplayer_fixture.csv`` — synthetic fixture; the
       fallback used by tests and pre-credentials dev runs.
    3. ``None`` — nothing to ingest; the worker logs a warning and skips.

    When ``LiveTCGPlayerSource`` (Selenium) lands, swap the body of this
    function to construct it.
    """
    from pathlib import Path

    from app.routes.sync import _default_fixture_path
    from app.sync.tcgplayer import FixtureTCGPlayerSource

    live = Path(LIVE_CSV_PATH)
    if live.exists():
        return FixtureTCGPlayerSource(live)
    fixture: Path = _default_fixture_path()
    if fixture.exists():
        return FixtureTCGPlayerSource(fixture)
    return None


def _attempt_portal_download() -> bool:
    """Try to fetch a fresh CSV from the TCGPlayer portal in headless mode.

    Returns True if a new file was downloaded, False on any failure.
    Failures are expected when the session has expired — the caller falls
    back to whatever CSV is already on disk.
    """
    from app.config import settings
    from app.sync.tcgplayer.portal_downloader import (
        PortalDownloadError,
        download_pricing_csv,
        find_browser_executable,
    )

    browser = find_browser_executable()
    if browser is None:
        logger.warning("Scheduler download: no Chrome/Edge found — skipping portal pull")
        return False

    try:
        download_pricing_csv(
            headless=True,
            login_wait_sec=float(settings.tcgplayer_portal_auto_login_wait_sec),
            download_timeout_sec=float(settings.tcgplayer_portal_download_timeout_sec),
        )
        logger.info("Scheduler: fresh CSV downloaded from portal")
        return True
    except PortalDownloadError as e:
        logger.warning("Scheduler: portal download failed (%s: %s) — using cached CSV", type(e).__name__, e)
        return False
    except Exception:
        logger.exception("Scheduler: unexpected error during portal download — using cached CSV")
        return False


def _run_ingest_for_scheduler() -> None:
    import os

    from app.db.session import SessionLocal
    from app.sync.tcgplayer import run_ingest

    if os.environ.get("TAG_DISABLE_INGEST") == "1":
        # Test short-circuit so route smoke tests don't spawn real
        # ingest threads that hit the network.
        return

    _attempt_portal_download()  # best-effort; failures fall through to cached CSV

    source = _resolve_source()
    if source is None:
        logger.warning("No TCGPlayer source available — skipping run")
        return
    with SessionLocal() as session:
        run_ingest(source, session)


def coordinator() -> SyncCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = SyncCoordinator(run_fn=_run_ingest_for_scheduler)
    return _coordinator


def _request_run() -> RunStatus:
    """Indirection so tests can monkeypatch without touching the global."""
    return coordinator().request()


def _tick(session_factory=None) -> None:
    """One scheduler iteration. Gates on auto-sync flag + open hours."""
    import time

    from app.config import settings
    from app.db.session import SessionLocal
    from app.settings_store import get_setting

    global _last_tick_at
    _last_tick_at = time.time()  # liveness heartbeat (set regardless of gates)

    if session_factory is None:
        session_factory = SessionLocal

    with session_factory() as s:
        flag = get_setting(s, "tcgplayer_auto_sync", default="on")

    if flag != "on":
        logger.debug("Scheduler tick: auto-sync disabled — skipping")
        return
    if not is_open_hours(
        datetime.now(),
        tz=settings.store_timezone,
        open_t=settings.store_open_time,
        close_t=settings.store_close_time,
        days=settings.store_open_days,
    ):
        logger.debug("Scheduler tick: outside store hours — skipping")
        return
    _request_run()


def start_scheduler():
    """Start the BackgroundScheduler. Idempotent."""
    import os

    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if os.environ.get("TAG_DISABLE_SCHEDULER") == "1":
        # Test short-circuit so the BackgroundScheduler isn't created
        # during pytest runs.
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from app.config import settings

    s = BackgroundScheduler(timezone=settings.store_timezone)
    s.add_job(
        _tick,
        IntervalTrigger(minutes=settings.tcgplayer_sync_interval_min),
        id="tcgplayer_tick",
        coalesce=True,
        max_instances=1,
    )

    # Maintenance: truncate the WAL hourly so it can't grow unbounded across a
    # long uptime (one long-lived reader can otherwise starve auto-checkpoint).
    s.add_job(
        _maintenance_checkpoint,
        IntervalTrigger(hours=1),
        id="wal_checkpoint",
        coalesce=True,
        max_instances=1,
    )

    # Disk-space guard: warn (and, in Wave 3, alert) when the volume holding
    # TAG_HOME runs low, before writes start failing.
    s.add_job(
        _disk_guard,
        IntervalTrigger(hours=1),
        id="disk_guard",
        coalesce=True,
        max_instances=1,
    )

    # Update check: notify when a newer release is published (no-op until
    # GITHUB_REPO is configured).
    if settings.update_check_enabled:
        s.add_job(
            _update_check,
            IntervalTrigger(hours=settings.update_check_interval_hours),
            id="update_check",
            coalesce=True,
            max_instances=1,
        )

    # End-of-day local backup with rolling retention. A malformed BACKUP_TIME
    # must disable only this job, never abort scheduler startup (which would
    # also take down auto-sync + WAL/disk maintenance).
    if settings.backup_enabled:
        try:
            hh, mm = _parse_hhmm(settings.backup_time)
            s.add_job(
                _daily_backup,
                CronTrigger(hour=hh, minute=mm),
                id="daily_backup",
                coalesce=True,
                max_instances=1,
            )
        except Exception:
            logger.exception(
                "Invalid BACKUP_TIME=%r — daily backup disabled (other jobs unaffected)",
                settings.backup_time,
            )

    s.start()
    _scheduler = s
    logger.info(
        "Scheduler started: tick every %d min, hours %s-%s %s; backup=%s @ %s, retain %dd",
        settings.tcgplayer_sync_interval_min,
        settings.store_open_time,
        settings.store_close_time,
        settings.store_timezone,
        "on" if settings.backup_enabled else "off",
        settings.backup_time,
        settings.backup_retention_days,
    )
    return s


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _maintenance_checkpoint() -> None:
    """Hourly WAL truncate. Swallows errors so a maintenance hiccup never
    propagates out of the scheduler thread."""
    try:
        from app.db.base import checkpoint_wal

        checkpoint_wal()
    except Exception:
        logger.exception("WAL checkpoint failed")


def _daily_backup() -> None:
    """End-of-day local backup. Errors are logged + alerted, never raised."""
    try:
        from app.backup import run_backup

        run_backup()
    except Exception as e:
        logger.exception("Daily backup failed")
        from app.alerts import send_alert

        send_alert(
            "Daily backup FAILED",
            f"The end-of-day local backup did not complete: {type(e).__name__}: {e}\n"
            "Inventory/sales history is not being backed up — investigate the disk "
            "and the backups folder.",
            key="backup_failed",
        )


def _update_check() -> None:
    """Notify if a newer release is available. Errors logged, never raised."""
    try:
        from app.updater import notify_if_update

        notify_if_update()
    except Exception:
        logger.exception("update check failed")


_DISK_WARN_GB = 2.0


def _disk_guard() -> None:
    """Warn when free disk drops below the threshold. (Wave 3 will also alert.)"""
    try:
        from app.maintenance import disk_free_gb

        free = disk_free_gb()
        if free < _DISK_WARN_GB:
            logger.warning("disk guard: only %.2f GB free on TAG_HOME volume", free)
            from app.alerts import send_alert

            send_alert(
                "Low disk space",
                f"Only {free:.2f} GB free on the TAG_HOME volume. When it fills, "
                "database writes, backups, and logging all fail. Free up space.",
                key="disk_low",
            )
    except Exception:
        logger.exception("disk guard check failed")


def stop_scheduler() -> None:
    """Shut down the scheduler if running. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_is_alive() -> bool:
    """Return True if the background scheduler thread is running."""
    return _scheduler is not None and getattr(_scheduler, "running", False)
