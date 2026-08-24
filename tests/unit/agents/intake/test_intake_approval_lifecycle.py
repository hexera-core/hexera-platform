# Responsibility: Verify what counts as approval, what invalidates a snapshot, and that only a live one dispatches.
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests._geometry_support import (
    interpretation_lookup,
    interpretation_ref,
    source_lookup,
    source_ref,
    source_row,
)

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.api.v1.chat as chat_mod

# ONE approved upload for this module: the session points at it, the dispatch snapshot
# carries it, and the fingerprint binds it.
_SOURCE = source_ref(owner_id="alice", filename="input.step")
_INTERPRETATION = interpretation_ref(
    geometry_source_id=_SOURCE.source_id,
    interpretation_id="dddd1111-3333-4333-b333-dddddddddddd")

_PATCHES = [{"name": "inlet", "type": "inlet", "diameter_mm": 40},
            {"name": "outlet", "type": "outlet", "diameter_mm": 60},
            {"name": "wall", "type": "wall"}]
_DECL = {"purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "patches": _PATCHES, "engine_params": {"element_order": "2"}}
_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion. " * 2)
_SUBMIT = {"domain": "duct internal", "request_txt": _R, "review_brief_txt": _B,
           "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
           "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
           "engine_params": {"element_order": "2"}, "patches": _PATCHES}


# the confirmation grammar

def test_bare_approvals_approve():
    for m in ("yes", "Yes.", "yes, proceed", "Proceed exactly as shown.", "approve this "
              "configuration", "use these requirements", "go ahead", "Yes - proceed!",
              "confirmed", "OK, proceed please"):
        assert ap.classify(m) == ap.APPROVE_INTENT, m


def test_agreement_with_a_change_is_never_an_approval():
    for m in ("yes, but make the far-field 50 chords", "yes - change the engine to gmsh",
              "proceed, but drop the symmetry patch", "yes, and set Mach to 0.8",
              "approve, though the inlet should be 2 m/s"):
        assert ap.classify(m) == ap.CORRECTION_INTENT, m


def test_hedges_are_ambiguous_and_corrections_are_corrections():
    for m in ("maybe", "not sure", "I think so", "hmm"):
        assert ap.classify(m) == ap.HEDGE_INTENT, m
    for m in ("no", "change the engine", "remove this patch", "update Mach to 0.8",
              "that summary is wrong"):
        assert ap.classify(m) == ap.CORRECTION_INTENT, m


# the record and its verification clauses

def _sel(engine="gmsh", owner="u", session="s"):
    return es.select_from_structured_input(engine, session_id=session, owner_id=owner,
                                           revision="r1")


def _approval(selection=None, msg_count=1, owner="u", session="s", **over):
    selection = selection or _sel(owner=owner, session=session)
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES,
                                 {"element_order": "2"})
    intent = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={"element_order": "2"}, requested_mesh_fidelity="standard",
        request_txt=_SUBMIT["request_txt"], source_ref=_SOURCE)
    rec = ap.create(owner_id=owner, session_id=session, selection_id=selection["id"], token_id="tok",
                    canonical=canon, fingerprint=at.fingerprint(canon),
                    intent_canonical=intent, intent_fingerprint=at.fingerprint(intent),
                    payload=dict(_SUBMIT),
                    summary=at.CONFIRM_REQUIREMENTS_ASK, proposal_revision="r1",
                    proposal_msg_count=msg_count)
    rec.update(over)
    return rec, selection


def _ok(rec, selection, msg_count=2, owner="u", session="s"):
    return ap.verify(rec, owner_id=owner, session_id=session, selection=selection,
                     user_msg_count=msg_count)


def test_a_live_approval_verifies_on_exactly_the_next_message():
    rec, sel = _approval(msg_count=1)
    assert _ok(rec, sel, msg_count=2)[0] is True     # the expected confirmation message
    assert _ok(rec, sel, msg_count=3)[0] is False    # the user said something else since
    assert _ok(rec, sel, msg_count=1)[0] is False


