"""Parse a TCGPlayer "items have sold" notification email into a structured sale.

These emails arrive at the store inbox whenever an order is placed on
TCGPlayer's marketplace. The subject only names the *first* item
("... and 2 more items have sold!"), so the authoritative item list must
come from the HTML body's ORDER DETAILS table, not the subject.

Body shape (after base64-decoding the ``text/html`` part)::

    <b>Order:</b> <a ...>1D1B3BF6-923C64-5ED56</a>
    <b>Order Total:</b> $5.99
    ... ORDER DETAILS table, one row per line item ...
        <div ...vertical-align: top">1</div>          <- quantity
        <div ...><span style="margin-left:18px;">Left Leg of the
                 Forbidden One/Near Mint Unlimited</span></div>
    ... Remember to ship this order no later than 48 hours after the
        order date of 6/13/2026.

Each item cell holds ``<name>/<condition>``. The name itself can contain
a slash (e.g. ``Mamoswine ex - 174/159``), so the condition is split off
the **last** ``/`` only.

The parser is deliberically transport-agnostic: it takes raw bytes (a
``.eml`` file or an IMAP ``RFC822`` fetch) or a pre-parsed
``email.message.Message``. Wiring it to a live inbox lives elsewhere.
"""

from __future__ import annotations

import email
import html
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from email.message import Message

logger = logging.getLogger(__name__)

# Hard cap on the HTML body we'll scan with regex (ReDoS defense-in-depth).
_MAX_BODY_CHARS = 2_000_000


class EmailParseError(ValueError):
    """Raised when an email can't be parsed into a SoldOnlineEmail."""


@dataclass(frozen=True)
class SoldItem:
    """One line item from a TCGPlayer sale notification."""

    quantity: int
    name: str
    condition: str | None  # raw printing/condition, e.g. "Near Mint Unlimited"


@dataclass(frozen=True)
class SoldOnlineEmail:
    """Structured contents of one "items have sold" notification."""

    order_id: str | None
    order_total: Decimal | None
    order_date: date | None
    subject: str
    items: list[SoldItem]
    message_id: str | None = None


# --- header / subject helpers ----------------------------------------------

# Subject: "Your TCGplayer.com items of <first item> have sold!"
_SUBJECT_RE = re.compile(
    r"items of (?P<lead>.+?)\s+have sold", re.IGNORECASE | re.DOTALL
)


def is_sale_notification(
    msg: Message,
    *,
    from_contains: str = "tcgplayer",
    subject_contains: str = "have sold",
) -> bool:
    """Cheap pre-filter: does this look like a sale-notification email?

    A message qualifies when its From header contains ``from_contains`` and
    its Subject contains ``subject_contains`` (both case-insensitive). An
    empty criterion is treated as "always satisfied" so callers can disable
    either half. Lets a receiver skip non-sale mail without a full body
    parse. Defaults recognize TCGPlayer's "items have sold" emails.
    """
    subject = (msg.get("Subject", "") or "").lower()
    sender = (msg.get("From", "") or "").lower()
    return (
        from_contains.lower() in sender
        and subject_contains.lower() in subject
    )


# --- body extraction --------------------------------------------------------

def _html_body(msg: Message) -> str:
    """Return the decoded text/html part (TCGPlayer sends a single HTML part).

    Falls back to the first text part if no text/html is present.
    """
    html_part: str | None = None
    text_part: str | None = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart():
            continue
        if ctype == "text/html" and html_part is None:
            html_part = _decode_part(part)
        elif ctype == "text/plain" and text_part is None:
            text_part = _decode_part(part)
    body = html_part if html_part is not None else text_part
    if body is None:
        raise EmailParseError("email has no decodable text part")
    # Cap body size before any regex runs — a multi-MB hostile body shouldn't
    # be scanned (defense-in-depth alongside the bounded quantifiers). A real
    # TCGPlayer notification is a few tens of KB.
    if len(body) > _MAX_BODY_CHARS:
        logger.warning("email body %d chars exceeds cap — truncating", len(body))
        body = body[:_MAX_BODY_CHARS]
    return body


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)  # handles base64 / quoted-printable
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# --- field extraction -------------------------------------------------------

