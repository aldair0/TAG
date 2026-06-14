from __future__ import annotations

import os
import tempfile
from pathlib import Path
from collections.abc import Iterator

# Force the test DB to a per-test-session file so we never touch dev data.
_TEST_DB = Path(tempfile.gettempdir()) / "tag_inventory_test.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"

# Keep the BackgroundScheduler dormant during tests, and make the
# coordinator's ingest run_fn a no-op so route smoke tests don't fan
# out to real CSV parsing + image fetches on background threads.
os.environ["TAG_DISABLE_SCHEDULER"] = "1"
os.environ["TAG_DISABLE_INGEST"] = "1"
# Default to skipping the image-fetch block during direct run_ingest()
# calls in tests. Otherwise the fetcher would lazy-create a real
# httpx-backed MarketplaceSearchClient and hit the live API for any
# product without a cached marketplace_product_id. Tests that
# specifically exercise image-fetching (tests/test_product_images.py)
# work via mocked collaborators and don't go through run_ingest.
os.environ.setdefault("TAG_SKIP_IMAGE_FETCH", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.base import Base, engine
from app.db.session import SessionLocal, get_session
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        # Clean every row so tests don't bleed.
        s.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()
        s.close()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    """TestClient with the get_session dependency overridden to share the
    test session, so route tests see in-flight test data.
    """

    def _override() -> Iterator[Session]:
        try:
            yield session
        finally:
            pass  # outer fixture handles cleanup

    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
