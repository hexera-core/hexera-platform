# Responsibility: Run cartesianMesh (and the native cartesian2DMesh for 2D) and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from meshpipeline.cad.fluid_passages import PASSAGES_FILE
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

#: PASSAGES_FILE (cad/fluid_passages) is written by cfmesh_runner._stage_passages when the
#: confirmed openings lie on several separate passages: every passage after the first is a case
#: directory of its own (its surface only), meshed here with the same meshDict and merged in.
_SURFACE_FILE = re.compile(r'surfaceFile\s+"[^"]*"\s*;')

_BAD_FACES = re.compile(r"Number of bad faces is\s+(\d+)")
_WRONG_WAY = re.compile(r"(\d+)\s+faces pointing the wrong way")
_UNTANGLED = ("Finished untangling the mesh", "Finished renaming boundary patches")


def untangle_stall(log_tail: str) -> list[str]:
    """The broken cells a cartesianMesh run that ran out of time was still repairing, read from its
    log tail: its last untangling pass still counted bad faces, and it never reported the
    untangling finished. [] for a run that was merely slow (the soccer ball had finished untangling
    and was renaming its patches when its time ran out)."""
    text = str(log_tail or "")
    counts = list(_BAD_FACES.finditer(text))
    if not counts or int(counts[-1].group(1)) <= 0:
        return []
    if any(text.rfind(done) > counts[-1].start() for done in _UNTANGLED):
        return []
    wrong = [int(m.group(1)) for m in _WRONG_WAY.finditer(text)]
    bad = int(counts[-1].group(1))
    return [f"{bad:,} bad faces cfMesh could not untangle"
            + (f", {wrong[-1]:,} of them pointing the wrong way" if wrong else "")]


#: What the run keeps back from its cap for the steps after the mesher (passage measure, layer
#: census, foamToVTK), beyond twice the first checkMesh's time: a rescue never eats into them.
POST_MESH_RESERVE_S = 120.0


def _rescued(ws: Path, q: dict, *, bashrc: str, timing: dict, t0: float, timeout: int,
             is_2d: bool) -> dict:
    """The quality record after a near-edge rescue (engines/cfmesh/rescue.py) - `q` unchanged
    when the mesh is not near its validity edge. The rescue runs inside the run's own cap, less
    what the steps after it need, so the run still returns inside the time it was given."""
    from meshpipeline.engines.cfmesh.rescue import near_edge, rescue
    if not near_edge(q):
        return q
    t = time.monotonic()
    deadline = t0 + float(timeout) - (2.0 * float(timing.get("checkMesh_s") or 0.0)
                                      + POST_MESH_RESERVE_S)
    mesher_s = float(timing.get(f"{CARTESIAN_2D_MESH if is_2d else CARTESIAN_MESH}_s") or 0.0)
    try:
        out = rescue(ws, q, bashrc=bashrc, deadline=deadline, mesher_seconds=mesher_s,
                     is_2d=is_2d)
    except Exception:  # noqa: BLE001 - a second chance must never lose the first mesh
        logger.warning("cfMesh rescue errored; the mesh as built stands", exc_info=True)
        return q
    timing["rescue_s"] = round(time.monotonic() - t, 1)
    return out


def run_cartesian_mesh(workspace, *, context=None, bashrc: str = _DEFAULT_BASHRC,
                       timeout: int = 1800) -> dict:
    reason = scan_case_dicts(workspace)
    if reason:
        return {"rc": RC_DICTS_REJECTED, "timed_out": False, "log_tail": f"REJECTED: {reason}"}
    from meshpipeline.contracts.mesh_execution import run_mesh
    return run_mesh(workspace, engine="cfmesh", timeout=timeout)


