"""Slugifier + filename builder for product images.

Deterministic naming so any subsystem (POS, admin, eBay outbound) can
construct the same path from a Product without consulting the DB beyond
its set/name/number fields.
"""

from __future__ import annotations

from app.sync.tcgplayer.image_paths import (
    image_filename,
    image_local_path,
    image_url_path,
    slugify,
)


# ---- slugify ----------------------------------------------------------


def test_slugify_lowercases():
    assert slugify("Team Rocket") == "team-rocket"


def test_slugify_replaces_spaces_with_hyphens():
    assert slugify("Base Set 2") == "base-set-2"


def test_slugify_strips_punctuation():
    assert slugify("Bloom's Bundle!") == "blooms-bundle"
    assert slugify("Set: The Sequel") == "set-the-sequel"


def test_slugify_replaces_slashes_in_card_numbers():
    """Card numbers like '35/82' must become filesystem-safe."""
    assert slugify("35/82") == "35-82"
    assert slugify("065a/119") == "065a-119"


def test_slugify_collapses_repeated_separators():
    assert slugify("hello   world") == "hello-world"
    assert slugify("a / / b") == "a-b"


def test_slugify_strips_leading_trailing_separators():
    assert slugify("  hello world  ") == "hello-world"
    assert slugify("--abc--") == "abc"


def test_slugify_handles_unicode_via_ascii_fold():
    """Pokemon often has non-ASCII like 'é' — fold to ASCII so paths
    stay simple."""
    assert slugify("Pokémon") == "pokemon"


def test_slugify_empty_string():
    assert slugify("") == "unknown"
    assert slugify(None) == "unknown"
    assert slugify("   ") == "unknown"


# ---- image_filename ---------------------------------------------------


def test_image_filename_combines_name_and_number():
    assert (
        image_filename(name="Dark Flareon", number="35/82")
        == "dark-flareon__35-82.jpg"
    )


def test_image_filename_when_number_missing():
    """Sealed and supplies often have no collector number."""
    assert image_filename(name="Bloomburrow Booster Box", number=None) == (
        "bloomburrow-booster-box.jpg"
    )
    assert image_filename(name="Bloomburrow Booster Box", number="") == (
        "bloomburrow-booster-box.jpg"
    )


# ---- image_local_path -------------------------------------------------


def test_image_local_path_groups_by_set_slug():
    p = image_local_path(set_name="Team Rocket", name="Dark Flareon", number="35/82")
    # Path-typed return; ".as_posix()" gives a forward-slash form for assertion
    assert p.as_posix() == "data/images/team-rocket/dark-flareon__35-82.jpg"


def test_image_local_path_unknown_set_falls_back():
    p = image_local_path(set_name=None, name="Mystery Card", number="1/1")
    assert p.as_posix() == "data/images/unknown/mystery-card__1-1.jpg"


# ---- image_url_path ---------------------------------------------------


def test_image_url_path_serves_from_images_mount():
    """What templates render in <img src=...>"""
    url = image_url_path(set_name="Team Rocket", name="Dark Flareon", number="35/82")
    assert url == "/images/team-rocket/dark-flareon__35-82.jpg"
