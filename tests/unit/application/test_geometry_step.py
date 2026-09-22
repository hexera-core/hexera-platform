# Responsibility: Verify the geometry agent's step on the platform: the switch, the order, the plan, the third intake, the builder's handoff and the fail-open.
# Boundaries: pure functions over a real stored measurement of a corpus part, with the package's deterministic stand-in policy as the model. No database, no provider, no network.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("geometry_agent.chain.job",
                    reason="the measurement package is not on this interpreter's path")

import meshpipeline.settings.policy as polcfg  # noqa: E402
from meshpipeline.application import geometry_step as gst  # noqa: E402
from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"


def _doc(case: str = "venturi_orifice_001") -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _brief(case: str = "venturi_orifice_001") -> str:
    return (FIXTURES / f"{case}.brief.txt").read_text(encoding="utf-8")


@pytest.fixture
def armed(monkeypatch):
    """Every gate under the step on, as an operator would set them. The default is every one off."""
    for name in ("GEOMETRY_MEASUREMENT_ENABLED", "GEOMETRY_REPORT_READERS_ENABLED",
                 "GEOMETRY_SURVEY_ENABLED", "GEOMETRY_AGENT_STEP_ENABLED"):
        monkeypatch.setattr(polcfg, name, True)
    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_PROVIDER", "reference")


def _surveyed(case: str = "venturi_orifice_001") -> dict:
    doc = _doc(case)
    state = gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=_brief(case), engine="snappy"))
    return gs.mark_asked(state, gs.open_now(state))


def _answered(case: str = "venturi_orifice_001") -> tuple[dict, dict]:
    """The row with every step-4 question settled by the customer, and its document.

    A different mouth for each role: the same mouth twice is refused, which is a rule of its own
    (`_refuse_conflict`) and not what these tests are about."""
    doc, state = _doc(case), _surveyed(case)
    for view in list(gs.open_now(state)):
        if view["route"] != gs.ROUTE_INTAKE:
            continue
        pick = view["options"][-1] if view["id"] == "role_outlet" else view["options"][0]
        said = f"{pick} it is"
        state = gs.answered(state, doc, question_id=view["id"], choice=pick,
                            words=said, latest_user_message=said, principal="owner-7f3a")
    return state, doc


def _planned(case: str = "venturi_orifice_001") -> tuple[dict, dict]:
    state, doc = _answered(case)
    return gst.plan_the_part(state, doc, fidelity="standard", job_id="job-1",
                             client=gst.planner_client("reference")), doc


# -------------------------------------------------------------------------------------------------
# THE SWITCH. Nothing below runs, and nothing it owns exists on a row, until it is set.
# -------------------------------------------------------------------------------------------------

def test_the_step_is_off_by_default_and_its_own_switch_is_not_enough():
    assert polcfg.GEOMETRY_AGENT_STEP_ENABLED is False
    assert gst.step_enabled() is False


def test_the_switch_alone_does_nothing_without_the_survey(monkeypatch):
    """`GEOMETRY_AGENT_STEP_ENABLED` is read with every gate beneath it, every time, so setting one
    flag in a test or in an environment cannot arm a chain whose earlier steps are off."""
    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_ENABLED", True)
    assert gst.step_enabled() is False
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_REPORT_READERS_ENABLED", True)
    assert gst.step_enabled() is False, "the survey is still off"
    monkeypatch.setattr(polcfg, "GEOMETRY_SURVEY_ENABLED", True)
    assert gst.step_enabled() is True


def test_with_the_step_off_the_survey_raises_no_late_question_and_reads_none(monkeypatch):
    """A row that carries a `late` key - written before the switch was turned off, say - is not a
    question with the step off: `late_view` is the only reader and it is gated."""
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASUREMENT_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_REPORT_READERS_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_SURVEY_ENABLED", True)
    state = {**_surveyed(), "late": {"schema": gst.LATE_SCHEMA, "id": "budget_after_plan",
                                     "text": "raise it?", "options": ["hold", "raise"],
                                     "default": "hold", "envelope": {"cap": 1, "cells_high": 2}}}
    assert gs.late_view(state) is None
    assert [v["id"] for v in gs.question_views(state) if v["route"] == gs.ROUTE_LATE] == []
    assert [v for v in gs.open_now(state) if v["route"] == gs.ROUTE_LATE] == []
    with pytest.raises(gs.SurveyError, match="there is no survey question"):
        gs.record_answer(state, question_id="budget_after_plan", choice="hold",
                         words="hold", latest_user_message="hold")


