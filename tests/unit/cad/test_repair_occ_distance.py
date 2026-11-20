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


# --- many pairs in ONE child, with partial progress kept --------------------------------------
# `_nearest_faces` measures a vertex against every candidate face, `_contact_support_faces` up to
# eight solid pairs, and `deviation._distance_to` one sample at a time inside every rehearsal.
# Each had a deadline checked only BETWEEN calls, so one slow call could not be interrupted. A
# child per call would cost more in process starts than it saves, so a call measures its whole
# batch in one child - and what the child finished before the budget ran out is kept, because
# these callers already act on partial answers.

def test_a_batch_is_measured_in_one_child(monkeypatch):
    # The child prints JSON; the stub has to print the SAME JSON, so the dicts go in as Python
    # literals via repr. (Embedding `json.dumps` output in a `-c` script plants a bare `null`,
    # which is not Python - my first version of this test failed on exactly that.)
    answers = [{"min_distance_mm": 1.0, "point_a": None, "point_b": None},
               {"min_distance_mm": 2.0, "point_a": None, "point_b": None}]
    script = "; ".join(["import json",
                        f"print(json.dumps({answers[0]!r}))",
                        f"print(json.dumps({answers[1]!r}))"])
    starts = {"n": 0}
    real = subprocess.Popen

    def counting(*a, **k):
        starts["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting)
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", script])

    out = od.distances([_Shape(), _Shape(), _Shape()], [(0, 1), (0, 2)], budget_s=5.0)

    assert [o["min_distance_mm"] for o in out] == [1.0, 2.0]
    assert starts["n"] == 1, "one child for the whole batch"


def test_what_the_child_finished_before_the_budget_is_kept(monkeypatch):
    """PARTIAL PROGRESS IS THE POINT. One slow pair must not discard the pairs already measured -
    the old code's deadline between calls at least kept those."""
    first = {"min_distance_mm": 7.0, "point_a": None, "point_b": None}
    script = ("import json,sys,time; "
              f"print(json.dumps({first!r})); sys.stdout.flush(); time.sleep(30)")
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", script])

    started = time.monotonic()
    out = od.distances([_Shape(), _Shape(), _Shape()], [(0, 1), (0, 2)], budget_s=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"the bound did not hold: {elapsed:.1f}s"
    assert out[0]["min_distance_mm"] == 7.0, "the finished pair survives"
    assert out[1]["min_distance_mm"] is None, "the unfinished one is unmeasured, not 0"
    assert "unmeasured" in out[1]["distance_status"]


def test_an_empty_batch_starts_no_child(monkeypatch):
    def _no_popen(*_a, **_k):
        raise AssertionError("no child for an empty batch")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)
    assert od.distances([_Shape()], [], budget_s=5.0) == []


def test_a_batch_with_no_budget_is_all_unmeasured(monkeypatch):
    def _no_popen(*_a, **_k):
        raise AssertionError("no child without budget")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    out = od.distances([_Shape(), _Shape()], [(0, 1)], budget_s=0.0)

    assert len(out) == 1 and out[0]["min_distance_mm"] is None


def test_batch_writes_are_taken_from_the_childs_allowance(monkeypatch):
    """The batch budget covers preparing every shape and the manifest, not just the child."""
    def _slow_write(shape, path):
        time.sleep(0.3)
        path.write_text("brep")

    seen: list[float] = []
    monkeypatch.setattr(od, "_write_brep", _slow_write)
    monkeypatch.setattr(od, "_run_batch",
                        lambda root, manifest, count, budget: seen.append(budget) or [])

    od.distances([_Shape(), _Shape()], [(0, 1)], budget_s=2.0)

    assert len(seen) == 1 and seen[0] <= 2.0 - 0.6 + 0.05, seen


def test_batch_writes_that_spend_the_budget_start_no_child(monkeypatch):
    def _slow_write(shape, path):
        time.sleep(0.4)
        path.write_text("brep")

    popens: list[object] = []
    monkeypatch.setattr(od, "_write_brep", _slow_write)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popens.append(a))

    out = od.distances([_Shape(), _Shape(), _Shape()], [(0, 1), (1, 2)], budget_s=0.5)

    assert popens == []
    assert [o["distance_status"] for o in out] == ["unmeasured: budget_spent"] * 2


