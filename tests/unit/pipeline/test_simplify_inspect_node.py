# Responsibility: Verify the mesh run's simplification inspection records a report without touching the run's geometry, and that every way it can fail is recorded as inconclusive while the run goes on.
from __future__ import annotations

import asyncio

from tests._geometry_support import geometry_state

from meshpipeline.cad.simplify.contracts import (
    SimplifyMode,
    SimplifyPolicy,
    SimplifyProfile,
    SimplifyReport,
    SimplifyResult,
    SimplifyStatus,
    SimplifyTarget,
)
from meshpipeline.pipeline.simplify_inspect import node_simplify_inspect


def _run(state):
    return asyncio.run(node_simplify_inspect(state))


def _result(status=SimplifyStatus.none):
    from meshpipeline.cad.repair.inspect import repair_input_for
    return lambda geometry, **kw: SimplifyResult(
        status=status,
        input=repair_input_for(geometry),
        policy=SimplifyPolicy(mode=SimplifyMode.inspect, profile=SimplifyProfile.inspect_only,
                              target=kw.get("target", SimplifyTarget.meshing), engine=kw.get("engine", "")),
        report=SimplifyReport(candidates=(), summary="inspected"),
    )


def test_records_the_report_and_leaves_geometry_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr("meshpipeline.cad.simplify.inspect.inspect_features", _result())
    out = _run({"job_id": "t", "engine": "gmsh", "geometry": geometry_state(tmp_path)})

    assert out["simplify_status"] == SimplifyStatus.none.value
    assert out["simplify_report"]["report"]["summary"] == "inspected"
    # THE INVARIANT: inspection is diagnostics; the handle admission and the builder read is unchanged
    assert "geometry" not in out


def test_inspects_for_the_chosen_engine_as_a_meshing_intake(tmp_path, monkeypatch):
    seen: dict = {}

    def _capture(geometry, **kw):
        seen.update(kw)
        return _result()(geometry, **kw)

    monkeypatch.setattr("meshpipeline.cad.simplify.inspect.inspect_features", _capture)
    _run({"job_id": "t", "engine": "snappy", "geometry": geometry_state(tmp_path)})

    assert seen == {"target": SimplifyTarget.meshing, "engine": "snappy"}


def test_an_inspection_that_raises_is_inconclusive_and_names_no_path(tmp_path, monkeypatch):
    def _boom(geometry, **kw):
        raise FileNotFoundError("/worker/local/secret/path.step")

    monkeypatch.setattr("meshpipeline.cad.simplify.inspect.inspect_features", _boom)
    out = _run({"job_id": "t", "geometry": geometry_state(tmp_path)})

    assert out["simplify_status"] == SimplifyStatus.inconclusive.value
    assert out["simplify_report"] == {"service_failure": "inspection_failed", "error_type": "FileNotFoundError"}


def test_a_file_over_the_size_bound_is_not_inspected_and_says_so(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_simplify as scfg

    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_INSPECT_MAX_BYTES", 10)
    called = []
    monkeypatch.setattr("meshpipeline.cad.simplify.inspect.inspect_features",
                        lambda *a, **k: called.append(1))
    out = _run({"job_id": "t", "geometry": geometry_state(tmp_path)})

    assert not called
    assert out["simplify_status"] == SimplifyStatus.inconclusive.value
    assert out["simplify_report"]["service_failure"] == "not_inspected_too_large"


def test_a_slow_inspection_is_abandoned_on_its_clock(tmp_path, monkeypatch):
    import time

    import meshpipeline.settings.cad_simplify as scfg

    monkeypatch.setattr(scfg, "CAD_SIMPLIFY_INSPECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("meshpipeline.cad.simplify.inspect.inspect_features",
                        lambda *a, **k: time.sleep(0.5))
    out = _run({"job_id": "t", "geometry": geometry_state(tmp_path)})

    assert out["simplify_status"] == SimplifyStatus.inconclusive.value
    assert out["simplify_report"]["service_failure"] == "inspection_timed_out"


def test_no_geometry_on_disk_says_nothing(tmp_path):
    assert _run({"job_id": "t"}) == {}
