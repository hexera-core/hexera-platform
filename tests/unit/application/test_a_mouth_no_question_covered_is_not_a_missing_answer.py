# Responsibility: a measured opening that no role question ever named is the platform's to place, not the customer's to be asked about again.
# Boundaries: pure functions over stored fixtures; the conversation and the agent's own checker are other tests.
"""A customer who had already delegated was asked to type out sixteen opening ids.

MEASURED live on 2026-09-26, shell_and_tube_7_unshared, and reproduced offline against the part itself:

    measured openings          32
    representation             fluid_domain
    catalog port openings      16   o3, o4, o19-o32
    mouths in NO question      16   o1, o2, o5-o18

`ask.uncertainty.port_role_uncertainties` covers the BUILDER'S candidate set, which under a fluid-domain
representation is `catalog.port_openings`. So half that part's mouths appeared in no question and carried no
proposal. The customer said "no, you pick the rest"; `accepts_a_proposal` reads that correctly as a delegation;
the 16 covered mouths were recorded from the platform's own proposal, and the other 16 were refused with

    "tube_end is declared outlet and binds to o1, which the customer called nothing"

so the model's only lever was to ask them to name sixteen mouths. It did, for four turns, and the job never
submitted out of nine.

AND IT IS NOT RARE. Of the eight stored fixtures here, block_boss_sharp has 5 of 7 mouths in no question and
bend_elbow_001 has 2 of 4. Any job where the agent puts a flow role on one of those hit the same wall.

Their silence about a mouth nobody asked them about is not a refusal, and the platform does not need their
words to place it: R31 gives every opening the catalog cannot call a port the wall patch, so
`UNASKED_DEFAULT_ROLE` is what happens when nobody answers at all. Carrying it invents nothing. Where the
agent wants something else the two readings disagree, which is not a missing answer, and the owner's call on
2026-09-26 was to put that once naming both rather than to guess either way.
"""
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.application import geometry_survey as gs

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"


def _state(case: str) -> dict:
    doc = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
    bt = FIXTURES / f"{case}.brief.txt"
    brief = bt.read_text(encoding="utf-8") if bt.is_file() else "internal cfd, air through it"
    return doc, gs.carry_answers(None, gs.compose(doc, purpose="internal_cfd", brief=brief))


def _settled(state: dict) -> dict:
    """The customer delegates, so every mouth a question NAMED is settled from the platform's proposal."""
    for view in [v for v in gs.question_views(state) if v["about"] == "opening.role"]:
        state = gs.record_answer(state, question_id=view["id"], words="no, you pick the rest",
                                 latest_user_message="no, you pick the rest", accepted_proposal=True,
                                 principal="dev-user")
    return state


def _patch(name: str, role: str, opening: dict) -> dict:
    """`_match` reads `opening_id` first and says why: named beats inferred, because size-matching cannot
    separate two mouths of the same bore. So the patch names the mouth, which is what a real one does."""
    return {"name": name, "type": role, "opening_id": str(opening.get("id") or "")}


def _an_uncovered_opening(doc: dict, state: dict) -> dict:
    covered = gs._covered_by_a_role_question(state)
    for o in (doc.get("openings") or []):
        if str(o.get("id")) not in covered:
            return o
    raise AssertionError("this fixture covers every mouth; pick another")


def test_the_questions_do_not_cover_every_measured_mouth():
    """The premise, read off the stored fixtures rather than asserted."""
    for case, expect_uncovered in (("block_boss_sharp", 5), ("bend_elbow_001", 2)):
        doc, state = _state(case)
        ids = {str(o.get("id")) for o in (doc.get("openings") or [])}
        assert len(ids - gs._covered_by_a_role_question(state)) == expect_uncovered, case


def test_no_question_at_all_covers_nothing():
    assert gs._covered_by_a_role_question(None) == set()
    assert gs._covered_by_a_role_question({}) == set()


def test_the_default_is_the_one_the_pipeline_already_applies():
    """If this stops being what R31 does when nobody answers, it has stopped being a default."""
    assert gs.UNASKED_DEFAULT_ROLE == "wall"


