"""Fetch TCGPlayer product images and cache them locally.

We don't ship images in our DB; we keep them on disk under ``data/images/``
so the static handler can serve them as ``/static-images/<id>.jpg`` and
PyInstaller bundles stay reasonably sized.

The fetcher is built around an injectable ``httpx.Client`` so tests can
swap in ``httpx.MockTransport`` without hitting the network.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class ImageCache:
    def __init__(self, root: Path, client: httpx.Client | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=10.0, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ImageCache":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def local_path(self, tcgplayer_id: int) -> Path:
        return self.root / f"{tcgplayer_id}.jpg"

    def fetch_if_missing(self, tcgplayer_id: int, source_url: str) -> Path | None:
        """SKU-id-keyed convenience: download to ``<root>/<id>.jpg`` if
        not already on disk. Returns the local path on success, ``None``
        on failure (failures don't raise — they're logged so the
        surrounding sync run can keep going).
        """
        return self.fetch_to_path(self.local_path(tcgplayer_id), source_url)

    def fetch_to_path(self, local_path: Path, source_url: str) -> Path | None:
        """Download to an explicit local path. Used by callers that key
        their cache on something other than the SKU id (e.g., the
        product-image fetcher uses marketplace product ids).

        Skips the HTTP if ``local_path`` already exists and is non-empty.
        """
        if local_path.exists() and local_path.stat().st_size > 0:
            return local_path
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            r = self._client.get(source_url)
            r.raise_for_status()
            local_path.write_bytes(r.content)
            return local_path
        except Exception:
            logger.warning(
                "Image fetch failed for path=%s url=%s",
                local_path,
                source_url,
                exc_info=True,
            )
            return None
