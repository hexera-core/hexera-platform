# Responsibility: Verify the chat says what the application knows - the mesh's size, the run's result, the user's notes, the files' unit - and asks for approval once, reproducing the demo dry run of 2 Nov 2026.
# Boundaries: the chat's facts and guards, the intake turn with a stand-in model, the approval reading; no
#             database, no network. The replies quoted are the ones the dry run recorded.
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

import meshpipeline.agents.intake.agent as intake
from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import chat_facts as cf
from meshpipeline.application import geometry_confirmation as gcf
from meshpipeline.contracts import user_notes as un
from meshpipeline.contracts.mesh_units import export_units_words

SID = uuid.UUID("cccc1111-2222-4222-b222-cccccccccccc")
OLD_JOB = uuid.uuid4()

# the dry run's own replies (~/demos/dryrun/*.json, 2 Nov)
CAMARO_SIZE = (
    "Roughly **10–30 million cells** for a full-size Camaro external-aerodynamics mesh with:\n\n"
    "- Surface refinement around the car, wheels, underbody, and ground clearance\n"
    "- A refined wake region\n- 3 prism layers on the car and ground\n- The stated far-field dimensions\n\n"
    "A coarser preliminary mesh could be around **3–8 million cells**, while a higher-detail production "
    "mesh could exceed **30 million**. The final count depends mainly on the chosen surface, wake, and "
    "base-cell sizing; it is not fixed by the vehicle’s physical size alone.")
MANIFOLD_SIZE = (
    "The exact count is determined during meshing because the edge length scales with the local passage "
    "radius. With 0.15 × local radius, the 35.5 mm runners would have a surface sizing of roughly 2.7 mm, "
    "while the 63 mm outlet would be roughly 4.7 mm.\n\nFor this header, I would expect roughly "
    "**50,000–200,000 volume cells**, including the 5 near-wall layers. The final count may vary with the "
    "branch junctions, curvature, and local radius transitions.")
PUNCH_SIZE = (
    "The exact count is determined automatically from the geometry and local feature sizes, so no fixed "
    "element count was specified.\n\nFor a bonded metal-tool assembly of about 639 × 411 × 52 mm, a typical "
    "quadratic tetrahedral mesh might contain roughly **100,000 to 1,000,000 elements**, depending mainly on:\n\n"
    "- Small holes, fillets, and narrow sections\n- Resolution needed across the 0.5 mm gaps\n\n"
    "The delivered mesh will use **quadratic tetrahedra**; the 0.5 mm gaps may drive local refinement and "
    "therefore increase the total. The actual count will be reported after meshing.")
KNUCKLE_SIZE = (
    "For a C5 Corvette front suspension knuckle, a reasonable rough estimate is:\n\n"
    "- **Coarse screening mesh:** 50,000–150,000 quadratic tetrahedra\n"
    "- **Typical static-stress mesh:** 200,000–800,000 quadratic tetrahedra\n"
    "- **Detailed stress-concentration study:** 1–3 million or more\n\n"
    "The final count depends mainly on refinement around bolt holes, fillets, bearing seats, and other "
    "sharp or highly curved features.")
KNUCKLE_UNITS = (
    "The mesh coordinates will remain in **millimetres**, matching the uploaded geometry.\n\n"
    "FEA mesh files are generally unitless, so the solver must use a consistent unit system.")
PUNCH_AFTER = (
    "The mesh completed successfully and passed the automatic validity checks.\n\n"
    "- Worst aspect ratio: **23.48**; target was at most **20**.\n"
    "- **22 elements** were located at sharp CAD cusps and set aside from the quality floor.\n"
    "- Walls get at least 2 elements through them, with 3 prism layers elsewhere.")
KNUCKLE_NOTE = ("Front suspension knuckle. Loads go in through the ball-joint and tie-rod bosses and the hub "
                "bore. Keep fillets at the arm roots and resolve the bolt holes for the caliper mount.")


