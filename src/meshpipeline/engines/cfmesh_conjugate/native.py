# Responsibility: Run the conjugate mesher where OpenFOAM is, from the plan the host wrote, and leave a report beside the mesh.
# Boundaries: it runs and records. The mesher is conjugate.py, the sizing is plan.py, and it judges nothing the gates judge.
# Collaborates with: engines/dispatch.py (which calls it with a workspace and nothing else) and cfmesh_conjugate/finalize.py.
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import meshpipeline.engines.determinism as det
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


def note_plan_settings(plan: PL.ConjugatePlan) -> None:
    """Put everything about this run that CHANGES THE MESH into the reproducibility stamp.

    THE RANK COUNT, because parallel changes the mesh. Measured on the coupon: serial
    29,812/29,262 cells against parallel 29,813/29,261 - one cell moves between the regions,
    because topoSet classifies a cell on a processor boundary differently. Two runs of one job at
    different rank counts are two different meshes, and for a long time nothing in this package
    called note_setting at all, so the stamp could not say which one you had. The mesh report
    carries a "ranks" field too, which is not the same thing: that describes the MESH, this is what
    makes the RUN repeatable.

    ITS BASIS IN THE PLAN'S OWN WORDS when it has any. More than one demand can set that number -
    the octree's label ceiling, and the floor the back-scale projection survives on - and a stamp
    that picks between them from the count alone names the wrong one as soon as they disagree. Job
    b6762d5c was planned on 8 by the stretch floor and stamped as the octree ceiling, which two
    ranks met.

    AND THE RESOLUTION, which changes the mesh far more than the rank count and was stamped
    nowhere. The plan file is hashed as an input, so two runs at different cell sizes cannot
    collide - but a digest says only THAT they differ, and a reader comparing two meshes of one
    part needs to know WHICH knob moved. The pinned cell is a separate row from the background
    because "the planner chose this" and "an operator pinned this" are different claims about the
    same number (engines.determinism.CONJUGATE_FINE_CELL_M).

    IT IS ITS OWN FUNCTION so it can be tested against `det.recording()` rather than by running a
    mesh or by reading source, which is how every other setting in the stamp is tested.
    """
    ranks = int(getattr(plan, "ranks", 1) or 1)
    basis = str(getattr(plan, "ranks_basis", "") or "").strip()
    det.note_setting("conjugate_ranks", ranks, f"the plan's own count - {basis}" if basis else
                     ("the plan's own count, from the mesh size against the per-process octree "
                      "ceiling" if ranks > 1 else "one process: the mesh fits in one"))
    sizing = str(getattr(plan, "sizing_basis", "") or "").strip()
    det.note_setting("conjugate_background_cell_m", float(plan.background_cell_m),
                     sizing or "carried in the plan; this plan predates sizing_basis")
    det.note_setting("conjugate_octree_levels", int(plan.levels),
                     "octree levels under the background, from the plan")
    # THE LAYER COUNT, which moves 21% of the delivered cells on the measured part - more than
    # anything else in this stamp except the cell size itself.
    det.note_setting("conjugate_layers", int(getattr(plan, "layers", 0) or 0),
                     "prism layers asked for on the conjugate interface, from the plan")
    fine = [int(s.get("levels", 0) or 0) for s in (getattr(plan, "fine_surfaces", ()) or ())]
    if fine:
        det.note_setting("conjugate_fine_surface_levels", fine,
                         "levels under the background for each refined surface, from the plan")
    # FROM THE PLAN, NEVER FROM THIS PROCESS'S ENVIRONMENT. The pin is the host's decision and
    # the container's environment is assembled from declared pair lists rather than forwarded
    # wholesale, so asking the environment here returns "nothing pinned" on precisely the runs
    # that WERE pinned - the stamp inverted on every hosted run. Same seam rule as every other
    # decision in the plan.
    pinned = float(getattr(plan, "fine_cell_pinned_m", 0.0) or 0.0)
    det.note_setting("conjugate_fine_cell_pinned_m", pinned or None,
                     "pinned by CONJUGATE_FINE_CELL_M on the host, so the planner did NOT choose "
                     "this resolution" if pinned else
                     "nothing pinned: the planner chose the resolution")
    if pinned and bool(getattr(plan, "under_planner_floor", False)):
        det.note_setting("conjugate_under_planner_floor", True,
                         "the pinned cell resolves the thin passages BELOW the floor the planner "
                         "holds; it was pinned there deliberately")


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

    note_plan_settings(plan)
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
            outer_patch=str(getattr(plan, "outer_patch", "") or ""),
            ranks=int(getattr(plan, "ranks", 1) or 1),
            layers=int(getattr(plan, "layers", 0) or 0),
            merge=bool(getattr(plan, "merge_regions", True)),
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
    # WHAT "REFUSED" MEANS HERE, and it is not "has any problem at all". `report.problems` holds
    # every reading `verify` takes, and two of them - "checkMesh did not report Mesh OK", once per
    # region - gate nothing: there is no `mesh_ok` criterion in this engine's CRITERIA_ROWS, and
    # both regions of a perfectly deliverable conjugate mesh fail the same five -allGeometry
    # checks. Passing `bool(report.problems)` therefore called a mesh refused that its own gates
    # were about to pass, skipped the export above VOLUME_SKIP_ABOVE_CELLS, and left the surface
    # unbuilt.
    #
    # MEASURED, job 57b613d7, 2026-11-15, and it is the whole reason the reviewer has never
    # produced a verdict: `quality_floor` PASSED, every declared criterion passed, the executor
    # recorded success=True and the reviewer STARTED for the first time in eleven jobs - then
    # refused with "required artifact 'mesh_paths.surface' does not exist", because this call had
    # already decided the mesh was refused and skipped the export its evidence is built from.
    #
    # finalize.py drew exactly this line once before, for the run's success: "SUCCESS IS 'A MESH
    # CAME OUT', NOT 'THE MESH IS GOOD'", written after a 367-piece coolant made mesh_ok False and
    # NOT ONE of the eight gates ran. This is the same distinction in the one place that fix did
    # not reach, so it uses the same test `finalize.a_mesh_came_out` does: cells in both regions
    # and no fatal checkMesh class. Everything else is for the gates to judge - and for the
    # reviewer to be shown.
    _no_mesh = not (int(report.fluid.cells) and int(report.solid.cells))
    _fatal = bool(report.fluid.fatal) or bool(report.solid.fatal)
    # WHICH OF THESE TWO PASSES RUNS FIRST depends on where the review surface's evidence lives,
    # and on a decomposed delivery that is the boundary index.
    #
    # `_review_aids` builds the reviewer's surface from the .vtp tree foamToVTK writes, and falls
    # back to the boundary index when there is no tree (`review_surface.build_region_msh`, added
    # after job 57b613d7). A DECOMPOSED DELIVERY HAS NO TREE AND CAN HAVE NONE: foamToVTK reads
    # `constant/<region>/polyMesh`, which on that path carries a patch table and no cells, and
    # `foamToVTK -parallel` is not the way out because every consumer of its output is written for
    # the serial layout. So the fallback is the only way in - and in the order this used to run,
    # it would be reading a sidecar `_boundary_indexes` had not written yet, on every single run.
    # `mesh_paths.surface` is required=True, so that is the whole review.
    #
    # A MERGED DELIVERY KEEPS THE ORDER IT WAS MEASURED IN. Its review surface comes from the
    # export, which `build_region_msh` prefers whenever it is there and says why - so nothing
    # about it reads the index. The two passes also share one budget, so swapping them swaps which
    # of them a run that has run out of time loses, and that is a change nobody asked for on the
    # path that works.
    for _pass in _tail_passes(ws):
        if _pass == TAIL_REVIEW:
            body["review"] = _review_aids(ws, bashrc=bashrc, refused=_no_mesh or _fatal,
                                          cells=int(report.fluid.cells + report.solid.cells),
                                          budget_left_s=_left(), plan=plan,
                                          layers=dict(getattr(report, "layers", None) or {}))
        else:
            body["boundary_index"] = _boundary_indexes(ws, budget_left_s=_left())
        # AFTER EACH, NOT AFTER BOTH. The report on disk is what a run that is killed between
        # them leaves behind, and `_left()` is read fresh for the second so it is priced against
        # what the first actually spent.
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


#: The two non-fatal passes that run after the mesh is built and the report is first written.
TAIL_REVIEW = "review"
TAIL_INDEX = "boundary_index"


def _tail_passes(ws) -> tuple[str, ...]:
    """The order those two run in, which is decided by where the review surface's evidence lives.

    `_review_aids` builds the reviewer's surface from the .vtp tree foamToVTK writes, and falls
    back to the boundary index when there is no tree (`review_surface.build_region_msh`, added
    after job 57b613d7). A DECOMPOSED DELIVERY HAS NO TREE AND CAN HAVE NONE: foamToVTK reads
    `constant/<region>/polyMesh`, which on that path carries a patch table and no cells, and
    `foamToVTK -parallel` is not the way out because every consumer of its output is written for
    the serial layout. So the fallback is the only way in - and in the other order it reads a
    sidecar that has not been written yet, on every single run. `mesh_paths.surface` is
    required=True, so that is the whole review.

    A MERGED DELIVERY KEEPS THE ORDER IT WAS MEASURED IN. Its review surface comes from the
    export, which `build_region_msh` prefers whenever it is there and says why, so nothing about
    it reads the index. The two passes also share one budget, so swapping them swaps which of them
    a run that has run out of time loses - a change nobody asked for on the path that works.

    RETURNED RATHER THAN BRANCHED ON, so the order is a value a test can read off a real
    workspace. It was an `if` first, and a mutation that pinned the condition to False left every
    test passing: they could all see the branch and none of them could see the order.
    """
    return ((TAIL_INDEX, TAIL_REVIEW) if _delivered_decomposed(ws)
            else (TAIL_REVIEW, TAIL_INDEX))


def _delivered_decomposed(ws: Path) -> bool:
    """Whether any delivered region's cells live in per-rank meshes rather than beside its
    boundary file.

    The same question `_index_regions` asks per region, asked once here for one reason only: to
    decide which of two passes runs first. It is not the per-region decision - that one is taken
    again where the builder is chosen, off the same predicate, so this cannot send a merged region
    down a decomposed path or the other way round.

    NEVER RAISES. Getting the ORDER of two non-fatal passes wrong is not worth failing a mesh
    over, so an unreadable workspace keeps the order that has always been measured.
    """
    from meshpipeline.render.boundary_index import index_is_decomposed
    from meshpipeline.render.detail_window import delivered_regions

    try:
        return any(index_is_decomposed(ws, pm) for _name, pm in delivered_regions(ws))
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: could not tell whether this delivery is decomposed, so the "
                       "boundary index keeps its usual place after the review aids",
                       exc_info=True)
        return False


#: The phase names the two quiet in-process passes are watched under (engines/dispatch.PHASE_LOG).
INDEX_PHASE = "boundary_index"
SURFACE_PHASE = "review_surface"


