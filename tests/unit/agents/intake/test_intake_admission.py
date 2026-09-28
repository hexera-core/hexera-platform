# Responsibility: Verify an impossible pairing goes back to the model to repair or put to the user, names no substitute, writes nothing.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake.validation import (
    ADMIT_IMPOSSIBLE,
    ADMIT_MALFORMED,
    ADMIT_SUPPORTED,
    detect_semantic_loss,
    preview_admission,
)
from meshpipeline.agents.intake.vocabulary import engines_named_in
from meshpipeline.engines.purposes import INPUT_KINDS, purpose_keys
from meshpipeline.engines.registry import engine_names

_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion for this case. " * 2)
_FIVE = [{"name": n, "type": "wall"} for n in
         ("fuselage", "wing", "horizontal_tail", "nacelles", "pylons")] + [{"name": "farfield", "type": "farfield"}]


def _no_other_engine(text: str, selected: str) -> bool:
    # The prefix rule this used to apply treated snappy and snappy_multiregion as one engine. They
    # are not: only one meshes external CFD from a body surface, and only the other does conjugate
    # heat transfer. The shared scanner matches the longest spelling first, so an engine whose name
    # contains another's is told apart from it.
    return not engines_named_in(text, selected)


# preview covers every declared-phase rejection category; impossible preserves intent

def test_multiple_wall_patches_is_impossible_and_preserves_all_five():
    # snappy CAN deliver several named wall patches now, so the blocker is the geometry: a body
    # with one region cannot supply five names, whatever the engine is capable of.
    r = preview_admission("snappy", "external_cfd", "body-surface", dimensionality="3D",
                          patches=_FIVE, geometry_facts={"region_count": 1,
                                                         "region_names": ["body"]})
    assert r["verdict"] == ADMIT_IMPOSSIBLE
    assert r["blocking_rule_code"] == "multiple_wall_patches_unsupported"
    kept = {p["name"] for p in r["preserved_declared_values"]["patches"]}
    assert {"fuselage", "wing", "horizontal_tail", "nacelles", "pylons"} <= kept   # nothing dropped
    assert _no_other_engine(r["safe_user_message"], "snappy"), r["safe_user_message"]
    assert "revise" in r["safe_user_message"]


def test_capability_dimensionality_symmetry_malformed_categories():
    assert preview_admission("snappy", "structural", "body-surface")["verdict"] == ADMIT_IMPOSSIBLE
    assert preview_admission("gmsh", "external_cfd", "solid-body",
                             engine_params={"element_order": "2"})["verdict"] == ADMIT_IMPOSSIBLE
    assert preview_admission("snappy", "external_cfd", "body-surface",
                             dimensionality="2D")["verdict"] == ADMIT_IMPOSSIBLE   # snappy has no 2D
    assert preview_admission("nope", "structural", "body-surface")["verdict"] == ADMIT_MALFORMED
    assert preview_admission("gmsh", "external_cfd", "fluid-domain",
                             engine_params={"element_order": "2"})["verdict"] == ADMIT_SUPPORTED


def test_every_impossible_combination_names_no_other_engine_registry_derived():
    seen = 0
    for eng in engine_names():
        for pur in purpose_keys():
            for ik in INPUT_KINDS:
                r = preview_admission(eng, pur, ik, patches=_FIVE, dimensionality="3D")
                if r["verdict"] != ADMIT_IMPOSSIBLE:
                    continue
                seen += 1
                assert r["selected_engine"] == eng
                assert _no_other_engine(r["safe_user_message"], eng), (eng, pur, ik, r["safe_user_message"])
    assert seen > 0


# detect_semantic_loss: the five→one collapse and each protected field

def test_semantic_loss_detects_patch_collapse_and_protected_field_changes():
    declared = {"engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
                "dimensionality": "3D", "patches": _FIVE}
    collapsed = {"engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
                 "dimensionality": "3D", "patches": [{"name": "aircraft", "type": "wall"},
                                                     {"name": "farfield", "type": "farfield"}]}
    loss = detect_semantic_loss(declared, collapsed)
    assert any("patch count dropped" in x for x in loss)
    assert sum("dropped or merged" in x for x in loss) == 5
    # protected fields
    assert detect_semantic_loss({"engine": "snappy"}, {"engine": "gmsh"})
    assert detect_semantic_loss({"dimensionality": "3D"}, {"dimensionality": "2D"})
    assert detect_semantic_loss({"purpose": "external_cfd"}, {"purpose": "internal_cfd"})
    # reorder-only is NOT a loss
    assert detect_semantic_loss(declared, {**declared, "patches": list(reversed(_FIVE))}) == []


