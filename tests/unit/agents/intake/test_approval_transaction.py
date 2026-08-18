# Responsibility: Verify one live approval creates exactly one job, and every refusal leaves nothing half-approved.
# Boundaries: the approval authority alone; the session, job and source repositories are doubles.
from __future__ import annotations

import time
import uuid

import pytest

from meshpipeline.agents.intake import approval as ap

OWNER = "owner-1"
SESSION_ID = uuid.uuid4()


class _Log:
    def __init__(self): self.infos = []; self.warnings = []; self.errors = []
    def info(self, *a, **k): self.infos.append(a)
    def warning(self, *a, **k): self.warnings.append(a)
    def error(self, *a, **k): self.errors.append(a)


class _Ref:
    def __init__(self, sid): self.source_id = str(sid); self.original_filename = "wing.step"
    def to_payload(self): return {"source_id": self.source_id, "sha256": "a" * 64}


class _InterpRef:
    def __init__(self): self.interpretation_id = str(uuid.uuid4())
    def to_payload(self): return {"interpretation_id": self.interpretation_id}


class _Session:
    def __init__(self, *, job_id=None, gate=None, source_id=None):
        self.job_id = job_id
        self.intake_gate = gate or {}
        self.geometry_source_id = source_id or uuid.uuid4()
        self.messages = [{"role": "user", "content": "yes"}]
        self.llm_metadata = []


class _Db:
    def __init__(self, rec): self.rec = rec
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): self.rec.commits += 1


class _Rec:

    def __init__(self):
        self.commits = 0; self.jobs = 0; self.dispatches = 0
        self.store_reads = 0; self.gate_writes = []; self.links = []
        self.request_txt_writes = 0; self.launch_failures = 0


class _SessionRepo:
    def __init__(self, rec, locked): self.rec = rec; self.locked = locked
    async def get_for_update(self, db, sid): return self.locked
    async def set_intake_gate(self, db, sid, gate): self.rec.gate_writes.append(gate)
    async def link_job(self, db, sid, jid): self.rec.links.append(jid)
    async def set_request_txt(self, db, sid, txt): self.rec.request_txt_writes += 1


class _JobRepo:
    def __init__(self, rec): self.rec = rec
    async def create(self, db, *, owner_id):
        self.rec.jobs += 1
        return type("J", (), {"id": uuid.uuid4(), "geometry_source_id": None,
                              "geometry_interpretation_id": None})()
    async def mark_launch_failed(self, db, jid, detail): self.rec.launch_failures += 1


class _JobService:
    def __init__(self, quota_error=None): self.quota_error = quota_error
    async def check_quotas(self, db, owner_id):
        if self.quota_error:
            raise ValueError(self.quota_error)


class _SourceRepo:
    def __init__(self, rec, purged=False, missing=False):
        self.rec = rec; self.purged = purged; self.missing = missing
    async def get_for_owner(self, db, sid, owner):
        self.rec.store_reads += 1
        if self.missing:
            return None
        return type("R", (), {"purged_at": time.time() if self.purged else None})()


def _snapshot(**over):
    s = {"id": str(uuid.uuid4()), "status": ap.AWAITING, "owner_id": OWNER,
         "session_id": str(SESSION_ID), "fingerprint": "fp-approved",
         "intent_fingerprint": "ifp-approved", "intent_canonical": {"engine": "cfmesh"},
         "payload": {"mesh_engine": "cfmesh", "purpose": "external_cfd"}}
    s.update(over)
    return s


