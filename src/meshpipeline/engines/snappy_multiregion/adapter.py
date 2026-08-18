# Responsibility: Bind multi-region snappyHexMesh to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner; it holds no mesher logic.
from __future__ import annotations


class ENGINE:
    name = "snappy_multiregion"

    def run_cartesian_mesh(self, workspace, *, timeout: int, context=None) -> dict:
        # the uniform run_mesh seam → dispatch to Cloud Run (background snappy +
        # splitMeshRegions + regionProperties run in the cloud container). Cloud-only,
        # no local fallback; the execution is multiregion_runner._run_snappy_multiregion_local.
        from meshpipeline.contracts.mesh_execution import run_mesh
        return run_mesh(workspace, engine="snappy_multiregion", timeout=timeout)

    def check_solvability(self, workspace, metrics_out=None):
        return None

    def check_domain_extents(self, request_txt, manifest):
        from meshpipeline.engines.domain_extent_gate import extent_gate_for_request
        return extent_gate_for_request(request_txt, manifest)

    def __getattr__(self, item: str):
        # generic geometry/quality/artifact helpers come from the snappy backend
        # (re-exported through multiregion_runner where a multi-region variant is needed).
        from meshpipeline.engines.snappy_multiregion import multiregion_runner
        return getattr(multiregion_runner, item)
