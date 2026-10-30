# Responsibility: Run the conjugate mesher where OpenFOAM is, from the plan the host wrote, and leave a report beside the mesh.
# Boundaries: it runs and records. The mesher is conjugate.py, the sizing is plan.py, and it judges nothing the gates judge.
# Collaborates with: engines/dispatch.py (which calls it with a workspace and nothing else) and cfmesh_conjugate/finalize.py.
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import plan as PL
from meshpipeline.engines.cfmesh_conjugate.conjugate import ConjugateError, build
from meshpipeline.engines.cfmesh_conjugate.foam_exec import _DEFAULT_BASHRC

logger = logging.getLogger(__name__)

#: What dispatch reads off a native result. rc 0 is a mesh; anything else is not.
#:
#: POSITIVE, ALL OF THEM. The shared seam reads a NEGATIVE return code as "killed by a signal" and
#: names the signal from it, so RC_NO_PLAN = -2 would have reported a workspace with no plan as a
#: mesher killed by SIGINT. A refusal and a kernel kill are the two things a reader of this most
#: needs to tell apart.
RC_OK = 0
RC_REFUSED = 1
RC_NO_PLAN = 2

#: The stage name a refusal carries when the mesher itself refused rather than a named OpenFOAM
#: run failing. `describe_native_result` prints it beside the command.
_STAGE = "conjugate"


def _result(rc: int, *, stage: str = "", output: str = "", **extra) -> dict:
    """The process result in the ONE shape every engine returns.

    THE SEAM IS CALLED, NOT COPIED (sandbox/safe_exec.describe_native_result). This runner used to
    hand-build {"rc", "timed_out", "log_tail"} and so returned five fewer keys than every other
    engine - no `signalled`, no `signal`, no `outcome`, no `command`, no `stage` - which is the
    exact divergence tests/unit/engines/test_native_signal_policy exists to refuse. The arguments
    are this bundle's own commands, so the command identity names the mesher rather than a shell.
    """
    from meshpipeline.sandbox.safe_exec import describe_native_result

    # THE COMMAND IS THE STAGE THAT FAILED, where one did: a build runs cartesianMesh, topoSet,
    # splitMeshRegions, checkMesh and foamToVTK in turn, so naming the first of them on a topoSet
    # failure would put the wrong utility in the report the reader acts on.
    out = describe_native_result(returncode=int(rc), args=[stage or "cartesianMesh"], stage=stage,
                                 output=output)
    out.update(extra)
    return out


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
        return _result(RC_NO_PLAN, stage=_STAGE, output=f"REJECTED: {exc}")

    try:
        report = build(
            ws, block_stl=PL.BLOCK_STL, fluid_stl=PL.FLUID_STL,
            background_cell_m=plan.background_cell_m, levels=plan.levels,
            thickness_m=plan.thickness_m,
            expect_fluid_volume_m3=plan.expect_fluid_volume_m3,
            expect_fluid_pieces=plan.expect_fluid_pieces,
            volume_tol=plan.volume_tol, ports=PL.ports_from(plan),
            narrowest_m=plan.narrowest_m, patch_names=PL.as_patch_names(plan),
            fine_zones=[dict(z) for z in (plan.fine_zones or ())],
            fine_surfaces=[dict(s) for s in (plan.fine_surfaces or ())],
            stretch_zones=[dict(z) for z in (plan.stretch_zones or ())],
            port_patches=[dict(p) for p in (plan.port_patches or ())],
            ranks=int(getattr(plan, "ranks", 1) or 1),
            bashrc=bashrc, timeout=int(timeout))
    except ConjugateError as exc:
        # A REFUSAL IS NOT A CRASH. The mesher refuses a case it can see is wrong - more than two
        # regions out of the split, a degenerate block, a stage that exited non-zero - and that
        # reason is the most useful thing the run produces.
        logger.info("conjugate: refused - %s", exc)
        _write_report(ws, {"ok": False, "refused": str(exc)})
        # A STAGE'S OWN EXIT CODE when one of the OpenFOAM runs is what failed, so a mesher the
        # kernel killed arrives as signalled rather than as this engine declining to trust a mesh.
        return _result(getattr(exc, "rc", None) or RC_REFUSED,
                       stage=getattr(exc, "stage", "") or _STAGE, output=f"REFUSED: {exc}")
    except Exception as exc:              # noqa: BLE001 - dispatch wants a dict, never a raise
        logger.exception("conjugate: the run failed")
        _write_report(ws, {"ok": False, "refused": f"{type(exc).__name__}: {exc}"})
        return _result(RC_REFUSED, stage=_STAGE,
                       output=f"FAILED: {type(exc).__name__}: {exc}")

    # THE SOLVER CASE AROUND THE MESH, written here because this is where the delivered patch
    # names are. Structure only - both regions, every patch named as the mesh names it, the
    # coupled condition on the interface written out in full - and every physical number left as
    # a token that is not a number. A mesh without a case is a mesh the customer has to wire up
    # themselves and the commonest way that goes wrong is a conjugate interface that runs and
    # transfers no heat; a case with an invented flow rate is worse, because it solves.
    #
    # IT NEVER FAILS THE RUN. The mesh is the deliverable and a mesh is never lost over the case
    # written beside it.
    case_files: list[str] = []
    try:
        from meshpipeline.engines.cfmesh_conjugate.solver_case import write_solver_case

        case_files = write_solver_case(
            ws, fluid_patches=dict(report.fluid.patches or {}),
            solid_patches=dict(report.solid.patches or {}),
            ports=({"inlet": report.ports.inlet, "outlet": report.ports.outlet}
                   if report.ports else None))
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: the solver case could not be written (non-fatal)",
                       exc_info=True)

    body = _as_dict(report, plan)
    body["solver_case"] = case_files
    # WRITTEN BEFORE THE AIDS AND AGAIN AFTER. The aids are the expensive part of the run - a
    # 35 MB surface and a per-region VTK export - and a report that only appeared once they
    # finished would be lost by anything that killed the process while they ran, taking the
    # measurement of a mesh that is on disk with it.
    _write_report(ws, body)
    # THE REVIEW AIDS, built here because this is where OpenFOAM is. Neither can fail the run:
    # a mesh is never lost over a picture of it.
    body["review"] = _review_aids(ws, bashrc=bashrc, refused=bool(report.problems),
                                  cells=int(report.fluid.cells + report.solid.cells))
    _write_report(ws, body)
    # THE BOUNDARY INDEX, for the same reason and with the same rule: built where the mesh is,
    # and never able to fail the run.
    body["boundary_index"] = _boundary_indexes(ws)
    _write_report(ws, body)
    tail = (f"fluid {report.fluid.cells:,} cells, solid {report.solid.cells:,}, interface "
            f"{report.interface_faces_fluid:,} faces both sides")
    if report.problems:
        tail += "; problems: " + "; ".join(report.problems)
    return _result(RC_OK, output=tail, cells=int(report.fluid.cells + report.solid.cells))


