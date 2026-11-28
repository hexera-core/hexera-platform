# Responsibility: The `mesh_gmsh` kernel function - run gmsh on one STEP file as a CHILD PROCESS of the pool
#                 worker, so that gmsh dying is visible as an exit signal and the worker survives it.
# Owns: the `mesh_gmsh` registration, the child's exit classification and the names the rest of the
#       package (and the tests) import from `mesh_child`: MIN_SICN, judge, profile_config, algorithms.
# Boundaries: no gmsh here, the worker never imports it; the meshing is `mesh_child`. What the answer means
#             for the panel (a MemberResult) is `gmsh_members`; whose fault a signal is, is `panel`.
"""Why a child process: gmsh runs in a native library that aborts on the broken geometry the benchmark is
made of. Inside the pool worker such an abort and a dead worker are the same EOF, so a file that crashes
gmsh read as 'the infrastructure died' and left the scoreboard. As a child, the exit status says which:

  killed by SIGSEGV / SIGABRT / SIGFPE / SIGILL / SIGBUS   -> `crashed`, `gmsh_crash`: a vote against the file
  killed by SIGKILL (OOM killer, an operator) or another   -> `crashed`, `infrastructure`: inconclusive on
                                                               one sight; the member re-runs a SIGKILL once and
                                                               a repeat is `gmsh_crash` (a memory-hungry file
                                                               dies twice; a flaky host rarely does)
  exits non-zero with a Python traceback                   -> raises: a bug or a MemoryError, not the file
  exits non-zero without one (gmsh called exit)            -> `crashed`, `gmsh_crash`
  outlives `timeout_s`                                     -> `timeout`: the member retries it at twice the time

The pool worker dying is now only ever the pool failing, which stays infrastructure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.verdict import mesh_child, thin_sizing
from meshpipeline.cad.repair_v4.verdict.mesh_child import (  # noqa: F401 - the package's public names
    ALT_OPTIONS,
    ALT_SIZE_DIVISOR,
    MIN_SICN,
    algorithms,
    is_gmsh_error,
    judge,
    profile_config,
)
from meshpipeline.cad.repair_v4.verdict.panel import INFRASTRUCTURE_KIND, MESHER_CRASH_KIND, signal_verdict

_STDERR_TAIL = 600


def _child_argv(payload_path: str, answer_path: str) -> list[str]:
    """The command that runs `mesh_child` (tests substitute a child that dies by a chosen signal)."""
    return [sys.executable, "-m", mesh_child.__name__, payload_path, answer_path]


def _stopped(status: str, kind: str | None, message: str, cause: str | None = None) -> dict:
    return {
        "status": status,
        "error_kind": kind,
        "cause": cause,
        "message": message,
        "located": [],
        "min_sicn": None,
        "elements": 0,
    }


def _child_event(what: str, code: int | None, payload: dict, timeout_s: float, stderr: str | bytes | None) -> None:
    """The gmsh child's end, when it was not a clean exit, with a LONGER stderr excerpt than the result's message
    keeps (CRS v5 event log; runs in the pool worker, which carries the caller's context)."""
    from meshpipeline.cad.repair_v5 import events as EV

    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else (stderr or "")
    EV.emit("mesher_child", severity="WARNING", sync=True, end=what, child_exit_code=code,
            profile=payload.get("profile"), timeout_s=timeout_s, step=payload.get("step"),
            stderr_excerpt=text.strip()[-4000:])


@register("mesh_gmsh")
def mesh_gmsh(payload: dict) -> dict:
    """Mesh `payload["step"]` under `payload["profile"]` in a child process, within `payload["timeout_s"]`."""
    mesh_child._profile(str(payload["profile"]), 1.0)  # validates the profile name before a process starts
    if payload.get("timeout_s") is None:
        raise ValueError("mesh_gmsh needs payload['timeout_s']: a child process with no clock can hang the worker")
    timeout_s = float(payload["timeout_s"])
    # owner decision 2026-11-26: the part's thin walls, measured once per file (thin_sizing.plan_for) and sized in
    # the child; the measuring time comes out of the member's own clock
    started = time.monotonic()
    sizing = thin_sizing.plan_for(str(payload["step"]), min(thin_sizing.MEASURE_TIMEOUT_S, 0.5 * timeout_s))
    timeout_s = max(1.0, timeout_s - (time.monotonic() - started))
    with tempfile.TemporaryDirectory(prefix="mesh_gmsh_") as tmp:
        payload_path, answer_path = str(Path(tmp, "payload.json")), str(Path(tmp, "answer.json"))
        Path(payload_path).write_text(json.dumps({"step": str(payload["step"]), "profile": str(payload["profile"]),
                                                  "sizing": sizing}))
        try:
            proc = subprocess.run(
                _child_argv(payload_path, answer_path), capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            _child_event("timeout", None, payload, timeout_s, exc.stderr)
            return _stopped("timeout", None, f"gmsh exceeded {timeout_s:.0f}s")
        code = proc.returncode
        tail = (proc.stderr or "").strip()[-_STDERR_TAIL:]
        if code != 0:
            _child_event("exit", code, payload, timeout_s, proc.stderr)
        if code < 0:
            kind, cause = signal_verdict(-code)
            return _stopped("crashed", kind, f"gmsh process killed by signal {-code}: {tail}", cause)
        answer = Path(answer_path)
        if code == 0 and answer.is_file():
            return json.loads(answer.read_text())
        if code == 0:
            raise RuntimeError("mesh_gmsh child exited 0 without an answer")
        if "Traceback" in (proc.stderr or ""):
            raise RuntimeError(f"mesh_gmsh child raised (exit {code}): {tail}")
        return _stopped("crashed", MESHER_CRASH_KIND, f"gmsh process exited {code}: {tail}", f"exit_{code}")


__all__ = ["ALT_OPTIONS", "ALT_SIZE_DIVISOR", "INFRASTRUCTURE_KIND", "MIN_SICN", "algorithms", "is_gmsh_error", "judge", "mesh_gmsh", "profile_config"]
