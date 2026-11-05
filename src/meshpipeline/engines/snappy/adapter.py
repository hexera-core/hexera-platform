# Responsibility: Bind snappyHexMesh to the runtime engine seam.
# Boundaries: a thin forwarder to this bundle's runner; it holds no mesher logic.
from __future__ import annotations


class ENGINE:
    name = "snappy"

    def run_cartesian_mesh(self, workspace, *, timeout: int, context=None) -> dict:
        from meshpipeline.engines.snappy import snappy_runner
        return snappy_runner.run_snappy(workspace, timeout=timeout, context=context)

    def __getattr__(self, item: str):
        from meshpipeline.engines.snappy import snappy_runner
        return getattr(snappy_runner, item)
