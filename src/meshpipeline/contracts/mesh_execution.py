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


#: The workspace fact naming WHICH planned meshing pass of the current attempt the next native
#: submission belongs to. An engine driver runs several plan->mesh->judge passes inside one
#: attempt, all in the attempt's one workspace, and the submission authority sees nothing but
#: that workspace - so the pass index crosses the seam the same way the attempt does: recorded
#: in the workspace, read back where the operation identity is composed. It lives HERE for the
#: same reason RC_INFRASTRUCTURE does: engines/ writes it and application/ reads it, engines/
#: may not import application/, and two spellings of one file name is how the two sides stop
#: describing the same pass.
NATIVE_PASS_FACT = ".native_pass"  # noqa: S105 - a workspace file name, not a credential


def note_native_pass(workspace: Any, pass_index: int) -> None:
    # Recorded BEFORE dispatch, overwritten per pass. The driver numbers its passes from its own
    # control flow, so a re-executed node re-derives the same number for the same pass - which is
    # exactly what lets a replay deduplicate while a revised pass claims a run of its own.
    from pathlib import Path
    Path(workspace, NATIVE_PASS_FACT).write_text(str(int(pass_index)), encoding="utf-8")


def read_native_pass(workspace: Any) -> int | None:
    # Fail closed: an absent or unreadable fact means "no pass recorded", never a guessed one -
    # the caller then keeps the identity it composed without it, exactly the old behaviour.
    from pathlib import Path
    try:
        text = Path(workspace, NATIVE_PASS_FACT).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isascii() and text.isdigit() else None


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
