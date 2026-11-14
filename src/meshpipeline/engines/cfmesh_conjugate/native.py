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
import meshpipeline.engines.determinism as det

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


def _result(rc: int, *, stage: str = "", output: str = "", outcome=None, **extra) -> dict:
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
                                 output=output, outcome=outcome)
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
    # ONE DEADLINE FOR THE WHOLE RUN, started here because this is the first thing the container
    # does. `timeout` is the run's budget (spec.run_policy.run_timeout) and the SAME number is
    # what the machine's life is derived from (gce_vm.lifetime_seconds = timeout + the priced
    # return), so everything this function starts has to come out of it: the mesher's chain, the
    # two checkMesh runs (both inside `build`, which keeps its own deadline from the same budget),
    # the review aids after them AND the boundary index after those. Before this, foamToVTK alone
    # could be allowed 7,200 s on top of a budget the chain had already spent, on a VM that would
    # be deleted first - and the boundary index was counted by nobody at all.
    started = time.monotonic()

    def _left() -> float:
        return max(0.0, float(timeout) - (time.monotonic() - started))

    try:
        plan = PL.read_plan(ws)
    except (OSError, ValueError) as exc:
        logger.warning("conjugate: no usable plan in %s (%s)", ws, exc)
        _write_report(ws, {"ok": False, "refused": str(exc)})
        return _result(RC_NO_PLAN, stage=_STAGE, output=f"REJECTED: {exc}")

    # THE RANK COUNT GOES IN THE REPRODUCIBILITY STAMP, because it changes the mesh. Measured on
    # the coupon: serial 29,812/29,262 cells against parallel 29,813/29,261 - one cell moves
    # between the regions, because topoSet classifies a cell on a processor boundary differently.
    # Two runs of the same job at different rank counts are therefore two different meshes, and
    # until now nothing in this package called note_setting at all, so the stamp could not say
    # which one you had. The mesh report carries a "ranks" field (further down this file), which
    # is not the same thing: that describes the mesh, this is what makes the run repeatable.
    _ranks = int(getattr(plan, "ranks", 1) or 1)
    # THE PLAN'S OWN WORDS WHEN IT HAS ANY. More than one demand can set this number - the
    # octree's label ceiling, and the floor the back-scale projection survives on - and a stamp
    # that picks between them from the count alone names the wrong one as soon as they disagree.
    # Job b6762d5c was planned on 8 by the stretch floor and stamped as the octree ceiling, which
    # two ranks met. A plan from before this field falls back to describing the count.
    _basis = str(getattr(plan, "ranks_basis", "") or "").strip()
    det.note_setting("conjugate_ranks", _ranks, f"the plan's own count - {_basis}" if _basis else
                     ("the plan's own count, from the mesh size against the per-process octree "
                      "ceiling" if _ranks > 1 else "one process: the mesh fits in one"))
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
        #
        # ...AND A RUN THAT RAN OUT OF TIME IS NOT A REFUSAL EITHER, which nothing was saying.
        # This engine declares a GATING criterion on `timed_out` ("Build completes within compute
        # budget", criteria.CRITERIA_ROWS) and `describe_native_result` derives that flag from the
        # outcome, so a build stopped at its budget was arriving with `timed_out` False, outcome
        # "failed" - read as a mesher that broke. Worse, the loop the driver now runs this under
        # (`contracts/native_time.run_off_the_wall`) decides RUN-5 and RUN-3 off exactly these
        # flags: with `timed_out` never set, a Cloud Run pass stopped at the 13,800 s wall still
        # working was never re-run on a VM, and a stage stopped at the memory guard never stepped
        # one machine up.
        #
        # SAID THROUGH THE SHARED SEAM, not with a second vocabulary: the outcome is `timed_out`,
        # and `safe_exec.why_it_stopped` then refines it to STUCK (`stalled`) or OUT OF MEMORY
        # (`out_of_memory`, which clears `timed_out` and keeps the peak in `used_gib`) off the
        # exception the watch raised - the same call every watched engine's runner makes.
        from meshpipeline.sandbox.safe_exec import NativeOutcome, why_it_stopped

        stopped = getattr(exc, "stopped", False)
        out = _result(getattr(exc, "rc", None) or RC_REFUSED,
                      stage=getattr(exc, "stage", "") or _STAGE, output=f"REFUSED: {exc}",
                      outcome=NativeOutcome.timed_out if stopped else None)
        return why_it_stopped(stopped, out) if isinstance(stopped, BaseException) else out
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
                                  cells=int(report.fluid.cells + report.solid.cells),
                                  budget_left_s=_left())
    _write_report(ws, body)
    # THE BOUNDARY INDEX, for the same reason and with the same rule: built where the mesh is,
    # and never able to fail the run.
    #
    # AND UNDER THE SAME DEADLINE AS EVERYTHING ELSE. This ran with NO budget at all, which put it
    # in the one place nothing was counting it: `timeout` is the engine's half of the chain and
    # gce_vm.LIFETIME_MARGIN_S is dispatch's half after the engine returns, and the index fell
    # between them. It is not free at this size - measured 2026-10-30 on the 182,269,914-cell
    # plate, 105 s for the fluid's 13,181,685 boundary faces and 126 s for the solid's 13,407,661,
    # which at the Corintis plate's 31,184,373 + 40,667,460 is about 630 s, and the per-face
    # quality reduction beside it another 264 s. A run that had already spent its budget on the
    # mesh was then allowed to spend 15 unaccounted minutes here, on a machine whose life was
    # derived from the budget it had spent.
    body["boundary_index"] = _boundary_indexes(ws, budget_left_s=_left())
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
#: A refused mesh gets no review, so the slices it would feed are slices nobody asks for.
#:
#: AND NEITHER IS THE SURFACE, WHICH THIS LINE USED TO DENY. It read "The surface is still built:
#: it is small, it is a required render artifact, and a reader of a refusal wants to SEE what was
#: refused" - and that cannot be true on this path. `review_surface.build_region_msh` converts the
#: boundary .vtp tree that foamToVTK writes, which is the very export being skipped, so with no
#: export there is no tree, `_patch_files` finds no region directory and the builder returns
#: ({}, None) having logged "no VTK boundary beside the mesh, nothing to convert". The note three
#: lines below (VOLUME_ALWAYS_ON_SUCCESS) had already worked this out for the success path and
#: says it: "The surface needs the same export too." So a refusal over this size is delivered with
#: no picture of itself at all - which is the honest cost of not writing hundreds of gigabytes for
#: a mesh its own gate has already thrown away, and is worth knowing rather than being told the
#: opposite.
#:
#: ...and only above this size, because the always-export rule is deliberate and right at the
#: sizes it was written for. native.py's own note: "ALWAYS, on every run, and that is deliberate
#: ... the way the region slices actually materialise is by the volume being THERE." A 10M-cell
#: refusal exports in seconds and a reviewer may yet be asked about it; a 300M one cannot.
VOLUME_SKIP_ABOVE_CELLS = 40_000_000

