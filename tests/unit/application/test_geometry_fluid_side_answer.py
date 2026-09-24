# Responsibility: An answer to the fluid-side question reaches the chain as the REPRESENTATION it means and not
#                 as the sentence the customer read, and a side nobody has answered asserts nothing downstream.
# Boundaries: pure functions over one real stored measurement. The package decides what is ambiguous; this file
#             only pins what the platform does with the question and the answer.
#
# WHY THIS FILE EXISTS. `question_views` did not pass `option_values` through, so `answered` stored the OPTION,
# which for this question is a sentence for a person to read. `Given.representation` is a confirmed field, so the
# sentence won over the measured word and the loop's own consistency check then refused every plan on a part whose
# side had been answered: answering made the job worse than saying nothing. Nothing pinned it.
#
# THE PART is `F0_block_sharp` from `eval/hard_real`, a 120 x 80 x 40 mm solid block with a boss and ONE 16 mm
# through bore. It has no junctions. Read as the fluid it grows four, which is the whole reason for the question.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.agent.catalog", needs="the fluid-side question the catalog raises")

import meshpipeline.settings.package_switches as package_switches  # noqa: E402
import meshpipeline.settings.policy as polcfg  # noqa: E402
from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "block_boss_sharp"
THROUGH = "the fluid runs through the bores; the part is the solid around it"


@pytest.fixture
def side_on(monkeypatch):
    """The package's own switch, armed the way the platform arms it and not by hand."""
    monkeypatch.setattr(polcfg, "GEOMETRY_FLUID_SIDE_ENABLED", True)
    monkeypatch.setattr(polcfg, "GEOMETRY_MEASURED_STOPS_ENABLED", False)
    for name in package_switches.PACKAGE_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    assert polcfg.arm_the_package()["GEOMETRY_AGENT_FLUID_SIDE"] == "on"


def _doc() -> dict:
    return json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8"))


def _brief() -> str:
    return (FIXTURES / f"{CASE}.brief.txt").read_text(encoding="utf-8")


def _fresh() -> dict:
    return gs.carry_answers(None, gs.compose(_doc(), purpose="internal_cfd", brief=_brief(), declared=[]))


def _side_view(state: dict) -> dict:
    got = [v for v in gs.question_views(state) if v["id"] == "fluid_side"]
    assert got, f"the side was not asked; questions were {[v['id'] for v in gs.question_views(state)]}"
    return got[0]


def test_the_question_is_put_on_a_part_whose_brief_does_not_say_which_side_is_the_fluid(side_on):
    view = _side_view(_fresh())
    assert view["about"] == "representation"
    assert len(view["options"]) == 2, view["options"]
    assert THROUGH in view["options"]


def test_each_option_carries_the_representation_it_means_beside_the_words(side_on):
    """`options` are for a person and `option_values` are for the chain, paired by position. Without the pair a
    caller has to read a machine value back out of prose, and the one that tried stored the prose."""
    view = _side_view(_fresh())
    values = view["option_values"]
    assert len(values) == len(view["options"]), (view["options"], values)
    assert set(values) == {"wall_shell", "annular_fluid"}, values
    assert values[view["options"].index(THROUGH)] == "wall_shell"


def test_answering_stores_the_representation_and_never_the_sentence(side_on):
    """THE REGRESSION. The stored value is what the whole chain reads as `Given.representation`; a sentence there
    contradicts the survey's own word and the loop refuses the plan.

    Read off `answers` and not `live_answers`: answering RECOMPOSES, the side is settled in the new survey, so the
    question is gone and an answer bound to a question that no longer exists is not live. It is still the record of
    what the person said, and `composed_for.confirmed_representation` is what carries it forward.
    """
    state = gs.answered(_fresh(), _doc(), question_id="fluid_side", choice=THROUGH, words=THROUGH,
                        latest_user_message=THROUGH, principal="owner-test")
    said = [a for a in (state.get("answers") or []) if a.get("question_id") == "fluid_side"]
    assert said, state.get("answers")
    assert said[-1]["value"] == "wall_shell", said[-1]
    assert said[-1]["value"] != THROUGH, "the sentence is the words, never the value"


def test_the_words_the_customer_read_are_still_kept(side_on):
    """Nothing a person said is lost: the value is the representation and the sentence is still on the record."""
    state = gs.answered(_fresh(), _doc(), question_id="fluid_side", choice=THROUGH, words=THROUGH,
                        latest_user_message=THROUGH, principal="owner-test")
    said = [a for a in (state.get("answers") or []) if a.get("question_id") == "fluid_side"][-1]
    assert THROUGH in json.dumps(said), said


def test_the_answer_composes_the_survey_again_so_the_row_stops_disagreeing_with_it(side_on):
    """A confirmed side is a new composition. Before it was, the row kept the representation measured before
    anybody answered and the agent's check refused the plan for contradicting a survey nobody had recomposed."""
    before = _fresh()
    assert (before.get("composed_for") or {}).get("representation") == "annular_fluid"
    after = gs.answered(before, _doc(), question_id="fluid_side", choice=THROUGH, words=THROUGH,
                        latest_user_message=THROUGH, principal="owner-test")
    composed = after.get("composed_for") or {}
    assert composed.get("representation") == "wall_shell"
    assert composed.get("confirmed_representation") == "wall_shell"


def test_a_skipped_side_confirms_nothing_and_places_nothing_on_the_flow_path(side_on):
    """A DEFAULT IS NOT A CONFIRMATION. Skipped, today's reading stands so the job does not change, but it is
    marked as a default nobody stated and no place is put on the flow path behind it."""
    state = gs.answered(_fresh(), _doc(), question_id="fluid_side", words="I cannot say",
                        latest_user_message="I cannot say", skipped=True, principal="owner-test")
    assert gs.confirmed_representation(state) is None
    block = gs.builder_block(state)
    assert (block["survey"]["representation"]["kind"]) == "assumed", block["survey"]["representation"]
    assert block["places"] == [], block["places"]
    unsettled = [u for u in block["survey"]["unsettled"] if u["about"] == "representation"]
    assert unsettled, "the builder is owed the reason there is no flow path"
    assert "until a person says which it is" in unsettled[0]["why"]
