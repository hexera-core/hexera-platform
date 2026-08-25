# Responsibility: Verify a confirmation turn dispatches only on the user's own words, and never from a model tool call.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

import meshpipeline.agents.intake.admission_token as at  # noqa: E402
import meshpipeline.agents.intake.agent as intake  # noqa: E402
import meshpipeline.agents.intake.engine_selection as es  # noqa: E402


def _seed_selected_token(state: dict) -> str:
    _eng = state.get("engine", "")
    sel = es.select_from_structured_input(
        _eng, session_id=str(state.get("session_id", "")), owner_id=str(state.get("user_id", "")),
        revision=at.revision_of(state.get("messages", [])))
    tok = at.issue(session_id=str(state.get("session_id", "")), owner_id=str(state.get("user_id", "")),
                   revision=at.revision_of(state.get("messages", [])),
                   canonical={"engine": _eng}, verdict="supported", mode=at.SELECTED,
                   selection_id=sel["id"])
    state["intake_gate"] = {"selection": sel, "admission": tok}
    return tok["token"]


def _tool_call(name: str, args: str):
    return SimpleNamespace(id="tc1", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


def _run(state, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    it = iter(responses)

    async def _call(**_kw):
        return next(it)

    with patch.object(llm_router, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


_SUBMITTED = {
    "job_id": "j", "session_id": "s", "user_id": "u",
    "messages": [
        {"role": "user", "content": "external aero on a NACA 0012, snappy, 20 chords"},
        {"role": "assistant", "content": "Shall I proceed with mesh generation?"},
        {"role": "user", "content": "yes, but make the far-field 50 chords instead of 20"},
    ],
    "request_txt": "NACA 0012 external aero. Far-field 20 chords.",
    "review_brief_txt": "check it",
    "engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
    "dimensionality": "2D", "engine_params": {"topology": "external"},
    "intake_patches": [{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
}


# intake actually runs on the confirmation turn

def test_intake_runs_when_awaiting_confirmation():
    out = _run({**_SUBMITTED, "awaiting_confirmation": True},
               [_resp(content="Shall I proceed?")])
    assert out != {}


def test_intake_still_short_circuits_for_the_pipeline_graph():
    assert asyncio.run(intake.node_intake({**_SUBMITTED})) == {}


# the bug: agreement WITH a change is not consent

def test_agreement_with_a_change_does_not_dispatch():
    out = _run({**_SUBMITTED, "awaiting_confirmation": True},
               [_resp(content="I've updated the far-field to 50 chords. Shall I proceed?")])
    assert out.get("dispatch_confirmed") is False


def test_the_model_has_no_dispatch_tool_at_all():
    assert "confirm_dispatch" not in {t["function"]["name"] for t in intake.INTAKE_TOOLS}
    st = {**_SUBMITTED, "awaiting_confirmation": True}
    _seed_selected_token(st)
    out = _run(st, [
        _resp([_tool_call("confirm_dispatch", json.dumps({"user_agreed_verbatim": "yes"}))]),
        _resp(content="I cannot start it myself."),
    ])
    assert out.get("dispatch_confirmed") is False


def test_a_question_does_not_dispatch():
    out = _run({**_SUBMITTED, "awaiting_confirmation": True},
               [_resp(content="Do you want prism layers on the airfoil?")])
    assert out.get("dispatch_confirmed") is False


# confirm_dispatch guards: it spends money and cannot be undone

def test_confirm_dispatch_without_requirements_is_refused():
    out = _run({"job_id": "j", "messages": [{"role": "user", "content": "go"}]}, [
        _resp([_tool_call("confirm_dispatch", '{"user_agreed_verbatim": "go"}')]),
        _resp(content="I still need your requirements."),
    ])
    assert out.get("dispatch_confirmed") is False


def test_confirm_dispatch_without_a_quote_is_refused():
    out = _run({**_SUBMITTED, "awaiting_confirmation": True}, [
        _resp([_tool_call("confirm_dispatch", "{}")]),
        _resp(content="Could you confirm?"),
    ])
    assert out.get("dispatch_confirmed") is False


def test_confirm_dispatch_with_a_blank_quote_is_refused():
    out = _run({**_SUBMITTED, "awaiting_confirmation": True}, [
        _resp([_tool_call("confirm_dispatch", '{"user_agreed_verbatim": "   "}')]),
        _resp(content="Could you confirm?"),
    ])
    assert out.get("dispatch_confirmed") is False


# the confirmation turn shows the model what it submitted

def test_confirmation_block_shows_the_standing_requirements():
    block = intake._confirmation_block(_SUBMITTED)
    assert "snappy" in block and "external_cfd" in block and "2D" in block
    assert "airfoil(wall)" in block
    assert "Far-field 20 chords" in block
    assert "submit_requirements" in block and "propose_engine_selection" in block
    assert "THIS TURN DID NOT DISPATCH" in block   # the model cannot start a mesh
    assert "never claim it has started or will" in block


def test_confirmation_turn_never_force_submits():
    many = [{"role": "assistant", "content": f"q{i}"} for i in range(13)]
    out = _run({**_SUBMITTED, "awaiting_confirmation": True, "messages": many},
               [_resp(content="Shall I proceed?")])
    ev = out.get("_intake_training_event", {})
    assert ev.get("payload", {}).get("max_turns_reached") is False


# the tool loop no longer treats unknown tools as web_search

def test_unknown_tool_is_not_executed_as_a_web_search():
    calls = []
    with patch.object(intake, "_execute_intake_tool",
                      lambda *a, **k: calls.append(a) or "searched"):
        out = _run({"job_id": "j", "messages": [{"role": "user", "content": "hi"}]}, [
            _resp([_tool_call("delete_everything", "{}")]),
            _resp(content="I don't have that tool."),
        ])
    assert calls == [], "an unrecognised tool name fell through to web_search"
    assert out.get("dispatch_confirmed") is False


# a confirmation-turn resubmit that changes nothing must not loop

_REQ = (
    "External aerodynamics on a NACA 0012 section. The geometry is a body surface that "
    "needs a surrounding far-field fluid domain built around it. Freestream Mach 0.15 and "
    "Reynolds number 6 million, k-omega SST turbulence with wall functions. Target a "
    "wall-function y+ between 30 and 300 with several prism layers on the airfoil wall. "
    "Resolve the leading edge and the sharp trailing edge, and keep reasonable wake "
    "resolution. The far-field sits about 20 chords from the body in every direction. "
    "Patches are a wall named airfoil and a single farfield patch."
)
_BRIEF = (
    "The mesh must capture the airfoil profile without staircasing or excessive faceting. "
    "The wall patch named airfoil must be present and correctly assigned. A single farfield "
    "patch must enclose the domain at roughly 20 chords from the body. Prism layers must "
    "cover the airfoil wall well enough to support a y+ in the 30 to 300 band. Cell quality "
    "must satisfy the standard non-orthogonality and skewness limits for this solver."
)

_SUBMIT_ARGS = {
    "domain": "NACA 0012 external aero",
    "request_txt": _REQ,
    "review_brief_txt": _BRIEF,
    "patches": [{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
    "dimensionality": "3D", "purpose": "external_cfd", "input_kind": "body-surface",
    "mesh_engine": "snappy", "mesh_fidelity": "standard", "engine_source": "user_direct",
    "engine_params": {},
}

# the standing run, as node_intake sees it on the confirmation turn
_STANDING = {
    "job_id": "j", "session_id": "s", "user_id": "u",
    "messages": [
        {"role": "user", "content": "external aero on a NACA 0012, snappy, 20 chords"},
        {"role": "assistant", "content": "Shall I proceed with mesh generation?"},
        {"role": "user", "content": "yes"},
    ],
    "request_txt": _REQ, "review_brief_txt": _BRIEF,
    "engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
    "dimensionality": "3D", "engine_params": {},
    "intake_patches": [{"name": "airfoil", "type": "wall"},
                       {"name": "farfield", "type": "farfield"}],
}



def test_confirmation_turn_resubmit_with_a_stale_token_is_rejected_no_loop():
    st = {**_STANDING, "awaiting_confirmation": True}
    # a token issued for the PRIOR revision (before the user's "yes" was appended)
    _sel = es.select_from_structured_input("cfmesh", session_id="s", owner_id="u",
                                           revision=at.revision_of(st.get("messages", [])))
    prior = at.issue(session_id="s", owner_id="u", selection_id=_sel["id"],
                     revision=at.revision_of(_STANDING["messages"][:-1]),
                     canonical=at.canonical_payload(_SUBMIT_ARGS["mesh_engine"], _SUBMIT_ARGS["purpose"],
                        _SUBMIT_ARGS["input_kind"], _SUBMIT_ARGS["dimensionality"],
                        _SUBMIT_ARGS["patches"], _SUBMIT_ARGS["engine_params"]),
                     verdict="supported", mode=at.SELECTED)
    st["intake_gate"] = {"selection": _sel, "admission": prior}
    captured: list = []
    import meshpipeline.adapters.model_inference.router as llm_router
    responses = iter([
        _resp([_tool_call("submit_requirements",
                          json.dumps({**_SUBMIT_ARGS, "preview_token": prior["token"]}))]),
        _resp(content="Let me re-check the current requirements."),
    ])

    async def _call(**kw):
        captured.append(kw["messages"]); return next(responses)
    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(st))

    tool_msgs = [m for m in captured[-1] if m.get("role") == "tool"]
    assert any("NOT authorized" in m["content"] for m in tool_msgs), "stale-revision re-submit must be refused"
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_a_changed_resubmit_is_not_nudged():
    import json as _json
    captured: list = []
    changed = {**_SUBMIT_ARGS, "request_txt": "NACA 0012 external aero. Far-field 50 chords."}

    import meshpipeline.adapters.model_inference.router as llm_router
    responses = iter([
        _resp([_tool_call("submit_requirements", _json.dumps(changed))]),
        _resp(content="Updated to 50 chords. Shall I proceed?"),
    ])

    async def _call(**kw):
        captured.append(kw["messages"])
        return next(responses)

    with patch.object(llm_router, "call_intake_model", _call):
        asyncio.run(intake.node_intake({**_STANDING, "awaiting_confirmation": True}))

    tool_msgs = [m for m in captured[-1] if m.get("role") == "tool"]
    assert "call confirm_dispatch now" not in tool_msgs[-1]["content"]


def test_a_resubmit_outside_the_confirmation_turn_is_never_nudged():
    import json as _json
    captured: list = []
    import meshpipeline.adapters.model_inference.router as llm_router
    responses = iter([
        _resp([_tool_call("submit_requirements", _json.dumps(_SUBMIT_ARGS))]),
        _resp(content="Shall I proceed?"),
    ])

    async def _call(**kw):
        captured.append(kw["messages"])
        return next(responses)

    state = {k: v for k, v in _STANDING.items() if k != "request_txt"}
    with patch.object(llm_router, "call_intake_model", _call):
        asyncio.run(intake.node_intake(state))

    tool_msgs = [m for m in captured[-1] if m.get("role") == "tool"]
    assert "call confirm_dispatch now" not in tool_msgs[-1]["content"]