#: WHY THE SUCCESS PATH STILL EXPORTS AT ANY SIZE, decided 2026-11-12 and written down because it
#: looks like an oversight and is not.
#:
#: The two recorded export disasters do NOT condemn a clean export of this plate, and being fair
#: about that is the whole decision:
#:
#:   * the 336,666,582-cell export that had to be killed by hand had ALREADY been refused by its
#:     own coolant_pieces gate - which is exactly the case VOLUME_SKIP_ABOVE_CELLS above now
#:     catches, and it was never going to be reviewed;
#:   * the 102,630,992-cell export that gave up at 301 s leaving both VTK directories empty ran
#:     against a 300 s ceiling that has since been raised 24x (foam_exec.EXPORT_TIMEOUT_S).
#:
#: AND THE REVIEWER NEEDS IT. mesh_paths.volume is how the review inspects the interior - a
#: conformal two-region split and one merged block agree on every number the gates print and
#: differ only in a cut through the coolant - and this engine declares
#: `review_requires_the_interior=True` (spec.py), so a run that publishes no volume cannot reach a
#: verdict about the one thing it adds. Switching the export off on the success path would turn
#: every large conjugate mesh into an inconclusive review by construction. The surface needs the
#: same export too: `review_surface.build_region_msh` converts the boundary .vtp tree foamToVTK
#: writes, and mesh_paths.surface is required=True.
#:
#: SO WHAT IS BOUNDED INSTEAD IS TIME AND DISK, which is what was actually unpriced:
#:
#:   * TIME - the export now gets what is LEFT of the run's budget rather than its own
#:     EXPORT_TIMEOUT_S on top of it (see _run_conjugate_local; the machine's life is derived from
#:     that budget in adapters/mesh_execution/gce_vm.lifetime_seconds);
#:   * DISK - priced for what actually gets written, including this second copy of every cell, in
#:     gce_vm.DISK_PER_WORKSPACE_GIB, where the shares are worked out at this plate's size;
#:   * and the half of the second copy that NOTHING READS is given back as soon as it has been
#:     written, below.
#:
#: Recorded in the report either way (`review.volume_policy`), so a reader of a run never has to
#: infer which of these happened.
VOLUME_ALWAYS_ON_SUCCESS = (
    "the mesh passed its own gates, so the volume export is written at any size: it is how the "
    "review inspects the interior (spec.review_requires_the_interior) and the required review "
    "surface is converted from the same export. What is bounded is the time it may take (the "
    "rest of the run's budget) and the disk it is priced against")

