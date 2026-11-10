"""PLAN-fea-13: the FEA mesher has no guessed time limit. It is stopped only when it has made no
progress for the stall window, or at the hard ceiling - and what the watchdog saw is kept."""
from __future__ import annotations

import json
import sys
import time

import pytest

from meshpipeline.engines import mesher_stage
from meshpipeline.engines.gmsh import fea_adapter
from meshpipeline.sandbox.safe_exec import run_watched


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_slow_mesher_that_keeps_printing_is_never_stopped(tmp_path):
    # runs 3 s, printing every 0.3 s; the stall window is 1 s: it is moving, so it finishes
    w = run_watched(_py("import time\nfor i in range(10):\n    print(i, flush=True); time.sleep(0.3)"),
                    log_path=tmp_path / "a.log", hard_timeout=60, stall_s=1.0, poll_s=0.1)
    assert w.stopped == "ok" and w.returncode == 0 and "9" in w.tail
    assert w.longest_silence_s < 1.0


def test_a_mesher_that_goes_quiet_is_stopped_as_stuck(tmp_path):
    w = run_watched(_py("print('start', flush=True)\nimport time\ntime.sleep(60)"),
                    log_path=tmp_path / "b.log", hard_timeout=60, stall_s=1.0, poll_s=0.1)
    assert w.stopped == "stalled" and w.seconds < 20 and w.silence_at_stop_s >= 1.0 and "start" in w.tail


def test_a_new_step_counts_as_progress(tmp_path):
    # silent on its output, but it notes a new step every 0.3 s: not stuck
    stage = tmp_path / "fea_stage.json"
    code = (f"import time, pathlib\np = pathlib.Path({str(stage)!r})\n"
            "for i in range(8):\n    p.write_text(str(i)); time.sleep(0.3)")
    w = run_watched(_py(code), log_path=tmp_path / "c.log", hard_timeout=60, stall_s=1.0, poll_s=0.1,
                    progress_paths=(stage,))
    assert w.stopped == "ok" and w.returncode == 0


_BUSY = "import time\nt = time.monotonic()\nwhile time.monotonic() - t < {s}:\n    sum(range(10000))\n"
_linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="CPU is read from /proc")


@_linux_only
def test_a_quiet_mesher_still_burning_cpu_is_never_stopped(tmp_path):
    """gmsh's 1D curve meshing prints nothing per curve (ox_cryo_ll, ox_mc_lid, hemy_mount_base, dev-areen
    25 Oct: stopped as stalled while still computing). No output for 3 s against a 1 s window, but
    the CPU is busy the whole time: it is working, so it finishes."""
    w = run_watched(_py("print('meshing curves', flush=True)\n" + _BUSY.format(s=3.0)),
                    log_path=tmp_path / "e.log", hard_timeout=60, stall_s=1.0, poll_s=0.1)
    assert w.stopped == "ok" and w.returncode == 0
    assert w.longest_silence_s < 1.0 and w.cpu_s >= 1.0


@_linux_only
def test_a_mesher_whose_cpu_goes_flat_too_is_stopped(tmp_path):
    # busy and quiet for 1 s, then quiet AND idle: every sign is flat, so it is stuck
    w = run_watched(_py(_BUSY.format(s=1.0) + "time.sleep(60)"),
                    log_path=tmp_path / "f.log", hard_timeout=60, stall_s=1.0, poll_s=0.1)
    assert w.stopped == "stalled" and w.seconds < 20 and w.silence_at_stop_s >= 1.0


@_linux_only
def test_the_cpu_of_a_process_the_mesher_started_counts(tmp_path):
    # the watched process only waits; the work is done by the child it started (a native tool)
    code = ("import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-c', {_BUSY.format(s=2.5)!r}])")
    w = run_watched(_py(code), log_path=tmp_path / "g.log", hard_timeout=60, stall_s=1.0, poll_s=0.1)
    assert w.stopped == "ok" and w.returncode == 0


def test_the_hard_ceiling_still_holds_for_a_mesher_that_is_moving(tmp_path):
    w = run_watched(_py("import time\nwhile True:\n    print('.', flush=True); time.sleep(0.1)"),
                    log_path=tmp_path / "d.log", hard_timeout=1.0, stall_s=30.0, poll_s=0.1)
    assert w.stopped == "ceiling" and w.seconds < 20