def _index_regions(ws: Path, regions, out: dict, *, deadline: float | None) -> None:
    """One index per delivered region, inside the run's remaining budget. See `_boundary_indexes`,
    which is where the deadline and the watch come from; split out only so that function reads as
    the one decision it makes."""
    from meshpipeline.render.boundary_index import (
        index_is_decomposed,
        write_index,
        write_index_decomposed,
    )

    def _one(name, polymesh, decomposed: bool) -> dict:
        """One region's sidecar, on its own thread.

        IT TOUCHES NOTHING ANOTHER THREAD TOUCHES. `write_index` writes under this region's own
        `polyMesh/<INDEX_DIRNAME>` (boundary_index.index_dir) and `_boundary_quality` reads this
        region's own boundary; the one question that must be answered identically for both
        sidecars - `index_is_decomposed` - is settled in the loop below, before either starts,
        and handed in. Nothing here writes to `out`; the caller does that with the result.
        """
        t0 = time.monotonic()
        try:
            # SAME PRICE PER FACE for the two paths, and that is measured rather than assumed
            # equal: the ascii read is the cost, and a decomposed region's ranks between them
            # scan exactly the internal faces a merged one scans in a single file. The binary
            # form is strictly cheaper - two seeks instead of a walk - so INDEX_SECONDS_PER_FACE
            # stays the ceiling for both and no second constant is invented for this path.
            meta = (write_index_decomposed(ws, region=name) if decomposed
                    else write_index(polymesh, region=name))
            return {
                "ok": True,
                "faces": int(meta["n_faces"]),
                "points": int(meta["n_points"]),
                "buckets": int(meta["grid"]["n_buckets"]),
                "bytes": int(sum(meta["bytes"].values())),
                "seconds": round(time.monotonic() - t0, 1),
                # WHICH MESH IT WAS BUILT FROM, in the report. A reader comparing this run's
                # figures against another's needs to know, and it is the one thing about the
                # sidecar that cannot be read back off the arrays.
                "decomposed": bool(decomposed),
                "quality": _boundary_quality(polymesh, region=name,
                                             case=ws if decomposed else None),
            }
        except Exception as exc:          # noqa: BLE001
            # NAMED, not absent. A region with no index is a region whose detail window will
            # refuse, and a reader of this report should be able to see which one and why.
            logger.warning("conjugate: the boundary index failed for %s (non-fatal)", name,
                           exc_info=True)
            return {"ok": False, "why": str(exc), "seconds": round(time.monotonic() - t0, 1)}

    # WHICH REGIONS ARE WORTH STARTING, decided serially and before any of them starts, because
    # these are the decisions that have to see each other: the skip ceiling, the budget, and
    # which mesh each region actually is.
    todo: list = []
    for name, polymesh in regions:
        t0 = time.monotonic()
        try:
            # WHICH MESH THIS REGION ACTUALLY IS, decided ONCE here and handed to both sidecars.
            # They are numbered against each other face for face, so an index built from the
            # ranks beside a quality array reduced from a serial mesh would paint one mesh's
            # numbers on another's faces and look entirely plausible. One question, one answer,
            # both callers - and now also one answer per region BEFORE either thread starts, so
            # two concurrent builds cannot disagree about it.
            #
            # A DECOMPOSED REGION IS NOT A SPECIAL CASE OF A SMALL ONE. With
            # CONJUGATE_MERGE_REGIONS=0 the region's polyMesh holds the patch table
            # `_publish_decomposed_boundaries` wrote and no cells at all, so `write_index` would
            # refuse it by name and the detail window would refuse every region of the delivery.
            decomposed = index_is_decomposed(ws, polymesh)
            # THE DECLARED COUNT IS RIGHT EITHER WAY: the published boundary file carries the
            # ranks' summed counts, so `_patch_rows` reads the same number for a decomposed
            # region that a merged one has.
            declared = sum(int(p["n_faces"]) for p in _patch_rows(polymesh))
        except Exception as exc:          # noqa: BLE001
            logger.warning("conjugate: could not size the boundary index for %s (non-fatal)",
                           name, exc_info=True)
            out["regions"][name or "mesh"] = {"ok": False, "why": str(exc),
                                              "seconds": round(time.monotonic() - t0, 1)}
            continue
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
        # PRICED AGAINST THE WHOLE REMAINING BUDGET, for every region, which is right BECAUSE
        # they now run together: the pair costs the slower of the two and not their sum, so
        # charging the second region for the first one's time would refuse an index that fits.
        if left is not None and want > left:
            # SAID AS THE RESOURCE IT IS, not as an index that failed. The mesh is on disk and
            # what ran out is the run's time; a reader has to be able to tell that apart from a
            # write_index that broke, which `_one` reports.
            out["regions"][name or "mesh"] = {
                "ok": False,
                "why": (f"{declared:,} boundary faces price at about {want:.0f}s and the "
                        f"run's budget has {left:.0f}s left, so no index was built for this "
                        "region (the detail window will refuse to open it)"),
            }
            logger.error("conjugate: boundary index not started for %s - %s", name,
                         out["regions"][name or "mesh"]["why"])
            continue
        todo.append((name, polymesh, decomposed))

    if not todo:
        return
    if len(todo) == 1:
        _name, _pm, _dec = todo[0]
        out["regions"][_name or "mesh"] = _one(_name, _pm, _dec)
        return

    # AT ONCE, ON THREADS, AND THE THREADS WERE NOT OBVIOUS. The ascii read is the whole cost of
    # this pass, and while it went through `re.findall` the GIL made concurrency worthless -
    # measured 1.04x on two threads. `render/boundary_index` no longer uses a regex, and
    # `np.fromstring` releases the GIL: re-measured on two files in the plate's own proportion
    # (1.3M and 1.7M records, 98.0 MiB), serial 1.21 s, two threads 0.61 s (1.99x), two
    # PROCESSES 0.67 s (1.80x, paying for the pickling). So threads, and the earlier
    # "processes, decided" answer belonged to the earlier code.
    #
    # MEMORY IS THE OBJECTION AND IT DOES NOT BITE HERE. The pair peaked at 25.0 GiB of 512 run
    # one after the other; holding both regions' arrays at once is twice that at worst, on a box
    # chosen for 512. `engines/dispatch.watching_progress` reads the whole process tree, so the
    # threads' CPU still counts as a sign of life against PROGRESS_GRACE_S.
    with ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="bndidx") as pool:
        futures = {pool.submit(_one, n, p, d): n for n, p, d in todo}
        for fut in as_completed(futures):
            _n = futures[fut]
            out["regions"][_n or "mesh"] = fut.result()


def _boundary_quality(polymesh: Path, *, region: str, case: Path | None = None) -> dict:
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
    from meshpipeline.render.boundary_quality import (
        FIELD_FILES,
        write_quality,
        write_quality_decomposed,
    )

    t0 = time.monotonic()
    try:
        # `case` IS THE DECISION, not a second reading of it: `_index_regions` asked
        # `index_is_decomposed` once and passed the workspace when the answer was yes, so this
        # cannot disagree with the index it is numbered against.
        meta = (write_quality_decomposed(case, region=region) if case is not None
                else write_quality(polymesh, region=region))
    except Exception as exc:              # noqa: BLE001
        logger.warning("conjugate: no per-face quality for %s (non-fatal)", region, exc_info=True)
        return {"ok": False, "why": str(exc), "seconds": round(time.monotonic() - t0, 1)}

    # WHERE THE RAW FIELDS ARE depends on which mesh this was: beside the region's polyMesh on a
    # merged delivery, and ONE COPY PER RANK on a decomposed one. They are the big thing here -
    # 4.25 GB across both regions of the customer plate, reduced to 319 MB - and on a decomposed
    # run they are 4.25 GB spread over the ranks instead of gathered, which is the same bytes in
    # more places and exactly as worth clearing.
    cleared = 0
    for base in _field_dirs(polymesh, case=case, region=region):
        for fname in FIELD_FILES.values():
            p = base / fname
            try:
                if p.is_file():
                    cleared += p.stat().st_size
                    p.unlink()
            except OSError:
                logger.warning("conjugate: could not clear %s after reducing it", p,
                               exc_info=True)
    return {"ok": True, "metrics": sorted(meta["basis"]), "basis": dict(meta["basis"]),
            "bytes": int(sum(meta["bytes"].values())), "fields_cleared_bytes": int(cleared),
            "notes": dict(meta.get("notes") or {}),
            "checked_against_checkMesh": dict(meta.get("checked_against_checkMesh") or {}),
            "ranks": int(meta.get("ranks") or 1),
            "seconds": round(time.monotonic() - t0, 1)}


def _field_dirs(polymesh: Path, *, case: Path | None, region: str) -> list[Path]:
    """Every directory checkMesh wrote this region's fields into, so none is left behind.

    ON FAILURE THEY ARE LEFT, which is why this is only ever called after a reduction that
    worked: they are then the evidence of what went wrong, and they are the only copy of it.
    """
    if case is None:
        return [Path(polymesh).parent]
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import processor_dirs

    return [(d / "constant" / region) if region else (d / "constant")
            for d in processor_dirs(Path(case))]


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


#: Where the pictures of the cuts are written, beside the mesh.
CUT_DIRNAME = "review_cuts"

#: WHAT READING THE EXPORTED INTERIOR COSTS A BYTE, so a cut that cannot be afforded out of what
#: is left of the run is not started rather than killed with the VM.
#:
#: MEASURED ON A .vtu, because the shape of the file matters and a first measurement on the wrong
#: shape was out by 3.3x. The .vtp row is a real boundary of job 0ff6f9e5; the .vtu row is a
#: 5,000,211-cell grid of 93,924,028 B, read SIX times, three of them with the page cache dropped
#: between reads (2026-11-17, Ubuntu 24.04, 15 GiB box):
#:
#:     a real .vtp of 16,582,198 faces   1,238,969,100 B   4.944 s   250,591,972 B/s   0.97 GB RSS
#:     the same .vtu, warm                 93,924,028 B    1.265 s    74,222,168 B/s   0.87 GB RSS
#:     the same .vtu, cold, slowest        93,924,028 B    1.440 s    65,217,992 B/s   0.87 GB RSS
#:
#: THE SLOWEST OF THE SIX IS THE ONE CHARGED, rounded down. This gate decides whether to START a
#: pass that cannot be abandoned, so reading fast is the direction that gets a pass killed with
#: the machine. The previous 76,000,000 was the fastest row of one warm read.
#:
#: AND A CLAIM THAT IT IS 1.8x OPTIMISTIC DOES NOT REPRODUCE HERE. An adversary measured
#: 43,017,027 B/s on a .vtu and called the gate 1.8x unsafe. Six reads of a .vtu on this box, warm
#: and cold, land between 65,217,992 and 74,222,168 B/s - a 1.14x band that does not reach it.
#: Neither measurement is on a mesh VM, so the honest statement is that the rate is machine-bound
#: and the slowest number anyone has measured is the one charged.
#:
#: CHARGING PER BYTE OVER-PRICES A REAL FILE, which is the safe direction. The .vtu measured above
#: is a uniform grid and compresses extremely well - 18.8 bytes a cell, against the plate's own
#: 113.5 (10,366,611,525 bytes over 91,323,244 fluid cells) - so a real file carries far more of
#: its content per byte read.
#:
#: AND THE READ IS STILL NOT MEASURED ON THE ARTIFACT ITSELF. The plate's internal.vtu was pruned
#: off the workspace by return_scope before anyone read it. Extrapolated per cell from the rows
#: above, 0.28 s and 0.15 GB of RSS per million cells puts that file at roughly 26 s and 14 GB;
#: extrapolated per byte it is 159 s and far more. Both fit the mesh VM's 512 GiB and NEITHER fits
#: the 16 GiB worker, which is the whole reason the pictures travel and the interior does not.
#:
#: WHAT THE PEAK RSS SAYS ABOUT THE HOME SIDE'S CEILING, measured in the same six reads: peak RSS
#: is 9.48x to 9.50x the file's bytes for this .vtu shape, against 0.85x for the 1.24 GB .vtp. The
#: shape that travels is the .vtu one, so `sandbox/render_adapter.VOLUME_READ_CEILING_BYTES` of
#: 4 GiB is about 38 GB of RSS in a 16 GiB worker. That constant is not this file's to change.
VOLUME_READ_BYTES_PER_S = 65_000_000

#: WHAT A CUT COSTS, CHARGED PER PLANNED RENDER RATHER THAN PER MILLION CELLS - because the
#: drawing is not per cell and there is a great deal more of it than one per-Mcell rate can carry.
#:
#: THE RATE THAT WAS HERE PRICED THE WRONG WORK. `CUT_SECONDS_PER_MCELL = 0.22` was fitted on
#: "slice it, then draw it twice" and that is not what the code does: `_place_by_what_is_drawn`
#: takes up to POSITION_TRIES slices and POSITION_TRIES x (WINDOW_TRIES + 1) = 20 zoom renders
#: per cut, measuring every one of them, before the whole-part frame is drawn at all.
#:
#: MEASURED 2026-11-17 on this box, through this file's own `_slice_at`, `_render_frame` and
#: `_measure_frame`, on a SYNTHETIC 171^3 = 5,000,211-cell unstructured grid (the real plate's
#: 10.4 GB interior was pruned off its workspace, so no interior of the part exists to time).
#: Worst of three runs each:
#:
#:     slice one plane                 0.3753 s      = 0.0750 s per Mcell
#:     clip a window out of the slice  0.0438 s
#:     draw a zoom frame               0.1997 s
#:     measure a drawn frame back      0.0250 s
#:     draw the WHOLE cut              0.7747 s      (it draws every cell of the plane)
#:
#:     slice + 2 draws, the behaviour the old comment described   1.350 s   0.2699 s/Mcell
#:     5 slices + 20 placement renders + 1 whole, what the code can take   8.047 s
#:     priced at CUT_SECONDS_PER_MCELL 0.22                       1.100 s
#:
#: So the old rate under-priced its own stated behaviour by 1.23x and the behaviour the code
#: actually has by 7.3x. A figure of 3.89 s per cut was reported for this; it does not reproduce
#: here in either direction - the minimal path is 1.35 s and the worst case 8.05 s.
#:
#: THE GATE DECIDES WHETHER TO START A PASS IT CANNOT ABANDON, so it charges the WORST case and
#: the slowest of the measured runs, rounded up. A pass killed with the machine leaves no picture
#: and costs the rest of the run.
CUT_SLICE_SECONDS_PER_MCELL = 0.08
#: One placement render: clip the window, draw it, measure the PNG back. Measured 0.2685 s.
CUT_ZOOM_RENDER_SECONDS = 0.28
#: The whole-part frame: it draws every cell of the plane rather than a window of it, which is why
#: it is three times a zoom. Measured 0.7997 s with its measure.
CUT_WHOLE_RENDER_SECONDS = 0.80