def mesh_passages(ws: Path, *, bashrc: str = _DEFAULT_BASHRC, timeout: float = 1800.0) -> tuple[int, bool, str]:
    """THE OTHER PASSAGES of a case whose confirmed openings lie on several that do not connect
    (PASSAGES_FILE). cartesianMesh keeps the largest closed region it finds, so each passage is
    meshed in a case of its own - its surface, the case's own meshDict and controls - and merged
    into the case's mesh (mergeMeshes; patches of one name are one patch). Returns (rc, timed out,
    what went wrong in words, or ""): a passage that does not mesh fails the run, since its
    openings would otherwise come back with no faces."""
    try:
        spec = json.loads((ws / PASSAGES_FILE).read_text())
    except (OSError, ValueError) as exc:
        return 1, False, f"[CFMESH] the passages record could not be read: {exc}"
    deadline = time.monotonic() + float(timeout)
    angle = float(spec.get("feature_angle", 30.0))
    for k, rel in enumerate(spec.get("runs") or [], start=2):
        sub = (ws / str(rel)).resolve()
        if ws.resolve() not in sub.parents or not (sub / "geom.stl").exists():
            return 1, False, f"[CFMESH] passage {k}: its surface is missing ({rel})"
        shutil.rmtree(sub / "system", ignore_errors=True)
        shutil.rmtree(sub / "constant", ignore_errors=True)
        shutil.copytree(ws / "system", sub / "system")
        (sub / "constant").mkdir(exist_ok=True)

        def _left() -> float:
            return max(1.0, deadline - time.monotonic())

        def _run(cmd: str, cwd: Path, log_name: str) -> int:
            with (cwd / log_name).open("w") as fh:
                return run_guarded(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && {cmd}"], cwd=str(cwd),
                                   env=_foam_env(), stdout=fh, stderr=subprocess.STDOUT, timeout=_left()).returncode

        try:
            surface = "geom.fms" if _run(f"surfaceFeatureEdges -angle {angle:g} geom.stl geom.fms", sub,
                                         "surfaceFeatureEdges.log") == 0 and (sub / "geom.fms").exists() else "geom.stl"
            md = sub / "system" / "meshDict"
            md.write_text(_SURFACE_FILE.sub(f'surfaceFile "{surface}";', md.read_text(), count=1))
            if _run(CARTESIAN_MESH, sub, "cartesianMesh.log") != 0 or not (sub / "constant" / "polyMesh" / "owner").exists():
                tail = "\n".join((sub / "cartesianMesh.log").read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
                return 1, False, f"[CFMESH] passage {k} ({', '.join(spec.get('ports', [[]] * k)[k - 1])}) did not mesh:\n{tail}"
            if _run(f"mergeMeshes -overwrite . {shlex.quote(str(rel))}", ws, f"mergeMeshes_p{k}.log") != 0:
                tail = "\n".join((ws / f"mergeMeshes_p{k}.log").read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
                return 1, False, f"[CFMESH] passage {k} could not be merged into the mesh:\n{tail}"
        except subprocess.TimeoutExpired:
            return -1, True, f"[CFMESH] passage {k} ran out of time"
        # the merged mesh is the case's; the passage's own copy only fills the returned archive
        shutil.rmtree(sub / "constant" / "polyMesh", ignore_errors=True)
    logger.info("cfMesh: %d further passage(s) meshed and merged", len(spec.get("runs") or []))
    return 0, False, ""


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
    if rc == 0 and not timed_out and not is_2d and (ws / PASSAGES_FILE).exists():
        t_p = time.monotonic()
        rc, timed_out, why = mesh_passages(ws, bashrc=bashrc,
                                           timeout=max(60.0, timeout - (time.monotonic() - t0)))
        timing["passages_s"] = round(time.monotonic() - t_p, 1)
        if why:
            tail = (tail + "\n" + why).strip()
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
                q = _rescued(ws, q, bashrc=bashrc, timing=timing, t0=t0, timeout=timeout,
                             is_2d=is_2d)
                # the passage measure beside the mesh too: cells across the local passage at
                # every boundary point (flow_gates.resolution_floor holds 12 at the narrowest).
                # It reads the boundary files only and gives up past its own budget.
                try:
                    from meshpipeline.engines.passage import passage_of_polymesh
                    from meshpipeline.engines.workspace_facts import read_flow_topology
                    if read_flow_topology(ws) == "internal":
                        t2 = time.monotonic()
                        q.update(passage_of_polymesh(ws))
                        timing["passage_measure_s"] = round(time.monotonic() - t2, 1)
                except Exception:  # noqa: BLE001 - evidence, not a verdict
                    logger.warning("passage measure after meshing failed; omitted", exc_info=True)
                # the LAYERS cfMesh does not report, counted on the mesh where it was built: the
                # builder's run_mesh result then shows "2.0 of 5 requested", and the finalize step
                # reads the same figures back from the quality file
                try:
                    from meshpipeline.engines.cfmesh.finalize import census_quality
                    from meshpipeline.engines.workspace_facts import contract_patches
                    t4 = time.monotonic()
                    q.update(census_quality(ws, contract_patches(ws)))
                    timing["layer_census_s"] = round(time.monotonic() - t4, 1)
                except Exception:  # noqa: BLE001 - evidence, not a verdict
                    logger.warning("layer census after meshing failed; omitted", exc_info=True)
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
           "PASSAGES_FILE", "QUALITY_FILE", "mesh_passages", "RC_DICTS_REJECTED", "TWO_D_MARKER", "run_cartesian_mesh",
           "untangle_stall"]
