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
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# SSRF guard: image URLs come from an untrusted CSV (`Photo URL`). Only fetch
# https URLs on TCGPlayer's own domains — never internal hosts / metadata IPs.
_ALLOWED_HOST_SUFFIX = ".tcgplayer.com"
# Memory guard: never buffer an unbounded response body.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _is_allowed_image_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https":
        return False
    host = (p.hostname or "").lower()
    return host == "tcgplayer.com" or host.endswith(_ALLOWED_HOST_SUFFIX)


class ImageCache:
    def __init__(self, root: Path, client: httpx.Client | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._owns_client = client is None
        # follow_redirects=False: a redirect could otherwise bounce an
        # allow-listed URL to an internal host, defeating the SSRF guard.
        self._client = client or httpx.Client(timeout=10.0, follow_redirects=False)

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
        if not _is_allowed_image_url(source_url):
            logger.warning("Image fetch refused (URL not allowed): %s", source_url)
            return None
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            data = self._download_capped(source_url)
            if data is None:
                return None
            local_path.write_bytes(data)
            return local_path
        except httpx.HTTPError:
            logger.warning(
                "Image fetch failed for path=%s url=%s",
                local_path,
                source_url,
                exc_info=True,
            )
            return None

    def _download_capped(self, url: str) -> bytes | None:
        """Stream the body, enforcing an image content-type and a hard byte
        cap so a hostile/oversized response can't exhaust memory."""
        with self._client.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            if not ctype.startswith("image/"):
                logger.warning("Image fetch refused (content-type=%r): %s", ctype, url)
                return None
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_IMAGE_BYTES:
                logger.warning("Image fetch refused (too large: %s bytes): %s", declared, url)
                return None
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > _MAX_IMAGE_BYTES:
                    logger.warning("Image fetch aborted (exceeded %d bytes): %s", _MAX_IMAGE_BYTES, url)
                    return None
            return bytes(buf)
