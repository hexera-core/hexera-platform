# Responsibility: Bind the conjugate cfMesh mesher to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's own modules; it holds no mesher logic.
from __future__ import annotations

import shutil
from pathlib import Path


class ENGINE:
    name = "cfmesh_conjugate"

    # geometry: the part itself, kept in the workspace because the coolant is cut out of it

    def inspect_stl(self, workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
        from meshpipeline.cad.stl_io import read_stl_solids

        stl = Path(workspace) / geometry_file
        if not stl.exists():
            return {"error": f"{geometry_file} is not staged"}
        solids = read_stl_solids(stl)
        pts = [v for tris in solids.values() for t in tris for v in t]
        if not pts:
            return {"solids": [], "error": f"{geometry_file} holds no triangles"}
        return {"solids": sorted(solids), "solid_count": len(solids),
                "bbox_min": [min(p[i] for p in pts) for i in range(3)],
                "bbox_max": [max(p[i] for p in pts) for i in range(3)],
                "units": "m"}

    def tessellate_to_stl(self, geom_path, out_stl, *, context=None, prepared=None) -> Path:
        # THE B-REP IS KEPT, under its own extension. This engine does not mesh the skin it
        # writes here: the coolant is cut out of the solid itself (cad/fluid_volume), which needs
        # the CAD and not a triangle file, and the driver reads it back out of the workspace.
        from meshpipeline.cad.cad_tessellate import tessellate_to_stl as _skin

        out_stl = Path(out_stl)
        suffix = Path(str(geom_path)).suffix.lower()
        staged = out_stl.parent / f"geometry{suffix}"
        if Path(str(geom_path)).resolve() != staged.resolve():
            shutil.copy2(geom_path, staged)
        _skin(geom_path, out_stl, prepared=prepared)
        return out_stl

    def read_stl_solids(self, path: Path) -> dict:
        from meshpipeline.cad.stl_io import read_stl_solids
        return read_stl_solids(path)

    # meshing

    def run_cartesian_mesh(self, workspace, *, timeout: int, context=None) -> dict:
        # the uniform run_mesh seam -> the mesh executor, which runs
        # cfmesh_conjugate/native._run_conjugate_local where OpenFOAM is
        from meshpipeline.contracts.mesh_execution import run_mesh
        return run_mesh(workspace, engine="cfmesh_conjugate", timeout=timeout)

    def check_mesh(self, workspace) -> dict:
        from meshpipeline.engines.cfmesh_conjugate.finalize import quality_of
        from meshpipeline.engines.cfmesh_conjugate.native import read_report

        report = read_report(workspace)
        if not report:
            return {"mesh_ok": False, "fatal": ["no conjugate_report.json"]}
        return quality_of(Path(workspace), report)

    def check_solvability(self, workspace, metrics_out=None):
        # A coupled case is solvable when each region is itself a valid mesh and the interface
        # pairs one-to-one; both are measured by the run and gated. There is no single pressure
        # matrix to assemble here - there are two regions and a mapped coupling between them.
        return None

    def check_domain_extents(self, request_txt, manifest):
        from meshpipeline.engines.domain_extent_gate import extent_gate_for_request
        return extent_gate_for_request(request_txt, manifest)

    # review / artifacts

    def export_volume_vtk(self, workspace) -> str | None:
        # written by the run, per region, never as the merged block
        from meshpipeline.engines.cfmesh_conjugate.review_surface import review_volume
        return review_volume(workspace)

    def build_review_msh(self, workspace, patches_tris: dict):
        from meshpipeline.render.review_artifacts import build_review_msh
        return build_review_msh(workspace, patches_tris)

    def write_manifest(self, workspace, **kwargs):
        from meshpipeline.engines.manifest import write_manifest
        return write_manifest(workspace, **kwargs)

    def finalize(self, workspace_dir, intake_patches, engine, domain="", internal_flow=False,
                 engine_params=None, flow_topology=""):
        from meshpipeline.engines.cfmesh_conjugate.finalize import finalize
        return finalize(workspace_dir, intake_patches, engine, domain, internal_flow,
                        engine_params, flow_topology)
