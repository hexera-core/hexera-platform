# Responsibility: Verify the geometry measurement is off by default, invisible when off, and never able to fail an upload.
# Boundaries: the upload seam and the measurement's own decisions; the measurement package itself is not installed here.
from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from meshpipeline.api.v1.upload import router as upload_router

_app = FastAPI()
_app.include_router(upload_router, prefix="/api/v1/upload")

_VALID_STEP = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('test'),'2;1');\nENDSEC;\n"
               b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n")
_SESSION_ID = uuid.UUID("12345678-1234-4234-a234-123456789abc")


@asynccontextmanager
async def _mock_get_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    yield db


def _svc():
    svc = MagicMock()
    svc.check_quotas = AsyncMock()
    svc.create_session = AsyncMock(return_value=_SESSION_ID)
    return svc


async def _upload(tmp_path, *, filename: str = "wing.step", body: bytes = _VALID_STEP):
    with (
        patch("meshpipeline.persistence.session.get_db", _mock_get_db),
        patch("meshpipeline.application.job_service.JobService", return_value=_svc()),
        patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path),
    ):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            return await c.post("/api/v1/upload/step-file",
                                files={"file": (filename, body, "application/octet-stream")})


# OFF BY DEFAULT, AND INVISIBLE WHEN OFF


def test_the_setting_is_off_in_a_deployment_that_says_nothing():
    import meshpipeline.settings.policy as polcfg
    assert polcfg.GEOMETRY_MEASUREMENT_ENABLED is False


async def test_with_the_setting_off_the_upload_never_reaches_the_measurement_at_all(tmp_path):
    """Not "it does nothing": it is not called. The gate is read in the route, so an image without
    the measurement package imports nothing and an operator who turned nothing on pays nothing."""
    name = "meshpipeline.application.geometry_measurement"
    already = sys.modules.pop(name, None)
    try:
        with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", False):
            resp = await _upload(tmp_path)
        assert name not in sys.modules, (
            "the upload imported the measurement module with the feature switched off")
    finally:
        if already is not None:
            sys.modules[name] = already
    assert resp.status_code == 200


async def test_with_the_setting_off_the_response_is_byte_for_byte_what_it_was(tmp_path):
    """The whole claim, stated as an assertion: with the feature off, an upload returns exactly the
    payload it returned before this path existed. The session id is the only value that moves, and
    it is fixed by the double."""
    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT

    with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", False):
        resp = await _upload(tmp_path)
    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": str(_SESSION_ID),
        "step_filename": "wing.step",
        "intake_greeting": UPLOAD_ACKNOWLEDGEMENT,
        "trace": [],
    }


async def test_the_refusals_in_front_of_the_measurement_still_come_first(tmp_path):
    """A file refused at upload is refused before anything measures it, with the feature on or off:
    the measurement is keyed to durable bytes, and a refused upload has none."""
    with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True):
        with patch("meshpipeline.application.geometry_measurement.on_upload",
                   new=AsyncMock(side_effect=AssertionError("measured a refused upload"))):
            bad = await _upload(tmp_path, body=b"PK\x03\x04 not a step file")
            empty = await _upload(tmp_path, body=b"")
    assert bad.status_code == 400
    assert empty.status_code == 422


# WITH IT ON, NOTHING IT DOES CAN COST AN UPLOAD


async def test_with_the_setting_on_a_measurement_that_explodes_changes_nothing(tmp_path):
    from meshpipeline.api.v1.upload import UPLOAD_ACKNOWLEDGEMENT

    boom = AsyncMock(side_effect=RuntimeError("the measurement package fell over"))
    with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True):
        with patch("meshpipeline.application.geometry_measurement.on_upload", new=boom):
            resp = await _upload(tmp_path)
    assert boom.await_count == 1
    assert resp.status_code == 200
    assert resp.json()["intake_greeting"] == UPLOAD_ACKNOWLEDGEMENT
    assert resp.json()["step_filename"] == "wing.step"


async def test_with_the_setting_on_the_measurement_is_handed_the_stored_bytes(tmp_path):
    seen = AsyncMock(return_value="measured")
    with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True):
        with patch("meshpipeline.application.geometry_measurement.on_upload", new=seen):
            resp = await _upload(tmp_path)
    assert resp.status_code == 200
    (source_id, owner_id), kwargs = seen.await_args
    assert uuid.UUID(source_id)
    assert owner_id
    assert kwargs["size_bytes"] == len(_VALID_STEP)


# THE SYNCHRONOUS / ASYNCHRONOUS SPLIT


# A CORRECTED FILE, MID-CONVERSATION