# Item rows: a quantity div ("vertical-align: top">1</div>") followed by the
# name/condition span ("margin-left:18px;">Name/Condition</span>").
# Bounded quantifiers (qty 1-4 digits, gap/text length-capped) so a crafted
# email body can't trigger pathological regex backtracking (ReDoS).
_ITEM_RE = re.compile(
    r'vertical-align:\s*top"?\s*>\s*(?P<qty>\d{1,4})\s*</div>'
    r".{0,4000}?"
    r'margin-left:\s*18px;[^>]*>(?P<text>.{0,500}?)</span>',
    re.DOTALL | re.IGNORECASE,
)
# Fallback when the quantity div shape changes but item spans survive.
_ITEM_SPAN_RE = re.compile(
    r'margin-left:\s*18px;[^>]*>(?P<text>.{0,500}?)</span>', re.DOTALL | re.IGNORECASE
)

_ORDER_ID_RE = re.compile(r"manageorder/([A-Za-z0-9][A-Za-z0-9-]+)", re.IGNORECASE)
_ORDER_TOTAL_RE = re.compile(r"Order Total:\s*</b>\s*\$?\s*([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)
_ORDER_DATE_RE = re.compile(r"order date of\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)


def _clean_text(raw: str) -> str:
    """Unescape entities and collapse whitespace from an HTML text node."""
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _split_name_condition(text: str) -> tuple[str, str | None]:
    """Split ``"Name/Condition"`` on the LAST slash.

    The card name may itself contain ``/`` (e.g. ``Mamoswine ex - 174/159``),
    so only the final ``/`` separates name from condition.
    """
    name, sep, condition = text.rpartition("/")
    if not sep:
        return text.strip(), None
    return name.strip(), condition.strip() or None


def _extract_items(body: str) -> list[SoldItem]:
    items: list[SoldItem] = []
    matches = list(_ITEM_RE.finditer(body))
    if matches:
        for m in matches:
            name, condition = _split_name_condition(_clean_text(m.group("text")))
            if not name:
                continue
            items.append(SoldItem(quantity=int(m.group("qty")), name=name, condition=condition))
        return items

    # Fallback: spans without a parseable quantity → assume qty 1.
    for m in _ITEM_SPAN_RE.finditer(body):
        name, condition = _split_name_condition(_clean_text(m.group("text")))
        if name:
            items.append(SoldItem(quantity=1, name=name, condition=condition))
    return items


def _extract_order_id(body: str) -> str | None:
    m = _ORDER_ID_RE.search(body)
    return m.group(1) if m else None


def _extract_order_total(body: str) -> Decimal | None:
    m = _ORDER_TOTAL_RE.search(body)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _extract_order_date(body: str) -> date | None:
    m = _ORDER_DATE_RE.search(body)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --- public entrypoints -----------------------------------------------------

def parse_message(msg: Message) -> SoldOnlineEmail:
    """Parse an already-loaded email Message into a SoldOnlineEmail."""
    subject = _clean_text(msg.get("Subject", "") or "")
    body = _html_body(msg)
    items = _extract_items(body)
    if not items:
        raise EmailParseError(
            f"no line items found in sale email (subject={subject!r})"
        )
    return SoldOnlineEmail(
        order_id=_extract_order_id(body),
        order_total=_extract_order_total(body),
        order_date=_extract_order_date(body),
        subject=subject,
        items=items,
        message_id=(msg.get("Message-ID") or "").strip() or None,
    )


def parse_bytes(raw: bytes) -> SoldOnlineEmail:
    """Parse raw RFC822 bytes (a ``.eml`` file or an IMAP fetch) into a sale."""
    return parse_message(email.message_from_bytes(raw))
