# Responsibility: Verify the compatibility check answers from the registry, names no replacement and writes no state.
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.agent as intake
from meshpipeline.agents.intake.validation import check_engine_compatibility
from meshpipeline.engines.purposes import INPUT_KINDS, purpose_keys
from meshpipeline.engines.registry import engine_names


def _names_engine(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


# 1 + 7: impossible combinations name ONLY the selected engine

def test_snappy_structural_body_surface_is_impossible_and_names_no_other_engine():
    r = check_engine_compatibility("snappy", "structural", "body-surface")
    assert r["result"] == "impossible"
    assert "snappy" in r["explanation"]
    assert not any(_names_engine(r["explanation"], n) for n in engine_names() if not n.startswith("snappy")), \
        r["explanation"]
    assert "revise" in r["explanation"] and "engine" in r["explanation"]


def test_every_impossible_catalog_combination_names_no_other_engine():
    engines = engine_names()
    checked = impossible = 0
    for eng in engines:
        for pur in purpose_keys():
            for ik in INPUT_KINDS:
                checked += 1
                r = check_engine_compatibility(eng, pur, ik)
                if r["result"] != "impossible":
                    continue
                impossible += 1
                named_others = [n for n in engines if n != eng and _names_engine(r["explanation"], n)]
                assert not named_others, f"{eng}/{pur}/{ik} named {named_others}: {r['explanation']!r}"
    assert impossible > 0, "expected at least one impossible combination in the catalog"


# 4: supported unusual combination - no recommendation, no pressure

def test_supported_gmsh_external_cfd_fluid_domain():
    r = check_engine_compatibility("gmsh", "external_cfd", "fluid-domain")
    assert r["result"] == "supported"
    assert not any(_names_engine(r["explanation"], n) for n in engine_names() if n != "gmsh")


# 5: insufficient information asks, never invents

def test_insufficient_information_asks_for_the_missing_field():
    r = check_engine_compatibility("snappy", "structural", "")
    assert r["result"] == "insufficient_information"
    assert "input_kind" in r["explanation"] and "not guess" in r["explanation"]


def test_malformed_value_is_reported_not_guessed():
    r = check_engine_compatibility("no-such-engine", "structural", "body-surface")
    assert r["result"] == "malformed"


# catalog is the SOLE source: the tool agrees with validate_submission's admit path

def test_compat_matches_the_submission_admission_verdict():
    from meshpipeline.agents.intake.validation import validate_submission
    R = ("A complete requirements summary covering geometry, simulation type, parameters and mesh "
         "requirements for this case. " * 2)
    B = ("Acceptance criteria: valid mesh, correct regions, sound quality at the builder's "
         "discretion. " * 2)
    for eng, pur, ik, patches in [
        ("snappy", "structural", "body-surface", [{"name": "b", "type": "fixed"}]),
        ("gmsh", "external_cfd", "solid-body", [{"name": "b", "type": "wall"}, {"name": "f", "type": "farfield"}]),
        ("gmsh", "external_cfd", "fluid-domain", [{"name": "b", "type": "wall"}, {"name": "f", "type": "farfield"}]),
    ]:
        tool_impossible = check_engine_compatibility(eng, pur, ik)["result"] == "impossible"
        errs = validate_submission({
            "domain": "d test", "request_txt": R, "review_brief_txt": B, "dimensionality": "3D",
            "mesh_fidelity": "standard", "engine_source": "user_direct", "mesh_engine": eng, "purpose": pur, "input_kind": ik,
            "engine_params": {"element_order": "2"}, "patches": patches})
        submit_capability_reject = any("cannot produce" in e for e in errs)
        assert tool_impossible == submit_capability_reject, f"{eng}/{pur}/{ik}: tool≠submit"


# 3: only submit_requirements changes the engine; the compat tool writes NO state

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


def _run(state, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    it = iter(responses)

    async def _call(**_kw):
        return next(it)
    with patch.object(llm_router, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


def test_a_tool_call_the_agent_does_not_offer_writes_no_structured_state():
    # `check_engine_compatibility` is a helper the intake BRIEF calls, never a tool the agent
    # advertises. A model that invents it must change nothing.
    advertised = {f["function"]["name"] for f in intake.INTAKE_TOOLS}
    assert "check_engine_compatibility" not in advertised
    state = {"job_id": "j", "messages": [
        {"role": "user", "content": "structural mesh from a body surface with snappy"}]}
    out = _run(state, [
        _resp([_tool_call("check_engine_compatibility",
                          '{"engine":"snappy","purpose":"structural","input_kind":"body-surface"}')]),
        _resp(content="snappy cannot produce a structural mesh. Revise the engine, purpose, or input kind?"),
    ])
    assert out.get("dispatch_confirmed") is False
    for f in ("mesh_engine", "purpose", "input_kind", "dimensionality", "intake_patches", "request_txt"):
        assert not out.get(f), f"a compatibility check must not write {f!r}: {out.get(f)!r}"


# 6: prompt policy - the composed prompt forbids naming a replacement on incompatibility

def test_composed_prompt_states_the_global_recommendation_policy():
    S = intake.compose_intake_system()
    assert "ENGINE-RECOMMENDATION POLICY" in S
    assert "call preview_selected_admission" in S
    assert "recommend_compatible_engines" in S
    assert "a recommendation is NOT a selection" in S
    assert "Do NOT name, rank, select, suggest, or imply any replacement engine" in S
    assert "ONLY when the user EXPLICITLY asks for a recommendation" in S
    assert "the engine changes ONLY when the user explicitly picks a new one" in S
    # the policy must OVERRIDE softer wording later in the prompt
    assert "OVERRIDES any softer wording" in S
