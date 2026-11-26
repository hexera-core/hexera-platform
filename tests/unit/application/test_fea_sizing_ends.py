# Responsibility: Verify TIME-11 - the FEA sizing the geometry check runs after the stage is shown always ends: past its marker's deadline or with its worker gone it is served as not sized in plain words (the stage and the chat's answer stop waiting; the run sizes it), its clock's stop is said in minutes, its child stops itself and never outlives an interrupted wait, a yielded naming keeps the sizing's time, and the joins measure keeps its budget inside a pair of parts.
# Boundaries: no worker, no store, no model: the check, the progress record and the clock are stood in; the joins measure runs on two blocks built here.
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from meshpipeline.application import fea_check as FC
from meshpipeline.application import geometry_check as gc
from meshpipeline.engines import fea_plan_options as PO

NOW = 1_000_000.0


def _facts(**mark) -> dict:
    return {"x": 1, FC.PLAN_FACT_KEY: {"sizing": True, "token": "t", **mark}}


def _ready(facts: dict) -> dict:
    return {"status": gc.STATUS_READY, "named": True, "facts": facts}


def test_a_sizing_past_its_deadline_is_stopped_by_its_clock_in_plain_words():
    got = FC.sizing_now(_facts(until=NOW - 1), {"beat_at": NOW}, NOW)
    plan = got[FC.PLAN_FACT_KEY]
    assert plan == {"sized": False, "stopped": "clock", "why": "Sizing the mesh did not finish in 30 minutes",
                    "rule": "TIME-11"}
    assert got["x"] == 1
    view = FC.plan_view(got)
    assert view["state"] == "not_sized"
    assert view["words"].startswith("Sizing the mesh did not finish in 30 minutes: the run sizes it")


def test_a_sizing_whose_naming_ended_without_storing_it_is_its_worker_gone():
    got = FC.sizing_now(_facts(until=NOW + 600), {"ended_at": NOW - FC.SIZING_END_GRACE_S - 1}, NOW)
    assert got[FC.PLAN_FACT_KEY]["stopped"] == "worker"
    assert FC.plan_view(got)["words"].startswith("The worker sizing the mesh stopped before it finished: the run")


def test_a_sizing_whose_worker_went_quiet_is_its_worker_gone():
    from meshpipeline.application import geometry_progress as gp
    got = FC.sizing_now(_facts(), {"picked_at": NOW - 900, "beat_at": NOW - gp.BEAT_STALE_S - 1}, NOW)
    assert got[FC.PLAN_FACT_KEY]["stopped"] == "worker"


@pytest.mark.parametrize("naming", [{"picked_at": NOW - 60, "beat_at": NOW - 20},     # beating
                                    {"ended_at": NOW - 5},                          # just ended: stored a moment ago
                                    None])                                          # no record: nothing to go on
def test_a_sizing_still_at_work_is_still_sizing(naming):
    facts = _facts(until=NOW + 600)
    assert FC.sizing_now(facts, naming, NOW) is facts
    assert FC.plan_view(facts)["state"] == "sizing"


def test_every_reader_of_the_check_gets_the_settled_sizing():
    stored = _ready(_facts(until=NOW - 1))
    served = gc.time_boxed(stored, None, now=NOW, progress={"naming": {"ended_at": NOW - 600}})
    assert served["facts"][FC.PLAN_FACT_KEY]["sized"] is False
    assert stored["facts"][FC.PLAN_FACT_KEY]["sizing"] is True          # nothing stored, nothing changed
    live = _ready(_facts(until=NOW + 600))
    assert gc.time_boxed(live, None, now=NOW, progress={"naming": {"beat_at": NOW}}) is live


def test_the_chat_answer_stops_waiting_on_a_sizing_whose_worker_is_gone(monkeypatch):
    from meshpipeline.application import geometry_progress as gp
    stored = _ready(_facts(until=time.time() + 600))
    monkeypatch.setattr(gc, "read_check", lambda _s: stored)
    monkeypatch.setattr(gc, "naming_requested", lambda _s: None)
    monkeypatch.setattr(gp, "read_progress", lambda _s: {"naming": {"beat_at": time.time()}})
    assert FC.planned_sizes("s1")["why"] == FC.SIZES_BEING_PLANNED           # still at work: said so
    monkeypatch.setattr(gp, "read_progress", lambda _s: {"naming": {"ended_at": time.time() - 600}})
    got = FC.planned_sizes("s1")
    assert got["known"] is False and got["why"] == FC.SIZES_AT_RUN            # never "in a moment" for ever


