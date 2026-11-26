# Responsibility: Verify a repair GOAL travels payload -> request -> state, that only the exact
#                 live posture lets it reach the repair, that an unset goal is byte-for-byte
#                 today's run, and that a customer's auto-accept threshold is judged before it can
#                 widen anything.
from __future__ import annotations

import asyncio
import inspect

import pytest
from tests._geometry_support import geometry_state

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.application import dispatch_contract
from meshpipeline.application.pipeline_run import JobRequest, run_pipeline
from meshpipeline.cad.repair import review_tier
from meshpipeline.cad.repair.contracts import RepairGoal, RepairStatus
from meshpipeline.pipeline import data_contract as dc
from meshpipeline.pipeline import repair_goal as rg
from meshpipeline.pipeline.graph import route_after_repair_attempt
from meshpipeline.pipeline.repair_attempt import node_repair_attempt


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTONOMOUS", True)


# The repair node publishes the run's Repair check step through the ownership-checked port, as
# tests/unit/pipeline/test_repair_attempt.py does: this suite exercises the goal's plumbing, not
# Redis or PostgreSQL, so the port is stood in for.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    return install(monkeypatch, _ep)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "live", raising=False)


# ---------------------------------------------------------------- the registry and the plumbing


def test_dispatch_kwargs_registry_lists_repair_goal():
    assert "repair_goal" in dc.dispatch_kwargs()
    assert "repair_goal" in dc.STATE_FIELDS
    # the standalone ending is its own declared field - the graph drops an undeclared one between
    # the node that writes it and the terminal path that delivers what it names
    assert "repair_delivery" in dc.STATE_FIELDS
    assert "repair_goal" in dispatch_contract.accepted_keys()
    assert "repair_goal" in JobRequest.__slots__
    assert inspect.signature(run_pipeline).parameters["repair_goal"].default is None


def test_unset_goal_is_todays_payload_and_state():
    # a request that says nothing carries nothing, so `from_state` has no goal to find and every
    # reader below sees exactly the run it saw before this field existed
    req = JobRequest(job_id="job-1")
    assert req.repair_goal == {}
    assert rg.from_state({"repair_goal": {}}) is None
    assert rg.from_state({}) is None


# ---------------------------------------------------------------- the posture, failing closed


@pytest.mark.parametrize(("posture", "expected"), [
    ("live", "live"),
    ("off", "off"),
    ("", "off"),
    ("LIVE", "off"),
    ("live ", "off"),
    ("shadow", "off"),
    (None, "off"),
])
def test_only_the_exact_live_string_turns_standalone_jobs_on(monkeypatch, posture, expected):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", posture, raising=False)
    assert rg.posture() == expected


def test_a_customer_goal_is_honoured_whatever_the_posture(monkeypatch):
    # CRS v5 beta, Decision 1: the three-way choice is every customer's; the posture gates only
    # the operator's admin re-run route (api/v1/admin_repair.py)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "off", raising=False)
    asked = rg.from_state({"repair_goal": {"goal": "standalone"}})
    assert asked is not None and asked.goal is RepairGoal.standalone


# ---------------------------------------------------------------- the untrusted threshold


#: (declared value, accepted?). BOTH SIDES, and pinned literally: a table parametrised on the
#: constant under test passes against any bound at all.
_THRESHOLDS = [
    (None, True),
    (0.0, True),
    (0.05, True),
    (1.0, True),
    (1.0000001, False),
    (2.0, False),
    (1e9, False),
    (-0.0001, False),
    (-1.0, False),
    (float("inf"), False),
    (float("-inf"), False),
    (float("nan"), False),
    ("0.05", False),
    (True, False),
    ([0.05], False),
]


@pytest.mark.parametrize(("value", "accepted"), _THRESHOLDS)
def test_the_customers_threshold_is_bounded_before_it_can_widen_anything(value, accepted):
    payload = {"goal": "standalone", "auto_accept_deviation_mm": value}
    if accepted:
        parsed = rg.parse(payload)
        assert parsed.auto_accept_mm == (None if value is None else float(value))
    else:
        with pytest.raises(rg.GoalRequestError):
            rg.parse(payload)


def test_the_bound_is_one_millimetre():
    # PINNED. The table above is only two-sided if the boundary it brackets is this number.
    assert rg.MAX_AUTO_ACCEPT_MM == 1.0


@pytest.mark.parametrize("name", ["", "benchmark", "Standalone", "mesh", "none", None, 3])
def test_a_request_may_only_name_a_customer_goal(name):
    with pytest.raises(rg.GoalRequestError):
        rg.parse({"goal": name, "auto_accept_deviation_mm": None})


def test_the_two_customer_goals_parse():
    assert rg.parse({"goal": "standalone"}).goal is RepairGoal.standalone
    assert rg.parse({"goal": "meshing"}).goal is RepairGoal.meshing
    assert RepairGoal.benchmark not in rg.REQUESTABLE


