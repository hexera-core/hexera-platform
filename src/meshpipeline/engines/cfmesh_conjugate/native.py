# Responsibility: Run the conjugate mesher where OpenFOAM is, from the plan the host wrote, and leave a report beside the mesh.
# Boundaries: it runs and records. The mesher is conjugate.py, the sizing is plan.py, and it judges nothing the gates judge.
# Collaborates with: engines/dispatch.py (which calls it with a workspace and nothing else) and cfmesh_conjugate/finalize.py.
from __future__ import annotations

import json
import logging
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import plan as PL
from meshpipeline.engines.cfmesh_conjugate.conjugate import ConjugateError, build
from meshpipeline.engines.cfmesh_conjugate.foam_exec import _DEFAULT_BASHRC

logger = logging.getLogger(__name__)

#: What dispatch reads off a native result. rc 0 is a mesh; anything else is not.
RC_OK = 0
RC_REFUSED = 1
RC_NO_PLAN = -2


def _run_conjugate_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                         timeout: int = 3600) -> dict:
    """Mesh the block, split it, and write the report. The signature dispatch calls, exactly.

    EVERY NUMBER COMES OUT OF THE PLAN, because dispatch hands over a workspace, a bashrc and a
    timeout and nothing else - and the cell size, the tolerance, the coolant's expected volume and
    the port plan all come from geometry that never reaches this side. plan.py is that seam; a
    workspace without a plan is refused rather than meshed at a size nobody chose.

    The report is written whether the build succeeded or not, because finalize runs on the far
    side of the container boundary and a refusal it cannot read is a refusal it cannot report.
    """
    ws = Path(workspace)
    try:
        plan = PL.read_plan(ws)
    except (OSError, ValueError) as exc:
        logger.warning("conjugate: no usable plan in %s (%s)", ws, exc)
        _write_report(ws, {"ok": False, "refused": str(exc)})
        return {"rc": RC_NO_PLAN, "timed_out": False, "log_tail": f"REJECTED: {exc}"}

    try:
        report = build(
            ws, block_stl=PL.BLOCK_STL, fluid_stl=PL.FLUID_STL,
            background_cell_m=plan.background_cell_m, levels=plan.levels,
            thickness_m=plan.thickness_m,
            expect_fluid_volume_m3=plan.expect_fluid_volume_m3,
            expect_fluid_pieces=plan.expect_fluid_pieces,
            volume_tol=plan.volume_tol, ports=PL.ports_from(plan),
            narrowest_m=plan.narrowest_m, patch_names=PL.as_patch_names(plan),
            bashrc=bashrc, timeout=int(timeout))
    except ConjugateError as exc:
        # A REFUSAL IS NOT A CRASH. The mesher refuses a case it can see is wrong - more than two
        # regions out of the split, a degenerate block, a stage that exited non-zero - and that
        # reason is the most useful thing the run produces.
        logger.info("conjugate: refused - %s", exc)
        _write_report(ws, {"ok": False, "refused": str(exc)})
        return {"rc": RC_REFUSED, "timed_out": False, "log_tail": f"REFUSED: {exc}"}
    except Exception as exc:              # noqa: BLE001 - dispatch wants a dict, never a raise
        logger.exception("conjugate: the run failed")
        _write_report(ws, {"ok": False, "refused": f"{type(exc).__name__}: {exc}"})
        return {"rc": RC_REFUSED, "timed_out": False,
                "log_tail": f"FAILED: {type(exc).__name__}: {exc}"}

    body = _as_dict(report, plan)
    # WRITTEN BEFORE THE AIDS AND AGAIN AFTER. The aids are the expensive part of the run - a
    # 35 MB surface and a per-region VTK export - and a report that only appeared once they
    # finished would be lost by anything that killed the process while they ran, taking the
    # measurement of a mesh that is on disk with it.
    _write_report(ws, body)
    # THE REVIEW AIDS, built here because this is where OpenFOAM is. Neither can fail the run:
    # a mesh is never lost over a picture of it.
    body["review"] = _review_aids(ws, bashrc=bashrc)
    _write_report(ws, body)
    tail = (f"fluid {report.fluid.cells:,} cells, solid {report.solid.cells:,}, interface "
            f"{report.interface_faces_fluid:,} faces both sides")
    if report.problems:
        tail += "; problems: " + "; ".join(report.problems)
    return {"rc": RC_OK, "timed_out": False, "log_tail": tail,
            "cells": int(report.fluid.cells + report.solid.cells)}