@pytest.fixture
def wired(monkeypatch):
    rec = _Rec()
    src_id = uuid.uuid4()

    async def _src(db, session, owner): return _Ref(src_id)
    async def _interp(db, session, owner): return _InterpRef()
    async def _dispatch(db, jid, payload): rec.dispatches += 1

    import meshpipeline.application.geometry_materializer as gm
    monkeypatch.setattr(gm, "source_ref_for_session", _src)
    monkeypatch.setattr(gm, "interpretation_ref_for_session", _interp)
    # both bindings agree by default; individual tests break one
    monkeypatch.setattr(ap, "assert_payload_matches_approval", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_build_dispatch_payload", lambda **k: {"job_id": str(k["job_id"])})
    rec.dispatch = _dispatch
    rec.source_id = src_id
    return rec


async def _confirm(rec, *, session=None, locked=None, purged=False, missing_source=False,
                   quota_error=None, dispatch=None, logger=None):
    session = session or _Session(source_id=rec.source_id)
    locked = locked if locked is not None else _Session(
        gate={"approval": _snapshot(), "selection": {"id": "sel"}}, source_id=rec.source_id)
    return await ap.confirm_pending_approval(
        session, _SessionRepo(rec, locked), OWNER, SESSION_ID, logger=logger or _Log(),
        sessions=lambda: _Db(rec), job_service=_JobService(quota_error),
        job_repo=_JobRepo(rec),
        source_repo=_SourceRepo(rec, purged=purged, missing=missing_source),
        dispatch=dispatch or rec.dispatch)


# normal approval

async def test_a_live_approval_creates_one_job_and_dispatches_once(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    out = await _confirm(wired)
    assert out.status is ap.ConfirmStatus.dispatched and out.dispatched
    assert wired.jobs == 1 and wired.dispatches == 1 and len(wired.links) == 1
    assert "wing.step" in out.message and str(out.job_id) in out.message


async def test_the_snapshot_is_marked_dispatched_and_consent_is_disarmed(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    await _confirm(wired)
    assert wired.gate_writes and wired.gate_writes[-1]["approval"]["status"] == ap.DISPATCHED
    assert wired.request_txt_writes == 1, (
        "consent was not disarmed - a later affirmative message could re-dispatch")


# idempotency / races

async def test_a_session_already_linked_never_re_dispatches(wired):
    out = await _confirm(wired, session=_Session(job_id=uuid.uuid4(), source_id=wired.source_id))
    assert out.status is ap.ConfirmStatus.already_running and out.ok
    assert wired.jobs == 0 and wired.dispatches == 0


async def test_the_locked_check_is_authoritative_when_the_fast_path_missed_it(wired):
    linked = _Session(job_id=uuid.uuid4(), gate={"approval": _snapshot()}, source_id=wired.source_id)
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired, locked=linked)
    assert ei.value.outcome.status is ap.ConfirmStatus.already_running
    assert wired.jobs == 0 and wired.dispatches == 0, "the race loser created a second paid job"


async def test_an_already_dispatched_snapshot_makes_no_second_job(wired):
    jid = str(uuid.uuid4())
    locked = _Session(gate={"approval": _snapshot(status=ap.DISPATCHED, job_id=jid)},
                      source_id=wired.source_id)
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired, locked=locked)
    out = ei.value.outcome
    assert out.status is ap.ConfirmStatus.already_dispatched and out.ok and out.job_id == jid
    assert wired.jobs == 0 and wired.dispatches == 0


# source eligibility

async def test_a_purged_source_is_refused_before_any_dispatch(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired, purged=True)
    out = ei.value.outcome
    assert out.status is ap.ConfirmStatus.source_expired
    assert "upload the file again" in out.message.lower()
    assert wired.dispatches == 0, "a run was dispatched for bytes that no longer exist"


async def test_a_session_without_geometry_is_refused_before_the_transaction(wired):
    s = _Session(source_id=wired.source_id)
    s.geometry_source_id = None
    out = await _confirm(wired, session=s)
    assert out.status is ap.ConfirmStatus.no_geometry
    assert wired.jobs == 0 and wired.store_reads == 0, (
        "the object store was read for a session with no geometry")


async def test_an_unconfirmed_unit_is_refused(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    import meshpipeline.application.geometry_materializer as gm

    async def _none(db, session, owner): return None
    monkeypatch.setattr(gm, "interpretation_ref_for_session", _none)
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired)
    assert ei.value.outcome.status is ap.ConfirmStatus.no_confirmed_unit
    assert wired.dispatches == 0


# intent consistency

async def test_a_snapshot_that_no_longer_verifies_is_refused_and_invalidated(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (False, "the engine changed since approval"))
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired)
    out = ei.value.outcome
    assert out.status is ap.ConfirmStatus.refused
    assert "the engine changed since approval" in out.message
    assert wired.jobs == 0 and wired.dispatches == 0
    assert wired.gate_writes, "the stale snapshot was not invalidated"


async def test_a_payload_that_drifts_from_the_approval_never_dispatches(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))

    def _mismatch(*a, **k):
        raise ap.ApprovalTransactionError(ap.ConfirmOutcome(
            ap.ConfirmStatus.inconsistent, "Internal consistency error: ... Nothing was started."))
    monkeypatch.setattr(ap, "assert_payload_matches_approval", _mismatch)
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired)
    assert ei.value.outcome.status is ap.ConfirmStatus.inconsistent
    assert wired.dispatches == 0, "a drifted payload was dispatched"


# failures

async def test_a_quota_refusal_creates_no_job(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired, quota_error="monthly job limit reached")
    assert ei.value.outcome.status is ap.ConfirmStatus.quota_exceeded
    assert wired.dispatches == 0


async def test_a_dispatch_failure_is_recorded_and_never_reported_as_success(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))

    async def _boom(db, jid, payload): raise RuntimeError("broker unreachable")
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await _confirm(wired, dispatch=_boom)
    out = ei.value.outcome
    assert out.status is ap.ConfirmStatus.dispatch_failed and not out.ok
    assert "nothing is running" in out.message
    assert wired.launch_failures == 1, "the job was left looking launched forever"


