from decimal import Decimal

from app.db.models import Channel, ChannelListing, InventoryUnit, Product, ProductKind
from app.sync.tcgplayer import diff as diff_module
from app.sync.tcgplayer.diff import build_plan
from app.sync.tcgplayer.parser import IngestRow


def _row(
    *,
    tcg_id: int,
    condition: str | None = "NM",
    qty: int = 1,
    price: Decimal = Decimal("1.00"),
    kind: str = "single",
    reserve_qty: int = 0,
) -> IngestRow:
    return IngestRow(
        tcgplayer_product_id=tcg_id,
        kind=kind,
        name=f"Card {tcg_id}",
        set="TestSet",
        condition=condition,
        quantity=qty,
        reserve_quantity=reserve_qty,
        unit_price=price,
        image_url=None,
        rarity="Common" if kind == "single" else None,
        product_line="Magic",
        sealed_subtype=None,
        number=None,
    )


def _seed_product(session, *, tcg_id: int, condition="NM", qty=1, price="1.00"):
    p = Product(
        tcgplayer_product_id=tcg_id,
        kind=ProductKind.SINGLE.value,
        name=f"Card {tcg_id}",
        set="TestSet",
    )
    session.add(p)
    session.flush()
    u = InventoryUnit(
        product_id=p.id,
        condition=condition,
        quantity_on_hand=qty,
        unit_price=Decimal(price),
    )
    session.add(u)
    session.flush()
    cl = ChannelListing(
        inventory_unit_id=u.id,
        channel=Channel.TCGPLAYER.value,
        external_listing_id=str(tcg_id),
        last_pushed_quantity=qty,
        last_pushed_price=Decimal(price),
        sync_state="ok",
    )
    session.add(cl)
    session.flush()
    return p, u


def test_empty_db_all_rows_are_new(session):
    plan = build_plan([_row(tcg_id=1), _row(tcg_id=2)], session)
    assert len(plan.new_products) == 2
    assert plan.new_variants == []
    assert plan.qty_changes == []
    assert plan.price_changes == []


def test_existing_product_no_changes(session):
    _seed_product(session, tcg_id=1, condition="NM", qty=3, price="2.00")
    plan = build_plan(
        [_row(tcg_id=1, condition="NM", qty=3, price=Decimal("2.00"))], session
    )
    assert plan.new_products == []
    assert plan.qty_changes == []
    assert plan.price_changes == []
    assert len(plan.unchanged) == 1


def test_quantity_change_detected(session):
    _seed_product(session, tcg_id=1, qty=5, price="1.00")
    plan = build_plan(
        [_row(tcg_id=1, qty=2, price=Decimal("1.00"))], session
    )
    assert plan.new_products == []
    assert len(plan.qty_changes) == 1
    assert plan.qty_changes[0][1] == 2


def test_price_change_detected(session):
    _seed_product(session, tcg_id=1, qty=1, price="1.00")
    plan = build_plan(
        [_row(tcg_id=1, qty=1, price=Decimal("3.50"))], session
    )
    assert len(plan.price_changes) == 1
    assert plan.price_changes[0][1] == Decimal("3.50")
    assert plan.qty_changes == []


def test_new_variant_on_existing_product(session):
    _seed_product(session, tcg_id=1, condition="NM", qty=1)
    plan = build_plan(
        [
            _row(tcg_id=1, condition="NM", qty=1),
            _row(tcg_id=1, condition="LP", qty=2),
        ],
        session,
    )
    assert plan.new_products == []
    assert len(plan.new_variants) == 1
    assert plan.new_variants[0].condition == "LP"


def test_reserve_quantity_change_detected(session):
    _seed_product(session, tcg_id=1, condition="NM", qty=2)
    plan = build_plan(
        [_row(tcg_id=1, condition="NM", qty=2, reserve_qty=3)], session
    )
    assert plan.qty_changes == []
    assert plan.price_changes == []
    assert len(plan.reserve_changes) == 1
    new_row, new_reserve = plan.reserve_changes[0]
    assert new_row.tcgplayer_product_id == 1
    assert new_reserve == 3


def test_reserve_quantity_unchanged_no_diff(session):
    """Both reserve=0 by default — no change should be flagged."""
    _seed_product(session, tcg_id=1, condition="NM", qty=2)
    plan = build_plan(
        [_row(tcg_id=1, condition="NM", qty=2, reserve_qty=0)], session
    )
    assert plan.reserve_changes == []
    assert plan.unchanged != []


def test_in_clause_chunks_when_over_limit(session, monkeypatch):
    """Real CSVs have 200K+ rows — well past SQLite's 32766 parameter cap.
    Force the chunk size down to 5 and feed 12 ids; the diff must still
    correctly identify 2 existing rows + 10 new ones."""
    for i in range(1, 3):
        _seed_product(session, tcg_id=i, condition="NM", qty=1)
    monkeypatch.setattr(diff_module, "_IN_CHUNK_SIZE", 5)
    plan = build_plan(
        [_row(tcg_id=i, condition="NM", qty=1) for i in range(1, 13)], session
    )
    assert len(plan.new_products) == 10  # ids 3..12
    assert len(plan.unchanged) == 2      # ids 1..2 (matched + identical)
    assert plan.qty_changes == []
