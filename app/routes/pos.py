"""POS UI routes — tablet-friendly browse + cart for walk-in sales.

The cart lives in a single cookie (see ``app.pos.cart``). Routes that
mutate it write a fresh cookie on the response. Checkout records a
local Sale via ``record_sale`` — Shopify is used only as a card payment
terminal and receives no inventory data.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Iterable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db.models import Channel, InventoryUnit, Product, ProductKind
from app.db.models.sale import Sale, SaleLine
from app.db.session import get_session
from app.paths import templates_dir
from app.pos.cart import CART_COOKIE, Cart, decode_cart, encode_cart
from app.routes.settings import get_pos_rates
from app.pos.totals import LineSnapshot, compute_totals
from app.sales import SaleLineInput, record_sale
from app.sync.tcgplayer.qty_updater import dispatch_after_sale as _tcg_dispatch

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))

PAGE_SIZE = 24



def _read_cart(request: Request) -> Cart:
    return decode_cart(request.cookies.get(CART_COOKIE))


def _persist_cart(response: Response, cart: Cart) -> None:
    response.set_cookie(
        CART_COOKIE,
        encode_cart(cart),
        max_age=60 * 60 * 8,  # 8h cashier shift
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_cart_cookie(response: Response) -> None:
    response.delete_cookie(CART_COOKIE, path="/")


def _sold_out_items(session: Session, cart: Cart) -> list[tuple[int, str]]:
    """Return (unit_id, title) for every non-supply cart item whose current
    quantity_on_hand is less than the quantity in the cart.

    Called before checkout to catch items that sold online while sitting in
    the cart cookie.  Supplies are excluded — they have no tracked quantity.
    """
    if cart.is_empty():
        return []
    units = _load_units(session, (it.inventory_unit_id for it in cart.items))
    result = []
    for it in cart.items:
        unit = units.get(it.inventory_unit_id)
        if unit is None:
            continue
        if unit.product.kind == ProductKind.SUPPLY.value:
            continue
        if unit.is_sold_online or unit.quantity_on_hand < it.quantity:
            result.append((unit.id, _line_title(unit)))
    return result


def _load_units(session: Session, unit_ids: Iterable[int]) -> dict[int, InventoryUnit]:
    ids = list(unit_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.id.in_(ids))
    ).unique().scalars().all()
    return {u.id: u for u in rows}


def _line_title(unit: InventoryUnit) -> str:
    p = unit.product
    parts = [p.name]
    if unit.condition:
        parts.append(f"({unit.condition})")
    if p.set:
        parts.append(f"— {p.set}")
    return " ".join(parts)


_CUSTOM_ITEM_UNIT_ID = 394  # created by create_custom_item.py


def _cart_lines(
    session: Session, cart: Cart
) -> tuple[list[LineSnapshot], set[int]]:
    """Return (snapshots, deleted_ids) — deleted_ids are cart unit IDs no longer in DB."""
    if cart.is_empty():
        return [], set()
    units = _load_units(session, (it.inventory_unit_id for it in cart.items))
    snaps: list[LineSnapshot] = []
    deleted_ids: set[int] = set()
    for it in cart.items:
        unit = units.get(it.inventory_unit_id)
        if unit is None:
            deleted_ids.add(it.inventory_unit_id)
            continue
        if it.override_price is not None:
            price = Decimal(it.override_price)
        else:
            price = unit.unit_price or Decimal("0.00")
        snaps.append(
            LineSnapshot(
                inventory_unit_id=unit.id,
                title=_line_title(unit),
                unit_price=price,
                quantity=it.quantity,
                override_price=it.override_price,
            )
        )
    return snaps, deleted_ids


# ---- Browse / search ----

def _browse_query(session: Session, *, q: str, kind: str, page: int):
    """Shared between full-page browse and the HTMX partial."""
    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE
    base = (
        select(InventoryUnit)
        .join(Product, Product.id == InventoryUnit.product_id)
        .options(joinedload(InventoryUnit.product))
        .where(InventoryUnit.quantity_on_hand > 0)
        .order_by(Product.name, InventoryUnit.condition)
    )
    if q:
        like = f"%{q}%"
        base = base.where(
            Product.name.ilike(like)
            | Product.set.ilike(like)
            | Product.supply_category.ilike(like)
        )
    if kind in ("single", "sealed", "supply"):
        base = base.where(Product.kind == kind)
    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    units = session.execute(base.limit(PAGE_SIZE).offset(offset)).unique().scalars().all()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return {"units": units, "total": total, "page": page, "pages": pages, "q": q, "kind": kind}


@router.get("/", response_class=HTMLResponse)
def pos_index(
    request: Request,
    q: str = "",
    page: int = 1,
    kind: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cart = _read_cart(request)
    ctx = _browse_query(session, q=q, kind=kind, page=page)
    return templates.TemplateResponse(
        request,
        "pos/index.html",
        {
            "title": "Point of Sale",
            "phase": "5 — Shopify",
            "cart_count": sum(it.quantity for it in cart.items),
            "custom_item_unit_id": _CUSTOM_ITEM_UNIT_ID,
            "custom_error": request.query_params.get("custom_error", ""),
            **ctx,
        },
    )


@router.get("/search", response_class=HTMLResponse)
def pos_search(
    request: Request,
    q: str = "",
    page: int = 1,
    kind: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial — returns only the product grid, no page chrome."""
    ctx = _browse_query(session, q=q, kind=kind, page=page)
    return templates.TemplateResponse(request, "pos/_grid.html", ctx)


