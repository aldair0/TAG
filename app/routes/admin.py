from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Conflict,
    InventoryUnit,
    OutboundChange,
    Product,
    ProductKind,
    Sale,
    SyncRun,
)
from app.db.session import get_session
from app.paths import templates_dir

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))


@router.get("/", response_class=HTMLResponse)
def admin_index(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    products_total = session.execute(select(func.count()).select_from(Product)).scalar_one()
    units_total = session.execute(select(func.count()).select_from(InventoryUnit)).scalar_one()
    on_hand = session.execute(
        select(func.coalesce(func.sum(InventoryUnit.quantity_on_hand), 0))
        .join(Product, Product.id == InventoryUnit.product_id)
        .where(Product.kind != ProductKind.SUPPLY.value)
    ).scalar_one()
    sales_total = session.execute(select(func.count()).select_from(Sale)).scalar_one()
    conflicts_open = session.execute(
        select(func.count()).select_from(Conflict).where(Conflict.status == "open")
    ).scalar_one()
    sync_runs_total = session.execute(select(func.count()).select_from(SyncRun)).scalar_one()
    pending_outbound = session.execute(
        select(func.count())
        .select_from(OutboundChange)
        .where(OutboundChange.completed_at.is_(None))
    ).scalar_one()

    by_kind = {
        row.kind: row.n
        for row in session.execute(
            select(Product.kind.label("kind"), func.count().label("n")).group_by(Product.kind)
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            "title": "Admin",
            "phase": "4 — POS UI",
            "products_total": products_total,
            "units_total": units_total,
            "on_hand": on_hand,
            "sales_total": sales_total,
            "conflicts_open": conflicts_open,
            "sync_runs_total": sync_runs_total,
            "pending_outbound": pending_outbound,
            "singles_total": by_kind.get(ProductKind.SINGLE.value, 0),
            "sealed_total": by_kind.get(ProductKind.SEALED.value, 0),
            "supplies_total": by_kind.get(ProductKind.SUPPLY.value, 0),
        },
    )
