from decimal import Decimal

import pytest

from app.sync.tcgplayer.parser import ParseError, parse_row


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "TCGplayer Id": "501001",
        "Product Line": "Magic",
        "Set Name": "Bloomburrow",
        "Product Name": "Lightning Helix",
        "Title": "Lightning Helix (Bloomburrow)",
        "Number": "042",
        "Rarity": "Uncommon",
        "Condition": "Near Mint",
        "TCG Marketplace Price": "1.50",
        "Total Quantity": "1",
        "Photo URL": "https://example.com/501001.jpg",
    }
    base.update(overrides)
    return base


def test_parses_a_single_card():
    out = parse_row(_row())
    assert out.tcgplayer_product_id == 501001
    assert out.kind == "single"
    assert out.name == "Lightning Helix"
    assert out.set == "Bloomburrow"
    assert out.condition == "Near Mint"
    assert out.quantity == 1
    assert out.unit_price == Decimal("1.50")
    assert out.rarity == "Uncommon"
    assert out.image_url == "https://example.com/501001.jpg"
    assert out.sealed_subtype is None


def test_condition_passes_through_verbatim():
    """CSV-canonical: whatever TCGPlayer's CSV writes is what we store.
    This includes foil suffixes ("Near Mint Holofoil", "Lightly Played
    Reverse Holofoil") which TCGPlayer treats as the condition value
    proper. Foil/non-foil rows have distinct TCGplayer Ids anyway, so
    no information is lost."""
    assert parse_row(_row(**{"Condition": "Near Mint"})).condition == "Near Mint"
    assert parse_row(_row(**{"Condition": "Lightly Played"})).condition == "Lightly Played"
    assert parse_row(_row(**{"Condition": "Moderately Played"})).condition == "Moderately Played"
    assert parse_row(_row(**{"Condition": "Heavily Played"})).condition == "Heavily Played"
    assert parse_row(_row(**{"Condition": "Damaged"})).condition == "Damaged"
    assert parse_row(_row(**{"Condition": "Near Mint Holofoil"})).condition == "Near Mint Holofoil"
    assert parse_row(_row(**{"Condition": "Lightly Played Reverse Holofoil"})).condition == "Lightly Played Reverse Holofoil"


def test_condition_is_trimmed_only():
    """Surrounding whitespace is stripped, but case + content untouched."""
    assert parse_row(_row(**{"Condition": "  Near Mint  "})).condition == "Near Mint"


def test_reserve_quantity_extracted_from_csv():
    """TCGPlayer's 'My Store Reserve Quantity' column is the floor below
    which the marketplace listing won't sell. Parsed as int."""
    assert parse_row(_row(**{"My Store Reserve Quantity": "3"})).reserve_quantity == 3
    assert parse_row(_row(**{"My Store Reserve Quantity": "0"})).reserve_quantity == 0


def test_reserve_quantity_blank_defaults_to_zero():
    assert parse_row(_row(**{"My Store Reserve Quantity": ""})).reserve_quantity == 0


def test_reserve_quantity_missing_column_defaults_to_zero():
    """Synthetic fixtures may omit this column entirely."""
    row = _row()
    row.pop("My Store Reserve Quantity", None)
    assert parse_row(row).reserve_quantity == 0


def test_reserve_quantity_non_integer_raises():
    with pytest.raises(ParseError):
        parse_row(_row(**{"My Store Reserve Quantity": "lots"}))


def test_detects_sealed_via_sealed_condition():
    out = parse_row(_row(**{"Condition": "Sealed", "Rarity": "", "Number": ""}))
    assert out.kind == "sealed"
    assert out.condition is None
    assert out.rarity is None


def test_detects_sealed_via_empty_rarity_and_condition():
    row = _row(
        **{
            "Condition": "",
            "Rarity": "",
            "Number": "",
            "Product Name": "Bloomburrow Booster Box",
        }
    )
    out = parse_row(row)
    assert out.kind == "sealed"
    assert out.sealed_subtype == "Booster Box"


def test_infers_bundle_subtype():
    row = _row(
        **{
            "Condition": "Sealed",
            "Rarity": "",
            "Number": "",
            "Product Name": "Bloomburrow Bundle",
        }
    )
    assert parse_row(row).sealed_subtype == "Bundle"


def test_dollar_sign_in_price():
    out = parse_row(_row(**{"TCG Marketplace Price": "$12.34"}))
    assert out.unit_price == Decimal("12.34")


def test_missing_id_raises():
    with pytest.raises(ParseError):
        parse_row(_row(**{"TCGplayer Id": ""}))


def test_non_integer_id_raises():
    with pytest.raises(ParseError):
        parse_row(_row(**{"TCGplayer Id": "not-a-number"}))


def test_single_without_condition_raises():
    with pytest.raises(ParseError):
        parse_row(_row(**{"Condition": "", "Rarity": "Rare"}))