def test_at_submission_with_the_step_off_returns_the_row_untouched_and_stores_nothing(monkeypatch):
    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_ENABLED", False)
    saved: list = []
    monkeypatch.setattr(gs, "save", lambda *a, **k: saved.append(a))
    state, doc = _answered()
    got = asyncio.run(gst.at_submission(owner_id="o", session_id="s", source_ref=object(), state=state,
                                        document=doc, fidelity="standard"))
    assert got is state and saved == []


# -------------------------------------------------------------------------------------------------
# STEP 5: THE PLAN, FROM THE SURVEY AND THE ANSWERS, AND NOTHING MEASURED AGAIN
# -------------------------------------------------------------------------------------------------

def test_the_agent_plans_the_part_from_the_stored_survey_and_the_customers_answers(armed):
    state, _doc_ = _planned()
    step = state["geometry_step"]
    assert step["status"] == gst.PLANNED, step.get("reason")
    assert step["exit"] == "submitted"
    assert step["provider"] == "reference" and step["schema"] == gst.STEP_SCHEMA
    assert step["representation"] == state["composed_for"]["representation"]
    assert step["flow_patches"], "the plan's flow patches are what the step is for"
    assert step["envelope"]["cells_high"] > 0


def test_the_cost_envelope_comes_from_the_builder_emulator_and_says_so(armed):
    """A cell forecast is `tools.estimate_builder_cells`, never `tools.refined_cell_estimate`. The
    envelope carries its own basis, and the basis is what this pins: a number with another source
    behind it would be a different thing wearing this one's name."""
    state, _doc_ = _planned()
    env = state["geometry_step"]["envelope"]
    assert "estimate_builder_cells" in env["source"]
    assert "refined_cell_estimate" not in json.dumps(env)
    assert env["basis"], "a forecast with no basis is a number nobody can check"


def test_the_geometry_agent_never_re_derives_what_the_survey_measured(armed):
    """The plan is made against the survey the customer answered, and that is checked rather than
    assumed: the stored inputs are composed again and the survey they give must be the stored one,
    field for field. A row whose survey this platform cannot reproduce is refused."""
    state, doc = _answered()
    broken = {**state, "survey": {**state["survey"], "facts_sha256": "0" * 64}}
    got = gst.plan_the_part(broken, doc, job_id="job-1", client=gst.planner_client("reference"))
    assert got["geometry_step"]["status"] == gst.FAILED
    assert "is not what its own inputs compose to" in got["geometry_step"]["reason"]


def test_a_plan_is_bound_to_the_answers_it_was_made_for(armed):
    """`plan_key` is what a plan was made FOR. A changed answer makes a new key and the stored plan
    is not handed to a builder, because a plan for other answers describes another job."""
    state, doc = _planned()
    key = state["geometry_step"]["for"]
    assert key == gst.plan_key(state, "standard")
    assert gst.plan_key(state, "max") != key, "the fidelity is part of what a plan was made for"
    moved = gs.recomposed(state, doc, cell_cap=999_999)
    assert gst.plan_key(moved, "standard") != key
    with pytest.raises(gst.StepRefused, match="another job"):
        gst.builder_handoff(moved, doc, request_txt="mesh it")


# -------------------------------------------------------------------------------------------------
# THE ORDER: STEP 5 IS AFTER STEP 4, INCLUDING THE SURVEY'S OWN TRADE
# -------------------------------------------------------------------------------------------------

def test_the_step_waits_for_every_question_the_survey_itself_raises(armed):
    """Measured on the corpus: of ten parts run end to end before this guard, the four whose survey
    still had its budget trade open were the four the builder fell back on. Confirming a budget
    composes the measurement again, which moves `plan_key`, so a plan made first is thrown away."""
    unanswered = _surveyed()
    assert gst.not_yet(unanswered), "the role questions have not been put"
    state, _doc_ = _answered()
    assert [w for w in gst.not_yet(state) if w.startswith("intake:")] == []


