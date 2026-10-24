"""PLAN-fea-13: the FEA mesher has no guessed time limit. It is stopped only when it has made no
progress for the stall window, or at the hard ceiling - and what the watchdog saw is kept."""
from __future__ import annotations

import json
import sys
import time

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
