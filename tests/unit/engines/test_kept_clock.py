# Responsibility: Verify MIXED-26, THE KEPT-SOLID ATTEMPT IS BOUNDED BY THE ALL-SHELL MESH'S OWN COST: the all-shell
# mesh is made beside the attempt, and once it is made the attempt gets KEPT_PER_PLAIN times its seconds (never less
# than KEPT_FLOOR_S); past that clock the all-shell mesh is delivered and the user is told; inside it the attempt
# finishes and its seconds are read.
# Boundaries: forked children that sleep (no mesher), and one real shell run (gmsh, OpenCASCADE) on the plate with a
# 30 mm boss (tests/shell_fixtures) whose attempt is made to sleep.
from __future__ import annotations

import json
import time

import pytest

from meshpipeline.engines import mixed_run as mr
from meshpipeline.engines import shell_run as sr


def test_the_clock_is_a_multiple_of_the_all_shell_mesh_with_a_floor():
    assert sr.kept_clock_s(0.5) == sr.KEPT_FLOOR_S                  # a tiny part is not cut off
    assert sr.kept_clock_s(100.0) == sr.KEPT_PER_PLAIN * 100.0
    assert sr.kept_clock_s(1e9) > sr.KEPT_BUDGET_S                    # MIXED-12's guard still caps it in _wait


def _burn(cpu_s: float) -> None:
    """Work for `cpu_s` CPU seconds (MIXED-27: the clock counts work, not wall seconds)."""
    stop = time.process_time() + cpu_s
    while time.process_time() < stop:
        pass


def test_an_attempt_past_its_clock_is_stopped_and_the_user_is_told(tmp_path):
    def slow():
        _burn(30.0)

    started = time.time()
    with pytest.raises(mr._Stop) as got:
        sr._wait(sr._fork(slow), 30.0, tmp_path, clock_s=0.5)
    assert time.time() - started < 10
    assert str(got.value) == sr.KEPT_CLOCK.format(s=0.5, k=sr.KEPT_PER_PLAIN)
    trips = json.loads((tmp_path / "guard_trips.json").read_text())
    # TIME-23: recorded as an optional extra's stop (by MIXED-26), which never ends the attempt
    assert any(t.get("rule") == "TIME-23" and t.get("by") == "MIXED-26" and t.get("where") == "shell_thick_kept"
               for t in trips)


def test_an_attempt_inside_its_clock_finishes_and_its_seconds_are_read(tmp_path):
    def quick():
        time.sleep(0.3)
        return 7

    h = sr._fork(quick)
    time.sleep(0.6)                 # the all-shell mesh being made meanwhile
    assert sr._wait(h, 30.0, tmp_path, clock_s=5.0) == 7
    assert 0.2 <= h["done_s"] <= 0.6 + 0.5          # when it finished, not when it was read


def test_the_guard_still_caps_a_clock_longer_than_it(tmp_path):
    def slow():
        time.sleep(30)

    with pytest.raises(mr._Stop) as got:
        sr._wait(sr._fork(slow), 0.5, tmp_path, clock_s=60.0)
    assert str(got.value) == sr.KEPT_SLOW.format(s=0.5)


def test_a_run_whose_attempt_runs_past_the_clock_delivers_the_all_shell_mesh_and_says_so(tmp_path, monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf

    def slow(*a, **k):
        _burn(60.0)

    monkeypatch.setattr(mr, "build", slow)
    monkeypatch.setattr(sr, "KEPT_START", "fork")         # the patched build reaches the child (MIXED-28)
    monkeypatch.setattr(sr, "KEPT_FLOOR_S", 1.0)
    monkeypatch.setattr(sr, "KEPT_PER_PLAIN", 1.0)
    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    ws = tmp_path / "ws"
    ws.mkdir()
    started = time.time()
    rec = sr.run(ws, step, mm_per_unit=1.0)
    assert time.time() - started < 50
    assert rec["made"] is True and "kept_solid" not in rec and rec["left_out"]["thick"] == 1
    assert "keeping them solid did not finish in the" in rec["words"]
    assert "times what the all-shell mesh took), so they are left out." in rec["words"]
    clock = rec["kept_clock"]
    assert clock["kept"] is False and clock["attempt_s"] is None
    plain = clock["plain_cpu_s"] if clock["by_cpu"] else clock["plain_s"]
    assert clock["allowed_s"] == pytest.approx(max(1.0, plain), abs=0.2)
    if clock["by_cpu"]:
        assert clock["stopped_cpu_s"] >= clock["allowed_s"]          # MIXED-27: stopped on its own CPU seconds
    assert "CTETRA" not in (ws / "shell.bdf").read_text()


def test_an_attempt_from_a_process_with_other_threads_starts_clean(tmp_path):
    # MIXED-28: a fork copies only the forking thread, so a pool left by a library hangs the child; with another
    # thread running, a picklable attempt is spawned (and still finishes and is read), a closure is forked as before
    import functools
    import os
    import threading

    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    try:
        assert sr._threads() >= 2
        assert sr._start_kind(functools.partial(os.getpid)) == "spawn"
        assert sr._start_kind(lambda: 1) == "fork"
        h = sr._fork(functools.partial(os.getpid))
        assert h["start"] == "spawn"
        assert sr._wait(h, 120.0, tmp_path) != os.getpid()
    finally:
        stop.set()
        t.join()