def cuts_priced_seconds(*, volume_bytes: int, cells: int, cuts: int) -> float:
    """The WORST this pass can cost: read the interior, then every render it may plan.

    Named and called rather than inlined so the gate and a test read the same arithmetic. The
    per-cut figure is POSITION_TRIES slices, POSITION_TRIES x (WINDOW_TRIES + 1) placement
    renders and one whole-part frame; the constants above carry the measurement.

    IT OVER-CHARGES ON PURPOSE, twice. Most cuts stop at the first placement that holds the array
    - on job 0ff6f9e5's own sweep the plan's place held nothing and the first searched position
    did, so the search cost two slices of a possible five - and the caller passes BOTH regions'
    cells while only the coolant's volume is ever read. Both are the safe direction for a gate
    deciding whether to begin something that cannot be stopped.
    """
    mcell = max(0, int(cells)) / 1e6
    per_cut = (POSITION_TRIES * CUT_SLICE_SECONDS_PER_MCELL * mcell
               + POSITION_TRIES * (WINDOW_TRIES + 1) * CUT_ZOOM_RENDER_SECONDS
               + CUT_WHOLE_RENDER_SECONDS)
    return max(0, int(volume_bytes)) / VOLUME_READ_BYTES_PER_S + max(1, int(cuts)) * per_cut

#: The cut the other two are AIMED FROM, and why it is this one.
#:
#: `through_the_plate` is normal to the part's thinnest axis, which is the axis a cold plate's
#: channels are prismatic along, so this plane crosses every passage wherever in the fine zone it
#: is placed. The other two are normal to in-plane axes, and exactly one of those axes is the one
#: the passages repeat along - a plane normal to THAT one runs along the passages and can never
#: show the array, at any window and any origin. Nothing in the plan says which of the two it is:
#: on job 0ff6f9e5 the fine zone is 60.2 x 12.0 x 52.1 mm, the passages repeat along x and run
#: along z, and neither span, nor the stretch factor, nor the feature thickness picks x out.
#:
#: So it is MEASURED instead: this cut is drawn first, `slices.ink_structure` reads the repeat
#: direction out of the picture, and the other two are re-placed from it. The cost of guessing
#: instead was `across_the_flow` placed along the passages on every run of this part.
AIM_FROM_CUT = "through_the_plate"

#: WHAT ONE BYTE OF A .vtu COSTS IN PEAK RSS when it is read, which is what decides whether the
#: interior can go home at all. MEASURED six times, 2026-11-17, on a 93,924,028 B .vtu of
#: 5,000,211 cells through `sandbox/backend`'s own pyvista: peak RSS 0.869 to 0.871 GB, so 9.48x
#: to 9.50x the file. The same read of a 1,238,969,100 B .vtp is 0.85x - a .vtp is already close
#: to its in-memory form and a compressed .vtu is not, and internal.vtu is the .vtu shape.
VOLUME_READ_RSS_RATIO = 9.5

#: ABOVE THIS THE INTERIOR DOES NOT GO HOME, because nothing at home could open it.
#:
#: The worker that reads it is capped at 16 GiB and `sandbox/backend._load_volume` reads the file
#: whole. At the ratio above, 1.2 GB of .vtu is about 11.4 GiB of peak RSS, which is inside the
#: roughly 12 GiB of headroom a 16 GiB worker has. 2 GB would be 19 GiB and an OOM kill of the
#: Celery worker, which costs the whole job and not just the picture.
#:
#: THE HOME SIDE HAS ITS OWN COPY OF THIS DECISION and it is four times larger:
#: `sandbox/render_adapter.VOLUME_READ_CEILING_BYTES` is 4 GiB, reasoned from a 1:1 read, which
#: at the measured ratio is about 38 GiB of RSS. That constant is not this file's to change and
#: the number above is the one this side acts on; they should be the same number.
VOLUME_KEEP_CEILING_BYTES = 1_200_000_000


def _expected_pitch_m(cuts: list[dict]) -> float:
    """The part's own feature pitch, from the window the cuts were already sized with.

    THIS IS THE BOUND ON THE YARDSTICK and it costs nothing to ask for: the number is already in
    every zoom frame, as `feature_m`, because the window was computed from it.

    `feature_m` is `plan.fine_cell_pinned_m` - the part's finest meshed cell - and
    `slices.CELL_PIXELS` records the identity that makes it the pitch: on job 0ff6f9e5
    `fine_surfaces[0].thickness_m` is 0.0001006312 m and `fine_cell_pinned_m` is 2.51578e-05 m,
    and the first divided by the second is 4.0 EXACTLY, "because the planner pins the cell AT the
    thickness over its four-cell floor". CELL_PIXELS is that same 4. So `feature_m * CELL_PIXELS`
    is the plan's feature pitch, and on the real plate it is 100.631 um against a passage period
    measured off that run's y-normal zoom at 100.631 um - the same number to six figures.

    ONE SOURCE, DELIBERATELY. `plan.fine_surfaces[0].thickness_m` is the other expression of the
    same quantity and taking it as well would give one number two readers that can disagree,
    which is a defect this engine has already paid for once (5% in a criterion against 19.89% in
    a gate). The cut is the source because the cut is what gets measured.

    AND IT IS A BOUND, NOT A SUBSTITUTE: `period_matches` is a ratio test at PERIOD_MATCH = 2.5,
    so a drawn pitch within a factor of two and a half of this stands. 4x and 8x do not. 0.0 for a
    cut set with no zoom frame, which is a plan that carries no fine cell - and then there is
    honestly no yardstick and the caller says so.
    """
    from meshpipeline.engines.cfmesh_conjugate.slices import CELL_PIXELS

    for cut in cuts or ():
        for frame in cut.get("frames") or ():
            if frame.get("scope") != "zoom":
                continue
            feature = float(frame.get("feature_m") or 0.0)
            pixels = int(frame.get("feature_pixels") or CELL_PIXELS)
            if feature > 0.0 and pixels > 0:
                return feature * pixels
    return 0.0


def _render_cuts(ws: Path, cuts: list[dict], *, volume: str | None, cells: int = 0,
                 budget_left_s: float | None = None, replan=None, ceiling: int = 0) -> dict:
    """Draw each cut on THIS machine, where the mesh already is, and leave the pictures behind.

    WHY THE PICTURES TRAVEL AND THE INTERIOR DOES NOT. `mesh_paths.volume` is the fluid interior,
    and the review has always been meant to cut it at home. On this plate that file is
    10,366,611,525 bytes and the worker that would open it is capped at 16 GiB, with
    `sandbox/backend._load_volume` reading it whole: a correct archive ceiling would deliver a
    10.4 GB file into a 16 GiB process and turn an honest "evidence missing" into an OOM kill of
    the Celery worker. The mesh VM has 512 GiB and the mesh is already on its disk, so the cut is
    made here and six PNGs go home instead of ten gigabytes.

    TWO FRAMES A CUT, AND THE ZOOM IS THE POINT. A cut has only ever been drawn once, fitted to
    the whole part. Measured against job 0ff6f9e5's own review bbox - 177.69 x 41.83 x 101.21 mm
    into 1280x960 - that is 105.42 um/px across the flow and 138.82 um/px on the other two, while
    the passages are 98.2422 um wide: the channel was UNDER ONE PIXEL on all three cuts. The
    reviewer was being handed three pictures of nothing and asked how many cells were across a
    channel, and `slices.review_slices` says in its own text that this is what the cuts are for.
    `slices.CELL_PIXELS` carries the measurement that fixes the zoom window.

    EVERY FRAME IS PUBLISHED WITH THE WINDOW IT WAS DRAWN IN and the um/px that came out of it,
    because a claim about a cut that cannot be checked against the scale of the picture it was
    made from is a guess with a picture next to it. A cut whose window is not stated is not
    evidence.

    ...AND WITH WHAT ITS OWN PIXELS WERE MEASURED TO HOLD, which is the only thing here that is
    not arithmetic about the plan. See `slices.CELL_PIXELS` and `slices.ink_structure`: the window
    is `plan_cell / 4` and the reader divides by the same plan cell, so a zoom frame declares
    itself countable whatever is drawn in it. Two of this plate's three zoom frames held a plenum
    rectangle and a port bore and were handed back as countable evidence of a channel count.

    AIMED FROM A PICTURE, NOT FROM THE PLAN. `AIM_FROM_CUT` is drawn first, the axis the passages
    repeat along is read out of it, and `replan(repeat_axis=...)` rebuilds the set so the other
    two planes cross the array instead of running along it. `replan` is optional: without it the
    planned set is drawn as given and whatever it holds is measured and said.

    ...BUT THE PICTURE IT IS AIMED FROM IS HELD TO THE PART'S OWN PITCH, which is what stops the
    yardstick certifying itself. `_expected_pitch_m` reads that pitch out of the cut set the
    caller already sized, and it goes into the AIMING cut as `expect_period_m` so that cut's
    placements are measured against it exactly as every later cut is measured against the
    yardstick.

    WITHOUT IT THE AIMING ZOOM WAS THE ONLY FRAME IN A RUN HELD TO NO PERIOD AT ALL, and three
    things had to combine for that: `_measure_frame` applies `period_matches` only when
    `expect_period_m` is already non-zero; `_rematch` skips the frame the yardstick came from;
    and `_place_by_what_is_drawn` stops at the FIRST placement that looks periodic. This plate
    demonstrably carries arrays at 1x, 2x, 4x and 8x the passage pitch - round 3's own cuts.json
    records `across_the_flow`'s zoom at 128.0 px and 6.28945 um/px, which is 805.05 um, with 39
    repeats and an autocorrelation peak of 0.582, and is a bank of coarse fins - so whichever of
    those a search reached first became the yardstick for the whole run, and PERIOD_MATCH's factor
    of 2.5 then matched every other cut to it with 2.5x of slack.

    MEASURED ON THE SHIPPED CODE, 2026-11-17, on a synthetic array drawn at exactly 8x the pitch
    its own cut set was sized for: `repeat_period_m` came back 201.262 um against a 25.1578 um
    cell, a ratio of 8.000 to the 100.631 um pitch, with the aiming zoom reporting
    `shows_structure: true` and `part_period_m: None` - held to nothing.

    AND WHEN NO PLACEMENT MATCHES there is no measured yardstick. Rather than fall through to
    `expect_period_m` 0.0, where `period_matches` returns True for everything and any resolved
    array stands, the part's own pitch is carried as the yardstick for the remaining cuts, and
    `out["yardstick_m"]` and `out["yardstick_source"]` say which of the two is in use.

    NON-FATAL, like every other aid here: a mesh is never lost over a picture of it.

    THE INTERIOR IS DROPPED ONCE THE PICTURES EXIST (see `_drop_cut_volume`), and not one moment
    earlier: it is dead weight from the instant the cuts are drawn, and the only artifact the
    review can reach a verdict from until then.
    """
    out: dict = {"dir": CUT_DIRNAME, "rendered": 0, "usable": 0, "why": ""}
    if not cuts:
        out["why"] = "the engine named no cuts to draw"
        return out
    src = None
    if volume:
        p = Path(volume)
        p = p if p.is_absolute() else ws / p
        if p.is_file():
            src = p
    if src is None:
        # SAID AS WHAT IT IS. With no interior there is nothing to cut, and a cut promised from
        # nothing is the defect this whole aid exists to stop.
        out["why"] = ("no volume export is on disk, so there is nothing to cut: the frames are "
                      "published with their windows and no pictures")
        logger.warning("conjugate: %s", out["why"])
        return out

    size = src.stat().st_size
    priced = cuts_priced_seconds(volume_bytes=size, cells=cells, cuts=len(cuts))
    out["priced_seconds"] = round(priced, 1)
    out["priced_renders"] = len(cuts) * (POSITION_TRIES * (WINDOW_TRIES + 1) + 1)
    out["volume_bytes_read"] = int(size)
    if budget_left_s is not None and priced > float(budget_left_s):
        out["why"] = (
            f"the cuts are priced at about {priced:.0f} s against {float(budget_left_s):.0f} s "
            "left of the run's budget, so they were not started: a pass killed with the machine "
            "leaves no picture and costs the rest of the run")
        logger.warning("conjugate: %s", out["why"])
        return out

    t0 = time.monotonic()
    mesh = None
    try:
        # THE HOME SIDE'S OWN RENDER SETTINGS, imported rather than restated. `sandbox/backend`
        # pins VTK to Mesa's software rasteriser with no X server, and those four environment
        # lines have to run BEFORE pyvista is first imported or they do nothing. Copying them onto
        # this side of the VM boundary is how the two drift, and a cut drawn under a different
        # backend is a cut whose published scale is no longer the scale the reviewer's frames are
        # in.
        #
        # TAKEN THROUGH backend's OWN pyvista rather than imported beside it, because the order is
        # the whole point and an import sorter will not preserve it: `import pyvista` sorts into
        # the third-party block and would end up ABOVE this first-party line, setting the backend
        # after VTK had already chosen one. Reaching through backend makes the dependency the
        # thing that enforces it.
        from meshpipeline.engines.cfmesh_conjugate.slices import FRAME_H, FRAME_W
        from meshpipeline.sandbox.backend import pv

        mesh = pv.read(str(src))
        out["read_seconds"] = round(time.monotonic() - t0, 1)
        out["cells_read"] = int(mesh.n_cells)
        # THE BOX THE CUTS ARE PLACED IN, taken from the volume ITSELF. The boundary index would
        # be the other source and it is NOT READABLE HERE: `_boundary_indexes` runs about sixteen
        # lines after this call, so constant/*/polyMesh/boundaryIndex/meta.json does not exist yet
        # and reading it would give an empty box on every run and a correct one on none.
        out["bbox_source"] = "the exported volume's own bounds"
        out["bbox"] = [float(v) for v in mesh.bounds]
        root = ws / CUT_DIRNAME
        root.mkdir(parents=True, exist_ok=True)
        done: set[int] = set()
        expected = _expected_pitch_m(cuts)
        if expected > 0.0:
            out["expected_pitch_m"] = expected
        if replan is not None:
            done = _aim_from_a_drawn_cut(mesh, cuts, root=root, ws=ws, pv=pv,
                                         width_px=FRAME_W, height_px=FRAME_H, out=out,
                                         replan=replan, expect_period_m=expected)
        # WHICH YARDSTICK THE REST OF THE RUN IS HELD TO, decided here and said. The measured
        # period wins when a frame produced one; the part's own pitch stands in when none did,
        # because the alternative is 0.0 and `period_matches` returns True for everything at 0.0 -
        # so a run whose aim failed used to hold every later cut to no period at all, which is the
        # state that let an 805.05 um bank of fins answer a question about a 100.631 um passage.
        measured_period = float(out.get("repeat_period_m") or 0.0)
        yardstick = measured_period or expected
        out["yardstick_m"] = yardstick
        out["yardstick_source"] = (
            "measured off the aiming cut's own zoom frame" if measured_period
            else "the part's own feature pitch, from the window the cuts were sized with "
                 "(feature_m x CELL_PIXELS); no drawn frame produced a period" if expected
            else "none: no frame produced a period and the cuts carry no feature size, so a "
                 "resolved array of any pitch stands")
        for cut in cuts:
            if id(cut) in done:
                continue
            _draw_one_cut(mesh, cut, root=root, ws=ws, pv=pv,
                          width_px=FRAME_W, height_px=FRAME_H, out=out,
                          expect_period_m=yardstick)
    except Exception as exc:              # noqa: BLE001
        logger.warning("conjugate: the review cuts could not be drawn (non-fatal)", exc_info=True)
        out["why"] = f"the cuts could not be drawn: {exc}"
    out["seconds"] = round(time.monotonic() - t0, 1)
    # ONLY A PICTURE THAT CAN ANSWER THE QUESTION BUYS THE INTERIOR'S DELETION. This was
    # `out["rendered"]`, which counts whole frames as well as zooms and now counts every drawn
    # frame whatever its pixels hold - so a run whose three zoom frames were all refused and
    # whose three whole frames drew would still have deleted the 10.4 GB interior, leaving a
    # review with no artifact it could reach a verdict from at all.
    if out.get("usable"):
        # MESH AND CUTS GO IN, so the drop can write a slab instead of nothing. `mesh` is the
        # interior already in memory and `cuts` carry the frames that say which planes went
        # unanswered; re-reading a 2.15 GiB .vtu to find that out would cost more than the slab.
        _drop_cut_volume(src, out, ceiling=ceiling, mesh=mesh, cuts=cuts)
    elif out["rendered"]:
        out["volume_kept"] = (
            f"{out['rendered']} frame(s) drew and none of them holds the periodic structure its "
            "cut exists to show, so the fluid interior is kept: it is then the only artifact a "
            "review could reach a verdict from, and a picture that cannot answer the question "
            "does not buy its deletion")
        logger.warning("conjugate: %s", out["volume_kept"])
    return out


