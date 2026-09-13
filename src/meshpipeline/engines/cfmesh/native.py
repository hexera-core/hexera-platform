# Responsibility: Run cartesianMesh (and the native cartesian2DMesh for 2D) and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from meshpipeline.engines.cfmesh.foam_exec import (
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    export_volume_vtk,
    scan_case_dicts,
)
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

#: The measurement taken beside the mesh; foam_exec.check_mesh reads it before shelling out.
QUALITY_FILE = "mesh_quality.json"

#: Wall seconds per native stage (mesher, checkMesh, passage measure, foamToVTK, total),
#: written beside the mesh and carried in the result as `timing`.
TIMING_FILE = "mesh_timing.json"


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
    # WALL TIME PER STAGE, in the result and beside the mesh. The mesher's own budget bounds
    # only the mesher; what runs after it (checkMesh, the passage measure, foamToVTK) has to
    # be visible in the run's record, or a 13-second cartesianMesh inside an 18-minute
    # execution is invisible (Cloud Run execution f5r99, 2026-09-12).
    timing: dict[str, float] = {}
    t0 = time.monotonic()
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
    timing[f"{binary}_s"] = round(time.monotonic() - t0, 1)
    tail = ""
    if log.exists():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
    # The SHARED reading of a native result - signal vs ordinary failure vs timeout - so this
    # bundle does not carry its own interpretation of a negative return code.
    out = describe_native_result(
        returncode=rc, args=["bash", "-lc", cmd], stage=binary, output=tail,
        outcome=NativeOutcome.timed_out if timed_out else None)
    # QUALITY IS MEASURED HERE, beside the mesh, and travels home with it. finalize and the
    # solvability gate call check_mesh on the worker, which carries no OpenFOAM: on the Cloud
    # Run path every figure came back empty and the reviewer refused a finished, paid-for mesh
    # for "required deterministic evidence missing ('metric:max_non_ortho',)" (HEX-6 pilot,
    # job 90f39982). Same remedy as engines/snappy/native.py: measure where the binary and the
    # bytes both are, write the figures next to the polyMesh, and read them back on the far
    # side (foam_exec.check_mesh prefers the file). The volume VTK the reviewer slices is
    # written by foamToVTK - another binary that exists only here - for the same reason.
    if rc == 0 and not timed_out and (ws / "constant" / "polyMesh" / "owner").exists():
        try:
            t1 = time.monotonic()
            q = check_mesh(ws, bashrc=bashrc)
            timing["checkMesh_s"] = round(time.monotonic() - t1, 1)
            if q:
                # the passage measure beside the mesh too: cells across the local passage at
                # every boundary point (flow_gates.resolution_floor holds 12 at the narrowest).
                # It reads the boundary files only and gives up past its own budget.
                try:
                    from meshpipeline.engines.passage import passage_of_polymesh
                    if (ws / "flow_topology").read_text().strip().lower() == "internal":
                        t2 = time.monotonic()
                        q.update(passage_of_polymesh(ws))
                        timing["passage_measure_s"] = round(time.monotonic() - t2, 1)
                except Exception:  # noqa: BLE001 - evidence, not a verdict
                    logger.warning("passage measure after meshing failed; omitted", exc_info=True)
                out["quality"] = q
                (ws / QUALITY_FILE).write_text(json.dumps(q, default=str))
        except Exception:  # noqa: BLE001 - a measurement must never lose a finished mesh
            logger.warning("checkMesh after meshing failed; quality omitted", exc_info=True)
        try:
            t3 = time.monotonic()
            if export_volume_vtk(ws, bashrc=bashrc):
                logger.info("volume VTK exported beside the mesh for review")
            timing["foamToVTK_s"] = round(time.monotonic() - t3, 1)
        except Exception:  # noqa: BLE001 - an export must never lose a finished mesh
            logger.warning("foamToVTK after meshing failed; the reviewer will have no volume "
                           "to slice", exc_info=True)
    timing["total_s"] = round(time.monotonic() - t0, 1)
    out["timing"] = timing
    try:
        (ws / TIMING_FILE).write_text(json.dumps(timing))
    except OSError:
        pass
    logger.info("cfMesh native stages (s): %s", timing)
    return out


__all__ = ["CARTESIAN_2D_MESH", "CARTESIAN_MESH", "CREATE_PATCH_TIMEOUT_S", "LOG_TAIL_LINES",
           "QUALITY_FILE", "RC_DICTS_REJECTED", "TWO_D_MARKER", "run_cartesian_mesh"]
