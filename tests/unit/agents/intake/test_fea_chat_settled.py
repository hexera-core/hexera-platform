# Responsibility: Verify an FEA part's chat asks nothing the FEA path settles - the engine is selected
# without a question, and a reply asking the element order, the engine, near-wall layers or a detail
# level is sent back once and, if it still asks, has those questions taken out.
# Boundaries: the wording check on the real replies of the 2026-10-22 real-path test; one intake turn
# with the model's rounds scripted (as test_thermal_chat does) and the stored check stood in for.
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.executor import fea_path
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application.fea_wording import settled_asks, without_settled_asks
from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK, FEA_LEAD

SID = uuid.UUID("aaaa2222-3333-4333-b333-aaaaaaaaaaaa")
FEA_DECLARATION = (f"{CONFIRMED_MARK} {FEA_LEAD}, input_kind solid-body: the part itself is meshed, "
                   "with no fluid around it. One body. Clean check: The geometry is clean. Part size: "
                   "60 x 50 x 10 mm. The file is in millimetres.")

# the replies dev-areen sent on 2026-10-22 (fea0100, fea0080, bearing bracket)
ENGINE_ASK = ("I'd mesh it with Gmsh - in our lab: no lab results yet for shapes like this; no reliable "
              "near-wall layers. Heads-up: Many sharp edges: tetrahedra pinned between two faces at a sharp "
              "edge come out as flat slivers (blade-row passages at 6.5-9.7: worst face 86-88 degrees; "
              "rotors, high-lift aircraft). OK, or pick another engine?")
ORDER_ASKS = (
    "For static stress, I'd use second-order quadratic tetrahedra for better accuracy on the 10 x 10 x 46 mm "
    "solid.  Use second-order elements, or switch to first-order?",
    "Element order: I recommend **second-order (quadratic) tetrahedra** for static-stress accuracy. Use that, "
    "or switch to first-order elements for a smaller, faster mesh?",
    "For the static-stress mesh, I'd use second-order quadratic tetrahedral elements for better stress "
    "accuracy. OK, or would you prefer first-order linear elements?",
)


@pytest.mark.parametrize("reply", ORDER_ASKS)
def test_the_element_order_question_is_caught(reply):
    assert "the element order" in settled_asks(reply)


def test_the_engine_question_with_cfd_words_is_caught():
    asked = settled_asks(ENGINE_ASK)
    assert "the engine" in asked


@pytest.mark.parametrize("reply", [
    "Please confirm the requirements above before I mesh anything.\n\nShall I proceed with mesh generation? "
    "Reply yes to start it, or tell me what to change.",
    "Shall I proceed with the Gmsh mesh of quadratic tetrahedra?",
    "What will you analyze - static stress, vibration (modal) or buckling?",
    "Every face is a named group, so you set supports and loads in your own solver.",
    "The mesh uses quadratic 10-node tetrahedra.",              # said, not asked
])
def test_the_approval_the_analysis_question_and_plain_statements_are_not_caught(reply):
    assert settled_asks(reply) == []


def test_a_topic_the_user_raised_is_theirs_to_discuss():
    reply = "First-order elements are smaller; do you want first-order then?"
    assert settled_asks(reply, ["can I have first-order elements instead?"]) == []
    assert settled_asks(reply) == ["the element order"]


def test_the_settled_questions_are_taken_out_and_the_rest_kept():
    text = "Static stress it is. " + ORDER_ASKS[2]
    trimmed = without_settled_asks(text)
    assert trimmed.startswith("Static stress it is.")
    assert "first-order" not in trimmed and settled_asks(trimmed) == []


def test_an_fea_part_is_never_measured_inside_the_api(monkeypatch):
    # fea0260 (5.9 MB sheet metal) ran the API container out of memory on every confirm retry
    from meshpipeline.agents.intake import measured_choice

    def _never(*a, **k):
        raise AssertionError("an FEA part's upload was staged to be measured in the API")
    monkeypatch.setattr(measured_choice, "staged_upload", _never)
    messages = [{"role": "user", "content": "This is for FEA, static stress."},
                {"role": "assistant", "content": FEA_DECLARATION},
                {"role": "user", "content": gh.CONTINUE_TEXT}]
    state = {"job_id": "j", "session_id": str(SID), "user_id": "u", "messages": messages}
    assert asyncio.run(intake._measured_recommendation(state, messages)) is None


def test_the_fea_path_is_the_structural_case_on_a_solid():
    assert fea_path(SimpleNamespace(declared_case=("structural", "solid-body")))
    assert not fea_path(SimpleNamespace(declared_case=("internal_cfd", "fluid-volume")))
    assert not fea_path(SimpleNamespace(declared_case=None))


# ---------------------------------------------------------- one intake turn, model scripted ----
class _Model:
    def __init__(self, rounds):
        self.rounds, self.tools, self.user_lines = iter(rounds), [], []

    async def __call__(self, **kw):
        messages = kw.get("messages") or []
        self.tools = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        self.user_lines = [m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        return next(self.rounds)


def _tc(name, args):
    return SimpleNamespace(id=f"t-{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


def _turn(model):
    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.application import geometry_check as gc

    messages = [{"role": "user", "content": "This is for FEA, static stress."},
                {"role": "assistant", "content": FEA_DECLARATION},
                {"role": "user", "content": gh.CONTINUE_TEXT}]
    ref = {"source_id": "s1", "owner_id": "u", "object_key": "k", "sha256": "a" * 64, "size_bytes": 10,
           "original_filename": "bracket.step", "suffix_hint": ".step"}
    state = {"job_id": "j", "session_id": str(SID), "user_id": "u", "messages": messages,
             "geometry": {"ref": ref}}

    async def _measured(state, msgs):
        return None
    with patch.object(llm_router, "call_intake_model", model), \
            patch.object(intake, "_measured_recommendation", _measured), \
            patch.object(gc, "read_check", lambda sid: {}):
        return asyncio.run(intake.node_intake(state))


def test_the_engine_is_selected_without_a_question_and_the_element_order_is_not_asked():
    model = _Model([
        _resp([_tc("propose_engine_selection", {"engine": "gmsh", "reason": "solid FEA mesh"})]),
        _resp(content=ORDER_ASKS[0]),
        _resp(content="Static stress it is: every face of the part is kept as a named group."),
    ])
    out = _turn(model)
    sel = out["intake_gate"]["selection"]
    assert sel["engine"] == "gmsh" and sel["state"] == es.CONFIRMED and sel["via"] == es.VIA_FEA_PATH
    assert "SELECTED" in model.tools[0]["content"]
    assert turn.FEA_SETTLED_NUDGE in model.user_lines
    reply = out["messages"][-1]["content"]
    assert reply == "Static stress it is: every face of the part is kept as a named group."
    assert "I'd mesh it with" not in reply and settled_asks(reply) == []


def test_a_reply_that_asks_again_after_the_nudge_has_the_question_taken_out():
    model = _Model([
        _resp(content="Static stress it is. " + ORDER_ASKS[2]),
        _resp(content="Static stress it is. " + ORDER_ASKS[1]),
    ])
    out = _turn(model)
    assert turn.FEA_SETTLED_NUDGE in model.user_lines
    reply = out["messages"][-1]["content"]
    assert reply.startswith("Static stress it is.") and settled_asks(reply) == []
