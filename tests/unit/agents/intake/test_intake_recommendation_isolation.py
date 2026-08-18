# Responsibility: Verify a recommendation answers only when asked, selects nothing, and does not survive the turn.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake.recommendation import recommendation_requested

_ASK = ("Which engines can handle my fluid domain? Just list compatible ones. Do not start meshing.")

_FIVE = [{"name": n, "type": "wall"} for n in
         ("fuselage", "wing", "horizontal_tail", "nacelles", "pylons")] + \
        [{"name": "farfield", "type": "farfield"}]
_ONE_WALL = [{"name": "aircraft", "type": "wall"}, {"name": "farfield", "type": "farfield"}]

# Catalog-derived fixtures (asserted in test_the_fixture_shapes_are_what_the_catalog_says):
_MANY = {"purpose": "external_cfd", "input_kind": "body-surface", "dimensionality": "3D",
         "patches": _ONE_WALL}                       # cfmesh + snappy compatible
# Exactly one engine, discriminated by a real capability difference: snappy has no 2D path. This
# used to lean on snappy refusing several wall patches, which it no longer does - and a fixture
# that depends on an engine's weakness stops meaning anything the moment the weakness is fixed.
_EXACTLY_ONE = {"purpose": "external_cfd", "input_kind": "body-surface", "dimensionality": "2D",
                "patches": [{"name": "airfoil", "type": "wall"},
                            {"name": "farfield", "type": "farfield"},
                            {"name": "front", "type": "empty"},
                            {"name": "back", "type": "empty"}]}
_NONE = {"purpose": "structural", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _FIVE}                           # nothing is compatible

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


_SEEN: list = []          # the tool results the model was shown, most recent last


def _run(state, responses):
    import meshpipeline.contracts.model_inference as llm
    calls = {"n": 0}
    it = iter(responses)
    _SEEN.clear()

    async def _call(**kw):
        calls["n"] += 1
        _SEEN[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict)
                    and m.get("role") == "tool"]
        return next(it)
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state)), calls["n"]


def _last_tool_payload():
    return json.loads(_SEEN[-1]["content"])


def _state(msg=_ASK, gate=None):
    s = {"job_id": "j", "session_id": "s", "user_id": "u",
         "messages": [{"role": "user", "content": msg}]}
    if gate is not None:
        s["intake_gate"] = gate
    return s


def _gate(out):
    return out.get("intake_gate") or {}


def _assert_recommendation_only(out, reply):
    assert not _gate(out).get("selection"), "no selection may be created"
    assert not _gate(out).get("admission"), "no admission token may be created"
    assert not out.get("request_txt") and not out.get("mesh_engine"), "nothing may be submitted"
    assert out.get("dispatch_confirmed") is False
    assert "Shall I proceed with mesh generation" not in reply, "no confirmation-to-mesh prompt"
    assert "Selected engine:" not in reply, "a comparison must not present a selection"


# the intent gate itself (turn-scoped, fail-closed)

def test_the_intent_gate_recognises_explicit_requests_and_fails_closed():
    for asked in ("Which engines can handle my fluid domain?", "what engine should I use?",
                  "Can you recommend an engine?", "list the compatible engines please",
                  "compare engines for me", "what alternatives do I have?",
                  "which meshers can preserve five wall patches?"):
        assert recommendation_requested(asked) is True, asked
    for not_asked in ("Use gmsh.", "Proceed.", "My geometry is a duct with three patches.",
                      "The engine is snappy and I want external CFD.", "yes", ""):
        assert recommendation_requested(not_asked) is False, not_asked


def test_the_fixture_shapes_are_what_the_catalog_says():
    from meshpipeline.agents.intake.recommendation import recommend_compatible_engines as R
    assert R(**_MANY, authorized=True)["compatible_engines"] == ["cfMesh", "snappyHexMesh"]   # names, not keys
    one = R(**_EXACTLY_ONE, authorized=True)
    assert one["compatible_engines"] == ["cfMesh"]
    # A rejected row survives only when the engine COULD do this purpose from other geometry.
    # An engine the catalog calls incapable of the purpose is not offered at all.
    assert sum(c["verdict"] == "impossible" for c in one["candidates"]) == 2
    assert "purpose_incompatible" not in {code for c in one["candidates"]
                                          for code in c["blocking_rule_codes"]}
    assert R(**_NONE, authorized=True)["compatible_engines"] == []


