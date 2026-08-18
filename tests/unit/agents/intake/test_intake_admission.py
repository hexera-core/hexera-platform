# Responsibility: Verify an impossible engine and purpose pairing ends the turn, names no substitute, writes nothing.
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


# node_intake: impossible preview is TERMINAL (sections 3 + 9)

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


def _run(state, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    calls = {"n": 0}
    it = iter(responses)

    async def _call(**_kw):
        calls["n"] += 1
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


def test_impossible_preview_terminates_the_turn_and_discards_an_unsafe_paraphrase():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "snappy, external CFD, body "
             "surface, separate wall patches fuselage/wing/htail/nacelles/pylons + farfield"}]}
    _confirmed(state, "snappy")
    out, ncalls = _run(state, [
        _resp([_tool_call("preview_selected_admission", _FIVE_ARGS)]),
        _resp(content="I should never be reached - you can also try cfmesh."),
    ])
    # The second call is the refusal paraphrase: it carries no tool, so it can advance nothing, and
    # its output reaches the user only by passing the checks. This response names another engine -
    # the precise leak the terminal used to prevent by never asking - so it must be discarded and
    # the rendered message delivered in its place.
    assert ncalls == 2, "the refusal was not put back into the user's terms"
    reply = out["messages"][-1]["content"]
    assert "cfmesh" not in reply, f"an unsafe paraphrase reached the user: {reply}"
    assert "wall patch" in reply and _no_other_engine(reply, "snappy"), reply   # app message, no alt
    assert out.get("dispatch_confirmed") is False
    for f in ("mesh_engine", "purpose", "intake_patches", "request_txt"):
        assert not out.get(f), f


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
