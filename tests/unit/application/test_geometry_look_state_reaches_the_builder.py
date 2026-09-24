# Responsibility: Verify the look's state reaches the builder on the path a PLANNED job takes, not only when
#                 the geometry agent failed to plan.
# Boundaries: real stored measurements of corpus parts with the package's deterministic stand-in planner. No
#             database, no provider, no network.
#
# THE GAP THIS FILE EXISTS FOR, measured over the stored fixtures with the reference planner before the fix:
#
#   case                                look           representation  looked  no-plan path  planned path
#   ahmed_variant_001                   failed         external        False   1 sentence    0
#   ahmed_variant_001                   not_attempted  external        False   1 sentence    0
#   ahmed_variant_001_external_looked   failed         external        False   1 sentence    0
#   bend_elbow_001                      not_attempted  wall_shell      False   1 sentence    0
#   block_boss_sharp                    failed         wall_shell      False   1 sentence    0
#   ... 12 of 12 planned rows: one sentence on the no-plan path, none on the planned one.
#
# `survey.looked` is a BOOLEAN over FOUR states, so never taken, still running and FAILED all reach the builder
# as the same False beside the same empty `seen`. `geometry_survey.LOOK_BECAUSE` exists to tell them apart in
# words, and `geometry_survey.builder_block` put that sentence in the block - but `builder_block` is the block
# the planner gets when the geometry agent produced NO plan. A planned job's block comes from
# `geometry_step.builder_handoff`, and it never passed through there. So on the path almost every job takes, a
# look that FAILED and a look nobody took were the same block, which is the exact collapse three rounds of work
# have been trying to stop.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.chain.job", needs="the geometry agent's step and the builder's handoff")

from meshpipeline.application import geometry_step as gst  # noqa: E402
from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"

#: One stored part per representation the Surveyor can resolve, as `(representation, case, purpose, which
#: option the customer picks when the survey asks which side of the wall the fluid is on)`. All four, because
#: the survey block's shape follows the representation and a fix proved on one shape is proved on one shape.
#:
#: MEASURED, not assumed. These parts all resolve `external` under `external_cfd` and their own representation
#: under `internal_cfd`; `block_boss_sharp` is the one the Surveyor asks about (`q_fluid_side`, about
#: `representation`), and the answer decides between `wall_shell` and `annular_fluid`. Picking the first option
#: everywhere - which is what the other step tests do - resolved it to `wall_shell` and left `annular_fluid`
#: with no case at all, so the pick is part of the row rather than a default.
BY_REPRESENTATION = [
    ("external", "ahmed_variant_001", "external_cfd", 0),
    ("wall_shell", "bend_elbow_001", "internal_cfd", 0),
    ("annular_fluid", "block_boss_sharp", "internal_cfd", 1),
    ("fluid_domain", "transition_007_fluid", "internal_cfd", 0),
]

#: One plan per part, kept for the session. A plan is the expensive step here and nothing below mutates a
#: planned row: every test composes a new dict from it.
_PLANNED: dict[tuple[str, str, int], tuple[dict, dict]] = {}


@pytest.fixture
def armed(monkeypatch):
    """The deterministic planner, so these tests measure the platform's chain and not a model."""
    import meshpipeline.settings.policy as polcfg

    monkeypatch.setattr(polcfg, "GEOMETRY_AGENT_STEP_PROVIDER", "reference")


