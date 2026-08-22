# Responsibility: Run OpenFOAM commands for snappyHexMesh with a bounded, secret-scrubbed environment.
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
    # A measurement taken WHERE THE MESH WAS BUILT wins, because here it may be impossible.
    # checkMesh is an OpenFOAM binary; on the Cloud Run path the mesh is built in a container
    # that has one and read back by a worker that does not. Shelling out here then returned an
    # empty dict on every single run - the `cells=None` in every log line this system has
    # written - and the reviewer refused each mesh for evidence that could not be produced.
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
    # An export that arrived WITH the mesh wins, for the same reason the quality measurement does:
    # foamToVTK is an OpenFOAM binary, and on the Cloud Run path the caller runs where there is
    # none. The failure was silent - nonzero bash, no exception, an empty glob, None returned - so
    # the manifest went on declaring inspection regions that could never be rendered.
    existing = sorted(ws.glob("VTK/**/internal.vtu"))
    if existing:
        return str(existing[0].relative_to(ws))
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