def test_at_submission_does_not_plan_while_the_survey_waits_and_records_no_failure(armed, monkeypatch):
    """Not yet is not a failure. Nothing is stored, so the next submission - once the question has
    been put - plans, rather than finding a `failed` row it refuses to retry."""
    saved: list = []

    async def _save(*a, **k):
        saved.append(a)
        return True

    monkeypatch.setattr(gs, "save", _save)
    state = _surveyed()
    got = asyncio.run(gst.at_submission(owner_id="o", session_id="s", source_ref=object(), state=state,
                                        document=_doc(), fidelity="standard"))
    assert got is state and saved == []
    assert "geometry_step" not in got


# -------------------------------------------------------------------------------------------------
# STEP 6: THE QUESTION ONLY THE PLAN CAN RAISE, PUT ONCE, WITH A DEFAULT
# -------------------------------------------------------------------------------------------------

def _with_a_raised_trade(state: dict) -> dict:
    """A row whose plan raised the third intake. Composed here rather than hunted for on the corpus:
    the trade is raised only when the plan's envelope exceeds the stated cap AND the survey had no
    budget question of its own, and what this pins is what the platform does once it is raised."""
    return {**state, "late": {
        "schema": gst.LATE_SCHEMA, "id": "budget_after_plan", "kind": "budget_trade",
        "text": "Resolving the places this plan names costs about 3,000,000 cells against the "
                "2,000,000 you stated. Hold the budget, or raise it?",
        "options": ["hold at 2,000,000", "raise to about 3,000,000"],
        "default": "hold at 2,000,000", "default_is": "the customer's own stated budget",
        "because": "the planned envelope is over the stated budget",
        "envelope": {"cap": 2_000_000, "cells_high": 3_000_000, "source": "tools.estimate_builder_cells"},
        "raised": {"id": "budget_after_plan", "about": "cell_budget", "why": "over budget",
                   "options": ["hold at 2,000,000", "raise to about 3,000,000"],
                   "options_from": "the stated budget and the builder emulator's envelope for this plan",
                   "default": "hold at 2,000,000", "effect": "changes_mesh", "evidence": []},
        "depends_on": state.get("survey", {}).get("sha256", ""), "raised_at": "2026-09-22T00:00:00+00:00"}}


def test_the_third_intake_is_the_last_question_and_only_after_the_survey_is_settled(armed):
    state, _doc_ = _planned()
    with_trade = _with_a_raised_trade(state)
    (view,) = [v for v in gs.open_now(with_trade) if v["route"] == gs.ROUTE_LATE]
    assert view["id"] == "budget_after_plan"
    assert view["default"] == "hold at 2,000,000"
    assert gs.stage_of(with_trade) == gs.STAGE_LATE
    assert gst.submission_problems(with_trade), "the submission waits until it has been put"
    # and it is not put while a step-4 question is still open
    unsettled = _with_a_raised_trade(_surveyed())
    assert [v["route"] for v in gs.open_now(unsettled)] == ["intake", "intake"]


def test_the_third_intake_is_asked_once_and_a_default_is_not_a_confirmation(armed):
    state = _with_a_raised_trade(_planned()[0])
    said = "hold at 2,000,000"
    held = gs.record_answer(state, question_id="budget_after_plan", words="leave it to you",
                            latest_user_message="leave it to you", took_default=True)
    (view,) = [v for v in gs.question_views(held) if v["route"] == gs.ROUTE_LATE]
    assert view["status"] == "defaulted"
    assert gst.submission_problems(held) == [], "a default that stood is a question that was put"
    assert gs.confirmed_cell_cap(held) is None, "a default is not a confirmation"
    with pytest.raises(gs.SurveyError, match="asked once"):
        gs.record_answer(held, question_id="budget_after_plan", choice=said, words=said,
                         latest_user_message=said)


def test_the_third_intakes_answer_is_the_number_its_own_options_were_written_from(armed):
    """Never parsed back out of the sentence: the two numbers ride in the view's marks, which is
    where `_trade_numbers` reads them."""
    state = _with_a_raised_trade(_planned()[0])
    said = "raise to about 3,000,000"
    raised = gs.record_answer(state, question_id="budget_after_plan", choice=said, words=said,
                              latest_user_message=said, principal="owner-7f3a")
    (row,) = [a for a in gs.live_answers(raised) if a["question_id"] == "budget_after_plan"]
    assert row["value"] == 3_000_000 and row["stage"] == gs.LATE_STAGE


