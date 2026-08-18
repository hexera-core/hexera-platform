# Responsibility: Record every native submission durably, outside the process that made it.
# Boundaries: it stands in for the provider at the MeshExecutor port; it prepares no mesh and decides nothing.
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

#: The recorder must OUTLIVE its client. A submission that was accepted and then lost with the
#: worker that made it would be indistinguishable from one that never happened, and telling those
#: two apart is the entire question a crash window asks. So the record is written and fsynced by a
#: separate process before the client is told anything, and the barriers are FIFOs: the client
#: blocks in `read`, which is an event, not an interval.
_SERVER = r'''
import hashlib, json, os, socket, sys

sock_path, log_path, accepted_fifo, release_fifo, collect_log, run_id = sys.argv[1:7]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock_path)
server.listen(8)
attempt = 0
while True:
    conn, _ = server.accept()
    try:
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            raw += chunk
        if not raw.strip():
            continue
        request = json.loads(raw)
        if request.get("command") == "shutdown":
            conn.sendall(b'{"ok": true}\n')
            break
        if request.get("command") == "collect":
            # A REUSE, NOT A SUBMISSION. It is logged separately and answered immediately: the
            # submission count is the measurement, and a reuse must never be able to inflate it.
            with open(collect_log, "a") as fh:
                fh.write(json.dumps({"operation_key": request.get("operation_key", ""),
                                     "job_id": request.get("job_id", "")}) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            conn.sendall(json.dumps({"rc": 0, "timed_out": False,
                                     "collected": True}).encode() + b"\n")
            continue
        attempt += 1
        record = {
            "attempt": attempt,
            "job_id": request.get("job_id", ""),
            "execution_generation": request.get("execution_generation"),
            "engine": request.get("engine", ""),
            "timeout": request.get("timeout"),
            # the workspace CONTENT, not its bytes: a digest identifies the request without
            # carrying anything secret into evidence
            "payload_digest": request.get("payload_digest", ""),
            # the namespace the exchange would use - the parent proves both processes agree on it
            "operation_key": request.get("operation_key", ""),
            "monotonic": os.times().elapsed,
        }
        # DURABLE BEFORE ACKNOWLEDGED. The client learns nothing until this survives its death.
        with open(log_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        # `accepted`: the parent test unblocks here, and only here
        with open(accepted_fifo, "w") as fh:
            fh.write(json.dumps({"attempt": attempt}) + "\n")
        # `allow_response`: the client stays blocked until the parent says otherwise
        with open(release_fifo) as fh:
            fh.readline()
        try:
            # The provider's OPERATION REFERENCE, as the real endpoint returns it: the
            # submission authority may record `accepted` only when it can point at one.
            conn.sendall(json.dumps({
                "rc": 0, "timed_out": False, "attempt": attempt,
                "provider_reference":
                    "projects/beta/locations/us-central1/operations/op-%s-%d" % (
                        run_id, attempt),
            }).encode() + b"\n")
        except OSError:
            # THE CLIENT DIED while its request was held. That is the crash this recorder exists
            # to observe, so it is an expected outcome here, not a reason to stop serving: the
            # replacement worker still has to be able to submit.
            pass
    finally:
        conn.close()
server.close()
'''