def _aim_from_a_drawn_cut(mesh, cuts: list[dict], *, root: Path, ws: Path, pv, width_px: int,
                          height_px: int, out: dict, replan,
                          expect_period_m: float = 0.0) -> set[int]:
    """Draw AIM_FROM_CUT, read the repeat direction out of the picture, and re-place the rest.

    See AIM_FROM_CUT for why the axis cannot be taken from the plan. Returns the ids of the cuts
    that are already drawn, so the caller does not draw them twice: the aiming cut is normal to
    the thin axis and the repeat axis never changes that, so its own pictures stand.

    A FAILURE HERE CHANGES NOTHING. If the aiming cut shows no array, the planned set stands and
    every frame is still measured and marked. The one thing that must not happen is the opposite:
    a set re-aimed from a frame that held no structure would be aimed from noise.

    `expect_period_m` IS WHAT BOUNDS THE YARDSTICK: the part's own feature pitch
    (`_expected_pitch_m`), handed to this cut so its placements are held to a period exactly as
    every later cut is held to the period this one produces. See `_render_cuts` for the three
    things that together left this one frame held to no period at all, and for the 805.05 um array
    on this plate that would otherwise become the yardstick if a search reached it first.
    """
    aim = next((c for c in cuts if c.get("name") == AIM_FROM_CUT), None)
    if aim is None:
        return set()
    _draw_one_cut(mesh, aim, root=root, ws=ws, pv=pv, width_px=width_px, height_px=height_px,
                  out=out, expect_period_m=max(0.0, float(expect_period_m or 0.0)))
    found = next((f.get("content") for f in aim.get("frames") or []
                  if f.get("scope") == "zoom" and isinstance(f.get("content"), dict)), None)
    axis = (found or {}).get("repeat_world_axis")
    if not (found and found.get("shows_structure") and axis is not None):
        out["aim"] = (f"{AIM_FROM_CUT} showed no repeating array, so the other cuts keep the "
                      "placement the plan gave them")
        return {id(aim)}
    rebuilt = replan(int(axis))
    # THE AIMING CUT MUST COME BACK UNCHANGED or its drawn frames are of a different plane. It is
    # normal to the thin axis and `repeat_axis` only decides which of the two IN-PLANE axes the
    # flow runs along, so this holds; it is checked rather than assumed because the cost of being
    # wrong is a picture published against the wrong plane's window.
    same = next((c for c in rebuilt or ()
                 if c.get("name") == AIM_FROM_CUT
                 and c.get("normal_axis") == aim.get("normal_axis")), None)
    if not rebuilt or same is None:
        return {id(aim)}
    cuts[:] = [aim if c.get("name") == AIM_FROM_CUT else c for c in rebuilt]
    out["repeat_world_axis"] = int(axis)
    out["repeat_period_m"] = found.get("period_m")
    # THE AIMING CUT'S OTHER FRAMES WERE MEASURED BEFORE THERE WAS A YARDSTICK, so they are
    # measured again against it now - off the PNG already on disk, with nothing re-drawn. Without
    # this the context frame of the one cut that found the array is the only frame in the run that
    # can say "shows_structure" of a moire of it: measured 11 repeats at 1,664 um on a frame whose
    # scale is 138.67 um/px, against passages of 100.63 um.
    # AND THE ONE COORDINATE IT MEASURED IS GIVEN TO THE CUTS THAT ARE MISSING IT.
    #
    # WHAT WAS WRONG. `replan` takes an axis index and nothing else, so the only thing that
    # crossed from the drawn picture into the rebuilt cut set was the repeat axis. Every other
    # cut came back with a PLAN centre - and `slices.py:303` builds that as the middle of the
    # fine zone. On this plate the middle of the fine zone is empty: the engine measured it
    # itself (see the table above POSITION_TRIES) and found 79 repeats at y = 22.111 and
    # 22.713 mm and nothing at all at 23.917 mm. Reproduced from the plan on disk: the zone is
    # y = 21.5095 to 33.5468 mm, so its middle is 27.5282 mm, the zoom window is 6.0379 mm tall,
    # and its lower edge lands at 24.5092 mm - 1.7960 mm ABOVE the array. No in-plane search
    # could reach it, because the only in-plane search is `_candidate_centres`, which aims at the
    # densest histogram patch, and on this part the densest thing on the plane is a plenum.
    #
    # WHY THE NUMBER TRANSFERS WITHOUT CONVERSION. The aiming cut is normal to the THIN axis and
    # `_place_by_what_is_drawn` wrote the plane it found into `aim["origin"][thin]`. The flow
    # axis is chosen from the axes that are not thin (slices.py:311), so thin is an IN-PLANE axis
    # of every other cut on this part - and a coordinate along it is exactly the in-plane centre
    # component those zoom frames were missing.
    #
    # IT COSTS NOTHING AND CHANGES NO BAR. `_candidate_centres` returns the planned centre FIRST,
    # before any density pick, so a centre written here is honoured ahead of density with no new
    # search; when it holds the array the search stops at the first placement instead of drawing
    # all twenty. The whole-part context frame is deliberately left alone - it is centred on the
    # BLOCK's middle so the rest of the part stays in view - and `search_m` is untouched, so a
    # plane still cannot wander outside its own zone.
    _aim_axis = aim.get("normal_axis")
    if _aim_axis is None:
        _nrm = [float(v) for v in (aim.get("normal") or [1.0, 0.0, 0.0])]
        _aim_axis = max(range(3), key=lambda k: abs(_nrm[k])) if len(_nrm) == 3 else 0
    _aim_axis = int(_aim_axis)
    _at = float((aim.get("origin") or [0.0, 0.0, 0.0])[_aim_axis])
    for _c in cuts:
        if _c is aim:
            continue
        _pa = [int(v) for v in (_c.get("plane_axes") or [])]
        if len(_pa) != 2 or _aim_axis not in _pa:
            continue
        _slot = 0 if _aim_axis == _pa[0] else 1
        for _f in _c.get("frames") or ():
            if _f.get("scope") != "zoom":
                continue
            _centre = [float(v) for v in (_f.get("centre_m") or ())]
            if len(_centre) != 2:
                continue
            _centre[_slot] = _at
            _f["centre_m"] = _centre
            _f["centre_source"] = (
                f"{AIM_FROM_CUT}'s own drawn frame measured the array at "
                f"{'xyz'[_aim_axis]} = {_at * 1e3:.3f} mm, so this window is centred there and "
                "not on the middle of the fine zone, which on this part holds nothing")
    _rematch(aim, root=root, expect_period_m=float(found.get("period_m") or 0.0))
    out["aim"] = (
        f"the passages were measured to repeat along {'xyz'[int(axis)]} every "
        f"{float(found.get('period_m') or 0.0) * 1e6:.2f} um in {AIM_FROM_CUT}'s own zoom frame, "
        "so the other cuts were re-placed to cross them rather than run along them")
    logger.info("conjugate: %s", out["aim"])
    return {id(aim)}


