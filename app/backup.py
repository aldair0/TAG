"""Local end-of-day SQLite backup with rolling retention.

Uses SQLite's **online backup API** (``sqlite3.Connection.backup``) so the
copy is consistent even while the app is mid-write — no need to stop the
service. Backups land in ``TAG_HOME/backups`` as
``tag_inventory-YYYYMMDD-HHMMSS.db`` and anything older than the retention
window is pruned.

Scheduled once per day (see ``app.scheduler``); also safe to call manually.
Durability scope is intentionally *local* for now (second-disk / offsite is a
later decision in the reliability plan).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.paths import backups_dir

logger = logging.getLogger(__name__)

_PREFIX = "tag_inventory-"
_SUFFIX = ".db"
_STAMP_FMT = "%Y%m%d-%H%M%S"


class BackupError(RuntimeError):
    pass


def run_backup(
    *,
    src_path: Path | None = None,
    dest_dir: Path | None = None,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> Path:
    """Create one backup and prune old ones. Returns the new backup path.

    Args are injectable for tests; production reads them from settings/paths.
    """
    src = src_path or settings.sqlite_path
    if src is None:
        raise BackupError("backup only supported for sqlite databases")
    src = Path(src)
    if not src.exists():
        raise BackupError(f"source DB not found: {src}")

    dest_dir = dest_dir or backups_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    retention = settings.backup_retention_days if retention_days is None else retention_days
    retention = max(1, retention)  # never let a 0/negative window delete everything
    stamp = (now or datetime.now()).strftime(_STAMP_FMT)
    dest = dest_dir / f"{_PREFIX}{stamp}{_SUFFIX}"

    _hot_copy(src, dest)
    logger.info("backup: wrote %s (%d bytes)", dest.name, dest.stat().st_size)

    pruned = prune_old(dest_dir, retention_days=retention, now=now)
    if pruned:
        logger.info("backup: pruned %d backup(s) older than %d days", pruned, retention)
    return dest


def _hot_copy(src: Path, dest: Path) -> None:
    """Consistent copy of a live SQLite DB via the online backup API."""
    # Unique temp name so a manual + scheduled backup in the same second can't
    # collide on the same .part file.
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    src_con = sqlite3.connect(str(src))
    try:
        # The backup connection is separate from the app engine, so it needs
        # its own busy_timeout or it fails immediately under WAL write load.
        src_con.execute("PRAGMA busy_timeout=5000")
        dst_con = sqlite3.connect(str(tmp))
        try:
            with dst_con:
                src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()
    # Atomic publish: only a fully-written backup ever appears with the real name.
    tmp.replace(dest)


def prune_old(
    dest_dir: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete backups whose timestamp is older than the retention window.

    Returns the number removed. Files whose names don't parse are left alone.
    """
    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    removed = 0
    for path in dest_dir.glob(f"{_PREFIX}*{_SUFFIX}"):
        ts = _parse_stamp(path)
        if ts is not None and ts < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("backup: could not delete old backup %s", path, exc_info=True)
    return removed


def _parse_stamp(path: Path) -> datetime | None:
    name = path.name
    if not (name.startswith(_PREFIX) and name.endswith(_SUFFIX)):
        return None
    core = name[len(_PREFIX) : -len(_SUFFIX)]
    try:
        return datetime.strptime(core, _STAMP_FMT)
    except ValueError:
        return None