def test_the_marker_deadline_covers_the_sizings_clock_and_its_margins():
    assert gc.sizing_deadline_s() == PO.BUDGET_S + gc.SIZING_MARGIN_S + gc.STALE_MARGIN_S


def test_the_childs_clock_is_told_and_it_stops_itself_past_it(monkeypatch, tmp_path):
    seen: dict = {}

    def timed_out(cmd, **kw):
        seen["cmd"] = cmd
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    import meshpipeline.sandbox.safe_exec as SE
    monkeypatch.setattr(SE, "run_guarded", timed_out)
    told: dict = {}
    assert PO.bounded(tmp_path / "p.step", to_metres=1e-3, budget_s=100.0, told=told) is None
    assert told == {"out_of_time": True}
    cmd = seen["cmd"]
    assert cmd[cmd.index("--limit-s") + 1] == f"{100.0 + PO.CHILD_GRACE_S:.0f}"


def test_the_child_sets_its_own_alarm_and_gives_way_on_the_cpu(monkeypatch, tmp_path):
    import os
    import signal
    calls: dict = {}
    monkeypatch.setattr(signal, "alarm", lambda s: calls.setdefault("alarm", s))
    monkeypatch.setattr(os, "nice", lambda n: calls.setdefault("nice", n))
    monkeypatch.setattr(PO, "stage_file", lambda *a, **k: None)
    monkeypatch.setattr(PO, "plan_staged", lambda *a, **k: {"full": {}})
    assert PO._main([str(tmp_path / "p.step"), "--limit-s", "1860"]) == 0
    assert calls == {"alarm": 1860, "nice": PO.CHILD_NICE}


def test_an_interrupted_wait_stops_the_child_with_it(monkeypatch):
    import signal

    from meshpipeline.sandbox import safe_exec as SE

    started: list = []
    real = subprocess.Popen

    class Seen(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)

    class Stop(BaseException):
        pass

    def stop(_sig, _frame):
        raise Stop()

    monkeypatch.setattr(SE.subprocess, "Popen", Seen)
    old = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        with pytest.raises(Stop):
            SE.run_guarded([sys.executable, "-c", "import time; time.sleep(60)"], timeout=120, capture_output=True)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    (proc,) = started
    assert proc.poll() is not None                     # stopped, not left sizing on its own


def test_a_yielded_naming_keeps_the_sizings_time(monkeypatch):
    from meshpipeline.adapters.pipeline_execution import celery as tasks
    from meshpipeline.adapters.pipeline_execution import geometry_running as GR
    queued: list = []
    monkeypatch.setattr(gc, "scout_still_running", lambda sid: True)
    monkeypatch.setattr(GR, "waiting", lambda **_k: 1)
    monkeypatch.setattr(gc, "naming_limits_for", lambda sid: (100, 120))
    monkeypatch.setattr(tasks.name_geometry, "apply_async", lambda **kw: queued.append(kw), raising=False)
    tasks.name_geometry(session_id="s1", owner_id="o", purpose_text="FEA", interpretation=None)
    (again,) = queued
    assert (again["soft_time_limit"], again["time_limit"]) == gc.naming_task_limits("s1")


def test_the_joins_budget_holds_inside_a_pair_of_parts(monkeypatch):
    pytest.importorskip("OCP.BRepPrimAPI")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad import fea_joins as J

    parts = [BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape(),
             BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10.0, 10.0, 10.0).Shape()]
    whole = J._measure(parts, tol=1e-3, mm=1.0, budget_s=600.0, t0=time.time(), out=J.Measured(tol=1e-3))
    assert (0, 1) in whole.joints and whole.complete            # measured in time: the touch is found

    class Clock:
        """The pair starts in time; every read after that is past the budget (a pair of many faces)."""
        n = 0

        def time(self) -> float:
            self.n += 1
            return 0.0 if self.n == 1 else 10_000.0

    monkeypatch.setattr(J, "time", Clock())
    late = J._measure(parts, tol=1e-3, mm=1.0, budget_s=600.0, t0=0.0, out=J.Measured(tol=1e-3))
    assert late.complete is False and (0, 1) not in late.joints and (0, 1) not in late.unmeasured