def _rematch(cut: dict, *, root: Path, expect_period_m: float) -> None:
    """Re-read the frames of `cut` that were drawn before the part's period was known.

    THE ZOOM IS SKIPPED BECAUSE THE YARDSTICK CAME FROM IT, and that is only safe because the
    aiming zoom is now held to a period of its own while it is being placed: `_render_cuts` passes
    the PLAN's feature pitch into the aiming cut as `expect_period_m`, so a placement that drew
    the 402.525 um or 805.050 um array was refused before it could become the yardstick. Without
    that this function left exactly one frame in the run - the one the yardstick came from - held
    to no period at all, and `_measure_frame` applies `period_matches` only when
    `expect_period_m` is non-zero.
    """
    ah, av = cut.get("plane_axes") or (0, 1)
    for frame in cut.get("frames") or []:
        if frame.get("scope") == "zoom":
            continue                      # the yardstick came from this one; see the docstring
        name = frame.get("image")
        mpp = float(frame.get("metres_per_pixel") or 0.0)
        if not name or mpp <= 0.0:
            continue
        path = root / Path(str(name)).name
        if not path.is_file():
            continue
        try:
            _measure_frame(path, frame, ah=ah, av=av, mpp=mpp,
                           expect_period_m=expect_period_m)
        except Exception:                 # noqa: BLE001
            logger.warning("conjugate: %s could not be measured again (non-fatal)", path.name,
                           exc_info=True)


def _drop_cut_volume(src: Path, out: dict, *, ceiling: int = 0,
                     mesh=None, cuts: list[dict] | None = None) -> None:
    """Give back the fluid interior, once the pictures of it exist AND it is too big to be read.

    IT IS DEAD WEIGHT THE MOMENT THE CUTS ARE DRAWN, at this plate's size. The interior is
    10,366,611,525 bytes on job 0ff6f9e5 and the only thing at home that would open it is a worker
    capped at 16 GiB reading the file whole - so it cannot be cut there, and the six PNGs that
    replace it are 43,228 bytes. Carrying it home buys a read nothing can afford and costs the
    archive ten gigabytes.

    AND IT IS WHAT STOPS THE PRUNE MAKING THINGS WORSE. `return_scope` works out what giving up
    every optional file would achieve and declines when even that cannot fit, which is right - but
    on this job the pre-prune estimate is 14,761,120,873 bytes and giving up the interior still
    leaves 11,406,088,609, so it DECLINES, keeps the file, and the archive grows from 11.4 GB to
    14.76 GB. With the interior gone there is nothing optional left to decline about.

    A SMALL INTERIOR IS NOT DEAD WEIGHT AND IS KEPT. A volume the home side can open is a
    RE-AIMABLE view of the mesh, which is better evidence than any fixed frame: the reviewer picks
    where to look instead of being handed six places. `VOLUME_KEEP_CEILING_BYTES` is where that
    stops being affordable.

    ONLY AFTER A PICTURE THAT CAN ANSWER THE QUESTION EXISTS, and that order is the whole safety
    of this. The caller reaches here on `out["usable"] > 0` - zoom frames whose OWN PIXELS were
    measured to hold the periodic structure the cut exists to show - and not on `rendered`.
    `rendered` counts whole frames as well as zooms, and every drawn frame is now published
    whatever its pixels hold, so a run where all three zoom frames were refused and all three
    whole frames drew would have had `rendered == 6` and deleted the interior. That leaves a
    review with nothing at all: six pictures that cannot be counted in, and no interior to re-cut.
    A run whose cuts failed or was never started keeps the interior exactly as it found it, for
    the same reason. Non-fatal: a file that will not unlink costs disk, never the mesh.

    THE TENSION IN KEEPING IT IS REAL AND IS NOT A REASON TO DELETE IT. Above
    VOLUME_KEEP_CEILING_BYTES the home side cannot open the interior either, and `return_scope`
    will decline to prune it and grow the archive. So keeping a 10.4 GB interior that no frame
    could replace buys a re-cut somewhere the file CAN be read - a mesh VM, which is where it
    already is - and not a review at home. That is more than deleting it leaves, which is nothing.
    """
    cap = int(ceiling or VOLUME_KEEP_CEILING_BYTES)
    try:
        if not src.is_file():
            return
        freed = src.stat().st_size
        if freed <= cap:
            out["volume_kept"] = (
                f"the fluid interior ({freed / 2**30:.2f} GiB) is inside the "
                f"{cap / 2**30:.2f} GiB a 16 GiB worker can read, so it travels home as well as "
                "the pictures: a volume the reviewer can re-aim beats six fixed frames")
            return
        # THE THIRD OPTION, AND IT IS THE ONE THIS CODE WAS MISSING. Above the ceiling the choice
        # here used to be between a file nothing at home can open and no interior at all, and it
        # took the second. On job 40e4e578 that is exactly what happened: `usable` went above zero
        # for the first time (the harmonic fix), this branch ran for the first time, 2.15 GiB went,
        # and the reviewer reported `region 'across_the_flow' was not produced` about a mesh that
        # was built, gated and delivered. A slab at the plane whose frame could not answer is the
        # SAME evidence at a size the home worker can read.
        if _reduce_to_review_slabs(src, out, cap=cap, mesh=mesh, cuts=cuts):
            return
        src.unlink()
    except OSError:
        logger.warning("conjugate: could not drop %s after the cuts were drawn", src,
                       exc_info=True)
        return
    out["volume_dropped_bytes"] = int(freed)
    out["volume_dropped"] = (
        f"the fluid interior ({freed / 2**30:.2f} GiB) was cut, drawn, and then dropped: the "
        f"{out.get('rendered', 0)} pictures of it are the evidence that travels, and reading the "
        f"file at home would take about {freed * VOLUME_READ_RSS_RATIO / 2**30:.0f} GiB of RSS in "
        "a worker capped at 16")
    logger.info("conjugate: %s", out["volume_dropped"])


#: HOW THICK A REVIEW SLAB IS, in planned cells either side of the plane. The region asks how
#: many cells lie along a row IN the plane, so the normal direction only has to hold enough cells
#: for that row to sit in a real volume rather than on a surface. Four is two either side.
REVIEW_SLAB_CELLS = 4

#: WHEN THE PLANNED CELL IS NOT ON THE FRAME, a slab still has to have a thickness, and a zero one
#: clips to nothing. The in-plane window over this is a cell-sized guess of last resort.
SLAB_FALLBACK_WINDOW_FRACTION = 200.0


def _slab_cell_m(zoom: dict, whole: dict) -> float:
    """The cell a slab's thickness is counted in, or 0.0 when there is nothing to count.

    The planned cell off the zoom, and when the producer did not state one, a cell-sized guess of
    last resort off the whole frame's window - see `SLAB_FALLBACK_WINDOW_FRACTION`. Shared by
    `_refused_planes` and `_all_declared_planes` so the two cannot disagree about how thick a slab
    at the same plane should be.
    """
    cell = float(zoom.get("feature_m") or 0.0)
    if cell > 0.0:
        return cell
    win = list(whole.get("window_m") or [])
    if len(win) != 2:
        return 0.0
    return min(float(win[0]), float(win[1])) / SLAB_FALLBACK_WINDOW_FRACTION


def _slab_box(cut: dict, frame: dict, *, thickness_m: float) -> list[float] | None:
    """One cut's plane as a box: the whole frame in-plane, a few cells thick along the normal."""
    normal = [float(v) for v in (cut.get("normal") or [])]
    origin = [float(v) for v in (cut.get("origin") or [])]
    win = list(frame.get("window_m") or [])
    centre = list(frame.get("centre_m") or [])
    if len(normal) != 3 or len(origin) != 3 or len(win) != 2 or len(centre) != 2:
        return None
    axis = max(range(3), key=lambda k: abs(normal[k]))
    pa = cut.get("plane_axes") or [k for k in range(3) if k != axis]
    ah, av = int(pa[0]), int(pa[1])
    half = max(float(thickness_m), 0.0) / 2.0
    if half <= 0.0:
        return None
    box = _window_box(centre, ah, av, float(win[0]), float(win[1]))
    box[2 * axis], box[2 * axis + 1] = origin[axis] - half, origin[axis] + half
    return box


def _zoom_discharges_its_region(zoom: dict) -> bool:
    """Whether this zoom is the evidence its region needs - the READER's bar, not a weaker one.

    `shows_structure` IS NOT THAT BAR, AND BELIEVING IT WAS LEFT THE OBLIGED PLANE WITH NO SLAB.
    What discharges a region is `review_session.ResolvedCut.counts_cells`, which is
    `cell_px >= COUNTABLE_CELL_PX and shows_cells` - and `shows_cells` ends at
    `wall_in_cells(ink.bare_run_min_px, repeat_pitch_px) >= WALL_CELLS_MIN`. A frame can show a
    perfectly regular structure and still have NO METAL in it, and a picture with no wall in it
    cannot be a picture of a wall.

    MEASURED on job 40e4e578, from its own mesh_manifest.json:

        frame                      shows_structure   bare_run_min_px   counts_cells
        across_the_flow   zoom     True              0.0               False
        through_the_plate zoom     True              30.0              True

    `across_the_flow`'s zoom resolves cells - 333 on its best row at 4.0 px - and the reader's own
    words for it are "the NARROWEST metal between two passages is 0.0 px - 0.00 cell(s) ... No
    column of this frame along that axis is bare, so there is no metal in it at all - this is a
    field of coolant, whatever its section does."

    WHAT THE TWO BARS COST TOGETHER. `expand_region_targets` offers a region only when the volume
    is available or one of its frames `counts_cells`, and the engine obliges
    `region:across_the_flow`. So the reader needed an interior at exactly the plane whose zoom
    `shows_structure` told this function to delete it at. Four jobs - 40e4e578, 606af55e,
    b8f09ae9, 110daca9 - delivered a mesh, a bundle and a viewer with
    `review_execution: failed_to_complete`, refused before the model was asked anything.

    ONE RULE, NOT A SECOND COPY OF IT. `slices.py` imports these from
    `sandbox/picture_structure` with the note that they moved there "verbatim, so the side that
    DRAWS a frame and the side" that reads it cannot drift apart. This function had drifted; it
    now asks the same question with the same constants.

    THE ASYMMETRY DECIDES THE DEFAULT. A slab that was not needed costs disk, bounded by `cap`.
    A slab that was needed and not built costs the verdict on a finished mesh. So anything this
    cannot show to be sufficient evidence is treated as refused.
    """
    from meshpipeline.sandbox.picture_structure import WALL_CELLS_MIN, wall_in_cells

    content = zoom.get("content")
    if not isinstance(content, dict) or not content.get("shows_structure"):
        return False
    # The drawn cell on the axis the structure repeats on. The producer sizes its zoom so one
    # planned cell is exactly `feature_pixels` across, which is COUNTABLE_CELL_PX by construction;
    # an absent one is silence, and silence is not evidence.
    cell_px = float(zoom.get("feature_pixels") or 0.0)
    wall = wall_in_cells(content.get("bare_run_min_px"), cell_px)
    return wall >= WALL_CELLS_MIN


def _all_declared_planes(cuts: list[dict] | None) -> list[tuple[dict, dict, float]]:
    """(cut, whole frame, planned cell) for EVERY cut that has a zoom to centre a slab on.

    WHY EVERY PLANE AND NOT ONLY THE REFUSED ONES. The obligation and the discharge are decided by
    two different authorities, and the weaker one builds the obligation. `spec._cuts_that_can_be_
    shown` names a region unless the PRODUCER explicitly vetoed it, and says so itself: "A cut it
    did NOT refuse may still be refused by the home side's own measurement of its own copy of the
    PNG; that stays where it is, and this does not try to predict it." The home side then applies
    five further floors - the ink floor, cells per row, row count, cell-pitch agreement and the
    metal between the passages - and `shows_passages` is explicit that "a present False still
    vetoes, a present True buys nothing". So a cut this engine did not refuse can still fail at
    home, and it is an obligation nothing in the session can then discharge.

    `volume_available` is the only thing that short-circuits that, and the interior was being
    deleted in exactly the configuration where the obligation names every cut: `_refused_planes`
    comes back empty when every zoom answered, so no slab was written and `review_volume` found
    nothing. This engine never waives the obligation either - `review_requires_the_interior` is
    True, which skips the waiver.

    A SLAB AT EVERY PLANE COSTS A FEW HUNDRED KiB AND REMOVES THE WHOLE CLASS. It is bounded by
    `cap` like everything else here, and `_reduce_to_review_slabs` still refuses and lets the
    caller unlink if the result will not fit. What it buys is that a dropped interior always
    leaves SOMETHING at every plane a region might be obliged at, whichever way the home side's
    own measurement goes.
    """
    want: list[tuple[dict, dict, float]] = []
    for cut in cuts or ():
        frames = list(cut.get("frames") or ())
        zoom = next((f for f in frames if f.get("scope") == "zoom"), None)
        if zoom is None:
            continue
        whole = next((f for f in frames if f.get("scope") == "whole"), zoom)
        cell = _slab_cell_m(zoom, whole)
        if cell > 0.0:
            want.append((cut, whole, cell))
    return want