def test_the_runner_says_what_the_watchdog_saw(tmp_path, monkeypatch):
    from meshpipeline.engines.gmsh import gmsh_runner as GR

    (tmp_path / "fea_plan.json").write_text("{}")
    monkeypatch.setattr(sys, "executable", sys.executable)
    monkeypatch.setattr(GR, "FEA_STALL_WINDOW_S", 1)
    calls = {}

    def fake(args, **kw):
        calls.update(kw)
        from meshpipeline.sandbox.safe_exec import Watched
        return Watched(returncode=-9, stopped="stalled", seconds=42.0, longest_silence_s=1.2,
                       silence_at_stop_s=1.2, tail="Info : Meshing 3D...")

    monkeypatch.setattr("meshpipeline.sandbox.safe_exec.run_watched", fake)
    out = GR._run_gmsh_local(tmp_path, timeout=13800)
    assert out["timed_out"] and out["stalled"] and out["watchdog"]["stopped"] == "stalled"
    assert out["watchdog"]["stall_window_s"] == 1.0 and out["watchdog"]["ceiling_s"] == 13800.0
    assert calls["hard_timeout"] == 13800.0 and calls["log_path"].name == GR.FEA_LOG


def test_the_live_line_names_the_fea_step(tmp_path):
    (tmp_path / "gmshfea.log").write_text("Info : Meshing 3D...\n")
    fea_adapter.note_stage(tmp_path, "volume", "filling the solid with elements")
    st = mesher_stage.read_stage(tmp_path)
    assert st == {"key": "volume", "label": "filling the solid with elements", "tool": "gmshfea"}
    assert json.loads((tmp_path / fea_adapter.STAGE_FILE).read_text())["key"] == "volume"


def test_the_ticker_renotes_its_step_and_stops(tmp_path):
    t = fea_adapter.Ticker(tmp_path, every_s=0.1)
    t.start("writing", "writing the solver files")
    first = (tmp_path / fea_adapter.STAGE_FILE).stat().st_mtime_ns
    time.sleep(0.5)
    assert (tmp_path / fea_adapter.STAGE_FILE).stat().st_mtime_ns != first
    t.stop()
    last = (tmp_path / fea_adapter.STAGE_FILE).stat().st_mtime_ns
    time.sleep(0.3)
    assert (tmp_path / fea_adapter.STAGE_FILE).stat().st_mtime_ns == last


def test_the_budget_stops_a_fill_but_never_a_mesh_already_filled(tmp_path):
    """LAD-fea-15 on the mesher's machine (gmsh_runner.FillBudget): past its budget a pass still filling
    is stopped as ``budget``; one past the fill - improving, curving, writing a mesh that exists - is
    stopped only by the stall watch or the wall (dev-areen 9 Nov: a filled 2.4M-element helmet was due to
    be cut in its clean-up, with nothing delivered)."""
    from meshpipeline.engines.gmsh import gmsh_runner as G

    (tmp_path / G.BUDGET_FILE).write_text(json.dumps({"budget_s": 1}))
    budget = G.fill_budget(tmp_path)
    stage = tmp_path / "fea_stage.json"
    for key, stops in (("surface", True), ("volume", True), ("optimising", False), ("curving", False),
                       ("writing", False)):
        stage.write_text(json.dumps({"key": key}))
        assert budget(0.5) is False
        assert budget(2.0) is stops, key
    stage.unlink()
    assert budget(2.0) is True                       # not yet started: still before the fill
    assert G.fill_budget(tmp_path / "none") is None  # no budget file: the wall alone

    busy = "print('x', flush=True)\nimport time\ntime.sleep(60)"
    stage.write_text(json.dumps({"key": "volume"}))
    w = run_watched(_py(busy), log_path=tmp_path / "f.log", hard_timeout=60, stall_s=30, poll_s=0.1,
                    over_budget=budget)
    assert w.stopped == "budget" and w.seconds < 10
    stage.write_text(json.dumps({"key": "optimising"}))
    w = run_watched(_py("print('x', flush=True)\nimport time\ntime.sleep(2)"), log_path=tmp_path / "o.log",
                    hard_timeout=60, stall_s=30, poll_s=0.1, over_budget=budget)
    assert w.stopped == "ok"
