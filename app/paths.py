"""Resource path resolution that works in both dev (running from source) and
frozen mode (PyInstaller one-folder bundle).

PyInstaller exposes the unpacked bundle root via ``sys._MEIPASS``. In dev, we
fall back to the package-relative location.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """Root directory containing bundled non-code resources (templates, static).

    In dev: the repo root (parent of the ``app`` package).
    In frozen mode: ``sys._MEIPASS``.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Root for user-editable, persistent state — ``.env``, ``data/``,
    ``logs/``, ``backups/``. This is the "TAG_HOME" of the install.

    Deliberately NOT ``resource_root()``: bundled resources are read-only and
    (one-file build) unpacked to a temp dir wiped on exit — useless for config
    the operator edits or a DB that must survive restarts and *upgrades*.

    Resolution order:
    1. ``TAG_HOME`` env var, if set — the production anchor. Point this at a
       stable folder *outside* the versioned program bundle so an upgrade
       (folder swap) can't wipe credentials or the database.
    2. Frozen: the directory containing the executable.
    3. Dev: the repo root.
    """
    override = os.environ.get("TAG_HOME")
    if override:
        return Path(override).resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def env_file() -> Path:
    """Absolute path to the ``.env`` config file (see :func:`app_dir`)."""
    return app_dir() / ".env"


def data_dir() -> Path:
    """Persistent data directory (SQLite DB, image cache) under TAG_HOME."""
    return app_dir() / "data"


def logs_dir() -> Path:
    """Rotating-log directory under TAG_HOME."""
    return app_dir() / "logs"


def backups_dir() -> Path:
    """Local DB-backup directory under TAG_HOME."""
    return app_dir() / "backups"


def templates_dir() -> Path:
    return resource_root() / "app" / "templates"


def static_dir() -> Path:
    return resource_root() / "app" / "static"