def _doc(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _brief(case: str) -> str:
    path = FIXTURES / f"{case}.brief.txt"
    if not path.is_file():
        path = FIXTURES / f"{case.split('_external')[0]}.brief.txt"
    return path.read_text(encoding="utf-8")


def _answered(case: str, purpose: str, doc: dict, side: int = 0) -> dict:
    """The row with every step-4 question settled: one role per mouth through the one role question, `side` for
    the representation question, and the first option of anything else."""
    state = gs.carry_answers(None, gs.compose(doc, purpose=purpose, brief=_brief(case), engine="snappy"))
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
        pick = view["options"][side if view["about"] == "representation" else 0]
        state = gs.answered(state, doc, question_id=view["id"], choice=pick, words=f"{pick} it is",
                            latest_user_message=f"{pick} it is", principal="owner-7f3a")


def _plan(case: str, purpose: str, doc: dict, side: int = 0) -> dict:
    state = gst.plan_the_part(_answered(case, purpose, doc, side), doc, fidelity="standard", job_id="job-1",
                              client=gst.planner_client("reference"))
    step = state["geometry_step"]
    assert step["status"] == gst.PLANNED, step.get("reason")
    return state


def _planned(case: str, purpose: str, side: int = 0) -> tuple[dict, dict]:
    """The planned row and its document, planned once per session."""
    if (case, purpose, side) not in _PLANNED:
        doc = _doc(case)
        _PLANNED[(case, purpose, side)] = (_plan(case, purpose, doc, side), doc)
    return _PLANNED[(case, purpose, side)]


def _typed(state: dict, doc: dict, case: str) -> dict:
    """The typed block the BUILDER reads on a planned job: `geometry_step.builder_handoff`'s."""
    return gst.builder_handoff(state, doc, request_txt=_brief(case))["typed"]


def _look_rows(survey: dict | None) -> list[str]:
    return [r["why"] for r in ((survey or {}).get("unsettled") or []) if r.get("about") == "look"]


@pytest.mark.parametrize("representation,case,purpose,side", BY_REPRESENTATION,
                         ids=[r for r, *_ in BY_REPRESENTATION])
def test_a_planned_job_tells_the_builder_a_look_was_never_taken(armed, representation, case, purpose,
                                                               side):
    """The state every fresh upload is in until the render worker writes something, on all four shapes.

    Before the fix this block carried `looked: false` and not one sentence, while the same part on the no-plan
    path carried the sentence below. `seen` is empty either way, which is what makes the absence unreadable.

    THE REPRESENTATION IS ASSERTED HERE TOO, and that is not decoration: the parametrisation claims to cover
    four shapes, and if answering the questions ever resolved a different one, these ids would stop meaning
    what they say and a shape would silently go uncovered.
    """
    state, doc = _planned(case, purpose, side)
    typed = _typed(state, doc, case)
    assert typed["representation"] == representation
    assert gs.look_state(state) == gs.LOOK_NONE
    assert typed["survey"]["looked"] is False and not typed["survey"].get("seen")
    assert _look_rows(typed["survey"]) == [gs.LOOK_BECAUSE[gs.LOOK_NONE]], (
        f"the builder is told nothing about the look on a planned {representation} part")


@pytest.mark.parametrize("representation,case,purpose,side", BY_REPRESENTATION,
                         ids=[r for r, *_ in BY_REPRESENTATION])
def test_a_planned_job_tells_the_builder_a_look_is_still_on_its_way(armed, representation, case, purpose,
                                                                   side):
    """The race: the look is queued as a worker and the planner can reach the survey first.

    `look_queued` is deliberately NOT part of `plan_key`, so the same plan stays valid for it - which is
    exactly why the distinction has to be made in the block rather than by refusing the plan.
    """
    state, doc = _planned(case, purpose, side)
    pending = {**state, "look_queued": gs.LOOK_QUEUED}
    assert gs.look_state(pending) == gs.LOOK_PENDING
    survey = _typed(pending, doc, case)["survey"]
    assert survey["looked"] is False
    assert _look_rows(survey) == [gs.LOOK_BECAUSE[gs.LOOK_PENDING]]
    assert _look_rows(survey) != [gs.LOOK_BECAUSE[gs.LOOK_NONE]], "pending and never taken are two states"


def test_a_planned_job_says_the_look_FAILED_and_never_lets_it_read_as_a_clear_passage(armed):
    """The state the product rule is about. A failed look is not a clear passage and is not an absent one.

    The look's status IS part of `plan_key` (`geometry_step.plan_key` reads `composed_for.look_status`), so this
    is a plan MADE in the failed state rather than a plan patched into it.
    """
    doc = {**_doc("ahmed_variant_001"), "look": {"status": "failed", "reason": "the reader returned nothing"}}
    state = _plan("ahmed_variant_001", "external_cfd", doc)
    assert gs.look_state(state) == gs.LOOK_FAILED
    survey = _typed(state, doc, "ahmed_variant_001")["survey"]
    assert survey["looked"] is False
    (why,) = _look_rows(survey)
    assert why == gs.LOOK_BECAUSE[gs.LOOK_FAILED]
    assert "FAILED" in why and "not a clear passage" in why
    assert why not in (gs.LOOK_BECAUSE[gs.LOOK_NONE], gs.LOOK_BECAUSE[gs.LOOK_PENDING])


def test_a_landed_look_says_nothing_about_itself_and_the_builder_gets_the_reading(armed):
    """The fourth state. There is nothing unsettled about a look that landed, so no row is added and the
    findings ride in `seen`, which is where the builder reads them."""
    state, doc = _planned("ahmed_variant_001_external_looked", "external_cfd")
    assert gs.look_state(state) == gs.LOOK_OK
    survey = _typed(state, doc, "ahmed_variant_001_external_looked")["survey"]
    assert survey["looked"] is True and survey["seen"]
    assert _look_rows(survey) == [], "a look that landed is not an unsettled thing"


@pytest.mark.parametrize("representation,case,purpose,side", BY_REPRESENTATION,
                         ids=[r for r, *_ in BY_REPRESENTATION])
def test_both_paths_say_it_in_the_same_words(armed, representation, case, purpose, side):
    """ONE WORDING. The no-plan path composes through `geometry_survey.builder_block` and the planned path
    through `geometry_step.builder_handoff`, and both now call `geometry_survey.with_the_look_state`. Two
    wordings of one fact drift, and this is the fact three rounds of work have been trying to keep straight."""
    state, doc = _planned(case, purpose, side)
    for label, row in (("never taken", state), ("pending", {**state, "look_queued": gs.LOOK_QUEUED})):
        planned = _look_rows(_typed(row, doc, case)["survey"])
        no_plan = _look_rows((gs.builder_block(row) or {}).get("survey"))
        assert planned == no_plan != [], f"{label}: planned path says {planned}, no-plan path says {no_plan}"


def test_the_row_the_platform_adds_is_held_to_the_packages_own_last_boundary(armed, monkeypatch):
    """`deliver.builder_handoff` validated the handoff BEFORE this row was in it, so the row would otherwise be
    the one thing in the builder's block that nothing checked. It is re-validated, and a refusal is a refused
    STEP - `planner_inputs_for_state` then falls back to the block that carries the look's state itself and
    writes the reason to the job's record - rather than a block quietly handed over without the row."""
    from geometry_agent.contract import deliver, marks

    state, doc = _planned("bend_elbow_001", "internal_cfd")
    assert _look_rows(_typed(state, doc, "bend_elbow_001")["survey"])        # passes today

    calls: list[int] = []
    real = deliver.check_builder_handoff

    def _refuse_the_second_time(handoff, given):
        calls.append(1)
        if len(calls) > 1:
            raise marks.ContractError("builder_handoff", "refused for this test")
        real(handoff, given)

    monkeypatch.setattr(deliver, "check_builder_handoff", _refuse_the_second_time)
    with pytest.raises(gst.StepRefused) as raised:
        gst.builder_handoff(state, doc, request_txt=_brief("bend_elbow_001"))
    assert "look's state" in str(raised.value)
    assert len(calls) == 2, ("the last boundary did not run again over the block that leaves, so the row this "
                            "platform adds is the one thing in it nothing checked")


def test_a_look_row_that_breaks_the_survey_contract_refuses_the_step_rather_than_dropping_the_row(armed,
                                                                                                 monkeypatch):
    """The other half of the same rule, one layer in: `with_the_look_state` runs the whole survey contract on
    the block it built, and its refusal must reach the caller as a refused step rather than a block handed over
    silently without the row, which is where the builder was before."""
    state, doc = _planned("bend_elbow_001", "internal_cfd")
    monkeypatch.setitem(gs.LOOK_BECAUSE, gs.LOOK_NONE, "the mesh will collapse here")
    with pytest.raises(gst.StepRefused) as raised:
        gst.builder_handoff(state, doc, request_txt=_brief("bend_elbow_001"))
    assert "look's state" in str(raised.value) and "the mesh will" in str(raised.value)


def test_the_row_survives_the_planners_allowlist_and_the_note_tells_a_model_how_to_read_it(armed):
    """FOLLOW THE CALL. A row in the typed block is not a row a model sees.

    `engines/snappy/planner._validated_agent_block` copies only `GEOMETRY_AGENT_BLOCK_KEYS` into the prompt and
    runs the package's own survey validator over what it keeps, so a row that did not survive that gate would
    be a fix that stopped one boundary short. And `_AGENT_SURVEY_NOTE` used to describe `unsettled` as rows
    about MOUTHS only, so the one row with no mouth arrived with nothing telling a model how to read it - and
    `_AGENT_LOOK_NOTE` does not ride either, because `look` is absent in exactly the three states this row is
    for.
    """
    from meshpipeline.engines.snappy.planner import (
        _AGENT_LOOK_NOTE,
        _AGENT_SURVEY_NOTE,
        GEOMETRY_AGENT_BLOCK_KEYS,
        _validated_agent_block,
    )

    state, doc = _planned("bend_elbow_001", "internal_cfd")
    typed = _typed(state, doc, "bend_elbow_001")
    assert "survey" in GEOMETRY_AGENT_BLOCK_KEYS
    shown = _validated_agent_block(typed, "job-test")
    assert shown is not None, "the planner refused the whole block"
    assert _look_rows(shown.get("survey")) == [gs.LOOK_BECAUSE[gs.LOOK_NONE]], (
        "the look's row did not survive the planner's allowlist and validator")
    assert not shown.get("look"), ("this part had no look, so the look's own note does not ride and the "
                                   "survey note is the only thing that can explain the row")
    assert '"about": "look"' in _AGENT_SURVEY_NOTE, (
        "the survey note does not tell a model that an `unsettled` row can be about the look; it describes "
        "only rows that name mouths, which is not what this row is")
    assert "never as clear" in _AGENT_SURVEY_NOTE
    assert "look" in _AGENT_LOOK_NOTE     # the other note still exists and is not what carries this


# THE OTHER HALF OF THE SAME FACT, WRITTEN BY THE PACKAGE
#
# The agent composes `survey["look_state"]` beside `looked`, with three values and no `pending`: a look in
# flight is a state of THIS side's queue and the stored document cannot see it. The wheel in vendor/wheels/
# WRITES THE KEY as of agent 0428ad41, so both facts are there in the image that ships today and the
# comparison is live. The wheel vendored before that wrote none, which is why the branch below still handles
# an absent key: a row measured by an older image has one and a row measured by this one does not. Once both
# are there they are two facts about one look, written from two sources, and a builder handed both and left
# to pick is back where it started.


@pytest.mark.parametrize("representation,case,purpose,side", BY_REPRESENTATION,
                         ids=[r for r, *_ in BY_REPRESENTATION])
def test_whatever_the_package_says_about_the_look_this_row_agrees_with_it(armed, representation, case, purpose,
                                                                         side):
    """MEASURED on real parts rather than argued, in BOTH worlds, because this suite runs against the agent's
    source tree on a workstation and against `vendor/wheels/` inside the image. Those two agreed about this key
    only once the wheel was rebuilt from agent 0428ad41; before that they differed on exactly it, and a stored
    row from an older image still carries no key. Where the package writes `look_state` it says what
    `PACKAGE_LOOK_STATE` maps this row onto, so the refusal never fires on a normal job; where it does not, the
    row's sentence is the whole distinction and has to be there. Neither branch is a skip."""
    state, doc = _planned(case, purpose, side)
    for row, expected in ((state, gs.LOOK_NONE), ({**state, "look_queued": gs.LOOK_QUEUED}, gs.LOOK_PENDING)):
        survey = _typed(row, doc, case)["survey"]
        assert gs.look_state(row) == expected
        said = survey.get("look_state")
        if said is not None:
            assert said == gs.PACKAGE_LOOK_STATE[expected], (
                f"the package says {said!r} and this row says {expected!r} on a part nothing has touched")
        assert _look_rows(survey) == [gs.LOOK_BECAUSE[expected]], (
            "the sentence is what the builder reads whether or not the package names the state")


def test_two_answers_about_one_look_are_refused_rather_than_both_handed_over(armed):
    """The row is composed from the document PLUS the queue's answer, so the two can only disagree when the row
    is stale against the document - which is what `cad/regions._surveyed_block` recomposes to prevent. A
    disagreement is that recomposition not having happened, and it is refused here rather than carried.

    The disagreement is written in rather than found, so this measures the refusal in both worlds: a wheel that
    writes no `look_state` has none to disagree with until something puts one there."""
    state, doc = _planned("bend_elbow_001", "internal_cfd")
    block = gs.builder_block(state)
    assert block is not None and gs.look_state(state) == gs.LOOK_NONE
    stale = {**block, "survey": {**block["survey"], "look_state": "failed"}}
    with pytest.raises(gs.SurveyError) as raised:
        gs.with_the_look_state(stale, gs.look_state(state))
    assert "two facts about one look" in str(raised.value)
    assert "'failed'" in str(raised.value) and "'not_attempted'" in str(raised.value)


def test_a_queued_look_is_not_a_disagreement_with_the_packages_not_attempted(armed):
    """`pending` is this side's state and the package deliberately has no fourth value for it, so a row that
    says pending beside a block that says `not_attempted` is the two halves agreeing, not disagreeing. The
    sentence the builder reads is still the pending one, which is the whole point."""
    state, doc = _planned("bend_elbow_001", "internal_cfd")
    block = gs.builder_block(state)
    assert block is not None
    agreeing = {**block, "survey": {**block["survey"], "look_state": "not_attempted"}}
    pending = {**state, "look_queued": gs.LOOK_QUEUED}
    assert gs.look_state(pending) == gs.LOOK_PENDING
    assert gs.PACKAGE_LOOK_STATE[gs.LOOK_PENDING] == "not_attempted"
    out = gs.with_the_look_state(agreeing, gs.look_state(pending))
    assert _look_rows(out["survey"]) == [gs.LOOK_BECAUSE[gs.LOOK_PENDING]]


def test_every_state_the_platform_names_has_a_package_state_to_be_checked_against():
    """A state with no entry maps to None, which would read as a disagreement with every block that says
    anything. A fifth look state must decide what the package's three call it, here, deliberately."""
    assert set(gs.PACKAGE_LOOK_STATE) == set(gs.LOOK_STATES)
    assert set(gs.PACKAGE_LOOK_STATE.values()) == {"ok", "failed", "not_attempted"}
