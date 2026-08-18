# Responsibility: Run cartesianMesh (and the native cartesian2DMesh for 2D) and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from meshpipeline.engines.cfmesh.foam_exec import _DEFAULT_BASHRC, _foam_env, scan_case_dicts
from meshpipeline.sandbox.safe_exec import NativeOutcome, describe_native_result, run_guarded

logger = logging.getLogger(__name__)

#: The two cfMesh binaries, chosen by the marker `_configure_external_2d` writes.
CARTESIAN_MESH = "cartesianMesh"
CARTESIAN_2D_MESH = "cartesian2DMesh"

#: Written by `_configure_external_2d`; its presence is what makes a case 2D at run time.
TWO_D_MARKER = ".cartesian2d"

#: The front/back merge is a dictionary rewrite over an existing mesh, not a meshing run.
CREATE_PATCH_TIMEOUT_S = 300

#: A case whose dicts were rejected never reaches an executor. Not a native failure.
RC_DICTS_REJECTED = -2

#: How much of the native log travels back with the result.
LOG_TAIL_LINES = 25


def run_cartesian_mesh(workspace, *, context=None, bashrc: str = _DEFAULT_BASHRC,
                       timeout: int = 1800) -> dict:
    reason = scan_case_dicts(workspace)
    if reason:
        return {"rc": RC_DICTS_REJECTED, "timed_out": False, "log_tail": f"REJECTED: {reason}"}
    from meshpipeline.contracts.mesh_execution import run_mesh
    return run_mesh(workspace, engine="cfmesh", timeout=timeout)


def _run_cartesian_mesh_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                              timeout: int = 1800) -> dict:
    ws = Path(workspace)
    is_2d = (ws / TWO_D_MARKER).exists()
    binary = CARTESIAN_2D_MESH if is_2d else CARTESIAN_MESH
    log = ws / "cartesianMesh.log"
    cmd = f"source {bashrc} >/dev/null 2>&1 && {binary}"
    try:
        with log.open("w") as fh:
            proc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                               stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        rc, timed_out = proc.returncode, False
        if rc == 0 and is_2d and (ws / "system" / "createPatchDict").exists():
            with (ws / "createPatch.log").open("w") as fh:
                proc2 = run_guarded(["bash", "-lc",
                                     f"source {bashrc} >/dev/null 2>&1 && createPatch -overwrite"],
                                    cwd=str(ws), env=_foam_env(),
                                    stdout=fh, stderr=subprocess.STDOUT,
                                    timeout=CREATE_PATCH_TIMEOUT_S)
            rc = proc2.returncode
    except subprocess.TimeoutExpired:
        rc, timed_out = -1, True
    tail = ""
    if log.exists():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
    # The SHARED reading of a native result - signal vs ordinary failure vs timeout - so this
    # bundle does not carry its own interpretation of a negative return code.
    return describe_native_result(
        returncode=rc, args=["bash", "-lc", cmd], stage=binary, output=tail,
        outcome=NativeOutcome.timed_out if timed_out else None)


__all__ = ["CARTESIAN_2D_MESH", "CARTESIAN_MESH", "CREATE_PATCH_TIMEOUT_S", "LOG_TAIL_LINES",
           "RC_DICTS_REJECTED", "TWO_D_MARKER", "run_cartesian_mesh"]
