# Responsibility: Declare how a mesh job is executed, wherever it runs.
# Owns: the outer executor Protocol, its process-wide binding, the error kinds a caller must tell apart, and the abandonment watch.
# Boundaries: a Protocol and its binding; the local and Cloud Run implementations live in adapters/mesh_execution/.
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: The infrastructure failure the product already speaks. A run whose submission cannot be
#: resolved is not a mesh verdict and not a success - it is the same class of outcome as the
#: provider being unreachable, and the builder already knows how to report it.
#:
#: It lives HERE, with the other error kinds a caller must tell apart, because both sides of the
#: exchange need the same number: application/ returns it, and engines/ has to recognise it to
#: avoid describing an infrastructure fault as a geometry one. engines/ may not import
#: application/, so a constant only application/ owned forced that edge.
RC_INFRASTRUCTURE = -3

#: A run that WAS dispatched and did not come back within its time limit. Not an outage and not
#: a crash: the mesh was too big or too slow for the budget, and the one repair is a SMALLER mesh.
#: It always travels with timed_out=True, which every judge reads before the exit code, and it is
#: the code a local stage timeout already reports (engines/snappy/parallel_stages). It lives here
#: beside RC_INFRASTRUCTURE because the two used to be one: a remote run that outlived the
#: worker's deadline was reported as -3, "the mesh run never started", and the planner was told
#: to resubmit the same plan - which ran out of time again (job 470c3eb9, 2 x 50 minutes).
RC_TIMED_OUT = -1

#: A run REFUSED before launch because the written case does not build the patches the user
#: approved (engines/case_contract.py). Not a mesh verdict and not an outage: nothing ran.
RC_CASE_CONTRACT = -5


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


#: The workspace fact naming WHAT the next native submission carries: the workspace-relative
#: paths (files or directories) that are the remote case's INPUT. A retry pass shares its
#: attempt's one workspace with the pass before it, and that pass's COLLECTED outputs - the
#: built polyMesh, feature-edge meshes, VTK exports, converted meshes, logs - land right back
#: in it. Tarring "the workspace" then ships a prior mesh to the mesher: the payload balloons
#: from tens of MB to most of a GB and the upload dies in transport, which is how every retry
#: pass of a real job burned its attempt on "the mesh run never started". The fact crosses the
#: executor seam the same way the pass fact does - engines/ writes it (only the driver knows
#: which entries are its case and which are a returned result), application/ and adapters/
#: read it - and it lives HERE for the same reason NATIVE_PASS_FACT does.
NATIVE_PAYLOAD_FACT = ".native_payload"  # noqa: S105 - a workspace file name, not a credential


def note_native_payload(workspace: Any, members: Any) -> None:
    # Recorded BEFORE dispatch, overwritten per pass, exactly like the pass fact. Members are
    # workspace-relative POSIX paths; a directory means "every file under it". Sorted and
    # de-duplicated so the fact's bytes - which travel in the payload and its digest - are a
    # deterministic function of what was declared, not of declaration order.
    import json
    from pathlib import Path
    rels = sorted({str(m).replace("\\", "/").strip("/") for m in members if str(m).strip("/")})
    Path(workspace, NATIVE_PAYLOAD_FACT).write_text(json.dumps(rels), encoding="utf-8")


def read_native_payload(workspace: Any) -> tuple[str, ...] | None:
    # Fail closed: absent, unreadable or malformed means "no payload declared" - the caller
    # then treats the WHOLE workspace as the payload, exactly the old behaviour.
    import json
    from pathlib import Path
    try:
        raw = Path(workspace, NATIVE_PAYLOAD_FACT).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(data, list) or not data
            or not all(isinstance(x, str) and x.strip("/").strip() for x in data)):
        return None
    return tuple(data)


