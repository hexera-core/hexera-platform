# Responsibility: Verify CRS v5's streaming event log: one flushed JSON line per event, the context every line carries,
#                 failure capture (exception, traceback, memory), the file's start/end, and the hooks that feed it
#                 (Trace.record, a crashed kernel worker).
# Boundaries: hermetic - a temp log file, the test-only kernel functions; no OCCT.
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshpipeline.cad.repair_v5 import events as EV


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(EV.ENV_PATH, str(path))
    monkeypatch.setenv(EV.ENV_STDOUT, "0")
    monkeypatch.setenv(EV.ENV_HEARTBEAT, "0")
    monkeypatch.setenv("CRS_V5_RUN_ID", "run-1")
    monkeypatch.setenv("CRS_V5_VM", "vm-a")
    monkeypatch.setenv("CRS_V5_SHARD", "3/8")
    EV._reset_for_tests()
    yield path
    EV._reset_for_tests()


def test_off_by_default_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EV.ENV_PATH, raising=False)
    monkeypatch.delenv(EV.ENV_STDOUT, raising=False)
    EV._reset_for_tests()
    assert not EV.enabled()
    EV.emit("x", a=1)
    EV.file_start("abc", "a.step")
    assert not (tmp_path / "events.jsonl").exists()


def test_each_event_is_on_disk_the_moment_it_is_emitted(log: Path) -> None:
    EV.emit("one", a=1)
    assert [e["event"] for e in _lines(log)] == ["one"]      # no close, no flush call: already there
    EV.emit("two", b=[1, 2])
    got = _lines(log)
    assert [e["event"] for e in got] == ["one", "two"]
    assert got[1]["seq"] == got[0]["seq"] + 1


def test_every_line_carries_the_run_file_phase_and_defect_context(log: Path) -> None:
    class D:
        id, cls, code, kind, member = "S7", "overlap", "body_overlap", "validity", None

    EV.file_start("f" * 64, "part.step", arm="v5")
    EV.set_context(part_faces=120, part_solids=3)
    EV.enter("tournament", round=2, **EV.defect_fields(D()))
    EV.emit("heat", op="cut_overlap", tier="rule", result="still_there", reason_key="still_there")
    e = _lines(log)[-1]
    assert (e["run_id"], e["vm"], e["shard"]) == ("run-1", "vm-a", "3/8")
    assert (e["file_sha256"], e["file_name"], e["arm"]) == ("f" * 64, "part.step", "v5")
    assert (e["part_faces"], e["part_solids"], e["phase"], e["round"]) == (120, 3, "tournament", 2)
    assert (e["defect_id"], e["defect_class"], e["defect_code"]) == ("S7", "overlap", "body_overlap")
    assert e["op"] == "cut_overlap" and e["severity"] == "INFO" and "file_elapsed_s" in e


def test_failure_carries_the_exception_and_the_traceback_tail(log: Path) -> None:
    def deep(n: int) -> None:
        if n == 0:
            raise RuntimeError("kernel said no")
        deep(n - 1)

    try:
        deep(40)
    except RuntimeError as exc:
        EV.failure("heat_apply_failed", exc, op="remove_small")
    e = _lines(log)[-1]
    assert e["severity"] == "ERROR" and e["exc_type"] == "RuntimeError" and e["exc_message"] == "kernel said no"
    tb = e["traceback"].splitlines()
    assert len(tb) <= EV.TRACEBACK_LINES and tb[-1] == "RuntimeError: kernel said no"
    assert "rss_hwm_mb" in e


def test_file_end_closes_the_phase_and_clears_the_file(log: Path) -> None:
    EV.file_start("a" * 64, "a.step")
    EV.enter("diagnose", round=1)
    EV.file_end("file_timeout", reason="file wall clock 900s exceeded", severity="WARNING")
    EV.emit("after")
    end, after = _lines(log)[-2:]
    assert end["event"] == "file_end" and end["result"] == "file_timeout" and end["prev_phase"] == "diagnose"
    assert end["wall_s"] >= 0 and "file_sha256" in end
    assert "file_sha256" not in after and "phase" not in after


