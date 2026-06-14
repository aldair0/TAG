# TCGPlayer Sync — Two-Trigger Scheduler

**Date:** 2026-05-01

**Goal:** Replace the lone manual button with a dual-trigger ingest system — (1) a manual "Pull from TCGPlayer now" button intended to be pressed right after staff upload inventory, and (2) an APScheduler-driven 30-minute poll active during store hours (12:00–20:00 America/New_York, every day). Both paths share a single in-flight slot with one queued follow-up; auto-sync on/off is persisted across restarts and toggled from the Admin UI; stale-state and last-success-time are surfaced in the Admin UI.

**Architecture:** `BackgroundScheduler` (APScheduler 3.x) started in the FastAPI lifespan. One job (`tcgplayer_tick`, every `TCGPLAYER_SYNC_INTERVAL_MIN` minutes) gates on the auto-sync flag and `is_open_hours(now)` then routes through a process-wide `SyncCoordinator`. The manual button calls the same coordinator, so the two paths can never run concurrently. The coordinator runs `run_ingest()` on a background thread; if a request arrives while a run is active it claims the single queued slot; further requests are rejected.

**Tech Stack:** APScheduler 3.x (in-process, threaded), `zoneinfo` (stdlib) for store-hours math, existing FastAPI + SQLAlchemy 2 + Jinja2/HTMX. No new infra — still one Python process.

**Note on commits:** the project is currently not a git repo, so the "commit" steps in tasks are advisory commit points rather than actual `git commit` invocations. Adopt them as commit boundaries once `git init` happens.

---

## Scope

### In
- New dep: `apscheduler>=3.10,<4.0`
- `app_setting` key/value table (Alembic migration) — generic enough to host future flags
- `app/scheduler.py` — `SyncCoordinator`, `is_open_hours`, scheduler lifecycle
- `app/settings_store.py` — `get_setting` / `set_setting`
- FastAPI lifespan in `app/main.py` starts/stops the scheduler
- `/admin/sync/` page additions:
  - "Auto-sync: ON/OFF" toggle button (POST `/admin/sync/auto/toggle`)
  - "Last successful update: 2026-05-01 14:32 ET" — absolute, no countdown
  - "Status: Healthy / Running… / Stale (no success in 60+ min)"
  - "Pull from TCGPlayer now" rebadged manual button
- Manual `POST /admin/sync/run` rerouted through the coordinator (so it queues instead of racing the tick)
- Tests:
  - `is_open_hours` boundary cases (open day/closed day, exact open/close minute)
  - Coordinator: idle run, queued follow-up, third-request rejection, exception in `run_fn` doesn't jam the loop
  - Settings store: get default, get-after-set, update existing
  - Tick: skips when off, skips outside hours, runs when on + open
  - Route smoke: toggle endpoint, status panel renders

### Out (deliberately)
- `LiveTCGPlayerSource` — still raises `NotImplementedError`. Source resolution stays as `_default_fixture_path()`. Live source is a separate plan blocked on shop credentials.
- Holiday/closed-day overrides
- Per-field admin form for store hours (env-only this phase)
- SMS/email staleness alerts (banner only)
- Multiple queued follow-ups (single slot is enough — each `run_ingest` re-reads the whole source)

---

## Schema additions

```
app_setting
  key TEXT PRIMARY KEY                 -- e.g. 'tcgplayer_auto_sync'
  value TEXT NOT NULL                  -- 'on' | 'off' | future scalar
  updated_at TIMESTAMP NOT NULL DEFAULT now
```

Keys used this phase:
- `tcgplayer_auto_sync` → `"on"` (default) | `"off"`

---

## Config additions (`app/config.py`)

| Field | Default | Notes |
|---|---|---|
| `STORE_TIMEZONE` | `America/New_York` | EST/EDT — APScheduler & is_open_hours use this |
| `STORE_OPEN_TIME` | `12:00` | inclusive |
| `STORE_CLOSE_TIME` | `20:00` | exclusive (20:00:00 is closed) |
| `STORE_OPEN_DAYS` | `mon,tue,wed,thu,fri,sat,sun` | csv of 3-letter days, lowercased |
| `TCGPLAYER_SYNC_INTERVAL_MIN` | `30` | scheduler cadence in minutes |

The auto-sync on/off flag is intentionally **not** an env var — it lives in `app_setting` so the admin can flip it without a restart.

---

## Component layout

