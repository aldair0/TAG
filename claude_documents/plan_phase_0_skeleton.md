# Phase 0 Implementation Plan — Skeleton

**Goal:** A FastAPI app that boots locally on the dev machine, has SQLite + Alembic ready for Phase 1 schema, serves a placeholder Admin UI, and is git-tracked. Shop-PC deployment (Caddy, NSSM, PyInstaller bundle) is **deferred to Phase 6**; what we need now is a working dev environment.

**Tech decisions (confirmed):** Python 3.10 + pip + venv. Final distribution as a PyInstaller one-folder bundle, but we don't bundle yet — we just write code that *will* bundle cleanly (resource paths via `importlib.resources` or `sys._MEIPASS`, configurable DB location, no `__file__`-based hacks).

**What's NOT in Phase 0:**
- Caddy / LAN HTTPS (Phase 0b later)
- Windows service install (Phase 6)
- PyInstaller actual build (Phase 6 — but we structure code to support it)
- Any Phase 1+ feature work (TCGPlayer ingest, eBay, Shopify, POS UI)
- Auth (single-user local app; defer)

---

## Repo layout (target)

```
TAG_Inventory/
  app/
    __init__.py
    main.py            # FastAPI entrypoint
    config.py          # pydantic-settings, .env loading
    paths.py           # resource path resolution (PyInstaller-aware)
    db/
      __init__.py
      base.py          # SQLAlchemy declarative_base, engine factory
      session.py       # session/connection helpers
      models/          # (empty in Phase 0; Phase 1 fills it)
        __init__.py
    routes/
      __init__.py
      admin.py         # /admin index page
      health.py        # /healthz
    templates/
      base.html
      admin/
        index.html
    static/
      css/
        app.css        # tiny — using Tailwind CDN initially
  alembic/
    env.py
    versions/          # (empty)
  tests/
    __init__.py
    test_smoke.py
  alembic.ini
  pyproject.toml
  .env.example
  .gitignore
  README.md
  claude_documents/    # already here (preserved)
  claude_skills/       # already here (preserved)
```

---

## Tasks

### Task 1: Git init + .gitignore
- `git init` in `c:\TAG_Inventory\`
- Write `.gitignore` covering `.env`, `.venv/`, `__pycache__/`, `*.db`, `*.db-journal`, `dist/`, `build/`, `*.spec`
- Initial commit AFTER scaffolding so the first commit has the whole skeleton

### Task 2: Python project files
- `pyproject.toml` — project metadata, dependencies, dev deps (no build-system entry yet; we'll add hatchling later if needed for packaging)
- `.env.example` — template for credentials (read by `app/config.py`)

Dependencies (pinned to versions known-good with Python 3.10):
- `fastapi`
- `uvicorn[standard]`
- `sqlalchemy>=2.0`
- `alembic`
- `pydantic-settings`
- `jinja2`
- `python-dotenv`
- `httpx`
- (later phases: `pandas`, `apscheduler`, `playwright`, `shopifyapi`)

Dev deps:
- `pytest`
- `pytest-asyncio`
- `httpx` (for TestClient)

### Task 3: app/config.py + paths.py
- `Settings` class via `pydantic-settings`, loads `.env`
- Initial settings: `DATABASE_URL` (default `sqlite:///./data/tag_inventory.db`), `LOG_LEVEL`, `ADMIN_BASE_PATH` (default `/admin`)
- `paths.py`: `resource_path(rel)` function that handles both dev and frozen (PyInstaller) modes. We'll use it for templates/static lookups.

### Task 4: app/db/ baseline
- `base.py` — `Base = declarative_base()`, `engine = create_engine(...)` from settings, WAL pragma on connect
- `session.py` — `SessionLocal`, `get_session()` dependency for FastAPI

### Task 5: Alembic init + first (empty) migration
- `alembic init alembic` (vendor in)
- Edit `alembic/env.py` to import our `Base` metadata and use settings-driven URL
- Generate an empty initial migration (so the migration chain exists)

### Task 6: app/main.py + routes
- FastAPI app factory with mounted templates/static
- `/healthz` returns `{"status": "ok"}`
- `/admin` renders `admin/index.html` with a placeholder body and the layout shell
- Templates: `base.html` with Tailwind CDN, basic responsive grid; `admin/index.html` extends it

### Task 7: Smoke test
- `tests/test_smoke.py` — TestClient hits `/healthz` and `/admin`, expects 200

### Task 8: README
- Brief: how to set up venv, install deps, run `alembic upgrade head`, run `uvicorn`, run pytest. Link to architecture plan and test-account guide.

### Task 9: Verify
- `python -m venv .venv && .venv\Scripts\activate && pip install -e .[dev]`
- `alembic upgrade head` — DB file appears at `data/tag_inventory.db`, migration chain at "head"
- `uvicorn app.main:app --reload` — server starts, `/admin` and `/healthz` load
- `pytest` — passes

### Task 10: First commit
- `git add -A && git commit -m "Phase 0: skeleton scaffold"`

---

## Definition of done

- [ ] Repo is git-tracked
- [ ] `pip install -e .[dev]` succeeds on Python 3.10
- [ ] `alembic upgrade head` creates the DB file and applies the empty initial migration
- [ ] `uvicorn app.main:app` boots and `/admin` returns 200 with the placeholder layout
- [ ] `pytest` runs and the smoke test passes
- [ ] First git commit recorded
- [ ] No Phase 1+ feature code exists yet

After this: Phase 1 plan (TCGPlayer ingestion).