# ---- Cart mutation ----
@router.post("/cart/add", response_class=HTMLResponse)
def cart_add(
    request: Request,
    inventory_unit_id: int = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    unit = session.get(InventoryUnit, inventory_unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unknown inventory unit")
    session.refresh(unit, ["product"])
    if unit.is_sold_online:
        resp = RedirectResponse(
            url=request.headers.get("referer") or "/pos/", status_code=303
        )
        return resp
    cart = _read_cart(request)
    current = cart.quantity_for(inventory_unit_id)
    is_supply = unit.product.kind == ProductKind.SUPPLY.value
    if not is_supply and current + 1 > unit.quantity_on_hand:
        # Don't blow up — bounce the cashier back with no change. The
        # browse card already shows qty available.
        resp = RedirectResponse(
            url=request.headers.get("referer") or "/pos/", status_code=303
        )
        return resp
    cart.add(inventory_unit_id, 1)
    resp = RedirectResponse(
        url=request.headers.get("referer") or "/pos/", status_code=303
    )
    _persist_cart(resp, cart)
    return resp


@router.post("/cart/set", response_class=HTMLResponse)
def cart_set(
    request: Request,
    inventory_unit_id: int = Form(...),
    quantity: int = Form(...),
    override_price: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    price = override_price.strip() or None
    cart = _read_cart(request)
    if quantity > 0 and price is None:
        unit = session.get(InventoryUnit, inventory_unit_id)
        if unit is not None:
            session.refresh(unit, ["product"])
            if unit.product.kind != ProductKind.SUPPLY.value:
                quantity = min(quantity, unit.quantity_on_hand)
    cart.set_quantity(inventory_unit_id, quantity, override_price=price)
    resp = RedirectResponse(url="/pos/cart", status_code=303)
    _persist_cart(resp, cart)
    return resp


@router.post("/cart/remove", response_class=HTMLResponse)
def cart_remove(
    request: Request,
    inventory_unit_id: int = Form(...),
    override_price: str = Form(""),
) -> RedirectResponse:
    price = override_price.strip() or None
    cart = _read_cart(request)
    cart.remove(inventory_unit_id, override_price=price)
    resp = RedirectResponse(url="/pos/cart", status_code=303)
    _persist_cart(resp, cart)
    return resp


@router.post("/cart/add_custom", response_class=HTMLResponse)
def cart_add_custom(
    request: Request,
    price: str = Form(...),
    quantity: int = Form(1),
) -> RedirectResponse:
    try:
        price_dec = Decimal(price.strip()).quantize(Decimal("0.01"))
        if price_dec <= 0:
            raise ValueError
    except Exception:
        return RedirectResponse(url="/pos/?custom_error=price", status_code=303)
    cart = _read_cart(request)
    cart.add(_CUSTOM_ITEM_UNIT_ID, max(1, quantity), override_price=str(price_dec))
    resp = RedirectResponse(url="/pos/cart", status_code=303)
    _persist_cart(resp, cart)
    return resp


@router.post("/cart/clear", response_class=HTMLResponse)
def cart_clear() -> RedirectResponse:
    resp = RedirectResponse(url="/pos/cart", status_code=303)
    _clear_cart_cookie(resp)
    return resp


# ---- Cart view + checkout preview ----
@router.get("/cart", response_class=HTMLResponse)
def cart_view(
    request: Request,
    payment_method: str = "card",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    cart = _read_cart(request)
    lines, deleted_ids = _cart_lines(session, cart)
    if payment_method not in ("card", "cash"):
        payment_method = "card"
    rates = get_pos_rates(session)
    totals = compute_totals(
        lines,
        tax_rate=rates["pos_tax_rate"],
        card_surcharge_rate=rates["pos_card_surcharge"],
        cash_discount_rate=rates["pos_cash_discount"],
        payment_method=payment_method,
    )
    # Collect sold-out unit IDs forwarded from cart_checkout validation.
    sold_out_ids: set[int] = set()
    for part in request.query_params.get("sold_out", "").split(","):
        try:
            sold_out_ids.add(int(part.strip()))
        except ValueError:
            pass

    # Parse checked_at timestamp (seconds since epoch) into a display string.
    sold_out_checked_at: str = ""
    try:
        ts = int(request.query_params.get("checked_at", ""))
        import datetime as _dt
        sold_out_checked_at = _dt.datetime.fromtimestamp(ts).strftime("%-I:%M:%S %p")
    except (ValueError, OSError):
        pass

    zero_price_ids = {ln.inventory_unit_id for ln in totals.lines if ln.unit_price == 0}

    return templates.TemplateResponse(
        request,
        "pos/cart.html",
        {
            "title": "Cart",
            "totals": totals,
            "tax_rate": rates["pos_tax_rate"],
            "card_surcharge_rate": rates["pos_card_surcharge"],
            "cash_discount_rate": rates["pos_cash_discount"],
            "is_empty": cart.is_empty(),
            "sold_out_ids": sold_out_ids,
            "sold_out_checked_at": sold_out_checked_at,
            "deleted_ids": deleted_ids,
            "zero_price_ids": zero_price_ids,
        },
    )


@router.post("/cart/checkout", response_class=HTMLResponse)
def cart_checkout(
    request: Request,
    payment_method: str = Form("card"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    cart = _read_cart(request)
    if cart.is_empty():
        return RedirectResponse(url="/pos/cart?empty_checkout=1", status_code=303)

    # Guard: catch items that sold online while sitting in the cart.
    # Bounce back before any payment is initiated so the cashier can
    # remove the unavailable items and retry.
    sold_out = _sold_out_items(session, cart)
    if sold_out:
        ids = ",".join(str(uid) for uid, _ in sold_out)
        ts = int(time.time())
        return RedirectResponse(
            url=f"/pos/cart?sold_out={ids}&payment_method={payment_method}&checked_at={ts}",
            status_code=303,
        )

    lines, _ = _cart_lines(session, cart)
    rates = get_pos_rates(session)
    totals = compute_totals(
        lines,
        tax_rate=rates["pos_tax_rate"],
        card_surcharge_rate=rates["pos_card_surcharge"],
        cash_discount_rate=rates["pos_cash_discount"],
        payment_method=payment_method,
    )

    if payment_method == "cash":
        if totals.total <= 0:
            return RedirectResponse(url="/pos/cart?zero_price=1", status_code=303)
        return _checkout_cash(request, session, totals)
    else:
        # Card: keep cart alive, redirect to "present card reader" confirmation screen
        return RedirectResponse(url="/pos/checkout/card", status_code=303)


def _checkout_cash(request, session, totals):
    """Record a cash sale locally — no Shopify API call."""
    sale_lines = [
        SaleLineInput(
            inventory_unit_id=ls.inventory_unit_id,
            quantity=ls.quantity,
            unit_price=ls.unit_price,
        )
        for ls in totals.lines
    ]
    result = record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        lines=sale_lines,
        payment_method="cash",
        subtotal=totals.subtotal,
        tax=totals.tax,
        total=totals.total,
        notes="Cash",
    )
    session.commit()

    # Push quantity change to TCGPlayer portal in background
    _tcg_dispatch([li.inventory_unit_id for li in sale_lines])

    oversell_param = "&oversell_conflict=1" if result.had_oversell else ""
    resp = RedirectResponse(
        url=f"/pos/checkout/done?method=cash&sale_id={result.sale.id}{oversell_param}",
        status_code=303,
    )
    _clear_cart_cookie(resp)
    return resp


@router.get("/checkout/card", response_class=HTMLResponse)
def checkout_card_prompt(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Show total and wait for cashier to confirm the card reader approved."""
    cart = _read_cart(request)
    if cart.is_empty():
        return RedirectResponse(url="/pos/cart?empty_checkout=1", status_code=303)
    lines, _ = _cart_lines(session, cart)
    rates = get_pos_rates(session)
    totals = compute_totals(
        lines,
        tax_rate=rates["pos_tax_rate"],
        card_surcharge_rate=rates["pos_card_surcharge"],
        cash_discount_rate=rates["pos_cash_discount"],
        payment_method="card",
    )
    if totals.total <= 0:
        return RedirectResponse(url="/pos/cart?zero_price=1", status_code=303)
    return templates.TemplateResponse(
        request,
        "pos/checkout_card.html",
        {
            "title": "Card payment",
            "phase": "5 — Shopify",
            "totals": totals,
            "card_surcharge_rate": rates["pos_card_surcharge"],
            "tax_rate": rates["pos_tax_rate"],
        },
    )


@router.post("/checkout/card/confirm", response_class=HTMLResponse)
def checkout_card_confirm(
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Cashier tapped 'Payment received' — record sale in Shopify + locally."""
    cart = _read_cart(request)
    if cart.is_empty():
        return RedirectResponse(url="/pos/cart?empty_checkout=1", status_code=303)

    lines, _ = _cart_lines(session, cart)
    rates = get_pos_rates(session)
    totals = compute_totals(
        lines,
        tax_rate=rates["pos_tax_rate"],
        card_surcharge_rate=rates["pos_card_surcharge"],
        cash_discount_rate=rates["pos_cash_discount"],
        payment_method="card",
    )

    if totals.total <= 0:
        return RedirectResponse(url="/pos/cart?zero_price=1", status_code=303)

    sale_lines = [
        SaleLineInput(
            inventory_unit_id=ls.inventory_unit_id,
            quantity=ls.quantity,
            unit_price=ls.unit_price,
        )
        for ls in totals.lines
    ]
    result = record_sale(
        session,
        channel=Channel.SHOPIFY_POS.value,
        lines=sale_lines,
        payment_method="card",
        subtotal=totals.subtotal,
        tax=totals.tax,
        card_surcharge=totals.card_surcharge,
        total=totals.total,
        notes="Card",
    )
    session.commit()

    # Push quantity change to TCGPlayer portal in background
    _tcg_dispatch([li.inventory_unit_id for li in sale_lines])

    error_param = "&oversell_conflict=1" if result.had_oversell else ""
    resp = RedirectResponse(
        url=f"/pos/checkout/done?method=card&sale_id={result.sale.id}{error_param}",
        status_code=303,
    )
    _clear_cart_cookie(resp)
    return resp


@router.get("/checkout/done", response_class=HTMLResponse)
def checkout_done(
    request: Request,
    method: str = "card",
    sale_id: int = 0,
    oversell_conflict: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    sale_lines: list[SaleLine] = []
    sale: Sale | None = None
    if sale_id:
        sale = session.get(Sale, sale_id)
        if sale:
            sale_lines = session.execute(
                select(SaleLine).where(SaleLine.sale_id == sale_id)
            ).scalars().all()
    return templates.TemplateResponse(
        request,
        "pos/checkout_done.html",
        {
            "title": "Sale complete",
            "method": method,
            "sale": sale,
            "sale_lines": sale_lines,
            "oversell_conflict": oversell_conflict,
        },
    )
