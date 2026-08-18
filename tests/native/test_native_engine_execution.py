# Responsibility: Verify each of the five engines produces a real mesh through the production dispatch path.
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from tests.native._native_geometry import (
    cad_state,
    prepared_surface_for,
    surface_state,
)

from meshpipeline.engines.dispatch import run_engine_local
from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "geometry"


def _run_declared_gates(ws, engine, domain, intake_patches):
    ctx = GateCtx(workspace=Path(ws), engine=engine, domain=domain,
                  intake_patches=intake_patches, engine_params={})
    results: list = []
    ok, failed, _ = run_gates(get_spec(engine).gates, ctx,
                              on_result=lambda k, o, fb: results.append((k, o, fb)))
    return ok, failed, results


def _sphere_stl(ws, name="input.stl"):
    import pyvista as pv
    sph = pv.Sphere(radius=0.5, theta_resolution=24, phi_resolution=24).triangulate()
    sph.save(str(Path(ws) / name))


# cfmesh (cut-cell external)
def test_cfmesh_native_pass(tmp_path, canonical_provenance):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
    _sphere_stl(tmp_path)
    (tmp_path / "flow_topology").write_text("external\n")
    (tmp_path / "dimensionality").write_text("3D\n")
    write_case_skeleton(tmp_path)   # controlDict/fvSchemes/fvSolution - the production builder hook
    contract = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
    cfg = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=prepared_surface_for(tmp_path / "input.stl"), strategy={"max_cells": 60000},
                           wall_patch="body", contract_patches=contract,
                           args={"domain_min": [-1.0, -1.0, -1.0], "domain_max": [1.5, 1.0, 1.0]},
                           cell_budget=60000)
    assert cfg.get("success"), cfg
    res = run_engine_local(tmp_path, engine="cfmesh", timeout=600)
    assert res["rc"] == 0 and not res["timed_out"], res
    assert (tmp_path / "constant" / "polyMesh" / "owner").exists()
    from meshpipeline.engines.cfmesh.finalize import finalize
    assert finalize(str(tmp_path), intake_patches=contract, engine="cfmesh",
                    domain="unit sphere ext", flow_topology="external")["success"]
    ok, failed, results = _run_declared_gates(tmp_path, "cfmesh", "unit sphere ext", contract)
    assert ok, f"cfmesh gate {failed} failed: {results}"


# snappy (body-fitted external, small domain)
def test_snappy_native_pass(tmp_path, canonical_provenance):
    import meshpipeline.engines.snappy.snappy_runner as R
    _sphere_stl(tmp_path)
    (tmp_path / "flow_topology").write_text("external\n")
    (tmp_path / "dimensionality").write_text("3D\n")
    contract = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
    R._write_case_skeleton(tmp_path)
    cfg = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=prepared_surface_for(tmp_path / "input.stl"), strategy={"max_cells": 40000},
                           wall_patch="body", contract_patches=contract,
                           args={"domain_min": [-1.0, -1.0, -1.0], "domain_max": [1.5, 1.0, 1.0]},
                           cell_budget=40000)
    assert cfg.get("success"), cfg
    res = run_engine_local(tmp_path, engine="snappy", timeout=900)
    assert res["rc"] == 0 and not res["timed_out"], res
    # the per-stage structured model must be present and every recorded stage must be ok
    assert res.get("stages"), "snappy result carries no per-stage breakdown"
    assert all(s["ok"] for s in res["stages"]), res["stages"]
    assert (tmp_path / "constant" / "polyMesh" / "owner").exists()
    from meshpipeline.engines.snappy.finalize import finalize
    assert finalize(str(tmp_path), intake_patches=contract, engine="snappy",
                    domain="unit sphere ext", flow_topology="external")["success"]
    ok, failed, results = _run_declared_gates(tmp_path, "snappy", "unit sphere ext", contract)
    assert ok, f"snappy gate {failed} failed: {results}"


