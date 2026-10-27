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

import meshpipeline.settings.policy as polcfg
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

    elif engine == "cfmesh_conjugate":
        # A COOLED SOLID, read the way an upload is read. The driver's own helpers do the deciding
        # (the CAD's unit, the part's bounds, the coolant cut out of the solid, the resolution, the
        # port names from the declaration), because what this case has to prove is that the
        # PRODUCTION decisions reach a delivered mesh - not that a hand-written plan does.
        #
        # IN MILLIMETRES, unlike the other fixtures here: a cold plate arrives in millimetres, the
        # unit the driver reads is the one slip this engine could carry silently (a scale of 1.0 on
        # a millimetre STEP made the plate's coolant 4.56 m3 of fluid in a 100 mm channel, and
        # every number after it was wrong by a thousand and consistently so), and `cad_state` takes
        # the unit for exactly this reason.
        import dataclasses

        from meshpipeline.cad.fluid_volume import surface_bounded
        from meshpipeline.contracts.geometry_units import LengthUnit
        from meshpipeline.engines.cfmesh_conjugate import budget as B
        from meshpipeline.engines.cfmesh_conjugate import conjugate as C
        from meshpipeline.engines.cfmesh_conjugate import plan as PL
        from meshpipeline.engines.cfmesh_conjugate.adapter import ENGINE as CONJUGATE
        from meshpipeline.engines.cfmesh_conjugate.case_scaffold import write_case_skeleton
        from meshpipeline.engines.cfmesh_conjugate.driver import (
            cad_scale,
            coolant_sits_inside,
            declared_port_names,
            find_source,
            skin_bounds,
        )
        from meshpipeline.engines.cfmesh_conjugate.finalize import finalize

        CONJUGATE().tessellate_to_stl(str(FIX / "cold_plate_2ch.step"), ws / "input.stl",
                                      prepared=cad_state(LengthUnit.millimetre))
        (ws / "flow_topology").write_text("internal\n"); (ws / "dimensionality").write_text("3D\n")
        write_case_skeleton(ws)
        contract = [{"name": "coolant_in", "type": "inlet"},
                    {"name": "coolant_out", "type": "outlet"}]
        (ws / "port_declaration.json").write_text(json.dumps(contract))

        source = find_source(ws)
        assert source is not None, "the staging left no B-rep to cut the coolant out of"
        bounds = skin_bounds(ws)
        assert bounds is not None, "the staging left no skin to read the part's bounds from"
        (ws / PL.FLUID_STL).parent.mkdir(parents=True, exist_ok=True)
        rec = surface_bounded(source, ws / PL.FLUID_STL, scale=cad_scale(source), kind="cad")
        assert rec, "no coolant was found inside the fixture"
        wrong = coolant_sits_inside(rec, bounds[0], bounds[1])
        assert not wrong, wrong
        across, _estimate, _said = B.cells_across_for(
            fidelity="standard", block_lo=bounds[0], block_hi=bounds[1],
            narrowest_m=float(rec["narrowest_m"]), limit=polcfg.CELL_HARD_LIMIT)
        plan, lo, hi = PL.plan_for(
            narrowest_m=float(rec["narrowest_m"]), fluid_volume_m3=float(rec["volume_m3"]),
            passages=int(rec["passages"]), solid_lo=bounds[0], solid_hi=bounds[1],
            fluid_lo=rec["bbox_min_m"], fluid_hi=rec["bbox_max_m"],
            cells_across=across, levels=B.LEVELS)
        names, _unnamed = declared_port_names(ws, plan)
        if names:
            plan = dataclasses.replace(plan, patch_names=PL._as_lists(names))
        (ws / PL.BLOCK_STL).write_text(C.block_surface_stl(lo, hi))
        PL.write_plan(ws, plan)
        marker, domain = "constant/regionProperties", "cooled solid"
        rc = run_engine_local(ws, engine=engine, timeout=900)
        finalize(str(ws), intake_patches=contract, engine=engine, domain=domain,
                 internal_flow=True, flow_topology="internal")

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
