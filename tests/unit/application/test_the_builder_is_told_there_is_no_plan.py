# Responsibility: Verify the builder is TOLD when the geometry agent made no plan for the part, and why.
# Boundaries: real stored measurements of corpus parts with the package's deterministic stand-in planner. No
#             database, no provider, no network.
#
# THE SILENCE THIS FILE EXISTS FOR. When step 5 cannot plan, `geometry_step.plan_the_part` stores
# `status: failed` with the reason, `geometry_step.builder_handoff` raises `StepRefused`, and
# `cad/regions.planner_inputs_for_state` falls back to `geometry_survey.builder_block` and writes the reason to
# the job's record. Every one of those knew. The BUILDER - the one reader that acts on the block - was handed a
# block byte for byte identical to the block of a job the geometry agent never ran on, so it could not tell a
# plan that was attempted and failed from a step that never happened. A fault that reaches the builder as
# silence is the failure the whole handoff exists to end.
#
# THE ROW IS AT RISK AND THE BLOCK IS NOT. Everything on this path fails open, because a block with no sentence
# is where the builder already was and no block at all is worse. So every test here checks the block survived.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.contract.deliver", needs="the survey row that says there is no plan")

from meshpipeline.application import geometry_step as gst  # noqa: E402
from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE, PURPOSE = "bend_elbow_001", "internal_cfd"


@pytest.fixture
def armed(monkeypatch):
    """The deterministic planner, so these tests measure the platform's chain and not a model."""
    import meshpipeline.settings.policy as polcfg

    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_PROVIDER", "reference")


def _doc() -> dict:
    return json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8"))


def _brief() -> str:
    return (FIXTURES / f"{CASE}.brief.txt").read_text(encoding="utf-8")


def _answered(doc: dict) -> dict:
    """The row with every step-4 question settled: one role per mouth, the first option of anything else."""
    state = gs.carry_answers(None, gs.compose(doc, purpose=PURPOSE, brief=_brief(), engine="snappy"))
    state = gs.mark_asked(state, gs.open_now(state))
    while True:
        open_intake = [v for v in gs.open_now(state) if v["route"] == gs.ROUTE_INTAKE]
        if not open_intake:
            return state
        view = open_intake[0]
        if view["about"] == "opening.role":
            for mouth, role in zip(view["subjects"], ("inlet", "outlet", *["wall"] * 64)):
                said = f"{mouth} is the {role}"
                state = gs.answered(state, doc, question_id=view["id"], choice=role, subject=mouth,
                                    words=said, latest_user_message=said, principal="owner-7f3a")
            continue
        pick = view["options"][0]
        state = gs.answered(state, doc, question_id=view["id"], choice=pick, words=f"{pick} it is",
                            latest_user_message=f"{pick} it is", principal="owner-7f3a")


_ROWS: dict[str, dict] = {}


def _planned(doc: dict) -> dict:
    if "state" not in _ROWS:
        state = gst.plan_the_part(_answered(doc), doc, fidelity="standard", job_id="job-1",
                                  client=gst.planner_client("reference"))
        assert state["geometry_step"]["status"] == gst.PLANNED, state["geometry_step"].get("reason")
        _ROWS["state"] = state
    return _ROWS["state"]


def _failed(doc: dict, reason: str) -> dict:
    """The same row with step 5 recorded as FAILED, which is what `plan_the_part` stores on any fault."""
    state = _planned(doc)
    step = {k: v for k, v in state["geometry_step"].items()
            if k not in ("plan", "envelope", "flow_patches", "unconfirmed_roles")}
    return {**state, "geometry_step": {**step, "status": gst.FAILED, "reason": reason}}


def _plan_rows(block: dict | None) -> list[str]:
    survey = (block or {}).get("survey")
    return [r["why"] for r in ((survey or {}).get("unsettled") or []) if r.get("about") == "plan"]


