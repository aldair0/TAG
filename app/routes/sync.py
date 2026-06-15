from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OutboundChange, SyncRun
from app.db.models.channel_listing import Channel
from app.db.session import SessionLocal, get_session
from app.paths import templates_dir
from app.scheduler import coordinator, scheduler_is_alive
from app.settings_store import (
    get_secret_setting,
    get_setting,
    set_secret_setting,
    set_setting,
)
from app.sync.ebay import (
    LoggingMockEbayClient,
    run_ebay_outbound,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))


# ---- Sync history ----
@router.get("/", response_class=HTMLResponse)
def sync_index(
    request: Request,
    session: Session = Depends(get_session),
    portal_ok: str = "",
    portal_error: str = "",
    cookie_saved: str = "",
    cookie_cleared: str = "",
    login_opened: str = "",
    login_error: str = "",
) -> HTMLResponse:
    runs = session.execute(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(50)
    ).scalars().all()

    pending_counts = {
        row.channel: row.n
        for row in session.execute(
            select(
                OutboundChange.channel.label("channel"),
                func.count().label("n"),
            )
            .where(OutboundChange.completed_at.is_(None))
            .group_by(OutboundChange.channel)
        ).all()
    }

    # TCGPlayer auto-sync status panel
    auto_sync = get_setting(session, "tcgplayer_auto_sync", default="on")

    last_success = session.execute(
        select(SyncRun)
        .where(
            SyncRun.worker == "tcgplayer",
            SyncRun.error.is_(None),
            SyncRun.ended_at.is_not(None),
        )
        .order_by(SyncRun.ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    stale_threshold = timedelta(minutes=2 * settings.tcgplayer_sync_interval_min)
    now_utc = datetime.now(timezone.utc)
    tz = ZoneInfo(settings.store_timezone)
    last_success_local: str | None = None
    if last_success and last_success.ended_at:
        last_ended = last_success.ended_at
        if last_ended.tzinfo is None:
            last_ended = last_ended.replace(tzinfo=timezone.utc)
        is_stale = (now_utc - last_ended) > stale_threshold
        last_success_local = last_ended.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    else:
        is_stale = True  # no successful run yet

    coord_state = coordinator().state()

    return templates.TemplateResponse(
        request,
        "admin/sync.html",
        {
            "title": "Sync",
            "phase": "scheduler",
            "runs": runs,
            "fixture_path": _default_fixture_path(),
            "pending_counts": pending_counts,
            "auto_sync": auto_sync,
            "last_success": last_success,
            "last_success_local": last_success_local,
            "is_stale": is_stale,
            "coord_state": coord_state,
            "interval_min": settings.tcgplayer_sync_interval_min,
            "store_open_time": settings.store_open_time,
            "store_close_time": settings.store_close_time,
            "store_timezone": settings.store_timezone,
            "portal_ok": portal_ok,
            "portal_error": portal_error,
            "portal_cookies_text": get_secret_setting(
                session, "tcgplayer_portal_cookies", default=""
            ) or "",
            "cookie_saved": cookie_saved,
            "cookie_cleared": cookie_cleared,
            "login_opened": login_opened,
            "login_error": login_error,
            "scheduler_alive": scheduler_is_alive(),
        },
    )


@router.post("/auto/toggle", response_class=HTMLResponse)
def toggle_auto_sync(session: Session = Depends(get_session)) -> RedirectResponse:
    current = get_setting(session, "tcgplayer_auto_sync", default="on")
    set_setting(
        session, "tcgplayer_auto_sync", "off" if current == "on" else "on"
    )
    session.commit()
    return RedirectResponse(url="/admin/sync/", status_code=303)


# ---- Outbound queue list ----
@router.get("/outbound", response_class=HTMLResponse)
def outbound_index(
    request: Request,
    state: str = "all",  # all | pending | done | error
    channel: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(OutboundChange).order_by(OutboundChange.enqueued_at.desc()).limit(200)
    if state == "pending":
        stmt = stmt.where(OutboundChange.completed_at.is_(None), OutboundChange.last_error.is_(None))
    elif state == "done":
        stmt = stmt.where(OutboundChange.completed_at.is_not(None))
    elif state == "error":
        stmt = stmt.where(OutboundChange.last_error.is_not(None))
    if channel:
        stmt = stmt.where(OutboundChange.channel == channel)

    rows = session.execute(stmt).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/outbound.html",
        {
            "title": "Outbound queue",
            "phase": "2 — outbound (mock)",
            "rows": rows,
            "state": state,
            "channel": channel,
        },
    )


# ---- Outbound queue bulk actions ----
@router.post("/outbound/clear_pending", response_class=HTMLResponse)
def outbound_clear_pending(
    channel: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Permanently delete all pending (incomplete, no error) outbound rows."""
    from sqlalchemy import delete as sql_delete

    stmt = sql_delete(OutboundChange).where(
        OutboundChange.completed_at.is_(None),
        OutboundChange.last_error.is_(None),
    )
    if channel:
        stmt = stmt.where(OutboundChange.channel == channel)
    session.execute(stmt)
    session.commit()
    redir = "/admin/sync/outbound?state=pending"
    if channel:
        redir += f"&channel={channel}"
    return RedirectResponse(url=redir, status_code=303)


@router.post("/outbound/retry_errors", response_class=HTMLResponse)
def outbound_retry_errors(
    channel: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Reset error state on all failed rows so they are retried next run."""
    stmt = select(OutboundChange).where(
        OutboundChange.completed_at.is_(None),
        OutboundChange.last_error.is_not(None),
    )
    if channel:
        stmt = stmt.where(OutboundChange.channel == channel)
    rows = session.execute(stmt).scalars().all()
    for row in rows:
        row.last_error = None
        row.attempts = 0
    session.commit()
    redir = "/admin/sync/outbound?state=error"
    if channel:
        redir += f"&channel={channel}"
    return RedirectResponse(url=redir, status_code=303)


# ---- Action endpoints ----
@router.post("/run", response_class=HTMLResponse)
def sync_run_tcgplayer() -> RedirectResponse:
    """Manual "Pull from TCGPlayer now" button.

    Goes through the same coordinator as the scheduled tick so the two
    triggers can never race. If a scheduled run is in flight, this click
    enqueues a single follow-up; further clicks while that's queued are
    silently dropped.
    """
    coordinator().request()
    return RedirectResponse(url="/admin/sync/", status_code=303)


@router.post("/portal_login", response_class=HTMLResponse)
def open_portal_login() -> RedirectResponse:
    """Customer-facing "Sign in to TCGPlayer" button.

    Opens a real, *Selenium-driven* Chrome window and, on a background
    thread, waits for the customer to finish logging in — then captures
    the live session cookies (incl. the in-memory ``TCGAuthTicket_Production``
    session cookie, which is never written to disk) and persists them for
    headless reuse. Returns a 303 immediately; the auth_status poll flips
    to "connected" once the cookies land.

    A plain detached window can't be used here: the auth ticket is a
    session cookie, so it must be read from the same live driver the user
    authenticated in — closing a plain window would simply discard it.
    """
    import threading

    from app.sync.tcgplayer.auth_health import ensure_healthy
    from app.sync.tcgplayer.portal_downloader import (
        PROFILE_DIR,
        find_browser_executable,
        login_and_capture,
    )

    browser = find_browser_executable()
    if browser is None:
        return RedirectResponse(
            url="/admin/sync/?login_error=no_browser", status_code=303
        )

    # Self-heal before opening the window: if the profile/backup came from
    # another machine, quarantine + clear them now so the user logs into a
    # clean profile keyed to THIS machine.
    ensure_healthy(profile_dir=PROFILE_DIR)

    # Drive login + live cookie capture on a background thread so the HTTP
    # request returns at once. login_and_capture holds the browser lock for
    # its duration, so the auth_status snag won't collide with it.
    threading.Thread(
        target=login_and_capture, name="tcg-login-capture", daemon=True
    ).start()
    return RedirectResponse(url="/admin/sync/?login_opened=1", status_code=303)


@router.get("/auth_status", response_class=HTMLResponse)
def auth_status_partial(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial — the status pill that the sync page polls every
    few seconds while a login window is open. Renders one of three
    states based on the cookie's presence in the profile dir AND the
    encrypted ``app_setting`` mirror."""
    from app.sync.tcgplayer.auth_health import AuthHealth, ensure_healthy
    from app.sync.tcgplayer.portal_auth import (
        AUTH_COOKIE_NAME,
        profile_has_auth_cookie,
        snag_auth_cookies,
    )
    from app.sync.tcgplayer.portal_downloader import (
        PROFILE_DIR,
        find_browser_executable,
    )

    # Self-heal on every poll: if the credentials were carried over from
    # another machine they get cleared/quarantined here, and the pill
    # shows a one-shot "from another computer" hint so the user knows why
    # they're being asked to sign in again.
    auth = ensure_healthy(profile_dir=PROFILE_DIR, session=session)
    foreign_healed = auth.healed and auth.health is not AuthHealth.OK

    in_profile = profile_has_auth_cookie(PROFILE_DIR)
    in_setting = bool(
        get_secret_setting(session, "tcgplayer_portal_cookies", default="")
    )

    stored_blob = get_secret_setting(session, "tcgplayer_portal_cookies", default="") or ""

    # Snag when the profile just got a new cookie but the setting is empty.
    # Pass the stored blob as seed so an existing valid ticket is injected
    # before navigating — TCGPlayer re-issues it into the live session.
    snagged_now = False
    if in_profile and not in_setting:
        browser = find_browser_executable()
        if browser is not None:
            cookies = snag_auth_cookies(
                chrome_binary=browser,
                profile_dir=PROFILE_DIR,
                seed_blob=stored_blob,
            )
            if cookies and AUTH_COOKIE_NAME in cookies:
                blob = "; ".join(f"{k}={v}" for k, v in cookies.items())
                set_secret_setting(session, "tcgplayer_portal_cookies", blob)
                session.commit()
                in_setting = True
                snagged_now = True

    if in_setting:
        # Encrypted setting is the actual credential used by portal_download;
        # profile presence is just a trigger for auto-snag, not the authority.
        state = "connected"
    elif in_profile:
        # Profile has cookie but snag hasn't run yet — still in progress.
        state = "snagging"
    else:
        state = "disconnected"

    return templates.TemplateResponse(
        request,
        "admin/_auth_status.html",
        {
            "state": state,
            "snagged_now": snagged_now,
            "foreign_healed": foreign_healed,
        },
    )


@router.post("/portal_cookie", response_class=HTMLResponse)
def save_portal_cookie(
    cookies_text: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Persist user-pasted TCGPlayer cookies for ``ProductImageFetcher``
    to inject before navigating to the admin pricing page. The textarea
    accepts ``;``- or newline-separated ``name=value`` pairs (DevTools
    cookie panel format).
    """
    set_secret_setting(session, "tcgplayer_portal_cookies", cookies_text.strip())
    session.commit()
    return RedirectResponse(url="/admin/sync/?cookie_saved=1", status_code=303)


@router.post("/portal_cookie/clear", response_class=HTMLResponse)
def clear_portal_cookie(
    session: Session = Depends(get_session),
) -> RedirectResponse:
    set_secret_setting(session, "tcgplayer_portal_cookies", "")
    session.commit()
    return RedirectResponse(url="/admin/sync/?cookie_cleared=1", status_code=303)


def _dump_portal_diag(reason: str, exc: BaseException) -> str:
    """Drop a traceback to ``data/diagnostics/portal_download_<ts>.log``
    so the user can find the failure detail without scrolling the
    uvicorn console. Returns the filename (not full path) for the
    redirect URL."""
    import traceback
    from datetime import datetime, timezone
    from pathlib import Path

    diag_dir = Path("data/diagnostics")
    diag_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"portal_download_{ts}.log"
    path = diag_dir / fname
    with path.open("w", encoding="utf-8") as f:
        f.write(f"reason   : {reason}\n")
        f.write(f"timestamp: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"exc type : {type(exc).__name__}\n")
        f.write(f"exc msg  : {exc}\n")
        f.write("\n--- traceback ---\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    return fname


@router.post("/portal_download", response_class=HTMLResponse)
def sync_portal_download() -> RedirectResponse:
    """Manual "Get from TCGPlayer Portal" button — drives a real Chrome/
    Edge to the seller's pricing admin, clicks "Export From Live", drops
    the new CSV into ``data/csv/tcgplayer_pricing.csv`` (rotating the
    previous file into ``_archive/``), then triggers an ingest.

    Blocks the request thread for ~10–20s on the happy path; longer if
    a fresh login is required. Errors come back as a redirect-with-flag
    so the admin page can show what went wrong instead of a 500 page.
    Every failure also drops a traceback to ``data/diagnostics/`` so
    the user has a discoverable file even if the uvicorn console
    output is gone.
    """
    from app.sync.tcgplayer.portal_downloader import (
        BrowserNotFoundError,
        DownloadTimeoutError,
        LoginRequiredError,
        PortalLayoutError,
        download_pricing_csv,
    )

    try:
        path = download_pricing_csv()
        logger.info("Portal download → %s", path)
    except BrowserNotFoundError as e:
        fname = _dump_portal_diag("no_browser", e)
        logger.exception("portal download: no browser found (log %s)", fname)
        return RedirectResponse(
            url=f"/admin/sync/?portal_error=no_browser&diag={fname}",
            status_code=303,
        )
    except LoginRequiredError as e:
        fname = _dump_portal_diag("login_required", e)
        logger.warning("portal download: login required (log %s)", fname)
        return RedirectResponse(
            url=f"/admin/sync/?portal_error=login_required&diag={fname}",
            status_code=303,
        )
    except DownloadTimeoutError as e:
        fname = _dump_portal_diag("download_timeout", e)
        logger.exception(
            "portal download: timed out waiting for CSV (log %s)", fname
        )
        return RedirectResponse(
            url=f"/admin/sync/?portal_error=download_timeout&diag={fname}",
            status_code=303,
        )
    except PortalLayoutError as e:
        fname = _dump_portal_diag("layout_changed", e)
        logger.exception("portal download: portal layout changed (log %s)", fname)
        return RedirectResponse(
            url=f"/admin/sync/?portal_error=layout_changed&diag={fname}",
            status_code=303,
        )
    except Exception as e:
        fname = _dump_portal_diag("unknown", e)
        logger.exception("portal download: unexpected failure (log %s)", fname)
        return RedirectResponse(
            url=f"/admin/sync/?portal_error=unknown&diag={fname}",
            status_code=303,
        )

    # CSV in place — kick off ingest via the coordinator (same path
    # the scheduled tick uses).
    coordinator().request()
    return RedirectResponse(
        url="/admin/sync/?portal_ok=1", status_code=303
    )


@router.post("/run_ebay", response_class=HTMLResponse)
def sync_run_ebay(session: Session = Depends(get_session)) -> RedirectResponse:
    try:
        run_ebay_outbound(session, LoggingMockEbayClient())
    except Exception:
        logger.exception("eBay outbound failed")
        # Don't leak internal exception detail to the client; it's in the logs.
        raise HTTPException(status_code=500, detail="eBay sync failed — see server logs.")
    return RedirectResponse(url="/admin/sync/", status_code=303)


def _default_fixture_path() -> Path:
    return Path("test_data/tcgplayer_fixture.csv").resolve()