class NativeSubmissionRecorder:

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # a short socket path: AF_UNIX paths are capped near 108 bytes, and a pytest tmp path
        # plus a name is comfortably over it
        self._dir = Path(tempfile.mkdtemp(prefix="nsr-"))
        self.socket_path = self._dir / "s"
        self.log_path = self.root / "submissions.jsonl"
        # UNIQUE PER RECORDER. PostgreSQL refuses one provider reference on two operations -
        # correctly - so a double that mints the same operation name in every test manufactures
        # a failure that production could never have.
        self.run_id = os.urandom(6).hex()
        self.collect_log_path = self.root / "collections.jsonl"
        self.collect_log_path.touch()
        self.accepted_fifo = self._dir / "accepted"
        self.release_fifo = self._dir / "release"
        os.mkfifo(self.accepted_fifo)
        os.mkfifo(self.release_fifo)
        self.log_path.touch()
        self._proc: subprocess.Popen | None = None

    def start(self) -> NativeSubmissionRecorder:
        self._proc = subprocess.Popen(
            [sys.executable, "-c", _SERVER, str(self.socket_path), str(self.log_path),
             str(self.accepted_fifo), str(self.release_fifo), str(self.collect_log_path),
             self.run_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # the socket appears when the server binds; opening it is the readiness signal
        while not self.socket_path.exists():
            if self._proc.poll() is not None:
                raise AssertionError(
                    f"the recorder died before binding: {self._proc.stderr.read().decode()}")
        return self

    # barriers
    def wait_accepted(self) -> dict:
        # Blocks until the recorder has DURABLY written a request. A FIFO read is an event.
        with open(self.accepted_fifo) as fh:
            return json.loads(fh.readline())

    def allow_response(self) -> None:
        with open(self.release_fifo, "w") as fh:
            fh.write("go\n")

    # readback
    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]

    def collections(self) -> list[dict]:
        # Reuses of an already-accepted operation. Deliberately a SEPARATE log: a reuse is
        # evidence the replacement resumed, and must never be counted as a submission.
        return [json.loads(line)
                for line in self.collect_log_path.read_text().splitlines() if line.strip()]

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(self.socket_path))
            client.sendall(b'{"command": "shutdown"}\n')
            client.close()
            self._proc.wait(timeout=30)
        except Exception:                      # noqa: BLE001 - teardown never fails a test
            self._proc.kill()
            self._proc.wait(timeout=30)
        finally:
            self._proc = None
            for path in (self.socket_path, self.accepted_fifo, self.release_fifo):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            try:
                self._dir.rmdir()
            except OSError:
                pass