def test_a_state_field_nobody_can_read_is_no_goal(live):
    # UNTRUSTED ON THE WAY OUT TOO: the state is reconstructed from a stored payload, so
    # `from_state` re-judges it and answers None rather than raising inside a node.
    # (The bare choice word, "standalone", IS a request since the CRS v5 beta three-way choice.)
    for junk in [{"goal": "benchmark"}, {"goal": "standalone", "auto_accept_deviation_mm": 50.0},
                 {"goal": None}, "bench", 7, {"auto_accept_deviation_mm": 0.05},
                 {"goal": "standalone", "auto_accept_repairs": "yes"}]:
        assert rg.from_state({"repair_goal": junk}) is None


def test_the_threshold_survives_the_state_round_trip(live):
    asked = rg.parse({"goal": "standalone", "auto_accept_deviation_mm": 0.05})
    found = rg.from_state({"repair_goal": asked.to_state()})
    assert found is not None
    assert found.goal is RepairGoal.standalone and found.auto_accept_mm == 0.05


# ---------------------------------------------------------------- vertex moves are never bought


def _verdict(op: str):
    class _V:
        kept = True
        delta: dict = {}
    v = _V()
    v.op = op
    return v


@pytest.mark.parametrize(("op", "tier"), [("sew_faces", review_tier.AUTO),
                                          ("close_wire_gaps", review_tier.REVIEWER),
                                          ("rebuild_face", review_tier.REVIEWER)])
def test_a_vertex_moving_package_is_never_auto_accepted_at_any_threshold(monkeypatch, op, tier):
    from meshpipeline.cad.repair import package_review
    monkeypatch.setattr(rcfg, "CAD_REPAIR_AUTO_ACCEPT", "live", raising=False)
    got, reason = review_tier.decide(
        verdicts=[_verdict(op)], reclassified=frozenset(), revealed=frozenset(),
        declared_matches=True, deviation={"deviation_mm": 0.0001},
        deviation_status=package_review.DEVIATION_MEASURED,
        threshold_mm=rg.MAX_AUTO_ACCEPT_MM)
    assert got == tier, reason


# ---------------------------------------------------------------- the attempt node's one call


def _state(tmp_path, **over) -> dict:
    report = {"report": {"summary": "inspected",
                         "defects": [{"code": "wire_gap", "severity": "error"}],
                         "entities": [{"code": "wire_gap", "severity": "error",
                                       "entity": "edge:8", "region": "unknown",
                                       "message": "a gap", "measurements": {},
                                       "location": {"centroid": [1.0, 2.0, 3.0]}}]}}
    state = {"job_id": "job-1", "engine": "snappy",
             "geometry": geometry_state(tmp_path),
             "repair_status": RepairStatus.repairable.value,
             "repair_report": report}
    state.update(over)
    return state


@pytest.fixture
def repairs(monkeypatch):
    """A repair that reports success and RECORDS the keywords it was called with."""
    from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport

    seen: list[dict] = []

    def _repair(source, destination, *, directives=(), goal=None, auto_accept_mm=None, **kw):
        seen.append({"goal": goal, "auto_accept_mm": auto_accept_mm, "kw": kw})
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        measured = {"stopped_because": "resolved", "resolved": True, "complete_scan": True,
                    "refusals": [], "planner": "rules-v1", "operations_attempted": 1,
                    "operations_kept": 1, "declared_area_mm2": 0.0, "declared_volume_mm3": 0.0}
        if goal is not None:
            from meshpipeline.cad.repair import goal as goal_policy
            measured["goal"] = {"profile": goal.value, "kernel_clean": True,
                                "meshability": {"meshes": True}, "score": 1.0,
                                "grind": {"tier": "small", "rule": "elements"},
                                "review": {},
                                "verdict_tier": goal_policy.VERDICT_TIER}
        return RepairReport(
            defects=(), operations=({"name": "sew_faces", "mutated": True, "status": "kept",
                                     "basis": "kernel", "targets": ["edge:aaaa"],
                                     "defect_code": "open_shell", "reason": "",
                                     "evidence": {}, "delta": {}},),
            measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measured.items()),
            summary="Repaired within the conservative caps.", entities=())

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="", budget_s=None: (True, ""))
    return seen


def test_goal_reaches_repair_step_file_whatever_the_posture(monkeypatch, tmp_path, repairs):
    for posture in ("off", "live"):
        monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", posture, raising=False)
        asyncio.run(node_repair_attempt(_state(tmp_path, repair_goal={"goal": "standalone"})))
        assert repairs[-1]["goal"] is RepairGoal.standalone


def test_an_unset_goal_still_passes_none_with_the_posture_live(live, tmp_path, repairs):
    asyncio.run(node_repair_attempt(_state(tmp_path)))
    assert repairs[-1]["goal"] is None


def test_threshold_reaches_the_profile(live, tmp_path, repairs):
    out = asyncio.run(node_repair_attempt(_state(
        tmp_path, repair_goal={"goal": "standalone", "auto_accept_deviation_mm": 0.05})))
    assert repairs[-1]["auto_accept_mm"] == 0.05
    assert out["repair_attempt"]["auto_accept_mm"] == 0.05


