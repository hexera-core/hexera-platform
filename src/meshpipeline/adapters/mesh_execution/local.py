# Responsibility: Satisfy the mesh-execution contract by running the engine in this process.
# Boundaries: dispatch only - production binds the Cloud Run executor; the native matrix runs against this one.
from __future__ import annotations

from typing import Any

from meshpipeline.contracts.mesh_execution import MeshExecutor
from meshpipeline.engines.dispatch import run_engine_local


class LocalMeshExecutor(MeshExecutor):
    def run(self, workspace: Any, *, engine: str, timeout: int,
            operation_key: str = "") -> dict:
        # A local run exchanges nothing, so the operation identity has nowhere to go here.
        # It is accepted so this executor satisfies the same inner contract as the remote one.
        return run_engine_local(workspace, engine=engine, timeout=timeout)
