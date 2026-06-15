"""Centralised logging configuration.

Replaces the bare ``logging.basicConfig`` (stderr only) with a rotating
**file** log under ``TAG_HOME/logs`` *plus* stderr. For an unattended
2-week run, a persistent, size-capped log is the difference between "we can
see what happened Tuesday" and a black box. Size cap (max_bytes ×
backup_count) keeps logs from ever filling the disk.

Idempotent: safe to call more than once (import-time + entrypoint).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import settings
from app.paths import logs_dir

_configured = False

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    formatter = logging.Formatter(_FORMAT)

    handlers: list[logging.Handler] = []

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    handlers.append(stream)

    # File logging is skipped in tests (TAG_DISABLE_FILE_LOG=1) so the suite
    # doesn't litter the repo with a logs/ dir.
    if os.environ.get("TAG_DISABLE_FILE_LOG") != "1":
        try:
            logs = logs_dir()
            logs.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                logs / "tag.log",
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError:
            # Never let a logging-setup failure stop the app from booting.
            root.warning("Could not open log file in %s — stderr only", logs_dir(), exc_info=True)

    root.handlers[:] = handlers
    _configured = True
