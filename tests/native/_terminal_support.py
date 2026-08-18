# Responsibility: Drive a real engine to a terminal outcome for the native terminal matrix.
# Boundaries: it runs the real toolchain; only the graph around it is stubbed.
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pyvista as pv
from tests.native._native_geometry import (
    cad_state,
    prepared_surface_for,
    surface_state,
)

from meshpipeline.engines.dispatch import run_engine_local
from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "geometry"


def stage_gmsh_brep(source_step: Path, ws: Path) -> Path:
    from tests._geometry_support import materialized

    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    ws.mkdir(parents=True, exist_ok=True)
    geom = materialized(ws / "_src", unit=LengthUnit.metre,
                        basis=ResolutionBasis.user_confirmed, filename="part.step")
    shutil.copy2(source_step, geom.local_path)
    prepare_surface(geom, ws / "input.stl", engine="gmsh")
    staged = ws / "geometry.step"
    assert staged.is_file(), "gmsh staging did not write the metre-normalised B-rep"
    assert staged.read_bytes() != source_step.read_bytes(), (
        "workspace/geometry.step is a byte copy of the source - the staged B-rep was bypassed")
    return staged


def _sphere(ws: Path) -> None:
    pv.Sphere(radius=0.5, theta_resolution=24, phi_resolution=24).triangulate().save(str(ws / "input.stl"))


