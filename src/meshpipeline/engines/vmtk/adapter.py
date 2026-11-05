# Responsibility: Bind VMTK to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner; it holds no mesher logic.
from __future__ import annotations


class ENGINE:
    name = "vmtk"

    def check_solvability(self, workspace, metrics_out=None):
        return None

    def check_domain_extents(self, request_txt, manifest):
        from meshpipeline.engines.domain_extent_gate import extent_gate_for_request
        return extent_gate_for_request(request_txt, manifest)

    def __getattr__(self, item: str):
        from meshpipeline.engines.vmtk import vmtk_runner
        return getattr(vmtk_runner, item)
