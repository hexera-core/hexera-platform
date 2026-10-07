# Responsibility: Bind the snap-grid (placed-parts) ECXML mesher to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner and mesher; it holds no mesher logic.
from __future__ import annotations

from pathlib import Path


class ENGINE:
    name = "snapgrid"

    # geometry: the placed parts (one named solid per part), read from the ECXML itself

    def inspect_stl(self, workspace, geometry_file: str = "input.stl", *, context=None) -> dict:
        from meshpipeline.cad.stl_io import read_stl_solids

        stl = Path(workspace) / geometry_file
        if not stl.exists():
            return {"error": f"{geometry_file} is not staged"}
        solids = read_stl_solids(stl)
        pts = [v for tris in solids.values() for t in tris for v in t]
        if not pts:
            return {"solids": [], "error": f"{geometry_file} holds no triangles"}
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        return {"solids": sorted(solids), "solid_count": len(solids),
                "bbox_min": lo, "bbox_max": hi, "units": "m"}

    def tessellate_to_stl(self, geom_path, out_stl, *, context=None, prepared=None) -> Path:
        # The placed parts, written from the ECXML beside the canonical file, in the file's
        # metres - whatever form the canonical took (a fused STEP or the placed STL).
        from meshpipeline.cad.ingest.ecxml import read_ecxml
        from meshpipeline.cad.ingest.ecxml_place import place, placed_stl
        from meshpipeline.engines.snapgrid.runner import find_source

        src = find_source(str(geom_path))
        if src is None:
            raise ValueError(f"no ECXML model beside {Path(str(geom_path)).name} - the snap-grid "
                             "mesher reads the ECXML file itself")
        placed_stl(place(read_ecxml(src)), out_stl, scale=1.0)
        return Path(out_stl)

    def read_stl_solids(self, path: Path) -> dict:
        from meshpipeline.cad.stl_io import read_stl_solids
        return read_stl_solids(path)

    # meshing

    def run_cartesian_mesh(self, workspace, *, timeout: int, context=None) -> dict:
        # the uniform run_mesh seam -> the mesh executor (the mesh image runs
        # engines/snapgrid/runner._run_snapgrid_local, where OpenFOAM is)
        from meshpipeline.contracts.mesh_execution import run_mesh
        return run_mesh(workspace, engine="snapgrid", timeout=timeout)

    def check_mesh(self, workspace) -> dict:
        import json

        from meshpipeline.engines.snapgrid.runner import REPORT_NAME, quality_of
        ws = Path(workspace)
        try:
            report = json.loads((ws / REPORT_NAME).read_text())
        except (OSError, ValueError):
            return {"mesh_ok": False, "fatal": ["no snapgrid_report.json"]}
        return quality_of(ws, report)

    def check_solvability(self, workspace, metrics_out=None):
        return None

    def check_domain_extents(self, request_txt, manifest):
        from meshpipeline.engines.domain_extent_gate import extent_gate_for_request
        return extent_gate_for_request(request_txt, manifest)

    # review / artifacts

    def export_volume_vtk(self, workspace) -> str | None:
        return None

    def build_review_msh(self, workspace, patches_tris: dict):
        from meshpipeline.render.review_artifacts import build_review_msh
        return build_review_msh(workspace, patches_tris)

    def write_manifest(self, workspace, **kwargs):
        from meshpipeline.engines.manifest import write_manifest
        return write_manifest(workspace, **kwargs)

    def _patch_face_counts(self, workspace) -> dict:
        import json

        from meshpipeline.engines.snapgrid.runner import SIDECAR_NAME
        try:
            sidecar = json.loads((Path(workspace) / SIDECAR_NAME).read_text())
        except (OSError, ValueError):
            return {}
        return {r["mesh_patch"]: int(r.get("faces") or 0)
                for r in sidecar.get("patches") or [] if r.get("mesh_patch")}

    def finalize(self, workspace_dir, intake_patches, engine, domain="", internal_flow=False,
                 engine_params=None, flow_topology=""):
        from meshpipeline.engines.snapgrid.runner import finalize
        return finalize(workspace_dir, intake_patches, engine, domain, internal_flow,
                        engine_params, flow_topology)