def test_the_platforms_own_reading_is_carried_without_asking_anybody():
    doc, state = _state("block_boss_sharp")
    state = _settled(state)
    mouth = _an_uncovered_opening(doc, state)
    problems = gs.role_problems(state, doc, [_patch("shell", gs.UNASKED_DEFAULT_ROLE, mouth)])
    assert not [p for p in problems if str(mouth["id"]) in p], problems


def test_a_flow_role_on_an_uncovered_mouth_asks_once_and_names_both_readings():
    doc, state = _state("block_boss_sharp")
    state = _settled(state)
    mouth = _an_uncovered_opening(doc, state)
    oid = str(mouth["id"])
    # a second outlet on a mouth a question DID name, so this one is not the only outlet. Without it the
    # load-bearing carve-out fires and the agent's reading stands, which the test below this one covers.
    covered = sorted(gs._covered_by_a_role_question(state))
    other = next(o for o in doc["openings"] if str(o["id"]) == covered[0])
    problems = gs.role_problems(state, doc, [_patch("tube_end", "outlet", mouth),
                                             _patch("port", "outlet", other)])
    hit = [p for p in problems if oid in p]
    assert hit, problems
    said = hit[0]
    assert "NO role question covered" in said
    assert f"the platform's own reading of it is {gs.UNASKED_DEFAULT_ROLE!r}" in said
    assert f"you read {oid} as outlet" in said, "both readings named"
    assert "say in the setup block" in said, "it rides on the confirmation, not a turn of its own"
    assert "one word from them changes it" in said
    # MEASURED on shell_and_tube_7 the first time this was a question of its own: it fired correctly,
    # the customer answered "good to go", which is not a choice between two readings, and it asked to
    # the turn cap. The owner's rule is asked once, take the best reading, move on.
    assert "NOT put it as a question of its own" in said
    assert "NOT ask them to list the mouths" in said, "four turns went on exactly that"


def test_a_mouth_they_were_asked_about_is_untouched_by_this():
    """The change is only about mouths nobody put to them. Where a question named it, the answer governs."""
    doc, state = _state("block_boss_sharp")
    state = _settled(state)
    covered = sorted(gs._covered_by_a_role_question(state))
    assert covered, "fixture must have at least one covered mouth"
    named = next(o for o in doc["openings"] if str(o.get("id")) == covered[0])
    confirmed = gs.confirmed_roles(state).get(covered[0])
    wrong = "inlet" if confirmed != "inlet" else "outlet"
    problems = gs.role_problems(state, doc, [_patch("port", wrong, named)])
    assert not any("NO role question covered" in p for p in problems), \
        "a covered mouth must not fall down the uncovered path"

def test_the_default_is_not_forced_where_it_would_leave_the_flow_nowhere_to_go():
    """MEASURED on the 36-run batch after the first version of this rule: two plans died with

        ["flow.inlet_ids contains o1 whose patch role is 'wall'",
         "flow.outlet_ids contains o2 whose patch role is 'wall'"]

    Both of that part's flow mouths were uncovered, the rule demanded the default on both, and the plan lost
    its inlet AND its outlet. The plan rate went 86 per cent to 79 on my own change.

    A mouth carrying the ONLY inlet, or the only outlet, is not a face where two readings differ by a detail:
    the agent's reading is the only one that leaves a runnable job. So it stands, and the customer sees it on
    the faces line of the setup they confirm, which is the same disclosure a role we proposed ourselves gets.
    """
    doc, state = _state("block_boss_sharp")
    state = _settled(state)
    covered = sorted(gs._covered_by_a_role_question(state))
    mouth = _an_uncovered_opening(doc, state)
    oid = str(mouth["id"])

    # the only inlet in the whole plan sits on an uncovered mouth: it stands
    alone = [_patch("in", "inlet", mouth),
             _patch("out", "outlet", next(o for o in doc["openings"] if str(o["id"]) == covered[0]))]
    problems = gs.role_problems(state, doc, alone)
    assert not any("NO role question covered" in p for p in problems), problems

    # a SECOND inlet elsewhere, so this one is no longer load bearing: the default is demanded again
    second = next(o for o in doc["openings"]
                  if str(o["id"]) not in (oid, covered[0]))
    crowded = alone + [_patch("in2", "inlet", second)]
    problems = gs.role_problems(state, doc, crowded)
    assert any("NO role question covered" in p and oid in p for p in problems), problems
