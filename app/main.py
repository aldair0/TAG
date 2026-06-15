from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app import __version__
from app.config import settings
from app.paths import static_dir
from app.routes import admin, health, inventory, pos, sales, sold_online, supplies, sync
from app.routes import settings as settings_routes
from app.routes import shopify_auth
from app.inbound_email import start_email_receiver, stop_email_receiver
from app.logging_setup import configure_logging
from app.scheduler import start_scheduler, stop_scheduler
from app.sync.tcgplayer.image_paths import IMAGES_ROOT


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify the DB recovered cleanly from any prior unclean shutdown before
    # we start serving / writing to it.
    from app.db.base import integrity_check
    from app.maintenance import reap_orphan_drivers

    integrity_check()
    # Clean up any automation-driver orphans left by a previous hard-killed
    # run — safe here at startup, before the scheduler can launch a new sync.
    # Skipped when the scheduler is disabled (tests) so we never taskkill a
    # developer's running driver.
    import os

    if os.environ.get("TAG_DISABLE_SCHEDULER") != "1":
        reap_orphan_drivers()
    start_scheduler()
    start_email_receiver()
    try:
        yield
    finally:
        stop_email_receiver()
        stop_scheduler()


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


async def _origin_guard(request: Request, call_next):
    """Refuse cross-site state-changing requests (A1 drive-by defense).

    A malicious page on the staff tablet can silently POST to the app's LAN
    address — the browser sends the request even though CORS hides the
    response. For any unsafe method, if the request carries an Origin (or
    Referer) whose host doesn't match our own Host, reject it. Same-origin
    form posts match; server-side callers (e.g. the email /signal poster)
    send neither header and are allowed (they have their own token auth).
    """
    if request.method not in _SAFE_METHODS:
        from urllib.parse import urlparse

        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            host = request.headers.get("host", "")
            netloc = urlparse(source).netloc
            if netloc and host and netloc != host:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"error": "cross-origin request refused"}, status_code=403
                )
    return await call_next(request)


async def _inject_nav_counts(request: Request, call_next):
    """Inject counts used by nav badges into request state."""
    from datetime import datetime, timezone
    from app.db.models import Conflict, InventoryUnit
    from app.db.session import get_session as _get_db
    try:
        session = next(_get_db())
        request.state.open_conflict_count = session.execute(
            select(func.count()).where(Conflict.status == "open")
        ).scalar_one()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        request.state.sold_online_count = session.execute(
            select(func.count()).select_from(InventoryUnit).where(
                InventoryUnit.sold_online_until.is_not(None),
                InventoryUnit.sold_online_until > now_utc,
            )
        ).scalar_one()
        session.close()
    except Exception:
        request.state.open_conflict_count = 0
        request.state.sold_online_count = 0
    return await call_next(request)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="TAG Inventory",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.middleware("http")(_inject_nav_counts)
    # Registered after _inject_nav_counts so it runs FIRST (Starlette runs
    # middleware in reverse registration order) — reject cross-origin POSTs
    # before any handler work.
    app.middleware("http")(_origin_guard)
    app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")

    # Product image cache. Populated by ProductImageFetcher; templates
    # render <img src="/images/<set-slug>/<name-slug>__<number-slug>.jpg">.
    # Created on app start so the mount doesn't fail on a fresh checkout.
    IMAGES_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/images",
        StaticFiles(directory=str(IMAGES_ROOT)),
        name="product-images",
    )

    app.include_router(health.router, tags=["meta"])
    app.include_router(admin.router, prefix="/admin", tags=["admin"])
    app.include_router(inventory.router, prefix="/admin/inventory", tags=["admin"])
    app.include_router(sync.router, prefix="/admin/sync", tags=["admin"])
    app.include_router(sales.router, prefix="/admin/sales", tags=["admin"])
    app.include_router(supplies.router, prefix="/admin/supplies", tags=["admin"])
    app.include_router(pos.router, prefix="/pos", tags=["pos"])
    app.include_router(settings_routes.router, prefix="/admin/settings", tags=["admin"])
    app.include_router(sold_online.router, prefix="/admin/sold-online", tags=["admin"])
    app.include_router(shopify_auth.router, prefix="/auth/shopify", tags=["shopify"])

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/admin/")

    return app


app = create_app()


def run() -> None:
    """Entrypoint for the `tag-inventory` console script (and PyInstaller later).

    Serves HTTPS directly (no reverse proxy) when ``SSL_ENABLED`` (default),
    auto-generating a self-signed LAN cert on first run.
    """
    import logging

    import uvicorn

    kwargs = dict(
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
    scheme = "http"
    if settings.ssl_enabled:
        from app.tls import ensure_self_signed_cert

        certfile, keyfile = ensure_self_signed_cert(
            settings.ssl_certfile or None, settings.ssl_keyfile or None
        )
        kwargs["ssl_certfile"] = str(certfile)
        kwargs["ssl_keyfile"] = str(keyfile)
        scheme = "https"

    logging.getLogger(__name__).info(
        "Serving on %s://%s:%s", scheme, settings.app_host, settings.app_port
    )
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    run()