def workspace_digest(workspace) -> str:
    # A NON-SECRET identity for the request: the sorted relative paths and their content hashes.
    parts = []
    root = Path(workspace)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts.append(f"{path.relative_to(root)}:"
                     f"{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class RecordingMeshExecutor:
    # The production MeshExecutor port, talking to the recorder instead of the provider. Nothing
    # above this point changes: preparation, ownership, fencing and dispatch stay production.
    def __init__(self, socket_path, *, job_id: str = "", execution_generation: int = 0) -> None:
        self.socket_path = str(socket_path)
        self.job_id = job_id
        self.execution_generation = execution_generation

    def run(self, workspace, *, engine: str, timeout: int, operation_key: str = "") -> dict:
        return self._speak({
            "job_id": self.job_id, "execution_generation": self.execution_generation,
            "engine": engine, "timeout": timeout, "operation_key": operation_key,
            "payload_digest": workspace_digest(workspace)})

    def collect(self, workspace, *, engine: str, timeout: int, operation_key: str) -> dict:
        # THE RESUMPTION PATH: read the accepted operation's result from its own namespace. It
        # sends no submission, which is precisely what the crash window measures.
        return self._speak({"command": "collect", "job_id": self.job_id,
                            "operation_key": operation_key, "engine": engine})

    def _speak(self, payload: dict) -> dict:
        request = json.dumps(payload) + "\n"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(self.socket_path)
        try:
            client.sendall(request.encode())
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    raise AssertionError("the recorder closed before responding")
                raw += chunk
            return {**json.loads(raw), "log_tail": "recorded"}
        finally:
            client.close()


# the cfMesh preparation fixture
# Held here, beside the recorder, because a BARE CHILD PROCESS needs it. A child that imported
# the test module to reach this would pull in `pytestmark` and module-scope fixtures and hang
# before it ever spoke to the recorder - which is exactly what happened.


def required_files(engine: str = "cfmesh") -> tuple[str, ...]:
    # DERIVED from the engine bundle, never a copied list: a change there must reach this fixture.
    from meshpipeline.engines.registry import get_spec
    return tuple(get_spec(engine).run_policy.required_files)


def cfmesh_context(root, *, job_id: str, generation: int, omit: str = "", engine: str = "cfmesh"):
    from tests._geometry_support import materialized

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.agents.builder.tools.meshing import STAGED_SURFACE

    root = Path(root)
    workspace = root / f"ws-{job_id[:8]}"
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in required_files(engine):
        if relative == omit:
            continue
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("FoamFile{ version 2.0; format ascii; object meshDict; }\n")
    geometry = materialized(root / f"geom-{job_id[:8]}", filename="part.stl")
    # the staged surface the input-contract gate measures, where production stages it
    staged = workspace / STAGED_SURFACE
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(geometry.local_path.read_text())
    return BuilderToolContext(
        workspace=workspace, geometry=geometry, job_id=job_id,
        execution_id=f"exec-{job_id[:8]}", execution_generation=generation,
        engine=engine, loop_deadline=None), workspace


def preparation_preconditions(ctx, workspace, *, engine: str = "cfmesh") -> dict:
    # Each precondition answered SEPARATELY, so a refusal names which one failed rather than
    # leaving a caller to infer it from a missing prepared run.
    from meshpipeline.agents.builder.tools.meshing import (
        input_contract_rejection,
        prepare_mesh_run,
    )
    from meshpipeline.engines.registry import get_spec

    rejection = input_contract_rejection(get_spec(engine), workspace, geometry=ctx.geometry)
    missing = [f for f in required_files(engine) if not (workspace / f).is_file()]
    prepared = prepare_mesh_run(ctx) if not rejection and not missing else None
    return {
        "input_contract_rejection": rejection,
        "missing_required_files": missing,
        "mesh_ok_present": (workspace / ".mesh_ok").exists(),
        "prepared": prepared,
        "refusal": None if prepared is None else prepared.refusal,
        "cap": 0 if prepared is None else prepared.cap,
    }


#: The child that submits. Bare: no pytest, ordered markers on stdout, one production dispatch.
#: Held here so a subprocess can run it without importing a test module - which is what made an
#: earlier attempt hang before it reached the recorder at all.
SUBMITTING_CHILD = r"""
import json, os, sys, uuid
from pathlib import Path

def mark(name, **kw):
    print("MARK " + json.dumps({"marker": name, **kw}), flush=True)

mark("child_started", pid=os.getpid())
sys.path.insert(0, sys.argv[6])
sock, root, job_id, generation, worker_token = sys.argv[1:6]
from meshpipeline.application.native_submission import ClaimingMeshExecutor
from meshpipeline.contracts import mesh_execution
from tests.integration.native_submission_recorder import (
    RecordingMeshExecutor, cfmesh_context, preparation_preconditions)

# COMPOSED AS PRODUCTION COMPOSES IT: the submission authority outside, the provider inside. The
# recorder stands in for Cloud Run at the provider port and nowhere else, so the claim, the
# operation identity and the disposition are the real ones.
mesh_execution.set_mesh_executor(ClaimingMeshExecutor(
    RecordingMeshExecutor(sock, job_id=job_id, execution_generation=int(generation))))
ctx, ws = cfmesh_context(Path(root), job_id=job_id, generation=int(generation))
mark("context_built", workspace=str(ws))
checks = preparation_preconditions(ctx, ws)
mark("preconditions", input_contract_rejection=checks["input_contract_rejection"],
     missing=checks["missing_required_files"], mesh_ok=checks["mesh_ok_present"],
     cap=checks["cap"], refusal=checks["refusal"])
assert checks["refusal"] is None and checks["cap"] > 0, checks
mark("preparation_accepted")

# THE OWNERSHIP THE FENCE READS. Without it the executor has no identity to claim under and the
# repair would be invisible here - which is exactly what this child previously failed to prove.
from meshpipeline.application import execution_fence
from meshpipeline.persistence.lease import ExecutionOwnership
own = ExecutionOwnership(job_id=uuid.UUID(job_id), execution_generation=int(generation),
                         worker_token=uuid.UUID(worker_token), backend="recorder",
                         pipeline_deadline_at=None)
from meshpipeline.agents.builder.tools import dispatch_prepared_mesh
with execution_fence.execution_ownership(own):
    mark("dispatch_entered")
    raw = dispatch_prepared_mesh(ctx, checks["prepared"])
mark("dispatch_returned", chars=len(raw), body=raw[:400])
if os.environ.get("HOLD_AFTER_DISPATCH"):
    # THE UN-CHECKPOINTED GAP: the provider accepted, the claim records it, and the graph has
    # not yet written anything. Holding here is how a killed worker is observed in that exact
    # window rather than at whichever instruction a timer happened to land on.
    import time
    mark("holding_before_checkpoint")
    time.sleep(3600)
mark("mesh_ok_after", present=(ws / ".mesh_ok").exists())
mark("child_exited")
"""


def bounded(name, fn, timeout=120):
    # EVERY wait is bounded on a named event. A bound expiring is a failed measurement with
    # diagnostics attached, never evidence that nothing happened.
    import threading

    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:                      # noqa: BLE001 - reported, not raised here
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"bound expired waiting for {name}")
    if "error" in box:
        raise AssertionError(f"{name}: {box['error']}")
    return box.get("value")


def markers(lines) -> list[dict]:
    return [json.loads(line[5:]) for line in lines if line.startswith("MARK ")]
