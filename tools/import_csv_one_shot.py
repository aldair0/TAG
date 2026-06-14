"""One-shot driver for the initial bulk CSV import.

Run with:

    set TAG_SKIP_IMAGE_FETCH=1
    .\.venv\Scripts\python.exe tools\import_csv_one_shot.py

Resolves source via app.scheduler._resolve_source (which now picks up
data/csv/tcgplayer_pricing.csv) and runs through run_ingest with the
prod-ish DB session.
"""

from __future__ import annotations

import logging
import time

from app.db.session import SessionLocal
from app.scheduler import _resolve_source
from app.sync.tcgplayer import run_ingest


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger("import_csv")

    source = _resolve_source()
    if source is None:
        log.error("No source resolved — neither data/csv/ nor test_data/ has a CSV.")
        raise SystemExit(2)
    log.info("Source: %s", source.path)

    t0 = time.perf_counter()
    with SessionLocal() as session:
        run = run_ingest(source, session)
        # Capture before the session closes — accessing ORM attrs on a
        # detached instance raises DetachedInstanceError.
        snapshot = (
            run.id, run.rows_seen, run.rows_inserted, run.rows_updated, run.error,
        )

    elapsed = time.perf_counter() - t0
    log.info(
        "Done in %.1fs — sync_run id=%s rows_seen=%s rows_inserted=%s rows_updated=%s error=%s",
        elapsed, *snapshot,
    )


if __name__ == "__main__":
    main()
