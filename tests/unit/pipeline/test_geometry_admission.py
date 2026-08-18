# Responsibility: Verify measured geometry admission blocks the builder only where an engine declares that contract.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests._geometry_support import geometry_state, stale_geometry_state

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.engines import registry as ec  # noqa: E402
from meshpipeline.pipeline.geometry_admission import node_geometry_admission  # noqa: E402


def _fake_prepare(geometry, destination, *, engine=""):

    from tests._geometry_support import prepared_surface
    Path(destination).write_text("")
    return prepared_surface(destination)

_SCALES = {"diag": 1.0, "extent": [1.0, 1.0, 1.0], "min_feature": 0.01,
           "thin_gap": 0.1, "n_triangles": 10, "surface_area": 3.0}

# A COMPLETE, valid declared context for vmtk - so the node (which now blocks on ALL admit
# rejections, declared or measured) sees no spurious declared rejection and the test isolates
# the measured phase. Mirrors what intake threads into state.
_VMTK_STATE = {
    "job_id": "t", "engine": "vmtk", "purpose": "internal_cfd",
    "input_kind": "body-surface", "dimensionality": "3D",
    "intake_patches": [{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                       {"name": "outlet", "type": "outlet"}],
    "engine_params": {"wall_layers": "on"},
}


def _run(state):
    return asyncio.run(node_geometry_admission(state))


def test_rejects_a_self_intersecting_input_before_the_builder(tmp_path, monkeypatch):
    upload = tmp_path / "lumen.vtp"
    upload.write_text("")   # existence only - staging is mocked
    # staging just materialises input.stl; the measured checks are mocked
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: True)

    out = _run({**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.vtp")})
    assert out["executor_success"] is False
    assert out["geometry_unsuitable_reason"].startswith("[GEOMETRY_UNSUITABLE]")
    assert "self-intersect" in out["geometry_unsuitable_reason"]
    # unfixable by any retry → exhaust the budget so the graph ends and the app reports it
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1


def test_admits_a_clean_input(tmp_path, monkeypatch):
    upload = tmp_path / "lumen.vtp"
    upload.write_text("")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: False)

    out = _run({**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.vtp")})
    assert out == {}   # admitted (declared + measured both clean) → proceed to the builder


def test_a_declared_rejection_from_post_intake_drift_blocks_the_builder(tmp_path, monkeypatch):
    upload = tmp_path / "lumen.vtp"
    upload.write_text("")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: False)  # geometry FINE
    drifted = {**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.vtp"),
               "engine_params": {"wall_layers": "on", "topology": "internal"}}  # unknown param injected
    out = _run(drifted)
    assert out["executor_success"] is False
    assert "topology" in out["geometry_unsuitable_reason"]   # the injected unknown param blocked it
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1


def test_is_a_free_no_op_for_engines_without_a_measured_contract(tmp_path, monkeypatch):
    tripped = {"v": False}
    def _tripwire(*_a, **_k):
        tripped["v"] = True
        return True
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _tripwire)
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", _tripwire)
    upload = tmp_path / "body.stl"
    upload.write_text("")
    for eng in ("snappy", "cfmesh", "gmsh"):
        assert _run({"job_id": "t", "engine": eng, "geometry": geometry_state(tmp_path, filename="lumen.vtp")}) == {}
    assert tripped["v"] is False


def test_never_blocks_on_a_missing_upload(tmp_path, monkeypatch):
    tripped = {"v": False}
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _fake_prepare)
    assert _run({"job_id": "t", "engine": "vmtk", "geometry": {}}) == {}
    assert _run({"job_id": "t", "engine": "vmtk",
                 "geometry": stale_geometry_state(tmp_path)}) == {}
    assert tripped["v"] is False


def test_the_admission_reason_matches_the_engine_spec_contract():
    spec_reason = ec.get_spec("vmtk").geometry_unsuitable({"self_intersecting": True})
    assert spec_reason.startswith("[GEOMETRY_UNSUITABLE]") and "self-intersect" in spec_reason


# These nodes publish through the ownership-checked port. The suites here exercise node
# contracts, not Redis or PostgreSQL, so the port is stood in for; ownership has its own suites.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    import meshpipeline.pipeline.executor as _ex
    if hasattr(_ex, "execution_publisher"):
        monkeypatch.setattr(_ex, "execution_publisher", _ep.execution_publisher)
    return made
