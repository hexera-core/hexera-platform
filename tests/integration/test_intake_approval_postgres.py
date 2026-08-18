# Responsibility: Verify an approval round-trips through Postgres and two concurrent confirmations dispatch once.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.models import ChatSession, SimulationJob
from meshpipeline.persistence.repositories.session_repository import SessionRepository

repo = SessionRepository()

_PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "wall", "type": "wall"}]
_PAYLOAD = {"domain": "duct", "request_txt": "r" * 120, "review_brief_txt": "b" * 90,
            "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
            "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
            "engine_params": {"element_order": "2"}, "patches": _PATCHES}


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=6)
        # Alembic is the only thing that creates this schema - see
        # tests/harness_provisioning.py. `create_all` built tables no migration
        # had produced, so a suite could pass against a schema production never has.
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "chat_sessions", "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the approval CAS test - it must be "
                    f"PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _snapshot(session_id: str, selection: dict, msg_count: int = 1, *,
              mesh_fidelity: str = "standard") -> dict:
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES,
                                 {"element_order": "2"})
    # the DURABLE approval record carries the COMPLETE approved intent, including the TYPED fidelity
    intent = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={"element_order": "2"}, requested_mesh_fidelity=mesh_fidelity,
        request_txt=_PAYLOAD["request_txt"], source_ref=None)
    return ap.create(owner_id="owner-1", session_id=session_id, selection_id=selection["id"],
                     token_id="tok", canonical=canon, fingerprint=at.fingerprint(canon),
                     intent_canonical=intent, intent_fingerprint=at.fingerprint(intent),
                     payload={**_PAYLOAD, "mesh_fidelity": mesh_fidelity},
                     summary=at.CONFIRM_REQUIREMENTS_ASK,
                     proposal_revision="r1", proposal_msg_count=msg_count)


async def _seed(SessionLocal, *, mesh_fidelity: str = "standard") -> tuple[uuid.UUID, dict, dict]:
    async with SessionLocal() as s:
        row = ChatSession(owner_id="owner-1", messages=[{"role": "user", "content": "hi"},
                                                        {"role": "user", "content": "yes proceed"}],
                          llm_metadata=[])
        s.add(row)
        await s.commit()
        sid = row.id
    sel = es.select_from_structured_input("gmsh", session_id=str(sid), owner_id="owner-1",
                                          revision="r1")
    snap = _snapshot(str(sid), sel, mesh_fidelity=mesh_fidelity)
    async with SessionLocal() as s:
        await repo.set_intake_gate(s, sid, {"selection": sel, "admission": None, "approval": snap})
        await s.commit()
    return sid, sel, snap


async def _claim(SessionLocal, sid, *, delay: float = 0.0) -> str:
    if delay:
        await asyncio.sleep(delay)
    async with SessionLocal() as db:
        locked = await repo.get_for_update(db, sid)
        gate = dict(locked.intake_gate or {})
        approval, selection = gate.get("approval"), gate.get("selection")
        if (approval or {}).get("status") == ap.DISPATCHED:
            await db.commit()
            return f"idempotent:{approval.get('job_id')}"
        umc = len([m for m in (locked.messages or []) if m.get("role") == "user"])
        ok, why = ap.verify(approval, owner_id="owner-1", session_id=str(sid),
                            selection=selection, user_msg_count=umc)
        if not ok:
            await db.commit()
            return f"refused:{why[:30]}"
        # a REAL job row, as production does - chat_sessions.job_id is a foreign key
        job = SimulationJob(owner_id="owner-1")
        db.add(job)
        await db.flush()
        job_id = job.id
        gate["approval"] = {**approval, "status": ap.DISPATCHED, "job_id": str(job_id)}
        await repo.set_intake_gate(db, sid, gate)
        await repo.link_job(db, sid, job_id)
        await db.commit()
        return f"dispatched:{job_id}"


async def test_a_pending_approval_round_trips_through_postgres(SessionLocal):
    sid, sel, snap = await _seed(SessionLocal)
    async with SessionLocal() as s:
        stored = (await repo.get_internal(s, sid)).intake_gate
    assert stored["approval"]["id"] == snap["id"]
    assert stored["approval"]["status"] == ap.AWAITING
    assert stored["approval"]["fingerprint"] == snap["fingerprint"]
    assert stored["approval"]["payload"]["patches"] == _PATCHES
    assert ap.builder_payload(stored["approval"]) == _PAYLOAD


