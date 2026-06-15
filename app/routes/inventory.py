from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional  # used by adjust + delete

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from fastapi.templating import Jinja2Templates

from app.db.models import (
    AdjustmentReason,
    ChannelListing,
    InventoryAdjustment,
    InventoryUnit,
    Product,
)
from app.db.session import get_session
from app.outbound import enqueue_for_qty_change
from app.paths import templates_dir

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))

logger = logging.getLogger(__name__)

PAGE_SIZE = 50

ADJUSTMENT_REASONS = [r.value for r in AdjustmentReason]


@router.get("/", response_class=HTMLResponse)
def inventory_index(
    request: Request,
    page: int = 1,
    q: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    base_stmt = (
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .options(
            joinedload(InventoryUnit.product),
            joinedload(InventoryUnit.channel_listings),
        )
        .order_by(Product.name, InventoryUnit.condition)
    )

    if q:
        like = f"%{q}%"
        base_stmt = base_stmt.where(Product.name.ilike(like) | Product.set.ilike(like))

    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = session.execute(count_stmt).scalar_one()

    rows_stmt = base_stmt.limit(PAGE_SIZE).offset(offset)
    units = session.execute(rows_stmt).unique().scalars().all()

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "admin/inventory.html",
        {
            "title": "Inventory",
            "phase": "1 — TCGPlayer ingestion",
            "units": units,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": PAGE_SIZE,
            "q": q,
        },
    )