async def test_a_corrected_file_is_a_new_session_with_its_own_measurement(tmp_path):
    """The upload route creates a session on every upload and nothing rebinds a session's source,
    so a second file is a second session measuring its own bytes. There is no stale measurement to
    invalidate, and the guard in the contract is what keeps it that way if rebinding ever lands."""
    seen = AsyncMock(return_value="measured")
    svc = _svc()
    with (patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True),
          patch("meshpipeline.application.geometry_measurement.on_upload", new=seen),
          patch("meshpipeline.persistence.session.get_db", _mock_get_db),
          patch("meshpipeline.application.job_service.JobService", return_value=svc),
          patch("meshpipeline.api.v1.upload._JOBS_DIR", tmp_path)):
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            first = await c.post("/api/v1/upload/step-file",
                                 files={"file": ("a.step", _VALID_STEP, "application/octet-stream")})
            second = await c.post("/api/v1/upload/step-file",
                                  files={"file": ("a.step", _VALID_STEP + b"\n",
                                                  "application/octet-stream")})
    assert first.status_code == 200 and second.status_code == 200
    assert svc.create_session.await_count == 2, "an upload that did not open a session would rebind one"
    ids = [call.args[0] for call in seen.await_args_list]
    assert len(ids) == 2 and ids[0] != ids[1], "two uploads measured the same source"


async def test_nothing_happens_at_all_when_the_feature_is_off():
    from meshpipeline.application.geometry_measurement import on_upload

    with patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", False):
        assert await on_upload("some-id", "owner", size_bytes=10) == "off"


async def test_a_small_file_is_measured_inside_the_request():
    from meshpipeline.application import geometry_measurement as gm

    with (patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True),
          patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 4.0),
          patch.object(gm, "_run_in_thread", return_value={"status": "ok"}) as ran):
        assert await gm.on_upload("s", "o", size_bytes=1024) == "measured"
    assert ran.call_count == 1


async def test_a_large_file_goes_to_a_worker_and_the_request_returns():
    from meshpipeline.application import geometry_measurement as gm

    with (patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True),
          patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 4.0),
          patch.object(gm, "_run_in_thread", side_effect=AssertionError("measured inline")),
          patch("meshpipeline.application.geometry_measurement.enqueue_measurement",
                return_value=True) as queued):
        assert await gm.on_upload("s", "o", size_bytes=64 * 1024 * 1024) == "queued"
    assert queued.call_count == 1


async def test_a_queue_that_will_not_take_it_is_a_measurement_that_does_not_happen():
    from meshpipeline.application import geometry_measurement as gm

    with (patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True),
          patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 4.0),
          patch("meshpipeline.application.geometry_measurement.enqueue_measurement",
                return_value=False)):
        assert await gm.on_upload("s", "o", size_bytes=64 * 1024 * 1024) == "skipped"


async def test_an_inline_measurement_that_raises_is_swallowed_by_the_caller():
    from meshpipeline.application import geometry_measurement as gm

    with (patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_ENABLED", True),
          patch("meshpipeline.settings.policy.GEOMETRY_MEASUREMENT_SYNC_MAX_MB", 4.0),
          patch.object(gm, "_run_in_thread", side_effect=RuntimeError("native fault"))):
        assert await gm.on_upload("s", "o", size_bytes=1024) == "skipped"


def test_the_inline_path_is_bounded_by_a_ceiling_a_customer_would_wait_for():
    from meshpipeline.application import geometry_measurement as gm

    assert 0 < gm.SYNCHRONOUS_DEADLINE_CEILING_S <= 120


# THE MEASUREMENT PACKAGE IS NOT IN THIS IMAGE, AND THAT IS AN OUTCOME


def test_an_absent_measurement_package_is_a_refusal_that_says_so(tmp_path):
    """The distribution is pinned and separate and an image need not carry it. Its absence is
    recorded as a refusal with a reason, not swallowed and not raised: an operator who switched the
    feature on and saw nothing appear can read why."""
    from meshpipeline.application import geometry_measurement as gm
    from meshpipeline.contracts.geometry_measurement import (
        MEASUREMENT_SCHEMA,
        STATUS_REFUSED,
    )

    target = tmp_path / "part.step"
    target.write_bytes(_VALID_STEP)
    with patch.object(gm, "_package", side_effect=gm._Unavailable("No module named 'geometry_agent'")):
        doc = gm.measure_local_file(target)
    assert doc["schema"] == MEASUREMENT_SCHEMA
    assert doc["status"] == STATUS_REFUSED
    assert doc["reason"] == gm.PACKAGE_ABSENT
    assert doc["plan"] is None


def test_a_measurement_that_breaks_on_the_file_is_a_failure_not_a_refusal(tmp_path):
    from meshpipeline.application import geometry_measurement as gm
    from meshpipeline.contracts.geometry_measurement import STATUS_MEASUREMENT_FAILED

    target = tmp_path / "part.step"
    target.write_bytes(_VALID_STEP)
    fake_measure = MagicMock()
    fake_measure.measure_isolated.side_effect = RuntimeError("the tessellation died")
    run = MagicMock(file_ceiling_bytes=lambda: 0)
    with patch.object(gm, "_package", return_value=(MagicMock(), run, fake_measure)):
        doc = gm.measure_local_file(target)
    assert doc["status"] == STATUS_MEASUREMENT_FAILED
    assert "tessellation died" in doc["reason"]
    assert "\n" not in doc["reason"], "a stored reason is one sentence, never a stack trace"


