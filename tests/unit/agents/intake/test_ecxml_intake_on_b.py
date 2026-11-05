# Responsibility: Pin the three intake fixes a real ECXML take on the snap-grid mesher needed: the engine that reads the upload ranks first, a shortened name binds to the engine proposed, and a question about the summary keeps it standing.
# Boundaries: pure reads of the ranking, the engine-answer readers and the approval grammar, plus the chat route with the model and the dispatch faked.
from __future__ import annotations

import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import measured_choice as mc
from meshpipeline.cad.shape_traits import ShapeTraits
from meshpipeline.engines.fitness import reads_own_format, recommend
from meshpipeline.engines.registry import engine_names

# THE TAKE (dev-areen, set_top_box.ecxml, 2026-10-11). The intake proposed snappyHexMesh
# multi-region for an ECXML model; the persona's "snappyHexMesh is fine" was taken as PLAIN
# snappyHexMesh, refused for heat transfer, and asked again three times; and after a question
# about the summary, "Yes, go ahead." found no summary to approve.

MULTI = "snappy_multiregion"


# 1. the engine that reads the upload itself ranks first - from the declarations, not by name


def test_the_format_reader_is_found_from_the_declarations():
    assert mc.source_format_of("/jobs/s/input.ecxml") == "ecxml"
    assert mc.source_format_of("", ".step") == "step"
    readers = mc.own_format_engines(engine_names(), "ecxml")
    assert readers and all(reads_own_format(e, "ecxml") for e in readers)
    assert mc.own_format_engines(engine_names(), "step") == []


def test_the_engine_that_reads_the_upload_ranks_first():
    readers = mc.own_format_engines(engine_names(), "ecxml")
    traits = ShapeTraits(flow="multi-region", input_kind="solid-assembly")
    able = [MULTI] + readers
    rec = recommend(traits, able, source_format="ecxml")
    assert rec.engine == readers[0], rec.ranking
    assert rec.ranking[0] == readers[0]


def test_without_a_format_reader_the_ranking_is_unchanged():
    traits = ShapeTraits(flow="multi-region", input_kind="solid-assembly")
    able = [MULTI] + mc.own_format_engines(engine_names(), "ecxml")
    assert recommend(traits, able, source_format="step").ranking == recommend(traits, able).ranking


# 2. an accepted proposal binds to exactly the engine proposed


def test_a_shortened_name_is_the_proposed_engine_not_its_sibling():
    assert "snappy" in es.short_siblings(MULTI)
    assert es.short_siblings("snappy") == frozenset()
    for said in ("snappyHexMesh is fine.", "Yes, snappyHexMesh is fine.", "Yes, use snappyHexMesh."):
        assert not es.names_another_engine(MULTI, said), said
        assert es.plain_assent(MULTI, said), said


def test_a_qualified_or_different_name_is_still_a_new_choice():
    for said in ("use plain snappyHexMesh", "the single-region snappyHexMesh please", "use cfMesh"):
        assert es.names_another_engine(MULTI, said), said
    # the longer name is never a shortening of the shorter one
    assert es.names_another_engine("snappy", "snappyHexMesh multi-region is fine")


def _proposed(engine: str, fits=()):
    sel = es.propose(engine, session_id="s", owner_id="u", revision="r1", user_msg_count=3)
    sel["recommendation"] = {"fits": [{"engine": e} for e in fits]}
    return sel


def test_the_short_yes_confirms_the_proposed_engine():
    sel = _proposed(MULTI, fits=(MULTI, "snappy"))
    confirmed = es.confirm_by_assent(sel, session_id="s", owner_id="u", revision="r2",
                                     latest_user_message="Yes, snappyHexMesh is fine.",
                                     user_msg_count=4)
    assert confirmed is not None and confirmed["engine"] == MULTI
    assert confirmed["state"] == es.CONFIRMED
    # and the listed sibling is not picked from the same words
    assert es.choose_listed(sel, session_id="s", owner_id="u", revision="r2",
                            latest_user_message="Yes, snappyHexMesh is fine.",
                            user_msg_count=4) is None


# 3. a question about the summary keeps it standing


def test_a_question_about_the_summary_is_a_question():
    assert ap.classify("Before I say yes: which mesher will you use for this?") == ap.QUESTION_INTENT
    assert ap.classify("What cell count should I expect?") == ap.QUESTION_INTENT


def test_a_question_that_asks_for_a_change_is_still_a_correction():
    for said in ("Can you make it finer?", "Could we use cfMesh instead?", "change the engine to gmsh"):
        assert ap.classify(said) == ap.CORRECTION_INTENT, said
    for said in ("Yes, go ahead.", "Yes.", "OK, proceed."):
        assert ap.classify(said) == ap.APPROVE_INTENT, said


def test_after_a_question_the_next_yes_dispatches():
    from tests.unit.agents.intake.test_intake_approval_lifecycle import _approval, _route

    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route("Before I say yes: which mesher will you use for this?", rec, sel)
    assert calls["dispatch"] == 0 and calls["intake"] == 1, "the intake answers the question"
    stored = repo.set_intake_gate.call_args_list[0][0][2]["approval"]
    assert stored["status"] == ap.AWAITING, "the summary must not be thrown away by a question"
    assert stored["expected_confirmation_msg_count"] == rec["expected_confirmation_msg_count"] + 1
    # the user's next clear yes lands on that same summary
    assert ap.verify(stored, owner_id="u", session_id="s", selection=sel,
                     user_msg_count=stored["expected_confirmation_msg_count"])[0] is True
