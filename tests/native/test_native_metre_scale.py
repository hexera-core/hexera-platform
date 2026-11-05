# Responsibility: Verify a genuinely meshed case measures the true physical size, whatever unit it was authored in.
from __future__ import annotations

from pathlib import Path

import pytest
from tests._geometry_support import materialized

from meshpipeline.cad.staging import prepare_surface
from meshpipeline.contracts.geometry_units import LengthUnit
from meshpipeline.engines.dispatch import run_engine_local

pytestmark = pytest.mark.native

#: A 20 mm sphere, expressed two ways. Physically identical; only the declaration differs.
EQUIVALENTS = [
    ("metre", LengthUnit.metre, 0.02),
    ("millimetre", LengthUnit.millimetre, 20.0),
]
DIAMETER_M = 0.02


def _sphere(path: Path, diameter: float) -> Path:
    import pyvista as pv

    sph = pv.Sphere(radius=diameter / 2, theta_resolution=20, phi_resolution=20).triangulate()
    path.parent.mkdir(parents=True, exist_ok=True)
    sph.save(str(path))
    return path


def _source(tmp_path, label, unit, coord_diameter):
    geom = materialized(tmp_path / f"src-{label}", unit=unit, filename="body.stl")
    _sphere(Path(geom.local_path), coord_diameter)
    return geom


def _extent(stl: Path) -> float:
    from meshpipeline.cad.stl_io import read_stl_triangles

    xs = [v[0] for t in read_stl_triangles(Path(stl)) for v in t]
    return max(xs) - min(xs)


def _meshed_bounds(ws: Path) -> tuple[float, float]:
    import json

    manifest = ws / "mesh_manifest.json"
    assert manifest.exists(), f"the bundle wrote no manifest under {ws}"
    data = json.loads(manifest.read_text())
    geom = data.get("geometry") or {}
    assert "box_xmin" in geom, f"manifest carries no meshed bounds: {sorted(data)}"
    # the bundle also states the unit it meshed in; a mesh that were not metres would say so
    assert data.get("mesh_units") == "m", f"bundle declared mesh_units={data.get('mesh_units')!r}"
    return float(geom["box_xmin"]), float(geom["box_xmax"])


@pytest.mark.parametrize("label,unit,coord_diameter", EQUIVALENTS)
def test_cfmesh_meshes_the_true_physical_size(label, unit, coord_diameter, tmp_path):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton

    ws = tmp_path / label
    ws.mkdir(parents=True)
    surface = prepare_surface(_source(tmp_path, label, unit, coord_diameter), ws / "input.stl")
    # a 20-facet sphere is chord-shortened by ~0.3%, so this is a scale check, not a
    # geometry check: 0.0199 m proves metres, 19.9 or 0.0000199 would not
    assert _extent(surface.path) == pytest.approx(DIAMETER_M, rel=5e-3)

    (ws / "flow_topology").write_text("external\n")
    (ws / "dimensionality").write_text("3D\n")
    write_case_skeleton(ws)
    cfg = R.configure_mesh(
        ws, geometry_file="input.stl", surface=surface, strategy={"max_cells": 40000},
        wall_patch="body",
        contract_patches=[{"name": "body", "type": "wall"},
                          {"name": "farfield", "type": "farfield"}],
        args={"domain_min": [-DIAMETER_M * 2] * 3, "domain_max": [DIAMETER_M * 2] * 3},
        cell_budget=200_000)
    assert cfg.get("success"), cfg

    res = run_engine_local(ws, engine="cfmesh", timeout=900)
    assert res["rc"] == 0 and not res["timed_out"], res.get("log_tail", "")[-800:]

    from meshpipeline.engines.cfmesh.finalize import finalize
    assert finalize(str(ws), intake_patches=[{"name": "body", "type": "wall"},
                                             {"name": "farfield", "type": "farfield"}],
                    engine="cfmesh", domain="sphere", flow_topology="external")["success"]
    lo, hi = _meshed_bounds(ws)
    # the domain encloses the body, so the mesh spans the requested metre domain
    assert (hi - lo) == pytest.approx(DIAMETER_M * 4, rel=0.2), (
        f"cfMesh produced a mesh spanning {hi - lo} m for a {DIAMETER_M} m body")
    assert abs(hi) < 1.0 and abs(lo) < 1.0, "mesh coordinates are not metre-scale"


def test_cfmesh_agrees_between_equivalent_units(tmp_path):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton

    spans = {}
    for label, unit, coord_diameter in EQUIVALENTS:
        ws = tmp_path / f"agree-{label}"
        ws.mkdir(parents=True)
        surface = prepare_surface(_source(tmp_path, f"a{label}", unit, coord_diameter),
                                  ws / "input.stl")
        (ws / "flow_topology").write_text("external\n")
        (ws / "dimensionality").write_text("3D\n")
        write_case_skeleton(ws)
        R.configure_mesh(ws, geometry_file="input.stl", surface=surface,
                         strategy={"max_cells": 40000}, wall_patch="body",
                         contract_patches=[{"name": "body", "type": "wall"},
                                           {"name": "farfield", "type": "farfield"}],
                         args={"domain_min": [-DIAMETER_M * 2] * 3,
                               "domain_max": [DIAMETER_M * 2] * 3},
                         cell_budget=200_000)
        res = run_engine_local(ws, engine="cfmesh", timeout=900)
        assert res["rc"] == 0 and not res["timed_out"], res.get("log_tail", "")[-800:]
        from meshpipeline.engines.cfmesh.finalize import finalize
        assert finalize(str(ws), intake_patches=[{"name": "body", "type": "wall"},
                                                 {"name": "farfield", "type": "farfield"}],
                        engine="cfmesh", domain="sphere", flow_topology="external")["success"]
        lo, hi = _meshed_bounds(ws)
        spans[label] = hi - lo

    assert spans["metre"] == pytest.approx(spans["millimetre"], rel=1e-6), spans


def test_gmsh_native_nodes_are_metres(tmp_path):
    import gmsh
    from tests.cad_fixtures import write_step_of_units

    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.step")
    write_step_of_units(geom.local_path, "MM", 20.0)          # a 20 mm box
    ws = tmp_path / "ws"
    prepare_surface(geom, ws / "input.stl", engine="gmsh")

    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("m")
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.02 / 3)
        gmsh.model.mesh.generate(3)
        _t, coords, _p = gmsh.model.mesh.getNodes()
        gmsh.model.remove()
    finally:
        gmsh.finalize()

    xs = coords[0::3]
    assert xs.size > 0, "gmsh produced no nodes"
    assert (max(xs) - min(xs)) == pytest.approx(0.02, rel=1e-3)