async def test_two_concurrent_confirmations_dispatch_exactly_once(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    a, b = await asyncio.gather(_claim(SessionLocal, sid), _claim(SessionLocal, sid, delay=0.01))
    outcomes = [a, b]
    dispatched = [o for o in outcomes if o.startswith("dispatched")]
    assert len(dispatched) == 1, outcomes
    loser = [o for o in outcomes if not o.startswith("dispatched")][0]
    assert loser.startswith("idempotent"), loser
    # the winner's job id is what the loser reports - one execution identity, not two
    assert loser.split(":")[1] == dispatched[0].split(":")[1]
    async with SessionLocal() as s:
        row = await repo.get_internal(s, sid)
    assert str(row.job_id) == dispatched[0].split(":")[1]
    assert row.intake_gate["approval"]["status"] == ap.DISPATCHED


async def test_a_repeated_confirmation_after_dispatch_is_idempotent(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    first = await _claim(SessionLocal, sid)
    for _ in range(3):                      # duplicate browser submits / network retries
        again = await _claim(SessionLocal, sid)
        assert again.startswith("idempotent") and again.split(":")[1] == first.split(":")[1]


async def test_a_pending_approval_survives_a_restart(SessionLocal):
    sid, _, snap = await _seed(SessionLocal)
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    engine2 = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args)
    Factory2 = async_sessionmaker(bind=engine2, expire_on_commit=False)
    try:
        async with Factory2() as s:
            reread = (await repo.get_internal(s, sid)).intake_gate["approval"]
        assert reread["id"] == snap["id"] and reread["status"] == ap.AWAITING
        out = await _claim(Factory2, sid)
        assert out.startswith("dispatched")
        assert (await _claim(Factory2, sid)).startswith("idempotent")
    finally:
        await engine2.dispose()


async def test_a_replaced_snapshot_cannot_be_dispatched(SessionLocal):
    sid, sel, snap_a = await _seed(SessionLocal)
    snap_b = _snapshot(str(sid), sel)
    async with SessionLocal() as s:
        gate = dict((await repo.get_internal(s, sid)).intake_gate)
        gate["approval"] = snap_b
        gate["superseded"] = ap.invalidate(snap_a, "replaced by a newer proposal")
        await repo.set_intake_gate(s, sid, gate)
        await s.commit()
    out = await _claim(SessionLocal, sid)
    assert out.startswith("dispatched")
    async with SessionLocal() as s:
        stored = (await repo.get_internal(s, sid)).intake_gate
    assert stored["approval"]["id"] == snap_b["id"], "B is what dispatched, not A"
    assert stored["superseded"]["id"] == snap_a["id"]
    assert stored["superseded"]["status"] == ap.INVALIDATED     # kept as audit evidence


async def test_an_invalidated_or_stale_snapshot_refuses(SessionLocal):
    sid, sel, snap = await _seed(SessionLocal)
    async with SessionLocal() as s:
        gate = dict((await repo.get_internal(s, sid)).intake_gate)
        gate["approval"] = ap.invalidate(snap, "user corrected it")
        await repo.set_intake_gate(s, sid, gate)
        await s.commit()
    assert (await _claim(SessionLocal, sid)).startswith("refused")

    # and a snapshot the user has since talked past (an extra message) is stale
    sid2, sel2, snap2 = await _seed(SessionLocal)
    async with SessionLocal() as s:
        row = await repo.get_internal(s, sid2)
        row.messages = list(row.messages) + [{"role": "user", "content": "actually wait"}]
        await s.commit()
    assert (await _claim(SessionLocal, sid2)).startswith("refused")


# the TYPED approved fidelity survives the durable round trip and binds the run
async def test_the_durable_approval_record_carries_the_typed_mesh_detail(SessionLocal):
    sid, sel, snap = await _seed(SessionLocal, mesh_fidelity="draft")
    async with SessionLocal() as s:
        stored = (await repo.get_internal(s, sid)).intake_gate["approval"]
    ic = stored["intent_canonical"]
    assert ic["requested_mesh_fidelity"] == "draft"       # the user's own choice
    assert ic["effective_mesh_fidelity"] == "draft"
    assert ic["mesh_fidelity_source"] == "user"
    assert ic["fidelity_policy_version"]
    assert stored["intent_fingerprint"] == at.fingerprint(ic)
    # the prose digest is present under its OWN name, distinct from the typed tier
    assert ic["approved_request_digest"] == at.approved_request_digest(_PAYLOAD["request_txt"])
    assert "high" not in (ic["requested_mesh_fidelity"], ic["effective_mesh_fidelity"])


async def test_a_fidelity_substitution_is_refused_against_the_durable_record(SessionLocal):
    sid, sel, _ = await _seed(SessionLocal)
    async with SessionLocal() as s:
        stored = (await repo.get_internal(s, sid)).intake_gate["approval"]

    approved_fp = stored["intent_fingerprint"]
    executed = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={"element_order": "2"},
        requested_mesh_fidelity="max",                         # the ONLY difference
        request_txt=_PAYLOAD["request_txt"], source_ref=None)
    assert at.fingerprint(executed) != approved_fp
    drifted = sorted(k for k in set(executed) | set(stored["intent_canonical"])
                     if executed.get(k) != stored["intent_canonical"].get(k))
    assert drifted == ["effective_mesh_fidelity", "requested_mesh_fidelity"]


async def test_each_declared_tier_round_trips_distinctly_and_default_differs(SessionLocal):
    from meshpipeline.pipeline.enums import MeshFidelity
    seen: dict[str, str] = {}
    for f in MeshFidelity:
        sid, sel, snap = await _seed(SessionLocal, mesh_fidelity=f.value)
        async with SessionLocal() as s:
            stored = (await repo.get_internal(s, sid)).intake_gate["approval"]
        assert stored["intent_canonical"]["effective_mesh_fidelity"] == f.value
        assert stored["intent_canonical"]["mesh_fidelity_source"] == "user"
        seen[f"user:{f.value}"] = stored["intent_fingerprint"]
    sid, sel, snap = await _seed(SessionLocal, mesh_fidelity="")   # user said nothing
    async with SessionLocal() as s:
        stored = (await repo.get_internal(s, sid)).intake_gate["approval"]
    ic = stored["intent_canonical"]
    assert ic["requested_mesh_fidelity"] is None and ic["mesh_fidelity_source"] == "default"
    seen["default:standard"] = stored["intent_fingerprint"]
    assert len(set(seen.values())) == len(seen)                # all four distinct
