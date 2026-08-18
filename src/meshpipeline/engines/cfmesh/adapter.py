# Responsibility: Bind cfMesh to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner; it holds no mesher logic.
from __future__ import annotations


class ENGINE:
    name = "cfmesh"

    def __getattr__(self, item: str):
        from meshpipeline.engines.cfmesh import cfmesh_runner
        return getattr(cfmesh_runner, item)