def test_the_third_intakes_budget_never_recomposes_the_survey_the_plan_was_made_against(armed):
    """The survey's own trade is composed back in; this one is not, because the composition it would
    change is the one the plan was made against. It reaches the builder through the handoff."""
    state, doc = _planned()
    key = state["geometry_step"]["for"]
    said = "raise to about 3,000,000"
    raised = gs.answered(_with_a_raised_trade(state), doc, question_id="budget_after_plan",
                         choice=said, words=said, latest_user_message=said)
    assert raised["composed_for"]["cell_cap"] == state["composed_for"]["cell_cap"]
    assert gst.plan_key(raised, "standard") == key, "the plan is still the plan for this job"
    assert gs.confirmed_cell_cap(raised) is None, "the SURVEY's confirmed budget is not this one"


def test_a_failed_step_drops_a_question_it_raised_and_never_put(armed):
    """There is no plan left to price it against, so holding the customer at the submission for it
    would be asking them about an envelope this platform can no longer produce."""
    state, doc = _planned()
    with_trade = _with_a_raised_trade(state)
    broken = {**with_trade, "survey": {**with_trade["survey"], "facts_sha256": "0" * 64}}
    got = gst.plan_the_part(broken, doc, job_id="job-1", client=gst.planner_client("reference"))
    assert got["geometry_step"]["status"] == gst.FAILED
    assert "late" not in got
    assert gst.submission_problems(got) == []


def test_a_question_already_put_survives_a_later_plan(armed):
    """Asked ONCE. Whatever a later plan costs, a question the customer has already had stays exactly
    as it was put, with whatever they said."""
    state, doc = _planned()
    put = gs.mark_asked(_with_a_raised_trade(state), [gs.late_view(_with_a_raised_trade(state))])
    again = gst.plan_the_part(put, doc, job_id="job-1", client=gst.planner_client("reference"))
    assert again["late"]["id"] == "budget_after_plan"
    assert again["late"]["text"] == put["late"]["text"]


# -------------------------------------------------------------------------------------------------
# STEP 7: BOTH WRITE-UPS, THE SURVEYOR INSIDE THE TYPED BLOCK
# -------------------------------------------------------------------------------------------------

def test_the_builder_gets_intakes_write_up_and_the_geometry_agents(armed):
    state, doc = _planned()
    handoff = gst.builder_handoff(state, doc, request_txt=_brief())
    typed = handoff["typed"]
    assert handoff["request_prefix"], "the geometry agent's write-up goes in front of the request"
    assert typed["intake"]["write_up"], "intake's write-up rides in the typed block"
    assert typed["survey"], "the Surveyor is inside the typed block"
    assert typed["flow_patches"] and typed["plan_envelope"]
    assert typed["places"], "the places the plan names"


def test_the_write_up_is_sized_so_the_brief_still_reaches_the_planner(armed):
    """The planner reads `request_txt[:2000]`. The geometry agent's write-up goes in FRONT of the
    brief, so a write-up sized without regard to the brief pushes the customer's own words off the
    end. `hexera.request_assembly` sizes it against this platform's request."""
    state, doc = _planned()
    brief = _brief()
    request = gst.request_with_write_up(gst.builder_handoff(state, doc, request_txt=brief), brief)
    assert request.startswith("MESH PLAN")
    assert brief.strip()[-30:] in request[:2000], "the brief's last words fell past the cut"


def test_the_surveyor_names_places_and_never_a_builder_setting(armed):
    """The typed block's places are geometry. A place carrying a refinement level or a cell count
    would be the Surveyor predicting the mesh, which is not its job."""
    state, doc = _planned()
    typed = gst.builder_handoff(state, doc, request_txt=_brief())["typed"]
    for place in typed["places"]:
        assert not {"surface_level", "feature_level", "n_layers", "max_cells", "cells"} & set(place)


def test_the_builders_handoff_is_validated_by_the_packages_own_contract(armed, monkeypatch):
    """Every handoff this module makes goes out through `contract.deliver.builder_handoff`, which
    checks it. A refusal there is a refusal here, and the job falls open."""
    from geometry_agent.contract import deliver

    state, doc = _planned()
    gst.builder_handoff(state, doc, request_txt=_brief())          # passes today

    def _refuse(*a, **k):
        raise deliver.ContractError("deliver", "the block is not what the builder takes")

    monkeypatch.setattr(deliver, "builder_handoff", _refuse)
    with pytest.raises(deliver.ContractError):
        gst.builder_handoff(state, doc, request_txt=_brief())