def test_the_route_this_fix_is_on_is_the_route_a_failed_step_actually_takes(armed):
    """FOLLOW THE CALL BEFORE BELIEVING THE CONNECTION. A fix on a function the failed-step path does not
    reach is a test that passes and a builder that is still not told, so the route is asserted first."""
    doc = _doc()
    with pytest.raises(gst.StepRefused) as raised:
        gst.builder_handoff(_failed(doc, "the model never answered"), doc, request_txt=_brief())
    assert "failed at submission" in str(raised.value)
    assert "the model never answered" in str(raised.value)


def test_a_failed_step_tells_the_builder_there_is_no_plan_and_why(armed):
    doc = _doc()
    block = gs.builder_block(_failed(doc, "the model never answered"))
    assert block is not None and isinstance(block.get("survey"), dict), "the block itself was lost"
    (why,) = _plan_rows(block)
    assert "made no plan for this part" in why, why
    assert "the model never answered" in why, why
    # THE ROW GOES FIRST, because it changes how every other row in the block reads.
    assert block["survey"]["unsettled"][0]["about"] == "plan"


def test_a_planned_job_carries_no_such_row(armed):
    """The row has to be absent where there IS a plan, or it says nothing by saying it everywhere."""
    doc = _doc()
    block = gs.builder_block(_planned(doc))
    assert block is not None and _plan_rows(block) == []


def test_a_row_the_step_never_ran_on_carries_no_such_row(armed):
    """A job whose step never ran is a third thing, and it is the thing a failed step used to look like."""
    doc = _doc()
    state = {k: v for k, v in _planned(doc).items() if k != "geometry_step"}
    block = gs.builder_block(state)
    assert block is not None and _plan_rows(block) == []


def test_a_reason_that_predicts_the_mesh_still_reaches_the_builder(armed):
    """THE EXPLANATION OF A REFUSAL MUST NEVER ITSELF BE REFUSABLE, and this is the one string on this path
    that is not ours: it is the agent's own `reason`, which quotes what the checker rejected.

    `contract.survey.OUTCOME_WORDS` is walked over every string in the block, so a reason saying "the mesh
    will collapse" would be refused a second time and - before `deliver.safe_reason` - cost the builder the
    whole survey. Every phrase the rule knows is in the reason here, and the block still arrives, still
    passes the platform's own six rules, and still says there is no plan.
    """
    doc = _doc()
    said = "; ".join("the plan said " + w for w in
                     ("will mesh", "will fail", "should mesh", "the mesh will", "cells will",
                      "layers will collapse", "is likely to fail", "expect the mesh"))
    block = gs.builder_block(_failed(doc, said))
    assert block is not None and isinstance(block.get("survey"), dict), "the block was lost over the quote"
    (why,) = _plan_rows(block)
    assert "made no plan for this part" in why, why
    gs.check_the_survey_block(block["survey"])      # the six rules, on the block the builder actually gets
    assert gs.what_the_surveyor_may_not_say(block["survey"]) == ""


def test_a_failed_step_with_no_reason_kept_still_says_there_is_no_plan(armed):
    doc = _doc()
    block = gs.builder_block(_failed(doc, ""))
    assert block is not None
    (why,) = _plan_rows(block)
    assert gs.NO_PLAN_NO_REASON in why, why


def test_the_row_and_the_looks_row_do_not_displace_each_other(armed):
    """Two platform-authored rows go in the same list, first, and both have to survive the other."""
    doc = {**_doc(), "look": {"status": "failed", "reason": "the reader returned nothing"}}
    state = _failed(doc, "the model never answered")
    state = {**state, "composed_for": {**(state.get("composed_for") or {}), "look_status": "failed"}}
    block = gs.builder_block(state)
    assert block is not None
    rows = block["survey"]["unsettled"]
    assert [r["why"] for r in rows if r.get("about") == "look"] == [gs.LOOK_BECAUSE[gs.LOOK_FAILED]]
    assert len(_plan_rows(block)) == 1
