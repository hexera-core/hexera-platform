# Responsibility: Verify only a token bound to this owner, session, revision and confirmed selection can authorise.
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es

_MSGS = [{"role": "user", "content": "external CFD, gmsh, fluid-domain, inlet/outlet/wall"}]
_REV = at.revision_of(_MSGS)
_CANON = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                              [{"name": "in", "type": "inlet"}, {"name": "out", "type": "outlet"},
                               {"name": "w", "type": "wall"}], {"element_order": "2"})


def _sel(engine="gmsh", sid="s", oid="u"):
    return es.select_from_structured_input(engine, session_id=sid, owner_id=oid, revision=_REV)


_SEL = _sel()


def _tok(selection=None, **over):
    t = at.issue(session_id="s", owner_id="u", revision=_REV, canonical=_CANON,
                 verdict="supported", mode=at.SELECTED,
                 selection_id=(selection or _SEL)["id"])
    t.update(over)
    return t


def _seed_gate(state, canonical=None, engine="gmsh"):
    sel = _sel(engine, str(state.get("session_id", "")), str(state.get("user_id", "")))
    tok = at.issue(session_id=str(state.get("session_id", "")), owner_id=str(state.get("user_id", "")),
                   revision=at.revision_of(state["messages"]), canonical=canonical or _CANON,
                   verdict="supported", mode=at.SELECTED, selection_id=sel["id"])
    state["intake_gate"] = {"selection": sel, "admission": tok}
    return tok["token"]


# verify_for_submit: every rejection clause (section 8: preview enforcement)

def _ok(pending, token=None, sid="s", oid="u", rev=_REV, canon=_CANON, selection=_SEL):
    return at.verify_for_submit(pending, token if token is not None else (pending or {}).get("token", ""),
                                session_id=sid, owner_id=oid, revision=rev, submitted_canonical=canon,
                                selection=selection)


def test_no_token_or_pending_is_rejected():
    assert _ok(None, "x")[0] is False
    assert _ok(_tok(), "")[0] is False


def test_wrong_token_string_is_rejected():
    assert _ok(_tok(), "not-the-token")[0] is False


def test_a_matching_token_is_accepted():
    p = _tok()
    assert _ok(p, p["token"])[0] is True


def test_owner_and_session_bound():
    p = _tok()
    assert _ok(p, p["token"], sid="other")[0] is False
    assert _ok(p, p["token"], oid="other")[0] is False


def test_expired_token_is_rejected():
    assert _ok(_tok(expires_at=time.time() - 1), )[0] is False


def test_recommendation_mode_token_cannot_authorize_submission():
    assert _ok(_tok(mode=at.RECOMMENDATION))[0] is False


def test_unsupported_verdict_token_is_rejected():
    assert _ok(_tok(verdict="impossible"))[0] is False


def test_a_new_user_message_revision_invalidates_the_token():
    assert _ok(_tok(), rev="different-revision")[0] is False


def test_partial_three_field_preview_cannot_authorize_a_full_submission():
    three = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", None, None, None)
    p = at.issue(session_id="s", owner_id="u", revision=_REV, canonical=three,
                 verdict="supported", mode=at.SELECTED, selection_id=_SEL["id"])
    assert _ok(p, p["token"], canon=_CANON)[0] is False       # full payload != previewed 3-field


def test_changed_payload_reuses_old_token_is_rejected():
    p = _tok()
    changed = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D",
                                   [{"name": "w", "type": "wall"}], {"element_order": "2"})  # patches dropped
    assert _ok(p, p["token"], canon=changed)[0] is False


# node_intake: submit is impossible without a valid token

def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content,
        finish_reason="tool_calls" if tool_calls else "stop")


def _tc(name, args):
    from types import SimpleNamespace
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _run(state, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    it = iter(responses)

    async def _call(**_k):
        return next(it)
    with patch.object(llm_router, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


_SUBMIT = {"domain": "duct", "request_txt": "x" * 120, "review_brief_txt": "y" * 90,
           "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
           "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct", "engine_params": {"element_order": "2"},
           "patches": [{"name": "in", "type": "inlet"}, {"name": "out", "type": "outlet"},
                       {"name": "w", "type": "wall"}]}


def test_submit_without_a_token_persists_nothing():
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(_MSGS)}
    out = _run(state, [_resp([_tc("submit_requirements", json.dumps(_SUBMIT))]),
                       _resp(content="I need to preview first.")])
    assert not out.get("request_txt") and out.get("dispatch_confirmed") is False


def test_submit_with_a_matching_seeded_token_is_authorized_and_shows_canonical_summary():
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(_MSGS)}
    _token = _seed_gate(state)           # confirmed gmsh + token bound to _CANON == _SUBMIT's canonical
    out = _run(state, [_resp([_tc("submit_requirements",
                                  json.dumps({**_SUBMIT, "preview_token": _token}))]),
                       _resp(content="never reached - app renders the summary")])
    assert out.get("request_txt")                        # authorized + persisted
    reply = out["messages"][-1]["content"]
    # app-rendered: the application asks, and the structured brief states what will be built
    assert "confirm the requirements" in reply and "Shall I proceed" in reply


def test_a_token_whose_backing_selection_was_replaced_is_rejected():
    p = _tok()
    replaced = _sel("gmsh")                      # same engine, but a different selection record
    assert _ok(p, p["token"], selection=replaced)[0] is False
    assert _ok(p, p["token"], selection=None)[0] is False          # selection cleared entirely
    other_engine = _sel("cfmesh")
    p2 = _tok(selection=other_engine)
    assert _ok(p2, p2["token"], selection=other_engine)[0] is False  # canonical engine != confirmed


def test_a_merely_proposed_selection_cannot_back_a_token():
    proposed = es.propose("gmsh", session_id="s", owner_id="u", revision=_REV, user_msg_count=1)
    p = _tok(selection=proposed)
    assert _ok(p, p["token"], selection=proposed)[0] is False


def test_submit_and_confirm_are_refused_in_a_recommendation_turn():
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "Which engines are compatible with my fluid domain?"}]}
    _token = _seed_gate(state)
    recs = json.dumps({"purpose": "internal_cfd", "input_kind": "fluid-domain"})
    out = _run(state, [_resp([_tc("recommend_compatible_engines", recs),
                              _tc("submit_requirements", json.dumps({**_SUBMIT, "preview_token": _token})),
                              _tc("confirm_dispatch", json.dumps({"user_agreed_verbatim": "go",
                                                                  "preview_token": _token}))]),
                       _resp(content="gmsh and cfmesh are compatible. Which would you like?")])
    assert not out.get("request_txt"), "a recommendation turn must not persist requirements"
    assert out.get("dispatch_confirmed") is False