def author_and_mesh(engine: str, ws: Path) -> dict:
    ws.mkdir(parents=True, exist_ok=True)
    contract: list = []
    domain = ""

    if engine == "cfmesh":
        import meshpipeline.engines.cfmesh.cfmesh_runner as R
        from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
        from meshpipeline.engines.cfmesh.finalize import finalize
        _sphere(ws)
        (ws / "flow_topology").write_text("external\n"); (ws / "dimensionality").write_text("3D\n")
        write_case_skeleton(ws)
        contract = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
        assert R.configure_mesh(ws, geometry_file="input.stl", surface=prepared_surface_for(ws / "input.stl"), strategy={"max_cells": 60000},
                                wall_patch="body", contract_patches=contract,
                                args={"domain_min": [-1.0, -1.0, -1.0], "domain_max": [1.5, 1.0, 1.0]},
                                cell_budget=60000).get("success")
        marker, domain = "constant/polyMesh/owner", "unit sphere ext"
        rc = run_engine_local(ws, engine=engine, timeout=600)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain, flow_topology="external")

    elif engine == "snappy":
        import meshpipeline.engines.snappy.snappy_runner as R
        from meshpipeline.engines.snappy.finalize import finalize
        _sphere(ws)
        (ws / "flow_topology").write_text("external\n"); (ws / "dimensionality").write_text("3D\n")
        R._write_case_skeleton(ws)
        contract = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
        assert R.configure_mesh(ws, geometry_file="input.stl", surface=prepared_surface_for(ws / "input.stl"), strategy={"max_cells": 40000},
                                wall_patch="body", contract_patches=contract,
                                args={"domain_min": [-1.0, -1.0, -1.0], "domain_max": [1.5, 1.0, 1.0]},
                                cell_budget=40000).get("success")
        marker, domain = "constant/polyMesh/owner", "unit sphere ext"
        rc = run_engine_local(ws, engine=engine, timeout=900)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain, flow_topology="external")

    elif engine == "gmsh":
        from meshpipeline.engines.gmsh.gmsh_runner import finalize
        # PRODUCTION STAGING, not a raw copy. `workspace/geometry.step` is the metre-normalised
        # B-rep `tessellate_to_stl` writes, and gmsh meshes that solid directly - copying the
        # source over it would produce native evidence in the SOURCE's units, which is exactly the
        # error the staged B-rep exists to prevent.
        stage_gmsh_brep(FIX / "plate_with_hole_2d.step", ws)
        (ws / "flow_topology").write_text("external\n"); (ws / "dimensionality").write_text("2D\n")
        (ws / "gmsh_spec.json").write_text(json.dumps({"dimensionality": "2D", "element_order": "1"}))
        marker, domain = "mesh.inp", "planar plate"
        rc = run_engine_local(ws, engine=engine, timeout=300)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain)

    elif engine == "snappy_multiregion":
        import meshpipeline.engines.snappy_multiregion.multiregion_runner as R
        from meshpipeline.engines.snappy_multiregion.multiregion_runner import finalize
        R.tessellate_to_stl(str(FIX / "cht_enclosing_2region.step"), ws / "input.stl", prepared=cad_state())
        (ws / "flow_topology").write_text("internal\n"); (ws / "dimensionality").write_text("3D\n")
        solids = json.loads((ws / "_assembly" / "solids.json").read_text())
        bv = sorted(solids, key=lambda s: s["volume"])
        regions = [{"name": "fluid", "type": "fluid", "solids": [int(bv[1]["index"])]},
                   {"name": "solid", "type": "solid", "solids": [int(bv[0]["index"])]}]
        R.configure_mesh(ws, surface=prepared_surface_for(ws / "input.stl"), strategy={"regions": regions, "surface_level": [1, 1],
                         "interface_refinement": 0, "n_layers": 0, "max_cells": 200000}, wall_patch="")
        marker, domain = "constant/regionProperties", "cht enclosing"
        rc = run_engine_local(ws, engine=engine, timeout=900)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain, internal_flow=True)

    elif engine == "vmtk":
        import meshpipeline.engines.vmtk.vmtk_runner as R
        from meshpipeline.engines.vmtk.vmtk_runner import finalize
        R.tessellate_to_stl(str(FIX / "vessel_tube_open.vtp"), ws / "input.stl", prepared=surface_state())
        (ws / "flow_topology").write_text("internal\n")
        R.configure_mesh(ws, surface=prepared_surface_for(ws / "input.stl"), strategy={"edge_length_factor": 0.4, "boundary_layers": 0,
                         "cap_openings": True, "remesh_surface": True,
                         "source_ids": [0], "target_ids": [1], "max_cells": 500000})
        marker, domain = "mesh.vtu", "parametric tube"
        rc = run_engine_local(ws, engine=engine, timeout=550)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain, internal_flow=True)
    else:
        raise ValueError(f"unknown engine {engine!r}")

    # the REAL declared gates → the real executor evidence
    ctx = GateCtx(workspace=ws, engine=engine, domain=domain, intake_patches=contract, engine_params={})
    gate_results: list = []
    gates_ok, failed_gate, _ = run_gates(get_spec(engine).gates, ctx,
                                         on_result=lambda k, o, fb: gate_results.append((k, bool(o))))
    return {"engine": engine, "workspace": str(ws), "marker": marker, "native_rc": rc.get("rc"),
            "gates_ok": bool(gates_ok), "failed_gate": failed_gate or "", "contract": contract,
            "domain": domain, "gate_results": gate_results}


class StubGraph:

    def __init__(self, native: dict, *, reviewer_verdict: str = "PASS",
                 purpose: str = "", dimensionality: str = "", approved_snapshot_id: str = ""):
        self._native = native
        self._reviewer_verdict = reviewer_verdict
        self._purpose = purpose
        self._dimensionality = dimensionality
        self._approved_snapshot_id = approved_snapshot_id

    async def aget_state(self, config=None):
        from types import SimpleNamespace
        return SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

    async def ainvoke(self, initial_state, config=None):
        n = self._native
        state = dict(initial_state)
        state.update({
            "engine": n["engine"],
            "openfoam_workspace": n["workspace"],
            "executor_success": bool(n["gates_ok"]),
            "executor_failed_gate": n["failed_gate"],
            "reviewer_verdict": self._reviewer_verdict if n["gates_ok"] else "",
            "reviewer_result": {"verdict": self._reviewer_verdict} if n["gates_ok"] else {},
            "retry_count": 0,
            "api_failure": "",
            "purpose": self._purpose,
            "dimensionality": self._dimensionality,
            "approved_snapshot_id": self._approved_snapshot_id,
        })
        return state


def build_stub_graph_factory(native: dict, **kw):
    def _factory(*args, **kwargs):
        return StubGraph(native, **kw)
    return _factory