async def test_a_transaction_failure_before_job_creation_leaves_nothing_half_approved(wired, monkeypatch):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))

    class _Failing(_JobRepo):
        async def create(self, db, *, owner_id): raise RuntimeError("db down")

    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await ap.confirm_pending_approval(
            _Session(source_id=wired.source_id), _SessionRepo(wired, _Session(
                gate={"approval": _snapshot(), "selection": {}}, source_id=wired.source_id)),
            OWNER, SESSION_ID, logger=_Log(), sessions=lambda: _Db(wired),
            job_service=_JobService(), job_repo=_Failing(wired),
            source_repo=_SourceRepo(wired), dispatch=wired.dispatch)
    assert ei.value.outcome.status is ap.ConfirmStatus.dispatch_failed
    assert wired.dispatches == 0
    assert not any(g.get("approval", {}).get("status") == ap.DISPATCHED
                   for g in wired.gate_writes), "the snapshot was marked dispatched anyway"


# leakage

@pytest.mark.parametrize("secret", ["a" * 64, "s3://", "minio", "bucket", "/srv/", "AKIA"])
async def test_no_storage_internal_reaches_a_public_message(wired, monkeypatch, secret):
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    messages = []
    for kw in ({"purged": True}, {"quota_error": "limit"}):
        try:
            out = await _confirm(wired, **kw)
            messages.append(out.message)
        except ap.ApprovalTransactionError as exc:
            messages.append(exc.outcome.message)
    for m in messages:
        assert secret not in m, f"a public message leaked {secret!r}: {m}"


def test_the_outcome_type_carries_no_storage_fields():
    fields = set(ap.ConfirmOutcome.__dataclass_fields__)
    for forbidden in ("source_id", "object_key", "bucket", "checksum", "sha256", "path",
                      "credential", "provider"):
        assert forbidden not in fields, f"ConfirmOutcome exposes {forbidden}"


# ownership

def test_the_authority_imports_no_http_layer():
    import inspect

    src = inspect.getsource(ap)
    for banned in ("fastapi", "HTTPException", "ChatResponse", "starlette", "meshpipeline.api"):
        assert banned not in src, f"the intake approval authority imports {banned}"


# the two bindings, unstubbed

class _At:

    def __init__(self, builder_fp, intent_fp):
        self.builder_fp, self.intent_fp = builder_fp, intent_fp
        self.calls = 0
    def canonical_payload(self, *a): return {"canonical": "builder"}
    def approved_intent_canonical(self, **k): return {"canonical": "intent"}
    def fingerprint(self, obj):
        self.calls += 1
        return self.builder_fp if obj == {"canonical": "builder"} else self.intent_fp


def _payload():
    return {"mesh_engine": "cfmesh", "purpose": "external_cfd", "input_kind": "solid-body",
            "dimensionality": "3D", "intake_patches": [], "engine_params": {},
            "requested_mesh_fidelity": None, "effective_mesh_fidelity": "standard",
            "mesh_fidelity_source": "default", "fidelity_policy_version": "3tier-v1",
            "request_txt": "mesh it"}


@pytest.fixture
def at(monkeypatch):
    def _install(builder_fp, intent_fp):
        stub = _At(builder_fp, intent_fp)
        import meshpipeline.agents.intake.admission_token as real
        for n in ("canonical_payload", "approved_intent_canonical", "fingerprint"):
            monkeypatch.setattr(real, n, getattr(stub, n))
        return stub
    return _install


def test_matching_bindings_admit_the_run(at):
    at("fp-approved", "ifp-approved")
    ap.assert_payload_matches_approval(_payload(), _snapshot(), _Ref(uuid.uuid4()), logger=_Log())


def test_a_builder_payload_that_drifts_from_the_preview_is_refused(at):
    at("fp-DIFFERENT", "ifp-approved")
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        ap.assert_payload_matches_approval(_payload(), _snapshot(), _Ref(uuid.uuid4()), logger=_Log())
    assert ei.value.outcome.status is ap.ConfirmStatus.inconsistent


def test_a_changed_intent_is_refused_against_the_DURABLE_record(at):
    at("fp-approved", "ifp-DIFFERENT")
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        ap.assert_payload_matches_approval(_payload(), _snapshot(), _Ref(uuid.uuid4()), logger=_Log())
    assert ei.value.outcome.status is ap.ConfirmStatus.inconsistent
    assert "Nothing was started" in ei.value.outcome.message


def test_a_snapshot_with_no_intent_fingerprint_is_refused(at):
    at("fp-approved", "ifp-approved")
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        ap.assert_payload_matches_approval(_payload(), _snapshot(intent_fingerprint=""),
                                           _Ref(uuid.uuid4()), logger=_Log())
    assert ei.value.outcome.status is ap.ConfirmStatus.inconsistent
    assert "incomplete" in ei.value.outcome.message


def test_a_refused_binding_never_names_the_drifted_values_publicly(at):
    at("fp-approved", "ifp-DIFFERENT")
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        ap.assert_payload_matches_approval(_payload(), _snapshot(), _Ref(uuid.uuid4()), logger=_Log())
    msg = ei.value.outcome.message
    assert "cfmesh" not in msg and "ifp-" not in msg
