# Responsibility: Verify TIME-23 and MIXED-29: the kept-solid attempt is an optional extra that can never cost the
# user the mesh - watched from its start, it is stopped at its own limit while the all-shell mesh is still being made
# (a finished one is read off its pipe at once), the all-shell mesh is delivered with one told sentence, and its stop
# is recorded as a trip that neither ends nor replays the attempt.
# Boundaries: forked children that sleep or return (no mesher) under short limits; one real shell run (gmsh,
# OpenCASCADE) on the plate with a 30 mm boss (tests/shell_fixtures) whose attempt is made to sleep; the executor's
# and the graph's own trip reading. No machine-size assumption: every limit is a few seconds at most.
from __future__ import annotations

import json
import time

import pytest

from meshpipeline.contracts import work_limits as W
from meshpipeline.engines import determinism as det
from meshpipeline.engines import mixed_run as mr
from meshpipeline.engines import shell_run as sr


def _until(cond, within_s: float) -> bool:
    end = time.time() + within_s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_an_attempt_past_its_guard_is_stopped_while_the_all_shell_mesh_is_still_being_made(tmp_path):
    def slow():
        time.sleep(60)

    h = sr._fork(slow)
    h["budget"] = 0.5
    sr._watch(h, tmp_path)
    # the all-shell mesh is still being made: nothing has waited for the attempt, and it is stopped all the same
    assert _until(lambda: not h["p"].is_alive(), 20.0), "the attempt ran on past its guard"
    with pytest.raises(mr._Stop) as got:
        sr._watched(h, tmp_path, clock=None)
    assert str(got.value) == sr.KEPT_SLOW.format(s=0.5)
    trips = json.loads((tmp_path / "guard_trips.json").read_text())
    kept = [t for t in trips if t.get("where") == "shell_thick_kept"]
    assert kept and all(t["rule"] == W.TIME_OPTIONAL_STEP and t["by"] == "MIXED-12" for t in kept)
    assert not any(W.ends_attempt(t) for t in kept)


def test_a_finished_attempt_is_read_at_once_even_when_its_result_is_larger_than_the_pipe(tmp_path):
    big = b"x" * (4 << 20)                  # far more than a pipe holds: the child waits until it is read

    def done():
        return big

    h = sr._fork(done)
    h["budget"] = 60.0
    sr._watch(h, tmp_path)
    assert _until(lambda: h.get("outcome") is not None, 30.0)
    assert h["outcome"][0] == "ok" and h["done_s"] is not None and h["done_s"] < 30.0
    assert sr._watched(h, tmp_path, clock=600.0) == big


def test_an_attempt_that_finished_before_the_clock_was_set_is_held_to_it(tmp_path):
    def work():
        stop = time.process_time() + 0.3
        while time.process_time() < stop:
            pass
        return 7

    h = sr._fork(work)
    h["budget"] = 60.0
    sr._watch(h, tmp_path)
    assert _until(lambda: h.get("outcome") is not None, 30.0)
    with pytest.raises(mr._Stop) as got:
        sr._watched(h, tmp_path, clock=1e-6)          # MIXED-26: allowed far less than it took
    assert str(got.value) == sr.KEPT_CLOCK.format(s=1e-6, k=sr.KEPT_PER_PLAIN)
    trips = json.loads((tmp_path / "guard_trips.json").read_text())
    assert any(t.get("rule") == W.TIME_OPTIONAL_STEP and t.get("by") == "MIXED-26" for t in trips)


def test_a_split_that_fails_is_told_in_one_plain_sentence_never_as_meshed_as_a_solid():
    # the tank with a cone roof: the attempt finished in 145 s with the mixed route's own reason; on a shell the
    # all-shell mesh is delivered, so "It is meshed as a solid." and its nested detail are not told (the record keeps
    # them)
    rec: dict = {}
    sr._kept_not(rec, mr._Stop(mr.WHY_SPLIT.format(why="the thick regions could not be cut out (a Boolean cut "
                                                       "failed)")))
    assert rec["kept_solid_why"] == ("The thick regions could not be kept as solid and joined to the shell (the thin "
                                     "walls and the thick regions could not be cut apart), so they are left out.")
    assert "Boolean cut failed" in rec["kept_solid_detail"]
    other: dict = {}
    sr._kept_not(other, mr._Stop(sr.KEPT_SLOW.format(s=300.0)))
    assert other["kept_solid_why"] == sr.KEPT_NOT.format(why=sr.KEPT_SLOW.format(s=300.0))
    assert "kept_solid_detail" not in other


def test_the_extras_stop_neither_ends_nor_replays_the_attempt(tmp_path):
    from meshpipeline.pipeline.executor import _attempt_ending, _guard_trip

    det.note_guard_trip("shell_thick_kept", workspace=tmp_path, rule=W.TIME_OPTIONAL_STEP, by="MIXED-12",
                        budget_s=300.0)
    assert det.guard_trips(tmp_path), "the stop is still recorded"
    assert _attempt_ending(det.guard_trips(tmp_path)) == []
    assert _guard_trip({"engine": "gmsh"}, str(tmp_path)) == {}       # the executor judges the result as made
    # a trip that ends the attempt still does, beside it
    det.note_guard_trip("snappy", workspace=tmp_path, guard_s=4000.0)
    assert _guard_trip({"engine": "snappy"}, str(tmp_path))["where"] == "snappy"


def test_a_run_whose_attempt_runs_past_its_guard_delivers_the_all_shell_mesh_and_says_so(tmp_path, monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf

    from meshpipeline.pipeline.executor import _guard_trip

    def slow(*a, **k):
        time.sleep(120)

    monkeypatch.setattr(mr, "build", slow)
    monkeypatch.setattr(sr, "KEPT_START", "fork")         # the patched build reaches the child (MIXED-28)
    monkeypatch.setattr(sr, "_kept_budget_s", lambda: 2.0)
    monkeypatch.setattr(sr, "kept_ahead", lambda *a, **k: "")
    step = sf.write_step(sf.plate_with_boss(), tmp_path / "boss.step")
    ws = tmp_path / "ws"
    ws.mkdir()
    started = time.time()
    rec = sr.run(ws, step, mm_per_unit=1.0)
    assert time.time() - started < 90
    assert rec["made"] is True and "kept_solid" not in rec
    told = sr.KEPT_NOT.format(why=sr.KEPT_SLOW.format(s=2.0))
    assert rec["kept_solid_why"] == told and told in rec["words"]
    clock = rec["kept_clock"]
    assert clock["kept"] is False and clock["guard_s"] == 2.0
    trips = det.guard_trips(ws)
    assert any(t.get("where") == "shell_thick_kept" and t.get("rule") == W.TIME_OPTIONAL_STEP for t in trips)
    assert _guard_trip({"engine": "gmsh"}, str(ws)) == {}
    assert "CTETRA" not in (ws / "shell.bdf").read_text()
