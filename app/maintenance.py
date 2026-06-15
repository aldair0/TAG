"""Startup/periodic maintenance helpers: orphan-driver reaper, disk checks.

Kept tiny and dependency-free (stdlib only) so it's safe to call from the
app lifespan and the scheduler without dragging in heavy imports.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

from app.paths import app_dir

logger = logging.getLogger(__name__)

# Automation *driver* binaries only — never "chrome.exe"/"msedge.exe", which
# is what staff use to browse. Killing the driver is safe; killing the browser
# would disrupt a person.
_DRIVER_IMAGES = ("chromedriver.exe", "msedgedriver.exe", "undetected_chromedriver.exe")


def reap_orphan_drivers() -> int:
    """Kill leftover automation-driver processes — orphans from a prior run
    that was hard-killed mid-sync (the normal path already quits the driver in
    a ``finally``). Call at **startup**, when no legitimate sync is in flight,
    so a stale driver can't accumulate across crash-restart cycles over a long
    uptime. Windows-only; a no-op elsewhere. Best-effort: never raises.
    """
    if not sys.platform.startswith("win"):
        return 0
    reaped = 0
    for image in _DRIVER_IMAGES:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            # rc 0 = killed something; rc 128 = "not found" (the common case).
            if result.returncode == 0:
                reaped += 1
                logger.info("maintenance: reaped orphan driver %s", image)
        except Exception:
            logger.debug("maintenance: reap of %s failed", image, exc_info=True)
    return reaped


def disk_free_gb(path: str | None = None) -> float:
    """Free space (GB) on the volume holding TAG_HOME."""
    usage = shutil.disk_usage(str(path or app_dir()))
    return usage.free / (1024 ** 3)
