"""CRS v5 beta: the backend findings PR 3 made driving the console against this API on its slug.

1. "Skip review" was ignored: a bypass run parked, and an approved re-run parked again.
2. "Repair CAD only" that kept nothing ended FAILED, closed with a mesher's words, and the plan card
   showed a mesh plan.
3. The final `repair_progress` must always carry `problem_total` and `problems_fixed`."""
from __future__ import annotations

import asyncio
import re

import pytest

from meshpipeline.application import repair_review_park as park
from meshpipeline.application.final_result import render_standalone
from meshpipeline.persistence.models import JobStatus
from meshpipeline.pipeline import repair_progress as RP
from meshpipeline.pipeline import repair_result as RR

_MESHERS = re.compile(r"snappy|gmsh|cfmesh|netgen|tetgen|mesh plan", re.I)


def _result(**over):
    base = {"engine": "v5", "outcome": "partly_repaired", "allowed_change_mm": 0.05,
            "max_change_mm": 0.08, "needs_review": True,
            "problems": [{"id": "p1", "type": "close_faces", "status": "fixed", "change_mm": None}]}
    return {**base, **over}


# ---------------------------------------------------------------- 1. the bypass and the re-run

@pytest.fixture
def review_on(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)


def test_a_bypass_run_never_parks(review_on):
    state = {"repair_review_candidates": [{"fingerprint": "x"}],
             "repair_goal": {"goal": "meshing", "auto_accept_repairs": True}}
    plan = park.plan_review(state, status=JobStatus.failed)
    assert plan is not None and not plan.park and plan.items
    without = park.plan_review({"repair_review_candidates": [{"fingerprint": "x"}]},
                               status=JobStatus.failed)
    assert without is not None and without.park


def _decision(monkeypatch, directives, result=None):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.pipeline.repair_attempt import _review_decision
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)
    return _review_decision({"repair_directives": directives}, result or _result(), ())


def test_an_approved_rerun_applies_whatever_the_rerun_numbers_its_problems(monkeypatch):
    # the re-run's v5 result renumbered its problems; every waiting change WAS accepted
    accepted = [{"op": RR.ACCEPT_OP, "targets": [RR.WHOLE_PART_ID], "item_id": "i1",
                 "source": "engineer"}]
    got = _decision(monkeypatch, accepted, _result(problems=[
        {"id": "p7", "type": "low_quality", "status": "fixed", "change_mm": 0.2}]))
    assert got["applied"] and got["accepted_by_person"] and not got["waiting_for_review"]


def test_a_change_left_on_the_review_is_not_applied_and_not_asked_again(monkeypatch):
    directives = [{"op": RR.ACCEPT_OP, "targets": ["p1"], "item_id": "i1"},
                  {"op": RR.LEAVE_OP, "targets": ["p2"], "item_id": "i2"}]
    got = _decision(monkeypatch, directives)
    assert not got["applied"] and not got["waiting_for_review"] and got["declined_by_person"]
    assert RR.without_acceptances(directives) == ()


def test_the_rerun_route_sends_a_leave_for_every_change_not_accepted():
    import inspect

    from meshpipeline.api.v1 import repair_review
    body = inspect.getsource(repair_review.run_review)
    assert "LEAVE_OP" in body and 'i.state != "accepted"' in body and "REVIEW_ORIGIN" in body


# ---------------------------------------------------------------- 2. "Repair CAD only"

@pytest.mark.parametrize("outcome, counts, expect", [
    ("repaired", {"found": 3, "fixed": 3}, "repaired: 3 of 3 problems fixed"),
    ("partly_repaired", {"found": 4, "fixed": 1}, "partly repaired: 1 of 4"),
    ("unchanged", {}, "nothing needed changing"),
    ("unrepairable", {}, "couldn't repair"),
])
def test_a_standalone_closing_is_the_repairs(outcome, counts, expect):
    text = render_standalone({"choice": "standalone", "outcome": outcome, "counts": counts})
    assert expect in text and not _MESHERS.search(text)


def test_render_message_uses_the_repair_wording_for_a_standalone_job():
    from meshpipeline.application.final_result import (
        FinalResult,
        TerminalStatus,
        render_message,
        with_repair,
    )
    fr = with_repair(FinalResult(schema_version=_schema(), job_id="j", owner_id="o",
                                 status=TerminalStatus.succeeded, engine="snappy"),
                     {"choice": "standalone", "ran": True, "outcome": "unrepairable"})
    text = render_message(fr)
    assert "couldn't repair" in text and not _MESHERS.search(text)


def test_a_standalone_run_waiting_for_review_is_not_a_completed_job():
    # found on the slug: the kept-nothing fix made a WAITING standalone run read succeeded
    from meshpipeline.application.final_result import _standalone_delivery
    goal = {"repair_goal": {"goal": "standalone"}}
    assert _standalone_delivery({**goal, "repair_review_wait": True}) is None
    assert _standalone_delivery({**goal, "repair_delivery": {"outcome": "unrepairable"}}) == {
        "outcome": "unrepairable"}


def _schema():
    from meshpipeline.application import final_result
    return max(final_result._READABLE_SCHEMA_VERSIONS)