```
app/
  scheduler.py              # NEW — SyncCoordinator, is_open_hours, lifecycle, _tick
  settings_store.py         # NEW — get_setting / set_setting helpers
  db/models/
    app_setting.py          # NEW
    __init__.py             # MODIFIED — re-export AppSetting
  routes/
    sync.py                 # MODIFIED — toggle endpoint, status data, manual button → coordinator
  templates/admin/
    sync.html               # MODIFIED — status panel + toggle + relabeled button
  main.py                   # MODIFIED — FastAPI lifespan
  config.py                 # MODIFIED — store hours + interval fields
alembic/versions/
  <new>_app_setting.py      # NEW migration
tests/
  test_scheduler.py         # NEW — open-hours, coordinator, tick gating
  test_settings_store.py    # NEW
  test_admin_sync_routes.py # NEW (or expand existing test_admin_routes.py)
pyproject.toml              # MODIFIED — add apscheduler
```

---

## Tasks

Each task ends with a "Commit point" — keep changes scoped to one logical unit.

### Task 1 — APScheduler dependency

- [ ] Add `"apscheduler>=3.10,<4.0"` to `pyproject.toml [project] dependencies`.
- [ ] `& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"` to pull it.
- [ ] `& .\.venv\Scripts\pytest.exe` — should still be green.
- [ ] **Commit point.**

### Task 2 — `app_setting` model + migration

- [ ] Create `app/db/models/app_setting.py`:

```python
from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_setting"
    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] Re-export from `app/db/models/__init__.py`.
- [ ] Generate migration: `& .\.venv\Scripts\alembic.exe revision --autogenerate -m "phase 4: app_setting kv table"`.
- [ ] Inspect generated migration: should contain a single `op.create_table('app_setting', …)` with PK `key`. Trim any extras alembic invents.
- [ ] `& .\.venv\Scripts\alembic.exe upgrade head` — confirm head advances; SQLite shows the new table.
- [ ] **Commit point.**

### Task 3 — `settings_store` (TDD)

- [ ] Write failing tests in `tests/test_settings_store.py`:

```python
import pytest

from app.settings_store import get_setting, set_setting


def test_get_default_when_missing(session):
    assert get_setting(session, "absent.key", default="x") == "x"


def test_set_then_get(session):
    set_setting(session, "k", "v")
    assert get_setting(session, "k", default="x") == "v"


def test_set_updates_existing(session):
    set_setting(session, "k", "first")
    set_setting(session, "k", "second")
    assert get_setting(session, "k") == "second"
```

(`session` fixture: re-use whatever conftest.py already supplies — see `tests/conftest.py`.)

- [ ] Run; expect `ImportError` on the module.
- [ ] Implement `app/settings_store.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting


def get_setting(session: Session, key: str, *, default: str | None = None) -> str | None:
    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()
```

- [ ] Tests pass.
- [ ] **Commit point.**

### Task 4 — `is_open_hours` predicate (TDD)

- [ ] Tests in `tests/test_scheduler.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler import is_open_hours

ET = ZoneInfo("America/New_York")
ALL_DAYS = "mon,tue,wed,thu,fri,sat,sun"


def _et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)


def test_open_at_noon():
    assert is_open_hours(_et(2026, 5, 1, 12, 0), tz="America/New_York",
                         open_t="12:00", close_t="20:00", days=ALL_DAYS)


def test_closed_one_minute_before_noon():
    assert not is_open_hours(_et(2026, 5, 1, 11, 59), tz="America/New_York",
                             open_t="12:00", close_t="20:00", days=ALL_DAYS)


def test_close_time_is_exclusive():
    assert not is_open_hours(_et(2026, 5, 1, 20, 0), tz="America/New_York",
                             open_t="12:00", close_t="20:00", days=ALL_DAYS)
    assert is_open_hours(_et(2026, 5, 1, 19, 59), tz="America/New_York",
                         open_t="12:00", close_t="20:00", days=ALL_DAYS)


def test_excluded_day_skipped():
    sunday = _et(2026, 5, 3, 14, 0)  # 2026-05-03 is Sunday
    assert not is_open_hours(sunday, tz="America/New_York",
                             open_t="12:00", close_t="20:00",
                             days="mon,tue,wed,thu,fri,sat")


def test_naive_datetime_assumed_in_store_tz():
    naive = datetime(2026, 5, 1, 14, 0)
    assert is_open_hours(naive, tz="America/New_York",
                         open_t="12:00", close_t="20:00", days=ALL_DAYS)
