# Responsibility: Verify the approved snapshot crosses the real dispatch seam, validated before anything is persisted.
from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests._geometry_support import (
    interpretation_lookup,
    interpretation_ref,
    source_lookup,
    source_ref,
    source_row,
)

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.api.v1.chat as chat_mod
from meshpipeline.application import dispatch_contract as dc
from meshpipeline.application.pipeline_run import JobRequest, run_pipeline
from meshpipeline.contracts import pipeline_execution as pe

# The approved upload this session points at.
_SOURCE = source_ref(owner_id="alice", filename="input.step",
                     source_id="22222222-2222-4222-8222-222222222222")
_INTERPRETATION = interpretation_ref(
    geometry_source_id=_SOURCE.source_id,
    interpretation_id="33333333-3333-4333-8333-333333333333")


_SID = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")
_PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "wall", "type": "wall"}]
_R = ("A complete requirements summary covering the geometry, the simulation type, every confirmed "
      "parameter and the mesh requirements for this case. " * 2)
_B = ("Acceptance criteria: a valid mesh, correct regions, no fatal defects, sizing at the "
      "builder's discretion. " * 2)
_APPROVED = {"domain": "duct internal", "request_txt": _R, "review_brief_txt": _B,
             "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
             "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
             "engine_params": {"element_order": "2"}, "patches": _PATCHES}


def _approved_session():
    sel = es.select_from_structured_input("gmsh", session_id=str(_SID), owner_id="alice",
                                          revision="r1")
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES,
                                 {"element_order": "2"})
    # production stores the COMPLETE approved intent (admission fields + approved fidelity +
    # approved geometry content identity) on the approval record; the dispatch seam re-verifies the
    # run against THAT durable record, so the fixture must carry it exactly as production does.
    intent = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={"element_order": "2"}, requested_mesh_fidelity="standard",
        request_txt=_APPROVED["request_txt"], source_ref=_SOURCE)
    snap = ap.create(owner_id="alice", session_id=str(_SID), selection_id=sel["id"],
                     token_id="tok", canonical=canon, fingerprint=at.fingerprint(canon),
                     intent_canonical=intent, intent_fingerprint=at.fingerprint(intent),
                     payload=dict(_APPROVED), summary=at.CONFIRM_REQUIREMENTS_ASK,
                     proposal_revision="r1", proposal_msg_count=1)
    session = SimpleNamespace(
        id=_SID, owner_id="alice", job_id=None,
        messages=[{"role": "user", "content": "hi"}, {"role": "user", "content": "yes, proceed"}],
        intake_gate={"selection": sel, "admission": None, "approval": snap},
        llm_metadata=[], geometry_source_id=_SOURCE.source_id,
        geometry_source=source_row(_SOURCE),
        # an approved session has resolved BOTH halves: which bytes, and what size
        geometry_interpretation_id=_INTERPRETATION.interpretation_id,
        request_txt="STALE REQUEST", review_brief_txt="STALE BRIEF",
        intake_patches=[{"name": "STALE", "type": "wall"}], dimensionality="2D",
        purpose="structural", input_kind="solid-body", mesh_engine="snappy", domain="stale",
        engine_params={"stale": "1"})
    return session, snap


def _run_real_confirmation(session):
    captured: dict = {}

    class _CapturingLauncher:
        async def launch(self, db, job_id: str, payload: dict) -> None:
            captured["job_id"] = job_id
            captured["payload"] = payload

    repo = MagicMock()
    repo.get_for_update = AsyncMock(return_value=session)
    repo.set_intake_gate = AsyncMock()
    repo.link_job = AsyncMock()
    repo.set_request_txt = AsyncMock()
    made: list = []

    class _JobRepo:
        async def create(self, _db, owner_id):
            j = SimpleNamespace(id=uuid.uuid4(), geometry_source_id=None,
                                owner_id=owner_id)
            made.append(j)
            return j

        async def set_dispatch_payload(self, _db, job_id, payload):
            captured["persisted"] = payload      # the durable row the Cloud Run path reloads

        async def mark_launch_failed(self, _db, job_id, error):
            captured["launch_failed"] = error

    class _Svc:
        async def check_quotas(self, _db, _owner):
            return None

    @asynccontextmanager
    async def _db():
        yield AsyncMock()

    prior = pe._launcher
    pe.set_pipeline_launcher(_CapturingLauncher())
    try:
        with (patch("meshpipeline.persistence.session.get_db", _db),
              patch("meshpipeline.persistence.repositories.job_repository.JobRepository", _JobRepo),
              patch("meshpipeline.application.job_service.JobService", _Svc),
              source_lookup(_SOURCE),
              interpretation_lookup(_INTERPRETATION, owner_id="alice")):
            resp = asyncio.run(chat_mod._confirm_pending_approval(session, repo, "alice", _SID))
    finally:
        pe._launcher = prior
    return resp, captured, made