def _refused_planes(cuts: list[dict] | None) -> list[tuple[dict, dict, float]]:
    """(cut, whole frame, planned cell) for every cut whose zoom frame cannot answer its question.

    A ZOOM THAT DISCHARGES ITS REGION NEEDS NO INTERIOR - see `_zoom_discharges_its_region` for
    which bar that is and why `shows_structure` alone was the wrong one. This asks, per cut,
    whether the picture of THAT cut answered what the cut exists for. A region whose picture did
    is evidence already and buys the deletion honestly.
    """
    want: list[tuple[dict, dict, float]] = []
    for cut in cuts or ():
        frames = list(cut.get("frames") or ())
        zoom = next((f for f in frames if f.get("scope") == "zoom"), None)
        if zoom is None:
            continue
        if _zoom_discharges_its_region(zoom):
            continue
        whole = next((f for f in frames if f.get("scope") == "whole"), zoom)
        cell = _slab_cell_m(zoom, whole)
        if cell > 0.0:
            want.append((cut, whole, cell))
    return want


def _reduce_to_review_slabs(src: Path, out: dict, *, cap: int,
                            mesh=None, cuts: list[dict] | None = None) -> bool:
    """Replace an unopenable interior with a slab at each unanswered plane. True when one fit.

    NON-FATAL IN EVERY DIRECTION, like the rest of this aid: False on anything at all, and the
    caller then does what it did before, which is to unlink. A review aid may never cost a mesh.
    """
    # THE REFUSED PLANES FIRST, because those are the ones a reader provably cannot answer from
    # its picture and so the ones a slab is most needed at. Then every other declared plane, for
    # the reason in `_all_declared_planes`: the obligation is built by a weaker test than the one
    # that discharges it, so a plane this engine thinks is fine can still need an interior.
    _refused = _refused_planes(cuts)
    _named = {id(c) for c, _w, _cell in _refused}
    planes = _refused + [t for t in _all_declared_planes(cuts) if id(t[0]) not in _named]
    if mesh is None or not planes:
        return False
    # THE SUFFIX HAS TO SURVIVE, because pyvista picks its writer off the extension and
    # refuses ".slab" outright - which the non-fatal handler below then turns into a
    # silent fallback to deleting the interior. Keep the real suffix last.
    tmp = src.with_name(src.stem + ".slab" + src.suffix)
    try:
        merged = None
        kept: list[str] = []
        for cut, whole, cell in planes:
            box = _slab_box(cut, whole, thickness_m=cell * REVIEW_SLAB_CELLS)
            if box is None:
                continue
            # `crinkle=True` BECAUSE THE REVIEWER COUNTS CELLS. A plain clip cuts the
            # cells it crosses and tetrahedralises them: measured on a 64,000-cell grid,
            # a 1/20 slab came back with 64,000 cells and the same mesh crinkled came
            # back with 6,400. Handing a region whose question is "how many cells lie
            # across this passage" a volume of clip fragments would be answered
            # precisely and wrongly. Whole cells also store 9.6x smaller here.
            piece = mesh.clip_box(box, invert=False, crinkle=True)
            if not piece.n_cells:
                continue
            merged = piece if merged is None else merged.merge(piece)
            kept.append(str(cut.get("name") or "?"))
        if merged is None or not merged.n_cells:
            return False
        merged.save(str(tmp))
        size = int(tmp.stat().st_size)
        if size > cap:
            out["volume_slab_refused"] = (
                f"a slab at {', '.join(kept)} came to {size / 2**20:.0f} MiB, over the "
                f"{cap / 2**20:.0f} MiB a 16 GiB worker can read, so the interior went after all")
            logger.warning("conjugate: %s", out["volume_slab_refused"])
            return False
        cells = int(merged.n_cells)
        tmp.replace(src)
    except Exception:                   # noqa: BLE001 - a review aid may never cost a built mesh
        logger.warning("conjugate: the review slab could not be written; the interior is handled "
                       "as it was before", exc_info=True)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    # SAID AS WHAT IT IS, because the reviewer reasons about what it is handed. Calling a slab
    # "the volume" would let a verdict rest on a completeness the file does not have.
    out["volume_reduced_bytes"] = size
    out["volume_reduced_cells"] = cells
    out["volume_reduced"] = (
        f"the fluid interior was too big to open at home, so what travels is a slab "
        f"{REVIEW_SLAB_CELLS} cells thick at the plane(s) whose frames could not answer "
        f"({', '.join(kept)}): {cells:,} cells, {size / 2**20:.0f} MiB. It is a slab and not the "
        "whole interior - the reviewer can re-aim along the row, which is what those regions ask, "
        "and cannot inspect anywhere else in the part")
    logger.info("conjugate: %s", out["volume_reduced"])
    return True


def _window_box(centre, ah: int, av: int, win_h: float, win_v: float) -> list[float]:
    box = [-1e9, 1e9, -1e9, 1e9, -1e9, 1e9]
    box[2 * ah], box[2 * ah + 1] = centre[0] - win_h / 2, centre[0] + win_h / 2
    box[2 * av], box[2 * av + 1] = centre[1] - win_v / 2, centre[1] + win_v / 2
    return box


#: HOW MANY PLACES A CUT MAY BE TRIED IN before it is reported as showing nothing, as (planes
#: along the normal, windows inside each plane).
#:
#: WHY A SEARCH AT ALL. The plan's own fine zone is a BOX and the features are not spread through
#: it. MEASURED on job 0ff6f9e5, sweeping the y-normal cut across its fine zone (21.51 to 33.55 mm)
#: and measuring every frame drawn:
#:
#:     y = 22.111 mm   79 repeats, 16.0 px, spread 0.00, 18.7% ink    the channel array
#:     y = 22.713 mm   79 repeats, 16.0 px, spread 0.00, 18.7% ink    the channel array
#:     y = 23.917 mm    0 repeats,                        0.4% ink    nothing
#:     y = 25.121 mm   60 repeats,  8.0 px, spread 1.63,  0.6% ink    scattered traces
#:     y = 27.528 mm   23 repeats,  8.0 px, spread 4.30,  0.7% ink    scattered traces
#:
#: The middle of the fine zone is empty and the channels are in the bottom tenth of it. So the
#: zone says WHERE TO LOOK and the pictures say WHICH of those places has the thing in it. On
#: this plate the plan's own place holds nothing and the first searched position - a tenth of the
#: way up the band, y = 22.713 mm - holds the array, so the whole search cost two planes. It
#: stops at the first frame that shows one, so a part whose plan already points at its features
#: costs one.
#:
#: AND THE SEARCH NEVER LEAVES THE ZONE (`cut["search_m"]`). A plane free to wander the whole
#: block until something looked periodic would find the array in another part of the part and
#: report it under this cut's name.
POSITION_TRIES = 5
WINDOW_TRIES = 3


def _slice_at(mesh, axis: int, origin, cut: dict):
    """The cut plane at this origin, or None when it crosses nothing."""
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    try:
        sl = mesh.slice(normal=tuple(normal), origin=tuple(origin))
    except Exception as exc:              # noqa: BLE001
        cut["cut_why"] = f"this plane could not be cut: {exc}"
        return None
    return sl if sl.n_points else None


def _candidate_origins(cut: dict, axis: int, origin: list[float]) -> list[list[float]]:
    """Where the plane may sit: the planned place first, then across the search band.

    The planned place is tried FIRST and always, so a part whose plan already points at its
    features costs exactly one slice and the search never runs.
    """
    out = [list(origin)]
    band = cut.get("search_m")
    if not (isinstance(band, (list, tuple)) and len(band) == 2):
        return out
    lo, hi = float(band[0]), float(band[1])
    if not (hi > lo):
        return out
    for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
        at = lo + frac * (hi - lo)
        if all(abs(at - o[axis]) > 0.02 * (hi - lo) for o in out):
            o = list(origin)
            o[axis] = at
            out.append(o)
        if len(out) >= POSITION_TRIES:
            break
    return out


def _candidate_centres(sl, ah: int, av: int, win_h: float, win_v: float, planned):
    """Where to point the window: the plan's own place first, then the densest patches.

    THE FINE ZONE BEFORE DENSITY, which is the opposite of how this was first built. On this part
    the densest cells are port-rim slivers of 7.787 um against the channels' pinned 25.1578 um,
    and aiming at raw density is why `through_the_plate`'s zoom came back as one circular port
    bore and was published as a channel count.
    """
    import numpy as np

    out = [[float(planned[0]), float(planned[1])]]
    pts = np.asarray(sl.points)
    if pts.shape[0] < 2 or win_h <= 0.0 or win_v <= 0.0:
        return out
    h, v = pts[:, ah], pts[:, av]
    sh, sv = float(h.max() - h.min()), float(v.max() - v.min())
    nh = max(1, min(2048, int(sh / win_h) + 1))
    nv = max(1, min(2048, int(sv / win_v) + 1))
    hist, he, ve = np.histogram2d(h, v, bins=[nh, nv])
    for flat in np.argsort(hist, axis=None)[::-1][:WINDOW_TRIES]:
        i, j = np.unravel_index(int(flat), hist.shape)
        if hist[i, j] <= 0:
            break
        out.append([float((he[i] + he[i + 1]) / 2.0), float((ve[j] + ve[j + 1]) / 2.0)])
    return out[:WINDOW_TRIES + 1]


def _measure_frame(path: Path, frame: dict, *, ah: int, av: int, mpp: float,
                   expect_period_m: float = 0.0) -> dict:
    """Read the drawn PNG back and record what its own pixels hold. See slices.ink_structure.

    THE FIGURE IS PUBLISHED BESIDE THE WINDOW because the window alone cannot say anything about
    the content: the window is `plan_cell / 4` and the reader divides by the same plan cell, so
    every zoom frame is countable by construction. This is the only number on a frame that came
    out of the picture rather than out of the plan.

    `expect_period_m` is the part's OWN passage period, measured earlier off another frame of the
    same mesh (see slices.PERIOD_MATCH). A regular array at four or eight times that figure is a
    different feature of the same part, and a frame of it would answer "how many cells across a
    passage" about the wrong passage.
    """
    from PIL import Image

    from meshpipeline.engines.cfmesh_conjugate.slices import (
        SCAN_AXES,
        ink_structure,
        period_matches,
    )

    with Image.open(path) as im:
        content = ink_structure(_as_gray(im))
    if content.get("period_px"):
        content["period_m"] = float(content["period_px"]) * float(mpp)
        content["period_um"] = round(content["period_m"] * 1e6, 3)
        # WHICH WORLD AXIS the repeats run along, which is what re-aims the other cuts.
        content["repeat_world_axis"] = ah if content.get("scan") == SCAN_AXES[0] else av
    if expect_period_m and content.get("shows_structure"):
        content["part_period_m"] = float(expect_period_m)
        if not period_matches(content.get("period_m") or 0.0, expect_period_m):
            content["shows_structure"] = False
            content["why"] = (
                f"this array repeats every {float(content.get('period_m') or 0.0) * 1e6:.2f} um "
                f"and this part's passages were measured at {expect_period_m * 1e6:.2f} um, so "
                "it is a different feature of the same part")
    content["measured"] = "the drawn PNG's own pixels"
    frame["content"] = content
    return content


def _as_gray(im):
    import numpy as np

    return np.asarray(im.convert("L"))