def test_a_standalone_run_keeps_the_goals_finishing_verdict(live, tmp_path, repairs):
    out = asyncio.run(node_repair_attempt(_state(
        tmp_path, repair_goal={"goal": "standalone"})))
    verdict = out["repair_attempt"]["measurements"]["goal"]
    assert verdict["profile"] == "standalone" and verdict["score"] == 1.0


def test_a_standalone_run_never_proves_itself_against_a_mesher(live, tmp_path, repairs,
                                                               monkeypatch):
    # SPEC AMENDMENT 1: the product mesher is not part of the standalone verdict, and the staging
    # proof is a mesher's question - so a standalone run does not ask it, and promotes nothing.
    def _never(*a, **kw):
        raise AssertionError("a standalone repair job reached the staging proof")

    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.promote", _never)
    out = asyncio.run(node_repair_attempt(_state(tmp_path, repair_goal={"goal": "standalone"})))
    assert out["repair_attempt"]["attempted"] is True
    assert "repair_lineage" not in out
    assert out["repair_attempt"]["repaired_path"].endswith("-repaired.step")


def test_a_meshing_goal_still_meshes_the_repair(live, tmp_path, repairs):
    out = asyncio.run(node_repair_attempt(_state(tmp_path, repair_goal={"goal": "meshing"})))
    assert repairs[-1]["goal"] is RepairGoal.meshing
    # today's behaviour: the repair is promoted and the run meshes it
    assert out["repair_lineage"]["original"]["sha256"]


# ---------------------------------------------------------------- the route


def test_standalone_skips_the_mesher(live):
    assert route_after_repair_attempt(
        {"repair_goal": {"goal": "standalone"}}) == "node_repair_deliver"


@pytest.mark.parametrize("state", [
    {},
    {"repair_goal": {}},
    {"repair_goal": {"goal": "meshing"}},
    {"repair_goal": {"goal": "benchmark"}},
])
def test_meshing_goal_keeps_todays_edge(live, state):
    assert route_after_repair_attempt(state) == "node_geometry_admission"


def test_a_standalone_choice_ends_with_the_repair_with_the_posture_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STANDALONE_JOBS", "off", raising=False)
    assert route_after_repair_attempt(
        {"repair_goal": {"goal": "standalone"}}) == "node_repair_deliver"


# ---------------------------------------------------------------- the terminal status
#
# The standalone branch ends at node_repair_deliver: no builder, no executor, no reviewer. The
# terminal status must be read off THAT ending, not off the mesher's fields it never set - else
# every repaired standalone job ends `failed / mesh_generation`.


def _terminal(state):
    import logging

    from meshpipeline.application.final_result import RunOutcome, derive_terminal_status
    return derive_terminal_status(RunOutcome.from_graph_state(state), job_id="j",
                                  jlog=logging.getLogger("t"))


def _standalone_ended(node_output: dict) -> dict:
    return {"repair_goal": {"goal": "standalone"}, **node_output}


def test_a_repaired_standalone_job_ends_succeeded(live, tmp_path):
    from meshpipeline.pipeline.repair_deliver import node_repair_deliver
    fixed = tmp_path / "fixed.step"
    fixed.write_text("ISO-10303-21;")
    ended = asyncio.run(node_repair_deliver({"repair_attempt": {
        "attempted": True, "resolved": True, "repaired_path": str(fixed)}}))
    assert ended["repair_delivery"]["outcome"] == "repaired"
    decision = _terminal(_standalone_ended(ended))
    assert decision.succeeded, decision


def test_an_unrepairable_standalone_job_still_fails(live):
    from meshpipeline.pipeline.repair_deliver import node_repair_deliver
    ended = asyncio.run(node_repair_deliver({"repair_attempt": {
        "attempted": True, "resolved": False, "stopped_because": "budget"}}))
    assert not _terminal(_standalone_ended(ended)).succeeded


def test_a_repaired_ending_with_no_file_is_no_success(live):
    assert not _terminal(_standalone_ended({"repair_delivery": {
        "outcome": "repaired", "goal": {}, "path": "", "suffix": ""}})).succeeded


def test_a_system_failure_still_wins_over_a_standalone_ending(live, tmp_path):
    fixed = tmp_path / "fixed.step"
    fixed.write_text("x")
    assert not _terminal(_standalone_ended({
        "api_failure": "storage: unavailable",
        "repair_delivery": {"outcome": "repaired", "path": str(fixed), "suffix": ".step"}},
    )).succeeded


def test_a_meshing_run_ignores_a_stray_repair_delivery(live, tmp_path):
    # only the standalone ending speaks for the status: a meshing run with no validated mesh fails
    fixed = tmp_path / "fixed.step"
    fixed.write_text("x")
    assert not _terminal({"repair_goal": {"goal": "meshing"}, "repair_delivery": {
        "outcome": "repaired", "path": str(fixed), "suffix": ".step"}}).succeeded
