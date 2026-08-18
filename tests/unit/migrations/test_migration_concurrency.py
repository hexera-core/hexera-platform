# Responsibility: Verify the advisory lock wraps the upgrade, is always released, and two runners never overlap.
from __future__ import annotations

import threading
import time

import pytest

from meshpipeline.runtime import migrate


class FakeConn:

    def __init__(self, on_lock=None, on_unlock=None, fail_substr=None):
        self.calls: list[str] = []
        self._on_lock = on_lock
        self._on_unlock = on_unlock
        self._fail_substr = fail_substr

    def execute(self, clause, params=None):
        sql = str(clause)
        self.calls.append(sql)
        if self._fail_substr and self._fail_substr in sql:
            raise RuntimeError("statement failed")
        if "pg_advisory_lock" in sql and self._on_lock:
            self._on_lock()
        if "pg_advisory_unlock" in sql and self._on_unlock:
            self._on_unlock()
        return None


def test_the_lock_wraps_the_upgrade():
    conn = FakeConn()
    conn_upgrade_marker = []
    migrate._run_locked(conn, lambda: conn.calls.append("UPGRADE") or conn_upgrade_marker.append(1))
    lock_i = next(i for i, c in enumerate(conn.calls) if "pg_advisory_lock(" in c)
    up_i = conn.calls.index("UPGRADE")
    unlock_i = next(i for i, c in enumerate(conn.calls) if "pg_advisory_unlock" in c)
    assert lock_i < up_i < unlock_i, conn.calls
    assert conn_upgrade_marker == [1]


def test_the_lock_is_released_even_when_the_upgrade_fails():
    conn = FakeConn()

    def _boom():
        raise RuntimeError("migration blew up")

    with pytest.raises(RuntimeError, match="blew up"):
        migrate._run_locked(conn, _boom)
    assert any("pg_advisory_unlock" in c for c in conn.calls), "the lock leaked on failure"


def test_a_bounded_acquire_failure_aborts_the_rollout():
    conn = FakeConn(fail_substr="pg_advisory_lock")
    with pytest.raises(SystemExit, match="could not acquire the migration lock"):
        migrate._run_locked(conn, lambda: None)


def test_two_runners_never_migrate_at_the_same_time():
    gate = threading.Lock()
    inside = {"n": 0, "overlap": False}
    guard = threading.Lock()

    def on_lock():
        gate.acquire()
        with guard:
            inside["n"] += 1
            if inside["n"] > 1:
                inside["overlap"] = True

    def on_unlock():
        with guard:
            inside["n"] -= 1
        gate.release()

    def upgrade():
        time.sleep(0.05)   # hold the critical section long enough for a race to show

    def worker():
        migrate._run_locked(FakeConn(on_lock=on_lock, on_unlock=on_unlock), upgrade)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not inside["overlap"], "two instances ran migrations concurrently - the lock did not serialise"


# the local-DB-in-production guard

@pytest.mark.parametrize("host", ["postgres", "localhost", "127.0.0.1", "db"])
def test_production_refuses_a_local_compose_database(host):
    with pytest.raises(SystemExit, match="local host"):
        migrate._guard_not_local_db_in_production(f"postgresql://u:p@{host}:5432/mesh", "production")


def test_production_allows_a_hosted_database():
    migrate._guard_not_local_db_in_production(
        "postgresql://u:p@ep-cool-name.us-east-2.aws.neon.tech/mesh?sslmode=require", "production")


def test_dev_allows_the_local_database():
    migrate._guard_not_local_db_in_production("postgresql://u:p@postgres:5432/mesh", "dev")
