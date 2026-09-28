# Responsibility: Verify every failure class tells the user whose problem it is and what to do next, and only the domain classes carry a reason.
# Boundaries: the copy and its classification; no transport, no store.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.errors import FailureClass, failed_reason_for, user_message_for

# The dead-end audit of 2026-09-28: an expired upload read as "on our side, not your geometry,
# try again", a self-intersecting surface as "did not meet the required quality checks", and a
# reaped job closed with "Job already failed." Each sentence below is the honest replacement.


# whose problem it is, and the next step

def test_an_expired_upload_is_the_users_next_step_not_our_outage():
    msg = user_message_for(
        FailureClass.USER_INPUT,
        reason="this geometry was removed after its retention period, so it is no longer "
               "available to mesh")
    assert "not our systems" in msg
    assert "removed after its retention period" in msg
    assert "Upload the file again" in msg and "start a new run" in msg
    assert "on our side" not in msg and "try again in a few minutes" not in msg


def test_a_rejected_geometry_says_fix_the_cad_and_re_upload():
    msg = user_message_for(FailureClass.DOMAIN_REJECTED,
                           reason="the surface self-intersects (12 faces)")
    assert "CAD file" in msg and "not our systems" in msg
    assert "self-intersects" in msg
    assert msg.endswith("Fix the geometry and upload it again.")
    assert "quality checks" not in msg


def test_a_lost_worker_is_ours_and_the_run_can_be_started_again():
    msg = user_message_for(FailureClass.WORKER_LOST)
    assert "worker" in msg and "lost" in msg
    assert "on our side" in msg and "starting a new run" in msg
    assert FailureClass.WORKER_LOST.is_system
    assert not FailureClass.WORKER_LOST.is_retryable


def test_domain_messages_read_without_a_reason():
    for fc in (FailureClass.USER_INPUT, FailureClass.DOMAIN_REJECTED):
        msg = user_message_for(fc)
        assert "not our systems" in msg and msg.endswith(".")
        assert ": ." not in msg and ":." not in msg, msg


def test_a_reason_is_folded_in_once_and_ends_its_clause():
    msg = user_message_for(FailureClass.USER_INPUT, reason="  no confirmed   unit.  ")
    assert "systems: no confirmed unit. Upload the file again" in msg
    assert msg.count("no confirmed unit") == 1


# the redaction boundary

@pytest.mark.parametrize("fc", [fc for fc in FailureClass
                                if fc not in (FailureClass.USER_INPUT, FailureClass.DOMAIN_REJECTED)])
def test_only_the_domain_classes_ever_carry_the_reason(fc):
    leak = "s3://prod-geometry/sources/abc AccessDenied arn:aws:iam::9:user/svc"
    assert leak not in user_message_for(fc, reason=leak)
    assert user_message_for(fc, reason=leak) == user_message_for(fc)


def test_no_class_is_left_to_the_internal_fallback():
    internal = user_message_for(FailureClass.INTERNAL)
    for fc in FailureClass:
        if fc is not FailureClass.INTERNAL:
            assert user_message_for(fc) != internal, f"{fc} falls through to the INTERNAL text"


def test_the_coarse_db_reason_is_total_and_valid():
    valid = {"api_failure", "unhandled", "reviewer_rejected", "mesh_generation"}
    for fc in FailureClass:
        assert failed_reason_for(fc) in valid, fc
    assert failed_reason_for(FailureClass.DOMAIN_REJECTED) == "mesh_generation"
    assert failed_reason_for(FailureClass.WORKER_LOST) == "unhandled"


# the refusal at execution preparation publishes the honest sentence

class _Log:
    def error(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass


class _S:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None


class _Repo:
    def __init__(self): self.row = type("R", (), {"failed_reason": None})()
    async def transition(self, db, jid, status):
        from meshpipeline.persistence.job_state import TransitionResult
        return TransitionResult.applied
    async def get_internal(self, db, jid): return self.row


async def _refuse(monkeypatch, exc):
    from meshpipeline.application import geometry_materializer as gm

    async def _classify(thread): return "pending"

    async def _materialize(src, interp, *, job_id):
        raise exc

    monkeypatch.setattr(gm, "prepare_execution_geometry", _materialize)
    published: list[str] = []
    out = await gm.prepare_for_execution(
        lambda: _S(), job_id=str(uuid.uuid4()), geometry_source=object(),
        geometry_interpretation=object(), classify_checkpoint=_classify, checkpoint_thread="t",
        job_repo=_Repo(), jlog=_Log(), publish=published.append)
    assert not out.prepared
    return published


async def test_an_expired_upload_refusal_publishes_the_re_upload_sentence(monkeypatch):
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    published = await _refuse(monkeypatch, GeometrySourceError(
        "this geometry was removed after its retention period, so it is no longer available "
        "to mesh", failure_class=FailureClass.USER_INPUT))
    assert len(published) == 1
    text = published[0]
    assert "not our systems" in text and "retention period" in text
    assert "Upload the file again and start a new run." in text
    assert "on our side" not in text


async def test_a_missing_unit_refusal_names_the_unit_not_an_outage(monkeypatch):
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    published = await _refuse(monkeypatch, GeometrySourceError(
        "this geometry has no confirmed unit, so its physical size is unknown",
        failure_class=FailureClass.USER_INPUT))
    assert "no confirmed unit" in published[0] and "not our systems" in published[0]


async def test_a_storage_refusal_still_leaks_nothing(monkeypatch):
    from meshpipeline.contracts.geometry_source import GeometrySourceError
    published = await _refuse(monkeypatch, GeometrySourceError(
        "s3://prod-geometry/sources/abc AccessDenied arn:aws:iam::9:user/svc",
        failure_class=FailureClass.DEPENDENCY_DOWN))
    blob = published[0].lower()
    for leak in ("s3://", "accessdenied", "arn:", "sources/"):
        assert leak not in blob
    assert "our side" in blob