#: THE VOLUME EXPORT IS SKIPPED ABOVE THIS, when the mesh was refused. foamToVTK writes a second
#: copy of every cell, and on a mesh this size that copy is hundreds of gigabytes - measured on
#: 2026-10-29, where a 336,666,582-cell mesh whose own gate had ALREADY refused it (the coolant
#: came out in 5 pieces) was still being exported with 168 GB left on the disk, and the export had
#: to be killed by hand to stop it taking the box with it.
#:
#: A refused mesh gets no review, so the slices it would feed are slices nobody asks for. The
#: surface is still built: it is small, it is a required render artifact, and a reader of a
#: refusal wants to SEE what was refused.
#:
#: ...and only above this size, because the always-export rule is deliberate and right at the
#: sizes it was written for. native.py's own note: "ALWAYS, on every run, and that is deliberate
#: ... the way the region slices actually materialise is by the volume being THERE." A 10M-cell
#: refusal exports in seconds and a reviewer may yet be asked about it; a 300M one cannot.
VOLUME_SKIP_ABOVE_CELLS = 40_000_000


#: Above this many boundary faces on one region, the index is not built. It is a ceiling on a
#: pass that is otherwise proportional and cheap - measured 2026-10-30 on the 182,269,914-cell
#: customer plate, 105 s and 623 MB for the fluid's 13,181,685 faces and 126 s and 634 MB for the
#: solid's 13,407,661 - so this sits far above anything seen and exists to stop a pathological
#: boundary from spending a mesh job's remaining time, not to express a judgement about size.
INDEX_SKIP_ABOVE_FACES = 120_000_000


