# Responsibility: Verify the admission slot reads a stored verdict instead of computing one, still checks a job that never had one, and never blames a customer for a measurement of ours that failed.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.pipeline import geometry_admission as ga

# WHY THE SLOT STAYS. The measurement package refuses a bad file at step 2, seconds after the upload,
# so nothing that reaches dispatch should be a surprise. What this node is for is the job that got
# here WITHOUT step 2 - an older session, a week when the measurement was off - and deleting it would
# remove the only check those jobs get.
#
# The plan's summary said this node is a no-op because no engine declares a measured rule. That is
# wrong and the first test says so: `engines/vmtk/spec.py:190` declares
# `require_no_self_intersection=True` on an implemented engine.

STATE = {"job_id": "j", "engine": "vmtk", "purpose": "internal_cfd", "input_kind": "body-surface",
         # vmtk declares no intake param (its layer switch was one nothing honoured), so an
         # engine_params entry here would be an unknown-param declared rejection, not a fixture.
         "dimensionality": "3D", "intake_patches": [], "engine_params": {},
         "geometry": {"ref": {"source_id": "11111111-1111-4111-8111-111111111111",
                              "owner_id": "o", "object_key": "sources/x", "sha256": "b" * 64,
                              "size_bytes": 10, "original_filename": "p.stl",
                              "suffix_hint": ".stl"}}}

MEASURED_OK = {"status": "ok", "region_names": [], "region_count": 1, "region_source": "",
               "self_intersecting_state": "unknown", "diag": 2.62, "thin_gap": 0.03}


def test_an_engine_that_measures_really_does_exist():
    """The premise. If this ever becomes false the node IS a no-op and can be retired."""
    from meshpipeline.engines.registry import ENGINE_CATALOG

    measuring = [s.name for s in ENGINE_CATALOG.values()
                 if s.implemented and s.input_contract is not None
                 and (s.input_contract.require_no_self_intersection
                      or s.input_contract.min_thickness_ratio > 0)]
    assert measuring == ["vmtk"], measuring


def _silent_publish(monkeypatch):
    """The node publishes a rejection through the ownership-checked publisher, which a unit test has
    no execution to own. What is under test is the verdict, not the stream."""
    async def _quiet(*_a, **_k):
        return None

    monkeypatch.setattr(ga, "_publish", _quiet)


def _no_compute(monkeypatch):
    """Make any attempt to stage and probe a surface fail the test loudly."""
    def _boom(*_a, **_k):
        raise AssertionError("the slot computed a surface analysis instead of reading one")

    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _boom)
    monkeypatch.setattr("meshpipeline.cad.surface_checks.surface_analysis_for", _boom)


def _stored(monkeypatch, analysis):
    async def _read(_ref):
        return analysis

    monkeypatch.setattr("meshpipeline.cad.regions.reading_for_source", _read)


def test_it_reads_the_stored_verdict_and_does_not_compute_one(monkeypatch):
    _no_compute(monkeypatch)
    _stored(monkeypatch, MEASURED_OK)
    assert asyncio.run(ga.node_geometry_admission(dict(STATE))) == {}


def test_a_stored_verdict_that_rejects_still_stops_the_build(monkeypatch):
    """The slot is a check, not a formality: a measurement that says the surface self-intersects
    exhausts the budget and hands the reason to the executor short-circuit."""
    _silent_publish(monkeypatch)
    _no_compute(monkeypatch)
    _stored(monkeypatch, {**MEASURED_OK, "self_intersecting": True})
    out = asyncio.run(ga.node_geometry_admission(dict(STATE)))
    assert out["executor_success"] is False
    assert "self-intersect" in out["geometry_unsuitable_reason"]
    assert out["retry_count"] > 0


def test_an_unmeasured_self_intersection_flag_never_rejects(monkeypatch):
    """`"unknown"` is truthy. Passing the measurement package's own word through to `base.py:565`
    would reject every vmtk upload for a defect nothing looked for."""
    _no_compute(monkeypatch)
    _stored(monkeypatch, MEASURED_OK)
    assert asyncio.run(ga.node_geometry_admission(dict(STATE))) == {}


def test_a_measurement_that_failed_defers_and_never_blames_the_customer(monkeypatch):
    _no_compute(monkeypatch)
    _stored(monkeypatch, {"status": "measurement_failed", "reason": "the file could not be opened"})
    assert asyncio.run(ga.node_geometry_admission(dict(STATE))) == {}


def test_a_job_that_never_went_through_step_two_is_still_checked(monkeypatch):
    """The whole reason the slot stays: with nothing stored it stages and probes, exactly as today."""
    _silent_publish(monkeypatch)
    _stored(monkeypatch, None)
    probed: list = []

    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        lambda *a, **k: probed.append("staged") or object())
    monkeypatch.setattr("meshpipeline.cad.surface_checks.surface_analysis_for",
                        lambda *a, **k: {"self_intersecting": True, "diag": 1.0})

    class _Geometry:
        path = __file__          # any existing file: only `exists()` is consulted

    monkeypatch.setattr("meshpipeline.pipeline.geometry_state.materialized",
                        lambda _s: _Geometry())
    out = asyncio.run(ga.node_geometry_admission(dict(STATE)))
    assert probed == ["staged"]
    assert out["executor_success"] is False


@pytest.mark.parametrize("engine", ["snappy", "gmsh", "cfmesh"])
def test_an_engine_with_no_measured_rule_still_proceeds_instantly(monkeypatch, engine):
    """This keeps the gate free for the wrap-then-fill engines, which is every corpus case."""

    async def _never(_ref):
        raise AssertionError("a measurement was read for an engine that declares no measured rule")

    monkeypatch.setattr("meshpipeline.cad.regions.reading_for_source", _never)
    assert asyncio.run(ga.node_geometry_admission({**STATE, "engine": engine})) == {}


def test_a_read_that_raises_falls_through_to_the_probe(monkeypatch):
    """Nothing here fails a build. A reader that throws is a reader that produced nothing."""

    async def _boom(_ref):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr("meshpipeline.cad.regions.reading_for_source", _boom)
    monkeypatch.setattr("meshpipeline.pipeline.geometry_state.materialized", lambda _s: None)
    assert asyncio.run(ga.node_geometry_admission(dict(STATE))) == {}
