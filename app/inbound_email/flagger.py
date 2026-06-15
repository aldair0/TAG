"""Map a parsed sale email onto inventory and set the sold-online flag.

This is the *inventory action* half of the email receiver, kept separate
from IMAP transport so it can be unit-tested against a DB session with no
network. Each email line item is matched to inventory units on
``(product name, condition)`` — both compared case-insensitively and
exactly, because the email's condition string (e.g. "Near Mint
Unlimited") is the same raw value the CSV parser stores.

The flag is intentionally non-destructive: a wrong match just needs a
staff "dismiss", and re-flagging the same unit only refreshes its expiry.
The authoritative stock decrement still comes from the TCGPlayer CSV
sync — this only closes the POS-oversell gap until that sync runs.

Unmatched items (sold online but absent/zero locally) are returned in the
summary and logged; they signal real inventory drift worth a human look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db.models import InventoryUnit, Product
from app.sync.tcgplayer.email_parser import SoldItem, SoldOnlineEmail

logger = logging.getLogger(__name__)


def expiry_for_flag(flagged_at: datetime) -> datetime:
    """UTC datetime = end of the calendar day *after* ``flagged_at`` (store tz).

    A unit flagged at any time today stays blocked through the end of
    tomorrow, store-local, giving the CSV sync ample time to catch up.
    Returned naive-UTC to match how the column is stored.
    """
    tz = ZoneInfo(settings.store_timezone)
    local = (
        flagged_at.astimezone(tz)
        if flagged_at.tzinfo
        else flagged_at.replace(tzinfo=timezone.utc).astimezone(tz)
    )
    # midnight at the start of the day-after-tomorrow == end of tomorrow
    expiry_local = datetime(local.year, local.month, local.day) + timedelta(days=2)
    return expiry_local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class ItemMatch:
    item: SoldItem
    flagged_unit_ids: list[int] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return bool(self.flagged_unit_ids)


@dataclass
class FlagSummary:
    order_id: str | None
    matches: list[ItemMatch] = field(default_factory=list)

    @property
    def flagged_unit_ids(self) -> list[int]:
        return [uid for m in self.matches for uid in m.flagged_unit_ids]

    @property
    def unmatched_items(self) -> list[SoldItem]:
        return [m.item for m in self.matches if not m.matched]

    @property
    def fully_matched(self) -> bool:
        return all(m.matched for m in self.matches)


def _find_units(session: Session, item: SoldItem) -> list[InventoryUnit]:
    """Units with stock whose product name (and condition, if known) match.

    Exact, case-insensitive. Condition is only constrained when the email
    actually carried one — a name-only match is better than no flag.
    """
    stmt = (
        select(InventoryUnit)
        .join(Product)
        .options(joinedload(InventoryUnit.product))
        .where(
            func.lower(Product.name) == item.name.lower(),
            InventoryUnit.quantity_on_hand > 0,
        )
    )
    if item.condition:
        stmt = stmt.where(func.lower(InventoryUnit.condition) == item.condition.lower())
    return list(session.execute(stmt).unique().scalars().all())


def flag_email_sale(
    session: Session,
    parsed: SoldOnlineEmail,
    *,
    source: str = "email",
) -> FlagSummary:
    """Flag inventory units for every line item in ``parsed`` as sold-online.

    Flushes but does not commit — the caller owns the transaction (mirrors
    :func:`app.sales.recorder.record_sale`).
    """
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    until = expiry_for_flag(now_utc)
    summary = FlagSummary(order_id=parsed.order_id)

    for item in parsed.items:
        match = ItemMatch(item=item)
        for unit in _find_units(session, item):
            unit.sold_online_at = now_utc
            unit.sold_online_until = until
            match.flagged_unit_ids.append(unit.id)
        if match.matched:
            logger.info(
                "sold_online: order=%s flagged %d unit(s) for %r [%s] (source=%s)",
                parsed.order_id or "—",
                len(match.flagged_unit_ids),
                item.name,
                item.condition or "any condition",
                source,
            )
        else:
            logger.warning(
                "sold_online: order=%s NO inventory match for %r [%s] — "
                "possible inventory drift",
                parsed.order_id or "—",
                item.name,
                item.condition or "any condition",
            )
        summary.matches.append(match)

    session.flush()
    return summary
