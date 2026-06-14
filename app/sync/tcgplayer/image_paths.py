"""Deterministic image path scheme.

Filenames are built from ``set + product name + collector number`` so
that any caller (POS, admin, eBay outbound, manual fs inspection) can
construct the exact same path without consulting the DB beyond those
three fields.

Layout::

    data/images/<set-slug>/<name-slug>__<number-slug>.jpg

Examples::

    Dark Flareon (Team Rocket, 35/82)
        → data/images/team-rocket/dark-flareon__35-82.jpg
    Bloomburrow Booster Box (Bloomburrow, no number)
        → data/images/bloomburrow/bloomburrow-booster-box.jpg

The ``Product`` row carries a boolean ``has_image`` that's flipped to
``True`` once the JPEG lands on disk; templates render the image only
when that flag is set, and use ``image_url_path`` to compute the
``<img src=...>`` URL.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

IMAGES_ROOT = Path("data/images")
IMAGES_URL_PREFIX = "/images"

_APOSTROPHE = re.compile(r"['’ʼ]")  # straight, curly, modifier
_SLUG_REPLACE = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def slugify(value: str | None) -> str:
    """Lowercase, ASCII-fold, drop apostrophes (so ``Bloom's`` → ``blooms``,
    not ``bloom-s``), then replace runs of non-alphanumerics with a
    single hyphen and strip edges. Returns ``"unknown"`` for empty
    input so paths never collapse to ``data/images//foo.jpg``."""
    if not value or not value.strip():
        return "unknown"
    # ASCII-fold (é → e, etc.) so paths stay 7-bit safe.
    folded = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    # Drop apostrophes BEFORE the broader regex so they collapse words
    # rather than acting as separators.
    folded = _APOSTROPHE.sub("", folded)
    s = _SLUG_REPLACE.sub("-", folded.lower())
    s = _SLUG_TRIM.sub("", s)
    return s or "unknown"


def image_filename(*, name: str, number: str | None) -> str:
    """Build the filename portion (no directory, no leading slash).
    Sealed/supply rows often lack a number — in that case we drop the
    ``__<number>`` suffix to keep the name compact."""
    name_slug = slugify(name)
    if number and number.strip():
        return f"{name_slug}__{slugify(number)}.jpg"
    return f"{name_slug}.jpg"


def image_local_path(
    *, set_name: str | None, name: str, number: str | None, root: Path | None = None
) -> Path:
    """Filesystem path that ``ProductImageFetcher`` writes to and reads
    from. Pass ``root`` to override the default ``data/images/`` (used
    by tests to isolate to ``tmp_path``)."""
    base = root if root is not None else IMAGES_ROOT
    return base / slugify(set_name) / image_filename(name=name, number=number)


def image_url_path(
    *, set_name: str | None, name: str, number: str | None
) -> str:
    """URL the static mount serves — what templates render in
    ``<img src=...>``."""
    return (
        f"{IMAGES_URL_PREFIX}/{slugify(set_name)}/"
        f"{image_filename(name=name, number=number)}"
    )
