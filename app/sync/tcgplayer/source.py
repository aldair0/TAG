"""Where the TCGPlayer CSV comes from.

Two implementations:

- ``FixtureTCGPlayerSource(path)`` reads a local CSV file. Used by tests and
  by the dev workflow until shop-owner credentials arrive.
- ``LiveTCGPlayerSource(...)`` will pull the CSV from the PRO Seller portal
  using a Playwright-captured session cookie. Stubbed for now; raises a
  clear error so it's obvious what's missing.

Callers depend on the abstract base only.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path


class TCGPlayerSource(ABC):
    @abstractmethod
    def fetch_rows(self) -> Iterator[dict[str, str]]:
        """Yield CSV rows as dicts keyed by the CSV header."""


class FixtureTCGPlayerSource(TCGPlayerSource):
    """Reads a CSV file from disk. The dev/test source."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch_rows(self) -> Iterator[dict[str, str]]:
        with self.path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                yield {k: (v if v is not None else "") for k, v in row.items()}


class LiveTCGPlayerSource(TCGPlayerSource):
    """The real thing — Playwright-captured session + httpx CSV download.

    Not implemented until shop-owner credentials and the auth flow land in
    Phase 1b. Calling ``fetch_rows`` raises a descriptive error so it's
    obvious which piece is still mocked.
    """

    def fetch_rows(self) -> Iterator[dict[str, str]]:
        raise NotImplementedError(
            "LiveTCGPlayerSource is not implemented yet. "
            "Set TCGPLAYER_PRO_USERNAME / TCGPLAYER_PRO_PASSWORD in .env and "
            "wire up the Playwright auth flow before using this. Until then, "
            "use FixtureTCGPlayerSource(path) with a local CSV."
        )