def _fea_declaration(unit="mm"):
    return gcf.fea_confirmation_message(SimpleNamespace(
        part="C5 Corvette Knuckle", bodies=1, fea_connection="", clean_summary="The geometry is clean.",
        size_mm=[263.64, 120.88, 317.2], unit=unit, unit_fixed=False))


def _state(**over):
    base = {"job_id": str(SID), "session_id": str(SID), "user_id": "alice",
            "messages": [{"role": "user", "content": "This is for FEA, static stress."},
                         {"role": "assistant", "content": _fea_declaration()},
                         {"role": "user", "content": "How big will the mesh be?"}],
            "request_txt": "", "engine": "gmsh", "purpose": "structural", "input_kind": "solid-body",
            "dimensionality": "3D", "engine_params": {}, "intake_patches": [],
            "requested_mesh_fidelity": None, "intake_gate": {}}
    base.update(over)
    return base


# 1. the size comes from the run, never a guess

@pytest.mark.parametrize("reply", [CAMARO_SIZE, MANIFOLD_SIZE, PUNCH_SIZE, KNUCKLE_SIZE])
def test_a_guessed_size_before_any_run_is_replaced_by_not_computed_yet(reply):
    held = cf.hold_to_facts(reply, _state())
    assert held.changed
    assert cf.size_fact(_state()) in held.text
    for guess in ("10–30 million", "3–8 million", "50,000–200,000", "100,000 to 1,000,000",
                  "200,000–800,000", "1–3 million"):
        assert guess not in held.text, f"the guess {guess!r} still reaches the user"


def test_what_is_not_a_size_stays_in_the_reply():
    held = cf.hold_to_facts(MANIFOLD_SIZE, _state())
    assert "2.7 mm" in held.text and "local passage radius" in held.text
    knuckle = cf.hold_to_facts(KNUCKLE_SIZE, _state())
    assert "rough estimate is:" not in knuckle.text, "a lead-in was left with nothing after it"
    assert "bolt holes" in knuckle.text


def test_small_counts_and_quality_figures_are_not_sizes():
    after = _state(previous_run={"job_id": str(OLD_JOB), "status": "succeeded", "cells": 3_841_270,
                                 "purpose": "structural", "outcome": "Passed, with notes"})
    held = cf.hold_to_facts(PUNCH_AFTER, after)
    assert not held.changed and held.text == PUNCH_AFTER


def test_after_the_run_the_measured_count_stands_and_a_wrong_one_does_not():
    after = _state(previous_run={"job_id": str(OLD_JOB), "status": "succeeded", "cells": 3_660_268,
                                 "purpose": "external_cfd", "outcome": "ok"})
    right = "The delivered mesh has **3,660,268 cells** (about 3.7M cells)."
    assert not cf.hold_to_facts(right, after).changed
    wrong = cf.hold_to_facts("It has roughly 10–30 million cells.", after)
    assert wrong.changed and "3,660,268 cells" in wrong.text
    assert cf.size_fact(after) == "The mesh the last run delivered has 3,660,268 cells."
    fea = _state(previous_run={"job_id": "j", "status": "succeeded", "cells": 1_017_710, "purpose": "structural"})
    assert cf.size_fact(fea).endswith("1,017,710 elements.")
    # before a run, an FEA mesh is still counted in elements, a flow mesh in cells
    assert "about ... elements" in cf.size_fact(_state())
    assert "about ... cells" in cf.size_fact(_state(purpose="external_cfd"))


# 3. the notes are in the chat's context

def test_every_turn_is_given_the_users_notes_word_for_word():
    notes = un.clean([un.make(KNUCKLE_NOTE, source=un.SOURCE_CONSOLE)])
    block = cf.facts_block(_state(user_notes=notes))
    assert KNUCKLE_NOTE in block
    assert "never say the requirements do not mention it" in block
    assert un.chat_block([], consumer="chat") == ""