#: The region whose cells NOTHING reads once the export has finished.
#:
#: `foamToVTK -allRegions` writes an internal.vtu per region - a second copy of every cell, both
#: times - and `review_surface.review_volume` returns the FLUID region's and only ever the
#: fluid's, by name, deliberately ("that merged file is the whole block as one body, which is the
#: very thing a conjugate review has to be able to tell a real split apart from"). So the metal's
#: copy is written and then read by nobody: on the Corintis plate's margined 333,869,285 cells the
#: solid is roughly three quarters of them, which at about 200 bytes a cell in VTK is some 45 GiB
#: of disk and of archive for nothing.
#:
#: IT IS NOT A NEW POLICY, it is the same one made earlier. `engines/return_scope.OPTIONAL_GLOBS`
#: is ("VTK/**/internal.vtu",) and it gives them up LARGEST FIRST when the archive will not fit -
#: so on a conjugate case the metal's copy is already the first thing dropped on the way home.
#: This drops it on the DISK instead, which is where the run can die, and before the return has to
#: compress every byte of it to decide.
#:
#: WHAT IT DOES NOT DO is lower the PEAK: the file exists while foamToVTK is writing it. The peak
#: is what gce_vm's disk price is for.
#:
#: The boundary .vtp tree of the solid STAYS - `build_region_msh` reads both regions' boundaries
#: and refuses to publish half a surface - and so does the fluid's internal.vtu, which is the
#: artifact itself.
UNREAD_VOLUME_REGION = "solid"


#: Above this many boundary faces on one region, the index is not built. It is a ceiling on a
#: pass that is otherwise proportional and cheap - measured 2026-10-30 on the 182,269,914-cell
#: customer plate, 105 s and 623 MB for the fluid's 13,181,685 faces and 126 s and 634 MB for the
#: solid's 13,407,661 - so this sits far above anything seen and exists to stop a pathological
#: boundary from spending a mesh job's remaining time, not to express a judgement about size.
INDEX_SKIP_ABOVE_FACES = 120_000_000