```

- [ ] Run; ImportError expected.
- [ ] Implement in `app/scheduler.py`:

```python
from __future__ import annotations

from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _hhmm(s: str) -> _time:
    h, m = s.split(":")
    return _time(int(h), int(m))


def is_open_hours(now: datetime, *, tz: str, open_t: str, close_t: str, days: str) -> bool:
    z = ZoneInfo(tz)
    now = now.replace(tzinfo=z) if now.tzinfo is None else now.astimezone(z)
    allowed = {d.strip().lower() for d in days.split(",") if d.strip()}
    if _DAYS[now.weekday()] not in allowed:
        return False
    return _hhmm(open_t) <= now.time() < _hhmm(close_t)
```

- [ ] Tests pass.
- [ ] **Commit point.**

### Task 5 — `SyncCoordinator` (TDD)

- [ ] Tests in `tests/test_scheduler.py`:

```python
import threading

from app.scheduler import RunStatus, SyncCoordinator


def test_idle_request_runs():
    runs = []
    coord = SyncCoordinator(run_fn=lambda: runs.append(1))
    status = coord.request()
    assert coord.wait_idle(timeout=2)
    assert status == RunStatus.STARTED
    assert runs == [1]


def test_concurrent_request_queues_one():
    started = threading.Event()
    release = threading.Event()
    runs = []

    def slow():
        started.set()
        release.wait(timeout=2)
        runs.append(1)

    coord = SyncCoordinator(run_fn=slow)
    s1 = coord.request()
    assert started.wait(timeout=1)
    s2 = coord.request()  # queued
    s3 = coord.request()  # rejected (slot already taken)
    release.set()
    assert coord.wait_idle(timeout=3)
    assert (s1, s2, s3) == (RunStatus.STARTED, RunStatus.QUEUED, RunStatus.REJECTED)
    assert len(runs) == 2  # original + queued


def test_run_fn_exception_does_not_jam_coordinator():
    runs = []

    def fn():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")

    coord = SyncCoordinator(run_fn=fn)
    coord.request()
    assert coord.wait_idle(timeout=2)
    coord.request()
    assert coord.wait_idle(timeout=2)
    assert len(runs) == 2
```

- [ ] Implement (append to `app/scheduler.py`):

```python
import enum
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class RunStatus(enum.Enum):
    STARTED = "started"
    QUEUED = "queued"
    REJECTED = "rejected"


class SyncCoordinator:
    """Single-slot run coordinator with one queued follow-up.

    The first request runs immediately on a background thread. A second
    request while the first is in flight claims the single queued slot.
    A third (or further) request is rejected — there's nothing to gain
    from queueing 3 deep when each run re-reads the whole source.
    """

    def __init__(self, run_fn: Callable[[], None]) -> None:
        self._run_fn = run_fn
        self._lock = threading.Lock()
        self._running = False
        self._queued = False
        self._idle = threading.Event()
        self._idle.set()

    def request(self) -> RunStatus:
        with self._lock:
            if not self._running:
                self._running = True
                self._idle.clear()
                threading.Thread(target=self._loop, daemon=True).start()
                return RunStatus.STARTED
            if not self._queued:
                self._queued = True
                return RunStatus.QUEUED
            return RunStatus.REJECTED

    def state(self) -> dict:
        with self._lock:
            return {"running": self._running, "queued": self._queued}

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout=timeout)

    def _loop(self) -> None:
        try:
            while True:
                try:
                    self._run_fn()
                except Exception:
                    logger.exception("Sync run failed inside coordinator")
                with self._lock:
                    if not self._queued:
                        self._running = False
                        self._idle.set()
                        return
                    self._queued = False  # consume the queued slot, loop
        except Exception:
            logger.exception("Coordinator loop crashed")
            with self._lock:
                self._running = False
                self._queued = False
                self._idle.set()
```

- [ ] Tests pass.
- [ ] **Commit point.**

### Task 6 — Tick + scheduler lifecycle

- [ ] Tests in `tests/test_scheduler.py`:

```python
from app import scheduler as sch
from app.settings_store import set_setting


def test_tick_skips_when_disabled(session, monkeypatch):
    set_setting(session, "tcgplayer_auto_sync", "off")
    session.commit()
    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: True)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == []


def test_tick_skips_outside_hours(session, monkeypatch):
    set_setting(session, "tcgplayer_auto_sync", "on")
    session.commit()
    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: False)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == []


