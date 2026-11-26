# Responsibility: Verify that the verdict's speed-ups change no answer: per-pool memos of the STEP read,
#                 layer 1 and layer 3, and layer 1's overlap of the shape check with the round trip.
# Boundaries: hermetic - scripted pools stand in for the kernel workers; no OCCT. That the memoised and
#             overlapped verdicts equal the plain ones on real files is proved on corpus files (Lane A report).
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v4.verdict.faithful import check_faithful_steps
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, check_valid
from meshpipeline.cad.repair_v4.worker_pool import CallResult


def _ok(value: dict) -> CallResult:
    return CallResult(ok=True, value=value, error=None, status="ok", seconds=0.0)


_FAITHFUL = {
    "ok": True, "reasons": [], "max_dev_mm": 0.0, "bound_mm": 0.1, "diag_mm": 100.0, "fill_span_mm": 0.0,
    "area_ratio": 1.0, "volume_ratio": 1.0, "bodies_before": 1, "bodies_after": 1,
}  # fmt: skip


class CountingPool:
    """A thread-safe scripted kernel. Check results are chosen by the BREP's path, never by call order,
    because layer 1 now checks the input and its round trip concurrently."""

    def __init__(self, *, hard=(), unbounded=False, fail=None, gate=None) -> None:
        self.hard, self.unbounded, self.fail, self.gate = list(hard), unbounded, fail, gate
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def count(self, fn: str) -> int:
        return sum(1 for c in self.calls if c == fn)

    def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
        with self._lock:
            self.calls.append(fn)
        if self.fail is not None and self.fail[0] == fn:
            return self.fail[1]
        if fn == "read_step_xcaf":
            Path(payload["brep_out"]).write_text("brep")  # the real kernel writes it; the memo checks it is there
            return _ok({"brep": payload["brep_out"], "labels": {}, "assembly_ids": [], "units": "mm"})
        if fn == "write_step_xcaf":
            if self.gate is not None:
                self.gate.wrote.set()
            return _ok({"path": payload["path"]})
        if fn == "valid_extent":
            return _ok({"unbounded": self.unbounded})
        if fn == "check_valid":
            rt = "round_trip" in payload["brep"]
            if not rt and self.gate is not None:  # the input's check waits until the round trip has started
                self.gate.overlapped = self.gate.wrote.wait(5.0)
            hard = ["unbounded_geometry"] if self.unbounded else ([] if rt else self.hard)
            return _ok({"solids": 1, "hard": [{"code": c, "entities": [], "detail": {}} for c in hard], "soft": []})
        if fn == "faithful_check":
            return _ok(dict(_FAITHFUL))
        raise AssertionError(f"unexpected kernel call {fn}")


@pytest.fixture
def step(tmp_path: Path) -> str:
    path = tmp_path / "part.step"
    path.write_bytes(b"ISO-10303-21; a part")
    return str(path)


def test_layer_1_on_the_same_file_and_pool_is_measured_once(step) -> None:
    pool = CountingPool()
    first = check_valid(pool, step, original_units="mm")
    calls = len(pool.calls)
    second = check_valid(pool, step, original_units="mm")
    assert second == first
    assert len(pool.calls) == calls  # no kernel call the second time


def test_layer_1_memo_is_keyed_by_the_original_unit_the_pool_and_the_bytes(step) -> None:
    pool = CountingPool()
    check_valid(pool, step, original_units="mm")
    reads = pool.count("check_valid")
    check_valid(pool, step, original_units="inch")
    assert pool.count("check_valid") > reads  # a different original unit is a different question
    reads = pool.count("check_valid")
    Path(step).write_bytes(b"ISO-10303-21; another part")
    check_valid(pool, step, original_units="mm")
    assert pool.count("check_valid") > reads  # different bytes, measured again
    other = CountingPool()
    check_valid(other, step, original_units="mm")
    assert other.count("check_valid") > 0  # a memo never outlives the workers that measured it


def test_a_memoised_report_is_a_copy_the_caller_may_change(step) -> None:
    pool = CountingPool(hard=["open_shell"])
    first = check_valid(pool, step, original_units=None)
    first.hard.clear()
    assert [f.code for f in check_valid(pool, step, original_units=None).hard] == ["open_shell"]


def test_an_unavailable_checker_is_never_memoised(step) -> None:
    pool = CountingPool(fail=("check_valid", CallResult(False, None, "worker gone", "crashed", 0.0)))
    with pytest.raises(CheckerUnavailable):
        check_valid(pool, step, original_units=None)
    pool.fail = None
    assert check_valid(pool, step, original_units=None).ok


def test_a_missing_file_is_not_memoised_and_still_judged(tmp_path) -> None:
    pool = CountingPool(fail=("read_step_xcaf", CallResult(False, None, "ValueError: no such file", "error", 0.0)))
    report = check_valid(pool, str(tmp_path / "gone.step"), original_units=None)
    assert [f.code for f in report.hard] == ["brep_invalid"]


def test_the_input_check_and_the_round_trip_run_concurrently(step) -> None:
    class Gate:
        wrote = threading.Event()
        overlapped = False

    gate = Gate()
    report = check_valid(CountingPool(gate=gate), step, original_units=None)
    assert report.ok and report.round_trip_ok
    assert gate.overlapped  # the round trip was written while the input's check was still running


def test_an_unbounded_input_is_still_never_written_back(step) -> None:
    pool = CountingPool(unbounded=True)
    report = check_valid(pool, step, original_units=None)
    assert report.unbounded and not report.round_trip_ok
    assert pool.count("write_step_xcaf") == 0 and pool.count("check_valid") == 1


def test_a_failed_extent_call_falls_back_to_the_plain_order(step) -> None:
    pool = CountingPool(fail=("valid_extent", CallResult(False, None, "worker gone", "crashed", 0.0)))
    assert check_valid(pool, step, original_units=None).ok


def test_layer_3_reuses_layer_1s_read_and_is_measured_once(step) -> None:
    pool = CountingPool()
    check_valid(pool, step, original_units=None)
    reads = pool.count("read_step_xcaf")  # the input once, its round trip once
    first = check_faithful_steps(pool, step, step, declared_added=[], declared_removed=[])
    assert pool.count("read_step_xcaf") == reads  # original and repaired are the file layer 1 already read
    calls = len(pool.calls)
    second = check_faithful_steps(pool, step, step, declared_added=[], declared_removed=[])
    assert second == first and len(pool.calls) == calls


def test_layer_3_memo_is_keyed_by_the_declared_edits(step) -> None:
    pool = CountingPool()
    check_faithful_steps(pool, step, step, declared_added=[], declared_removed=[])
    before = pool.count("faithful_check")
    check_faithful_steps(pool, step, step, declared_added=["F1"], declared_removed=[])
    assert pool.count("faithful_check") == before + 1


def test_a_raise_is_not_held_up_by_the_round_trip(step) -> None:
    release = threading.Event()

    class SlowWrite(CountingPool):
        def call(self, fn: str, payload: dict, *, timeout_s: float) -> CallResult:
            if fn == "write_step_xcaf":
                release.wait(10.0)
            return super().call(fn, payload, timeout_s=timeout_s)

    pool = SlowWrite(fail=("check_valid", CallResult(False, None, "worker gone", "crashed", 0.0)))
    start = time.monotonic()
    try:
        with pytest.raises(CheckerUnavailable):
            check_valid(pool, step, original_units=None)
        assert time.monotonic() - start < 5.0  # it returned while the write was still blocked
    finally:
        release.set()
