"""Manual "Get from TCGPlayer Portal" — drive a real Chrome/Edge to the
seller pricing page, click "Export From Live", wait for the CSV
download, and slot it into ``data/csv/tcgplayer_pricing.csv`` for the
ingest pipeline to pick up.

This is the only piece that actually talks to the TCGPlayer admin
portal. It exists alongside the in-portal export workflow we'd been
relying on (where a human exports manually and drops the file into
``data/csv/``) — eliminating that human step is the whole point.

Architecture:
- A real browser binary (Chrome or Edge) is launched with a persistent
  ``user-data-dir`` so the seller stays logged in between runs. First
  call typically requires a manual login in the browser window that
  pops up; subsequent calls land straight on the pricing page.
- We use Selenium's built-in driver management (Selenium Manager) which
  auto-downloads a matching ``chromedriver``/``msedgedriver``. We
  branched on the browser binary to pick the right Driver class.
- A handful of basic stealth tweaks (suppressing the
  ``--enable-automation`` switch, hiding ``navigator.webdriver``)
  reduce automation fingerprint. The seller admin is authenticated, so
  full-fledged Cloudflare evasion isn't typically required — auth
  cookies usually bypass the harder challenges.
- Downloads are funneled into ``data/csv/_incoming/``. Once a new .csv
  appears (and its ``.crdownload`` companion is gone), the file is
  moved to the canonical target with old versions rotating into
  ``data/csv/_archive/`` (keep last 5).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Serializes every profile-driving browser session (the CSV download and
# the auth-cookie snag). Without it, the auth_status poll can launch a
# second Chrome on the same profile while a download is mid-flight; the
# newcomer's ensure_profile_free() then evicts the download's window and
# the session dies with InvalidSessionIdException. Download takes it
# blocking; snag tries non-blocking and skips if a download holds it.
_BROWSER_LOCK = threading.Lock()

PRICING_URL = "https://store.tcgplayer.com/admin/pricing"
LOGIN_URL_PATTERN = "/admin/Login"
EXPORT_BUTTON_CSS = "input.info-icon[value='Export From Live']"

# Where the canonical CSV ends up — same path _resolve_source() reads.
PRICING_TARGET = Path("data/csv/tcgplayer_pricing.csv")
DOWNLOAD_DIR = Path("data/csv/_incoming")
ARCHIVE_DIR = Path("data/csv/_archive")
PROFILE_DIR = Path("data/chrome_profile")

_PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
_PROGRAM_FILES_X86 = Path(
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
)
_LOCALAPPDATA = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
)

# Order matters: undetected-chromedriver is most reliable against Chrome.
# Edge is a close second (Chromium-based; the same WebDriver protocol).
_KNOWN_BROWSERS: list[Path] = [
    _PROGRAM_FILES / "Google" / "Chrome" / "Application" / "chrome.exe",
    _PROGRAM_FILES_X86 / "Google" / "Chrome" / "Application" / "chrome.exe",
    _LOCALAPPDATA / "Google" / "Chrome" / "Application" / "chrome.exe",
    _PROGRAM_FILES / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    _PROGRAM_FILES_X86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    _LOCALAPPDATA / "Microsoft" / "Edge" / "Application" / "msedge.exe",
]


class PortalDownloadError(RuntimeError):
    """Base class for download failures so the route can render a
    user-friendly message instead of a 500 stack trace."""


class BrowserNotFoundError(PortalDownloadError):
    pass


class LoginRequiredError(PortalDownloadError):
    """Pricing page redirected to login and the user didn't authenticate
    in the allotted window. Click the button again and log in when the
    browser pops up."""


class DownloadTimeoutError(PortalDownloadError):
    """The browser clicked the export button, but no CSV materialized
    in the download dir within the timeout."""


class PortalLayoutError(PortalDownloadError):
    """An expected DOM element wasn't found — the seller portal layout
    may have changed. See ``EXPORT_BUTTON_CSS`` and friends in
    portal_downloader.py."""


# ---- testable helpers --------------------------------------------------


def _parse_cookies(text: str) -> list[dict]:
    """Parse user-pasted cookies into ``add_cookie``-ready dicts.

    Accepts ``;`` and/or newline separated ``name=value`` pairs (the
    same format DevTools' cookie panel and "Copy as cURL" produce).
    Empty entries and entries without ``=`` are skipped silently. All
    returned cookies are scoped to ``.tcgplayer.com`` / which is what
    the captured curl showed for the auth ticket — sufficient for
    every cookie TCGPlayer's portal needs.

    Note: ``value`` is everything after the first ``=`` so base64-
    style values containing ``=`` round-trip correctly.
    """
    cookies: list[dict] = []
    if not text:
        return cookies
    # Normalize separators so we can split on a single delimiter.
    normalized = text.replace("\r", "").replace("\n", ";")
    for piece in normalized.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".tcgplayer.com",
                "path": "/",
            }
        )
    return cookies


def find_browser_executable() -> Path | None:
    """First entry in ``_KNOWN_BROWSERS`` that exists on disk, or
    ``None`` if neither Chrome nor Edge is installed in standard
    locations. Patched in tests via ``unittest.mock.patch``."""
    for p in _KNOWN_BROWSERS:
        if p.exists():
            return p
    return None


def archive_and_replace(
    *,
    source: Path,
    target: Path,
    archive_dir: Path,
    keep: int = 5,
) -> Path:
    """Move ``source`` to ``target``. If ``target`` already exists, it
    rotates into ``archive_dir/<stem>_<timestamp><suffix>`` first; the
    archive is then trimmed to the ``keep`` most recent files matching
    ``<stem>_*<suffix>`` (other files in archive_dir are left alone).

    Returns the new ``target`` path.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    if target.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        archived = archive_dir / f"{target.stem}_{ts}{target.suffix}"
        # Avoid clobbering on rapid re-runs.
        suffix_idx = 1
        while archived.exists():
            archived = archive_dir / f"{target.stem}_{ts}_{suffix_idx}{target.suffix}"
            suffix_idx += 1
        shutil.move(str(target), str(archived))

    shutil.move(str(source), str(target))

    # Trim to last `keep` matching files. Sort newest-first by mtime.
    pattern = f"{target.stem}_*{target.suffix}"
    matching = sorted(
        archive_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in matching[keep:]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Could not delete old archive: %s", old, exc_info=True)

    return target


def _await_new_csv(
    download_dir: Path,
    *,
    files_before: Iterable[Path],
    timeout_sec: float,
    poll_interval: float = 0.5,
) -> Path:
    """Wait for a new, fully-downloaded .csv to appear in
    ``download_dir``. Excludes Chrome's .crdownload partial-download
    sidecar files. Raises ``DownloadTimeoutError`` on timeout."""
    seen = {p.resolve() for p in files_before}
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        candidates = [
            f for f in download_dir.glob("*.csv") if f.resolve() not in seen
        ]
        # Exclude in-progress downloads.
        ready = [
            f
            for f in candidates
            if not (f.with_suffix(f.suffix + ".crdownload")).exists()
            and not f.name.endswith(".crdownload")
        ]
        if ready:
            # Newest first.
            ready.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return ready[0]
        time.sleep(poll_interval)
    raise DownloadTimeoutError(
        f"No CSV materialized in {download_dir} within {timeout_sec}s"
    )


# ---- main entry --------------------------------------------------------


_COOKIE_SETTINGS_KEY = "tcgplayer_portal_cookies"
_COOKIE_DOMAIN_LANDING_URL = "https://store.tcgplayer.com/"


def _inject_stored_cookies(driver) -> int:
    """Read cookies from ``app_setting[tcgplayer_portal_cookies]`` and
    inject them into the running driver. Returns the number actually
    set.

    Side-effect: navigates to ``store.tcgplayer.com`` first so that
    ``driver.add_cookie`` will accept ``.tcgplayer.com``-scoped
    cookies. Caller's subsequent ``driver.get(PRICING_URL)`` will
    carry them.

    Failures are logged, not raised — a bad cookie line shouldn't
    abort the whole portal-download attempt; the user will see the
    eventual login-redirect timeout if auth is missing.
    """
    try:
        from app.db.session import SessionLocal
        from app.settings_store import get_secret_setting
    except Exception:
        logger.warning("Could not import settings_store", exc_info=True)
        return 0

    try:
        with SessionLocal() as s:
            raw = get_secret_setting(s, _COOKIE_SETTINGS_KEY, default="") or ""
    except Exception:
        logger.warning("Could not load stored cookies", exc_info=True)
        return 0

    cookies = _parse_cookies(raw)
    if not cookies:
        return 0

    logger.info(
        "Injecting %d stored cookie(s): %s",
        len(cookies),
        ", ".join(c["name"] for c in cookies),
    )
    driver.get(_COOKIE_DOMAIN_LANDING_URL)
    set_count = 0
    for c in cookies:
        try:
            driver.add_cookie(c)
            set_count += 1
        except Exception:
            logger.warning("Failed to set cookie %s", c.get("name"), exc_info=True)
    return set_count


def _tcg_cookie_map(cookies) -> dict[str, str]:
    """Reduce a list of cookie dicts (Selenium ``get_cookies`` or CDP
    ``Storage.getCookies`` shape) to ``{name: value}`` for tcgplayer
    cookies with non-empty values."""
    out: dict[str, str] = {}
    for c in cookies:
        if "tcgplayer" not in (c.get("domain") or ""):
            continue
        name, value = c.get("name"), c.get("value")
        if name and value:
            out[name] = value
    return out


def _save_tcg_cookies(cookie_map: dict[str, str]) -> int:
    """Persist captured tcgplayer cookies to
    ``app_setting[tcgplayer_portal_cookies]`` — but only if the auth
    ticket is present, so we never overwrite a good blob with a
    logged-out set. Returns the number persisted (0 = not saved)."""
    from app.sync.tcgplayer.portal_auth import AUTH_COOKIE_NAME

    if AUTH_COOKIE_NAME not in cookie_map:
        logger.info(
            "No %s among captured cookies — not persisting (still logged out)",
            AUTH_COOKIE_NAME,
        )
        return 0

    blob = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
    try:
        from app.db.session import SessionLocal
        from app.settings_store import set_secret_setting

        with SessionLocal() as s:
            set_secret_setting(s, _COOKIE_SETTINGS_KEY, blob)
            s.commit()
    except Exception:
        logger.warning("Could not persist captured cookies", exc_info=True)
        return 0

    logger.info(
        "Persisted %d tcgplayer cookies (incl. %s)", len(cookie_map), AUTH_COOKIE_NAME
    )
    return len(cookie_map)


def _persist_driver_cookies(driver) -> int:
    """Capture cookies from a live, authenticated Selenium driver and save
    the tcgplayer ones (incl. the in-memory auth ticket). Used by the
    download flow, which already holds a driver. Returns count saved."""
    try:
        cookies = driver.get_cookies()
    except Exception:
        logger.warning("Could not read cookies from live driver", exc_info=True)
        return 0
    return _save_tcg_cookies(_tcg_cookie_map(cookies))


# ---- profile-lock self-healing ----------------------------------------
#
# A Chrome/Edge ``--user-data-dir`` is single-owner: if a second browser
# process is launched against a profile another process already holds, the
# newcomer hands off to the existing process and exits immediately. The
# WebDriver that launched it then loses its connection and raises
# ``InvalidSessionIdException`` / ``SessionNotCreatedException`` ("not
# connected to DevTools" / "chrome not reachable"). This bites us because
# ``launch_login_window`` leaves a *detached* Chrome open on the same
# profile, and stale/zombie browsers from prior runs linger too. Before
# driving Selenium we therefore evict any process holding our profile and
# clear stale lock files — a self-heal that turns a hard crash into a clean
# launch.

# Chrome's per-profile lock files (Windows uses ``lockfile``; the
# ``Singleton*`` trio appears on POSIX and is harmless to clear anywhere).
_PROFILE_LOCK_FILES = ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket")

# Windows quotes command-line args three ways: the whole arg wrapped
# (``"--user-data-dir=C:\with spaces"``), just the value wrapped
# (``--user-data-dir="C:\with spaces"``), or bare (``--user-data-dir=C:\x``).
# Try most-specific first so a value with spaces survives.
_USER_DATA_DIR_PATTERNS = (
    re.compile(r'"--user-data-dir=([^"]*)"'),
    re.compile(r'--user-data-dir="([^"]*)"'),
    re.compile(r"--user-data-dir=(\S+)"),
)


def _extract_user_data_dir(cmd: str) -> str | None:
    for pat in _USER_DATA_DIR_PATTERNS:
        m = pat.search(cmd)
        if m:
            return m.group(1)
    return None


def _profile_lock_holders(profile_dir: Path) -> list[int]:
    """PIDs of chrome/msedge processes launched against *exactly* this
    ``--user-data-dir``. Windows-only; returns ``[]`` elsewhere or on any
    failure. Strict path match so we never touch the user's everyday
    browser (which runs out of a different profile dir)."""
    if sys.platform != "win32":
        return []
    try:
        target = profile_dir.resolve()
    except OSError:
        return []

    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='chrome.exe' OR Name='msedge.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not enumerate browser processes", exc_info=True)
        return []

    holders: list[int] = []
    for line in out.splitlines():
        pid_str, _, cmd = line.partition("\t")
        if not cmd:
            continue
        raw = _extract_user_data_dir(cmd)
        if not raw:
            continue
        try:
            if Path(raw).resolve() != target:
                continue
        except OSError:
            continue
        try:
            holders.append(int(pid_str.strip()))
        except ValueError:
            continue
    return holders


def _clear_profile_locks(profile_dir: Path) -> None:
    """Remove stale single-instance lock files left by a crashed browser."""
    for name in _PROFILE_LOCK_FILES:
        lock = profile_dir / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except OSError:
            logger.debug("Could not remove lock %s", lock, exc_info=True)


def ensure_profile_free(profile_dir: Path) -> int:
    """Make ``profile_dir`` safe to launch a fresh WebDriver against:
    terminate any browser process still holding it, then clear stale lock
    files. Returns the number of processes evicted. Best-effort and
    idempotent — a no-op when the profile is already free."""
    holders = _profile_lock_holders(profile_dir)
    for pid in holders:
        logger.info("Evicting browser pid %d holding profile %s", pid, profile_dir)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("taskkill failed for pid %d", pid, exc_info=True)
    if holders:
        time.sleep(1.0)  # give the OS a moment to release the lock
    _clear_profile_locks(profile_dir)
    return len(holders)


def _chrome_major_version(browser: Path) -> int | None:
    """Best-effort major version of the browser binary.

    undetected-chromedriver 3.5.5 can't auto-detect Chrome 149 — it logs
    "could not detect version_main" and falls back to *assuming* Chrome
    108, which means it may patch/launch with the wrong version profile.
    Passing ``version_main`` explicitly fixes that. Windows-only;
    ``None`` on any failure, in which case we let UC auto-detect (the
    prior behaviour)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Item '{browser}').VersionInfo.ProductVersion",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        return int(out.split(".")[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.debug("Could not detect browser version for %s", browser, exc_info=True)
        return None


def _cloak_headless_ua(driver) -> None:
    """Strip ``HeadlessChrome`` from the User-Agent.

    Headless Chrome/Edge advertise ``HeadlessChrome/<ver>`` in the UA —
    an instant bot-detection tell that triggers Cloudflare captchas on
    the seller portal. undetected-chromedriver 3.5.5 *can't* cloak it for
    Chrome 149 (it logs "could not detect version_main" and skips its
    version-specific UA fixup), so we override the UA ourselves via CDP.
    No-op when the UA is already clean (headed runs). Best-effort —
    failure here just leaves the original UA, it never aborts a launch.
    """
    try:
        ua = driver.execute_script("return navigator.userAgent") or ""
    except Exception:
        logger.debug("Could not read UA for cloaking", exc_info=True)
        return
    if "Headless" not in ua:
        return
    fixed = ua.replace("HeadlessChrome", "Chrome")
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": fixed})
        logger.info("Cloaked headless UA → %s", fixed)
    except Exception:
        logger.warning("Could not override headless UA", exc_info=True)


def _build_driver(browser: Path, *, profile_dir: Path, download_dir: Path, headless: bool = False):
    """Construct a WebDriver pointed at ``browser``.

    Two branches:

    - **Chrome** → ``undetected_chromedriver`` (uc.Chrome). UC ships a
      patched chromedriver that suppresses the standard automation
      fingerprints (``cdc_*`` window properties, ``--enable-automation``
      switch, etc.) which Cloudflare/PerimeterX gate on. Best stealth
      we have off-the-shelf.
    - **Edge** → plain ``selenium.webdriver.Edge``. UC explicitly
      rejects Edge's version string (`unrecognized Chrome version:
      Edg/...`), so we fall back to Selenium with manual stealth tweaks
      (`--disable-blink-features=AutomationControlled`,
      `excludeSwitches`, runtime ``navigator.webdriver`` patch). Less
      robust against bot detection but works for the cookie-injection
      flow where Selenium doesn't touch the login form.
    """
    # Self-heal: a profile already opened by another browser process (e.g.
    # the detached login window, or a zombie from a prior run) makes this
    # launch die with "not connected to DevTools". Evict any holder first.
    ensure_profile_free(profile_dir)

    is_edge = "msedge" in browser.name.lower()

    download_prefs = {
        "download.default_directory": str(download_dir.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    }

    if is_edge:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options as EdgeOptions

        options = EdgeOptions()
        options.binary_location = str(browser)
        options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("prefs", download_prefs)
        driver = webdriver.Edge(options=options)
        # Manual stealth patch — UC does this internally for Chrome.
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": (
                        "Object.defineProperty(navigator, 'webdriver', "
                        "{get: () => undefined});"
                    )
                },
            )
        except Exception:
            logger.debug("Could not apply navigator.webdriver patch", exc_info=True)
        if headless:
            _cloak_headless_ua(driver)
        return driver

    # Chrome path — undetected-chromedriver.
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.binary_location = str(browser)
    options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    # UC handles the automation-flag scrubbing; we still set download
    # prefs the same way.
    options.add_experimental_option("prefs", download_prefs)
    # Tell UC the real Chrome major version — it can't auto-detect 149 and
    # otherwise assumes 108, risking a version-mismatched driver.
    uc_kwargs: dict = {"options": options}
    version_main = _chrome_major_version(browser)
    if version_main:
        uc_kwargs["version_main"] = version_main
    driver = uc.Chrome(**uc_kwargs)
    # UC 3.5.5 can't cloak the headless UA for Chrome 149 — do it ourselves.
    if headless:
        _cloak_headless_ua(driver)
    return driver


def download_pricing_csv(
    *,
    target_path: Path = PRICING_TARGET,
    profile_dir: Path = PROFILE_DIR,
    download_dir: Path = DOWNLOAD_DIR,
    archive_dir: Path = ARCHIVE_DIR,
    download_timeout_sec: float | None = None,
    login_wait_sec: float | None = None,
    headless: bool = False,
) -> Path:
    """Open the seller portal in a real browser, click "Export From
    Live", and slot the resulting CSV into ``target_path``.

    Blocking — typically 10–20 seconds when already logged in; up to
    ``login_wait_sec`` if anything human is required (login form,
    Cloudflare captcha, MFA prompt). All of those share a single
    budget — there's only one wait, not a 30s pre-check + a 15min
    login wait stacked together.

    Defaults for the two timeouts come from
    ``settings.tcgplayer_portal_login_wait_sec`` /
    ``settings.tcgplayer_portal_download_timeout_sec`` (env-tunable,
    see .env.example). Pass explicit values for tests.

    Returns the final ``target_path``. Raises a ``PortalDownloadError``
    subclass on any specific failure mode.
    """
    from selenium.common.exceptions import (
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    from app.config import settings

    if login_wait_sec is None:
        login_wait_sec = float(settings.tcgplayer_portal_login_wait_sec)
    if download_timeout_sec is None:
        download_timeout_sec = float(settings.tcgplayer_portal_download_timeout_sec)

    # Self-healing pre-flight: if the stored session was sealed to a
    # different machine (e.g. data/ copied from another computer), detect
    # it now, clear the dead backup, and quarantine the foreign Chrome
    # profile so a fresh login starts clean. Headless (scheduler) runs
    # fast-fail here instead of burning the whole login-wait timeout on a
    # session that can never authenticate unattended.
    from app.sync.tcgplayer.auth_health import AuthHealth, ensure_healthy

    auth = ensure_healthy(profile_dir=profile_dir)
    if headless and auth.health is not AuthHealth.OK:
        raise LoginRequiredError(
            f"TCGPlayer auth is not usable on this machine ({auth.reason}). "
            "Open the sync page and click 'Sign in to TCGPlayer' to "
            "re-authenticate; headless scheduler pulls resume automatically "
            "once a fresh session is captured."
        )

    browser = find_browser_executable()
    if browser is None:
        raise BrowserNotFoundError(
            "No Chrome or Edge install found in standard locations. "
            "Install one or override _KNOWN_BROWSERS."
        )

    download_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    files_before = list(download_dir.glob("*.csv"))

    logger.info("Launching %s with profile=%s headless=%s", browser, profile_dir, headless)
    # Hold the browser lock for the whole session so a concurrent snag
    # (fired by the auth_status poll every few seconds) can't launch a
    # competing Chrome that evicts this window mid-download.
    _BROWSER_LOCK.acquire()
    driver = None
    try:
        driver = _build_driver(
            browser, profile_dir=profile_dir, download_dir=download_dir, headless=headless
        )
        # Cookie injection — load any user-pasted cookies (including the
        # auth ticket) BEFORE navigating to the protected admin path.
        # Selenium requires the driver to be on the cookie's target
        # domain before add_cookie can set it, so we hit the bare
        # domain first, set every parsed cookie, then go to /admin/pricing.
        _inject_stored_cookies(driver)

        driver.get(PRICING_URL)

        # One long wait for the Export button to appear. Whatever's
        # between us and that button — login form, Cloudflare captcha,
        # interstitial — is yours to navigate at human pace inside this
        # window. Previous code gated on a 30s "are we on login or
        # pricing yet?" check that fired before the user could even see
        # a Cloudflare challenge.
        logger.info(
            "Waiting up to %.0fs for the Export button (covers login "
            "form, Cloudflare check, anything else in between)",
            login_wait_sec,
        )
        try:
            WebDriverWait(driver, login_wait_sec).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, EXPORT_BUTTON_CSS)
                )
            )
        except TimeoutException as e:
            raise LoginRequiredError(
                f"Export button never appeared within {login_wait_sec:.0f}s. "
                "Did you finish logging in / pass the Cloudflare check? "
                "Try again, or pre-authenticate by opening the profile dir "
                "in Edge: msedge.exe --user-data-dir=data\\chrome_profile"
            ) from e

        # Already present — wait briefly for it to be clickable
        # (Knockout binding may still be settling).
        try:
            button = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, EXPORT_BUTTON_CSS))
            )
        except TimeoutException as e:
            raise PortalLayoutError(
                f"Export button found but never became clickable "
                f"({EXPORT_BUTTON_CSS!r}) — TCGPlayer may have changed "
                "the layout."
            ) from e

        # Reaching the Export button means auth succeeded. Capture the
        # live session's cookies NOW so future (headless) pulls inject
        # them and skip the login entirely — this is the reliable save
        # point, not a separate snag that races Chrome's cookie flush.
        _persist_driver_cookies(driver)

        button.click()
        logger.info("Clicked Export From Live; waiting for CSV download")

        new_csv = _await_new_csv(
            download_dir,
            files_before=files_before,
            timeout_sec=download_timeout_sec,
        )
        logger.info("CSV landed: %s (%d bytes)", new_csv, new_csv.stat().st_size)

        # Archive + replace BEFORE driver.quit(): Edge cleans up its
        # configured download directory on exit, so the source file
        # vanishes between "we just polled it" and any later access.
        # Moving it out of download_dir while Edge is still alive
        # locks the file in place.
        final_path = archive_and_replace(
            source=new_csv,
            target=target_path,
            archive_dir=archive_dir,
            keep=5,
        )
        logger.info("CSV moved into place: %s", final_path)
    except WebDriverException:
        raise
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            logger.warning("driver.quit() raised", exc_info=True)
        _BROWSER_LOCK.release()

    return final_path


def _find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _read_cookies_via_cdp(port: int) -> dict[str, str]:
    """Read the live browser's cookies (incl. in-memory session cookies)
    over the DevTools protocol — ``Storage.getCookies`` at the browser
    target. This touches no page and runs no page script, so it's
    invisible to the login page's bot detection. Returns ``{name: value}``
    for tcgplayer cookies, or ``{}`` if the endpoint isn't ready yet."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=3
        ) as r:
            ws_url = json.loads(r.read().decode()).get("webSocketDebuggerUrl")
    except Exception:
        return {}
    if not ws_url:
        return {}

    try:
        from websocket import create_connection

        ws = create_connection(ws_url, timeout=5)
    except Exception:
        logger.debug("CDP websocket connect failed on port %d", port, exc_info=True)
        return {}
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        for _ in range(30):
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                cookies = (msg.get("result") or {}).get("cookies") or []
                return _tcg_cookie_map(cookies)
        return {}
    except Exception:
        logger.debug("CDP getCookies failed", exc_info=True)
        return {}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def login_and_capture(
    *,
    profile_dir: Path = PROFILE_DIR,
    login_wait_sec: float | None = None,
    poll_interval: float = 3.0,
) -> bool:
    """Open a **plain** Chrome login window and capture the live session
    cookies over DevTools the moment the auth ticket appears.

    Why this shape: ``TCGAuthTicket_Production`` is a *session* cookie
    (in browser memory, never written to disk), so it can only be read
    from the live browser — but driving that browser with Selenium trips
    the login captcha. So we launch an ordinary, non-automated Chrome
    (captcha behaves normally for the human) with a remote-debugging port,
    and read its cookies via ``Storage.getCookies`` — a browser-level CDP
    call that runs no page script and is invisible to the page. Best of
    both: human-passable login + reliable session-cookie capture.

    Runs on a background thread (the route returns immediately) and holds
    the browser lock for its duration. Returns True iff the auth ticket
    was captured and persisted.
    """
    from app.config import settings
    from app.sync.tcgplayer.portal_auth import AUTH_COOKIE_NAME, launch_login_window

    if login_wait_sec is None:
        login_wait_sec = float(settings.tcgplayer_portal_login_wait_sec)

    browser = find_browser_executable()
    if browser is None:
        logger.warning("login_and_capture: no Chrome/Edge found")
        return False

    _BROWSER_LOCK.acquire()
    try:
        # Clear any stale holder so the login window owns the profile alone.
        ensure_profile_free(profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        port = _find_free_port()
        launch_login_window(
            chrome_binary=browser,
            profile_dir=profile_dir,
            remote_debugging_port=port,
        )
        logger.info(
            "login_and_capture: plain login window up (CDP port %d); polling up "
            "to %.0fs for the auth ticket",
            port,
            login_wait_sec,
        )
        deadline = time.monotonic() + login_wait_sec
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            cookies = _read_cookies_via_cdp(port)
            if AUTH_COOKIE_NAME in cookies:
                n = _save_tcg_cookies(cookies)
                logger.info("login_and_capture: captured auth ticket (%d cookies)", n)
                return n > 0
        logger.warning(
            "login_and_capture: auth ticket not seen within %.0fs", login_wait_sec
        )
        return False
    except Exception:
        logger.warning("login_and_capture failed", exc_info=True)
        return False
    finally:
        # Close the login window cleanly and free the profile.
        try:
            ensure_profile_free(profile_dir)
        except Exception:
            logger.debug("cleanup ensure_profile_free failed", exc_info=True)
        _BROWSER_LOCK.release()
