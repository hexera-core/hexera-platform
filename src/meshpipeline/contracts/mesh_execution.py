# Responsibility: Declare how a mesh job is executed, wherever it runs.
# Owns: the outer executor Protocol, its process-wide binding, and the error kinds a caller must tell apart.
# Boundaries: a Protocol and its binding; the local and Cloud Run implementations live in adapters/mesh_execution/.
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: The infrastructure failure the product already speaks. A run whose submission cannot be
#: resolved is not a mesh verdict and not a success - it is the same class of outcome as the
#: provider being unreachable, and the builder already knows how to report it.
#:
#: It lives HERE, with the other error kinds a caller must tell apart, because both sides of the
#: exchange need the same number: application/ returns it, and engines/ has to recognise it to
#: avoid describing an infrastructure fault as a geometry one. engines/ may not import
#: application/, so a constant only application/ owned forced that edge.
RC_INFRASTRUCTURE = -3


class MeshExecutionError(RuntimeError):
    pass


class SubmissionIndeterminate(MeshExecutionError):
    # The provider MAY have accepted the run and its acknowledgement did not reach us. A distinct
    # class because it demands the opposite response to a definitive refusal: one may be retried,
    # the other must never be.
    pass


@runtime_checkable
class MeshExecutor(Protocol):

    def run(self, workspace: Any, *, engine: str, timeout: int) -> dict: ...


_executor: MeshExecutor | None = None


def set_mesh_executor(executor: MeshExecutor | None) -> None:
    global _executor
    _executor = executor


def run_mesh(workspace: Any, *, engine: str, timeout: int) -> dict:
    if _executor is None:
        raise MeshExecutionError(
            "no MeshExecutor configured - the runtime composition root must call "
            "set_mesh_executor() before meshing")
    return _executor.run(workspace, engine=engine, timeout=timeout)
