# Responsibility: Verify that every key the survey state carries survives the trip to the row and back, and that a customer's answer still lands on the row that was read back.
# Boundaries: the repository against a stand-in session; a real database is the integration tier's parity gate.
#
# WHY THIS FILE EXISTS. `record` writes a NAMED LIST of keys, and a key that is not named is dropped
# without a word. The geometry agent's step wrote its plan and its third-intake question into the state
# and both were lost on the way to the table: the plan was made inside the submission turn and gone by
# the time the builder read the row, so the builder fell back on every job while the step reported
# success, and the third intake's question could not be answered in the next turn because the row it was
# raised on no longer had it. Nothing above the repository could see it - every test and every harness
# that stands in for the database keeps whatever it is handed.
#
# WHY IT WAS REWRITTEN, and this is the lesson rather than the bug. The test that claimed to check every
# key checked a HAND-WRITTEN state, and the hand-written state carried neither of the two keys that were
# being dropped: `asking`, the question finder's own decisions, and `look_queued`, the look queue's answer.
# So the check had the same blind spot as the thing it checked, and it went green while a customer's answer
# to the fluid-side question and to the budget trade was accepted in memory and refused the moment the row
# came back, and while a look that was still running read back as one nobody had ever taken.
#
# So the state under test here is COMPOSED BY THE REAL CHAIN from a real stored measurement, and the look
# states are ENUMERATED from `geometry_survey.LOOK_STATES` rather than listed by hand. A key added to the
# state and not to `record` now fails the first test; a fifth look state added and not stored fails the
# second.
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.contract.deliver", needs="a survey composed by the real chain to round-trip")

from meshpipeline.application import geometry_survey as gs  # noqa: E402
from meshpipeline.persistence.models import GeometrySurvey  # noqa: E402
from meshpipeline.persistence.repositories.geometry_survey_repository import (  # noqa: E402
    GeometrySurveyRepository,
    state_of,
)

SHA = "a" * 64
FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
#: The part whose budget trade carries real numbers: its brief states 2,000,000 cells and the measurement
#: forecasts 2,046,473, so the trade is raised and its envelope is `{cap, cells_high}`.
TRADE_CASE = "transition_007_fluid"
#: The part the fluid-side question is raised on: a solid block with one 16 mm bore, which read as the
#: fluid grows four junctions. Its two options are SENTENCES and the map back to a representation is in
#: the finder's own record row, which is the row this table used to drop.
SIDE_CASE = "block_boss_sharp"
#: `ask.say.FLUID_SIDE_WORDS["through"]` with one bore. The sentence a person reads; `wall_shell` is what
#: it sets.
THROUGH = "the fluid flows through the bore; the part is the solid around it"