def test_every_other_invalidation_clause():
    rec, sel = _approval()
    assert _ok(rec, sel, owner="other")[0] is False
    assert _ok(rec, sel, session="other")[0] is False
    assert _ok({**rec, "expires_at": time.time() - 1}, sel)[0] is False
    assert _ok({**rec, "status": ap.DISPATCHED}, sel)[0] is False
    assert _ok({**rec, "status": ap.INVALIDATED}, sel)[0] is False
    assert _ok(rec, None)[0] is False                             # selection gone
    assert _ok(rec, _sel())[0] is False                           # selection REPLACED
    assert _ok(rec, es.propose("gmsh", session_id="s", owner_id="u", revision="r1",
                               user_msg_count=1))[0] is False     # only proposed
    assert ap.verify(None, owner_id="u", session_id="s", selection=sel, user_msg_count=2)[0] is False


def test_snapshot_a_cannot_be_dispatched_by_a_confirmation_meant_for_b():
    a, sel = _approval()
    b = {**a, "id": uuid.uuid4().hex, "fingerprint": "different"}
    # B replaced A; a confirmation now resolves B, and A is not what dispatches.
    assert b["id"] != a["id"]
    assert _ok(b, sel)[0] is True
    assert ap.invalidate(a, "replaced")["status"] == ap.INVALIDATED


def test_invalidation_preserves_the_record_as_audit_evidence():
    rec, _ = _approval()
    dead = ap.invalidate(rec, "user replied with a correction")
    assert dead["status"] == ap.INVALIDATED and dead["id"] == rec["id"]
    assert dead["invalidated_reason"] == "user replied with a correction"
    assert dead["canonical"] == rec["canonical"]      # evidence kept, not dropped


# node_intake persists the snapshot on submit

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
        nxt = next(it)
        return nxt() if callable(nxt) else nxt
    with patch.object(llm, "call_intake_model", _call):
        return asyncio.run(intake.node_intake(state))


def test_a_supported_submit_persists_a_pending_snapshot_bound_to_everything():
    sel = es.select_from_structured_input(
        "gmsh", session_id="s", owner_id="u",
        revision=at.revision_of([{"role": "user", "content": "go on"}]))
    state = {"job_id": "j", "session_id": "s", "user_id": "u",
             "messages": [{"role": "user", "content": "go on"}],
             "intake_gate": {"selection": sel, "admission": None}}

    def _submit_with_token():
        tok = json.loads(_SEEN[-1]["content"])["preview_token"]
        return _resp([_tc("submit_requirements", json.dumps({**_SUBMIT, "preview_token": tok}))])

    out = _run(state, [
        _resp([_tc("preview_selected_admission", json.dumps({"selected_engine": "gmsh", **_DECL}))]),
        _submit_with_token, _resp(content="unreached")])

    snap = out["intake_gate"]["approval"]
    assert snap["status"] == ap.AWAITING
    assert snap["owner"] == "u" and snap["session"] == "s"
    assert snap["selection_id"] == sel["id"]
    assert snap["token_id"] == out["intake_gate"]["admission"]["token"]
    assert snap["fingerprint"] == at.fingerprint(snap["canonical"])
    assert snap["expected_confirmation_msg_count"] == snap["proposal_msg_count"] + 1 == 2
    # the summary the user sees IS the stored summary, rendered from the stored canonical
    assert snap["summary"] == out["messages"][-1]["content"]
    assert "confirm the requirements" in snap["summary"]   # the ask; the brief states them
    # the stored payload is the exact submission - what the builder will consume
    assert ap.builder_payload(snap)["patches"] == _PATCHES
    assert ap.builder_payload(snap)["request_txt"] == _R


# chat routing: approval never reaches the model

def _session_with(approval, selection, messages):
    return SimpleNamespace(
        id=uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb"), owner_id="alice", job_id=None,
        messages=messages, request_txt="req", review_brief_txt="brief", intake_patches=_PATCHES,
        dimensionality="3D", purpose="internal_cfd", input_kind="fluid-domain",
        mesh_engine="gmsh", domain="duct", engine_params={"element_order": "2"}, requested_mesh_fidelity="standard",
        geometry_source_id=_SOURCE.source_id, geometry_source=source_row(_SOURCE),
        # A session that has reached approval has a confirmed scale - intake cannot complete
        # without one. Leaving it unset describes a state the approval flow never sees, and the
        # turn would be taken by the unit question instead of the branch under test.
        geometry_interpretation_id=uuid.UUID("dddd1111-3333-4333-b333-dddddddddddd"),
        llm_metadata=[],
        intake_gate={"selection": selection, "admission": None, "approval": approval})


