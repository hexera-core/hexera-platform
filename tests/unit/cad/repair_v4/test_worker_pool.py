# Responsibility: Verify the v4 kernel worker pool moves calls to warm spawned workers, and that a
#                 hung or crashed worker is killed, reported and replaced.
# Boundaries: hermetic - uses the test-only kernel functions (HEXERA_TEST_KERNEL_FNS=1), no OCCT.
from __future__ import annotations

from collections.abc import Iterator

import pytest

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


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
