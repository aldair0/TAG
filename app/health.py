"""Subsystem health snapshot for /healthz, the status page, and alerting.

Every probe is defensive — a failure in one subsystem becomes that
subsystem's red status, never an exception out of ``collect_health`` — so
the health endpoint is the *last* thing to break. ``status`` is:

- ``ok``       — DB reachable and disk healthy.
- ``degraded`` — app is up but something needs attention (receiver down,
  disk low, backups stale). The watchdog leaves these alone (the process is
  fine); alerting (Wave 3) is what chases them.

The HTTP layer maps a hard-down DB to 503 so even a dumb external watchdog
bounces the service; everything else stays 200.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from app import __version__
from app.config import settings
from app.paths import backups_dir

logger = logging.getLogger(__name__)

DISK_FREE_WARN_GB = 2.0
BACKUP_STALE_HOURS = 26  # daily backup + slack


def collect_health() -> dict:
    db = _db_health()
    disk = _disk_health()
    health = {
        "version": __version__,
        "db": db,
        "disk": disk,
        "receiver": _receiver_health(),
        "scheduler": _scheduler_health(),
        "backup": _backup_health(),
        "update": _update_health(),
    }
    # Overall status: DB down ⇒ "down" (watchdog bounces). Disk-low or the
    # always-on receiver being unhealthy ⇒ "degraded". Backup staleness and
    # scheduler liveness are reported per-subsystem (and chased by alerting),
    # but kept out of the overall roll-up so a stale backup or a paused
    # scheduler doesn't trip the watchdog or the liveness smoke check.
    if not db.get("ok"):
        health["status"] = "down"
    elif not disk.get("ok") or not health["receiver"].get("ok"):
        health["status"] = "degraded"
    else:
        health["status"] = "ok"
    return health


def _db_health() -> dict:
    try:
        from app.db.base import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        size = None
        if settings.sqlite_path and settings.sqlite_path.exists():
            size = settings.sqlite_path.stat().st_size
        return {"ok": True, "size_bytes": size}
    except Exception as e:
        logger.exception("health: db check failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _disk_health() -> dict:
    try:
        from app.maintenance import disk_free_gb

        free = disk_free_gb()
        return {"ok": free >= DISK_FREE_WARN_GB, "free_gb": round(free, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _receiver_health() -> dict:
    try:
        from app.inbound_email.receiver import receiver_status

        s = receiver_status()
        # Healthy = either disabled-by-config (not expected to run) or running
        # and connected. Auth failures or a disconnected-but-enabled receiver
        # are degraded.
        if not s.get("enabled"):
            s["ok"] = True
        else:
            s["ok"] = bool(s.get("connected")) and not s.get("auth_failures")
        return s
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _scheduler_health() -> dict:
    try:
        import time

        from app.scheduler import last_tick_at, scheduler_is_alive

        alive = scheduler_is_alive()
        last = last_tick_at()
        return {
            "ok": alive,
            "alive": alive,
            "seconds_since_tick": (time.time() - last) if last else None,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _update_health() -> dict:
    """Cached update-check result (no network call here)."""
    try:
        from app.updater import update_status

        return update_status()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _backup_health() -> dict:
    try:
        d = backups_dir()
        backups = sorted(d.glob("tag_inventory-*.db")) if d.exists() else []
        if not backups:
            # No backup yet isn't "unhealthy" on a fresh install.
            return {"ok": True, "count": 0, "latest": None, "age_hours": None}
        latest = max(backups, key=lambda p: p.stat().st_mtime)
        age_h = (datetime.now().timestamp() - latest.stat().st_mtime) / 3600
        return {
            "ok": age_h <= BACKUP_STALE_HOURS,
            "count": len(backups),
            "latest": latest.name,
            "age_hours": round(age_h, 1),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