def test_tick_runs_when_on_and_open(session, monkeypatch):
    set_setting(session, "tcgplayer_auto_sync", "on")
    session.commit()
    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: True)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == [1]
```

- [ ] Add config fields to `app/config.py`:

```python
store_timezone: str = Field(default="America/New_York", alias="STORE_TIMEZONE")
store_open_time: str = Field(default="12:00", alias="STORE_OPEN_TIME")
store_close_time: str = Field(default="20:00", alias="STORE_CLOSE_TIME")
store_open_days: str = Field(
    default="mon,tue,wed,thu,fri,sat,sun", alias="STORE_OPEN_DAYS"
)
tcgplayer_sync_interval_min: int = Field(
    default=30, alias="TCGPLAYER_SYNC_INTERVAL_MIN"
)
```

- [ ] Append to `app/scheduler.py`:

```python
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db.session import SessionLocal
from app.settings_store import get_setting

_scheduler: BackgroundScheduler | None = None
_coordinator: SyncCoordinator | None = None


def _resolve_source():
    """Single chokepoint for source resolution. Swap to LiveTCGPlayerSource
    here when credentials and Playwright auth land."""
    from app.routes.sync import _default_fixture_path  # imported late to avoid cycle
    from app.sync.tcgplayer import FixtureTCGPlayerSource

    fixture: Path = _default_fixture_path()
    if not fixture.exists():
        return None
    return FixtureTCGPlayerSource(fixture)


def _run_ingest_for_scheduler() -> None:
    from app.sync.tcgplayer import run_ingest

    source = _resolve_source()
    if source is None:
        logger.warning("No TCGPlayer source available — skipping run")
        return
    with SessionLocal() as session:
        run_ingest(source, session)


def coordinator() -> SyncCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = SyncCoordinator(run_fn=_run_ingest_for_scheduler)
    return _coordinator


def _request_run() -> RunStatus:
    """Indirection so tests can monkeypatch without touching the global."""
    return coordinator().request()


def _tick(session_factory=SessionLocal) -> None:
    with session_factory() as s:
        flag = get_setting(s, "tcgplayer_auto_sync", default="on")
    if flag != "on":
        return
    if not is_open_hours(
        datetime.now(),
        tz=settings.store_timezone,
        open_t=settings.store_open_time,
        close_t=settings.store_close_time,
        days=settings.store_open_days,
    ):
        return
    _request_run()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    s = BackgroundScheduler(timezone=settings.store_timezone)
    s.add_job(
        _tick,
        IntervalTrigger(minutes=settings.tcgplayer_sync_interval_min),
        id="tcgplayer_tick",
        coalesce=True,
        max_instances=1,
    )
    s.start()
    _scheduler = s
    logger.info(
        "Scheduler started: tick every %d min, hours %s–%s %s",
        settings.tcgplayer_sync_interval_min,
        settings.store_open_time,
        settings.store_close_time,
        settings.store_timezone,
    )
    return s


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
```

- [ ] Tests pass.
- [ ] **Commit point.**

### Task 7 — FastAPI lifespan wiring

- [ ] Modify `app/main.py`:

```python
from contextlib import asynccontextmanager

from app.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


def create_app() -> FastAPI:
    logging.basicConfig(level=settings.log_level)
    app = FastAPI(
        title="TAG Inventory",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,   # ← add this
    )
    # … rest unchanged …
```

- [ ] Restart server, watch logs for "Scheduler started: tick every 30 min, hours 12:00–20:00 America/New_York".
- [ ] Existing tests: keep green. Add a small lifespan smoke test if conftest already supports `TestClient` with lifespan; otherwise rely on Task 8's manual verification.
- [ ] **Commit point.**

### Task 8 — `/admin/sync/` route + UI

**8a — toggle endpoint**

- [ ] Add to `app/routes/sync.py`:

```python
from datetime import datetime, timedelta, timezone

from app.scheduler import coordinator
from app.settings_store import get_setting, set_setting


@router.post("/auto/toggle", response_class=HTMLResponse)
def toggle_auto_sync(session: Session = Depends(get_session)) -> RedirectResponse:
    current = get_setting(session, "tcgplayer_auto_sync", default="on")
    set_setting(session, "tcgplayer_auto_sync", "off" if current == "on" else "on")
    session.commit()
    return RedirectResponse(url="/admin/sync/", status_code=303)