def _render_frame(pv, sub, path: Path, *, axis: int, ah: int, av: int, origin, centre,
                  mpp: float, width_px: int, height_px: int) -> None:
    """One screenshot of one window of one plane, at exactly the published metres per pixel."""
    plotter = pv.Plotter(off_screen=True, window_size=[width_px, height_px])
    plotter.set_background("white")
    # EDGES ON, ONE PIXEL WIDE, AND NO LIGHTING. The question is how many CELLS are across a
    # passage, and a flat-shaded cut answers where the coolant is and not that.
    #
    # `lighting=False` is what the home side's own `inspect_region` already passes, and it is
    # worth about 30% of contrast: with it on, Mesa shades the fill from grey 193 down to 149 and
    # the pixels a cell edge blends into from 87 to 67, while the black of a fully covered edge
    # pixel stays at 0. Measured on one slice drawn both ways, the median contrast across a scan
    # row is 53 against 41 at the aligned camera phase and 142 against 109 half a pixel off.
    #
    # AND THE EDGE STAYS ONE PIXEL, against the instruction to widen it, because widening it is
    # worse at every phase. RE-MEASURED 2026-11-17 through this function's own body, sweeping the
    # camera centre over 16 sixteenths of a pixel on a uniform 25.1578 um grid slab (SYNTHETIC -
    # the real plate's interior was pruned off its workspace, so this is a measurement of the
    # RENDERER and not of the part):
    #
    #                             line_width 1                    line_width 2, black pixels
    #     4 px/cell  aligned      480 of 960 rows flat, med 53    75.00%
    #     4 px/cell  other 15     0 of 960 rows flat, med 68-147  56.25% at every one
    #     8 px/cell  aligned      240 of 960 rows flat, med 106   43.75%
    #     8 px/cell  other 15     0 of 960 rows flat, med 118-193 32.81% at every one
    #
    # TWO EARLIER STATEMENTS OF THIS ARE BOTH WRONG and neither is wrong in a way that changes
    # the decision. This comment said a 2-pixel edge renders "phases 0 to 4 of 16 as SOLID BLACK
    # at 4 pixels per cell" and "seven pixels in eight black at 8 per cell": 0 of 16 phases are
    # solid black at either scale, the worst is 75.00%, and the 8 px/cell worst is 43.75% - half
    # of seven-in-eight. A correction offered against it, "75.00% at the aligned phase and 43.75%
    # at the other fifteen", mixes the two scales: 75.00% and 43.75% are the ALIGNED phases of 4
    # and 8 px/cell, and the fifteen others are 56.25% and 32.81%. Either way more than half the
    # frame is edge at line_width 2 where a 1-pixel edge leaves an interior at every phase, so
    # the conclusion stands unchanged.
    #
    # WHAT IT WAS MEANT TO CURE DOES NOT HAPPEN. The report was that 3 of 16 sub-pixel camera
    # phases render a 4-cell channel as one uniform bar with no interior boundary. Swept over the
    # WHOLE FRAME rather than one scan row, the weaker of the frame's two directions never falls
    # below 53 grey levels at any of the 16 phases and 0 of 16 render as one bar. What IS true is
    # narrower: at the ONE aligned phase, and only there, half the scan rows at 4 px per cell and
    # a quarter of them at 8 carry no variation at all, because a cell edge blends across the
    # whole row - and 0 rows do at the other fifteen phases, measured, which is also a correction
    # to a claim that seven of the sixteen carry 240 flat rows. A reader that samples ONE row at
    # the aligned phase sees a flat bar; the frame it sampled has 53 grey levels of contrast at
    # the cell's own 4-pixel period. `ink_structure` sums a whole profile and never samples a
    # row, which is why it does not care.
    plotter.add_mesh(sub, show_edges=True, line_width=1, color="lightsteelblue",
                     edge_color="black", lighting=False)
    cam = plotter.camera
    cam.parallel_projection = True
    pos = list(origin)
    # Far enough back that the whole cut is in front of the camera whatever the part's size;
    # parallel projection makes the distance itself irrelevant to the scale.
    pos[axis] = origin[axis] + max(1e-3, 10.0 * mpp * max(width_px, height_px))
    focal = list(origin)
    focal[ah], focal[av] = centre[0], centre[1]
    pos[ah], pos[av] = centre[0], centre[1]
    cam.position = tuple(pos)
    cam.focal_point = tuple(focal)
    up = [0.0, 0.0, 0.0]
    up[av] = 1.0
    cam.up = tuple(up)
    # THE SCALE, SET RATHER THAN FITTED. parallel_scale is the half-height of the view in metres,
    # so this makes the published um/px exactly true of the picture. Verified on the real plate by
    # drawing a 100-pixel calibration segment into the frame and measuring it back out: it came
    # back 100 px at both windows tested.
    cam.parallel_scale = mpp * height_px / 2.0
    plotter.screenshot(str(path))
    plotter.close()


def _place_by_what_is_drawn(mesh, cut: dict, *, frame: dict, axis: int, ah: int, av: int,
                            root: Path, pv, width_px: int, height_px: int,
                            expect_period_m: float = 0.0):
    """Move the plane and the window until the PICTURE holds the array, or report that none does.

    This is the whole point of the round. A window is `plan_cell / CELL_PIXELS` and the reader
    divides by the same plan cell, so every zoom frame is countable by arithmetic whatever is in
    it - and on job 0ff6f9e5 two of the three held a plenum rectangle and a port bore and were
    introduced to the reviewer as frames cells can be counted in. Nothing but the pixels can tell
    those apart from the real thing, so the pixels are what chooses where the cut goes.

    THE ORDER IS PLAN FIRST. The planned plane and the planned window are tried before anything
    else, so a part whose fine zone already points at its features costs one slice and one
    screenshot and this function never searches. See POSITION_TRIES for the sweep that shows why
    the fallback exists: the middle of this plate's fine zone is empty and the channels are in the
    bottom tenth of it.

    Returns (slice, origin) for the place that was kept, and fills the frame's `content`,
    `centre_m`, `centre_source` and the cut's `origin`/`origin_source`. When nothing shows the
    array it keeps the attempt with the most repeats, so the published frame is the best picture
    there was and is marked as not holding what the cut was for.
    """
    mpp = float(frame.get("metres_per_pixel") or 0.0)
    win_h, win_v = mpp * width_px, mpp * height_px
    planned = [float(v) for v in (frame.get("centre_m")
                                  or (cut["origin"][ah], cut["origin"][av]))]
    planned_centre_source = str(frame.get("centre_source") or "")
    path = root / f"{cut.get('name', 'cut')}_zoom.png"
    tries = 0
    best = None                       # (score, origin, centre, slice, content, source)
    for origin in _candidate_origins(cut, axis, [float(v) for v in cut["origin"]]):
        sl = _slice_at(mesh, axis, origin, cut)
        if sl is None:
            continue
        for n, centre in enumerate(_candidate_centres(sl, ah, av, win_h, win_v, planned)):
            sub = sl.clip_box(_window_box(centre, ah, av, win_h, win_v), invert=False)
            if sub.n_points == 0:
                continue
            tries += 1
            try:
                _render_frame(pv, sub, path, axis=axis, ah=ah, av=av, origin=origin,
                              centre=centre, mpp=mpp, width_px=width_px, height_px=height_px)
                content = _measure_frame(path, frame, ah=ah, av=av, mpp=mpp,
                                         expect_period_m=expect_period_m)
            except Exception:             # noqa: BLE001
                logger.warning("conjugate: a placement of %s could not be drawn (non-fatal)",
                               cut.get("name"), exc_info=True)
                continue
            source = (planned_centre_source if n == 0 else
                      f"the densest window-sized patch of this plane (rank {n})")
            # A FAILED SEARCH USED TO SELECT FOR THE ONE FRAME THAT CANNOT SHOW ANYTHING.
            # The second term was `ink_fraction`, highest wins, and `ink` is `a < INK_MAX` - so a
            # fully saturated frame reads 1.000000, the maximum the term can take. With no
            # placement showing structure the tiebreak therefore published the MOST saturated
            # frame drawn, which is exactly the plenum signature `picture_structure` exists to
            # catch: measured on across_the_flow_zoom.png of job cba7ac69, ink_fraction 1.000000,
            # zero pixels at or above 250 of 1,228,800, 0 repeats. That frame was not an unlucky
            # draw, it is what the score asked for.
            #
            # BALANCE INSTEAD OF QUANTITY. `min(ink, 1 - ink)` is 0 at both ends and peaks at a
            # half, so of the frames that answered nothing it keeps the one a reader can see both
            # the coolant and the metal in - which is the whole stated purpose of publishing a
            # refused frame ("so a reader can see WHAT was drawn instead"). An all-ink frame and
            # an empty frame both score 0, correctly: neither shows a thing.
            _ink = float(content.get("ink_fraction") or 0.0)
            score = (int(content.get("repeats") or 0), min(_ink, 1.0 - _ink))
            if content.get("shows_structure"):
                best = (score, origin, centre, sl, content, source)
                break
            if best is None or score > best[0]:
                best = (score, origin, centre, sl, content, source)
        if best is not None and best[4].get("shows_structure"):
            break
    if best is None:
        return None, [float(v) for v in cut["origin"]]
    _, origin, centre, sl, content, source = best
    cut["origin"] = [float(v) for v in origin]
    cut["origin_source"] = (
        f"{tries} place(s) inside the search band were drawn and measured; this one "
        + ("holds the array" if content.get("shows_structure") else "held the most of anything"))
    frame["centre_m"] = [float(centre[0]), float(centre[1])]
    frame["centre_source"] = source
    frame["content"] = content
    return sl, [float(v) for v in origin]


def _draw_one_cut(mesh, cut: dict, *, root: Path, ws: Path, pv, width_px: int, height_px: int,
                  out: dict, expect_period_m: float = 0.0) -> None:
    """One cut, drawn once per published frame, with the frame's own window on the camera.

    `expect_period_m` is the part's own passage period as another frame of this mesh measured it;
    see slices.PERIOD_MATCH for the two coarser arrays on this plate that it keeps out.
    """
    normal = [float(v) for v in cut.get("normal") or (1.0, 0.0, 0.0)]
    origin = [float(v) for v in cut.get("origin") or (0.0, 0.0, 0.0)]
    axis = max(range(3), key=lambda k: abs(normal[k]))
    ah, av = cut.get("plane_axes") or [k for k in range(3) if k != axis]
    sl = None
    zoom_frame = next((f for f in cut.get("frames") or []
                       if f.get("scope") == "zoom"
                       and float(f.get("metres_per_pixel") or 0.0) > 0.0), None)
    if zoom_frame is not None:
        # THE ZOOM IS WHAT PLACES THE CUT, because it is the frame that has to hold the array and
        # the only one anything can be counted in. The plane and window it was found at are then
        # what the whole-part frame is drawn through too, so the two frames are of the same place.
        sl, origin = _place_by_what_is_drawn(mesh, cut, frame=zoom_frame, axis=axis, ah=ah, av=av,
                                             root=root, pv=pv, width_px=width_px,
                                             height_px=height_px,
                                             expect_period_m=expect_period_m)
    if sl is None:
        sl = _slice_at(mesh, axis, origin, cut)
    if sl is None:
        # A PLANE THAT MISSED. Said against the cut rather than left as an empty picture, because
        # an empty frame and a frame of fine metal look identical to a reader.
        cut.setdefault("cut_why", "this plane cut no cells")
        return
    for frame in cut.get("frames") or []:
        zoom = frame.get("scope") == "zoom"
        mpp = float(frame.get("metres_per_pixel") or 0.0)
        if mpp <= 0.0:
            continue
        centre = [float(v) for v in (frame.get("centre_m") or (origin[ah], origin[av]))]
        name = f"{cut.get('name', 'cut')}_{frame.get('scope', 'frame')}.png"
        path = root / name
        try:
            sub = sl
            if zoom:
                sub = sl.clip_box(_window_box(centre, ah, av, mpp * width_px, mpp * height_px),
                                  invert=False)
                if sub.n_points == 0:
                    frame["frame_why"] = "nothing of the cut falls inside this window"
                    continue
            _render_frame(pv, sub, path, axis=axis, ah=ah, av=av, origin=origin, centre=centre,
                          mpp=mpp, width_px=width_px, height_px=height_px)
        except Exception as exc:          # noqa: BLE001
            frame["frame_why"] = f"this frame could not be drawn: {exc}"
            logger.warning("conjugate: cut frame %s failed (non-fatal)", name, exc_info=True)
            continue
        if not path.is_file():
            continue
        try:
            content = _measure_frame(path, frame, ah=ah, av=av, mpp=mpp,
                                     expect_period_m=expect_period_m)
        except Exception as exc:          # noqa: BLE001
            # UNMEASURED IS NOT EVIDENCE EITHER. A frame nobody read the pixels of is in exactly
            # the position every frame was in before this existed, and that is what published a
            # port bore as a channel count.
            content = {"shows_structure": False, "why": f"this frame could not be measured: {exc}"}
            frame["content"] = content
            logger.warning("conjugate: cut frame %s could not be measured (non-fatal)", name,
                           exc_info=True)
        # EVERY DRAWN FRAME IS PUBLISHED UNDER `image`, AND THE FIGURE THAT REFUSED IT TRAVELS
        # WITH IT. The previous round withheld a refused zoom's pixels - it wrote the path to
        # `image_context` instead - so that the home side's coverage floor could not be
        # discharged by a picture of a port bore. That was the right worry and the wrong lever,
        # for four measured reasons:
        #
        #   1. WITHHOLDING DESTROYS THE REASON. `sandbox/review_session._resolve_cut` takes the
        #      path from `_PATH_KEYS = ("image", "path")` and returns None when neither is there
        #      - "names no file - not offered" - BEFORE it reads the `content` block. So the
        #      reviewer was told the region has no zoom frame and never learned that one WAS
        #      drawn, measured, and found to be a port bore or an 805.05 um bank of coarse fins.
        #      `_draw_one_cut`'s own comment twelve lines up says why that is the worst outcome:
        #      "an empty frame and a frame of fine metal look identical to a reader".
        #   2. NOTHING READ EITHER KEY. `image_context` had exactly one reader in src/ - the
        #      `_rematch` fallback in this file, now gone with it - and `frame_why` had none at
        #      all. `engines/manifest._published_cuts` checks `image` for existence and passes
        #      every other key through, so a dangling `image_context` travelled unchecked while
        #      a dangling `image` was dropped and logged.
        #   3. THE HOME SIDE ALREADY REFUSES IT, on its own measurement of its own copy of the
        #      PNG, with no content block needed: `review_session.ResolvedCut.shows_cells` reads
        #      `cells_on_best_row` against COUNTABLE_CELLS_ON_A_ROW. Measured on job 0ff6f9e5's
        #      two no-passage frames, that reading is 1 and 0 against a floor of 4.
        #   4. AND IT ALREADY HANDLES A PRODUCER VETO CORRECTLY. `_resolve_cut` carries
        #      `claimed_structure` and `claimed_why` off this `content` block, and
        #      `review_session` logs "the side that drew it refused the frame ... a claim from the
        #      producer may veto and may not grant" and does not satisfy the region from it. So
        #      publishing the picture WITH `shows_structure: false` is strictly stronger than
        #      hiding it: the region is still not discharged, and the reviewer gets the picture
        #      and the figure that refused it.
        frame["image"] = path.relative_to(ws).as_posix()
        frame["image_bytes"] = int(path.stat().st_size)
        out["rendered"] = int(out.get("rendered", 0)) + 1
        if not zoom:
            continue
        if content.get("shows_structure"):
            # THE ONLY FRAMES THAT CAN ANSWER WHAT THE CUT EXISTS FOR, counted separately from
            # `rendered`. See _drop_cut_volume: `rendered` counts whole frames too and every
            # frame is now published whatever its pixels hold, so it cannot be the guard on
            # throwing the interior away.
            out["usable"] = int(out.get("usable", 0)) + 1
        else:
            frame["frame_why"] = (
                "this frame is published with the measurement that refuses it as evidence: "
                + str(content.get("why") or "its pixels hold no repeating structure")
                + ". The picture is here so a reader can see WHAT was drawn instead")
            logger.warning("conjugate: %s is not evidence - %s", name, frame["frame_why"])


