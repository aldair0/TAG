from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.health import collect_health

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    """Structured subsystem health. Returns 503 only when the DB is down so an
    external watchdog can bounce the service; degraded states stay 200."""
    health = collect_health()
    code = 503 if health.get("status") == "down" else 200
    return JSONResponse(health, status_code=code)


@router.get("/healthz/simple")
def healthz_simple() -> dict:
    """Back-compat tiny check (some smoke tests assert on this shape)."""
    return {"status": "ok", "version": __version__}
