# Responsibility: Verify mesh-intake repair inspection records a report without touching the run's geometry.
from __future__ import annotations

import asyncio

import pytest
from tests._geometry_support import geometry_state

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
)
from meshpipeline.pipeline.repair_inspect import node_repair_inspect


def _run(state):
    return asyncio.run(node_repair_inspect(state))


def _result(status=RepairStatus.clean, defects=()):
    from meshpipeline.cad.repair.inspect import repair_input_for
    return lambda geometry, **kw: RepairResult(
        status=status,
        input=repair_input_for(geometry),
        policy=RepairPolicy(mode=RepairMode.inspect, profile=RepairProfile.conservative,
                            target=kw.get("target", RepairTarget.meshing),
                            engine=kw.get("engine", "")),
        report=RepairReport(defects=tuple(defects), measurements=(),
                            operations=({"name": "inspect", "mutated": False},),
                            summary="inspected"),
        output=None,
    )


def test_records_the_report_and_leaves_geometry_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry", _result())
    state = {"job_id": "t", "engine": "snappy",
             "geometry": geometry_state(tmp_path)}

    out = _run(state)

    assert out["repair_status"] == RepairStatus.clean.value
    assert out["repair_report"]["report"]["summary"] == "inspected"
    # THE INVARIANT this node exists under: inspection is diagnostics. The geometry handle the
    # admission gate and the builder read must be the one the upload verified, byte for byte.
    assert "geometry" not in out


def test_inspects_for_the_chosen_engine_as_a_meshing_intake(tmp_path, monkeypatch):
    seen: dict = {}

    def _capture(geometry, **kw):
        seen.update(kw)
        return _result()(geometry, **kw)

    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry", _capture)
    _run({"job_id": "t", "engine": "vmtk", "geometry": geometry_state(tmp_path)})

    assert seen["engine"] == "vmtk"
    assert seen["target"] is RepairTarget.meshing
    assert seen["profile"] is RepairProfile.conservative


def test_a_defective_input_is_reported_but_never_rejected_here(tmp_path, monkeypatch):
    defect = RepairDefect(code=DefectCode.wire_gap, severity=DefectSeverity.error,
                          message="a wire has a gap")
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry",
                        _result(RepairStatus.repairable, (defect,)))

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    assert out["repair_status"] == RepairStatus.repairable.value
    assert out["repair_report"]["report"]["defects"][0]["code"] == DefectCode.wire_gap.value
    # admission is the only gate that refuses an input; this node never ends a run
    assert "geometry_unsuitable_reason" not in out
    assert "executor_success" not in out


def test_an_inspection_that_falls_over_is_a_service_failure_not_bad_cad(tmp_path, monkeypatch):
    def _boom(geometry, **kw):
        raise RuntimeError("occt unavailable")

    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry", _boom)

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    # INCONCLUSIVE, not unrepairable: our tooling failed, so the file is not accused of anything.
    assert out["repair_status"] == RepairStatus.inconclusive.value
    assert out["repair_report"]["service_failure"]
    assert "geometry_unsuitable_reason" not in out


def test_no_materialised_geometry_leaves_the_run_exactly_as_it_was(monkeypatch):
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry",
                        lambda *a, **k: pytest.fail("nothing to inspect"))
    assert _run({"job_id": "t", "engine": "snappy"}) == {}


def test_a_geometry_whose_bytes_are_gone_is_not_inspected(tmp_path, monkeypatch):
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry",
                        lambda *a, **k: pytest.fail("nothing to inspect"))
    state = {"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)}
    (tmp_path / "part.step").unlink()

    assert _run(state) == {}