@router.get("/search", response_class=HTMLResponse)
def inventory_search(
    request: Request,
    page: int = 1,
    q: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial — returns only the rows + pagination, no page chrome."""
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    base_stmt = (
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .options(
            joinedload(InventoryUnit.product),
            joinedload(InventoryUnit.channel_listings),
        )
        .order_by(Product.name, InventoryUnit.condition)
    )

    if q:
        like = f"%{q}%"
        base_stmt = base_stmt.where(Product.name.ilike(like) | Product.set.ilike(like))

    total = session.execute(select(func.count()).select_from(base_stmt.subquery())).scalar_one()
    units = session.execute(base_stmt.limit(PAGE_SIZE).offset(offset)).unique().scalars().all()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "admin/_inventory_rows.html",
        {
            "units": units,
            "total": total,
            "page": page,
            "pages": pages,
            "q": q,
        },
    )


# ---------------------------------------------------------------------------
# Edit existing unit
# ---------------------------------------------------------------------------

def _get_unit_or_404(session: Session, unit_id: int) -> InventoryUnit:
    unit = session.execute(
        select(InventoryUnit)
        .options(
            joinedload(InventoryUnit.product),
            joinedload(InventoryUnit.adjustments),
        )
        .where(InventoryUnit.id == unit_id)
    ).unique().scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=404)
    return unit


@router.get("/{unit_id}/edit", response_class=HTMLResponse)
def inventory_edit_form(
    request: Request,
    unit_id: int,
    error: str = "",
    created: int = 0,
    adjusted: int = 0,
    saved: int = 0,
    image_saved: int = 0,
    image_removed: int = 0,
    back_q: str = "",
    back_page: int = 0,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    unit = _get_unit_or_404(session, unit_id)
    back_url = "/admin/inventory/"
    if back_q or back_page > 1:
        parts = []
        if back_q:
            parts.append(f"q={back_q}")
        if back_page > 1:
            parts.append(f"page={back_page}")
        back_url += "?" + "&".join(parts)
    return templates.TemplateResponse(
        request,
        "admin/inventory_edit.html",
        {
            "title": f"Edit — {unit.product.name}",
            "unit": unit,
            "error": error,
            "created": created,
            "adjusted": adjusted,
            "saved": saved,
            "image_saved": image_saved,
            "image_removed": image_removed,
            "back_url": back_url,
            "adjustment_reasons": ADJUSTMENT_REASONS,
            "recent_adjustments": unit.adjustments[:10],
            "is_store_item": unit.product.tcgplayer_product_id is None,
            # pre-populated field values after a failed save
            "form_price": request.query_params.get("form_price", ""),
            "form_qty": request.query_params.get("form_qty", ""),
            "form_reserve": request.query_params.get("form_reserve", ""),
        },
    )


@router.post("/{unit_id}/edit", response_class=HTMLResponse)
def inventory_edit_save(
    request: Request,
    unit_id: int,
    unit_price: str = Form(...),
    quantity_on_hand: int = Form(...),
    reserve_quantity: int = Form(0),
    back_q: str = Form(""),
    back_page: int = Form(0),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    unit = _get_unit_or_404(session, unit_id)

    def _back(error: str) -> RedirectResponse:
        params = f"error={error}&form_price={unit_price}&form_qty={quantity_on_hand}&form_reserve={reserve_quantity}"
        if back_q:
            params += f"&back_q={back_q}"
        if back_page > 1:
            params += f"&back_page={back_page}"
        return RedirectResponse(
            url=f"/admin/inventory/{unit_id}/edit?{params}", status_code=303
        )

    try:
        price = Decimal(unit_price) if unit_price.strip() else None
        if price is not None and price < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return _back("bad_price")

    if quantity_on_hand < 0 or reserve_quantity < 0:
        return _back("bad_quantity")

    if reserve_quantity > quantity_on_hand:
        return _back("reserve_exceeds_qty")

    unit.unit_price = price
    unit.quantity_on_hand = quantity_on_hand
    unit.reserve_quantity = reserve_quantity
    session.commit()

    back_suffix = ""
    if back_q:
        back_suffix += f"&back_q={back_q}"
    if back_page > 1:
        back_suffix += f"&back_page={back_page}"
    return RedirectResponse(
        url=f"/admin/inventory/{unit_id}/edit?saved=1{back_suffix}", status_code=303
    )


# ---------------------------------------------------------------------------
# Manual product image (when TCGPlayer has none / the CDN download failed)
# ---------------------------------------------------------------------------

# Cap uploads so a stray huge file can't fill the disk; cards are small.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@router.post("/{unit_id}/image", response_class=HTMLResponse)
def inventory_upload_image(
    unit_id: int,
    image: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Save a manually-uploaded picture for this unit's product.

    Writes to the same deterministic path the TCGPlayer fetcher uses
    (``data/images/<set>/<name>__<number>.jpg``) and flips
    ``has_image`` — so the image then renders everywhere (inventory,
    POS, edit) and is served by the existing ``/images`` mount. Works
    for store items and for TCGPlayer cards whose CDN image 403'd.
    """
    unit = _get_unit_or_404(session, unit_id)
    product = unit.product

    def _back(flag: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"/admin/inventory/{unit_id}/edit?{flag}", status_code=303
        )

    if not (image.content_type or "").lower().startswith("image/"):
        return _back("error=bad_image")

    # Read with a hard cap (one extra byte tells us it overflowed).
    data = image.file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        return _back("error=empty_image")
    if len(data) > MAX_IMAGE_BYTES:
        return _back("error=image_too_large")

    from app.sync.tcgplayer.image_paths import image_local_path

    path = image_local_path(
        set_name=product.set, name=product.name, number=product.number
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        return _back("error=image_write_failed")

    product.has_image = True
    session.commit()
    return _back("image_saved=1")


@router.post("/{unit_id}/image/delete", response_class=HTMLResponse)
def inventory_delete_image(
    unit_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Remove the product's image file and clear ``has_image`` so the
    placeholder shows again (and a fresh upload / re-fetch can replace it)."""
    unit = _get_unit_or_404(session, unit_id)
    product = unit.product

    from app.sync.tcgplayer.image_paths import image_local_path

    path = image_local_path(
        set_name=product.set, name=product.name, number=product.number
    )
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not delete image file %s", path, exc_info=True)

    product.has_image = False
    session.commit()
    return RedirectResponse(
        url=f"/admin/inventory/{unit_id}/edit?image_removed=1", status_code=303
    )


# ---------------------------------------------------------------------------
# Manual adjustment
# ---------------------------------------------------------------------------

@router.post("/{unit_id}/adjust", response_class=HTMLResponse)
def inventory_adjust(
    unit_id: int,
    delta: int = Form(...),
    reason: str = Form(...),
    note: Optional[str] = Form(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    unit = _get_unit_or_404(session, unit_id)

    if delta == 0:
        return RedirectResponse(
            url=f"/admin/inventory/{unit_id}/edit?error=zero_delta", status_code=303
        )

    if reason not in ADJUSTMENT_REASONS:
        return RedirectResponse(
            url=f"/admin/inventory/{unit_id}/edit?error=bad_reason", status_code=303
        )

    new_qty = max(0, unit.quantity_on_hand + delta)

    adj = InventoryAdjustment(
        inventory_unit_id=unit.id,
        delta=delta,
        reason=reason,
        note=(note or "").strip() or None,
    )
    session.add(adj)

    unit.quantity_on_hand = new_qty
    enqueue_for_qty_change(session, unit, new_qty)
    session.commit()

    return RedirectResponse(
        url=f"/admin/inventory/{unit_id}/edit?adjusted={delta}", status_code=303
    )


# ---------------------------------------------------------------------------
# Delete store-managed item (not TCGPlayer-synced)
# ---------------------------------------------------------------------------

@router.post("/{unit_id}/delete", response_class=HTMLResponse)
def inventory_delete(
    unit_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    unit = session.execute(
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.id == unit_id)
    ).scalar_one_or_none()

    if unit is None:
        raise HTTPException(status_code=404)

    if unit.product.tcgplayer_product_id is not None:
        # TCGPlayer-managed items must be removed via TCGPlayer; refusing
        # to delete them here prevents accidental desyncs.
        raise HTTPException(status_code=403, detail="Cannot delete TCGPlayer-managed items.")

    product = unit.product
    session.delete(unit)
    # If this was the last unit on the product, remove the product too.
    session.flush()
    remaining = session.execute(
        select(func.count()).where(InventoryUnit.product_id == product.id)
    ).scalar_one()
    if remaining == 0:
        session.delete(product)

    session.commit()
    return RedirectResponse(url="/admin/inventory/?deleted=1", status_code=303)