# recommendation-only request, across every compatibility shape

def test_recommendation_only_request_creates_nothing_for_every_compatibility_shape():
    for shape in (_NONE, _EXACTLY_ONE, _MANY):
        state = _state()
        out, ncalls = _run(state, [
            _resp([_tc("recommend_compatible_engines", json.dumps(shape))]),
            _resp(content="Here are the compatible engines. Which would you like to use?")])
        assert ncalls == 2, "an incompatible candidate must not terminate the comparison turn"
        _assert_recommendation_only(out, out["messages"][-1]["content"])


def test_an_impossible_candidate_never_hijacks_the_turn():
    state = _state()
    out, ncalls = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE))]),
        _resp(content="cfmesh can preserve your five separate wall patches.")])
    reply = out["messages"][-1]["content"]
    assert ncalls == 2
    assert "cfmesh can preserve" in reply
    assert "would you like to revise" not in reply, "an incompatibility reply hijacked the comparison"


def test_recommendation_is_refused_when_the_user_did_not_ask():
    state = _state("My geometry is a duct. Purpose is internal CFD.")
    out, ncalls = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
        _resp(content="Would you like me to suggest which engines are compatible?")])
    assert ncalls == 2
    payload = _last_tool_payload()
    assert payload["recommendation_authorized"] is False
    assert payload["candidates"] == [], "no named alternatives when intent is unclear"
    _assert_recommendation_only(out, out["messages"][-1]["content"])


# the defect itself: call count must not determine mode

def test_sequential_one_per_response_exploration_is_still_recommendation_only():
    state = _state()
    out, ncalls = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE))]),
        _resp([_tc("recommend_compatible_engines", json.dumps(_NONE))]),
        _resp(content="Those are the compatible engines - which would you like?")])
    assert ncalls == 4
    _assert_recommendation_only(out, out["messages"][-1]["content"])


def test_call_count_in_a_response_has_no_effect_on_mode():
    batched = _state()
    out_b, _ = _run(batched, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_MANY)),
               _tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE)),
               _tc("recommend_compatible_engines", json.dumps(_NONE))]),
        _resp(content="answer")])
    sequential = _state()
    out_s, _ = _run(sequential, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE))]),
        _resp([_tc("recommend_compatible_engines", json.dumps(_NONE))]),
        _resp(content="answer")])
    assert _gate(out_b) == _gate(out_s) == {"selection": None, "admission": None,
                                            "approval": None}


# same-turn escalation is impossible in every direction

def test_same_turn_escalation_after_a_recommendation_is_refused():
    state = _state()
    submit = json.dumps({"domain": "aircraft aero", "request_txt": _R, "review_brief_txt": _B,
                         "dimensionality": "3D", "purpose": "external_cfd",
                         "input_kind": "body-surface", "mesh_engine": "cfmesh",
                         "mesh_fidelity": "standard", "engine_source": "user_direct", "engine_params": {},
                         "patches": _FIVE, "preview_token": "anything"})
    out, _ = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE))]),
        _resp([_tc("propose_engine_selection", json.dumps({"engine": "cfmesh"})),
               _tc("preview_selected_admission", json.dumps({"selected_engine": "cfmesh", **_EXACTLY_ONE})),
               _tc("submit_requirements", submit),
               _tc("confirm_dispatch", json.dumps({"user_agreed_verbatim": "go",
                                                   "preview_token": "anything"}))]),
        _resp(content="cfmesh is the compatible engine - would you like to use it?")])
    _assert_recommendation_only(out, out["messages"][-1]["content"])


