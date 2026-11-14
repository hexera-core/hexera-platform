# Responsibility: Verify an engine the APPLICATION chose follows the case (review on #465): once the user asks for
# another analysis of the same part, or corrects what the file is, the automatic choice is made again; an engine
# the USER typed stays theirs unless it cannot mesh the new analysis at all - then that is said plainly and an
# engine that can is used. The model's second thoughts on an unchanged case still change nothing.
# Boundaries: one intake turn end to end with the model's rounds scripted and the session's stored gate given
# (test_engine_never_asked_twice's harness), the real admission gate behind it.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake.executor import IntakeToolExecutor
from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK


def _tool_call(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


CFD_STAGE = {"role": "assistant", "content": f"{CONFIRMED_MARK} the file is the bracket's surface, input_kind "
                                             "body-surface; the fluid flows around it."}
STRESS = "Now I need a static stress analysis of the same part."


def _auto(engine: str, *, via: str = es.VIA_AUTO, **extra) -> dict:
    return {"id": "sel1", "engine": engine, "state": es.CONFIRMED, "via": via, "session": "s", "owner": "u",
            "proposed_revision": "r0", "confirmed_revision": "r0", "expires_at": es.time.time() + 3000, **extra}


def _run_with_gate(messages, selection, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    seen: list = []
    it = iter(responses)

    async def _call(**kw):
        seen[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict) and m.get("role") == "tool"]
        return next(it)
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": messages,
             "intake_gate": {"selection": selection}}
    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))
    return out, seen


@pytest.fixture(autouse=True)
def _no_file(monkeypatch):
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_form", lambda self: "cad")
    monkeypatch.setattr(IntakeToolExecutor, "_geometry_facts", lambda self: {})


def _state(convo):
    from meshpipeline.agents.intake.executor import IntakeExecutionState
    from meshpipeline.application.geometry_confirmation import declared_case
    return IntakeExecutionState(declared_case=declared_case(convo), conversation=tuple(convo),
                                user_texts=tuple(m["content"] for m in convo if m["role"] == "user"))


def _fea_stage() -> dict:
    # the FEA stage's own sentence, as application/geometry_confirmation writes it on Proceed
    from meshpipeline.application import geometry_confirmation as gcf
    body = SimpleNamespace(check_mode="fea", part="", bodies=1, clean_summary="The geometry is clean.",
                           size_mm=[200.0, 100.0, 5.0], unit="mm", unit_fixed=False)
    msg = gcf.confirmation_message(body)
    assert gcf.declared_case([{"role": "assistant", "content": msg}]) == ("structural", "solid-body")
    return {"role": "assistant", "content": msg}


@pytest.mark.parametrize("said,purpose", [
    (STRESS, "structural"), ("The part's strength, stress or vibration (FEA)", "structural"),
    ("now a modal analysis please", "structural"),
    # words that name no analysis leave the stage's flow standing - however FEA-ish they sound to the router
    ("refine the pipe bend", "external_cfd"), ("it broke last time near the outlet", "external_cfd"),
    ("resolve the wall shear stress", "external_cfd"), ("5 prism layers, 3 mm", "external_cfd")])
def test_the_latest_named_analysis_is_the_purpose_now(said, purpose):
    from meshpipeline.agents.intake.executor import current_purpose, fea_path
    st = _state([CFD_STAGE, {"role": "user", "content": said}])
    assert current_purpose(st) == purpose
    assert fea_path(st) is (purpose == "structural")


def test_a_flow_named_after_the_fea_stage_leaves_the_fea_path_and_a_load_does_not():
    from meshpipeline.agents.intake.executor import current_purpose, fea_path
    stage = _fea_stage()
    for said, purpose in (("actually run CFD on it - air flows around it", "external_cfd"),
                          ("it sits in water", "structural"), ("the stress from the water flow", "structural"),
                          ("the water flow loads the wall", "structural")):
        st = _state([stage, {"role": "user", "content": said}])
        assert current_purpose(st) == purpose, said
        assert fea_path(st) is (purpose == "structural"), said


@pytest.mark.parametrize("stored", [
    {"for_case": ["external_cfd", "body-surface"], "for_purpose": "external_cfd"},
    {},                                                   # a selection stored before the case was kept with it
])
def test_a_new_analysis_chooses_the_engine_again(stored):
    # snappyHexMesh was chosen by the application for the flow; the user asks for a stress analysis and names no
    # engine. Before: "Not changed: snappyHexMesh is the selected engine", and the user was stuck
    out, seen = _run_with_gate([CFD_STAGE, {"role": "user", "content": STRESS}], _auto("snappy", **stored), [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
        _resp(content="Gmsh will mesh the part for the stress analysis."),
    ])
    assert "Not changed" not in seen[-1]["content"], seen[-1]["content"]
    sel = out["intake_gate"]["selection"]
    assert sel["engine"] == "gmsh" and sel["state"] == es.CONFIRMED, sel
    assert sel["via"] == es.VIA_FEA_PATH and sel["for_purpose"] == "structural", sel


