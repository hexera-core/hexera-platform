# Responsibility: Run cartesianMesh (and the native cartesian2DMesh for 2D) and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
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
    # QUALITY IS MEASURED HERE, BESIDE THE MESH, and travels home with it.
    #
    # `engines/snappy/native._run_snappy_local` has done this since the day the same defect was
    # found there, in its own words: "the local worker image carries no OpenFOAM at all, so every
    # metric came back empty ... the reviewer refused each run for missing max_non_ortho evidence
    # AFTER a mesh had been built, paid for, and passed both the manifest and solvability gates."
    # cfMesh was never given the same treatment and it fails the same way for the same reason.
    #
    # MEASURED 2026-09-27, job 1457d15f: cartesianMesh rc=0 on Cloud Run, polyMesh confirmed,
    # solvability PASSED, executor success=True, then DEAD_LETTER review_evidence_missing for
    # ('metric:max_non_ortho',). `cfmesh/criteria.py` requires that metric, so this was every
    # cfMesh run on the remote path, not an edge case.
    #
    # The stale file is removed first. `check_mesh` PREFERS mesh_quality.json when it is there,
    # which is the whole point of it, so a retry in a reused workspace would otherwise measure
    # nothing and hand back the previous attempt's numbers as if they described this mesh. A fact
    # that lies is worse than a missing one, and these numbers decide whether the customer gets
    # the mesh at all.
    if rc == 0 and (ws / "constant" / "polyMesh" / "owner").exists():
        try:
            from meshpipeline.engines.cfmesh.foam_exec import check_mesh
            stale = ws / "mesh_quality.json"
            if stale.exists():
                stale.unlink()
            q = check_mesh(ws, bashrc=bashrc)
            if q:
                (ws / "mesh_quality.json").write_text(json.dumps(q, default=str))
        except Exception:  # noqa: BLE001 - a measurement must never lose a finished mesh
            logger.warning("checkMesh after cartesianMesh failed; quality omitted", exc_info=True)
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