def test_a_sole_compatible_engine_is_still_not_selected():
    from meshpipeline.agents.intake.recommendation import recommend_compatible_engines as R
    one = R(**_EXACTLY_ONE, authorized=True)
    assert one["compatible_engines"] == ["cfMesh"]
    assert one["authorizes_selection"] is False, "a sole candidate must not authorize selection"
    assert one["authorizes_submission"] is False
    state = _state()
    out, _ = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE)),
               _tc("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
        _resp(content="Only cfmesh is compatible. Would you like to use it?")])
    _assert_recommendation_only(out, out["messages"][-1]["content"])


# authorization is turn-scoped, and never touches an existing selection

def test_recommendation_permission_does_not_survive_into_the_next_turn():
    state = _state()   # turn 1 asked for recommendations
    out, _ = _run(state, [_resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
                          _resp(content="cfmesh and snappy are compatible.")])
    # turn 2 is an unrelated message - permission must NOT be inherited
    state2 = _state("The inlet velocity is 2 m/s.")
    out2, _ = _run(state2, [_resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
                            _resp(content="Would you like engine recommendations?")])
    payload = _last_tool_payload()
    assert payload["recommendation_authorized"] is False
    assert payload["candidates"] == []
    _assert_recommendation_only(out2, out2["messages"][-1]["content"])


def test_comparing_alternatives_leaves_an_existing_confirmed_selection_untouched():
    sel = es.select_from_structured_input("snappy", session_id="s", owner_id="u", revision="r0")
    tok = at.issue(session_id="s", owner_id="u", revision="r0",
                   canonical=at.canonical_payload("snappy", "external_cfd", "body-surface", "3D",
                                                  _ONE_WALL, {}),
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    state = _state("Which other engines could I compare against?",
                   gate={"selection": sel, "admission": tok})
    out, _ = _run(state, [_resp([_tc("recommend_compatible_engines", json.dumps(_MANY))]),
                          _resp(content="cfmesh and snappy are both compatible.")])
    assert _gate(out)["selection"] == sel, "the confirmed selection must not change"
    assert _gate(out)["selection"]["engine"] == "snappy"
    assert out.get("dispatch_confirmed") is False
    assert not out.get("mesh_engine")


def test_an_impossible_check_of_the_confirmed_engine_cannot_hijack_a_comparison_turn():
    sel = es.select_from_structured_input("snappy", session_id="s", owner_id="u",
                                          revision="r-earlier-turn")
    state = _state("Which engines could preserve my five separate wall patches?",
                   gate={"selection": sel, "admission": None})
    out, ncalls = _run(state, [
        _resp([_tc("recommend_compatible_engines", json.dumps(_EXACTLY_ONE)),
               _tc("preview_selected_admission",
                   json.dumps({"selected_engine": "snappy", **_EXACTLY_ONE}))]),
        _resp(content="Only cfmesh can preserve all five; snappy wraps them into one.")])
    reply = out["messages"][-1]["content"]
    assert ncalls == 2, "the comparison turn was terminated by an incompatible candidate"
    assert "Only cfmesh can preserve all five" in reply
    assert "would you like to revise" not in reply
    assert _gate(out)["selection"] == sel, "the confirmed selection is untouched"
    assert _gate(out)["admission"] is None


def test_recommendation_results_come_only_from_the_registry():
    from meshpipeline.agents.intake.recommendation import recommend_compatible_engines as R
    from meshpipeline.engines.registry import engine_names
    r = R(**_MANY, authorized=True)
    listed = [c["engine"] for c in r["candidates"]]
    # Rows are named in the vocabulary intake speaks; each must map back to a registered engine,
    # which is a stricter check than the old key comparison - it proves the name resolves.
    from meshpipeline.agents.intake import vocabulary as _vocab
    assert {_vocab.to_key(_vocab.ENGINE, n) for n in listed} <= set(engine_names()), \
        "a row named an engine that is not registered"
    keys = [_vocab.to_key(_vocab.ENGINE, n) for n in listed]
    assert keys == sorted(keys, key=engine_names().index), "rows keep registry order"
    assert all(c["reason"] for c in r["candidates"]), "every row carries a catalog-derived reason"
    assert r["authorizes_selection"] is False and r["authorizes_submission"] is False
