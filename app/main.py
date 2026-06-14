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
from app.scheduler import start_scheduler, stop_scheduler
from app.sync.tcgplayer.image_paths import IMAGES_ROOT


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


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
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="TAG Inventory",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.middleware("http")(_inject_nav_counts)
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
    """Entrypoint for the `tag-inventory` console script (and PyInstaller later)."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