def _boundary_indexes(ws: Path) -> dict:
    """An index of each delivered region's boundary, so a reader can zoom in on a mesh of any size.

    WHY IT EXISTS. `render.detail_window` cuts one neighbourhood out of a delivered boundary at
    full resolution, for a reader who has zoomed past what the merged drawing can show. Without an
    index it has to parse the WHOLE boundary to find that neighbourhood - a face's position is not
    known until its points are - so it refuses anything over MAX_SOURCE_FACES, 2,000,000 faces.
    This plate delivers 13,181,685 on the coolant side alone. The mesh the detail window was
    written for is the mesh it will not open.

    WHY HERE. The index is that parse, done once and written down, and it belongs in the process
    that already has the mesh and the memory for it. Measured on the customer plate, 2026-10-30:
    105 s and 623 MB for the fluid side, 126 s and 634 MB for the solid, under 4% of the mesh on
    disk - against a parse that would have cost about 303 s and read a 2.7 GB points file whole
    inside an API process every time somebody clicked.

    THE REGIONS ARE THE ONES THE READER WILL ASK ABOUT, taken from `delivered_regions` rather than
    from this engine's own idea of its regions, so the set written is the set looked for. That
    function also knows to leave out the pre-split block at constant/polyMesh, which carries both
    regions' face counts added together and was never delivered.

    NO `skip_names`. One index serves every caller, and callers want different things: the main
    surface drops `solid_to_fluid` and a detail cut does not. Faces are named at read time.

    NON-FATAL, like the aids above it: a mesh is never lost over a way of looking at it.
    """
    from meshpipeline.render.boundary_index import write_index
    from meshpipeline.render.detail_window import delivered_regions

    out: dict = {"regions": {}}
    try:
        regions = delivered_regions(ws)
    except Exception as exc:              # noqa: BLE001
        logger.warning("conjugate: could not list delivered regions for the index (non-fatal)",
                       exc_info=True)
        out["why"] = f"the delivered regions could not be listed: {exc}"
        return out
    if not regions:
        out["why"] = "no delivered region has a boundary to index"
        return out

    for name, polymesh in regions:
        t0 = time.monotonic()
        try:
            declared = sum(int(p["n_faces"]) for p in _patch_rows(polymesh))
            if declared > INDEX_SKIP_ABOVE_FACES:
                out["regions"][name or "mesh"] = {
                    "ok": False,
                    "why": (f"{declared:,} boundary faces is over the {INDEX_SKIP_ABOVE_FACES:,} "
                            "this will index in a mesh job"),
                }
                logger.warning("conjugate: boundary index skipped for %s - %s faces", name,
                               format(declared, ","))
                continue
            meta = write_index(polymesh, region=name)
            out["regions"][name or "mesh"] = {
                "ok": True,
                "faces": int(meta["n_faces"]),
                "points": int(meta["n_points"]),
                "buckets": int(meta["grid"]["n_buckets"]),
                "bytes": int(sum(meta["bytes"].values())),
                "seconds": round(time.monotonic() - t0, 1),
            }
        except Exception as exc:          # noqa: BLE001
            # NAMED, not absent. A region with no index is a region whose detail window will
            # refuse, and a reader of this report should be able to see which one and why.
            logger.warning("conjugate: the boundary index failed for %s (non-fatal)", name,
                           exc_info=True)
            out["regions"][name or "mesh"] = {"ok": False, "why": str(exc),
                                              "seconds": round(time.monotonic() - t0, 1)}
    return out


def _patch_rows(polymesh: Path) -> list[dict]:
    """This region's boundary patch table, for the size check above."""
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    return read_boundary_patches(polymesh)


