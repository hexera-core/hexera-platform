# Responsibility: Verify workload evidence and profile telemetry observe, but never alter, provider execution.
from __future__ import annotations

import json
import logging
import struct

import pytest

from meshpipeline.application import mesh_execution_shadow as shadow
from meshpipeline.application.mesh_execution_profiles import MeshExecutionProfile
from meshpipeline.application.mesh_execution_shadow import (
    run_with_shadow_profile,
    workload_from_workspace,
)
from meshpipeline.contracts.mesh_execution import note_native_payload


def _binary_stl(path, triangles: int = 2) -> int:
    data = b"shadow-profile".ljust(80, b"\0") + struct.pack("<I", triangles) + b"\0" * (50 * triangles)
    path.write_bytes(data)
    return len(data)


def _record(caplog):
    message = next(r.getMessage() for r in caplog.records
                   if r.getMessage().startswith("mesh execution profile shadow: "))
    return json.loads(message.split(": ", 1)[1])


def test_declared_geometry_and_nested_plan_become_workload_evidence(tmp_path):
    source_bytes = _binary_stl(tmp_path / "part.stl")
    (tmp_path / "plan.json").write_text(json.dumps({
        "limits": {"max_cells": 12_000_000},
        "mesh": {"n_layers": 8},
        "regions": {"region_count": 3},
    }))
    note_native_payload(tmp_path, ["part.stl", "plan.json"])

    workload = workload_from_workspace(tmp_path, "snappy")

    assert workload.source_bytes == source_bytes
    assert workload.surface_triangles == 2
    assert workload.requested_cells == 12_000_000
    assert workload.region_count == 3
    assert workload.boundary_layers == 8


def test_undeclared_workspace_is_not_scanned(tmp_path):
    _binary_stl(tmp_path / "part.stl", triangles=500)

    workload = workload_from_workspace(tmp_path, "snappy")

    assert workload.source_bytes is None
    assert workload.surface_triangles is None


def test_malformed_evidence_is_ignored_without_losing_valid_geometry(tmp_path):
    source_bytes = _binary_stl(tmp_path / "part.stl")
    (tmp_path / "plan.json").write_text("not-json")
    note_native_payload(tmp_path, ["part.stl", "plan.json"])

    workload = workload_from_workspace(tmp_path, "gmsh")

    assert workload.source_bytes == source_bytes
    assert workload.requested_cells is None


def test_json_read_is_bounded_by_encoded_bytes(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "padding": "é" * 1_100_000,
        "max_cells": 99_000_000,
    }, ensure_ascii=False))
    note_native_payload(tmp_path, ["plan.json"])

    assert workload_from_workspace(tmp_path, "gmsh").requested_cells is None


def test_shadow_record_contains_recommendation_and_actuals_without_altering_result(tmp_path, caplog):
    _binary_stl(tmp_path / "part.stl")
    (tmp_path / "plan.json").write_text(json.dumps({"max_cells": 12_000_000}))
    note_native_payload(tmp_path, ["part.stl", "plan.json"])
    result = {"rc": 0, "timed_out": False, "quality": {"cells": 11_500_000},
              "provider_reference": "op-1"}

    with caplog.at_level(logging.INFO):
        observed = run_with_shadow_profile(tmp_path, "gmsh", lambda: result)

    assert observed is result
    record = _record(caplog)
    assert record["policy_version"] == "mesh-execution-profile-v1"
    assert record["recommended_profile"] == MeshExecutionProfile.HIGH_MEMORY
    assert record["actual"]["cells"] == 11_500_000
    assert record["actual"]["rc"] == 0
    assert record["actual"]["timed_out"] is False
    assert record["actual"]["elapsed_seconds"] >= 0


def test_shadow_record_uses_remote_quality_when_result_was_not_collected(tmp_path, caplog):
    result = {
        "rc": -3,
        "timed_out": False,
        "remote_quality": {"cells": 7_000_000},
    }

    with caplog.at_level(logging.INFO):
        observed = run_with_shadow_profile(tmp_path, "snappy", lambda: result)

    assert observed is result
    assert _record(caplog)["actual"]["cells"] == 7_000_000


def test_provider_exception_is_recorded_and_reraised_unchanged(tmp_path, caplog):
    error = RuntimeError("provider down")

    def fail():
        raise error

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError) as caught:
        run_with_shadow_profile(tmp_path, "cfmesh", fail)

    assert caught.value is error
    assert _record(caplog)["actual"]["exception"] == "RuntimeError"


def test_observation_failure_does_not_block_provider(tmp_path, caplog):
    (tmp_path / ".native_payload").write_text(json.dumps(["missing.stl"]))
    result = {"rc": 0, "timed_out": False}

    with caplog.at_level(logging.WARNING):
        observed = run_with_shadow_profile(tmp_path, "gmsh", lambda: result)

    assert observed is result
    assert "observation failed" in caplog.text


def test_warning_handler_failure_does_not_block_provider(tmp_path, monkeypatch):
    (tmp_path / ".native_payload").write_text(json.dumps(["missing.stl"]))
    result = {"rc": 0, "timed_out": False}

    monkeypatch.setattr(shadow.logger, "warning", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("logging down")))

    assert run_with_shadow_profile(tmp_path, "gmsh", lambda: result) is result


def test_logging_failure_does_not_replace_successful_result(tmp_path, monkeypatch):
    result = {"rc": 0, "timed_out": False}
    monkeypatch.setattr(shadow, "_emit", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("logging down")))
    monkeypatch.setattr(shadow.logger, "warning", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("warning down")))

    assert run_with_shadow_profile(tmp_path, "gmsh", lambda: result) is result


def test_logging_failure_does_not_replace_provider_exception(tmp_path, monkeypatch):
    provider_error = RuntimeError("provider down")
    monkeypatch.setattr(shadow, "_emit", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("logging down")))
    monkeypatch.setattr(shadow.logger, "warning", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("warning down")))

    with pytest.raises(RuntimeError) as caught:
        run_with_shadow_profile(tmp_path, "gmsh", lambda: (_ for _ in ()).throw(provider_error))

    assert caught.value is provider_error


def test_cloud_run_executor_emits_shadow_record_on_its_real_run_path(tmp_path, monkeypatch, caplog):
    from meshpipeline.adapters.mesh_execution import cloud_run_client

    _binary_stl(tmp_path / "part.stl")
    note_native_payload(tmp_path, ["part.stl"])
    result = {"rc": 0, "timed_out": False, "cells": 2_000}
    calls = []

    def run_mesh_remote(workspace, **kwargs):
        calls.append((workspace, kwargs))
        return result

    monkeypatch.setattr(cloud_run_client, "run_mesh_remote", run_mesh_remote)

    with caplog.at_level(logging.INFO):
        observed = cloud_run_client.CloudRunMeshExecutor().run(
            tmp_path, engine="snappy", timeout=60, operation_key="operation-1")

    assert observed is result
    assert calls == [(tmp_path, {
        "engine": "snappy", "timeout": 60, "operation_key": "operation-1",
    })]
    assert _record(caplog)["actual"]["cells"] == 2_000
