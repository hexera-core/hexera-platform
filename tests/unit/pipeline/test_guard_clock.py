# Responsibility: Verify D3 (docs/RULES.md TIME-1..5): planned work sets every guard, the same inputs give the same decision whatever the elapsed time, and a guard trip is an infrastructure failure - replayed once as the same attempt, then "ran out of time", never a lever, an engine switch or a verdict.
# Boundaries: contracts/work_limits, engines/determinism's trip record, pipeline/executor's trip reading and pipeline/graph's routing; nothing native runs and no clock is read for a decision.
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline.contracts import work_limits as W
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines import determinism as det
from meshpipeline.pipeline import graph as G

# #
# the guard arithmetic (TIME-2)
# #


def test_a_guard_is_three_times_the_planned_time_between_its_floor_and_ceiling():
    assert W.guard_seconds(None, floor_s=1200, ceiling_s=3000) == 1200     # no plan: the floor
    assert W.guard_seconds(100.0, floor_s=1200, ceiling_s=3000) == 1200    # never under the floor
    assert W.guard_seconds(600.0, floor_s=1200, ceiling_s=3000) == 1800    # 3 x the plan
    assert W.guard_seconds(5000.0, floor_s=1200, ceiling_s=3000) == 3000   # never over the ceiling
    # an old cap is never shortened, even by a ceiling under it
    assert W.guard_seconds(5000.0, floor_s=2400, ceiling_s=1500) == 2400


def test_an_engine_with_no_lab_rate_keeps_its_old_cap():
    for engine in ("gmsh", "snappy_multiregion", "snapgrid"):
        work = W.guard_for(engine, cells=5_000_000)
        assert work.planned_seconds is None
        assert work.guard_seconds == int(W.FLOORS_S[engine])


def test_every_engine_guard_is_at_least_its_old_cap():
    for engine, floor in W.FLOORS_S.items():
        for cells in (None, 1, 100_000, 2_000_000, 8_000_000, 40_000_000):
            assert W.guard_for(engine, cells=cells).guard_seconds >= floor


def test_the_guard_replays_from_its_recorded_planned_work():
    work = W.guard_for("snappy", cells=2_000_000)
    guard, rule = W.work_limit(work.as_dict())
    assert (guard, rule) == (work.guard_seconds, W.TIME_GUARD) == (4000, "TIME-2")
    # the dict that goes into the why record survives JSON
    assert W.work_limit(json.loads(json.dumps(work.as_dict()))) == (4000, "TIME-2")


def test_the_fea_driver_gets_the_same_interface():
    # FEA step 4: planned elements at its own measured rate, the old fixed cap as the floor
    work = W.PlannedWork(where="fea.gmsh", unit="elements", amount=3_000_000, rate=5000.0,
                         floor_s=600.0, ceiling_s=3600.0)
    assert work.planned_seconds == 600.0 and work.guard_seconds == 1800
    assert W.work_limit(work.as_dict()) == (1800, "TIME-2")


# #
# after a trip (TIME-3): counts decide, never the clock
# #

def test_a_trip_is_replayed_once_then_the_run_ran_out_of_time():
    assert W.after_trip({"replays": 0}) == (W.REPLAY, "TIME-3")
    assert W.after_trip({"replays": 1}) == (W.RAN_OUT_OF_TIME, "TIME-3")
    assert W.after_trip({"replays": 0, "allowed": 0}) == (W.RAN_OUT_OF_TIME, "TIME-3")


def _tripped(**over) -> dict:
    state = {"job_id": "j", "engine": "snappy", "retry_count": 1, "executor_success": False,
             "executor_failed_gate": "finalize",
             "executor_failure_cause": FailureCause.ENGINE_TIMED_OUT.value,
             "executor_failure_facts": {"engine": "snappyHexMesh",
                                        "guard": {"where": "snappy", "guard_s": 4000.0}}}
    state.update(over)
    return state


def test_a_trip_is_routed_to_the_replay_never_to_the_classifier_or_the_ladder():
    assert G.route_after_executor(_tripped()) == "node_guard_replay"
    # ...however many regular attempts are left: a trip spends none and moves nothing
    assert G.route_after_executor(_tripped(retry_count=0)) == "node_guard_replay"
    # after its one replay, a second trip ends the run as ran out of time
    assert G.route_after_executor(_tripped(guard_replay_count=1)) == G.END


def test_a_timeout_that_is_not_a_guard_trip_routes_as_before():
    # a cause with no `guard` fact (an older record) keeps the old path
    plain = _tripped(executor_failure_facts={"engine": "snappyHexMesh"})
    assert G.guard_trip_of(plain) == {}
    assert G.route_after_executor(plain) != "node_guard_replay"


def test_the_replay_is_the_same_attempt_with_its_trips_cleared(tmp_path, monkeypatch):
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    det.note_guard_trip("snappy", workspace=ws, guard_s=4000.0)
    (ws / det.STAMP_FILE).write_text(json.dumps({"guard_trips": [{"where": "cfmesh"}]}))
    assert det.guard_trips(ws)

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr("asyncio.sleep", _noop)
    out = asyncio.run(G.node_guard_replay(_tripped(openfoam_workspace=str(ws), retry_count=2)))
    assert out["guard_replay_count"] == 1
    assert out["retry_count"] == 1, "the builder prepares the same attempt number again"
    assert out["executor_failure_cause"] == "" and out["executor_failure_facts"] == {}
    assert det.guard_trips(ws) == []


