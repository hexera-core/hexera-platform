# Responsibility: Pin that the investigator's OCC distance measurement cannot hang its process.
# Boundaries: No kernel. The child is stubbed; what is tested is the BOUND and the refusal shape.
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from meshpipeline.cad.repair import occ_distance as od


class _Shape:
    """A shape double. `occ_distance` only ever writes it out, which the writer stub intercepts."""


@pytest.fixture(autouse=True)
def _writes(monkeypatch):
    """The BREP write is the one kernel call in the parent; stub it so these tests stay hermetic."""
    monkeypatch.setattr(od, "_write_brep", lambda shape, path: path.write_text("brep"))


def test_a_hanging_child_is_bounded_and_reports_unmeasured(monkeypatch):
    """THE BUG THIS EXISTS FOR. BRepExtrema_DistShapeShape ran IN-PROCESS and held the GIL, so a
    wave-5 shard sat in it with neither the tool clock nor the region wall able to interrupt it
    (lane B's py-spy, investigator_tools.py:410). A child that will not answer must be killed and
    reported as unmeasured, never as a distance of 0."""
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", "import time; time.sleep(30)"])

    started = time.monotonic()
    out = od.distance_between(_Shape(), _Shape(), budget_s=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 7.0, f"the bound did not hold: {elapsed:.1f}s"   # 1s budget + 5s reap + slack
    assert out["min_distance_mm"] is None
    assert out["point_a"] is None and out["point_b"] is None
    assert out["distance_status"] == "unmeasured: budget_spent"


def test_it_never_forks(monkeypatch):
    """THE REPO RULE: a forked child inherits the parent's OCC state and its locks. Only a FRESH
    interpreter is allowed, so a fork here is a test failure rather than a slow surprise."""
    forks: list[object] = []
    monkeypatch.setattr(os, "fork", lambda *a, **k: forks.append(a) or 0)
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", "pass"])

    out = od.distance_between(_Shape(), _Shape(), budget_s=1.0)

    assert forks == [], "os.fork is forbidden: use a fresh interpreter"
    assert out["min_distance_mm"] is None          # the stub answers nothing, so unmeasured


def test_a_child_answer_is_read_back(monkeypatch):
    answer = {"min_distance_mm": 1.25, "point_a": [0.0, 0.0, 0.0], "point_b": [1.25, 0.0, 0.0]}
    monkeypatch.setattr(od, "_CHILD_CMD",
                        [sys.executable, "-c", f"print({json.dumps(json.dumps(answer))})"])

    out = od.distance_between(_Shape(), _Shape(), budget_s=5.0)

    assert out["min_distance_mm"] == 1.25
    assert out["point_b"] == [1.25, 0.0, 0.0]
    assert out["distance_status"] == "measured"


def test_no_budget_means_no_child_at_all(monkeypatch):
    """A measurement with no time left is not attempted: starting a child we must immediately kill
    spends the clock on nothing."""
    popens: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popens.append(a))

    out = od.distance_between(_Shape(), _Shape(), budget_s=0.0)

    assert popens == [], "no child may be started without budget"
    assert out["min_distance_mm"] is None
    assert out["distance_status"] == "unmeasured: no budget"


def test_a_dead_child_is_not_a_zero(monkeypatch):
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", "import sys; sys.exit(3)"])

    out = od.distance_between(_Shape(), _Shape(), budget_s=5.0)

    assert out["min_distance_mm"] is None
    assert "unmeasured" in out["distance_status"]


def test_time_spent_writing_is_taken_from_the_childs_allowance(monkeypatch):
    """The caller's budget covers the whole call. Writing both BREPs spends part of it, so the child
    gets only what is left, never the full allowance a second time."""
    def _slow_write(shape, path):
        time.sleep(0.3)
        path.write_text("brep")

    seen: list[float] = []
    monkeypatch.setattr(od, "_write_brep", _slow_write)
    monkeypatch.setattr(od, "_run_child", lambda a, b, budget: seen.append(budget) or {})

    od.distance_between(_Shape(), _Shape(), budget_s=2.0)

    assert len(seen) == 1 and seen[0] <= 2.0 - 0.6 + 0.05, seen


def test_writes_that_spend_the_whole_budget_start_no_child(monkeypatch):
    def _slow_write(shape, path):
        time.sleep(0.4)
        path.write_text("brep")

    popens: list[object] = []
    monkeypatch.setattr(od, "_write_brep", _slow_write)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popens.append(a))

    out = od.distance_between(_Shape(), _Shape(), budget_s=0.5)

    assert popens == []
    assert out["min_distance_mm"] is None
    assert out["distance_status"] == "unmeasured: budget_spent"
