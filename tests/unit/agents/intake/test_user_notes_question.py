# Responsibility: Verify the intake asks its one notes question once, keeps the user's words verbatim, and never holds a submission for it.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.executor import NOTES_KEPT
from meshpipeline.contracts import user_notes as un

_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
            {"name": "wall", "type": "wall"}]
_DECL = {"purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _PATCHES, "engine_params": {"element_order": "2"}}
_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion. " * 2)
_SUBMIT = {"domain": "duct internal", "request_txt": _R, "review_brief_txt": _B,
           "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
           "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
           "engine_params": {"element_order": "2"}, "patches": _PATCHES}
#: synthetic, multi-line, with units - a pasted spec must arrive exactly as typed
SPEC = ("Duct for a blower test rig, mm.\n"
        "- Keep at least 10 prism layers on the wall.\n"
        "- Inlet velocity 12 m/s, ramped over 2 s (solver setting).\n\n"
        "Outcome: pressure drop inlet to outlet.")


def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


def _submit_turn(messages, extra=None):
    """One intake turn whose model previews and then submits; returns (patch, user lines seen)."""
    import meshpipeline.contracts.model_inference as llm
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u",
                                          revision=at.revision_of(messages))
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(messages),
             "intake_gate": {"selection": sel, "admission": None}}
    seen: dict = {"tools": [], "users": []}
    rounds = iter([
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
        lambda: _resp([_tc("submit_requirements", json.dumps(
            {**_SUBMIT, **(extra or {}),
             "preview_token": json.loads(seen["tools"][-1]["content"])["preview_token"]}))]),
        _resp(content="unreached")])

    async def _call(**kw):
        msgs = [m for m in (kw.get("messages") or []) if isinstance(m, dict)]
        seen["tools"] = [m for m in msgs if m.get("role") == "tool"]
        seen["users"] = [m.get("content") for m in msgs if m.get("role") == "user"]
        nxt = next(rounds)
        return nxt() if callable(nxt) else nxt
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state)), seen["users"]


def test_the_first_submission_is_held_once_for_the_question():
    out, _ = _submit_turn([{"role": "user", "content": "mesh this duct for CFD"}])
    assert out["messages"][-1]["content"] == un.QUESTION
    # nothing approved, nothing submitted: the question holds the summary back one turn only
    assert not (out["intake_gate"] or {}).get("approval")
    assert not out.get("request_txt")


def test_the_answer_is_kept_verbatim_and_goes_straight_to_the_summary():
    msgs = [{"role": "user", "content": "mesh this duct for CFD"},
            {"role": "assistant", "content": un.QUESTION},
            {"role": "user", "content": SPEC}]
    out, users = _submit_turn(msgs)
    snap = out["intake_gate"]["approval"]
    assert snap["status"] == ap.AWAITING
    assert [n["text"] for n in snap["payload"]["user_notes"]] == [SPEC]
    assert [n["text"] for n in out["user_notes"]] == [SPEC]
    assert NOTES_KEPT in out["messages"][-1]["content"]
    # the request summary stays the model's own; the words ride beside it
    assert out["request_txt"].startswith("A complete requirements summary")
    # the answer closed the question, in code, before the model ran
    assert turn.NOTES_NUDGE in users


def test_a_no_skips_and_the_question_is_never_asked_again():
    msgs = [{"role": "user", "content": "mesh this duct for CFD"},
            {"role": "assistant", "content": un.QUESTION},
            {"role": "user", "content": "no"}]
    out, _ = _submit_turn(msgs)
    snap = out["intake_gate"]["approval"]
    assert snap["status"] == ap.AWAITING and "user_notes" not in snap["payload"]
    assert out["user_notes"] == [] and NOTES_KEPT not in out["messages"][-1]["content"]
    # a later change after the summary resubmits without asking again
    later = [*msgs, {"role": "assistant", "content": snap["summary"]},
             {"role": "user", "content": "make it finer"}]
    out2, _ = _submit_turn(later)
    assert out2["intake_gate"]["approval"]["status"] == ap.AWAITING


def test_details_the_user_volunteered_skip_the_question_and_are_kept_whole():
    said = "Internal flow through the duct. Please use 10 prism layers on the wall."
    out, _ = _submit_turn([{"role": "user", "content": said}],
                          extra={"user_note_quotes": ["10 prism layers"]})
    assert out["intake_gate"]["approval"]["status"] == ap.AWAITING
    assert [n["text"] for n in out["user_notes"]] == [said]
    assert NOTES_KEPT in out["messages"][-1]["content"]


def test_a_quote_the_user_never_wrote_is_not_a_note_and_the_question_is_asked():
    out, _ = _submit_turn([{"role": "user", "content": "mesh this duct for CFD"}],
                          extra={"user_note_quotes": ["use 20 layers"]})
    assert out["messages"][-1]["content"] == un.QUESTION and "user_notes" not in out


def test_the_prompt_tells_the_model_the_application_asks_and_it_never_does():
    block = intake._block_user_notes()
    assert un.QUESTION in block and "never ask it yourself" in block.lower()
    assert "user_note_quotes" in block
    assert turn.NOTES_NUDGE in turn.SYNTHETIC_NUDGES
