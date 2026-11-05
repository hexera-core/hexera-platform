# Responsibility: Verify a geometry naming whose scout is still running gives its worker slot to a waiting task (back to the queue's end, bounded), and otherwise runs as before (shared dev 2 Nov: the Camaro's naming queued 4 minutes behind the punch's scout and the punch's waiting naming).
from __future__ import annotations

import pytest

from meshpipeline.adapters.pipeline_execution import celery as tasks
from meshpipeline.adapters.pipeline_execution import geometry_running as GR
from meshpipeline.application import geometry_check as gc


@pytest.fixture()
def world(monkeypatch):
    state = {"scouting": True, "waiting": 1, "queued": [], "ran": []}
    monkeypatch.setattr(gc, "scout_still_running", lambda sid: state["scouting"])
    monkeypatch.setattr(GR, "waiting", lambda **_k: state["waiting"])
    monkeypatch.setattr(gc, "naming_limits_for", lambda sid: (100, 120))
    monkeypatch.setattr(gc, "run_geometry_naming", lambda **kw: state["ran"].append(kw) or {"status": "ready"})
    # the unit tier's Celery stub leaves the task a plain function: its re-queue is stood in
    monkeypatch.setattr(tasks.name_geometry, "apply_async", lambda **kw: state["queued"].append(kw),
                        raising=False)
    return state


def _name(**extra):
    return tasks.name_geometry(session_id="s1", owner_id="o", purpose_text="FEA", interpretation=None, **extra)


def test_a_naming_that_could_only_wait_goes_back_behind_the_waiting_task(world):
    out = _name()
    assert out == {"status": "yielded", "yielded": 1}
    assert not world["ran"]
    (again,) = world["queued"]
    assert again["kwargs"][tasks.NAMING_YIELDED] == 1
    assert again["task_id"] == "geometry-naming-s1"
    assert (again["soft_time_limit"], again["time_limit"]) == (100, 120)


def test_a_naming_runs_at_once_when_nothing_waits(world):
    world["waiting"] = 0
    assert _name() == {"status": "ready"}
    assert not world["queued"] and world["ran"]


def test_a_naming_whose_scout_has_landed_runs_at_once(world):
    world["scouting"] = False
    assert _name() == {"status": "ready"}
    assert not world["queued"]


def test_the_yields_are_bounded_and_never_reach_the_naming_body(world):
    assert _name(**{tasks.NAMING_YIELDED: tasks.NAMING_YIELD_MAX}) == {"status": "ready"}
    assert tasks.NAMING_YIELDED not in world["ran"][0]


def test_scout_still_running_reads_the_stored_check(monkeypatch):
    monkeypatch.setattr(gc, "read_check", lambda sid: None)
    assert gc.scout_still_running("s") is True
    monkeypatch.setattr(gc, "read_check", lambda sid: {"status": gc.STATUS_PENDING})
    assert gc.scout_still_running("s") is True
    monkeypatch.setattr(gc, "read_check", lambda sid: {"status": gc.STATUS_SCOUTED, "proposal": {}})
    monkeypatch.setattr(gc, "is_preview", lambda st: False)
    assert gc.scout_still_running("s") is False


def test_waiting_never_raises():
    def broken():
        raise ConnectionError("no redis")
    assert GR.waiting(client_factory=broken, prefix="") == 0
