"""Update check — the inbound half of the support model.

Polls the GitHub Releases API for a version newer than the running one and,
when found, emails a notification with the release link. The shop then runs
the new installer (data in TAG_HOME is preserved across the upgrade).

This module deliberately stops at **notify**. The actual download-and-apply is
left for when the release/repo logistics are settled — and whatever does it
MUST verify a signature or checksum on the downloaded installer before running
it (a laptop that runs an unverified artifact off a URL is an RCE risk).

No network call happens until ``GITHUB_REPO`` is configured; ``update_status``
returns the cached last result so /healthz never makes a live call.
"""

from __future__ import annotations

import logging

import httpx

from app import __version__
from app.config import settings

logger = logging.getLogger(__name__)

_last_check: dict | None = None


def _parse_version(s: str) -> tuple[int, ...]:
    """Lenient semver-ish parse: 'v1.2.3' -> (1, 2, 3). Non-numeric -> 0."""
    s = (s or "").strip().lstrip("vV")
    out: list[int] = []
    for part in s.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def check_for_update(*, client: httpx.Client | None = None) -> dict:
    """Query the configured repo's latest release. Caches + returns the result.
    Never raises."""
    global _last_check
    repo = settings.github_repo.strip()
    if not (settings.update_check_enabled and repo):
        _last_check = {"enabled": False, "current": __version__}
        return _last_check

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    owns = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        latest = data.get("tag_name") or ""
        result = {
            "enabled": True,
            "current": __version__,
            "latest": latest,
            "update_available": _parse_version(latest) > _parse_version(__version__),
            "url": data.get("html_url"),
            "published_at": data.get("published_at"),
        }
    except Exception as e:  # noqa: BLE001
        result = {"enabled": True, "current": __version__, "error": f"{type(e).__name__}: {e}"}
    finally:
        if owns:
            client.close()

    _last_check = result
    return result


def update_status() -> dict:
    """Cached last check — for /healthz and the dashboard (no network)."""
    return _last_check or {"checked": False, "current": __version__}


def notify_if_update() -> dict:
    """Check, and email a one-time notification per new version. Scheduled."""
    result = check_for_update()
    if not result.get("update_available"):
        return result

    latest = result["latest"]
    from app.db.session import SessionLocal
    from app.settings_store import get_setting, set_setting

    with SessionLocal() as s:
        if get_setting(s, "update_notified_version") == latest:
            return result  # already told them about this version
        set_setting(s, "update_notified_version", latest)
        s.commit()

    from app.alerts import send_alert

    send_alert(
        f"Update available: {latest}",
        f"A new TAG Inventory release ({latest}) is available "
        f"(currently running {result['current']}).\n"
        f"Release: {result.get('url')}\n\n"
        "Download the installer from the release and run it on the shop PC to "
        "update. Inventory, sales, and settings are preserved across the upgrade.",
        key=f"update_{latest}",
    )
    return result