# node_intake: an impossible preview is a RESULT the model acts on, never a dead end (sections 3 + 9)

def _tool_call(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


_SEEN: list = []          # the tool results the model was shown, most recent last


def _run(state, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    calls = {"n": 0}
    it = iter(responses)
    _SEEN.clear()

    async def _call(**kw):
        calls["n"] += 1
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        return next(it)
    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))
    return out, calls["n"]


_FIVE_ARGS = json.dumps({"selected_engine": "snappy", "purpose": "external_cfd",
                         "input_kind": "body-surface", "dimensionality": "3D", "patches": _FIVE})


def _confirmed(state, engine):
    sel = es.select_from_structured_input(
        engine, session_id=str(state.get("session_id", "")), owner_id=str(state.get("user_id", "")),
        revision=at.revision_of(state["messages"]))
    state["intake_gate"] = {"selection": sel, "admission": None}
    return sel
_SUBMIT_COLLAPSED = json.dumps({
    "domain": "crm aero", "request_txt": _R, "review_brief_txt": _B, "dimensionality": "3D",
    "purpose": "external_cfd", "input_kind": "body-surface", "mesh_engine": "snappy",
    "mesh_fidelity": "standard", "engine_source": "user_direct", "engine_params": {},
    "patches": [{"name": "aircraft", "type": "wall"}, {"name": "farfield", "type": "farfield"}]})


_ONE_WALL_ARGS = json.dumps({
    "selected_engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
    "dimensionality": "3D", "patches": [{"name": "aircraft", "type": "wall"},
                                        {"name": "farfield", "type": "farfield"}]})


def _five_walls_state(said="snappy, external CFD, body surface, separate wall patches "
                           "fuselage/wing/htail/nacelles/pylons + farfield"):
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": said}]}
    _confirmed(state, "snappy")
    return state


def test_an_impossible_preview_is_a_result_the_model_receives_with_what_would_pass():
    # The old flow ended the turn on the application's sentence and left the user no move. Now
    # the finding, and what would satisfy the gate, come back as a tool result the model acts on.
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="Your file is one unnamed body, so snappyHexMesh can write one wall patch "
                      "for it, not five. I would call it 'aircraft' and keep the farfield - ok, "
                      "or tell me which part you want to revise?"),
    ])
    assert ncalls == 2, "the model was not given the finding to act on"
    result = json.loads(_SEEN[-1]["content"])
    assert result["verdict"] == "impossible" and result["recorded"] is False
    assert "multiple_wall_patches_unsupported" in result["blocking_rule_codes"]
    assert "one region" in result["finding"].lower()
    assert result["what_would_pass"] and "single wall patch" in result["what_would_pass"][0]
    assert "repair" in result["guidance"] and "propose" in result["guidance"]
    assert _no_other_engine(result["guidance"], "snappy")
    # the model's own reply - a proposal the user can answer with "ok" - is what the user reads
    reply = out["messages"][-1]["content"]
    assert reply.startswith("Your file is one unnamed body") and "ok" in reply
    assert out["intake_gate"]["admission"] is None, "nothing was authorized"
    assert out.get("dispatch_confirmed") is False
    for f in ("mesh_engine", "purpose", "intake_patches", "request_txt"):
        assert not out.get(f), f


def test_the_model_repairs_its_own_value_in_the_same_turn():
    # The user named ONE wall (the aircraft); the model had added four. With the finding in hand
    # it previews again with what the user actually declared - supported, token issued, and the
    # conversation simply continues. No dead end, no question the user did not need.
    out, ncalls = _run(_five_walls_state("snappy, external CFD, body surface: the aircraft is "
                                         "the wall, plus a farfield"), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp([_tool_call("preview_selected_admission", _ONE_WALL_ARGS)]),
        _resp(content="I kept the aircraft as the one wall patch. Fluid and speed? I would go "
                      "with air at 10 m/s - ok?"),
    ])
    assert ncalls == 3
    assert json.loads(_SEEN[-1]["content"])["verdict"] == "supported"
    assert out["intake_gate"]["admission"]["verdict"] == "supported"
    assert out["messages"][-1]["content"].startswith("I kept the aircraft")


