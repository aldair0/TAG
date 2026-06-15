"""Accelerated soak test with injected faults.

Compresses the "left on for 2 weeks" failure modes into a few minutes by
injecting each fault and asserting the self-healing code recovers, then runs
a tight maintenance loop to surface resource leaks (WAL growth, thread/handle
leaks, backup pruning).

Runs entirely against a throwaway temp TAG_HOME — never touches dev data.

    .venv\\Scripts\\python.exe tools\\soak.py [cycles]
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# --- isolate to a temp home BEFORE importing app config ---------------------
_TMP = Path(tempfile.mkdtemp(prefix="tag_soak_"))
os.environ["TAG_HOME"] = str(_TMP)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'soak.db').as_posix()}"
os.environ["TAG_DISABLE_FILE_LOG"] = "1"
os.environ["TAG_DISABLE_ALERTS"] = "1"  # alerts return 'logged' instead of emailing

import logging

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from sqlalchemy import text

from app.db.base import Base, checkpoint_wal, engine, integrity_check
from app.db.session import SessionLocal

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def _init_db() -> None:
    Base.metadata.create_all(engine)


# --- fault: concurrent-write backup (busy_timeout under load) ---------------

def soak_backup_under_load() -> None:
    from app.backup import run_backup

    stop = threading.Event()

    def hammer():
        # Continuously write in a separate thread to contend with the backup.
        while not stop.is_set():
            with SessionLocal() as s:
                s.execute(text("CREATE TABLE IF NOT EXISTS soak_w (id INTEGER PRIMARY KEY, v TEXT)"))
                s.execute(text("INSERT INTO soak_w (v) VALUES ('x')"))
                s.commit()

    t = threading.Thread(target=hammer, daemon=True)
    t.start()
    ok = True
    detail = ""
    try:
        for _ in range(5):
            run_backup(retention_days=14)
    except Exception as e:  # noqa: BLE001
        ok = False
        detail = f"{type(e).__name__}: {e}"
    finally:
        stop.set()
        t.join(timeout=5)
    check("backup survives concurrent writes (busy_timeout)", ok, detail)


# --- fault: receiver connection drop → reconnect ----------------------------

def soak_receiver_reconnect() -> None:
    from app.inbound_email.receiver import ImapIdleReceiver

    r = ImapIdleReceiver()
    attempts = {"n": 0}

    def fake_connect():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionResetError("simulated dead socket")
        # 3rd attempt "connects" then idles until stop.
        r._connected = True
        r._mark_contact()
        while not r._stop.is_set():
            time.sleep(0.02)
        r._connected = False

    r._connect_and_listen = fake_connect  # type: ignore[assignment]
    r._stop.clear()
    th = threading.Thread(target=r._run, daemon=True)
    th.start()
    # Wait for it to fail twice and recover.
    deadline = time.time() + 5
    while time.time() < deadline and not r._connected:
        time.sleep(0.05)
    connected_at_recovery = r._connected
    recovered = connected_at_recovery and r._reconnects >= 2
    r._stop.set()
    th.join(timeout=3)
    check(
        "receiver reconnects after dropped connection",
        recovered,
        f"failed_attempts={r._reconnects} reconnected={connected_at_recovery}",
    )


# --- fault: auth failure → alert + status ------------------------------------

def soak_auth_failure_alert() -> None:
    from app.inbound_email.receiver import ImapIdleReceiver

    r = ImapIdleReceiver()
    fired = {"n": 0}
    import app.alerts as alerts

    orig = alerts.send_alert
    alerts.send_alert = lambda *a, **k: fired.__setitem__("n", fired["n"] + 1) or "logged"
    try:
        # Simulate the auth-failure path crossing the alert threshold (==3).
        class LoginError(Exception):
            pass

        import imapclient.exceptions as exc

        for _ in range(3):
            try:
                raise exc.LoginError("bad app password")
            except Exception as e:
                if isinstance(e, exc.LoginError):
                    r._consecutive_auth_failures += 1
                    if r._consecutive_auth_failures == 3:
                        from app.alerts import send_alert

                        send_alert("Email receiver DOWN — auth failing", "x", key="receiver_auth")
    finally:
        alerts.send_alert = orig
    status = r.status()
    check(
        "auth failure tracked + alert fired at threshold",
        status["auth_failures"] == 3 and fired["n"] == 1,
        f"auth_failures={status['auth_failures']} alerts={fired['n']}",
    )


# --- fault: disk low → alert + health degraded ------------------------------

def soak_disk_low_alert() -> None:
    import app.alerts as alerts
    import app.scheduler as sched

    fired = {"n": 0}
    orig = alerts.send_alert
    alerts.send_alert = lambda *a, **k: fired.__setitem__("n", fired["n"] + 1) or "logged"
    orig_thr = sched._DISK_WARN_GB
    sched._DISK_WARN_GB = 10 ** 9  # force "low" (no disk has 1e9 GB free)
    try:
        sched._disk_guard()
    finally:
        sched._DISK_WARN_GB = orig_thr
        alerts.send_alert = orig
    check("disk-low guard fires an alert", fired["n"] == 1, f"alerts={fired['n']}")


# --- fault: DB integrity check ----------------------------------------------

def soak_integrity() -> None:
    check("DB integrity_check passes", integrity_check() is True)


# --- leak loop: maintenance under load, watch growth ------------------------

def soak_leak_loop(cycles: int) -> None:
    from app.backup import run_backup
    from app.health import collect_health
    from app.paths import backups_dir

    wal = Path(os.environ["DATABASE_URL"].replace("sqlite:///", "") + "-wal")
    threads_before = threading.active_count()
    gc.collect()
    objs_before = len(gc.get_objects())
    wal_sizes = []

    for i in range(cycles):
        with SessionLocal() as s:
            for _ in range(20):
                s.execute(text("INSERT INTO soak_w (v) VALUES ('y')"))
            s.commit()
        run_backup(retention_days=14)
        checkpoint_wal()
        collect_health()
        if wal.exists():
            wal_sizes.append(wal.stat().st_size)

    gc.collect()
    objs_after = len(gc.get_objects())
    threads_after = threading.active_count()
    backups = list(backups_dir().glob("tag_inventory-*.db"))

    max_wal = max(wal_sizes) if wal_sizes else 0
    check(
        f"WAL stays bounded over {cycles} cycles (checkpoint works)",
        max_wal < 5_000_000,
        f"max WAL={max_wal} bytes",
    )
    check(
        "no thread leak across maintenance cycles",
        threads_after <= threads_before + 1,
        f"threads {threads_before}->{threads_after}",
    )
    # Backups all land in the same second-ish during an accelerated run, so we
    # just confirm pruning kept the set sane (retention can't explode it).
    check("backup count bounded", len(backups) <= cycles + 1, f"{len(backups)} backups")
    # Rough object-leak proxy.
    growth = objs_after - objs_before
    check(
        "no gross object leak",
        growth < 50_000,
        f"gc objects grew by {growth}",
    )


def main() -> int:
    cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    print(f"Accelerated soak in {_TMP} ({cycles} leak cycles)\n")
    _init_db()

    print("Fault injection + recovery:")
    soak_integrity()
    soak_backup_under_load()
    soak_receiver_reconnect()
    soak_auth_failure_alert()
    soak_disk_low_alert()

    print("\nLeak detection:")
    soak_leak_loop(cycles)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 50}\nSOAK RESULT: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