#: WHAT ONE BOUNDARY FACE COSTS THE INDEX, so a region can be priced against what is left of the
#: run's budget before it is started rather than discovered to be unaffordable afterwards.
#: `render.boundary_index.write_index` is one pass and has no deadline of its own, so the only
#: honest way to bound it is not to start one that cannot finish.
#:
#: Measured 2026-10-30 on the 182,269,914-cell customer plate, both halves of the pass that runs
#: per region here - the index itself and the per-face quality reduction after it:
#:
#:     region   faces        write_index   write_quality   together   a face
#:     fluid    13,181,685        105 s          34.9 s     139.9 s   10.6 us
#:     solid    13,407,661        126 s          59.9 s     185.9 s   13.9 us
#:
#: The SLOWER of the two is charged, because the metal is the region a conjugate case has more of
#: and because a skipped index is a worse outcome than a tight one. At the Corintis plate's
#: 31,184,373 + 40,667,460 faces that prices the pair at 434 s + 565 s = 999 s.
INDEX_SECONDS_PER_FACE = 1.4e-5


def _boundary_indexes(ws: Path, *, budget_left_s: float | None = None) -> dict:
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

    `budget_left_s` is what remains of the run's whole budget (see _run_conjugate_local). A region
    whose index cannot be afforded out of it is NOT STARTED - priced at INDEX_SECONDS_PER_FACE off
    the boundary table the mesh already carries - rather than started and then killed with the VM.
    None means "unbounded by the run", which is what every caller outside the pipeline wants.
    """
    from meshpipeline.render.boundary_index import INDEX_DIRNAME
    from meshpipeline.render.detail_window import delivered_regions

    out: dict = {"regions": {}}
    deadline = None if budget_left_s is None else time.monotonic() + max(0.0, float(budget_left_s))
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

    # AND IT IS WATCHED WHILE IT RUNS. `write_index` is one numpy pass that prints nothing and
    # writes its sidecar only at the end, so for the 999 s this pair is priced at (434 s + 565 s at
    # the plate's 31,184,373 + 40,667,460 faces) NOTHING under the workspace moved - and a `*.log`
    # under the workspace moving is precisely what the job counts as progress
    # (`runtime/mesh_runner._output_mark`). The solid's share alone is 55% of PROGRESS_GRACE_S,
    # and this phase follows an export and two checkMesh runs that were silent for the same
    # reason, so the quiet stretches add up long before either of them. The watch relays this
    # process's own signs of life - its CPU, its memory, and these index files appearing - into a
    # workspace log, and reports nothing at all for a pass that has hung
    # (engines/dispatch.watching_progress).
    from meshpipeline.engines.dispatch import watching_progress

    with watching_progress(ws, phase=INDEX_PHASE,
                           watch_paths=(str(ws / "constant" / "*" / "polyMesh" / INDEX_DIRNAME
                                            / "*"),)):
        _index_regions(ws, regions, out, deadline=deadline)
    return out


#: The phase names the two quiet in-process passes are watched under (engines/dispatch.PHASE_LOG).
INDEX_PHASE = "boundary_index"
SURFACE_PHASE = "review_surface"


def _index_regions(ws: Path, regions, out: dict, *, deadline: float | None) -> None:
    """One index per delivered region, inside the run's remaining budget. See `_boundary_indexes`,
    which is where the deadline and the watch come from; split out only so that function reads as
    the one decision it makes."""
    from meshpipeline.render.boundary_index import write_index

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
            want = declared * INDEX_SECONDS_PER_FACE
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            if left is not None and want > left:
                # SAID AS THE RESOURCE IT IS, not as an index that failed. The mesh is on disk and
                # what ran out is the run's time; a reader has to be able to tell that apart from
                # a write_index that broke, which the branch below reports.
                out["regions"][name or "mesh"] = {
                    "ok": False,
                    "why": (f"{declared:,} boundary faces price at about {want:.0f}s and the "
                            f"run's budget has {left:.0f}s left, so no index was built for this "
                            "region (the detail window will refuse to open it)"),
                }
                logger.error("conjugate: boundary index not started for %s - %s", name,
                             out["regions"][name or "mesh"]["why"])
                continue
            meta = write_index(polymesh, region=name)
            out["regions"][name or "mesh"] = {
                "ok": True,
                "faces": int(meta["n_faces"]),
                "points": int(meta["n_points"]),
                "buckets": int(meta["grid"]["n_buckets"]),
                "bytes": int(sum(meta["bytes"].values())),
                "seconds": round(time.monotonic() - t0, 1),
                "quality": _boundary_quality(polymesh, region=name),
            }
        except Exception as exc:          # noqa: BLE001
            # NAMED, not absent. A region with no index is a region whose detail window will
            # refuse, and a reader of this report should be able to see which one and why.
            logger.warning("conjugate: the boundary index failed for %s (non-fatal)", name,
                           exc_info=True)
            out["regions"][name or "mesh"] = {"ok": False, "why": str(exc),
                                              "seconds": round(time.monotonic() - t0, 1)}


def _boundary_quality(polymesh: Path, *, region: str) -> dict:
    """Reduce checkMesh's fields to one value per boundary face, and clear the fields away.

    IT RUNS AFTER THE INDEX and is numbered against it, so it can only exist where one does.
    Failing here costs the colours and nothing else: the detail window still opens, and a cut
    without quality is a cut with no colour rather than a wrong one.

    THE RAW FIELDS DO NOT TRAVEL. checkMesh writes them beside the polyMesh and they are large.
    Measured on the customer plate's 182,269,914-cell mesh, 2026-10-30, both regions end to end:

        region   cells         checkMesh   fields written   reduced   reduce
        fluid     75,162,351      652 s         1.80 GB     158.2 MB    34.9 s
        solid    107,107,563      878 s         2.45 GB     160.9 MB    59.9 s

    So 4.25 GB becomes 319 MB, and only the 319 MB is of any use to anything downstream. They are
    this step's intermediate and nothing else reads them, so they go as soon as they have been
    read. On failure they are LEFT, because then they are the evidence of what went wrong.

    THE REDUCED FIGURES ARE CHECKMESH'S OWN, which is the check that the reduction is right: on
    that mesh the boundary's skewness maxima came out 0.70451 and 0.69126 against checkMesh's
    "Max skewness = 0.704509" and "= 0.69126", and the solid's aspect ratio 2.09807 against its
    "Max aspect ratio = 2.09807". Non-orthogonality comes out LOWER than the whole-mesh figure
    (15.70 against 18.72, 15.54 against 22.36) and must: it is the owner cell's value over the
    cells that touch the boundary, which are a subset of the mesh.
    """
    from meshpipeline.render.boundary_quality import FIELD_FILES, write_quality

    t0 = time.monotonic()
    try:
        meta = write_quality(polymesh, region=region)
    except Exception as exc:              # noqa: BLE001
        logger.warning("conjugate: no per-face quality for %s (non-fatal)", region, exc_info=True)
        return {"ok": False, "why": str(exc), "seconds": round(time.monotonic() - t0, 1)}

    cleared = 0
    for fname in FIELD_FILES.values():
        p = Path(polymesh).parent / fname
        try:
            if p.is_file():
                cleared += p.stat().st_size
                p.unlink()
        except OSError:
            logger.warning("conjugate: could not clear %s after reducing it", p, exc_info=True)
    return {"ok": True, "metrics": sorted(meta["basis"]), "basis": dict(meta["basis"]),
            "bytes": int(sum(meta["bytes"].values())), "fields_cleared_bytes": int(cleared),
            "notes": dict(meta.get("notes") or {}),
            "seconds": round(time.monotonic() - t0, 1)}


def _patch_rows(polymesh: Path) -> list[dict]:
    """This region's boundary patch table, for the size check above."""
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    return read_boundary_patches(polymesh)


