import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler import RunStatus, SyncCoordinator, is_open_hours

ET = ZoneInfo("America/New_York")
ALL_DAYS = "mon,tue,wed,thu,fri,sat,sun"


def _et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)


def test_open_at_noon():
    assert is_open_hours(
        _et(2026, 5, 1, 12, 0),
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days=ALL_DAYS,
    )


def test_closed_one_minute_before_noon():
    assert not is_open_hours(
        _et(2026, 5, 1, 11, 59),
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days=ALL_DAYS,
    )


def test_close_time_is_exclusive():
    assert not is_open_hours(
        _et(2026, 5, 1, 20, 0),
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days=ALL_DAYS,
    )
    assert is_open_hours(
        _et(2026, 5, 1, 19, 59),
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days=ALL_DAYS,
    )


def test_excluded_day_skipped():
    # 2026-05-03 is a Sunday; days list omits sun.
    sunday = _et(2026, 5, 3, 14, 0)
    assert not is_open_hours(
        sunday,
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days="mon,tue,wed,thu,fri,sat",
    )


def test_naive_datetime_assumed_in_store_tz():
    naive = datetime(2026, 5, 1, 14, 0)
    assert is_open_hours(
        naive,
        tz="America/New_York",
        open_t="12:00",
        close_t="20:00",
        days=ALL_DAYS,
    )


# ---- SyncCoordinator ---------------------------------------------------


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
    s2 = coord.request()  # queued (slot taken)
    s3 = coord.request()  # rejected (slot already taken)
    release.set()
    assert coord.wait_idle(timeout=3)
    assert (s1, s2, s3) == (RunStatus.STARTED, RunStatus.QUEUED, RunStatus.REJECTED)
    assert len(runs) == 2  # original + queued follow-up


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


def test_state_reflects_running_and_queued():
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=2)

    coord = SyncCoordinator(run_fn=slow)
    assert coord.state() == {"running": False, "queued": False}
    coord.request()
    assert started.wait(timeout=1)
    assert coord.state() == {"running": True, "queued": False}
    coord.request()
    assert coord.state() == {"running": True, "queued": True}
    release.set()
    assert coord.wait_idle(timeout=3)
    assert coord.state() == {"running": False, "queued": False}


# ---- _tick gating ------------------------------------------------------


def test_tick_skips_when_disabled(session, monkeypatch):
    from app import scheduler as sch
    from app.settings_store import set_setting

    set_setting(session, "tcgplayer_auto_sync", "off")
    session.commit()

    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: True)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == []


def test_tick_skips_outside_hours(session, monkeypatch):
    from app import scheduler as sch
    from app.settings_store import set_setting

    set_setting(session, "tcgplayer_auto_sync", "on")
    session.commit()

    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: False)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == []


def test_tick_runs_when_on_and_open(session, monkeypatch):
    from app import scheduler as sch
    from app.settings_store import set_setting

    set_setting(session, "tcgplayer_auto_sync", "on")
    session.commit()

    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: True)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == [1]


def test_tick_default_when_setting_missing_is_on(session, monkeypatch):
    """If no row exists for tcgplayer_auto_sync, default behavior is ON."""
    from app import scheduler as sch

    calls = []
    monkeypatch.setattr(sch, "is_open_hours", lambda *a, **k: True)
    monkeypatch.setattr(sch, "_request_run", lambda: calls.append(1))
    sch._tick(session_factory=lambda: session)
    assert calls == [1]