def test_a_retry_the_gate_never_checked_keeps_the_refusal_standing():
    # The second preview is malformed (a purpose that is not one), so the gate never ran on it
    # and nothing was resolved. The reply is still held to the refusal: this one names another
    # engine, and the rendered finding goes out instead.
    junk = json.dumps({**json.loads(_FIVE_ARGS), "purpose": "not-a-purpose"})
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp([_tool_call("preview_selected_admission", junk)]),
        _resp(content="snappyHexMesh cannot do this - cfmesh could, though."),
    ])
    assert ncalls == 3
    assert json.loads(_SEEN[-1]["content"])["verdict"] == "malformed"
    reply = out["messages"][-1]["content"]
    assert "cfmesh" not in reply and "wall patch" in reply and "revise" in reply


def test_a_retry_the_gate_passed_with_gaps_left_clears_the_refusal():
    # One wall and no flow boundary yet: the gate ran, refused nothing, and asked for the missing
    # patch. The refusal is resolved, so the model's next question goes out as written - here a
    # statement that the refusal check would otherwise have replaced.
    one_wall_no_farfield = json.dumps({**json.loads(_ONE_WALL_ARGS),
                                       "patches": [{"name": "aircraft", "type": "wall"}]})
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp([_tool_call("preview_selected_admission", one_wall_no_farfield)]),
        _resp(content="Noted - I will take a single farfield as the flow boundary."),
    ])
    assert ncalls == 3
    seen = json.loads(_SEEN[-1]["content"])
    assert seen["verdict"] == "incomplete" and seen["missing_fields"] == ["patches"]
    assert out["messages"][-1]["content"].startswith("Noted - I will take a single farfield")


def test_an_unsafe_reply_after_an_unrepaired_refusal_is_discarded_for_the_rendered_finding():
    # The model's reply names another engine - the precise leak the old terminal prevented by
    # never asking. It is discarded and the rendered finding delivered in its place.
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="snappyHexMesh cannot do this - you can also try cfmesh."),
    ])
    assert ncalls == 2
    reply = out["messages"][-1]["content"]
    assert "cfmesh" not in reply, f"an unsafe reply reached the user: {reply}"
    assert "wall patch" in reply and _no_other_engine(reply, "snappy"), reply   # app message, no alt
    assert "revise" in reply
    assert out.get("dispatch_confirmed") is False
    for f in ("mesh_engine", "purpose", "intake_patches", "request_txt"):
        assert not out.get(f), f


def test_a_reply_that_asks_nothing_after_a_refusal_is_discarded_too():
    out, _ = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="That cannot be meshed. Nothing was changed."),
    ])
    reply = out["messages"][-1]["content"]
    assert "revise" in reply and _no_other_engine(reply, "snappy")


def test_running_out_of_rounds_after_a_refusal_still_delivers_the_finding(monkeypatch):
    # A model that keeps re-checking the same refused payload never writes a reply. The user still
    # gets the finding and the question, not silence.
    monkeypatch.setattr(intake.icfg, "INTAKE_MAX_ROUNDS", 2)
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="unreached"),
    ])
    assert ncalls == 2
    reply = out["messages"][-1]["content"]
    assert "wall patch" in reply and "revise" in reply and _no_other_engine(reply, "snappy")


