# Responsibility: Verify the repair node moves an investigation's per-turn trace out of the report
#                 and into the job's object storage, leaving its location and the outcome table.
# Boundaries: hermetic - a recording object store double; no kernel, no model, no bucket.
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline import artifact_keys
from meshpipeline.contracts import object_storage
from meshpipeline.pipeline import repair_attempt


class _Store:
    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        if self.fail:
            raise OSError("bucket says no")
        self.objects[object_key] = local_path.read_bytes()
        return None


def _trace():
    return {"outcomes": [{"region": 0, "end": "runaway", "why_no_package": "runaway"}],
            "lines": [{"job_id": "", "region": 0, "kind": "call", "tool": "edit"},
                      {"job_id": "", "region": 0, "kind": "end", "end": "runaway"}],
            "truncated": False}


def test_trace_key_is_content_addressed_under_the_job():
    assert artifact_keys.investigation_trace_key("j1", "ab" * 32) == \
        "jobs/j1/investigation-trace-" + "ab" * 8 + ".jsonl"


def test_park_uploads_the_lines_and_leaves_their_location(monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)
    measured = {"investigation_trace": _trace()}
    asyncio.run(repair_attempt._park_trace("j1", measured))
    parked = measured["investigation_trace"]
    assert "lines" not in parked
    assert parked["outcomes"][0]["end"] == "runaway"
    key = parked["transcript"]["object_key"]
    assert key.startswith("jobs/j1/investigation-trace-") and key.endswith(".jsonl")
    assert parked["transcript"]["lines"] == 2
    lines = [json.loads(x) for x in store.objects[key].decode().splitlines()]
    assert [ln["job_id"] for ln in lines] == ["j1", "j1"]


def test_park_records_a_failed_upload_and_never_raises(monkeypatch):
    monkeypatch.setattr(object_storage, "_store", _Store(fail=True))
    measured = {"investigation_trace": _trace()}
    asyncio.run(repair_attempt._park_trace("j1", measured))
    parked = measured["investigation_trace"]
    assert "lines" not in parked
    assert "OSError" in parked["transcript"]["error"]


@pytest.mark.parametrize("measured", [{}, {"investigation_trace": None},
                                      {"investigation_trace": {"outcomes": [], "lines": []}}])
def test_park_without_lines_writes_nothing(monkeypatch, measured):
    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)
    asyncio.run(repair_attempt._park_trace("j1", measured))
    assert store.objects == {}


# ---------------------------------------------------------------- through the real node

from tests.unit.pipeline.test_repair_attempt import (  # noqa: E402
    _execution_publisher,  # noqa: F401 - autouse fixture
    _looped,
    _opted_in,  # noqa: F401 - autouse fixture
    _run,
    _state,
)


def test_a_refused_repair_parks_its_trace_and_keeps_the_outcomes(tmp_path, monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)

    def _refuse(source, destination, **kw):
        raise repair_attempt_refused("nothing kept", measurements={"investigation_trace": _trace()})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    out = _run(_state(tmp_path))
    parked = out["repair_attempt"]["detail"]["measurements"]["investigation_trace"]
    assert "lines" not in parked
    assert parked["outcomes"][0]["why_no_package"] == "runaway"
    assert parked["transcript"]["object_key"] in store.objects


def test_a_promoted_repair_parks_its_trace(tmp_path, monkeypatch):
    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)
    _looped(monkeypatch, stopped_because="resolved", resolved=True, complete_scan=True,
            refusals=[], planner="regions-investigator-v1", operations_attempted=0,
            operations_kept=0, investigation_trace=_trace())
    attempt = _run(_state(tmp_path))["repair_attempt"]
    parked = attempt["measurements"]["investigation_trace"]
    assert "lines" not in parked
    assert parked["transcript"]["object_key"] in store.objects


def repair_attempt_refused(message, *, measurements):
    from meshpipeline.cad.repair.conservative import RepairRefused
    return RepairRefused(message, measurements=measurements)


# ---------------------------------------------------------------- never on the critical path

class _SlowStore(_Store):
    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        import time
        time.sleep(1.0)
        return super().upload_file(local_path=local_path, object_key=object_key)


def test_a_stalled_upload_is_abandoned_at_its_timeout_and_recorded(monkeypatch):
    """Greptile: the upload sat outside the repair's wait_for with no timeout of its own, so a
    stalled store held the job between repair and mesh."""
    import time
    monkeypatch.setattr(object_storage, "_store", _SlowStore())
    monkeypatch.setattr(repair_attempt, "TRACE_UPLOAD_TIMEOUT_S", 0.05)
    measured = {"investigation_trace": _trace()}
    started = time.monotonic()
    asyncio.run(repair_attempt._park_trace("j1", measured))
    assert time.monotonic() - started < 0.9
    parked = measured["investigation_trace"]
    assert "lines" not in parked
    assert "TimeoutError" in parked["transcript"]["error"]
    assert "object_key" not in parked["transcript"]


def test_the_upload_timeout_is_short():
    assert 0 < repair_attempt.TRACE_UPLOAD_TIMEOUT_S <= 15


# ---------------------------------------------------------------- the mesher handback's trace

def _handback_state(tmp_path):
    from tests._geometry_support import geometry_state
    return {"job_id": "job-hb", "engine": "gmsh", "geometry": geometry_state(tmp_path)}


def test_a_refused_handback_parks_its_trace_in_its_report(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.conservative import RepairRefused
    from meshpipeline.pipeline import repair_handback as HB

    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)

    def _refuse(source, destination, **kw):
        raise RepairRefused("nothing kept", measurements={"investigation_trace": _trace()})

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _refuse)
    out = asyncio.run(HB._scoped_repair(_handback_state(tmp_path), focus_faces=(3,)))
    parked = out["report"]["investigation_trace"]
    assert "lines" not in parked
    assert parked["outcomes"][0]["why_no_package"] == "runaway"
    assert parked["transcript"]["object_key"] in store.objects


def test_a_handback_that_repaired_keeps_its_trace_in_its_summary(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport
    from meshpipeline.pipeline import repair_handback as HB

    store = _Store()
    monkeypatch.setattr(object_storage, "_store", store)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)

    def _repair(source, destination, **kw):
        destination.write_bytes(source.read_bytes() + b"\n/* repaired */\n")
        return RepairReport(defects=(), operations=(), summary="ok", entities=(),
                            measurements=(RepairMeasurement(name="investigation_trace",
                                                            value=_trace()),))

    monkeypatch.setattr("meshpipeline.cad.repair.conservative.repair_step_file", _repair)
    monkeypatch.setattr("meshpipeline.pipeline.repair_promote.stages_for",
                        lambda geometry, engine="", budget_s=None: (True, ""))
    out = asyncio.run(HB._scoped_repair(_handback_state(tmp_path), focus_faces=(3,)))
    parked = out["report"]["investigation_trace"]
    assert "lines" not in parked
    assert parked["transcript"]["object_key"] in store.objects
