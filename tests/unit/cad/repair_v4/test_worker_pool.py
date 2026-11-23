# Responsibility: Verify the v4 kernel worker pool moves calls to warm spawned workers, and that a
#                 hung or crashed worker is killed, reported and replaced.
# Boundaries: hermetic - uses the test-only kernel functions (HEXERA_TEST_KERNEL_FNS=1), no OCCT.
from __future__ import annotations

import multiprocessing
import os
import threading
import time
from collections.abc import Iterator

import pytest

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


def _wait_gone(pid: int, limit_s: float = 5.0) -> bool:
    """True once no process has this pid (the pool reaps its workers, so this is prompt)."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerPool]:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def test_call_returns_value(pool: WorkerPool) -> None:
    result = pool.call("echo", {"brep": "/tmp/a.brep", "n": 3}, timeout_s=20)
    assert result.ok is True
    assert result.status == "ok"
    assert result.error is None
    assert result.value is not None
    assert result.value["echo"] == {"brep": "/tmp/a.brep", "n": 3}
    assert result.seconds >= 0


def test_timeout_kills_and_replaces(pool: WorkerPool) -> None:
    before = pool.call("echo", {}, timeout_s=20)
    assert before.value is not None
    hung = pool.call("sleep_forever", {}, timeout_s=0.5)
    assert hung.status == "timeout"
    assert hung.ok is False
    assert hung.value is None
    after = pool.call("echo", {}, timeout_s=20)
    assert after.status == "ok"
    assert after.value is not None
    assert after.value["pid"] != before.value["pid"]
    assert _wait_gone(before.value["pid"]), "timed-out worker was not killed"


def test_crash_is_reported(pool: WorkerPool) -> None:
    before = pool.call("echo", {}, timeout_s=20)
    assert before.value is not None
    crashed = pool.call("abort", {}, timeout_s=20)
    assert crashed.status == "crashed"
    assert crashed.ok is False
    after = pool.call("echo", {}, timeout_s=20)
    assert after.status == "ok"
    assert after.value is not None
    assert after.value["pid"] != before.value["pid"]


def test_unknown_fn_refused(pool: WorkerPool) -> None:
    result = pool.call("os_system", {"cmd": "true"}, timeout_s=20)
    assert result.status == "error"
    assert result.ok is False
    assert result.error is not None
    assert "os_system" in result.error


def test_test_fns_absent_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEXERA_TEST_KERNEL_FNS", raising=False)
    p = WorkerPool(1)
    try:
        assert p.call("echo", {}, timeout_s=20).status == "error"
    finally:
        p.close()


def test_workers_are_spawned_not_forked(pool: WorkerPool) -> None:
    assert pool.start_method == "spawn"


def test_failed_respawn_leaves_pool_usable(pool: WorkerPool, monkeypatch: pytest.MonkeyPatch) -> None:
    real_spawn = pool._spawn

    def failing_spawn() -> object:
        raise RuntimeError("spawn refused")

    monkeypatch.setattr(pool, "_spawn", failing_spawn)
    crashed = pool.call("abort", {}, timeout_s=20)  # kills the worker, respawn fails
    assert crashed.status == "crashed"
    stranded = pool.call("echo", {}, timeout_s=20)  # slot is empty, lazy spawn fails too
    assert stranded.status == "crashed"
    assert stranded.error is not None and "spawn refused" in stranded.error
    monkeypatch.setattr(pool, "_spawn", real_spawn)
    recovered = pool.call("echo", {}, timeout_s=20)
    assert recovered.status == "ok"


def test_close_leaves_no_live_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(2)
    pids = {p.call("echo", {}, timeout_s=20).value["pid"] for _ in range(4)}
    p.close()
    assert all(_wait_gone(pid) for pid in pids)
    assert multiprocessing.active_children() == []


def test_close_during_inflight_call_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    results: list[object] = []
    t = threading.Thread(target=lambda: results.append(p.call("sleep_forever", {}, timeout_s=60)), daemon=True)
    t.start()
    time.sleep(0.5)  # let the call reach poll()
    p.close()
    t.join(15)
    assert not t.is_alive()
    assert getattr(results[0], "status") == "crashed"
    assert multiprocessing.active_children() == []