def test_after_a_refusal_the_model_may_not_switch_engines_for_the_user():
    # With the finding in hand the model proposes another engine the user never named. That would
    # put "Selected engine: cfMesh" in front of a user who asked for snappyHexMesh, in the turn
    # that refused their setup. Refused; the confirmed selection stands; no engine is named.
    out, ncalls = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS),
               _tool_call("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
        _resp(content="Only one wall patch can be written for this body. Shall I declare the "
                      "aircraft as that single wall patch and keep the farfield?"),
    ])
    assert ncalls == 2
    assert "Not allowed" in _SEEN[-1]["content"] and "cfMesh" in _SEEN[-1]["content"]
    gate = out["intake_gate"]
    assert gate["selection"]["engine"] == "snappy" and gate["selection"]["state"] == es.CONFIRMED
    reply = out["messages"][-1]["content"]
    assert "Selected engine" not in reply and _no_other_engine(reply, "snappy")
    assert reply.startswith("Only one wall patch")


def test_after_a_refusal_an_engine_the_user_named_themselves_still_goes_forward():
    out, _ = _run(_five_walls_state("switch to cfmesh then, and keep my five wall patches"), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS),
               _tool_call("propose_engine_selection",
                          json.dumps({"engine": "cfmesh", "user_named_verbatim": "switch to cfmesh"}))]),
        _resp(content="cfMesh it is. Checking your five wall patches against it next."),
    ])
    assert "SELECTED" in _SEEN[-1]["content"]
    gate = out["intake_gate"]
    assert gate["selection"]["engine"] == "cfmesh" and gate["selection"]["state"] == es.CONFIRMED
    # the refusal was about the engine the user just left: the reply about theirs is delivered
    assert out["messages"][-1]["content"].startswith("cfMesh it is")


def test_the_refusal_stays_on_the_public_trace():
    out, _ = _run(_five_walls_state(), [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="One wall patch it must be - shall I call it 'aircraft'?"),
    ])
    said = [str((e.get("payload") or {}).get("conclusion", "")) for e in out["_public_trace"]
            if (e.get("payload") or {}).get("type") == "rationale"]
    assert any("cannot produce this mesh" in s.lower() for s in said), \
        "the refusal left the audit trail"


def test_impossible_preview_then_submit_in_same_response_writes_nothing():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "five wall patches on snappy"}]}
    _confirmed(state, "snappy")
    out, _ = _run(state, [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS),
               _tool_call("submit_requirements", _SUBMIT_COLLAPSED)]),
        _resp(content="unreached"),
    ])
    assert not out.get("request_txt") and not out.get("mesh_engine")
    assert out.get("dispatch_confirmed") is False


_SUPPORTED_SUBMIT = json.dumps({
    "domain": "duct internal", "request_txt": _R, "review_brief_txt": _B, "dimensionality": "3D",
    "purpose": "internal_cfd", "input_kind": "fluid-domain", "mesh_engine": "gmsh",
    "mesh_fidelity": "standard", "engine_source": "user_direct", "engine_params": {"element_order": "2"},
    "patches": [{"name": "in", "type": "inlet"}, {"name": "out", "type": "outlet"},
                {"name": "pipe", "type": "wall"}]})


def test_a_pending_supported_submit_is_voided_by_a_later_impossible_preview_same_turn():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "two different things"}]}
    _confirmed(state, "snappy")
    out, _ = _run(state, [
        _resp([_tool_call("submit_requirements", _SUPPORTED_SUBMIT),
               _tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="unreached"),
    ])
    assert not out.get("request_txt"), "an impossible preview must void a pending submission"
    assert out.get("dispatch_confirmed") is False


def test_submit_that_collapses_declared_patches_is_refused_by_token_mismatch():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "five wall patches on cfmesh"}]}
    # Seed a confirmed cfmesh selection + a valid token for the FIVE-patch cfmesh canonical.
    sel = _confirmed(state, "cfmesh")
    tok = at.issue(session_id="s", owner_id="u", revision=at.revision_of(state["messages"]),
                   canonical=at.canonical_payload("cfmesh", "external_cfd", "body-surface", "3D", _FIVE, {}),
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    state["intake_gate"] = {"selection": sel, "admission": tok}
    submit_collapsed = json.dumps({
        **json.loads(_SUBMIT_COLLAPSED), "mesh_engine": "cfmesh", "preview_token": tok["token"]})
    out, _ = _run(state, [
        _resp([_tool_call("submit_requirements", submit_collapsed)]),
        _resp(content="unreached"),
    ])
    assert not out.get("request_txt"), "a payload differing from the previewed one must not persist"
    assert out.get("dispatch_confirmed") is False
