# TAG Inventory

Multi-channel inventory system for a card shop. Single source of truth (local SQLite) syncs to TCGPlayer (PRO Seller CSV), eBay (Sell APIs), and Shopify POS (Admin API + webhooks).

- **Architecture plan:** [claude_documents/we-are-going-to-jazzy-river.md](claude_documents/we-are-going-to-jazzy-river.md)
- **Data flow demo:** [claude_documents/data-flow-demo.html](claude_documents/data-flow-demo.html) (open in a browser)
- **Test-account setup:** [claude_documents/test_accounts_setup.md](claude_documents/test_accounts_setup.md)
- **Phase 0 plan:** [claude_documents/plan_phase_0_skeleton.md](claude_documents/plan_phase_0_skeleton.md)

## Status

**Phase 0 (skeleton).** FastAPI app boots, SQLite + Alembic baseline in place, empty Admin UI shell. No feature code yet.

## Requirements

- Python 3.10 (locked — see [pyproject.toml](pyproject.toml))
- Windows or *nix dev box

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # (Windows; use source .venv/bin/activate on *nix)
pip install -e .[dev]
copy .env.example .env          # then fill in credentials as you create accounts
```

## Run the dev server

```bash
alembic upgrade head            # creates ./data/tag_inventory.db
uvicorn app.main:app --reload   # http://127.0.0.1:8000/
```

Visit:
- `/` — redirects to `/admin/`
- `/admin/` — placeholder dashboard
- `/healthz` — `{"status": "ok", "version": "..."}`
- `/api/docs` — OpenAPI Swagger UI

## Tests

```bash
pytest
```

## Migrations (when Phase 1+ adds models)

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
alembic downgrade -1            # roll back one
```

## Distribution (Phase 6)

Final deliverable is a PyInstaller one-folder bundle for Windows. The code is structured so resource lookups (templates, static, alembic/) work in both dev mode and frozen mode (see [app/paths.py](app/paths.py)).

## Project layout

```
app/
  main.py            # FastAPI entrypoint + create_app()
  config.py          # pydantic-settings, .env loading
  paths.py           # resource path resolution (PyInstaller-aware)
  db/
    base.py          # SQLAlchemy Base + engine factory (WAL on connect)
    session.py       # SessionLocal + get_session FastAPI dep
    models/          # filled in Phase 1
  routes/
    admin.py         # /admin
    health.py        # /healthz
  templates/         # Jinja2 + HTMX + Tailwind CDN
  static/
alembic/             # migrations
tests/
claude_documents/    # planning + design artifacts
claude_skills/       # (in-tree workflow notes; not runtime)
```