def _fine_zone(plan) -> tuple[list | None, list | None]:
    """The band of the block the part's fine geometry is in, from the plan's own zones.

    `stretch_zones[0]` first and `fine_zones[0]` after it. The stretch zone is the box the cells
    were stretched in because the geometry is PRISMATIC through it, which is the same box the
    channels occupy - on job 0ff6f9e5 it is y = 21.51 to 33.55 mm and the plan carries no fine
    zone at all (`fine_zones: []`, because the refinement is carried by a fine SURFACE,
    `fluid_fine.stl`, which has bounds but is not read here: this runs before any geometry is
    opened and a 22 MB STL read to place a camera is a cost for nothing).

    (None, None) when the plan has neither, which is a legal plan: the cuts then fall back to the
    middle of the block, exactly as they did, and whatever they catch is measured and said.
    """
    for attr in ("stretch_zones", "fine_zones"):
        for zone in (getattr(plan, attr, None) or ()):
            lo, hi = (zone.get("lo"), zone.get("hi")) if isinstance(zone, dict) else (None, None)
            if lo is not None and hi is not None and len(lo) == 3 and len(hi) == 3:
                return list(lo), list(hi)
    return None, None


def _review_aids(ws: Path, *, bashrc: str, refused: bool = False, cells: int = 0,
                 budget_left_s: float | None = None, plan=None,
                 layers: dict | None = None) -> dict:
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
    # THE CUTS, DRAWN HERE because this is where the mesh and the 512 GiB are. See _render_cuts
    # for why the pictures travel home and the interior does not.
    #
    # THE BOX THEY ARE PLACED IN comes from the PLAN, not from the boundary index: this function
    # runs about sixteen lines before `_boundary_indexes` writes that index, so anything reading
    # constant/*/polyMesh/boundaryIndex/meta.json here reads a file that does not exist yet.
    # `_render_cuts` then records the exported volume's own bounds beside them, so a reader can
    # see both the box the cuts were placed from and the box they were drawn against.
    try:
        from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID
        from meshpipeline.engines.cfmesh_conjugate.slices import (
            drawn_cell_m as slices_drawn_cell_m,
        )
        from meshpipeline.engines.cfmesh_conjugate.slices import review_slices

        lo = getattr(plan, "block_lo", None) if plan is not None else None
        hi = getattr(plan, "block_hi", None) if plan is not None else None
        if lo and hi:
            out["cuts_bbox_source"] = "the plan's own block (conjugate_plan.block_lo/block_hi)"
            # ...AND THE PLANES COME FROM THE FINE ZONE INSIDE IT. The block is the whole domain
            # and the channels are one band of it: on job 0ff6f9e5 the block's middle puts
            # `through_the_plate` at y = 41.070 mm while every passage lies between 21.51 and
            # 33.55 mm, so that plane could not cross one at any window. The zone is the plan's
            # own statement of where the fine geometry is (`stretch_zones[0]`, the band the cells
            # were stretched in), which is the same band `fine_surfaces[0].file` covers.
            fine_lo, fine_hi = _fine_zone(plan)
            out["cuts_placed_from"] = ("the plan's fine zone (conjugate_plan.stretch_zones[0])"
                                       if fine_lo else "the plan's block (it names no fine zone)")

            # WHAT THE ZOOM FRAMES WILL ACTUALLY DRAW, which is not the cell the plan pinned.
            # The layer stack sits INSIDE the interface cell, so the picture shows what is
            # left of that cell. Passing it here is what lets the frame state a claim the
            # reader can check instead of a scale identity it cannot - see
            # slices.drawn_cell_m, and job b8f09ae9, which lost its whole review to the
            # difference. The row is the interface patch's own measured one; anything
            # missing leaves the plan cell in place.
            _plan_cell = float(getattr(plan, "fine_cell_pinned_m", 0.0) or 0.0)
            _row = ((layers or {}).get("patches") or {}).get(FLUID_TO_SOLID) or {}
            _drawn = slices_drawn_cell_m(_plan_cell,
                                         layer_thickness_m=_row.get("thickness_m"),
                                         layers_in_mesh=_row.get("mesh_layers"))

            def _slices(repeat_axis: int | None = None):
                return review_slices(lo, hi,
                                     inlet=getattr(plan, "inlet", "") or "",
                                     outlet=getattr(plan, "outlet", "") or "",
                                     cell_m=_plan_cell, drawn_m=_drawn,
                                     fine_lo=fine_lo, fine_hi=fine_hi, repeat_axis=repeat_axis)

            cuts = _slices()
            left = (None if budget_left_s is None
                    else max(0.0, float(budget_left_s) - (time.monotonic() - started)))
            out["cuts_render"] = _render_cuts(ws, cuts, volume=out.get("volume"),
                                              cells=int(cells), budget_left_s=left,
                                              replan=_slices)
            out["cuts"] = cuts
        else:
            out["cuts"] = []
            out["cuts_render"] = {"rendered": 0,
                                  "why": "the plan carries no block, so no cut can be placed"}
    except Exception:                     # noqa: BLE001
        logger.warning("conjugate: the review cuts failed (non-fatal)", exc_info=True)
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
                     "pieces": int(r.pieces),
                     # THE SIZES BESIDE THE COUNT, and named here or they do not exist on the
                     # other side: this dict is what finalize reads across the container
                     # boundary. The count arrived alone for every run until now, and it is the
                     # one reading that separates a passage cut in half from cells that were
                     # never coolant. [[project-corintis-island-diagnosis]]
                     "piece_cells": [int(x) for x in r.piece_cells],
                     "mesh_ok": bool(r.mesh_ok),
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
        # THE ASK AND THE OUTCOME, side by side. conjugate_plan.json ships to the customer and
        # records what was ASKED of the mesher - it is written before the mesh exists, so it
        # cannot know which mouths survived. This is where the mesh answers it. A dropped mouth
        # is invisible to `empty_patches`, because createPatch removes the patch rather than
        # leaving it empty (job 57b613d7, mouth_1).
        "port_patches_planned": list(report.port_patches_planned or ()),
        "port_patches_dropped": list(report.port_patches_dropped or ()),
        "seconds": dict(report.seconds),
        "peak_rss_bytes": int(report.peak_rss_bytes or 0),
        # WHAT THE LAYER PASS GREW, which is a DIFFERENT NUMBER from what was asked for and the
        # one this dict never carried. `conjugate.build` reads the layer log the moment the stage
        # exits, refuses a pass that grew nothing (`layers.complain`) and lists where it did worse
        # than the run the thresholds came from (`layers.shortfalls`) - and then put all of it in
        # `ConjugateReport.layers`, which this function dropped. So the gate ran and nothing it
        # measured ever crossed the container boundary.
        #
        # MEASURED ON THE REAL PLATE, job 0ff6f9e5 (2026-11-16), whose plan asked for 3 layers on
        # 8 ranks and whose snappyHexMesh.log is preserved. Re-read through `layers.read_layer_log`:
        # extrusion started at 97.2267% and the ten repair iterations took it DOWN every time to
        # 88.5826%, at 71.9% of the asked thickness and 2.25 layers of 3, with 8,916 faces in error
        # over 37,247,044 cells added - 239 per million. Against this module's own floors that is
        # THREE shortfalls (KEPT_OF_OFFERED_FLOOR_PCT 95.0, THICKNESS_FLOOR_PCT 75.0,
        # ILLEGAL_PER_MILLION_CEILING 200.0), every one of which `shortfalls` produced and logged.
        # The delivered `conjugate_report.json` has no `layers` key at all and the delivered
        # `mesh_manifest.json` published `near_wall_layers: 3`. The run's Python log is not among
        # the preserved files either, so the only trace of the judgement was a warning on a VM that
        # has since been deleted.
        #
        # `layers.shortfalls` IS WHERE THE READING IS TAKEN, once, and this carries what it said
        # rather than re-deriving it on the far side. `cells` there is the pass's own `cells_added`,
        # which is not any number the manifest holds, so a second reader could not get the same
        # answer - and this repo has already paid for one quantity with two readers that disagreed.
        "layers": dict(getattr(report, "layers", None) or {}),
        "plan": {"background_cell_m": plan.background_cell_m, "levels": plan.levels,
                 "cells_across": plan.cells_across, "volume_tol": plan.volume_tol,
                 "narrowest_m": plan.narrowest_m,
                 "expect_fluid_volume_m3": plan.expect_fluid_volume_m3,
                 "expect_fluid_pieces": plan.expect_fluid_pieces,
                 "ranks": int(getattr(plan, "ranks", 1) or 1),
                 # WHETHER THE REGIONS WERE MERGED, because a delivery that was not is a
                 # different artefact: no serial volume mesh, the boundary index built
                 # from processor*/ and the reviewer's interior coming from the index
                 # rather than a VTK. It was absent, and absent reads as False, which
                 # reads as "not merged" for a run that was. Default True to match
                 # `ConjugatePlan.merge_regions`, so a plan written before the field
                 # existed still echoes what it actually did.
                 "merge_regions": bool(getattr(plan, "merge_regions", True)),
                 # THE LAYER ASK, for the same reason the fine surfaces below are here and with
                 # the same defect behind it: `finalize._near_wall` reads `near_wall_layers` off
                 # THIS dict, so without the key a three-layer mesh published "Boundary layers:
                 # none, graded refinement instead" in the customer's own report.
                 "layers": int(getattr(plan, "layers", 0) or 0),
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
