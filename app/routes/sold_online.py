"""Admin routes for the "Sold Online" flag system.

When a unit is sold on an external channel (TCGPlayer, eBay) before the
CSV sync updates inventory, an out-of-band signal (currently: manual action;
future: email webhook) marks it as "sold online". The flag blocks POS sale
and auto-expires at the end of the calendar day *following* the flag date
(store timezone). Staff can also dismiss it manually.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db.models import InventoryUnit, Product
from app.db.session import get_session
from app.paths import templates_dir

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(templates_dir()))


def _expiry_for_flag(flagged_at: datetime) -> datetime:
    """Return UTC datetime = end of the calendar day *after* flagged_at (store tz)."""
    tz = ZoneInfo(settings.store_timezone)
    local = flagged_at.astimezone(tz) if flagged_at.tzinfo else flagged_at.replace(tzinfo=timezone.utc).astimezone(tz)
    # midnight at the start of the day-after-tomorrow == end of tomorrow
    expiry_local = datetime(local.year, local.month, local.day) + timedelta(days=2)
    return expiry_local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def active_sold_online_count(session: Session) -> int:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return session.execute(
        select(func.count()).select_from(InventoryUnit).where(
            InventoryUnit.sold_online_until.is_not(None),
            InventoryUnit.sold_online_until > now_utc,
        )
    ).scalar_one()


@router.get("/", response_class=HTMLResponse)
def sold_online_index(
    request: Request,
    q: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    tz = ZoneInfo(settings.store_timezone)

    # Active flags
    active_stmt = (
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(
            InventoryUnit.sold_online_until.is_not(None),
            InventoryUnit.sold_online_until > now_utc,
        )
        .order_by(InventoryUnit.sold_online_at.desc())
    )
    active = session.execute(active_stmt).unique().scalars().all()

    # Recently expired / dismissed (last 7 days), for history
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
    history_stmt = (
        select(InventoryUnit)
        .options(joinedload(InventoryUnit.product))
        .where(
            InventoryUnit.sold_online_at.is_not(None),
            InventoryUnit.sold_online_at >= week_ago,
            or_(
                InventoryUnit.sold_online_until.is_(None),
                InventoryUnit.sold_online_until <= now_utc,
            ),
        )
        .order_by(InventoryUnit.sold_online_at.desc())
        .limit(50)
    )
    history = session.execute(history_stmt).unique().scalars().all()

    # Search results for the manual-flag form
    search_results = []
    if q:
        like = f"%{q}%"
        search_stmt = (
            select(InventoryUnit)
            .join(Product)
            .options(joinedload(InventoryUnit.product))
            .where(
                InventoryUnit.quantity_on_hand > 0,
                or_(
                    Product.name.ilike(like),
                    Product.set.ilike(like),
                ),
            )
            .order_by(Product.name, InventoryUnit.condition)
            .limit(20)
        )
        search_results = session.execute(search_stmt).unique().scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/sold_online.html",
        {
            "title": "Sold Online",
            "active": active,
            "history": history,
            "search_results": search_results,
            "q": q,
            "now_tz": datetime.now(tz),
        },
    )


@router.post("/flag/{unit_id}", response_class=HTMLResponse)
def flag_unit(
    unit_id: int,
    source: str = Form("manual"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Mark a unit as sold online. Source='manual' now; 'email' when webhook is wired up."""
    unit = session.get(InventoryUnit, unit_id)
    if unit is None:
        return RedirectResponse(url="/admin/sold-online/", status_code=303)

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    unit.sold_online_at = now_utc
    unit.sold_online_until = _expiry_for_flag(now_utc)
    session.commit()
    logger.info("sold_online: flagged unit %d (source=%s, until=%s)", unit_id, source, unit.sold_online_until)
    return RedirectResponse(url="/admin/sold-online/", status_code=303)


@router.post("/dismiss/{unit_id}", response_class=HTMLResponse)
def dismiss_unit(
    unit_id: int,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Manually dismiss the sold-online flag — unit is sellable again immediately."""
    unit = session.get(InventoryUnit, unit_id)
    if unit is not None:
        unit.sold_online_until = None
        session.commit()
        logger.info("sold_online: dismissed unit %d", unit_id)
    return RedirectResponse(url="/admin/sold-online/", status_code=303)


# ---------------------------------------------------------------------------
# Email signal endpoint (placeholder)
# ---------------------------------------------------------------------------
# FUTURE IMPLEMENTATION:
#   Wire an email parsing service (e.g. Postmark inbound, SendGrid parse,
#   or a local IMAP poller) to POST here whenever a TCGPlayer/eBay sale
#   notification email arrives. The parser extracts the card name + channel
#   from the email body and calls this endpoint.
#
#   Expected caller flow:
#     1. Email arrives at a forwarding address (e.g. sales@tagcollects.com)
#     2. Email service POSTs to POST /admin/sold-online/signal with JSON body
#     3. This endpoint fuzzy-matches the card name against inventory and
#        flags matching unit(s) as sold online
#     4. Returns JSON so the email service can log success/failure
#
#   Auth: add a shared secret header check (X-Signal-Token) before deploying
#   to a public URL. Currently open — only reachable on the local network.
# ---------------------------------------------------------------------------

@router.post("/signal")
def email_signal(
    request: Request,
    card_name: str = Form(...),
    channel: str = Form("tcgplayer"),
    quantity: int = Form(1),
    order_id: str = Form(""),
    session: Session = Depends(get_session),
) -> JSONResponse:
    """Receive an external sale signal (e.g. from a parsed email) and flag
    matching inventory units as sold online.

    Matches on exact product name (case-insensitive). If multiple units
    exist for the same product (different conditions), all are flagged —
    the cashier dismisses whichever didn't actually sell.

    Returns JSON: {"flagged": [unit_id, ...], "unmatched": true/false}
    """
    like = card_name.strip()
    if not like:
        return JSONResponse({"error": "card_name is required"}, status_code=400)

    stmt = (
        select(InventoryUnit)
        .join(Product)
        .options(joinedload(InventoryUnit.product))
        .where(Product.name.ilike(like))
        .where(InventoryUnit.quantity_on_hand > 0)
    )
    units = session.execute(stmt).unique().scalars().all()

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    flagged_ids = []
    for unit in units:
        unit.sold_online_at = now_utc
        unit.sold_online_until = _expiry_for_flag(now_utc)
        flagged_ids.append(unit.id)
        logger.info(
            "sold_online: email signal flagged unit %d (%s) channel=%s order=%s",
            unit.id, card_name, channel, order_id or "—",
        )

    if flagged_ids:
        session.commit()

    return JSONResponse({
        "flagged": flagged_ids,
        "unmatched": len(flagged_ids) == 0,
        "card_name": like,
        "channel": channel,
        "order_id": order_id or None,
    })
