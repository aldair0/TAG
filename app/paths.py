"""Resource path resolution that works in both dev (running from source) and
frozen mode (PyInstaller one-folder bundle).

PyInstaller exposes the unpacked bundle root via ``sys._MEIPASS``. In dev, we
fall back to the package-relative location.
"""

from __future__ import annotations

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


def templates_dir() -> Path:
    return resource_root() / "app" / "templates"


def static_dir() -> Path:
    return resource_root() / "app" / "static"