def test_the_batch_child_answers_in_order_reuses_shapes_and_survives_a_failed_pair(
        monkeypatch, tmp_path, capsys):
    reads: list[str] = []

    def _read(path):
        reads.append(path.name)
        return path.stem

    def _measure(a, b):
        if (a, b) == ("1", "2"):
            raise RuntimeError("kernel refused")
        return {"min_distance_mm": float(int(a) * 10 + int(b)), "point_a": None, "point_b": None}

    monkeypatch.setattr(od, "_read_brep", _read)
    monkeypatch.setattr(od, "_measure_here", _measure)
    manifest = tmp_path / "pairs.json"
    manifest.write_text(json.dumps([[0, 1], [1, 2], [0, 2]]))

    assert od._child_main(["--batch", str(tmp_path), str(manifest)]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["min_distance_mm"] for line in lines] == [1.0, None, 2.0]
    assert sorted(reads) == ["0.brep", "1.brep", "2.brep"], "each shape is read once"


def _manifest(tmp_path, spec):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(spec))
    return path


def test_the_child_prunes_exactly_where_the_parent_did(monkeypatch, tmp_path):
    """WHY THE CHILD AND NOT THE PARENT. `_nearest_faces` measures candidates in order of box
    distance and stops at the first whose box is already further than the best EXACT distance it
    has - a rule that needs the distances measured so far, which only the child has. Handing the
    child a flat list of every candidate would measure faces the parent would have skipped, so the
    pruning travels with the batch: the ordered bounds and the tolerance."""
    measured: list[tuple[int, int]] = []
    monkeypatch.setattr(od, "_read_brep", lambda path: _Shape())
    monkeypatch.setattr(od, "_measure_here",
                        lambda a, b: measured.append((0, 0)) or {"min_distance_mm": 4.0,
                                                                 "point_a": None, "point_b": None})

    manifest = _manifest(tmp_path, {"pairs": [[0, i] for i in range(1, 6)],
                                    "bounds": [0.0, 1.0, 9.0, 20.0, 30.0], "tol": 0.5})
    od._batch_main(tmp_path, manifest)

    assert len(measured) == 2, (
        "the third candidate's box is 9.0 away and the best exact distance is 4.0, so nothing from "
        f"the third on can be nearest - it must not be measured (measured {len(measured)})")


def test_a_batch_without_bounds_measures_every_pair(monkeypatch, tmp_path):
    """Pruning is opt-in: the callers that have no ordering (a deviation sample, a contact pair
    under its cap) still want every pair they asked for."""
    measured: list[int] = []
    monkeypatch.setattr(od, "_read_brep", lambda path: _Shape())
    monkeypatch.setattr(od, "_measure_here",
                        lambda a, b: measured.append(1) or {"min_distance_mm": 4.0,
                                                            "point_a": None, "point_b": None})

    od._batch_main(tmp_path, _manifest(tmp_path, [[0, i] for i in range(1, 6)]))

    assert len(measured) == 5


def test_a_pruned_tail_is_not_an_unmeasured_tail(monkeypatch):
    """A PRUNED PAIR AND AN UNANSWERED PAIR MEAN OPPOSITE THINGS. Pruned is a proof - nothing from
    there on can be nearest - and the caller may act on the answer. Unanswered is ignorance, and
    the caller must report that it did not finish. One status for both would turn a bounded search
    into a silent wrong answer, so they are told apart."""
    lines = [json.dumps({"min_distance_mm": 4.0, "point_a": None, "point_b": None}),
             json.dumps({"min_distance_mm": 4.5, "point_a": None, "point_b": None}),
             json.dumps({"pruned_after": 2})]
    script = "\n".join(f"print({json.dumps(line)})" for line in lines)
    monkeypatch.setattr(od, "_CHILD_CMD", [sys.executable, "-c", script])

    out = od.distances([_Shape()] * 6, [(0, i) for i in range(1, 6)],
                       budget_s=5.0, bounds=[0.0, 1.0, 9.0, 20.0, 30.0], tol=0.5)

    assert [a["distance_status"] for a in out] == ["measured", "measured", "pruned", "pruned",
                                                   "pruned"]
    assert all(a["min_distance_mm"] is None for a in out[2:]), "a pruned pair has no distance"


def test_the_bounds_and_the_tolerance_reach_the_child(monkeypatch):
    seen: dict = {}

    def _batch(root, manifest, count, budget):
        seen.update(json.loads(manifest.read_text()))
        return [dict(od.UNMEASURED) for _ in range(count)]

    monkeypatch.setattr(od, "_run_batch", _batch)

    od.distances([_Shape()] * 3, [(0, 1), (0, 2)], budget_s=5.0, bounds=[0.0, 7.0], tol=0.25)

    assert seen["bounds"] == [0.0, 7.0] and seen["tol"] == 0.25
    assert seen["pairs"] == [[0, 1], [0, 2]]