def test_the_replay_starts_from_the_workspace_the_attempt_started_from(tmp_path):
    # TIME-3 "as it was": what the tripped attempt wrote (plan memory, a mesh, a reshuffled
    # surface) would make its replay decide differently from a quiet machine's run
    ws = tmp_path / "attempt_2"
    (ws / "system").mkdir(parents=True)
    (ws / "input.stl").write_text("solid as staged")
    (ws / "system" / "meshDict").write_text("as authored")
    det.mark_attempt_start(ws)
    (ws / ".last_plan.json").write_text('{"max_cells": 3000000}')
    (ws / "input.stl").write_text("solid reshuffled")
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "owner").write_text("x")
    det.mark_attempt_start(ws)                    # a second mark keeps the first start
    assert det.restore_attempt_start(ws) is True
    assert sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()) == [
        "input.stl", "system/meshDict"]
    assert (ws / "input.stl").read_text() == "solid as staged"
    # the copy sits beside the workspace, never in it (no payload, no case hash sees it)
    assert not any(p.name.endswith(".start") for p in ws.iterdir())
    det.drop_attempt_start(ws)
    assert det.restore_attempt_start(ws) is False


def test_the_routing_cites_the_rule_in_the_why_record():
    from meshpipeline.contracts import decision_record as dr
    G.route_after_executor(_tripped(job_id="cite-guard"))
    G.route_after_executor(_tripped(job_id="cite-guard", guard_replay_count=1))
    got = [(e["stage"], e["value"], e["rule"], e["source"]) for e in dr.cited_for("cite-guard")]
    assert got == [("guard", "replay", "TIME-3", "guard-clock"),
                   ("guard", "ran_out_of_time", "TIME-3", "guard-clock")]
    assert dr.replay(dr.cited_for("cite-guard")) == []


# #
# the executor reads a trip, whatever is on disk
# #

def test_the_executor_reads_a_trip_beside_the_case_and_ignores_a_waits(tmp_path):
    from meshpipeline.pipeline import executor as X
    assert X._guard_trip({"engine": "snappy"}, tmp_path) == {}
    det.note_guard_trip("engine_ladder", workspace=tmp_path, rule=W.TIME_WAIT, guard_s=900.0)
    assert X._guard_trip({"engine": "snappy"}, tmp_path) == {}, "a wait's trip is that step's"
    det.note_guard_trip("passage.measure", workspace=tmp_path, guard_s=240.0)
    assert X._guard_trip({"engine": "snappy"}, tmp_path)["where"] == "passage.measure"


def test_the_executor_reads_a_native_run_stopped_at_its_guard(tmp_path):
    from meshpipeline.contracts import mesh_execution as ME
    from meshpipeline.pipeline import executor as X
    ME.open_native_record(tmp_path)
    ME._note_native_run(tmp_path, {"rc": -1, "timed_out": True})
    trip = X._guard_trip({"engine": "cfmesh"}, tmp_path)
    assert trip and trip["native_run"] is True


def test_a_tripped_attempt_is_never_judged_whatever_is_on_disk(tmp_path, monkeypatch):
    from meshpipeline.pipeline import executor as X
    # a valid-looking mesh is there (a rescue cut short kept "as built"): it is not judged
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("x")
    (tmp_path / det.STAMP_FILE).write_text(json.dumps(
        {"guard_trips": [{"where": "cfmesh.rescue", "rule": "TIME-3", "step": "optimised"}]}))

    class _Pub:
        async def astage(self, *a, **k): pass
        async def anote(self, *a, **k): pass
        async def acheck(self, *a, **k): pass

    async def _owner(*_a, **_k):
        return None
    monkeypatch.setattr(X, "execution_publisher", lambda *a, **k: _Pub())
    monkeypatch.setattr("meshpipeline.application.execution_fence.assert_current_owner", _owner)
    monkeypatch.setattr(X, "get_engine", lambda *_a: pytest.fail("finalize ran on a trip"))
    out = asyncio.run(X.node_executor({"job_id": "j", "engine": "cfmesh",
                                       "openfoam_workspace": str(tmp_path)}))
    assert out["executor_success"] is False
    assert out["executor_failure_cause"] == FailureCause.ENGINE_TIMED_OUT.value
    assert out["executor_failure_facts"]["guard"]["where"] == "cfmesh.rescue"
    assert G.route_after_executor({**out, "job_id": "j", "retry_count": 1}) == "node_guard_replay"


# #
# the waits (TIME-4): long guards, never a clock-chosen answer
# #

def test_the_waits_are_guards_far_above_the_work_they_wait_for():
    from meshpipeline.agents.intake import measured_choice
    from meshpipeline.pipeline import engine_fallback
    assert engine_fallback.ORDER_WAIT_S >= 900.0
    assert measured_choice.MEASURE_WAIT_S >= 300.0


def test_a_wait_that_trips_is_recorded_as_a_guard_trip_not_a_time_limited_decision():
    from meshpipeline.agents.intake import measured_choice

    def _hang(**_kw):
        import time
        time.sleep(0.2)

    det.drain_process_notes()
    det.drain_process_guard_trips()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(measured_choice, "rank", _hang)
        assert asyncio.run(measured_choice.rank_in_time(timeout_s=0.01)) is None
    assert det.drain_process_notes() == []
    trips = det.drain_process_guard_trips()
    assert [(t["where"], t["rule"]) for t in trips] == [("intake.recommendation", "TIME-4")]
