# Responsibility: Verify the approval route renders each outcome status and holds no transaction of its own.
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.api.v1 import chat

SESSION_ID = uuid.uuid4()


def _session(**kw):
    base = {"job_id": None, "intake_gate": None, "geometry_source_id": uuid.uuid4(),
            "messages": [], "llm_metadata": []}
    base.update(kw)
    return SimpleNamespace(**base)


async def _route(monkeypatch, outcome=None, raises=None):
    async def _confirm(*a, **k):
        if raises is not None:
            raise ap.ApprovalTransactionError(raises)
        return outcome
    monkeypatch.setattr(ap, "confirm_pending_approval", _confirm)
    return await chat._confirm_pending_approval(_session(), session_repo=None,
                                                owner_id="alice", session_id=SESSION_ID)


# success shapes

async def test_a_dispatched_run_renders_done_with_its_job_id(monkeypatch):
    jid = uuid.uuid4()
    resp = await _route(monkeypatch, ap.ConfirmOutcome(
        ap.ConfirmStatus.dispatched, "Mesh generation accepted and queued for w.step. "
        f"Job ID: {jid}", job_id=jid, dispatched=True))
    assert resp.done is True and str(resp.job_id) == str(jid)
    assert "accepted and queued" in resp.reply


async def test_an_idempotent_repeat_renders_the_same_shape(monkeypatch):
    jid = uuid.uuid4()
    resp = await _route(monkeypatch, raises=ap.ConfirmOutcome(
        ap.ConfirmStatus.already_running,
        f"Mesh generation is already running for this session. Job ID: {jid}", job_id=jid))
    assert resp.done is True and str(resp.job_id) == str(jid)


async def test_an_already_dispatched_snapshot_renders_done(monkeypatch):
    jid = str(uuid.uuid4())
    resp = await _route(monkeypatch, raises=ap.ConfirmOutcome(
        ap.ConfirmStatus.already_dispatched, "That configuration has already been dispatched.",
        job_id=jid))
    assert resp.done is True and str(resp.job_id) == str(jid)


# status mapping

@pytest.mark.parametrize("status,code", [
    (ap.ConfirmStatus.no_geometry,       400),
    (ap.ConfirmStatus.no_confirmed_unit, 400),
    (ap.ConfirmStatus.source_expired,    410),
    (ap.ConfirmStatus.quota_exceeded,    429),
    (ap.ConfirmStatus.inconsistent,      500),
    (ap.ConfirmStatus.dispatch_failed,   500),
])
async def test_each_refusal_maps_to_its_established_status(monkeypatch, status, code):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _route(monkeypatch, raises=ap.ConfirmOutcome(status, "public message"))
    assert ei.value.status_code == code
    assert ei.value.detail == "public message"


async def test_an_expired_source_is_410_and_says_to_upload_again(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _route(monkeypatch, raises=ap.ConfirmOutcome(
            ap.ConfirmStatus.source_expired,
            "This geometry was removed after its retention period. Upload the file again to "
            "start a new run."))
    assert ei.value.status_code == 410
    assert "upload the file again" in ei.value.detail.lower()


async def test_a_recoverable_refusal_is_a_conversational_reply_not_an_error(monkeypatch):
    resp = await _route(monkeypatch, raises=ap.ConfirmOutcome(
        ap.ConfirmStatus.refused, "I did not start anything: the engine changed."))
    assert resp.awaiting_confirmation is False
    assert resp.done is not True
    assert "I did not start anything" in resp.reply


async def test_every_outcome_status_is_rendered_by_the_route(monkeypatch):
    from fastapi import HTTPException

    for status in ap.ConfirmStatus:
        try:
            resp = await _route(monkeypatch, raises=ap.ConfirmOutcome(status, "m"))
        except HTTPException as exc:
            assert exc.status_code in (400, 410, 429, 500), status
        else:
            assert resp is not None, f"{status} rendered nothing"


# ownership

def test_the_route_delegates_and_keeps_no_transaction():
    src = inspect.getsource(chat._confirm_pending_approval)
    assert "confirm_pending_approval" in src, "the route no longer delegates"
    for owned_elsewhere in ("get_for_update", "job_repo.create", "link_job", "set_intake_gate",
                            "dispatch_pipeline", "check_quotas", "purged_at", "fingerprint",
                            "builder_payload", "GeometrySourceRepository"):
        assert owned_elsewhere not in src, (
            f"the route still performs approval transaction work: {owned_elsewhere}")


def test_the_whole_module_holds_no_second_approval_transaction():
    src = inspect.getsource(chat)
    assert "get_for_update" not in src, "chat.py still locks the session row itself"
    assert "dispatch_contract" not in src, "chat.py still builds a dispatch payload"
    # the definition, the docstring reference, the delegation call, and the one call site
    assert src.count("confirm_pending_approval") <= 4, (
        "more than one confirmation path exists in the route module")


def test_the_status_table_is_the_routes_only_approval_policy():
    assert set(chat._APPROVAL_STATUS) <= set(ap.ConfirmStatus)
    assert chat._APPROVAL_STATUS[ap.ConfirmStatus.source_expired] == 410