# -------------------------------------------------------------------------------------------------
# THE LEDGER: EVERY STEP WRITTEN DOWN
# -------------------------------------------------------------------------------------------------

def test_every_step_is_written_to_the_job_ledger_in_the_chains_own_order(armed):
    state, _doc_ = _planned()
    stages = [e["event"] for e in state["geometry_step"]["ledger"]["events"]]
    assert stages[:6] == ["brief", "survey", "look", "uncertainty", "intake", "plan"]
    assert state["geometry_step"]["ledger"]["meta"]["provenance"] == "heuristic", \
        "the stand-in policy is never recorded as a live model"


def test_the_handover_reaches_the_ledger_once_however_often_the_builder_asks(armed):
    state, doc = _planned()
    handoff = gst.builder_handoff(state, doc, request_txt=_brief())
    once = gst.record_handover(state, handoff)
    assert [e["event"] for e in once["geometry_step"]["ledger"]["events"]].count("handover") == 1
    twice = gst.record_handover(once, handoff)
    assert twice is once, "the builder asks on every plan call; the row is not written again"


# -------------------------------------------------------------------------------------------------
# FAIL-OPEN: THE JOB RUNS AS IT DOES TODAY, AND SAYS WHY
# -------------------------------------------------------------------------------------------------

def test_no_model_for_the_geometry_agent_is_a_refusal_and_never_another_provider(monkeypatch, armed):
    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_PROVIDER", "not_a_provider")
    with pytest.raises(gst.StepRefused, match="not one of"):
        gst.planner_client()


def test_plan_the_part_never_raises_whatever_goes_wrong(armed):
    """Anything at all: the job runs as it does with the switch off, and the row says why."""

    class Exploding:
        def complete(self, *a, **k):
            raise RuntimeError("the provider fell over")

    state, doc = _answered()
    got = gst.plan_the_part(state, doc, job_id="job-1", client=Exploding())
    assert got["geometry_step"]["status"] == gst.FAILED
    assert got["geometry_step"]["reason"]
    assert gst.submission_problems(got) == [], "a failed step holds nobody at the submission"
    with pytest.raises(gst.StepRefused, match="failed at submission"):
        gst.builder_handoff(got, doc, request_txt="mesh it")


def test_a_row_with_no_plan_refuses_the_handoff_rather_than_inventing_one(armed):
    state, doc = _answered()
    with pytest.raises(gst.StepRefused, match="did not plan this job"):
        gst.builder_handoff(state, doc, request_txt="mesh it")


def test_the_builders_own_read_falls_back_to_todays_two_values_and_says_why(armed, monkeypatch):
    """`cad.regions.planner_inputs_for_state` is what the snappy driver calls. Whatever is wrong -
    no plan, a failed step, a read that threw - it answers with the request and the block the job
    would have had with the switch off, and a sentence. It never raises."""
    import meshpipeline.cad.regions as regions

    async def _no_block(_state):
        return {"schema": "geometry_agent.planner_block.v1"}

    async def _document(_ref, _digest):
        raise RuntimeError("the object store is down")

    monkeypatch.setattr(regions, "agent_block_for_state", _no_block)
    monkeypatch.setattr(regions, "_stored_document", _document)
    request, block, why = asyncio.run(regions.planner_inputs_for_state(
        {"request_txt": "mesh it", "geometry": {"ref": None}}))
    assert (request, block) == ("mesh it", {"schema": "geometry_agent.planner_block.v1"})
    assert why == "this run names no uploaded geometry"


def test_with_the_step_off_the_builders_read_is_the_call_it_always_made(monkeypatch):
    import meshpipeline.cad.regions as regions

    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_ENABLED", False)
    seen: list = []

    async def _block(state):
        seen.append(state)
        return {"schema": "geometry_agent.planner_block.v1"}

    monkeypatch.setattr(regions, "agent_block_for_state", _block)
    got = asyncio.run(regions.planner_inputs_for_state({"request_txt": "mesh it"}))
    assert got == ("mesh it", {"schema": "geometry_agent.planner_block.v1"}, "")
    assert len(seen) == 1
