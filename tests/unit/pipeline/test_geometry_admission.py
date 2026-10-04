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
    upload = tmp_path / "lumen.step"
    upload.write_text("")   # existence only - staging is mocked
    # staging just materialises input.stl; the measured checks are mocked
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: True)

    out = _run({**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.step")})
    assert out["executor_success"] is False
    assert out["geometry_unsuitable_reason"].startswith("[GEOMETRY_UNSUITABLE]")
    assert "self-intersect" in out["geometry_unsuitable_reason"]
    # unfixable by any retry → exhaust the budget so the graph ends and the app reports it
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1


def test_admits_a_clean_input(tmp_path, monkeypatch):
    upload = tmp_path / "lumen.step"
    upload.write_text("")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: False)

    out = _run({**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.step")})
    assert out == {}   # admitted (declared + measured both clean) → proceed to the builder


def test_a_declared_rejection_from_post_intake_drift_blocks_the_builder(tmp_path, monkeypatch):
    upload = tmp_path / "lumen.step"
    upload.write_text("")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface",
                        _fake_prepare)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface", lambda *_a, **_k: dict(_SCALES))
    monkeypatch.setattr("meshpipeline.cad.surface_checks.self_intersects", lambda *_a, **_k: False)  # geometry FINE
    drifted = {**_VMTK_STATE, "geometry": geometry_state(tmp_path, filename="lumen.step"),
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
        assert _run({"job_id": "t", "engine": eng, "geometry": geometry_state(tmp_path, filename="lumen.step")}) == {}
    assert tripped["v"] is False


def test_never_blocks_on_a_missing_upload(tmp_path, monkeypatch):
    tripped = {"v": False}
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _fake_prepare)
    assert _run({"job_id": "t", "engine": "vmtk", "geometry": {}}) == {}
    assert _run({"job_id": "t", "engine": "vmtk",
                 "geometry": stale_geometry_state(tmp_path)}) == {}
    assert tripped["v"] is False


_INTERNAL_PORTS = [{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                   {"name": "outlet", "type": "outlet"}]


def _tripwired(monkeypatch) -> dict:
    tripped = {"v": False}

    def _tripwire(*_a, **_k):
        tripped["v"] = True
        raise AssertionError("a refused-by-design input was staged")
    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _tripwire)
    return tripped


@pytest.mark.parametrize("engine", ["snappy", "cfmesh", "vmtk", "gmsh"])
def test_a_surface_for_internal_flow_is_refused_by_design_before_anything_is_built(
        tmp_path, monkeypatch, engine):
    # the aorta STL of 2026-10-03: no internal-flow engine on main takes a surface, so each one
    # refuses it HERE - nothing staged, nothing built - with the facts the message is told from
    tripped = _tripwired(monkeypatch)
    out = _run({"job_id": "t", "engine": engine, "purpose": "internal_cfd",
                "input_kind": "fluid-domain", "dimensionality": "3D",
                "intake_patches": _INTERNAL_PORTS,
                "engine_params": ec.resolve_engine_params(engine, {}),
                "geometry": geometry_state(tmp_path, filename="aorta.stl")})
    assert tripped["v"] is False
    assert out["executor_success"] is False
    facts = out["executor_failure_facts"]
    assert facts["codes"] == ["geometry_form_unsupported"] and facts["phases"] == ["declared"]
    assert facts["refused_by_design"] is True and facts["before_meshing"] is True
    assert (facts["flow"], facts["form"], facts["engine"]) == ("internal", "surface", engine)
    assert facts["able"] == [], "an engine that cannot take an STL was named as able to"
    label = ec.engine_label(engine)
    assert out["geometry_unsuitable_reason"].startswith(
        f"{label} cannot mesh internal flow from a surface mesh")
    assert "crash" not in out["geometry_unsuitable_reason"].lower()


@pytest.mark.parametrize("engine,purpose,filename", [
    ("snappy", "external_cfd", "car.stl"), ("cfmesh", "external_cfd", "car.stl"),
    ("snappy", "internal_cfd", "duct.step"), ("cfmesh", "internal_cfd", "duct.igs")])
def test_a_file_the_engine_declares_it_takes_passes_the_form_check(tmp_path, monkeypatch,
                                                                   engine, purpose, filename):
    _tripwired(monkeypatch)        # these engines measure nothing: no staging either way
    patches = (_INTERNAL_PORTS if purpose == "internal_cfd" else
               [{"name": "car", "type": "wall"}, {"name": "farfield", "type": "farfield"}])
    assert _run({"job_id": "t", "engine": engine, "purpose": purpose,
                 "input_kind": "body-surface", "dimensionality": "3D", "intake_patches": patches,
                 "engine_params": {}, "geometry": geometry_state(tmp_path, filename=filename)}) == {}


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