def test_a_file_over_the_packages_own_ceiling_is_refused_unopened_in_the_packages_own_words(tmp_path):
    from meshpipeline.application import geometry_measurement as gm
    from meshpipeline.contracts.geometry_measurement import STATUS_REFUSED

    target = tmp_path / "huge.step"
    target.write_bytes(b"x" * 4096)
    opened = MagicMock()
    opened.measure_isolated.side_effect = AssertionError("opened a file it had refused")
    run = MagicMock(file_ceiling_bytes=lambda: 1024,
                    REFUSED_TOO_LARGE="the file is {mb:.1f} MiB, over the {cap:.0f} MiB ceiling ({var})",
                    MAX_FILE_MB_ENV="GEOMETRY_AGENT_MAX_FILE_MB")
    with patch.object(gm, "_package", return_value=(MagicMock(), run, opened)):
        doc = gm.measure_local_file(target)
    assert doc["status"] == STATUS_REFUSED
    assert "GEOMETRY_AGENT_MAX_FILE_MB" in doc["reason"]


# THE STORED DOCUMENT IS NEVER READ AGAINST THE WRONG BYTES


def _row(sha: str, document: dict | None = None):
    row = MagicMock()
    row.sha256 = sha
    row.document = document if document is not None else {"schema": "geometry_agent.measurement.v1",
                                                          "status": "ok", "projection": {}}
    return row


def test_no_row_means_not_attempted_and_not_a_verdict():
    from meshpipeline.contracts.geometry_measurement import document_for, projection_of

    assert document_for(None, sha256="a" * 64) is None
    assert projection_of(None) is None


def test_a_document_for_different_bytes_is_refused_rather_than_used():
    """A corrected re-upload is the case this exists for. A port table measured off the file the
    customer replaced is worse than no table, and the mismatch is data integrity: reaching the same
    row again cannot produce a different answer."""
    from meshpipeline.contracts.geometry_measurement import (
        MeasurementMismatch,
        document_for,
    )
    from meshpipeline.errors import FailureClass

    with pytest.raises(MeasurementMismatch):
        document_for(_row("a" * 64), sha256="b" * 64)
    assert MeasurementMismatch.failure_class is FailureClass.DATA_INTEGRITY
    assert FailureClass.DATA_INTEGRITY.is_retryable is False


def test_a_document_of_another_schema_is_refused():
    from meshpipeline.contracts.geometry_measurement import MeasurementMismatch, document_for

    with pytest.raises(MeasurementMismatch):
        document_for(_row("a" * 64, {"schema": "something.else.v1"}), sha256="a" * 64)


def test_the_matching_document_is_returned():
    from meshpipeline.contracts.geometry_measurement import document_for

    doc = document_for(_row("a" * 64), sha256="a" * 64)
    assert doc is not None and doc["status"] == "ok"


@pytest.mark.parametrize("status", ["refused", "measurement_failed"])
def test_a_failed_measurement_reaches_admission_as_a_status_not_as_an_empty_dict(status):
    """`engines/base.py:430-434` tests only that `surface_analysis is not None`, so an empty dict
    passes the gate carrying nothing. Three situations had one value; they now have three."""
    from meshpipeline.contracts.geometry_measurement import projection_of

    out = projection_of({"schema": "geometry_agent.measurement.v1", "status": status,
                         "reason": "because"})
    assert out == {"status": status, "reason": "because"}


def test_a_successful_measurement_reaches_admission_in_the_platforms_own_key_names():
    from meshpipeline.contracts.geometry_measurement import projection_of

    out = projection_of({"schema": "geometry_agent.measurement.v1", "status": "ok",
                         "projection": {"region_count": 2, "region_names": ["a", "b"],
                                        "self_intersecting": "unknown", "diag": 1.0,
                                        "thin_gap": 0.01}})
    assert out["status"] == "ok"
    assert out["region_count"] == 2 and out["region_names"] == ["a", "b"]
    # never None: `None` reads as "not self intersecting" to a dict .get, and nothing measured it
    assert out["self_intersecting"] == "unknown"


def test_an_enqueue_with_nothing_bound_is_a_no_op_rather_than_a_failure():
    from meshpipeline.contracts import geometry_measurement as gmc

    previous = gmc._enqueuer
    try:
        gmc.set_measurement_enqueuer(None)
        assert gmc.enqueue_measurement("s", "o", purpose="internal_cfd") is False
        gmc.set_measurement_enqueuer(MagicMock(side_effect=RuntimeError("broker down")))
        assert gmc.enqueue_measurement("s", "o", purpose="internal_cfd") is False
    finally:
        gmc.set_measurement_enqueuer(previous)