def _review_aids(ws: Path, *, bashrc: str, refused: bool = False, cells: int = 0) -> dict:
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

    out: dict = {"volume": None, "surface": None, "groups": {}, "bbox": None, "why": ""}
    skip = refused and int(cells) > VOLUME_SKIP_ABOVE_CELLS
    if skip:
        out["volume_skipped"] = (
            f"the mesh was refused and has {int(cells):,} cells, so the volume export - a second "
            "copy of every cell - is not written: no review will open it, and at this size it is "
            "hundreds of gigabytes of disk")
        logger.warning("conjugate: %s", out["volume_skipped"])
    else:
        try:
            out["volume"] = export_volume_vtk(ws, all_regions=True, region=FLUID_REGION,
                                              bashrc=bashrc)
        except Exception:                 # noqa: BLE001
            logger.warning("conjugate: the volume export failed (non-fatal)", exc_info=True)
    try:
        groups, bbox = build_region_msh(ws)
        if groups:
            out["surface"] = REVIEW_MSH
            out["groups"] = {str(k): [int(t) for t in v] for k, v in groups.items()}
            out["bbox"] = list(bbox) if bbox else None
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: the review surface failed (non-fatal)", exc_info=True)
    # WHY IT IS MISSING, RECORDED. `mesh_paths.surface` is a required render artifact and it is
    # converted from the VTK export above, so these two failures are one failure wearing two
    # names - and a reader of a run that could not be reviewed saw neither, only an absence.
    # The first customer mesh died exactly here: a 301 s timeout left both VTK directories empty
    # and the 1.75-hour build could not reach a verdict.
    if not out["surface"]:
        out["why"] = (
            out.get("volume_skipped") or
            ("the review surface is converted from the VTK export, and no VTK boundary "
             "was written" if not out["volume"] else
             "the VTK export was written but held no boundary to convert"))
        logger.error("conjugate: no review surface - %s", out["why"])
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
                     "max_skewness": r.max_skewness,
                     # THE REST OF WHAT CHECKMESH SAID, per region. Collapsed across the two
                     # regions these describe neither: on a conjugate mesh the coolant is the
                     # fine region and the metal the coarse one, so one pair of size numbers is
                     # an average of two different meshes.
                     "faces": int(r.faces), "points": int(r.points),
                     "cell_types": dict(r.cell_types),
                     "min_cell_m": r.min_cell_m, "max_cell_m": r.max_cell_m,
                     "max_aspect_ratio": r.max_aspect_ratio,
                     "avg_non_ortho": r.avg_non_ortho,
                     "skew_faces": int(r.skew_faces),
                     "small_determinant_cells": int(r.small_determinant_cells),
                     # WHERE the poor cells are, beside how many of them there are
                     "poor_sets": [dict(x) for x in (r.poor_sets or ())]}
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
                 "expect_fluid_pieces": plan.expect_fluid_pieces,
                 "ranks": int(getattr(plan, "ranks", 1) or 1),
                 # THE FINE SURFACES, because the near-wall reading is built from this block and
                 # cannot find them anywhere else. finalize._near_wall takes the DEEPEST level of
                 # plan["fine_surfaces"] over the coolant's own, and says why: "the thin passages
                 # get more levels than the coolant as a whole, so the coolant's own level count
                 # understates the resolution at the only wall that was hard to resolve". This
                 # block never carried them, so that loop has never run and the reading has always
                 # been the coolant's coarser one.
                 #
                 # MEASURED on the first customer plate, whose plan is levels 1 with a
                 # fine_surfaces entry at levels 4 over the 98 um microchannels and a background
                 # cell of 0.3521 mm:
                 #
                 #     published   near_wall_levels 1, near_wall_cell_m 176 um
                 #     actual      near_wall_levels 4, near_wall_cell_m  22 um
                 #
                 # Eight times too coarse, and wrong in the direction that matters: 176 um against
                 # a 98 um channel reads as a mesh that cannot resolve the thing it resolves. It is
                 # one of the six readings the customer asked for.
                 #
                 # The zones travel for the same reason - they are the other half of how a
                 # two-scale part was sized, and a reader of this report cannot otherwise tell a
                 # plan that graded the walls from one that did not.
                 "fine_surfaces": [dict(x) for x in (getattr(plan, "fine_surfaces", ()) or ())],
                 "fine_zones": [dict(x) for x in (getattr(plan, "fine_zones", ()) or ())],
                 "stretch_zones": [dict(x) for x in (getattr(plan, "stretch_zones", ()) or ())]},
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
