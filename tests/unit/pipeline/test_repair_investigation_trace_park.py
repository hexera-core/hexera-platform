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