def _route(user_message, approval, selection):
    sid = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")
    msgs = [{"role": "user", "content": "earlier"}]
    session = _session_with(approval, selection, msgs)
    repo = MagicMock()
    repo.get_internal = AsyncMock(return_value=session)
    repo.get_for_owner = AsyncMock(return_value=session)
    # Since the message and its gate consequence are ONE locked transaction owned by
    # `agents/intake/message`, so the row is taken FOR UPDATE - the route no longer reads it.
    repo.get_for_update = AsyncMock(return_value=session)
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    calls = {"intake": 0, "dispatch": 0}

    async def _fake_intake(_state):
        calls["intake"] += 1
        return {"messages": [{"role": "assistant", "content": "let me re-check that"}],
                "dispatch_confirmed": False, "intake_gate": {}}

    async def _fake_dispatch(*_a, **_k):
        calls["dispatch"] += 1
        return chat_mod.ChatResponse(session_id=sid, reply="Starting mesh generation.",
                                     done=True, job_id=uuid.uuid4())

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    with (patch.object(chat_mod, "get_db", _db),
          patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
                return_value=repo),
          patch("meshpipeline.agents.intake.agent.node_intake", _fake_intake),
          patch.object(chat_mod, "_confirm_pending_approval", _fake_dispatch)):
        body = SimpleNamespace(session_id=sid, content=user_message)
        resp = asyncio.run(chat_mod.chat_message(body, owner_id="alice"))
    return resp, calls, repo


def test_a_bare_approval_dispatches_with_zero_model_calls():
    rec, sel = _approval(msg_count=1)
    resp, calls, _ = _route("yes, proceed", rec, sel)
    assert calls["dispatch"] == 1
    assert calls["intake"] == 0, "the model must not be called on a confirmation turn"
    assert resp.done is True


def test_an_ambiguous_reply_asks_once_and_dispatches_nothing():
    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route("maybe", rec, sel)
    assert calls["dispatch"] == 0 and calls["intake"] == 0
    assert resp.reply == ap.CLARIFICATION and resp.awaiting_confirmation is True


def test_a_correction_invalidates_the_snapshot_and_returns_to_intake():
    rec, sel = _approval(msg_count=1)
    resp, calls, repo = _route("change the engine to gmsh", rec, sel)
    assert calls["dispatch"] == 0, "the old payload must never dispatch"
    assert calls["intake"] == 1
    stored = repo.set_intake_gate.call_args_list[0][0][2]   # the invalidating write
    assert stored["approval"]["status"] == ap.INVALIDATED
    assert resp.done is False


def test_after_a_clarification_the_users_next_clear_answer_still_dispatches():
    rec, sel = _approval(msg_count=1)
    deferred = ap.defer(rec)
    assert deferred["expected_confirmation_msg_count"] == 3      # moved with the question
    assert ap.is_live(deferred) and deferred["status"] == ap.AWAITING
    assert _ok(deferred, sel, msg_count=3)[0] is True            # the answer after the hedge
    assert _ok(deferred, sel, msg_count=2)[0] is False
    assert ap.defer(deferred)["deferred_count"] == 2             # hedging again defers again


def test_the_hedge_branch_persists_the_deferred_snapshot():
    rec, sel = _approval(msg_count=1)
    _, calls, repo = _route("maybe", rec, sel)
    assert calls["dispatch"] == 0 and calls["intake"] == 0
    stored = repo.set_intake_gate.call_args_list[0][0][2]["approval"]
    assert stored["status"] == ap.AWAITING                        # still alive, not invalidated
    assert stored["expected_confirmation_msg_count"] == rec["expected_confirmation_msg_count"] + 1


def test_a_dead_snapshot_does_not_capture_the_turn():
    rec, sel = _approval(msg_count=1, status=ap.INVALIDATED)
    _, calls, _ = _route("yes, proceed", rec, sel)
    assert calls["dispatch"] == 0 and calls["intake"] == 1


# the dispatch path itself: what the builder receives, and exactly-once