# the seam

def test_the_approved_snapshot_crosses_the_real_dispatch_seam_into_run_pipeline():
    session, snap = _approved_session()
    resp, captured, made = _run_real_confirmation(session)

    assert resp.done is True and len(made) == 1
    payload = captured["payload"]
    assert captured["persisted"] == payload, "the launched payload IS the persisted row"

    # THE ASSERTION THAT WAS MISSING: the exact payload the API built binds to the run entry.
    kwargs = dc.to_run_kwargs(payload)
    inspect.signature(run_pipeline).bind(**kwargs)

    # provenance survives the whole way into the execution request object
    assert payload["approved_snapshot_id"] == snap["id"]
    assert kwargs["approved_snapshot_id"] == snap["id"]
    assert JobRequest(**{k: v for k, v in kwargs.items()
                         if k in JobRequest.__slots__}).approved_snapshot_id == snap["id"]

    # the approved canonical payload is unchanged, and stale session state did not leak
    assert kwargs["request_txt"] == _R and kwargs["review_brief_txt"] == _B
    assert kwargs["intake_patches"] == _PATCHES
    assert (kwargs["mesh_engine"], kwargs["purpose"], kwargs["input_kind"],
            kwargs["dimensionality"]) == ("gmsh", "internal_cfd", "fluid-domain", "3D")
    assert kwargs["engine_params"] == {"element_order": "2"}
    assert "STALE" not in str(payload)
    assert at.fingerprint(at.canonical_payload(
        kwargs["mesh_engine"], kwargs["purpose"], kwargs["input_kind"],
        kwargs["dimensionality"], kwargs["intake_patches"], kwargs["engine_params"])) \
        == snap["fingerprint"]


def test_the_persisted_payload_is_versioned_and_reconstructable():
    session, _ = _approved_session()
    _, captured, _ = _run_real_confirmation(session)
    persisted = captured["persisted"]
    assert persisted["schema_version"] == dc.DISPATCH_SCHEMA_VERSION
    # exactly what run_from_job does with the durable row
    inspect.signature(run_pipeline).bind(**dc.to_run_kwargs(persisted, where="reconstruction"))


# the seam refuses drift in both directions

def test_an_extra_producer_key_fails_before_anything_is_launched():
    session, _ = _approved_session()
    real_build = dc.build

    def _build_with_rogue_key(**fields):
        return real_build(**fields, rogue_field="x")

    with patch.object(dc, "build", _build_with_rogue_key):
        with pytest.raises(Exception) as exc:      # surfaced to the caller, not swallowed
            _run_real_confirmation(session)
    assert "does not accept" in str(exc.value) or "500" in str(exc.value)


def test_a_missing_required_consumer_field_fails():
    with pytest.raises(dc.DispatchContractError, match="missing required"):
        dc.build(owner_id="alice", approved_snapshot_id="s")


def test_dispatch_validates_before_it_persists_or_launches():
    from meshpipeline.application.pipeline_run import dispatch

    touched: dict = {"persisted": False, "launched": False}

    class _Repo:
        async def set_dispatch_payload(self, *_a, **_k):
            touched["persisted"] = True

    class _L:
        async def launch(self, *_a, **_k):
            touched["launched"] = True

    prior = pe._launcher
    pe.set_pipeline_launcher(_L())
    try:
        with patch("meshpipeline.persistence.repositories.job_repository.JobRepository", _Repo):
            with pytest.raises(dc.DispatchContractError):
                asyncio.run(dispatch(AsyncMock(), str(uuid.uuid4()),
                                     {"job_id": "j", "bogus_key": 1}))
    finally:
        pe._launcher = prior
    assert touched == {"persisted": False, "launched": False}


def test_run_pipeline_threads_the_snapshot_id_into_the_execution_request(monkeypatch):
    import meshpipeline.application.pipeline_run as pr

    seen: dict = {}

    async def _fake_run_async(req):
        seen["req"] = req
        return {"status": "succeeded"}

    monkeypatch.setattr(pr, "_run_async", _fake_run_async)
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
    _fields = {"job_id": "J", "owner_id": "alice", "mesh_engine": "gmsh",
                   "intake_patches": list(_PATCHES), "approved_snapshot_id": "snap-77",
                   "approved_patch_contract": ApprovedPatchContract.build(_PATCHES).to_dict()}
    payload = dc.build(**_fields,
                       approved_intent_fingerprint=dc.recompute_intent_fingerprint(_fields))
    out = pr.run_pipeline(**dc.to_run_kwargs(payload))

    assert out["status"] == "succeeded"
    assert seen["req"].approved_snapshot_id == "snap-77", "provenance dropped between entry and request"
    assert seen["req"].intake_patches == _PATCHES
    # the typed contract reaches the request too
    assert seen["req"].approved_patch_contract is not None
