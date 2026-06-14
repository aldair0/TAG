"""Store Items — non-TCGPlayer inventory managed directly by the store.

Covers supplies (sleeves, deck boxes, 3D prints…), sealed products
(booster boxes, decks…), and any singles the store tracks locally without
a TCGPlayer listing. Nothing here is ever pushed to TCGPlayer or eBay
unless explicitly marked as online-listable.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db.models import InventoryUnit, Product, ProductKind
from app.db.session import get_session
from app.outbound import enqueue_for_new_unit
from app.paths import templates_dir

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))

SUPPLY_CATEGORIES = [
    "Sleeves",
    "Deck Box",
    "Playmat",
    "Dice",
    "Binder",
    "Card Holder",
    "3D Print",
    "Merchandise",
    "Other",
]

SEALED_SUBTYPES = [
    "Booster Box",
    "Booster Pack",
    "Pre-Release Pack",
    "Commander Deck",
    "Starter Deck",
    "Bundle",
    "Gift Set",
    "Collector Box",
    "Other",
]

CONDITIONS = ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"]

UNLIMITED_QTY = 1


def _all_store_items(session: Session) -> list[InventoryUnit]:
    """All inventory units that are NOT managed by TCGPlayer."""
    return (
        session.execute(
            select(InventoryUnit)
            .join(Product, Product.id == InventoryUnit.product_id)
            .options(joinedload(InventoryUnit.product), joinedload(InventoryUnit.channel_listings))
            .where(Product.tcgplayer_product_id.is_(None))
            .order_by(Product.kind, Product.name)
        )
        .unique()
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def store_items_index(
    request: Request,
    session: Session = Depends(get_session),
    error: str = "",
    created: int = 0,
    updated: int = 0,
    deleted: int = 0,
) -> HTMLResponse:
    rows = _all_store_items(session)
    return templates.TemplateResponse(
        request,
        "admin/store_items.html",
        {
            "title": "Store Items",
            "rows": rows,
            "error": error,
            "created": created,
            "updated": updated,
            "deleted": deleted,
            "unlimited_qty": UNLIMITED_QTY,
        },
    )


# ---------------------------------------------------------------------------
# Create — full form (all kinds)
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
def store_item_new_form(
    request: Request,
    error: str = "",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/store_item_new.html",
        {
            "title": "Add Store Item",
            "error": error,
            "supply_categories": SUPPLY_CATEGORIES,
            "sealed_subtypes": SEALED_SUBTYPES,
            "conditions": CONDITIONS,
        },
    )


@router.post("/new", response_class=HTMLResponse)
def store_item_new_save(
    kind: str = Form(...),
    name: str = Form(...),
    unit_price: str = Form(""),
    quantity: int = Form(0),
    condition: Optional[str] = Form(None),
    set_name: Optional[str] = Form(None),
    supply_category: Optional[str] = Form(None),
    sealed_subtype: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    is_online_listable: bool = Form(False),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/supplies/new?error=name_required", status_code=303)

    if kind not in (ProductKind.SINGLE.value, ProductKind.SEALED.value, ProductKind.SUPPLY.value):
        return RedirectResponse(url="/admin/supplies/new?error=bad_kind", status_code=303)

    try:
        price = Decimal(unit_price) if unit_price.strip() else None
        if price is not None and price < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return RedirectResponse(url="/admin/supplies/new?error=bad_price", status_code=303)

    if quantity < 0:
        return RedirectResponse(url="/admin/supplies/new?error=bad_quantity", status_code=303)

    if kind == ProductKind.SUPPLY.value:
        is_online_listable = False

    product = Product(
        kind=kind,
        name=name,
        set=set_name.strip() if set_name and set_name.strip() else None,
        supply_category=supply_category.strip() if supply_category and supply_category.strip() else None,
        sealed_subtype=sealed_subtype.strip() if sealed_subtype and sealed_subtype.strip() else None,
        description=(description or "").strip() or None,
        is_online_listable=is_online_listable,
        language=None,
        has_image=False,
    )
    session.add(product)
    session.flush()

    unit = InventoryUnit(
        product_id=product.id,
        condition=condition if condition and condition.strip() else None,
        quantity_on_hand=quantity,
        unit_price=price,
    )
    session.add(unit)
    session.flush()

    enqueue_for_new_unit(session, unit)
    session.commit()

    return RedirectResponse(url=f"/admin/supplies/?created={product.id}", status_code=303)


# ---------------------------------------------------------------------------
# Edit supply (kind=supply only — full product fields)
# ---------------------------------------------------------------------------

@router.get("/{unit_id}/edit", response_class=HTMLResponse)
def supply_edit_form(
    request: Request,
    unit_id: int,
    error: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    unit = session.execute(
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.id == unit_id)
    ).scalar_one_or_none()
    if unit is None or unit.product.kind != ProductKind.SUPPLY.value:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "admin/supply_edit.html",
        {
            "title": f"Edit {unit.product.name}",
            "unit": unit,
            "categories": SUPPLY_CATEGORIES,
            "unlimited_qty": UNLIMITED_QTY,
            "error": error,
        },
    )


@router.post("/{unit_id}/edit", response_class=HTMLResponse)
def supply_edit_save(
    unit_id: int,
    name: str = Form(...),
    supply_category: str = Form(...),
    unit_price: str = Form(...),
    quantity: int = Form(0),
    unlimited: bool = Form(False),
    description: Optional[str] = Form(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    unit = session.execute(
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.id == unit_id)
    ).scalar_one_or_none()
    if unit is None or unit.product.kind != ProductKind.SUPPLY.value:
        raise HTTPException(status_code=404)

    name = name.strip()
    if not name:
        return RedirectResponse(url=f"/admin/supplies/{unit_id}/edit?error=name_required", status_code=303)
    try:
        price = Decimal(unit_price)
        if price < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return RedirectResponse(url=f"/admin/supplies/{unit_id}/edit?error=bad_price", status_code=303)

    if unlimited:
        quantity = UNLIMITED_QTY
    if quantity < 0:
        return RedirectResponse(url=f"/admin/supplies/{unit_id}/edit?error=bad_quantity", status_code=303)

    unit.product.name = name
    unit.product.supply_category = supply_category.strip()
    unit.product.description = (description or "").strip() or None
    unit.unit_price = price
    unit.quantity_on_hand = quantity
    session.commit()

    return RedirectResponse(url="/admin/supplies/?updated=1", status_code=303)