def _dispatch_once(session_gate, session_overrides=None, msgs=None):
    sid = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")
    locked = SimpleNamespace(
        id=sid, owner_id="alice", job_id=None,
        messages=msgs or [{"role": "user", "content": "hi"}, {"role": "user", "content": "yes"}],
        intake_gate=session_gate, llm_metadata=[],
        geometry_source_id=_SOURCE.source_id, geometry_source=source_row(_SOURCE),
        geometry_interpretation_id=_INTERPRETATION.interpretation_id,
        # deliberately WRONG session columns: if the builder payload is reconstructed from these
        # instead of consumed from the approved snapshot, the assertions below fail.
        request_txt="STALE REQUEST", review_brief_txt="STALE BRIEF",
        intake_patches=[{"name": "STALE", "type": "wall"}], dimensionality="2D",
        purpose="structural", input_kind="solid-body", mesh_engine="snappy", domain="stale",
        engine_params={"stale": "1"})
    for k, v in (session_overrides or {}).items():
        setattr(locked, k, v)

    repo = MagicMock()
    repo.get_for_update = AsyncMock(return_value=locked)
    repo.set_intake_gate = AsyncMock()
    repo.link_job = AsyncMock()
    repo.set_request_txt = AsyncMock()
    made = []

    class _JobRepo:
        async def create(self, _db, owner_id):
            j = SimpleNamespace(id=uuid.uuid4(), geometry_source_id=None,
                                owner_id=owner_id)
            made.append(j)
            return j

    class _Svc:
        async def check_quotas(self, _db, _owner):
            return None

    sent = {}

    async def _fake_dispatch(_db, job_id, payload):
        sent["job_id"], sent["payload"] = job_id, payload

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    with (patch("meshpipeline.persistence.session.get_db", _db),
          patch("meshpipeline.persistence.repositories.job_repository.JobRepository", _JobRepo),
          patch("meshpipeline.application.job_service.JobService", _Svc),
          patch("meshpipeline.application.pipeline_run.dispatch", _fake_dispatch),
          source_lookup(_SOURCE),
          interpretation_lookup(_INTERPRETATION, owner_id="alice")):
        resp = asyncio.run(chat_mod._confirm_pending_approval(locked, repo, "alice", sid))
    return resp, sent.get("payload"), made


_SID = "bbbbcccc-2222-4222-b222-bbbbbbbbbbbb"


def test_the_builder_receives_the_approved_snapshot_not_the_session_columns():
    rec, sel = _approval(msg_count=1, owner="alice", session=_SID)
    resp, payload, made = _dispatch_once({"selection": sel, "admission": None, "approval": rec})
    assert resp.done is True and len(made) == 1
    # every run-determining value came from the APPROVED SNAPSHOT, not the stale session row
    assert payload["request_txt"] == _R and payload["review_brief_txt"] == _B
    assert payload["intake_patches"] == _PATCHES
    assert payload["mesh_engine"] == "gmsh" and payload["purpose"] == "internal_cfd"
    assert payload["input_kind"] == "fluid-domain" and payload["dimensionality"] == "3D"
    assert payload["engine_params"] == {"element_order": "2"}
    assert payload["domain"] == "duct internal"
    assert "STALE" not in json.dumps(payload)
    # the snapshot id rides along for diagnostics, and the fingerprints agree
    assert payload["approved_snapshot_id"] == rec["id"]
    assert at.fingerprint(at.canonical_payload(
        payload["mesh_engine"], payload["purpose"], payload["input_kind"],
        payload["dimensionality"], payload["intake_patches"], payload["engine_params"])) \
        == rec["fingerprint"]


def test_an_already_dispatched_snapshot_makes_no_second_job():
    rec, sel = _approval(msg_count=1, owner="alice", session=_SID)
    dead = {**rec, "status": ap.DISPATCHED, "job_id": str(uuid.uuid4())}
    resp, payload, made = _dispatch_once({"selection": sel, "admission": None, "approval": dead})
    assert made == [], "a duplicate confirmation must not create a second paid job"
    assert payload is None and resp.done is True


def test_a_stale_or_replaced_snapshot_refuses_to_dispatch():
    rec, sel = _approval(msg_count=1, owner="alice", session=_SID)
    # the user has said something else since the summary was shown
    resp, payload, made = _dispatch_once(
        {"selection": sel, "admission": None, "approval": rec},
        msgs=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
              {"role": "user", "content": "yes"}])
    assert made == [] and payload is None
    assert "did not start anything" in resp.reply

    # and a snapshot whose selection was replaced
    resp2, payload2, made2 = _dispatch_once(
        {"selection": _sel(owner="alice", session=_SID), "admission": None, "approval": rec})
    assert made2 == [] and payload2 is None
    assert "did not start anything" in resp2.reply
