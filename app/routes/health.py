from fastapi import APIRouter

from app import __version__

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": __version__}