```

**8b — status data on the index route**

- [ ] In `sync_index`, before the `TemplateResponse`:

```python
auto_sync = get_setting(session, "tcgplayer_auto_sync", default="on")

last_success = session.execute(
    select(SyncRun)
    .where(SyncRun.error.is_(None), SyncRun.ended_at.is_not(None))
    .order_by(SyncRun.ended_at.desc())
    .limit(1)
).scalar_one_or_none()

stale_threshold = timedelta(minutes=2 * settings.tcgplayer_sync_interval_min)
now_utc = datetime.now(timezone.utc)
is_stale = last_success is None or (now_utc - last_success.ended_at) > stale_threshold

coord_state = coordinator().state()
```

Pass into the template:

```python
return templates.TemplateResponse(
    request,
    "admin/sync.html",
    {
        "title": "Sync",
        "phase": "scheduler",
        "runs": runs,
        "fixture_path": _default_fixture_path(),
        "pending_counts": pending_counts,
        "auto_sync": auto_sync,
        "last_success": last_success,
        "is_stale": is_stale,
        "coord_state": coord_state,
        "interval_min": settings.tcgplayer_sync_interval_min,
        "store_hours": f"{settings.store_open_time}-{settings.store_close_time} {settings.store_timezone}",
    },
)
```

**8c — manual button uses coordinator**

- [ ] Replace the body of `sync_run_tcgplayer`:

```python
@router.post("/run", response_class=HTMLResponse)
def sync_run_tcgplayer() -> RedirectResponse:
    coordinator().request()
    return RedirectResponse(url="/admin/sync/", status_code=303)
```

(Removes the in-route fixture-resolution + try/except — both now live in `app/scheduler.py::_run_ingest_for_scheduler`.)

**8d — template**

- [ ] Replace the existing TCGPlayer section of `app/templates/admin/sync.html` (or insert above the runs table) with:

```html
<section class="bg-white rounded-2xl border border-sky-100 p-5 mb-4 space-y-4">
  <div class="flex items-center justify-between">
    <div>
      <h2 class="text-lg font-semibold text-slate-900">TCGPlayer sync</h2>
      <p class="text-xs text-slate-400">Every {{ interval_min }} min, {{ store_hours }}</p>
    </div>
    <form method="post" action="/admin/sync/auto/toggle">
      <button class="px-4 py-2 rounded-full text-sm font-bold transition-all
        {% if auto_sync == 'on' %}bg-emerald-100 text-emerald-700 hover:bg-emerald-200
        {% else %}bg-rose-100 text-rose-700 hover:bg-rose-200{% endif %}">
        Auto-sync: {{ auto_sync|upper }}
      </button>
    </form>
  </div>

  <div class="grid grid-cols-2 gap-3 text-sm">
    <div>
      <p class="text-slate-400">Last successful update</p>
      <p class="text-slate-900 font-medium tabular-nums">
        {% if last_success %}{{ last_success.ended_at.strftime('%Y-%m-%d %H:%M') }} UTC
        {% else %}—{% endif %}
      </p>
    </div>
    <div>
      <p class="text-slate-400">Status</p>
      {% if is_stale %}
      <p class="text-rose-600 font-semibold">⚠ Stale (no success in {{ 2 * interval_min }}+ min)</p>
      {% elif coord_state.running %}
      <p class="text-sky-600 font-semibold">Running…{% if coord_state.queued %} (queued: 1){% endif %}</p>
      {% else %}
      <p class="text-emerald-600 font-semibold">Healthy</p>
      {% endif %}
    </div>
  </div>

  <form method="post" action="/admin/sync/run">
    <button class="w-full py-3 rounded-2xl bg-sky-500 hover:bg-sky-600 text-white font-bold text-base shadow-sm transition-all active:scale-98">
      Pull from TCGPlayer now
    </button>
    <p class="text-xs text-slate-400 mt-2 text-center">
      Press right after uploading new inventory to TCGPlayer.
    </p>
  </form>
</section>
```

**8e — route smoke tests**

- [ ] In `tests/test_admin_sync_routes.py` (new) or extend `test_admin_routes.py`:

```python
def test_sync_index_renders_status_panel(client):
    r = client.get("/admin/sync/")
    assert r.status_code == 200
    assert b"Auto-sync:" in r.content
    assert b"Pull from TCGPlayer now" in r.content