def _review_aids(ws: Path, *, bashrc: str) -> dict:
    """The per-region volume and the two-region surface, for the reviewer, and what they are.

    WHAT THEY ARE IS RECORDED HERE because finalize runs on the other side of the container
    boundary and would otherwise have to find them again: the volume by walking the VTK tree, and
    the surface's named groups by re-reading 35 MB of mesh. Both were already worked out here.

    ALWAYS, on every run, and that is deliberate. The volume artifact is declared optional -
    requiring it would end a review before the model was asked, on a mesh that passed every gate -
    so the way the region slices actually materialise is by the volume being THERE. Exported per
    region, never as the merged block: that merged file is the very thing a conjugate review has
    to tell a real split apart from.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import export_volume_vtk
    from meshpipeline.engines.cfmesh_conjugate.review_surface import build_region_msh

    from meshpipeline.engines.cfmesh_conjugate.review_surface import REVIEW_MSH

    out: dict = {"volume": None, "surface": None, "groups": {}, "bbox": None}
    try:
        out["volume"] = export_volume_vtk(ws, all_regions=True, region=FLUID_REGION,
                                          bashrc=bashrc)
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: the volume export failed (non-fatal)", exc_info=True)
    try:
        groups, bbox = build_region_msh(ws)
        if groups:
            out["surface"] = REVIEW_MSH
            out["groups"] = {str(k): [int(t) for t in v] for k, v in groups.items()}
            out["bbox"] = list(bbox) if bbox else None
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: the review surface failed (non-fatal)", exc_info=True)
    return out


def _as_dict(report, plan: PL.ConjugatePlan) -> dict:
    """The report as finalize will read it on the other side of the container boundary."""
    return {
        "ok": not report.problems,
        "problems": list(report.problems),
        "notes": list(report.notes),
        "regions": {
            # THE FIELD NAMES ARE THE REPORT'S OWN, read off RegionReport rather than guessed at.
            # The first version of this asked for max_skew with a getattr default of 0.0 - the
            # field is max_skewness - so every mesh would have been recorded as perfectly
            # unskewed. None is carried as None: a quality number that was not measured is not
            # zero, and a gate reading 0.0 for "not measured" passes everything.
            r.name: {"cells": int(r.cells), "volume_m3": float(r.volume_m3),
                     "pieces": int(r.pieces), "mesh_ok": bool(r.mesh_ok),
                     "fatal": list(r.fatal), "patches": dict(r.patches),
                     "max_non_ortho": r.max_non_ortho,
                     "max_skewness": r.max_skewness}
            for r in (report.fluid, report.solid)
        },
        "interface_faces_fluid": int(report.interface_faces_fluid),
        "interface_faces_solid": int(report.interface_faces_solid),
        "ports": ({"inlet": report.ports.inlet, "outlet": report.ports.outlet,
                   "reason": report.ports.reason} if report.ports else None),
        "patch_names": dict(report.patch_names or {}),
        "seconds": dict(report.seconds),
        "peak_rss_bytes": int(report.peak_rss_bytes or 0),
        "plan": {"background_cell_m": plan.background_cell_m, "levels": plan.levels,
                 "cells_across": plan.cells_across, "volume_tol": plan.volume_tol,
                 "narrowest_m": plan.narrowest_m,
                 "expect_fluid_volume_m3": plan.expect_fluid_volume_m3,
                 "expect_fluid_pieces": plan.expect_fluid_pieces},
    }


def _write_report(ws: Path, body: dict) -> None:
    try:
        Path(ws, PL.REPORT_NAME).write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    except OSError:
        logger.warning("conjugate: could not write %s", PL.REPORT_NAME, exc_info=True)


def read_report(workspace) -> dict:
    """What the run left, or {} when there is none."""
    p = Path(workspace, PL.REPORT_NAME)
    if not p.is_file():
        return {}
    try:
        out = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("conjugate: %s is unreadable", PL.REPORT_NAME, exc_info=True)
        return {}
    return out if isinstance(out, dict) else {}
