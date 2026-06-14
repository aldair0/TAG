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

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

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
    return uc.Chrome(options=options)


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
    driver = _build_driver(
        browser, profile_dir=profile_dir, download_dir=download_dir, headless=headless
    )
    try:
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
            driver.quit()
        except Exception:
            logger.warning("driver.quit() raised", exc_info=True)

    return final_path