def test_the_intake_turn_carries_the_facts_and_holds_the_reply(monkeypatch):
    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.contracts.model_inference import ModelRoundResult

    seen: list = []

    async def _call(**kw):
        seen.append(kw["messages"][0]["content"])
        return ModelRoundResult(tool_calls=(), assistant_text=KNUCKLE_SIZE, finish_reason="stop")
    monkeypatch.setattr(llm_router, "call_intake_model", _call)
    notes = un.clean([un.make(KNUCKLE_NOTE, source=un.SOURCE_CONSOLE)])
    out = asyncio.run(intake.node_intake(_state(user_notes=notes)))
    system = seen[-1]
    assert KNUCKLE_NOTE in system, "the notes box never reached the chat"
    assert cf.size_fact(_state()) in system and "metres" in system
    reply = out["messages"][-1]["content"]
    assert cf.size_fact(_state()) in reply and "200,000" not in reply


# 5. the files' unit, one sentence everywhere

def test_the_units_sentence_names_the_conversion():
    said = export_units_words("mm")
    assert "metres" in said and "1 mm = 0.001 m" in said
    assert "millimetres" in said          # the places we show stay in millimetres
    assert "metres" in export_units_words(None)


def test_the_declared_unit_is_read_back_from_the_declaration():
    assert gcf.declared_unit([{"role": "assistant", "content": _fea_declaration("mm")}]) == "mm"
    assert gcf.declared_unit([{"role": "assistant", "content": _fea_declaration("in")}]) == "in"
    assert gcf.declared_unit([{"role": "user", "content": "The file is in millimetres"}]) == ""


def test_a_reply_saying_the_files_stay_in_millimetres_is_corrected():
    held = cf.hold_to_facts(KNUCKLE_UNITS, _state())
    assert held.changed
    assert "remain in **millimetres**" not in held.text
    assert "Lengths in the delivered files are in metres" in held.text and "1 mm = 0.001 m" in held.text
    # the right answer, and a place given in millimetres, are left alone
    for fine in ("The delivered files are in metres, converted from your millimetres.",
                 "The cusp is at (76.01, 22, 77.79) mm."):
        assert not cf.hold_to_facts(fine, _state()).changed


def test_the_fea_result_screen_says_the_same_units_sentence():
    import inspect

    from meshpipeline.engines import fea_results
    assert "export_units_words()" in inspect.getsource(fea_results.results)


# 2. after the run, the chat knows the run

def test_the_turn_after_a_run_says_the_mesh_exists_and_its_size():
    st = _state(previous_run={"job_id": str(OLD_JOB), "status": "succeeded", "cells": 5_606_651,
                              "engine": "snappyHexMesh", "purpose": "internal_cfd",
                              "outcome": "Mesh generation completed successfully.\nEngine: snappy"})
    block = intake._previous_run_block(st)
    assert "WAS GENERATED" in block and "never say it has not been generated" in block
    assert "5,606,651 cells" in block and "snappyHexMesh" in block
    assert "Mesh generation completed successfully." in block


@pytest.mark.parametrize("question", [
    "How did the mesh turn out, and does anything need attention before running the solver?",
    "How does the mesh look, and does anything need attention before I run the solver?",
    "How did the knuckle mesh turn out, and does anything need attention before solving?",
])
def test_a_question_about_the_finished_run_only_answers(question):
    st = _state(previous_run={"job_id": "j", "status": "succeeded"})
    assert cf.asks_about_last_run(st, question)
    assert not cf.asks_about_last_run(_state(), question), "no run yet: nothing to answer from"
    assert not cf.asks_about_last_run(st, "Run it again with 5 layers.")
    assert not cf.asks_about_last_run(st, "Could we try cfMesh instead?")


def test_a_submission_in_an_answer_only_turn_is_refused():
    from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor

    st = IntakeExecutionState(session_id=str(SID), owner_id="alice", revision="r", user_msg_count=3,
                              latest_user_msg="How did the mesh turn out?", answer_only=True)
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"], search_tool=None)
    res = asyncio.run(ex._do_submit_requirements({"mesh_engine": "snappy"}))
    assert res.accepted is False and "the mesh exists" in res.content
    assert st.submit_args is None and st.approval is None


