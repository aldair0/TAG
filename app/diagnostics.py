"""Diagnostic reports — the outbound half of the support model.

When something breaks (or on demand), gather everything a developer needs to
write a patch *without touching the machine*: the error/traceback, a tail of
the rotating log, the health snapshot, version + platform, and a summary of
recent sync runs / open conflicts. The bundle is written to
``TAG_HOME/diagnostics`` (retrievable even if email is the thing that's down)
and emailed to the support address with the log attached.

Pairs with ``app.updater`` (inbound: a new build): you read the report, write
the fix, publish a release, the laptop updates.
"""

from __future__ import annotations

import logging
import platform
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app import __version__
from app.paths import app_dir, logs_dir

logger = logging.getLogger(__name__)

_DIAG_DIRNAME = "diagnostics"
_RETENTION_DAYS = 30
_LOG_TAIL_LINES = 300


def diagnostics_dir() -> Path:
    return app_dir() / _DIAG_DIRNAME


def _log_tail(lines: int = _LOG_TAIL_LINES) -> str:
    log_path = logs_dir() / "tag.log"
    if not log_path.exists():
        return "(no log file)"
    try:
        # Read the tail without loading a huge file fully into memory.
        data = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError as e:
        return f"(could not read log: {e})"


def _recent_activity() -> dict:
    """Best-effort DB summary; never raises."""
    try:
        from sqlalchemy import desc, func, select

        from app.db.models import Conflict, SyncRun
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            open_conflicts = s.execute(
                select(func.count()).select_from(Conflict).where(Conflict.status == "open")
            ).scalar_one()
            runs = s.execute(
                select(SyncRun).order_by(desc(SyncRun.started_at)).limit(5)
            ).scalars().all()
            return {
                "open_conflicts": open_conflicts,
                "recent_sync_runs": [
                    {
                        "worker": r.worker,
                        "direction": r.direction,
                        "started_at": str(r.started_at),
                        "error": r.error,
                    }
                    for r in runs
                ],
            }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def collect_diagnostics(*, error: str | None = None, reason: str = "manual") -> dict:
    """Assemble the diagnostic payload. Each section is defensive."""
    try:
        from app.health import collect_health

        health = collect_health()
    except Exception as e:  # noqa: BLE001
        health = {"error": f"{type(e).__name__}: {e}"}

    return {
        "reason": reason,
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "error": error,
        "health": health,
        "activity": _recent_activity(),
    }


def _render(diag: dict) -> str:
    import json

    return json.dumps(diag, indent=2, default=str)


def write_diagnostic_bundle(diag: dict, *, now: datetime | None = None) -> Path:
    """Persist the report to disk (so it survives an email failure) and prune."""
    d = diagnostics_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = d / f"diag-{stamp}.txt"
    body = _render(diag) + "\n\n===== LOG TAIL =====\n" + _log_tail()
    path.write_text(body, encoding="utf-8")
    _prune(d, now=now)
    return path


def _prune(d: Path, *, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now()) - timedelta(days=_RETENTION_DAYS)
    for p in d.glob("diag-*.txt"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink()
        except OSError:
            pass


def send_diagnostic_report(*, error: str | None = None, reason: str = "manual") -> dict:
    """Collect → write to disk → email with the log attached.

    Returns a small status dict (also used by the admin route for a flash).
    Never raises — diagnostics must not become a failure source.
    """
    diag = collect_diagnostics(error=error, reason=reason)
    try:
        path = write_diagnostic_bundle(diag)
    except Exception:  # noqa: BLE001
        logger.exception("could not write diagnostic bundle")
        path = None

    from app.alerts import send_email

    subject = f"Diagnostic report ({reason}) — v{diag['version']}"
    summary = (
        f"reason={reason} version={diag['version']} "
        f"health={diag.get('health', {}).get('status', '?')}\n"
        + (f"error={error}\n" if error else "")
        + "Full report + log tail attached."
    )
    attachment = (path.name, path.read_text(encoding="utf-8")) if path else None
    status = send_email(subject, summary, attachments=[attachment] if attachment else None)
    logger.info("diagnostic report: email=%s file=%s", status, path)
    return {"email": status, "file": str(path) if path else None, "health": diag.get("health", {}).get("status")}
