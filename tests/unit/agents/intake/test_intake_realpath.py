# Responsibility: Verify a confirmation turn rebuilds each standing field from the record, leaking no superseded value.
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.agent as intake
import meshpipeline.api.v1.chat as chat

_REQ = ("External aerodynamics on a NACA 0012 section. The geometry is a body surface needing a "
        "surrounding far-field. Mach 0.15, Reynolds 6 million, k-omega SST with wall functions, "
        "target y+ 30-300 with prism layers on the airfoil. Far-field ~20 chords. Patches: a wall "
        "named airfoil and a single farfield.")
_BRIEF = ("Capture the airfoil profile without staircasing. The wall patch airfoil must be present. "
          "A single farfield encloses the domain at ~20 chords. Prism layers support y+ 30-300.")


def _session(**over):
    base = {
        "id": "00000000-0000-4000-8000-000000000000",
        "messages": [{"role": "user", "content": "external aero NACA0012 snappy 20 chords"},
                     {"role": "assistant", "content": "Shall I proceed with mesh generation?"},
                     {"role": "user", "content": "yes, proceed"}],
        "geometry_source": None, "request_txt": _REQ, "review_brief_txt": _BRIEF,
        "domain": "naca external aero", "mesh_engine": "snappy", "engine_params": {},
        "intake_patches": [{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
        "dimensionality": "3D", "purpose": "external_cfd", "input_kind": "body-surface",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _tool_call(name, args):
    return SimpleNamespace(id="tc1", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


def _run_confirm(session, quote="yes, proceed"):
    import meshpipeline.agents.intake.admission_token as at
    import meshpipeline.agents.intake.engine_selection as es
    state = chat._build_intake_state(session, "owner-A")
    state["awaiting_confirmation"] = True
    # a confirmed engine selection + the supported preview token it backs authorize the confirmation
    sel = es.select_from_structured_input(session.mesh_engine, session_id=str(state.get("session_id", "")),
                                          owner_id="owner-A",
                                          revision=at.revision_of(state.get("messages", [])))
    tok = at.issue(session_id=str(state.get("session_id", "")), owner_id="owner-A",
                   revision=at.revision_of(state.get("messages", [])),
                   canonical={"engine": session.mesh_engine}, verdict="supported", mode=at.SELECTED,
                   selection_id=sel["id"])
    state["intake_gate"] = {"selection": sel, "admission": tok}
    captured = {}
    import meshpipeline.adapters.model_inference.router as llm_router
    responses = iter([_resp([_tool_call("confirm_dispatch",
                       f'{{"user_agreed_verbatim": "{quote}", "preview_token": "{tok["token"]}"}}')]),
                      _resp(content="Starting mesh generation.")])

    async def _call(**kw):
        captured["system"] = kw["messages"][0]["content"]
        return next(responses)

    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))
    return state, out, captured.get("system", "")


# sections 3 + 10: real-path reconstruction, confirmation, dispatch, equality

def test_realpath_reconstructs_all_standing_fields():
    session = _session()
    state = chat._build_intake_state(session, "owner-A")
    assert state["engine"] == "snappy"                 # canonical field, from session.mesh_engine
    assert state["purpose"] == "external_cfd"
    assert state["input_kind"] == "body-surface"
    assert state["dimensionality"] == "3D"
    assert state["intake_patches"] == session.intake_patches
    assert state["engine_params"] == {}
    assert state["request_txt"] == _REQ


def test_realpath_confirmation_block_reflects_exact_values():
    session = _session()
    state, out, system = _run_confirm(session)
    # the block the model actually saw reflects the real standing submission (no "(unset)")
    header = system.split("request_txt")[0]
    assert "engine:         snappy" in system
    assert "purpose:        external_cfd" in system
    assert "input_kind:     body-surface" in system
    assert "dimensionality: 3D" in system
    assert "airfoil(wall)" in system and "farfield(farfield)" in system
    assert "(unset)" not in header and "(none)" not in header
    # the model cannot dispatch - a plain approval never reaches it (see the approval lifecycle
    # suite); the reconstructed session fields still equal the approved/displayed values.
    assert out.get("dispatch_confirmed") is False
    assert (session.mesh_engine, session.purpose, session.input_kind, session.dimensionality) \
        == ("snappy", "external_cfd", "body-surface", "3D")


def test_realpath_reconstruction_is_complete_enough_to_show_the_standing_run():
    legacy = {"request_txt": _REQ}       # the old sparse reconstruction
    assert "(unset)" in intake._confirmation_block(legacy)
    full = chat._build_intake_state(_session(), "owner-A")
    assert "(unset)" not in intake._confirmation_block(full).split("request_txt")[0]


# section 4: correction / replacement semantics via the real reconstruction

def test_engine_change_reconstructs_the_new_engine_no_stale_leak():
    # session already updated to the NEW complete submission (submit overwrites the whole snapshot)
    session = _session(mesh_engine="gmsh", engine_params={"element_order": "2"})
    state = chat._build_intake_state(session, "o")
    assert state["engine"] == "gmsh"
    assert state["engine_params"] == {"element_order": "2"}     # snappy params gone
    assert "cfmesh" not in intake._confirmation_block(state) and "snappy" not in intake._confirmation_block(state)


def _patches_line(block: str) -> str:
    return next(l for l in block.splitlines() if l.strip().startswith("patches:"))


def test_purpose_change_to_structural_drops_cfd_roles():
    # a complete, CONSISTENT structural snapshot (submit overwrites the whole set, prose included)
    session = _session(purpose="structural", input_kind="solid-body", mesh_engine="gmsh",
                       engine_params={"element_order": "2"},
                       request_txt=("Structural stress analysis of a bracket solid. Fixed at the base, "
                                    "load applied at the tip. Second-order tetrahedra. Resolve fillets."),
                       review_brief_txt=("Solid meshed throughout. Fixed and load faces present. Good "
                                         "element quality around fillets."),
                       intake_patches=[{"name": "base", "type": "fixed"}, {"name": "tip", "type": "load"}])
    state = chat._build_intake_state(session, "o")
    block = intake._confirmation_block(state)
    assert state["purpose"] == "structural"
    pl = _patches_line(block)
    assert "fixed" in pl and "load" in pl
    assert "farfield" not in pl and "inlet" not in pl   # no obsolete CFD roles in the patch set


def test_purpose_change_to_cfd_drops_structural_roles():
    session = _session(purpose="internal_cfd", mesh_engine="gmsh", engine_params={"element_order": "2"},
                       request_txt=("Internal flow through a pipe. Inlet, outlet and pipe wall. "
                                    "Second-order tetrahedra. Resolve the bend and the throat."),
                       review_brief_txt=("Fluid volume meshed. Inlet, outlet and wall patches present "
                                         "and correctly assigned. Good near-wall resolution."),
                       intake_patches=[{"name": "in", "type": "inlet"}, {"name": "out", "type": "outlet"},
                                       {"name": "pipe", "type": "wall"}])
    pl = _patches_line(intake._confirmation_block(chat._build_intake_state(session, "o")))
    assert "inlet" in pl and "outlet" in pl
    assert "fixed" not in pl and "load" not in pl


def test_dimensionality_and_patch_replacement_show_current_snapshot():
    session = _session(dimensionality="2D",
                       intake_patches=[{"name": "airfoil", "type": "wall"}, {"name": "ff", "type": "farfield"},
                                       {"name": "front", "type": "empty"}])
    block = intake._confirmation_block(chat._build_intake_state(session, "o"))
    assert "dimensionality: 2D" in block and "front(empty)" in block


def test_a_confirmation_turn_never_dispatches_from_the_model():
    session = _session()
    _, out, _ = _run_confirm(session, quote="yes")
    assert out.get("dispatch_confirmed") is False