def test_a_refusal_of_the_old_engine_for_the_new_analysis_is_no_dead_end():
    # the model first checks the held engine against the new analysis, is refused, then proposes one that can
    out, seen = _run_with_gate([CFD_STAGE, {"role": "user", "content": STRESS}],
                               _auto("snappy", for_case=["external_cfd", "body-surface"], for_purpose="external_cfd"), [
        _resp([_tool_call("preview_selected_admission", json.dumps({
            "selected_engine": "snappy", "purpose": "structural", "input_kind": "solid-body",
            "dimensionality": "3D", "patches": []}))]),
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
        _resp(content="Gmsh will mesh the part for the stress analysis."),
    ])
    assert json.loads(seen[0]["content"].split("\n\n")[-1])["verdict"] == "impossible"
    assert "Not allowed" not in seen[1]["content"], seen[1]["content"]
    assert out["intake_gate"]["selection"]["engine"] == "gmsh"


def test_the_models_second_thoughts_on_the_same_case_change_nothing():
    out, seen = _run_with_gate([CFD_STAGE, {"role": "user", "content": "5 prism layers on the bracket please"}],
                               _auto("snappy", for_case=["external_cfd", "body-surface"], for_purpose="external_cfd"), [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
        _resp(content="Five layers it is."),
    ])
    assert "Not changed: snappyHexMesh is the selected engine" in seen[-1]["content"], seen[-1]["content"]
    assert out["intake_gate"]["selection"]["engine"] == "snappy"


def test_an_engine_the_user_typed_that_can_do_the_new_analysis_stays_theirs():
    # the user typed Gmsh for the flow; it meshes a part for stress too, so it stays - whatever the model proposes
    convo = [{"role": "user", "content": "use Gmsh"}, CFD_STAGE, {"role": "user", "content": STRESS}]
    out, _seen = _run_with_gate(convo, _auto("gmsh", via=es.VIA_CONVERSATION,
                                             for_case=["external_cfd", "body-surface"], for_purpose="external_cfd"), [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "netgen"}))]),
        _resp(content="Gmsh it stays."),
    ])
    assert out["intake_gate"]["selection"]["engine"] == "gmsh"


def test_an_engine_the_user_typed_that_cannot_do_the_new_analysis_is_said_plainly_and_replaced():
    # the user typed cfMesh for the flow; cfMesh cannot mesh a part for stress at all
    convo = [{"role": "user", "content": "use cfMesh"}, CFD_STAGE, {"role": "user", "content": STRESS}]
    out, seen = _run_with_gate(convo, _auto("cfmesh", via=es.VIA_CONVERSATION,
                                            for_case=["external_cfd", "body-surface"], for_purpose="external_cfd"), [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
        _resp(content="cfMesh cannot mesh the part for a stress analysis, so Gmsh is used."),
    ])
    said = seen[-1]["content"]
    assert "cfMesh, the engine the user chose earlier, cannot mesh this part for" in said, said
    assert "Gmsh is used. Tell the user that in one plain sentence." in said, said
    assert out["intake_gate"]["selection"]["engine"] == "gmsh"
    # ...and when the model proposes the user's own engine again, the one that can is used all the same
    out, seen = _run_with_gate(convo, _auto("cfmesh", via=es.VIA_CONVERSATION), [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="Gmsh is used."),
    ])
    assert out["intake_gate"]["selection"]["engine"] == "gmsh", seen[-1]["content"]


def test_a_corrected_file_kind_chooses_the_engine_again():
    # KIND-1 with the choice following the case: snappyHexMesh was chosen for the car's surface; the user says the
    # file is the air already (in words no list of ours holds); the model checks with their words, snappyHexMesh
    # cannot mesh an air volume for an external flow, and the engine is chosen again rather than held
    stage = {"role": "assistant", "content": f"{CONFIRMED_MARK} the file is the car's surface, input_kind "
                                             "body-surface; the fluid flows around it."}
    said = ("It came out of our wind tunnel rig already - what you got is what the car sits in, so just mesh the "
            "inside of it.")
    patches = [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
    out, seen = _run_with_gate([stage, {"role": "user", "content": said}],
                               _auto("snappy", for_case=["external_cfd", "body-surface"], for_purpose="external_cfd"), [
        _resp([_tool_call("preview_selected_admission", json.dumps({
            "selected_engine": "snappy", "purpose": "external_cfd", "input_kind": "fluid-domain",
            "input_kind_user_words": "what you got is what the car sits in, so just mesh the inside of it",
            "dimensionality": "3D", "patches": patches}))]),
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "gmsh"}))]),
        _resp(content="Gmsh meshes the air volume as it is."),
    ])
    assert "is kept: the user corrected the geometry stage" in seen[0]["content"], seen[0]["content"]
    assert json.loads(seen[0]["content"].split("\n\n")[-1])["verdict"] == "impossible"
    sel = out["intake_gate"]["selection"]
    assert sel["engine"] == "gmsh" and sel["state"] == es.CONFIRMED, (sel, seen[-1]["content"])
    assert sel["for_case"] == ["external_cfd", "fluid-domain"], sel


def test_naming_the_engine_again_now_still_sends_it_to_the_admission():
    # their word in THIS message is theirs, as always: the admission then says why it cannot
    convo = [CFD_STAGE, {"role": "user", "content": "static stress analysis now, use cfMesh"}]
    out, _seen = _run_with_gate(convo, _auto("snappy", for_case=["external_cfd", "body-surface"],
                                             for_purpose="external_cfd"), [
        _resp([_tool_call("propose_engine_selection", json.dumps(
            {"engine": "cfmesh", "user_named_verbatim": "use cfMesh"}))]),
        _resp(content="Checking cfMesh."),
    ])
    assert out["intake_gate"]["selection"]["engine"] == "cfmesh"