# gmsh (structural / FEA planar plate)
def test_gmsh_native_pass(tmp_path, canonical_provenance):
    from tests._geometry_support import materialized

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    src_dir = tmp_path / "src"
    geom = materialized(src_dir, unit=LengthUnit.metre,
                        basis=ResolutionBasis.user_confirmed, filename="plate.step")
    shutil.copy2(_FIX / "plate_with_hole_2d.step", geom.local_path)
    raw_bytes = geom.local_path.read_bytes()

    prepare_surface(geom, tmp_path / "input.stl", engine="gmsh")
    staged = tmp_path / "geometry.step"
    assert staged.is_file(), "gmsh staging did not write the metre-normalised B-rep"
    assert staged.read_bytes() != raw_bytes, (
        "workspace/geometry.step is a byte copy of the source - the staged B-rep was bypassed")
    staged_sha = hashlib.sha256(staged.read_bytes()).hexdigest()

    (tmp_path / "flow_topology").write_text("external\n")
    (tmp_path / "dimensionality").write_text("2D\n")
    (tmp_path / "gmsh_spec.json").write_text(json.dumps({"dimensionality": "2D", "element_order": "1"}))
    res = run_engine_local(tmp_path, engine="gmsh", timeout=300)
    assert res["rc"] == 0 and not res["timed_out"], res
    assert (tmp_path / "mesh.inp").exists(), "gmsh produced no volume mesh (mesh.inp)"
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == staged_sha, (
        "a later step overwrote the staged B-rep the native driver was given")

    from meshpipeline.engines.gmsh.gmsh_runner import finalize
    assert finalize(str(tmp_path), intake_patches=[], engine="gmsh", domain="planar plate")["success"]
    ok, failed, results = _run_declared_gates(tmp_path, "gmsh", "planar plate", [])
    assert ok, f"gmsh gate {failed} failed: {results}"

    # the delivered facts are metres, and the manifest agrees with what was meshed
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    manifest = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert manifest["mesh_units"] == COMPLETED_MESH_UNIT.value, manifest["mesh_units"]
    # the ACTUAL meshed extent, published as `geometry.box_*`
    geom_block = manifest["geometry"]
    span = max(float(geom_block["box_xmax"]) - float(geom_block["box_xmin"]),
               float(geom_block["box_ymax"]) - float(geom_block["box_ymin"]))
    assert 1e-4 < span < 1e3, (
        f"delivered bounds are not metre-valued (a unit-conversion regression would show here): "
        f"span={span}, geometry={geom_block}")


# snappy_multiregion (enclosing-fluid CHT → exactly two regions, no domain0)
def test_multiregion_native_pass_exactly_two_regions(tmp_path, canonical_provenance):
    import meshpipeline.engines.snappy_multiregion.multiregion_runner as R
    R.tessellate_to_stl(str(_FIX / "cht_enclosing_2region.step"), tmp_path / "input.stl", prepared=cad_state())
    (tmp_path / "flow_topology").write_text("internal\n")
    (tmp_path / "dimensionality").write_text("3D\n")
    solids = json.loads((tmp_path / "_assembly" / "solids.json").read_text())
    by_vol = sorted(solids, key=lambda s: s["volume"])
    solid_idx, fluid_idx = int(by_vol[0]["index"]), int(by_vol[1]["index"])
    regions = [{"name": "fluid", "type": "fluid", "solids": [fluid_idx]},
               {"name": "solid", "type": "solid", "solids": [solid_idx]}]
    cfg = R.configure_mesh(tmp_path, surface=prepared_surface_for(tmp_path / "input.stl"), strategy={"regions": regions, "surface_level": [1, 1],
                           "interface_refinement": 0, "n_layers": 0, "max_cells": 200000},
                           wall_patch="")
    assert cfg.get("regions") == ["fluid", "solid"], cfg
    res = run_engine_local(tmp_path, engine="snappy_multiregion", timeout=900)
    assert res["rc"] == 0 and not res["timed_out"], res
    region_dirs = sorted(p.name for p in (tmp_path / "constant").iterdir()
                         if p.is_dir() and (p / "polyMesh" / "owner").exists())
    assert region_dirs == ["fluid", "solid"], f"undeclared/missing region: {region_dirs}"
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import check_mesh, finalize
    q = check_mesh(tmp_path)
    assert q["regions_undeclared"] == [] and q["interface_ok"] and q["mesh_ok"], q
    assert finalize(str(tmp_path), intake_patches=[], engine="snappy_multiregion",
                    domain="cht enclosing", internal_flow=True)["success"]
    ok, failed, results = _run_declared_gates(tmp_path, "snappy_multiregion", "cht enclosing", [])
    assert ok, f"multiregion gate {failed} failed: {results}"


# vmtk (radius-adaptive tets on an open tube; profile-id seeding)
def test_vmtk_native_pass(tmp_path, canonical_provenance):
    import meshpipeline.engines.vmtk.vmtk_runner as R
    R.tessellate_to_stl(str(_FIX / "vessel_tube_open.vtp"), tmp_path / "input.stl", prepared=surface_state())
    (tmp_path / "flow_topology").write_text("internal\n")
    insp = R.inspect_stl(tmp_path)
    assert insp["n_open_profiles"] == 2, insp
    # open-lumen seeding MUST be profile-id based: pointlist seeding produced a runaway centerline
    # (DistanceToCenterlines ~radius*18) that collapsed the mesh.
    R.configure_mesh(tmp_path, surface=prepared_surface_for(tmp_path / "input.stl"), strategy={"edge_length_factor": 0.4, "boundary_layers": 0,
                     "cap_openings": True, "remesh_surface": True,
                     "source_ids": [0], "target_ids": [1], "max_cells": 500000})
    res = run_engine_local(tmp_path, engine="vmtk", timeout=550)
    assert res["rc"] == 0 and not res["timed_out"], res
    q = R.check_mesh(tmp_path)
    assert q["cells"] > 1000 and not q["fatal"] and q["mesh_ok"], q
    from meshpipeline.engines.vmtk.vmtk_runner import finalize
    assert finalize(str(tmp_path), intake_patches=[], engine="vmtk",
                    domain="parametric tube", internal_flow=True)["success"]
    ok, failed, results = _run_declared_gates(tmp_path, "vmtk", "parametric tube", [])
    assert ok, f"vmtk gate {failed} failed: {results}"