def test_toggle_flips_auto_sync(client, session):
    r = client.post("/admin/sync/auto/toggle", follow_redirects=False)
    assert r.status_code == 303
    from app.settings_store import get_setting
    assert get_setting(session, "tcgplayer_auto_sync", default="on") == "off"


def test_manual_run_returns_303(client):
    r = client.post("/admin/sync/run", follow_redirects=False)
    assert r.status_code == 303
```

- [ ] All tests green.
- [ ] **Commit point.**

### Task 9 — End-to-end manual verification

Cashier-facing UX is the sharp end of this work; verify it manually before declaring done.

- [ ] `& .\.venv\Scripts\pytest.exe` — all green.
- [ ] `& .\.venv\Scripts\uvicorn.exe app.main:app --reload`. Server log line "Scheduler started: tick every 30 min, hours 12:00–20:00 America/New_York" should appear within a second.
- [ ] Navigate to http://127.0.0.1:8000/admin/sync/. Status panel renders with: "Auto-sync: ON" badge (emerald), "Last successful update: <timestamp>" (or em-dash on first boot), "Status: Healthy".
- [ ] Click **Pull from TCGPlayer now**. Page reloads. Confirm:
    - A new `sync_run` row appears in the runs table at the bottom.
    - "Last successful update" timestamp updated.
    - "Status" stays Healthy.
- [ ] Click **Auto-sync: ON** to flip → "Auto-sync: OFF" (rose). Wait `interval_min + 1` minutes; confirm no new sync_run rows. Flip back → ON.
- [ ] Force a "stale" state: temporarily edit `STORE_CLOSE_TIME=00:01` in `.env` and restart, then update an existing successful sync's `ended_at` to 2h ago via SQLite (or just wait); reload `/admin/sync/`; the Status pill goes red with "⚠ Stale (no success in 60+ min)". Restore `.env`.
- [ ] (Concurrency check) Double-click **Pull from TCGPlayer now** in quick succession; logs should show one run, then a second run begins immediately when the first finishes (the queued slot). A third rapid click is dropped — no log entry.
- [ ] Outside open hours: temporarily set `STORE_CLOSE_TIME=12:01`, restart, wait an interval — log shows the tick fired but `_request_run` was not called. Restore.
- [ ] Restart the server with auto-sync OFF. After restart the toggle still reads OFF (persistence verified).

---

## Definition of done

- [ ] APScheduler started in lifespan; "Scheduler started" log line on every boot.
- [ ] Manual click and scheduled tick share one in-flight slot; `test_concurrent_request_queues_one` proves single queued slot + rejection.
- [ ] `is_open_hours` rejects 11:59 ET, accepts 12:00 ET, rejects 20:00 ET, accepts 19:59 ET; rejects days outside `STORE_OPEN_DAYS`.
- [ ] Auto-sync flag persisted in `app_setting`; survives restart; toggleable from `/admin/sync/`.
- [ ] `/admin/sync/` shows: Auto-sync ON/OFF, last successful update (absolute timestamp, no countdown), Healthy / Running / Stale status, "Pull from TCGPlayer now" manual button, store-hours line.
- [ ] Stale banner appears when `now - last_success.ended_at > 2 × interval_min`.
- [ ] All existing tests still pass; new tests added for predicate + coordinator + tick gating + routes.

---

## Open follow-ups (not this plan)

- **Live source.** `LiveTCGPlayerSource` is still NotImplementedError. Once shop credentials are available, swap `_resolve_source()` in `app/scheduler.py` to construct `LiveTCGPlayerSource(...)`. Add the Playwright cookie-capture flow + a "Re-auth" admin button. Cookie-expiry detection should write a special `sync_run.error` value the status panel can highlight.
- **Editable store hours.** If the owner wants to change hours without touching `.env`, promote the four `STORE_*` settings from env-only to admin-form-editable rows in `app_setting` (`store.timezone`, `store.open_time`, etc.) and read them at tick time. Trade-off: scheduler interval is still `IntervalTrigger`-only and won't respect mid-run changes until restart unless we reschedule the job on toggle.
- **Hot-reschedule on interval change.** If `TCGPLAYER_SYNC_INTERVAL_MIN` becomes admin-editable, call `_scheduler.reschedule_job("tcgplayer_tick", trigger=IntervalTrigger(minutes=new))` on save.
- **Holiday closures.** If the shop ever needs override dates (Christmas, etc.), add a `closed_dates` setting and check it inside `is_open_hours`.