def test_phase_events_say_how_long_the_previous_phase_ran(log: Path) -> None:
    EV.file_start("a" * 64, "a.step")
    EV.enter("read")
    EV.enter("heal")
    ph = [e for e in _lines(log) if e["event"] == "phase"]
    assert ph[-1]["phase"] == "heal" and ph[-1]["prev_phase"] == "read" and ph[-1]["prev_wall_s"] >= 0


def test_a_huge_field_is_clipped_and_a_huge_line_drops_its_largest_field(log: Path) -> None:
    EV.emit("big", stderr_excerpt="x" * 50_000)
    e = _lines(log)[-1]
    assert len(e["stderr_excerpt"]) < 5_000 and "chars cut" in e["stderr_excerpt"]
    EV.emit("wide", **{f"k{i}": "y" * 3_900 for i in range(40)})
    line = log.read_text().splitlines()[-1]
    assert len(line) <= EV.MAX_LINE and "dropped_fields" in json.loads(line)


def test_core_keys_are_never_overwritten(log: Path) -> None:
    EV.emit("e", seq="mine", event="mine")
    e = _lines(log)[-1]
    assert e["event"] == "e" and e["x_seq"] == "mine" and e["x_event"] == "mine"


def test_stdout_gets_the_same_line_with_a_severity(log: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capfd: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(EV.ENV_STDOUT, "1")
    EV.emit("loud", severity="WARNING", a=1)
    out = [json.loads(x) for x in capfd.readouterr().out.splitlines() if x.startswith("{")]
    assert out and out[-1]["event"] == "loud" and out[-1]["severity"] == "WARNING"
    assert _lines(log)[-1]["seq"] == out[-1]["seq"]


def test_trace_record_streams_every_trace_event(log: Path) -> None:
    from meshpipeline.cad.repair_v4.trace import Trace

    t = Trace()
    t.record("v5_match", defect_id="S1", op="sew", result="won")
    e = _lines(log)[-1]
    assert e["event"] == "trace.v5_match" and e["op"] == "sew" and e["result"] == "won" and "t" in e
    assert t.events[0]["kind"] == "v5_match"          # the in-memory trace is unchanged


def test_a_crashed_kernel_worker_is_logged_with_its_exit_code(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    EV.file_start("c" * 64, "c.step")
    EV.enter("tournament")
    pool = WorkerPool(1)
    try:
        got = pool.call("abort", {}, timeout_s=20)
        late = pool.call("sleep_forever", {}, timeout_s=0.5)
    finally:
        pool.close()
    assert got.status == "crashed" and late.status == "timeout"
    fails = [e for e in _lines(log) if e["event"] == "kernel_call_failed"]
    crash, timeout = fails
    assert crash["kernel_call"] == "abort" and crash["status"] == "crashed" and crash["worker_exit_code"] == -6
    assert crash["phase"] == "tournament" and crash["file_name"] == "c.step" and crash["severity"] == "ERROR"
    assert timeout["status"] == "timeout" and timeout["timeout_s"] == 0.5 and timeout["reason_key"] == "kernel_timeout"


def test_nothing_of_one_file_or_phase_leaks_into_the_next(log: Path) -> None:
    EV.file_start("a" * 64, "a.step", job_id="j1")
    EV.enter("deliver", changed=True, round=3)
    EV.emit("in_deliver")
    EV.file_start("b" * 64, "b.step")
    EV.emit("next_file")
    phase, in_deliver, _start, nxt = [e for e in _lines(log) if e["event"] in ("phase", "in_deliver", "file_start",
                                                                              "next_file")][-4:]
    assert phase["changed"] is True and "changed" not in in_deliver and in_deliver["round"] == 3
    assert "job_id" not in nxt and "round" not in nxt and "phase" not in nxt and nxt["file_name"] == "b.step"