def test_the_plan_card_says_repair_cad_only():
    from types import SimpleNamespace

    from meshpipeline.application.intake_brief import repair_goal_of
    by_form = SimpleNamespace(intake_gate={}, messages=[
        {"role": "user", "content": "GEOMETRY CHECK (confirmed by the user): ... Repair: repair "
                                    "the CAD only (repair_goal standalone) - deliver the fixed "
                                    "STEP, no mesh."}])
    assert repair_goal_of(by_form) == "standalone"
    by_chat = SimpleNamespace(intake_gate={"approval": {"payload": {"repair_goal": "off"}}},
                              messages=by_form.messages)
    assert repair_goal_of(by_chat) == "off"
    assert repair_goal_of(SimpleNamespace(intake_gate=None, messages=[])) == "meshing"


# ---------------------------------------------------------------- 3. the last line's numbers

def test_the_last_line_always_carries_both_numbers():
    from meshpipeline.pipeline.repair_attempt import _done
    for out in ({"repair_attempt": {"attempted": False, "outcome": "refused"}},     # no counts
                {"repair_attempt": {"attempted": False, "repair_result_v5": _result(
                    needs_review=False)}}):
        line = _done({}, out)
        assert line["stage"] == "done"
        assert isinstance(line["problem_total"], int) and isinstance(line["problems_fixed"], int)


def test_the_engines_own_done_line_is_not_the_lanes(monkeypatch):
    said: list[dict] = []

    async def _publish(job_id, line, *, seq):
        said.append(dict(line))

    monkeypatch.setattr(RP, "apublish", _publish)

    async def _run():
        lane = RP.Lane("j")
        await lane.feed({"event": "repair_progress", "stage": "done", "problem_total": 4,
                         "text": "Repair finished"})
    asyncio.run(_run())
    assert said == []


# ---------------------------------------------------------------- PR 3's real v5 job c8f5dc5b

def test_a_change_past_the_allowance_waits_although_the_engine_said_no(monkeypatch):
    # 0.274 mm against 0.05 mm allowed came back needs_review false: the measured change decides
    result = _result(needs_review=False, max_change_mm=0.274, allowed_change_mm=0.05)
    got = _decision(monkeypatch, [], result)
    assert got["needs_review"] and got["waiting_for_review"] and not got["applied"]


def test_a_volume_loss_from_joined_overlaps_says_why():
    state = {"repair_attempt": {"attempted": True, "result": {"engine_result": _result(
        volume_ratio=0.838, flags=["volume_changed"], problems=[
            {"id": "p1", "type": "overlap_self_intersection", "status": "not_fixed"}])}}}
    sec = RR.section(state, choice="standalone")
    assert sec["flags"] == [RR.OVERLAP_VOLUME_REASON]
    plain_vol = RR.section({"repair_attempt": {"attempted": True, "result": {"engine_result":
        _result(volume_ratio=0.838, flags=["volume_changed"])}}}, choice="standalone")
    assert plain_vol["flags"] == ["The part's volume changed noticeably"]


def test_a_problem_with_only_bodies_gets_the_viewers_pin():
    state = {"repair_pins": {"p5": [1.0, 2.0, 3.0]},
             "repair_attempt": {"attempted": True, "result": {"engine_result": _result(problems=[
                 {"id": "p5", "type": "overlap_self_intersection", "status": "not_fixed",
                  "location": {"faces": [], "bodies": ["B2"], "points": []}}])}}}
    sec = RR.section(state, choice="meshing")
    assert sec["problems"][0]["location"]["points"] == [[1.0, 2.0, 3.0]]


def test_the_lanes_first_lines_carry_numbers_and_the_engines_steps_stay_plain(monkeypatch):
    # PR 1's ProgressMapper, fed raw v5 records whose defect turns carry NO problem number
    from meshpipeline.cad.repair_v5.result import ProgressMapper as EngineMapper
    engine = EngineMapper()
    records = [{"event": "v5_start"}, {"event": "diagnosed", "defects": 3},
               {"event": "phase", "phase": "conformal"},
               {"event": "phase", "phase": "tournament", "defect_class": "close_faces"},
               {"event": "defect_result", "defect_class": "close_faces", "what": "fixed"},
               {"event": "phase", "phase": "tournament", "defect_class": "low_quality"},
               {"event": "defect_result", "defect_class": "low_quality", "what": "skipped"},
               {"event": "diagnosed", "defects": 1},
               {"event": "phase", "phase": "tournament", "defect_class": "surface_crossing"}]
    plain_events = [e for e in (engine(r) for r in records) if e]
    lines = RP.map_events([{**e, "event": "repair_progress"} for e in plain_events])
    texts = [ln["text"] for ln in lines]
    assert texts == ["Checking your CAD", "Found 3 problems to fix",
                     "Lining up parts that should touch",
                     "Repairing a thin gap — problem 1 of 3", "Fixed",
                     "Repairing a tiny or sliver feature — problem 2 of 3",
                     "Couldn't fix this one — left as is",
                     "Checking again: 1 problem left",
                     "Repairing intersecting surfaces — problem 1 of 1"]
    fixed_line = lines[4]
    assert fixed_line["problem_index"] == 1 and fixed_line["problems_fixed"] == 1
    assert all("couldn't identify" not in t for t in texts)