def _volume_bytes(ws: Path, region: str) -> int:
    """The bytes of one region's exported cells, 0 when it was not written."""
    return sum(p.stat().st_size for p in (ws / "VTK" / region).glob("*/internal.vtu")
               if p.is_file())


def _drop_unread_volume(ws: Path, out: dict) -> None:
    """Give back the metal's copy of every cell, which nothing reads. See UNREAD_VOLUME_REGION."""
    freed = 0
    for p in sorted((ws / "VTK" / UNREAD_VOLUME_REGION).glob("*/internal.vtu")):
        try:
            if p.is_file():
                freed += p.stat().st_size
                p.unlink()
        except OSError:
            logger.warning("conjugate: could not drop %s after the export", p, exc_info=True)
    if freed:
        out["volume_dropped_bytes"] = int(freed)
        out["volume_dropped"] = (
            f"the {UNREAD_VOLUME_REGION} region's cells ({freed / 2**30:.2f} GiB) were exported "
            "and then dropped: the review opens the coolant's volume and only the coolant's, and "
            "return_scope already gives this file up first on the way home")
        logger.info("conjugate: %s", out["volume_dropped"])


def _review_aids(ws: Path, *, bashrc: str, refused: bool = False, cells: int = 0,
                 budget_left_s: float | None = None) -> dict:
    """The per-region volume and the two-region surface, for the reviewer, and what they are.

    WHAT THEY ARE IS RECORDED HERE because finalize runs on the other side of the container
    boundary and would otherwise have to find them again: the volume by walking the VTK tree, and
    the surface's named groups by re-reading 35 MB of mesh. Both were already worked out here.

    ALWAYS ON THE SUCCESS PATH, at any size, and that is a decision rather than an oversight:
    VOLUME_ALWAYS_ON_SUCCESS says why, and what is bounded instead. A REFUSED mesh over
    VOLUME_SKIP_ABOVE_CELLS is the one case that does not export. Exported per region, never as
    the merged block: that merged file is the very thing a conjugate review has to tell a real
    split apart from.

    `budget_left_s` is what remains of the run's whole budget (see _run_conjugate_local). The
    export is bounded by it rather than by EXPORT_TIMEOUT_S on top of it, because the machine's
    own life is derived from that same budget (gce_vm.lifetime_seconds) - an export allowed 7,200 s
    after a chain that had already spent the budget is an export that finishes after the VM has
    been deleted. None means "unbounded by the run", which is what every caller outside the
    pipeline wants.

    AND THE SURFACE IS BOUNDED BY WHAT THE EXPORT LEFT OF IT, which it was not: the export took
    `budget_left_s` and `build_region_msh` was handed nothing at all, which is the same shape as
    the unpriced boundary index this file's own header records. It reads the whole .vtp tree
    through pyvista - 71.9 M faces at the plate - and both halves of it are linear in that count
    (review_surface.SURFACE_SECONDS_PER_FACE carries the measurement), so a boundary that cannot
    be converted inside what is left is not started rather than killed with the machine. The
    remaining budget is recomputed here rather than re-using the figure the export was given,
    because the export may have spent most of it.
    """
    from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import (
        EXPORT_TIMEOUT_S,
        export_volume_vtk,
    )
    from meshpipeline.engines.cfmesh_conjugate.review_surface import REVIEW_MSH, build_region_msh

    out: dict = {"volume": None, "surface": None, "groups": {}, "bbox": None, "why": ""}
    started = time.monotonic()
    skip = refused and int(cells) > VOLUME_SKIP_ABOVE_CELLS
    allow = (EXPORT_TIMEOUT_S if budget_left_s is None
             else min(EXPORT_TIMEOUT_S, float(budget_left_s)))
    if skip:
        out["volume_skipped"] = (
            f"the mesh was refused and has {int(cells):,} cells, so the volume export - a second "
            "copy of every cell - is not written: no review will open it, and at this size it is "
            "hundreds of gigabytes of disk")
        logger.warning("conjugate: %s", out["volume_skipped"])
    elif allow <= 0.0:
        # THE BUDGET WAS ALREADY SPENT. Said as the resource it is, not as an export that failed:
        # the mesh is on disk and what ran out is the run's time, which a reader has to be able to
        # tell apart from a foamToVTK that broke.
        out["volume_skipped"] = (
            "the run's budget was spent before the volume export could start, so it was not "
            "attempted (CONJUGATE_RUN_TIMEOUT_S is the budget, and the machine's life is derived "
            "from it)")
        logger.error("conjugate: %s", out["volume_skipped"])
    else:
        out["volume_policy"] = VOLUME_ALWAYS_ON_SUCCESS if not refused else (
            f"the mesh was refused but has only {int(cells):,} cells, under the "
            f"{VOLUME_SKIP_ABOVE_CELLS:,} this will not export, so a reviewer can still be "
            "shown what was refused")
        # HOW MUCH ROOM THERE WAS, recorded before the export rather than inferred after it.
        # Running out of disk is what forced a 336 M-cell export to be killed by hand, and
        # `foam_exec.export_volume_vtk` does not look at foamToVTK's exit code at all - it globs
        # for what landed - so an export stopped by ENOSPC and one stopped by anything else arrive
        # here looking identical. This is the one number that tells them apart.
        try:
            import shutil

            usage = shutil.disk_usage(ws)
            out["disk_free_bytes_before_export"] = int(usage.free)
        except OSError:
            logger.warning("conjugate: could not read the free disk before the export",
                           exc_info=True)
        try:
            out["volume"] = export_volume_vtk(ws, all_regions=True, region=FLUID_REGION,
                                              bashrc=bashrc, timeout=allow)
        except Exception:                 # noqa: BLE001
            logger.warning("conjugate: the volume export failed (non-fatal)", exc_info=True)
        if out["volume"]:
            out["volume_bytes"] = _volume_bytes(ws, FLUID_REGION)
        # OUTSIDE THE `if`, AND THAT IS THE WHOLE POINT. This sat inside it, so the one case where
        # the disk is most at risk reclaimed NOTHING. `foam_exec.export_volume_vtk` returns None
        # when run_guarded raises on a timeout, and newest()/prefer_finished then reject an
        # unmarked time directory - so `out["volume"]` is falsy EXACTLY when foamToVTK was killed
        # partway, which bounding the export by what is left of the run makes more likely rather
        # than less. At the plate's size that left up to about 62 GiB of .vtu nothing can read on
        # the disk, which `engines/dispatch` then hands to `return_scope`, which compresses every
        # byte of it to decide whether the archive fits. A partly written file is worth no less to
        # the disk than a finished one and worth nothing at all to a reader.
        _drop_unread_volume(ws, out)
    # WATCHED FOR THE SAME REASON THE INDEX IS, and the one that is left of the four silent
    # windows: this is a pyvista read of the whole boundary tree followed by a gmsh write, and
    # neither prints a line into the workspace. Measured at 2.05 us a face (see
    # review_surface.SURFACE_SECONDS_PER_FACE), the plate's 71.9 M faces are about 147 s - inside
    # PROGRESS_GRACE_S on its own, and not inside it once it follows an export and two checkMesh
    # runs that used to be silent too.
    from meshpipeline.engines.dispatch import watching_progress

    left = None if budget_left_s is None else max(0.0, float(budget_left_s)
                                                  - (time.monotonic() - started))
    try:
        fit: dict = {}
        with watching_progress(ws, phase=SURFACE_PHASE, watch_paths=(str(ws / REVIEW_MSH),)):
            groups, bbox = build_region_msh(ws, into=fit, budget_left_s=left)
        out.update(fit)
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
    #
    # THE BUILDER'S OWN REFUSAL COMES FIRST, because it is the one case where the export is fine
    # and the surface is still absent: `review_surface.REFUSE_ABOVE_FACES` declines a boundary
    # bigger than a mesh job will walk, and "no VTK boundary was written" of an export that was
    # written and read sends a reader to look for a bug that is not there.
    #
    # AND IT IS `surface_refused`, NOT `refused`. The key the builder writes lands in the report
    # under `review`, one level below the top-level `refused` that means "the mesher refused this
    # case" and that `finalize.py` branches on to return success=False. Two keys of the same name
    # meaning different things in one document is a defect waiting for the first piece of code
    # that flattens or searches it, which would read a reduced-surface note as a failed mesh. The
    # longer name costs nothing and matches `surface_reduced` beside it.
    if not out["surface"]:
        out["why"] = (
            out.get("surface_refused") or
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
                     "concave_cells": int(r.concave_cells),
                     "low_quality_tet_faces": int(r.low_quality_tet_faces),
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
