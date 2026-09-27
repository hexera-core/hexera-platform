# Responsibility: Run OpenFOAM commands for cfMesh with a bounded, secret-scrubbed environment.
# Boundaries: every command is time-bounded, and parse-time code directives are refused before any mesher starts.
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.sandbox.safe_exec import run_guarded

logger = logging.getLogger(__name__)

_DEFAULT_BASHRC = rtcfg.OPENFOAM_BASHRC   # single source of truth (env-overridable there)

# The pre-execution guard is ONE authoritative implementation (sandbox.foam_case_guard) shared by
# every OpenFOAM-family bundle, so a security check can never drift between engines. It scans BOTH
# system/ and constant/ and refuses every executable/include/library-loading construct (#codeStream,
# #calc, #system, #include*, libs, dynamicCode …). This bundle re-exports it under the name every
# runner + dispatch already call, so there is no second copy of the logic here.
from meshpipeline.sandbox.foam_case_guard import scan_case_dicts  # noqa: F401  (bundle re-export)


def _foam_env() -> dict:
    from meshpipeline.sandbox.safe_exec import scrubbed_subprocess_env
    return scrubbed_subprocess_env()


# #
# checkMesh  (native - quality report + fatal-only verdict)
# #
_CM = {
    "cells": re.compile(r"cells:\s+(\d+)"),
    "faces": re.compile(r"faces:\s+(\d+)"),
    "hexahedra": re.compile(r"hexahedra:\s+(\d+)"),
    "polyhedra": re.compile(r"polyhedra:\s+(\d+)"),
    "max_non_ortho": re.compile(r"non-orthogonality Max:\s+([\d.]+)"),
    "max_skewness": re.compile(r"[Mm]ax skewness\s*=\s*([\d.eE+-]+)"),
    # checkMesh prints "N highly skew faces detected" ONLY when some exceed the threshold.
    # The COUNT (not the max) is what tells production-grade from broken: a handful of skewed
    # faces localized at a wing-body junction (out of millions) is normal + solvable; the single
    # worst value is not representative. Absent pattern ⇒ zero skewed faces.
    "skew_faces": re.compile(r"(\d+)\s+highly skew faces"),
}
_FATAL = (("negative volume", "negative-volume cells"),
          ("open cell", "open cells"),
          ("incorrectly oriented", "incorrectly oriented faces"))


def check_mesh(workspace, *, bashrc: str = _DEFAULT_BASHRC, region: str = "") -> dict:
    ws = Path(workspace)
    # A MEASUREMENT TAKEN WHERE THE MESH WAS BUILT WINS, because here it may be impossible.
    # checkMesh is an OpenFOAM binary; on the Cloud Run path the mesh is built in a container that
    # has one and read back by a worker that does not, so shelling out here returns an empty dict on
    # every single run. `engines/snappy/foam_exec.check_mesh` has said exactly this since the day it
    # was fixed there. It was never done for cfMesh, and the consequence is measured:
    #
    #   MEASURED 2026-09-27, job 1457d15f. cartesianMesh ran on Cloud Run rc=0, the builder confirmed
    #   the polyMesh, the solvability gate PASSED, the executor reported success=True - and then
    #   "Reviewer: required deterministic evidence missing ('metric:max_non_ortho',)" and
    #   DEAD_LETTER review_evidence_missing. A mesh that was built, paid for and sound was destroyed
    #   for a number nothing on this side could produce. `criteria.py` requires that metric, so this
    #   is every cfMesh run on the remote path, not an edge case.
    #
    # Worse than the missing number was the disagreement about it: solvability reads the same empty
    # dict and passes, because it only reports the metric when it is present. Two readers of one
    # missing fact, reaching opposite verdicts - this codebase's signature defect, and here it costs
    # the customer the whole mesh.
    #
    # The runner writes this file next to the polyMesh it measured; it arrives with it.
    if not region:
        cached = ws / "mesh_quality.json"
        if cached.is_file():
            try:
                measured = json.loads(cached.read_text())
                if isinstance(measured, dict) and measured:
                    return measured
            except (OSError, ValueError):
                logger.warning("mesh_quality.json unreadable; measuring locally instead")
    reason = scan_case_dicts(ws)
    if reason:
        return {"mesh_ok": False, "fatal": [f"case dicts rejected: {reason}"],
                "skew_faces": 0, "skew_fraction": 0.0}
    _reg = f" -region {region}" if region else ""
    cmd = f"source {bashrc} >/dev/null 2>&1 && checkMesh -constant{_reg}"
    proc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                       capture_output=True, text=True, timeout=900)
    out = proc.stdout + proc.stderr
    q: dict = {"mesh_ok": "Mesh OK" in out}
    for k, pat in _CM.items():
        m = pat.search(out)
        if m:
            v = m.group(1)
            q[k] = float(v) if any(c in v for c in ".eE") else int(v)
    low = out.lower()
    q["fatal"] = [lbl for mk, lbl in _FATAL if mk in low]
    # skewness LOCALIZATION: fraction of faces flagged skewed. A production external-aero mesh has
    # a few skewed faces at the wing-body junction (a known limit of octree-hex+prism meshers) and
    # is fully solvable; this fraction - not the single worst value - is the honest quality signal.
    q.setdefault("skew_faces", 0)
    q["skew_fraction"] = (q["skew_faces"] / q["faces"]) if q.get("faces") else 0.0
    mr = re.search(r"Number of regions:\s*(\d+)", out)
    if mr:
        # Region count is INFORMATION, not a hard gate. A disconnected region is a defect
        # for single-region flow, but LEGITIMATE for multi-region simulations (conjugate
        # heat transfer = solid + fluid regions by design). Whether 2+ regions is wrong
        # depends on the user's request - that's the reviewer's judgment, not a blind gate.
        # The fatal gate stays reserved for universally-invalid defects.
        q["regions"] = int(mr.group(1))
    mb = re.search(r"Overall domain bounding box \(([-\d.eE+\s]+?)\)\s*\(([-\d.eE+\s]+?)\)", out)
    if mb:
        try:
            mn = [float(x) for x in mb.group(1).split()]
            mx = [float(x) for x in mb.group(2).split()]
            if len(mn) == 3 and len(mx) == 3:
                q["bounds"] = mn + mx  # [xmin,ymin,zmin,xmax,ymax,zmax] of the ACTUAL mesh
        except Exception:
            pass
    return q


def export_volume_vtk(workspace, *, bashrc: str = _DEFAULT_BASHRC, timeout: int = 300) -> str | None:
    ws = Path(workspace)
    reason = scan_case_dicts(ws)
    if reason:
        logger.warning("export_volume_vtk: %s", reason)
        return None
    cmd = f"source {bashrc} >/dev/null 2>&1 && foamToVTK -constant -noFaceZones"
    try:
        run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                    capture_output=True, text=True, timeout=timeout)
    except Exception:
        logger.exception("export_volume_vtk: foamToVTK failed")
        return None
    hits = sorted(ws.glob("VTK/**/internal.vtu"))
    return str(hits[0].relative_to(ws)) if hits else None