def submission_payload_files(workspace: Any) -> list | None:
    """THE ONE ENUMERATION of a declared submission payload's files - None when none is declared.

    Both readers of the fact resolve it through here: the archive the executor uploads
    (adapters/mesh_execution/cloud_run_client) and the digest the claim pins the payload with
    (application/native_submission.workspace_digest). One enumerator is what keeps those two
    scopes the same set of bytes by construction - a digest over files the tar does not carry,
    or a tar of files the digest never saw, is how a replay gets refused for content that never
    differed, or accepted for content that did.

    The pass and payload facts themselves are always members: the pass index is part of the
    submission's identity and must stay pinned in the digest, and the declared scope is part of
    what makes two payloads "the same payload". A declared member that is MISSING is a loud
    error, never a silently smaller upload - the driver just staged the case, so a missing
    member is a mis-declaration, and the remote failing on an absent surface hours later is the
    expensive way to learn that.
    """
    from pathlib import Path
    root = Path(workspace)
    members = read_native_payload(root)
    if members is None:
        return None
    files: dict[str, Path] = {}
    for name in members:
        parts = [seg for seg in name.replace("\\", "/").split("/") if seg not in ("", ".")]
        if not parts or ".." in parts:
            raise ValueError(f"submission payload member escapes the workspace: {name!r}")
        member = root.joinpath(*parts)
        if member.is_dir():
            for f in (q for q in member.rglob("*") if q.is_file()):
                files.setdefault(f.relative_to(root).as_posix(), f)
        elif member.is_file():
            files.setdefault(member.relative_to(root).as_posix(), member)
        else:
            raise ValueError(f"submission payload member is missing from the workspace: {name!r}")
    for fact in (NATIVE_PASS_FACT, NATIVE_PAYLOAD_FACT):
        fp = root / fact
        if fp.is_file():
            files.setdefault(fact, fp)
    return [files[k] for k in sorted(files)]


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

#: THE LAST CHECK BEFORE A MESHER STARTS: (workspace, engine) -> None to launch, or a refusal
#: result shaped like a native one (rc RC_CASE_CONTRACT). Every engine's native run reaches the
#: executor through run_mesh below, so a check here cannot be skipped by one engine forgetting
#: it. Installed by the composition root beside the executor (engines/ may not be imported here).
_launch_check: Callable[[Any, str], dict | None] | None = None


def set_mesh_executor(executor: MeshExecutor | None) -> None:
    global _executor
    _executor = executor


def set_launch_check(check: Callable[[Any, str], dict | None] | None) -> None:
    global _launch_check
    _launch_check = check


#: THE ABANDONMENT WATCH: () -> why the run should stop waiting on its remote mesh, or None to
#: keep waiting. A worker waiting on a Cloud Run execution sits in a poll loop for up to its whole
#: deadline, and that loop used to ask nothing: on shared dev, 2026-10-01, job 470c3eb9 was
#: cancelled at 08:56 UTC and its execution dev-mesh-796ll ran on for another 54 minutes, paid
#: for, with nobody left to read its result. The submission authority (application/
#: native_submission) binds a watch around its provider call; the provider's poll loop reads it.
#: A ContextVar, like the execution guard's owner check, so it follows the run into the thread
#: the mesher is waited on from.
_abandon_watch: ContextVar[Callable[[], str | None] | None] = ContextVar(
    "mesh_abandon_watch", default=None)


@contextmanager
def abandonment_watch(check: Callable[[], str | None] | None) -> Iterator[None]:
    token = _abandon_watch.set(check)
    try:
        yield
    finally:
        _abandon_watch.reset(token)


def abandonment_reason() -> str | None:
    """Why the run waiting on a remote mesh should give it up now - None to keep waiting.

    Never raises. A watch that cannot answer reads as "keep waiting": a wrong "keep waiting" costs
    what it always did (the run waits out its deadline and the fence refuses the result), while a
    wrong "stop" kills a paid mesh its owner was about to collect."""
    check = _abandon_watch.get()
    if check is None:
        return None
    try:
        reason = check()
    except Exception as exc:  # noqa: BLE001 - see the docstring: an unanswerable watch is a no
        logger.warning("the mesh abandonment watch failed (%s) - still waiting on the run",
                       type(exc).__name__)
        return None
    return str(reason) if reason else None


def run_mesh(workspace: Any, *, engine: str, timeout: int) -> dict:
    if _executor is None:
        raise MeshExecutionError(
            "no MeshExecutor configured - the runtime composition root must call "
            "set_mesh_executor() before meshing")
    if _launch_check is not None:
        refusal = _launch_check(workspace, engine)
        if refusal is not None:
            return refusal
    return _executor.run(workspace, engine=engine, timeout=timeout)