def _doc(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _brief(case: str) -> str:
    return (FIXTURES / f"{case}.brief.txt").read_text(encoding="utf-8")


@pytest.fixture
def side_on(monkeypatch):
    """THERE IS NOTHING LEFT TO ARM, and this fixture now says so rather than arming it.

    This branch was cut before `settings/package_switches.py` and `policy.arm_the_package` were
    retired, and it still armed `GEOMETRY_FLUID_SIDE_ENABLED` by hand. Both platform settings are
    gone, the package deleted the reader, and the side is read on every composition, so the import
    of the retired module ended collection for the WHOLE unit lane on the merged tree. What is left
    worth holding is what `test_geometry_fluid_side_answer.py` holds: no export turns it back off.
    """
    from geometry_agent.agent import catalog
    monkeypatch.setenv("GEOMETRY_AGENT_FLUID_SIDE", "off")
    assert not hasattr(catalog, "FLUID_SIDE_ENV")


class _OneRow:
    """A session holding at most one survey row, so a state can be written, read back and written again.

    It is deliberately not a dict that keeps whatever it is handed: what is read back here is
    `state_of(row)` over the ORM object `record` actually filled in, so a column that does not exist and a
    key `record` does not name are both visible from this side.
    """

    def __init__(self):
        self.row: GeometrySurvey | None = None

    async def execute(self, _statement):
        return self

    def scalar_one_or_none(self):
        return self.row

    def add(self, row):
        self.row = row

    async def flush(self):
        return None


async def _written(state: dict, db: _OneRow | None = None) -> GeometrySurvey:
    db = db if db is not None else _OneRow()
    return await GeometrySurveyRepository().record(
        db, owner_id="owner-7f3a", geometry_source_id=uuid.uuid4(),
        sha256=str(state.get("sha256") or SHA), state=state)


async def _round_trip(state: dict, db: _OneRow | None = None) -> dict:
    """The state as it comes back out of the table. The one call every test below is about."""
    back = state_of(await _written(state, db))
    assert back is not None
    return back


def _composed(case: str, **kw) -> dict:
    """A survey composed by the real chain from a real stored measurement of these bytes."""
    kw.setdefault("purpose", "internal_cfd")
    kw.setdefault("brief", _brief(case))
    return gs.carry_answers(None, gs.compose(_doc(case), **kw))


def _hand_written(**extra) -> dict:
    """A state written out by hand. Kept only for the two tests that are about a key `record` names and
    nothing else, and never used for the total check again: it is the fixture that could not see the drop."""
    return {"sha256": SHA, "facts_sha256": "b" * 64, "stage": "settled",
            "survey": {"schema_name": "geometry_agent.contract.survey.v1"},
            "composed_for": {"purpose": "internal_cfd", "cell_cap": 2_000_000},
            "planner_block": {"schema": "geometry_agent.planner_block.v1"},
            "asked": ["role_inlet"],
            "answers": [{"question_id": "role_inlet", "subject": "o1", "value": "inlet",
                         "answered_by": "customer"}],
            "agent_git_sha": "c" * 40, **extra}


# EVERY KEY, AGAINST A STATE THE PRODUCT ACTUALLY COMPOSES


@pytest.mark.asyncio
async def test_every_key_a_real_composed_survey_carries_is_a_key_the_row_keeps(side_on):
    """THE TOTAL CHECK, and the state is the chain's own rather than one written out here.

    The list of names in `record` is the whole contract, so it is checked as a list: a key added to the
    state and not to `record` is a key that vanishes in production. Composed for two different parts,
    because the finder's row differs between them and a part with no budget trade would not have shown
    `asking.record` going missing.
    """
    for case, state in (("side", _composed(SIDE_CASE, declared=[])),
                        ("trade", _composed(TRADE_CASE))):
        state = gs._noted_queue(state, gs.LOOK_QUEUED)
        back = await _round_trip(state)
        assert set(state) - set(back) == set(), f"{case}: dropped {sorted(set(state) - set(back))}"
        for key, value in state.items():
            if key in ("stage", "agent_git_sha", "facts_sha256"):
                continue                            # stored truncated, checked by the column's own width
            assert back[key] == value, f"{case}: {key}"


@pytest.mark.asyncio
async def test_the_finders_own_decisions_come_back_whole_and_not_as_an_empty_row(side_on):
    """`asking` is not decoration: `record` holds the machine values, `put` holds the cap and the ranking
    and `text` holds the sentence a customer reads. An empty row is a legal value - a survey composed
    before the finder was wired - so the drop looked like that legal value to every reader above."""
    state = _composed(SIDE_CASE, declared=[])
    assert state["asking"]["finder"] == gs.QUESTION_FINDER
    back = await _round_trip(state)
    assert back["asking"] == state["asking"]
    assert gs.asking_of(back) == gs.asking_of(state) != {}
    assert [v["id"] for v in gs.question_views(back) if v["put"]] == [
        v["id"] for v in gs.question_views(state) if v["put"]], "the cap and the ranking are the finder's"
    assert [v["text"] for v in gs.question_views(back)] == [
        v["text"] for v in gs.question_views(state)], "a customer reads the finder's sentence, not a fallback"


# THE POINT OF THE WHOLE STEP: AN ANSWER GIVEN TO THE ROW THAT CAME BACK


@pytest.mark.asyncio
async def test_the_metal_versus_fluid_answer_lands_on_the_row_that_came_back(side_on):
    """THE REGRESSION, measured. This answer's options are two sentences and the map from a sentence to
    the representation it means is in the finder's record row. With that row dropped the answer was
    refused with "names neither reading of this surface, so it settles no representation; the readings
    are {}" - accepted in memory, refused after a round trip, so answering achieved nothing."""
    back = await _round_trip(_composed(SIDE_CASE, declared=[]))
    view = next(v for v in gs.question_views(back) if v["about"] == "representation")
    assert gs._side_readings(view) == {"through": "wall_shell", "is_fluid": "annular_fluid"}
    answered = gs.answered(back, _doc(SIDE_CASE), question_id=view["id"], choice=THROUGH,
                           words=THROUGH, latest_user_message=THROUGH, principal="owner-7f3a")
    said = [a for a in (answered.get("answers") or []) if a["question_id"] == view["id"]][-1]
    assert said["value"] == "wall_shell", said
    assert said["value"] != THROUGH, "the sentence is the words, never the value"
    # READ OFF `composed_for` AND NOT `live_answers`: answering recomposes, the side is settled in the new
    # survey, the question is gone and an answer bound to a question that no longer exists is not live. What
    # carries the confirmation forward is the composition it caused.
    assert answered["composed_for"]["confirmed_representation"] == "wall_shell"
    assert answered["composed_for"]["representation"] == "wall_shell"
    # and it survives being saved in its turn, which is the other half of a round trip mattering
    assert (await _round_trip(answered))["composed_for"]["confirmed_representation"] == "wall_shell"


@pytest.mark.asyncio
async def test_the_budget_answer_lands_on_the_row_that_came_back():
    """The trade's two numbers live in the same dropped row. Without it the envelope was None and all
    three options were refused with "its numbers are None and the option names neither holding nor
    raising", so the one question the customer is asked about cost could not be answered at all."""
    state = _composed(TRADE_CASE)
    back = await _round_trip(state)
    view = next(v for v in gs.question_views(back) if v["about"] == "cell_budget")
    assert gs._trade_envelope(view) == {"cap": 2_000_000, "cells_high": 2_046_473}
    caps = {option: gs._trade_cap(view, option) for option in view["options"]}
    assert None not in caps.values(), caps
    assert set(caps.values()) == {2_000_000, 2_046_473}, caps
    raise_it = next(o for o, cap in caps.items() if cap == 2_046_473)
    settled = back
    for open_view in [v for v in gs.open_now(back) if v["route"] == gs.ROUTE_INTAKE]:
        settled = gs.record_answer(settled, question_id=open_view["id"],
                                   choice=open_view["options"][0], subject=(open_view["subjects"] or [""])[0],
                                   words="that one", latest_user_message="that one, and raise the budget")
    settled = gs.record_answer(settled, question_id=view["id"], choice=raise_it, words="raise the budget",
                               latest_user_message="that one, and raise the budget")
    assert gs.confirmed_cell_cap(settled) == 2_046_473
    assert gs.confirmed_cell_cap(await _round_trip(settled)) == 2_046_473


# THE LOOK: EVERY STATE THE MODULE NAMES, ENUMERATED FROM THE MODULE


def _in_look_state(name: str, case: str = TRADE_CASE) -> dict:
    """A real composed survey in the named look state, built the way the product reaches it.

    It RAISES for a state it does not know how to build, which is the whole point of the parametrisation
    below: a fifth look state added to `geometry_survey.LOOK_STATES` fails here instead of quietly not
    being covered, which is how `look_queued` came to have no column in the first place.
    """
    doc = _doc(case)
    if name == gs.LOOK_NONE:
        return _composed(case)                      # the stored document carries no look at all
    if name == gs.LOOK_PENDING:
        # the queue's own answer, written by the one function that writes it
        return gs._noted_queue(_composed(case), gs.LOOK_QUEUED)
    if name == gs.LOOK_FAILED:
        looked = {**doc, "look": {"status": gs.LOOK_FAILED, "reason": "the reader returned nothing"}}
        return gs.carry_answers(None, gs.compose(looked, purpose="internal_cfd", brief=_brief(case)))
    if name == gs.LOOK_OK:
        looked = {**doc, "look": {"status": gs.LOOK_OK, "impression": {
            "looks_like": "a transition duct", "openings_seen": [], "internal_features": []}}}
        return gs.carry_answers(None, gs.compose(looked, purpose="internal_cfd", brief=_brief(case)))
    raise AssertionError(f"no case here for the look state {name!r}; add one rather than skipping it")


@pytest.mark.asyncio
@pytest.mark.parametrize("look", gs.LOOK_STATES)
async def test_every_look_state_the_module_names_survives_the_row(look):
    """A look that FAILED, one that has not happened yet and one that found a clear passage are three
    different things to the builder, and `pending` used to exist only inside the request that queued the
    look: the state was written, the row had no column for it, and it came back as `not_attempted`.

    The states are read off `geometry_survey.LOOK_STATES`, not listed here, so the list cannot fall behind.
    """
    state = _in_look_state(look)
    assert gs.look_state(state) == look, "the state was not built in the look state it claims"
    assert gs.look_state(await _round_trip(state)) == look, "the row lost which look state this was"


@pytest.mark.asyncio
async def test_the_queue_answer_still_says_which_answer_the_queue_gave():
    """`queued`, `cached` and `skipped` are three different answers from the queue and the row keeps the
    word, not a boolean: `skipped` means nothing will ever write a reading, so it is NOT pending."""
    for outcome, expected in ((gs.LOOK_QUEUED, gs.LOOK_PENDING), (gs.LOOK_CACHED, gs.LOOK_PENDING),
                              (gs.LOOK_SKIPPED, gs.LOOK_NONE)):
        back = await _round_trip(gs._noted_queue(_composed(TRADE_CASE), outcome))
        assert back["look_queued"] == outcome
        assert gs.look_state(back) == expected, outcome


@pytest.mark.asyncio
async def test_the_rows_own_stamp_says_which_state_shape_it_was_written_under():
    state = _composed(TRADE_CASE)
    assert state["schema"] == gs.SURVEY_STATE_SCHEMA
    assert (await _round_trip(state))["schema"] == gs.SURVEY_STATE_SCHEMA


@pytest.mark.asyncio
async def test_a_second_save_of_the_row_that_came_back_loses_nothing_either():
    """The product saves, loads and saves again on every turn. An update path that names fewer keys than
    the insert path would lose them on the second turn rather than the first, which is worse."""
    db = _OneRow()
    first = await _round_trip(gs._noted_queue(_composed(TRADE_CASE), gs.LOOK_QUEUED), db)
    second = await _round_trip(first, db)
    assert second == first


# THE GEOMETRY AGENT'S HALF, which is what this file was written for


@pytest.mark.asyncio
async def test_the_geometry_agents_plan_and_its_late_question_survive_the_row():
    step = {"schema": "meshpipeline.geometry_step.v1", "status": "planned", "for": "d" * 32,
            "fidelity": "standard", "envelope": {"cells_high": 987_828, "cap": 750_000},
            "flow_patches": {"o1": {"role": "inlet", "kind": "confirmed"}},
            "plan": {"unit": {"assumed": "mm"}},
            "ledger": {"meta": {"key": "k"}, "events": [{"event": "plan"}]}}
    late = {"schema": "meshpipeline.geometry_step.late.v1", "id": "u_budget_planned",
            "options": ["hold 750,000", "raise to about 987,828"], "default": "hold 750,000"}
    row = await _written(_hand_written(geometry_step=step, late=late))
    assert row.geometry_step == step, "the plan was dropped on the way to the row"
    assert row.late == late, "the question only a plan can raise was dropped on the way to the row"
    back = state_of(row)
    assert back["geometry_step"] == step and back["late"] == late


@pytest.mark.asyncio
async def test_a_row_the_step_never_touched_carries_neither_key_at_all():
    """Not null values under the keys: NO keys. `late_view` and `builder_handoff` tell "there is no
    third question" from "there is one and it is open" by whether the key is there."""
    row = await _written(_hand_written())
    assert row.geometry_step is None and row.late is None
    back = state_of(row)
    assert "geometry_step" not in back and "late" not in back
