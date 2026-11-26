# Responsibility: Verify the event-log query tool (devtools/validation/v4_eval/events_query.py) on a fixture JSONL:
#                 failures by phase/class/op/reason, a file's timeline, wall time per phase, exceptions, mesher
#                 failures, and a file that never ended (its last heartbeat).
# Boundaries: hermetic - a fixture written here; no gcloud, no engine.
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "devtools/validation/v4_eval"))
import events_query as Q  # noqa: E402

RUN = {"run_id": "r1", "vm": "vm-a", "shard": "1/2"}
A = {**RUN, "file_sha256": "a" * 64, "file_name": "a.step", "arm": "v5"}
B = {**RUN, "file_sha256": "b" * 64, "file_name": "b.step", "arm": "v5"}


def _ev(i: int, event: str, base: dict, **kw: object) -> dict:
    return {"ts": f"2026-11-25T10:00:{i:02d}.000Z", "seq": i, "pid": 10, "event": event, "severity": "INFO",
            **base, **kw}


FIXTURE = [
    _ev(1, "file_start", A),
    _ev(2, "phase", A, phase="read", prev_phase=None, prev_wall_s=None, file_elapsed_s=0.0),
    _ev(3, "phase", A, phase="tournament", prev_phase="read", prev_wall_s=4.0, round=1, defect_id="S1",
        defect_class="overlap", file_elapsed_s=4.0),
    _ev(4, "heat", A, phase="tournament", defect_id="S1", defect_class="overlap", op="cut_overlap", tier="rule",
        result="still_there", reason_key="still_there", reason="overlap remains", severity="WARNING"),
    _ev(5, "heat_apply_failed", A, phase="tournament", defect_class="overlap", op="fuse", severity="WARNING",
        exc_type="OpFailed", exc_message="fuse refused", traceback="Traceback\n  File x\nOpFailed: fuse refused"),
    _ev(6, "panel_member", A, phase="round_panel", member="gmsh_hxt", status="crashed", error_kind="gmsh_crash",
        cause="SIGSEGV", wall_s=12.5, message="gmsh process killed by signal 11", severity="WARNING"),
    _ev(7, "kernel_call_failed", A, phase="round_panel", kernel_call="mesh_gmsh", op="mesh_gmsh", status="timeout",
        reason_key="kernel_timeout", timeout_s=300, worker_exit_code=-9, error="exceeded", severity="WARNING"),
    _ev(8, "file_end", A, result="row", prev_phase="round_panel", prev_wall_s=20.0, wall_s=24.0),
    _ev(9, "file_start", B),
    _ev(10, "phase", B, phase="diagnose", prev_phase="heal", prev_wall_s=30.0, file_elapsed_s=30.0),
    _ev(11, "heartbeat", B, phase="diagnose", rss_mb=900.0, children_rss_mb=14000.0, file_elapsed_s=60.0,
        biggest_child={"pid": 77, "rss_mb": 13000.0, "cmd": "python -m mesh_child"}),
]


@pytest.fixture
def ev(tmp_path: Path) -> list[dict]:
    d = tmp_path / "out" / "shard-1-of-2"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in FIXTURE) + '{"torn": ')
    paths = Q.files_of(str(tmp_path / "out"))
    assert [p.name for p in paths] == ["events.jsonl"]
    return Q.load(paths)


def test_load_skips_a_torn_last_line(ev: list[dict]) -> None:
    assert len(ev) == len(FIXTURE)


def test_failures_group_by_phase_class_op_reason(ev: list[dict]) -> None:
    out = Q.failures(ev, ["phase", "defect_class", "op", "reason_key"])
    assert "still_there" in out and "cut_overlap" in out and "OpFailed" in out and "kernel_timeout" in out
    assert "file_start" not in out and "phase  " not in out.splitlines()[2]


def test_timeline_of_one_file(ev: list[dict]) -> None:
    out = Q.timeline(ev, "aaaa")
    assert out.startswith("a.step") and "cut_overlap" in out and "gmsh_hxt" in out and "b.step" not in out


def test_walltime_sums_each_phase(ev: list[dict]) -> None:
    out = Q.walltime(ev)
    rows = {line.split()[0]: line.split()[1] for line in out.splitlines()[2:6] if line.strip()}
    assert rows["heal"] == "30" and rows["round_panel"] == "20" and rows["read"] == "4"


def test_exceptions_show_an_example_traceback(ev: list[dict]) -> None:
    out = Q.exceptions(ev)
    assert "OpFailed (heat_apply_failed)" in out and "OpFailed: fuse refused" in out


def test_mesher_failures_by_member_and_kind(ev: list[dict]) -> None:
    out = Q.mesher(ev)
    assert "gmsh_hxt" in out and "gmsh_crash" in out and "SIGSEGV" in out


def test_killed_lists_the_file_that_never_ended_with_its_last_heartbeat(ev: list[dict]) -> None:
    out = Q.killed(ev)
    assert "b.step" in out and "a.step" not in out and "14000.0" in out and "mesh_child" in out


def test_an_oom_kill_is_attributed_to_the_file_its_unit_was_on(tmp_path: Path) -> None:
    unit = {**RUN, "shard": "dev_v5-3/22", "file_sha256": "c" * 64, "file_name": "c.step", "arm": "v5"}
    ev = [_ev(1, "file_start", unit), _ev(5, "phase", unit, phase="round_panel"),
          {"ts": "2026-11-25T10:00:07,123456+00:00", "event": "oom_kill", "vm": "vm-a", "unit": "dev_v5-3",
           "task": "python", "anon_rss_mb": 15000.0}]
    out = Q.killed(ev)
    assert "kernel OOM kills" in out and "c.step" in out and "round_panel" in out and "15000.0" in out


def test_oom_scrape_reads_cgroup_v2_kill_lines() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / ".superpowers/sdd/2026-11-22-crs-search-repair-plan"
                                                                 "/snap-v4"))
    import oom_scrape

    cid = "d" * 64
    lines = [f"2026-11-25T10:00:07,1+00:00 oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=docker-{cid}"
             f".scope,mems_allowed=0,global_oom,task_memcg=/system.slice/docker-{cid}.scope,task=python,pid=4242,uid=0",
             "2026-11-25T10:00:07,2+00:00 Out of memory: Killed process 4242 (python) total-vm:1kB, "
             "anon-rss:2097152kB, file-rss:0kB"]
    (ev,) = oom_scrape.parse(lines, {cid: "snap-dev_v5-3"}, "vm-a")
    assert ev["unit"] == "dev_v5-3" and ev["host_pid"] == 4242 and ev["anon_rss_mb"] == 2048.0


def test_summary_and_cli_run(ev: list[dict], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "KILLED" in Q.summary(ev)
    assert Q.main([str(tmp_path / "out"), "timeline", "--file", "b.step"]) == 0
    assert "heartbeat" in capsys.readouterr().out