# 7. approval asked once

@pytest.mark.parametrize("message", [
    "Which tool parts should I set as bonded contacts in the static stress model?",
    "How did the mesh turn out?",
    "Do I need to add a load case in my solver?",
    "What cell count should I expect?",
])
def test_a_question_is_a_question(message):
    assert ap.classify(message) == ap.QUESTION_INTENT


@pytest.mark.parametrize("message", [
    "Can I add more layers?", "Could you turn the layers off?", "Can you make it finer?",
    "No, use 5 layers; only 63 mm outlet; runner bores are 35.5 mm.",
])
def test_a_change_is_still_a_change(message):
    assert ap.classify(message) == ap.CORRECTION_INTENT


def _summary(**over):
    base = ap.create(owner_id="alice", session_id=str(SID), selection_id="sel", token_id="t",
                     canonical={}, fingerprint="f", payload={"engine_params": {"n_layers": 3}},
                     summary="s", proposal_revision="r", proposal_msg_count=4)
    base.update(over)
    return base


def test_a_summary_withdrawn_by_a_message_that_changed_nothing_stands_again():
    withdrawn = ap.invalidate(_summary(), "user replied with a correction")
    assert ap.revivable(withdrawn, user_msg_count=5)
    revived = ap.revive(withdrawn, user_msg_count=5)
    assert ap.is_live(revived) and revived["expected_confirmation_msg_count"] == 6
    assert "invalidated_reason" not in revived
    # never one a gate refused, an older message withdrew, or that has expired
    assert not ap.revivable(ap.invalidate(_summary(), "admission became impossible"), user_msg_count=5)
    assert not ap.revivable(withdrawn, user_msg_count=7)
    assert not ap.revivable(ap.invalidate(_summary(expires_at=time.time() - 1), "user replied with a x"),
                            user_msg_count=5)


def test_the_intake_turn_revives_the_summary_when_it_submitted_nothing(monkeypatch):
    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.contracts.model_inference import ModelRoundResult

    async def _call(**kw):
        return ModelRoundResult(tool_calls=(), assistant_text="Bond Part_1 to Part_3 and Part_4.",
                                finish_reason="stop")
    monkeypatch.setattr(llm_router, "call_intake_model", _call)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(4)] + [
        {"role": "assistant", "content": "Shall I proceed?"},
        {"role": "user", "content": "Which tool parts should I set as bonded contacts?"}]
    withdrawn = ap.invalidate(_summary(), "user replied with a correction")
    out = asyncio.run(intake.node_intake(_state(
        messages=msgs, request_txt="submitted", awaiting_confirmation=True,
        intake_gate={"approval": withdrawn, "selection": {"id": "sel", "state": "confirmed"}})))
    after = out["intake_gate"]["approval"]
    assert after["status"] == ap.AWAITING and after["expected_confirmation_msg_count"] == 6, (
        "a question cost the summary: the next yes would be asked to confirm it again")


def test_a_redone_summary_says_what_changed():
    before = {"engine_params": {"n_layers": 3}, "patches": [{"name": "inlet", "role": "inlet"},
                                                           {"name": "collector_outlet", "role": "outlet"}]}
    after = {"engine_params": {"n_layers": 5}, "patches": [{"name": "collector_outlet", "role": "outlet"}]}
    changes = ap.changes_between(before, after)
    assert "n layers: 3 -> 5" in changes and "inlet removed" in changes
    line = ap.changed_line(changes)
    assert line.startswith("Changed from the last summary:") and "3 -> 5" in line
    assert ap.changed_line([]) == "" and ap.changes_between(before, before) == []


def test_the_executor_puts_the_change_on_the_summary():
    import inspect

    from meshpipeline.agents.intake import executor
    src = inspect.getsource(executor.IntakeToolExecutor._do_submit_requirements)
    assert "ap.changed_line(ap.changes_between(" in src
