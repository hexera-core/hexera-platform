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


# BOUNDS AND LEAKS - inspection is evidence and must never cost a run its deadline or its privacy


def test_a_file_too_large_to_inspect_is_not_inspected_and_says_so(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_INSPECT_MAX_BYTES", 10)
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry",
                        lambda *a, **k: pytest.fail("a file over the bound must not be read"))

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    # reporting it clean would be worse than reporting that we did not look
    assert out["repair_status"] == RepairStatus.inconclusive.value
    assert out["repair_report"]["service_failure"] == "not_inspected_too_large"
    assert out["repair_report"]["limit_bytes"] == 10


def test_an_inspection_that_runs_too_long_is_abandoned(tmp_path, monkeypatch):
    import time

    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_INSPECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry",
                        lambda *a, **k: time.sleep(5))

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    # the run goes on; a pathological part must not spend the deadline proving it is pathological
    assert out["repair_status"] == RepairStatus.inconclusive.value
    assert out["repair_report"]["service_failure"] == "inspection_timed_out"


def test_a_failure_never_puts_a_worker_path_in_the_customers_report(tmp_path, monkeypatch):
    secret = "/srv/workspaces/job-42/private/part.step"

    def _raise(*a, **k):
        raise FileNotFoundError(secret)

    monkeypatch.setattr("meshpipeline.cad.repair.inspect.inspect_geometry", _raise)

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    # THE REPORT IS DOWNLOADABLE BY THE JOB'S OWNER, so it carries a code and the type - never the
    # kernel's sentence, which names paths on our workers.
    report = out["repair_report"]
    assert report["service_failure"] == "inspection_failed"
    assert report["error_type"] == "FileNotFoundError"
    assert secret not in str(report)
    assert "srv" not in str(report)


def test_an_inspection_dependency_that_cannot_import_does_not_fail_the_run(tmp_path, monkeypatch):
    import sys

    import meshpipeline.cad.repair as repair_pkg

    # BOTH the module cache and the package attribute: `from pkg import mod` finds the attribute
    # the first import left on the package, so clearing sys.modules alone changes nothing. This is
    # the shape of a worker whose inspection-only dependency is unavailable.
    monkeypatch.setitem(sys.modules, "meshpipeline.cad.repair.inspect", None)
    monkeypatch.delattr(repair_pkg, "inspect", raising=False)

    out = _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    # the inspector is loaded INSIDE the guard, so a worker missing an inspection-only dependency
    # still meshes sound geometry
    assert out["repair_status"] == RepairStatus.inconclusive.value
    # ModuleNotFoundError, which is an ImportError - the point is it was CAUGHT, not which
    # subclass the interpreter chose
    assert out["repair_report"]["error_type"].endswith("Error")
    assert "NotFound" in out["repair_report"]["error_type"] or \
        out["repair_report"]["error_type"] == "ImportError"
