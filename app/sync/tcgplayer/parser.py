"""Convert one TCGPlayer PRO Seller CSV row into our normalized IngestRow.

The CSV column shape comes from the documented PRO Seller export:

    TCGplayer Id, Product Line, Set Name, Product Name, Title, Number,
    Rarity, Condition, TCG Marketplace Price, Total Quantity, Photo URL

Real exports may have additional columns (TCG Market Price, TCG Direct
Low, My Store Reserve Quantity, etc.) — we tolerate extras and only read
the columns we care about.

Conditions are stored **as TCGPlayer writes them**. That includes foil
suffixes like "Near Mint Holofoil" or "Lightly Played Reverse Holofoil",
because the seller portal treats those strings as the canonical condition
value and foil/non-foil rows have distinct TCGplayer Ids anyway. Storing
the raw value avoids a normalization layer that would have to keep up
with every new printing-treatment TCGPlayer invents.

Sealed product detection: TCGPlayer rows don't have a stable "kind" field.
We use a few heuristics:
  - Condition is "Sealed", "Unopened", or empty AND Rarity is empty → sealed
  - Otherwise → single

Supplies never come through this parser (they're entered via Admin UI
in Phase 4), so the parser only ever produces single/sealed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ParseError(ValueError):
    """Raised when a CSV row can't be parsed into an IngestRow."""


@dataclass(frozen=True)
class IngestRow:
    tcgplayer_product_id: int
    kind: str  # "single" | "sealed"
    name: str
    set: str | None
    condition: str | None  # raw CSV value (e.g. "Near Mint Holofoil"); None for sealed
    quantity: int
    reserve_quantity: int  # TCGPlayer's floor — marketplace won't sell below this
    unit_price: Decimal | None
    image_url: str | None
    rarity: str | None
    product_line: str | None
    sealed_subtype: str | None
    number: str | None


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def _is_sealed_condition(raw_condition: str | None, raw_rarity: str | None) -> bool:
    if raw_condition:
        c = raw_condition.strip().lower()
        if c in {"sealed", "unopened", ""}:
            return True
    # Belt-and-suspenders: no rarity AND no condition → sealed.
    return not _clean(raw_rarity) and not _clean(raw_condition)


def parse_row(row: dict[str, str]) -> IngestRow:
    raw_id = _clean(row.get("TCGplayer Id"))
    if not raw_id:
        raise ParseError(f"Missing TCGplayer Id: {row!r}")
    try:
        tcg_id = int(raw_id)
    except ValueError as e:
        raise ParseError(f"TCGplayer Id is not an integer: {raw_id!r}") from e

    raw_qty = _clean(row.get("Total Quantity")) or "0"
    try:
        qty = int(raw_qty)
    except ValueError as e:
        raise ParseError(f"Total Quantity is not an integer: {raw_qty!r}") from e

    raw_reserve = _clean(row.get("My Store Reserve Quantity")) or "0"
    try:
        reserve_qty = int(raw_reserve)
    except ValueError as e:
        raise ParseError(
            f"My Store Reserve Quantity is not an integer: {raw_reserve!r}"
        ) from e

    price: Decimal | None = None
    raw_price = _clean(row.get("TCG Marketplace Price"))
    if raw_price:
        try:
            price = Decimal(raw_price.replace("$", "").replace(",", ""))
        except InvalidOperation as e:
            raise ParseError(f"TCG Marketplace Price is not numeric: {raw_price!r}") from e

    rarity = _clean(row.get("Rarity"))
    raw_condition = _clean(row.get("Condition"))

    if _is_sealed_condition(raw_condition, rarity):
        kind = "sealed"
        condition = None
        sealed_subtype = _infer_sealed_subtype(_clean(row.get("Product Name")), _clean(row.get("Title")))
    else:
        kind = "single"
        condition = raw_condition  # already trimmed by _clean above
        if condition is None:
            raise ParseError(f"Single-card row missing Condition: {row!r}")
        sealed_subtype = None

    name = _clean(row.get("Product Name")) or _clean(row.get("Title")) or f"TCG#{tcg_id}"

    return IngestRow(
        tcgplayer_product_id=tcg_id,
        kind=kind,
        name=name,
        set=_clean(row.get("Set Name")),
        condition=condition,
        quantity=qty,
        reserve_quantity=reserve_qty,
        unit_price=price,
        image_url=_clean(row.get("Photo URL")),
        rarity=rarity if kind == "single" else None,
        product_line=_clean(row.get("Product Line")),
        sealed_subtype=sealed_subtype,
        number=_clean(row.get("Number")),
    )


def _infer_sealed_subtype(product_name: str | None, title: str | None) -> str | None:
    text = " ".join(filter(None, [product_name, title])).lower()
    if "booster box" in text:
        return "Booster Box"
    if "bundle" in text:
        return "Bundle"
    if "theme deck" in text or "starter deck" in text:
        return "Deck"
    if "collector booster" in text:
        return "Collector Booster"
    if "pack" in text:
        return "Pack"
    return None
