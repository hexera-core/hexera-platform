# Responsibility: Verify an engine is proposed, confirmed, invalidated and expired on its own; only confirmed admits.
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es

_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
            {"name": "wall", "type": "wall"}]
_DECL = {"purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _PATCHES, "engine_params": {"element_order": "2"}}
_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion. " * 2)


def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


_SEEN: list = []


def _run(state, responses):
    import meshpipeline.contracts.model_inference as llm
    it = iter(responses)
    _SEEN.clear()

    async def _call(**kw):
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        return next(it)
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


def _state(msg, gate=None):
    s = {"job_id": "j", "session_id": "s", "user_id": "u",
         "messages": [{"role": "user", "content": msg}]}
    if gate is not None:
        s["intake_gate"] = gate
    return s


# the lifecycle unit

def test_lifecycle_states_and_expiry():
    assert es.state_of(None) == es.NO_SELECTION
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    assert es.state_of(p) == es.PROPOSED
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="yes, select gmsh", latest_user_message="Yes, select gmsh.")
    assert why == "" and es.state_of(c) == es.CONFIRMED
    assert es.state_of({**c, "expires_at": time.time() - 1}) == es.NO_SELECTION


def test_confirmation_requires_words_the_user_actually_wrote():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                        quote="yes use gmsh", latest_user_message="Actually, what about cfmesh?")
    assert c is None and "not in the user's latest message" in why


def test_a_new_user_revision_leaves_a_pending_selection_stale():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    c, why = es.confirm(p, session_id="s", owner_id="u", revision="r4", user_msg_count=3,
                        quote="yes", latest_user_message="yes")
    assert c is None and "stale" in why


def test_only_a_confirmed_selection_authorizes_admission():
    p = es.propose("gmsh", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    assert es.verify_confirmed(None, "gmsh", session_id="s", owner_id="u")[0] is False
    assert es.verify_confirmed(p, "gmsh", session_id="s", owner_id="u")[0] is False   # proposed only
    c, _ = es.confirm(p, session_id="s", owner_id="u", revision="r2", user_msg_count=2,
                      quote="yes", latest_user_message="yes")
    assert es.verify_confirmed(c, "gmsh", session_id="s", owner_id="u")[0] is True
    assert es.verify_confirmed(c, "cfmesh", session_id="s", owner_id="u")[0] is False  # other engine
    assert es.verify_confirmed(c, "gmsh", session_id="other", owner_id="u")[0] is False


# the explicit selection flow, end to end

def test_naming_an_engine_only_proposes_it_and_renders_the_canonical_statement():
    state = _state("Use gmsh.")
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
                       _resp(content="never reached - the app asks for confirmation")])
    reply = out["messages"][-1]["content"]
    # the statement shows the name a user reads; `gmsh` is the internal key
    assert "Selected engine: Gmsh" in reply
    assert "not a selection" in reply and "nothing will be meshed" in reply
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.PROPOSED and gate["selection"]["engine"] == "gmsh"
    assert gate["admission"] is None
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_confirming_establishes_selection_and_unlocks_a_fresh_admission_preview():
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision="r-earlier",
                          user_msg_count=0)   # the state below carries exactly one user message
    state = _state("Yes, select gmsh.", gate={"selection": proposed, "admission": None})
    out = _run(state, [
        _resp([_tc("confirm_engine_selection", json.dumps({"user_agreed_verbatim": "Yes, select gmsh."}))]),
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
        _resp(content="Admission is fine - now, what are the flow conditions?")])
    gate = out["intake_gate"]
    assert gate["selection"]["state"] == es.CONFIRMED
    assert gate["admission"] and gate["admission"]["verdict"] == "supported"
    assert gate["admission"]["selection_id"] == gate["selection"]["id"]
    # a supported preview still does NOT submit or dispatch anything
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_requirements_still_need_canonical_approval_after_a_confirmed_selection():
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u",
                                          revision=at.revision_of([{"role": "user", "content": "go on"}]))
    state = _state("go on", gate={"selection": sel, "admission": None})

    def _submit_with_token():
        tok = json.loads(_SEEN[-1]["content"])["preview_token"]
        return _resp([_tc("submit_requirements", json.dumps({
            "domain": "duct internal", "request_txt": _R, "review_brief_txt": _B,
            "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
            "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
            "engine_params": {"element_order": "2"}, "patches": _PATCHES, "preview_token": tok}))])

    import meshpipeline.contracts.model_inference as llm
    seq = [_resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
           _submit_with_token, _resp(content="unreached")]
    it = iter(seq)
    _SEEN.clear()

    async def _call(**kw):
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        nxt = next(it)
        return nxt() if callable(nxt) else nxt
    with patch.object(llm, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))

    reply = out["messages"][-1]["content"]
    assert out.get("request_txt"), "the submission was authorized"
    assert "confirm the requirements" in reply and "Shall I proceed" in reply   # app-rendered
    assert out.get("dispatch_confirmed") is False, "approval is a separate user turn"


def test_admission_is_refused_without_a_confirmed_selection():
    for gate in (None,
                 {"selection": es.propose("gmsh", session_id="s", owner_id="u", revision="r1",
                                          user_msg_count=1),
                  "admission": None}):
        state = _state("My duct has three patches.", gate=gate)
        out = _run(state, [
            _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
            _resp(content="Which engine would you like to use?")])
        assert (out["intake_gate"].get("admission")) is None, "no token without a confirmed selection"
        assert "Cannot check admission" in _SEEN[-1]["content"]


def test_admission_is_refused_for_an_engine_other_than_the_confirmed_one():
    sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    state = _state("go on", gate={"selection": sel, "admission": None})
    out = _run(state, [
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "cfmesh", **_DECL}))]),
        _resp(content="I need you to confirm the engine change first.")])
    assert out["intake_gate"].get("admission") is None
    assert "the confirmed engine is 'gmsh'" in _SEEN[-1]["content"]


# correction invalidates selection, preview and pending confirmation together

def test_an_engine_correction_invalidates_the_previous_selection_and_its_admission():
    old_sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    old_tok = at.issue(session_id="s", owner_id="u", revision="r1",
                       canonical=at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                                      _PATCHES, {"element_order": "2"}),
                       verdict="supported", mode=at.SELECTED, selection_id=old_sel["id"])
    state = _state("Actually, switch the engine to cfmesh.",
                   gate={"selection": old_sel, "admission": old_tok})
    out = _run(state, [_resp([_tc("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
                       _resp(content="unreached")])
    gate = out["intake_gate"]
    assert gate["selection"]["engine"] == "cfmesh" and gate["selection"]["state"] == es.PROPOSED
    assert gate["selection"]["id"] != old_sel["id"]
    assert gate["admission"] is None, "the correction must void the previous admission preview"
    assert "Selected engine: cfMesh" in out["messages"][-1]["content"]
    assert out.get("dispatch_confirmed") is False


def test_a_stale_token_from_the_previous_selection_cannot_dispatch():
    old_sel = es.select_from_structured_input("gmsh", session_id="s", owner_id="u", revision="r1")
    tok = at.issue(session_id="s", owner_id="u", revision="r1",
                   canonical=at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                                  _PATCHES, {"element_order": "2"}),
                   verdict="supported", mode=at.SELECTED, selection_id=old_sel["id"])
    new_sel = es.select_from_structured_input("cfmesh", session_id="s", owner_id="u", revision="r2")
    ok, why = at.verify_for_confirm(tok, tok["token"], selection=new_sel)
    assert ok is False and "selection changed" in why
