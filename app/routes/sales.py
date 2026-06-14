from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.db.models import Conflict, InventoryUnit, Sale, SaleLine
from app.db.session import get_session
from app.paths import templates_dir

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))

PAGE_SIZE = 50


@router.get("/", response_class=HTMLResponse)
def sales_index(
    request: Request,
    page: int = 1,
    channel: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(weeks=2)

    base = (
        select(Sale)
        .options(joinedload(Sale.lines).joinedload(SaleLine.inventory_unit))
        .where(Sale.occurred_at >= cutoff)
        .order_by(Sale.occurred_at.desc())
    )
    if channel:
        base = base.where(Sale.channel == channel)

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = (
        session.execute(base.limit(PAGE_SIZE).offset(offset))
        .unique()
        .scalars()
        .all()
    )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    by_channel = {
        row.channel: row.n
        for row in session.execute(
            select(Sale.channel.label("channel"), func.count().label("n"))
            .where(Sale.occurred_at >= cutoff)
            .group_by(Sale.channel)
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "admin/sales.html",
        {
            "title": "Sales",
            "rows": rows,
            "page": page,
            "pages": pages,
            "total": total,
            "channel": channel,
            "by_channel": by_channel,
            "cutoff_date": cutoff.strftime("%b %-d"),
        },
    )


@router.get("/export/sales.csv")
def export_sales_csv(
    channel: str = "",
    session: Session = Depends(get_session),
) -> StreamingResponse:
    stmt = (
        select(Sale)
        .options(joinedload(Sale.lines))
        .order_by(Sale.occurred_at.desc())
    )
    if channel:
        stmt = stmt.where(Sale.channel == channel)
    sales = session.execute(stmt).unique().scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["sale_id", "occurred_at", "channel", "payment_method",
                     "external_order_id", "subtotal", "tax", "card_surcharge",
                     "total", "item", "qty", "unit_price"])
    for sale in sales:
        for line in sale.lines:
            writer.writerow([
                sale.id,
                sale.occurred_at.strftime("%Y-%m-%d %H:%M:%S") if sale.occurred_at else "",
                sale.channel,
                sale.payment_method or "",
                sale.external_order_id or "",
                sale.subtotal or "",
                sale.tax or "",
                sale.card_surcharge or "",
                sale.total or "",
                line.title_at_sale,
                line.quantity_sold,
                line.unit_price,
            ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sales.csv"},
    )


@router.get("/conflicts", response_class=HTMLResponse)
def conflicts_index(
    request: Request,
    status: str = "open",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = (
        select(Conflict)
        .options(joinedload(Conflict.inventory_unit))
        .order_by(Conflict.created_at.desc())
        .limit(200)
    )
    if status in ("open", "resolved", "ignored"):
        stmt = stmt.where(Conflict.status == status)

    rows = session.execute(stmt).unique().scalars().all()
    counts = {
        row.status: row.n
        for row in session.execute(
            select(Conflict.status.label("status"), func.count().label("n")).group_by(
                Conflict.status
            )
        ).all()
    }

    return templates.TemplateResponse(
        request,
        "admin/conflicts.html",
        {
            "title": "Conflicts",
            "phase": "3 — cross-channel sale sync",
            "rows": rows,
            "status": status,
            "counts": counts,
        },
    )


@router.post("/conflicts/{conflict_id}/resolve", response_class=HTMLResponse)
def conflict_resolve(
    conflict_id: int,
    resolved_by: str = Form(""),
    resolved_notes: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    conflict = session.get(Conflict, conflict_id)
    if conflict is None:
        raise HTTPException(status_code=404)
    conflict.status = "resolved"
    conflict.resolved_at = datetime.now(timezone.utc)
    conflict.resolved_by = resolved_by.strip() or None
    conflict.resolved_notes = resolved_notes.strip() or None
    session.commit()
    return RedirectResponse(url="/admin/sales/conflicts?status=open", status_code=303)


@router.post("/conflicts/{conflict_id}/ignore", response_class=HTMLResponse)
def conflict_ignore(
    conflict_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    conflict = session.get(Conflict, conflict_id)
    if conflict is None:
        raise HTTPException(status_code=404)
    conflict.status = "ignored"
    conflict.resolved_at = datetime.now(timezone.utc)
    session.commit()
    return RedirectResponse(url="/admin/sales/conflicts?status=open", status_code=303)
