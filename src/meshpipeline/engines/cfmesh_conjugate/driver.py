# Responsibility: Build a conjugate mesh for one Builder attempt with no model in the loop: cut the coolant out of the part, choose the resolution, write the plan, run the mesher.
# Boundaries: deterministic; it authors nothing a model could get wrong. It publishes nothing itself - what the user reads comes through finalize's output and the manifest.
# Collaborates with: agents/builder/invoke.py (calls drive through spec.build_driver) and cfmesh_conjugate/plan.py.
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.engines.determinism as det
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import plan as PL

if TYPE_CHECKING:
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher

logger = logging.getLogger(__name__)

#: Failure markers this driver ends an attempt with (the run record carries them).
NO_SOURCE = "conjugate_source_missing"
NO_COOLANT = "conjugate_no_coolant"
#: REFUSED FOR ITS SIZE, NOT FOR ITS COOLANT. Four refusals below price the part against a budget
#: and decline before anything runs: two scales of passage that no pair of cell sizes resolves, an
#: estimate over the deployment's cell limit, cartesianMesh's own 32-bit octree ceiling, and an
#: archive the caller would not take back. Every one of them reported `conjugate_no_coolant`, on
#: parts whose coolant had just been found, measured and written to disk a few lines earlier.
#:
#: MEASURED on the first customer assembly, 2026-10-30 (ColdPlateAssembly_CLEAN.step, job
#: 7439e942): the split wrote 53,661.79 mm3 of coolant over two passages and the driver then
#: refused for two scales of passage - 2.11 mm against 0.0982 mm - and filed it as "no coolant".
#: The log line above each refusal says the true reason; the marker is what survives into the
#: failure record, and it was saying the opposite. The comment beside the archive check already
#: states the standard this did not meet: "the reason names the setting rather than blaming the
#: part".
TOO_FINE = "conjugate_too_fine_for_budget"
BUILD_FAILED = "conjugate_build_failed"

#: THE PART HAS NO PATH FROM ITS INLET TO ITS OUTLET, and its own marker because it blames
#: neither a setting nor this mesher: nothing about the budget, the resolution or the machine
#: changes it, and `conjugate_too_fine_for_budget` would send the next reader to lower the cell
#: count on a part that would still not be solvable at any count. See
#: `cad/fluid_passages.ports_on_one_passage` for the measurement and for why it is taken on the
#: surface rather than on the finished mesh.
SEVERED_COOLANT = "conjugate_coolant_severed"

#: The gate a refusal before meshing stands in for. `manifest_valid` is the one that already
#: covers "a mesh over budget" alongside a mesher that stopped and a boundary that is missing
#: (application/final_result._gate_account says so in those words), and a part priced out before
#: the mesher starts is exactly that.
_REFUSAL_GATE = "manifest_valid"


def _say_why_before_meshing(ws, words: str, *, job_id: str, priced_out: bool = True,
                            geometry: bool = False) -> None:
    """Carry a refusal's OWN WORDS to the user, not just its marker.

    THE WORDS WERE BEING THROWN AWAY. The comment on TOO_FINE above records half of this already -
    "the log line above each refusal says the true reason; the marker is what survives into the
    failure record" - and fixed the marker. The words were still only logged. Measured end to end
    on the first customer part, 2026-11-12, job 0904c8bc: the engine wrote

        this part has two scales of passage - about 2.11 mm and 0.0982 mm - and no pair of cell
        sizes resolves the thin ones at 4 cells across inside a limit of 400,000,000 cells and
        this mesher's own ceiling of 178,956,970; a configuration that fits the cell limit was
        refused on the octree ceiling alone and would build on 2 ranks

    and the customer was shown "Conjugate stopped while preparing the mesh, before the mesher
    started, so there was nothing to check. THE NOTES ABOVE SAY WHAT IT STOPPED ON." There were no
    notes above: not one of 2.11 mm, 0.0982 mm, 178,956,970, 400,000,000 or "2 ranks" appeared
    anywhere on the page. The sentence pointed at an explanation that did not exist, and the
    engine's best work - a measured, specific, customer-grade account of their own geometry - went
    only to a log file nobody outside this repo can read.

    NOTHING NEW IS INVENTED TO CARRY IT. `engines/preflight` exists for this and says so: "a
    pre-flight that refuses a run writes one PreflightRefusal into the attempt's workspace - the
    only thing that crosses from the builder to the executor - ... so the user reads what was
    wrong", and "ANY pre-flight may write it". The executor merges `facts` into the failure record
    (pipeline/executor.py), `failure_cause.describe` reads `facts["reason"]` for NOT_BUILT, and
    the result is the engine's own sentence in the user's own message. All four size refusals
    below go through here, because all four had the same silence and each has different words.

    `deterministic` is already true of this driver and is set so the retry policy keeps reading it
    that way (contracts/failure_cause.retry_can_help); it is the fact that stops a second
    identical refusal, and it is stated here rather than inferred.
    """
    from meshpipeline.contracts.failure_cause import FailureCause
    from meshpipeline.engines.preflight import PreflightRefusal

    said = str(words or "").strip()
    if not said:
        return
    PreflightRefusal(
        gate=_REFUSAL_GATE,
        cause=FailureCause.NOT_BUILT.value,
        builder_text=said,
        facts={"reason": said, "deterministic": True,
               # WHAT KIND OF REFUSAL THIS IS, so the sentence does not blame the customer's file.
               # The four SIZE refusals price a mesh against a limit of OURS: the file is sound,
               # the approved setup is sound, and the number that stopped it is a 32-bit label in
               # our build of OpenFOAM or a cell budget we chose. Without this the user reads
               # "could not prepare its input from your file" and goes looking for a fault that is
               # not there.
               #
               # AND THE OPPOSITE IS JUST AS WRONG. A refusal about the SHAPE - a coolant whose
               # two ports are on pieces that do not touch - is not priced against anything, and
               # saying so sent a customer the wrong three sentences: "priced the mesh this part
               # would need and declined it", "this is a limit on our side", and "tell me if a
               # coarser mesh would do". Measured on job ca246a07, 2026-11-13: every one of those
               # was false, and the last one is advice that cannot work at any cell count. So the
               # caller says which kind it is, and `contracts/failure_cause` has a branch for each.
               "priced_out": bool(priced_out),
               "geometry_refusal": bool(geometry),
               # the mesher never started, so there is no mesh to judge and nothing was spent on
               # one - the user's sentence says so and this is what entitles it to
               "meshers_started": 0},
               # NO `engine` KEY. `describe` prefers facts["engine"] over the label the caller
               # passes, and the caller passes the one a human reads ("Conjugate (coolant +
               # metal)", via registry.engine_label). Putting the registry key here made the page
               # say "cfmesh_conjugate" to a customer.
    ).write(ws)
    logger.info("conjugate: refusal recorded for the user at gate %s - job_id=%s",
                _REFUSAL_GATE, job_id)

#: A FIFTH REFUSAL, AND IT IS NOT A SIZE ONE. The four above price a part against a budget and
#: decline because the answer is too many cells. This one declines because the geometry is not in
#: metres at all, which is a different fact about a different thing: the part is not too fine, it
#: is mis-measured, and filing it as `conjugate_too_fine_for_budget` would tell the next reader to
#: ask for less detail on a part whose detail was never the problem.
#:
#: It cannot be a size refusal even in principle, because no cell count can detect it. budget.py
#: prices area/cell**2 and volume/background**3 - dimensionless ratios - so a job consistently in
#: millimetres yields the IDENTICAL estimate to one consistently in metres. Measured: the plate
#: whose surfaces were written unscaled estimated 334,340,370 cells, passed every gate it was
#: offered, and built 877,659,515 octree boxes before it was killed at 250 of 251 GiB. The only
#: number that reveals it is an absolute length, which is what conjugate.part_size_complaint reads.
NOT_METRES = "conjugate_geometry_not_metres"

#: The CAD extensions the coolant can be cut out of. A triangle file cannot: the extraction needs
#: the solid's own faces to find which of them bound the space inside it.
_CAD = (".step", ".stp", ".iges", ".igs", ".brep")

#: The skin the staging wrote, in metres - where the block's own bounds come from.
_SKIN = "input.stl"


def find_source(workspace, source_path: str = "") -> Path | None:
    """The part's CAD, as the staging left it in the workspace.

    THE B-REP AND NOT THE SKIN. `cad/fluid_volume` cuts the coolant out of the solid by finding
    which of its faces bound the space inside it, and a triangle file has no faces to ask.
    """
    ws = Path(workspace)
    for suffix in _CAD:
        staged = ws / f"geometry{suffix}"
        if staged.is_file():
            return staged
    p = Path(source_path) if source_path else None
    return p if p is not None and p.is_file() and p.suffix.lower() in _CAD else None


def skin_bounds(workspace) -> tuple[list[float], list[float]] | None:
    """The part's own bounds in metres, from the staged skin."""
    from meshpipeline.cad.stl_io import read_stl_solids

    stl = Path(workspace) / _SKIN
    if not stl.is_file():
        return None
    pts = [v for tris in read_stl_solids(stl).values() for t in tris for v in t]
    if not pts:
        return None
    return ([min(p[i] for p in pts) for i in range(3)],
            [max(p[i] for p in pts) for i in range(3)])


def cad_scale(source) -> float:
    """What an OpenCASCADE transfer's own coordinates must be multiplied by to be metres.

    THE ONE SLIP THIS FILE COULD CARRY IS A UNIT, and it carried it: the extraction was called with
    scale=1.0 on a STEP that declares millimetres, so the manifold plate's coolant came back as
    4.56 m3 of fluid in a 100 mm channel. Every number downstream was off by a thousand - the cell
    size, the background cell, the cell estimate - and nothing in the chain had any way to notice,
    because every one of them was consistently wrong.

    IT IS 1e-3, FOR EVERY FILE, AND THAT IS THE WHOLE POINT. OpenCASCADE transfers into its own
    system unit, so a shape it hands back is in MILLIMETRES whatever the file declared - it has
    already applied the file's unit on our behalf. The contract says so
    (contracts/coordinate_state.OCC_OUTPUT_UNIT) and the same conversion is what the staging path
    applies through `PreparedCoordinates.to_metres`, which collapses to exactly 1e-3 whenever the
    interpretation agrees with what the parser applied.

    Returning the DECLARED unit's scale, as this did, is right ONLY for a millimetre file, which is
    every file the corpus happens to be made of. Measured on this plate by rewriting one entity of
    its STEP and reading each variant back through the same reader:

        declares        raw extent OCC returns      declared scale      what that gives
        millimetre            177.5085                   1e-3            0.177509 m   right
        metre              177500.0158                   1.0        177500.015811 m   1000x out
        centimetre           1775.0853                   1e-2           17.750853 m     10x out

    So the correct factor is the one that undoes OCC's own normalisation, and nothing else. The
    declared unit is deliberately not consulted: it would only reintroduce the bug, and reading it
    also inherits `parser_applied_unit`'s five fallbacks to metre, each of which silently became a
    no-op conversion under the old formula.

    `source` is kept in the signature because this is the seam every caller already passes a path
    to, and because a user OVERRIDE of the unit - the one case where the file's own declaration
    matters - belongs here when there is one to thread through.
    """
    from meshpipeline.contracts.coordinate_state import OCC_OUTPUT_UNIT
    from meshpipeline.contracts.geometry_units import scale_to_metres

    _ = Path(source)
    return float(scale_to_metres(OCC_OUTPUT_UNIT))


def coolant_sits_inside(rec: dict, lo, hi) -> str:
    """"" when the extracted coolant lies within the part, or what is wrong with it.

    THE CHECK THAT CATCHES A UNIT. A coolant whose own extent is not inside the part it was cut out
    of is not that part's coolant, and a factor of a thousand shows up here as a passage a
    thousand times longer than the plate. Cheap, exact, and it does not depend on trusting the
    unit the reader reported.
    """
    try:
        c_lo = [float(v) for v in rec["bbox_min_m"]]
        c_hi = [float(v) for v in rec["bbox_max_m"]]
    except (KeyError, TypeError, ValueError):
        return "the extraction reported no extent for the coolant"
    slack = 1e-9 + 1e-6 * max(float(hi[k]) - float(lo[k]) for k in range(3))
    for k in range(3):
        if c_lo[k] < float(lo[k]) - slack or c_hi[k] > float(hi[k]) + slack:
            said = (f"the coolant runs from {c_lo[k] * 1e3:.4g} to {c_hi[k] * 1e3:.4g} mm on "
                    f"axis {k} where the part runs {float(lo[k]) * 1e3:.4g} to "
                    f"{float(hi[k]) * 1e3:.4g} mm")
            # ...AND IF IT IS A UNIT, SAY SO. This check is what catches a scale that was never
            # applied, and it reported it as "no coolant found in the part" - which reads as a
            # geometry failure and sends the next hour into the extraction. When the two extents
            # differ by a round factor, that factor IS the answer.
            part = max(float(hi[j]) - float(lo[j]) for j in range(3))
            cool = max(c_hi[j] - c_lo[j] for j in range(3))
            if part > 0 and cool > 0:
                from meshpipeline.engines.cfmesh_conjugate.conjugate import _UNIT_FACTORS

                ratio = cool / part
                for factor, what in _UNIT_FACTORS:
                    if 0.9 <= ratio / factor <= 1.1:
                        return (f"{said}. The coolant is {ratio:.4g} times the part, which is "
                                f"{what}: the extraction was handed the wrong scale, not the "
                                "wrong geometry")
            return said
    return ""


def declared_port_names(workspace, plan: PL.ConjugatePlan) -> tuple[dict | None, str]:
    """{block face -> (the user's name, the role)} from the intake declaration, and what to say.

    THE GEOMETRY PICKS WHICH FACE, THE DECLARATION PICKS THE NAME. The mesher already works out
    which faces of the block the coolant reaches and which two of them are the ports, from the
    passage's own extent - that is a measurement and the user has nothing to add to it. What only
    the user knows is what to call them, so the declared inlet names the planned inlet and the
    declared outlet names the planned outlet.

    ONE OF EACH, or none. PortPlan carries a single inlet and a single outlet, so a part declared
    with two inlets is outside what this engine can name: the block keeps its own face names and
    the run says so, rather than putting the customer's `coolant_in_a` on a face chosen by
    alphabetical order.
    """
    from meshpipeline.engines.workspace_facts import port_declaration

    declared = [p for p in port_declaration(workspace)
                if isinstance(p, dict) and p.get("name")]
    if not declared:
        return None, ""
    by_role: dict[str, list[str]] = {}
    for p in declared:
        by_role.setdefault(str(p.get("type") or "").strip().lower(), []).append(str(p["name"]))
    names: dict[str, tuple[str, str]] = {}
    for role, face in (("inlet", plan.inlet), ("outlet", plan.outlet)):
        if not face:
            continue
        if len(by_role.get(role, [])) == 1:
            names[face] = (by_role[role][0], role)
    walls = by_role.get("wall", [])
    if len(walls) == 1:
        for face in plan.port_walls:
            names[face] = (walls[0], "wall")
    unnamed = [r for r in ("inlet", "outlet") if len(by_role.get(r, [])) > 1]
    said = ("" if not unnamed else
            f"the part declares more than one {' and more than one '.join(unnamed)}, and a "
            "conjugate block carries one of each: those faces keep the block's own names "
            f"({plan.inlet}, {plan.outlet}) and can be renamed on the delivered case")
    return (names or None), said


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun, source_path: str = ""):
    from meshpipeline.agents.builder.driver_run import (
        STOP_IDENTICAL_RERUN,
        STOP_REVIEWED_CASE_REPEATS,
        review_caused_retry,
    )
    from meshpipeline.cad.fluid_volume import surface_bounded
    from meshpipeline.contracts.mesh_execution import (
        note_native_pass,
        note_native_payload,
        refused_identical_rerun,
    )
    from meshpipeline.engines.cfmesh_conjugate.conjugate import ConjugateError

    ws = Path(workspace)
    src = find_source(ws, source_path)
    if src is None:
        logger.warning("conjugate: no CAD in %s - job_id=%s", ws, job_id)
        await run.fence("deliver builder outcome")
        return False, NO_SOURCE, run.outcome(produced_deliverable=False,
                                             failure_marker=NO_SOURCE)
    bounds = skin_bounds(ws)
    if bounds is None:
        await run.fence("deliver builder outcome")
        return False, NO_SOURCE, run.outcome(produced_deliverable=False,
                                             failure_marker=NO_SOURCE)
    # IS THIS A PART, OR IS IT A UNIT. Checked here, first, before anything is extracted or priced,
    # because it is the only question in this function that no later gate can answer: the cell
    # estimate is a dimensionless ratio, so a job that is consistently in the wrong unit passes
    # CELL_HARD_LIMIT, refuse_for_octree and the archive ceiling alike and then meshes until it is
    # killed. See conjugate.MAX_PART_M for the run that did.
    from meshpipeline.engines.cfmesh_conjugate import conjugate as _CU

    if (wrong_size := _CU.part_size_complaint(bounds[0], bounds[1])):
        logger.warning("conjugate: refusing to start - %s - job_id=%s", wrong_size, job_id)
        await run.fence("deliver builder outcome")
        return False, NOT_METRES, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=NOT_METRES)

    # THE COOLANT, cut out of the part itself. This is the step that makes the case a conjugate
    # one: without it there is a solid and no fluid, and nothing to split the block along.
    fluid_stl = ws / PL.FLUID_STL
    fluid_stl.parent.mkdir(parents=True, exist_ok=True)
    # ...AND THE DOMAIN THE MESHER WILL FILL, out of the same extraction. A conjugate mesher fills
    # ONE manifold volume and cuts the regions out of it afterwards, and the easy manifold is the
    # cuboid round the part - every cell of air between the part and that cuboid is paid for at
    # the background size. Measured on the first customer cold plate: the cuboid is 846,543 mm3
    # and metal plus coolant is 274,807 mm3, 32.5% of it by volume - which is worth 19.8% in
    # CELLS, not 67.5% (336,223,062 against 269,650,437 with only the block changed; see
    # conjugate.OUTER_PATCH for the run).
    # Never fatal: no domain, or one not worth its faces, and the box stands exactly as before.
    from meshpipeline.engines.cfmesh_conjugate import conjugate as _C

    rec = await asyncio.to_thread(surface_bounded, src, fluid_stl, scale=cad_scale(src),
                                  kind="cad", domain_path=ws / PL.DOMAIN_STL,
                                  domain_patch=_C.OUTER_PATCH)
    if rec:
        wrong = coolant_sits_inside(rec, bounds[0], bounds[1])
        if wrong:
            logger.warning("conjugate: the extracted coolant is not this part's - %s - job_id=%s",
                           wrong, job_id)
            rec = None
    if not rec:
        logger.warning("conjugate: no coolant found in %s - job_id=%s", src.name, job_id)
        await run.fence("deliver builder outcome")
        return False, NO_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=NO_COOLANT)

    # THE PART'S OWN SKIN, OR THE BOX. `write_domain` already measured which: share_of_box is how
    # much of its bounding box the domain actually is, and BOX_WORTH_KEEPING is where the cuboid
    # stops being worth its air. Decided once, here, so everything below reads one flag.
    _dom = rec.get("domain") if isinstance(rec, dict) else None
    shell = _C.use_shell_domain(rec)
    if shell:
        logger.info("conjugate: the domain is the part's own skin - %s mm3 of a %s mm3 box "
                    "(%.1f%%), %d triangles, so the air the box would have meshed is not meshed "
                    "- job_id=%s", format(_dom["volume_mm3"], ",.0f"),
                    format(_dom["box_mm3"], ",.0f"), _dom["share_of_box"] * 100.0,
                    _dom["triangles"], job_id)
    elif _dom and _dom.get("ok"):
        logger.info("conjugate: the part fills %.1f%% of its own box, so the box IS the domain "
                    "and there is nothing to build - job_id=%s",
                    float(_dom.get("share_of_box") or 0.0) * 100.0, job_id)

    # A retry a review asked for is meshed one step finer: the build is deterministic, so the
    # same plan would rebuild exactly the mesh the review just rejected.
    fidelity = str(state.get("effective_mesh_fidelity", "") or "") or B.DEFAULT_FIDELITY
    if review_caused_retry(state):
        fidelity = _finer(fidelity)
        if fidelity == "":
            await run.fence("deliver builder outcome")
            return False, STOP_REVIEWED_CASE_REPEATS, run.outcome(
                produced_deliverable=False, failure_marker=STOP_REVIEWED_CASE_REPEATS)

    narrowest = float(rec["narrowest_m"])
    limit = int(polcfg.CELL_HARD_LIMIT)
    # HOW MANY PROCESSES THIS MAY BE PLANNED ACROSS, decided ONCE and handed to both size gates.
    #
    # BOTH, or the change does nothing. The two gates ask different questions - plan_two_scales
    # asks whether a pair of cell sizes fits, refuse_for_octree asks whether the mesher can
    # address the result - and both priced against one process. Teaching only the first moves the
    # refusal 73 lines down and changes nothing a user sees, because the second then declines the
    # very plan the first just approved.
    #
    # PINNED, NOT READ OFF THE MACHINE. `mesh_machine.choose` runs ~150 lines below this, so the
    # machine is not known here, and MACHINES is not monotonic in cores (8, 16, 32, 64, then 40)
    # so walking it for a count inherits a trap. Every route has at least 8 cores, so a pinned
    # count up to 8 is satisfiable wherever this lands. determinism.conjugate_ranks says what set
    # it, and the plan lowers it again from the mesh's own size further down - so a part that fits
    # on one process is still built on one process, and nothing already measured moves.
    ranks, ranks_basis = det.conjugate_ranks()
    # THE OCTREE DEPTH IS CHOSEN, where it used to be a constant nothing varied. The old estimate
    # prices the whole block at the finest cell, which divides the background count by eight per
    # level and multiplies its own refinement factor by eight - so its answer is IDENTICAL at every
    # depth and the one knob that buys a coarse background under a fine coolant surface was
    # invisible to it. On a customer cold plate that collapsed draft, standard, fine and max to the
    # same five cells across; the band estimate reaches eight, at a finer cell and FEWER cells.
    area = float(rec.get("wetted_area_m2") or 0.0)
    levels = B.LEVELS
    # TWO SIZES OF PASSAGE, TWO CELL SIZES. cad/scales measured whether this part has one scale or
    # two off the radius field, and wrote its thin walls out separately if it has two. Sizing such
    # a part from its narrowest passage everywhere prices the whole wetted area at the finer cell:
    # on the first real customer cold plate, 60,526 mm2 at a slot-sized cell, about three billion
    # cells, where the slots are 35.9% of that area inside 6.0% of the volume. So the broad
    # passages are held at what they need and the rest of the budget goes to the thin ones.
    #
    # The single-scale path below is unchanged and is what every part in the corpus still takes:
    # of ten read on 2026-10-28, every one came back single-scaled.
    fine_surface = None
    stretch_zone = None
    scales = rec.get("scales") or None
    # BOUND ON BOTH PATHS. `two` is the two-scale plan and only the two-scale branch below sets
    # it; the single-scale paths leave it unbound, and reading it afterwards to size the
    # decomposition was a NameError on every part that is not two-scale - which is most of them.
    two: dict | None = None
    # BOUND ON BOTH PATHS for the same reason `two` is. These travel into the plan so the
    # container and the stamp learn what the HOST decided, rather than asking their own
    # environment a question whose answer is not there (see plan.ConjugatePlan.fine_cell_pinned_m).
    pinned_cell_m: float = 0.0
    under_planner_floor: bool = False
    if scales and rec.get("fine_surface") and area > 0.0:
        asked = B.CELLS_ACROSS_BY_FIDELITY.get((fidelity or "").strip().lower(),
                                               B.CELLS_ACROSS_BY_FIDELITY[B.DEFAULT_FIDELITY])
        # THE LICENCE TO STRETCH, carried by the surface that was measured rather than inferred
        # here. cad/scales measures whether the thin walls are an extrusion when it writes them;
        # where they are, their cells may be made taller along that axis for free and the cell
        # saving is exactly the factor. The first customer's microchannel field reads as an
        # extrusion along y at 0.0694 against a bar of 0.25, while the whole coolant - manifold
        # loop included - correctly reads as having no prismatic axis at all.
        fine_rec = rec.get("fine_surface") or {}
        prismatic = fine_rec.get("prismatic_axis")
        # A PINNED THIN CELL OVERRIDES THE LADDER, AND NEVER FALLS BACK TO IT. If the forced cell
        # cannot be reached the run is refused, because a run that quietly meshed at the
        # planner's own choice instead would be reported under the pinned cell's name - the exact
        # shape of defect this engine spent 2026-11-13 and 14 removing. See
        # engines.determinism.CONJUGATE_FINE_CELL_M for why the knob exists at all and why it is
        # not a sizing rule.
        _forced = det.forced_fine_cell()
        if _forced > 0.0:
            two, two_said = B.two_scales_at_fine_cell(
                block_lo=bounds[0], block_hi=bounds[1], scales=scales, limit=limit,
                fine_cell_m=_forced,
                prismatic_axis=(None if prismatic is None else int(prismatic)), ranks=ranks)
            pinned_cell_m = _forced
            under_planner_floor = bool((two or {}).get("under_planner_floor"))
        else:
            two, two_said = B.plan_two_scales(block_lo=bounds[0], block_hi=bounds[1], scales=scales,
                                              limit=limit, asked_across=asked,
                                              prismatic_axis=(None if prismatic is None
                                                              else int(prismatic)),
                                              ranks=ranks)
        if two is None:
            # REFUSED, and the reason is the part's own two scales rather than a cell count. Said
            # once, here, because the single-scale path would refuse the same part for a different
            # and less useful reason - its narrowest passage - and the user would be told the wrong
            # thing about their own geometry.
            logger.info("conjugate: refusing to start - %s - job_id=%s", two_said, job_id)
            _say_why_before_meshing(ws, two_said, job_id=job_id)
            await run.fence("deliver builder outcome")
            return False, TOO_FINE, run.outcome(produced_deliverable=False, exhausted=True,
                                                  failure_marker=TOO_FINE)
        narrowest = float(scales["broad"]["width_m"])
        cells_across, levels, estimate, said = (two["broad_across"], two["broad_levels"],
                                                two["estimate"], two_said)
        fine_surface = {"name": "microchannels", "file": PL.FLUID_FINE_STL,
                        "levels": int(two["fine_levels"]),
                        # four of its own cells, which is what the band needs to grade out of; the
                        # depth itself comes from the span, not from this (budget.band_depth)
                        "thickness_m": 4.0 * float(two["fine_cell_m"])}
        # THE STRETCH ZONE, over the thin walls' own box and no further. Writing it over the whole
        # block would stretch the manifold too, and the manifold is the part of this coolant that
        # bends - its cells have to stay cubes. The box is grown by the band's own thickness so
        # the grading shells on either side of the thin walls are stretched with them rather than
        # meeting a jump in cell shape at the boundary.
        if two.get("stretch", 1.0) > 1.0 and fine_rec.get("lo") and fine_rec.get("hi"):
            pad = 4.0 * float(two["fine_cell_m"])
            scale = [1.0, 1.0, 1.0]
            scale[int(two["stretch_axis"])] = float(two["stretch"])
            stretch_zone = {"name": "microchannels",
                            "lo": [float(v) - pad for v in fine_rec["lo"]],
                            "hi": [float(v) + pad for v in fine_rec["hi"]],
                            "scale": scale}
    elif area > 0.0:
        asked = B.CELLS_ACROSS_BY_FIDELITY.get((fidelity or "").strip().lower(),
                                               B.CELLS_ACROSS_BY_FIDELITY[B.DEFAULT_FIDELITY])
        cells_across, levels, estimate, said = B.plan_for_budget(
            block_lo=bounds[0], block_hi=bounds[1], narrowest_m=narrowest,
            wetted_area_m2=area, limit=limit, asked_across=asked)
        if estimate > limit:
            # REFUSED BEFORE IT IS PAID FOR. The older ladder meshes at its floor whatever the
            # estimate says, because that estimate has come out high by three times and refusing
            # would turn parts that mesh into parts never tried. This is a different question: a
            # part whose narrowest feature wants three billion cells against a limit of eight
            # million is not a three-times argument, and starting it spends hours and ends in a
            # kernel kill. The sentence says the number rather than asking for "less detail".
            logger.info("conjugate: refusing to start - %s - job_id=%s", said, job_id)
            _say_why_before_meshing(ws, said, job_id=job_id)
            await run.fence("deliver builder outcome")
            return False, TOO_FINE, run.outcome(produced_deliverable=False, exhausted=True,
                                                  failure_marker=TOO_FINE)
    else:
        cells_across, estimate, said = B.cells_across_for(
            fidelity=fidelity, block_lo=bounds[0], block_hi=bounds[1],
            narrowest_m=narrowest, limit=limit)
    # THE MESHER'S OWN CEILING, WHICH IS A DIFFERENT QUESTION FROM THE DEPLOYMENT'S LIMIT.
    #
    # Everything above is priced against polcfg.CELL_HARD_LIMIT, which is a COMPUTE-feasibility cap
    # - "the Cloud Run feasibility limit (8 vCPU / 32 GiB / ~50 min)", says policy.py - and it is
    # env-settable, because a bench machine or a bigger VM is a different deployment. Raising it is
    # the right move for a large part and it is exactly what a conjugate run on a 251 GB box does.
    #
    # But cartesianMesh has a ceiling that no limit and no machine moves: it allocates about twelve
    # 32-bit labels per octree box, and this OpenFOAM is built with 32-bit labels, so it aborts
    # partway through above roughly 179 million boxes per process. Measured on one part, same
    # machine: 102,152,624 boxes built, 204,543,984 aborted after 302 s having used a third of the
    # memory available. See budget.OCTREE_BOXES_PROVEN_OK.
    #
    # So the two are checked separately, and this one is checked LAST, because it is the one that
    # does not care what machine was chosen. Without it a raised cell limit turns a refusal into a
    # five-minute abort that reports as a mesher which stopped before writing anything.
    # A PIN THAT COULD NOT APPLY SAYS SO. The pinned branch is reached only by a part with two
    # measured scales and a thin surface; a single-scale part is sized by its own ladder and the
    # knob is simply not a question that arises for it. Saying nothing was the worse option,
    # because the knob is deployment-wide: a mixed batch would quietly mesh most of its parts off
    # the ladder while an operator believed every one of them was pinned.
    if det.forced_fine_cell() > 0.0 and not pinned_cell_m:
        logger.info("conjugate: CONJUGATE_FINE_CELL_M is set to %.6g m but this part is not "
                    "two-scale, so the pin does not apply and the resolution below was chosen by "
                    "the planner - job_id=%s", det.forced_fine_cell(), job_id)
    octree_said = B.refuse_for_octree(estimate, ranks=ranks)
    if octree_said:
        logger.info("conjugate: refusing to start - %s - job_id=%s", octree_said, job_id)
        _say_why_before_meshing(ws, octree_said, job_id=job_id)
        await run.fence("deliver builder outcome")
        return False, TOO_FINE, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=TOO_FINE)
    # ...AND WHETHER IT CAN COME BACK. The mesher runs off-box and its workspace returns as one
    # archive through a bounded extractor; a mesh too big for that archive is a mesh that builds
    # and is then lost at the last step, which is exactly what the coupon plate did. Same shape as
    # the two checks above: priced from the estimate, refused before anything runs, and the reason
    # names the setting rather than blaming the part.
    # THE CEILING THE CALLER WILL ACTUALLY APPLY, not the generic one. A returned mesh is
    # extracted under `result_extraction_limits`, which sizes the archive from the cell budget and
    # from the mesh's own cell count - so comparing an estimate against safe_extract's 512 MiB
    # floor refuses meshes the caller would take. Measured 2026-10-29: the coupon plate's archive
    # was 570 MB and the extractor waiting for it allowed 1,279 MiB.
    #
    # The estimate is handed over as the cell count, because that is the number the extractor will
    # be given too - and `result_extraction_limits` bounds what it will believe at twice
    # CELL_HARD_LIMIT, which is why a 100M-cell mesh is still refused on an 8M budget. That bound
    # is the real lever and the refusal names it.
    from meshpipeline.adapters.mesh_execution.cloud_run_client import result_extraction_limits

    _cap = result_extraction_limits({"rc": 0, "quality": {"cells": int(estimate)}})
    return_said = B.refuse_for_return(estimate, cap_bytes=_cap.max_archive_bytes)
    if return_said:
        logger.info("conjugate: refusing to start - %s - job_id=%s", return_said, job_id)
        _say_why_before_meshing(ws, return_said, job_id=job_id)
        await run.fence("deliver builder outcome")
        return False, TOO_FINE, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=TOO_FINE)
    # A MESH IS WORTH HAVING EVEN WHEN THE PORTS CANNOT BE NAMED, and refusing to build one was
    # costing whole parts. The ports are the block faces the coolant reaches, which works while a
    # part's mouths lie ON those faces - eight of the nine in the package, because a channel milled
    # across a plate ends flush with the side it was cut on. A Corintis cold plate's mouths are
    # fitting BORES and its bounding box is set by the fittings' own bodies, so the coolant stops
    # 2 mm inside the block on every side, plan_ports says "it is sealed inside the metal", and the
    # whole job died on a question that is about SOLVING rather than about meshing.
    #
    # So the port plan is attempted and its failure is not fatal. What comes out is a real
    # two-region conjugate mesh with no named inlet or outlet, which is exactly what
    # `--no-ports` has always produced on the command line ("the mesh is then not solvable and is
    # still worth having"). The reason travels with the run rather than being swallowed, because a
    # case that cannot be solved must not look like one that can.
    ports_refused = ""
    try:
        plan, lo, hi = PL.plan_for(
            narrowest_m=narrowest, fluid_volume_m3=float(rec["volume_m3"]),
            passages=int(rec["passages"]), solid_lo=bounds[0], solid_hi=bounds[1],
            fluid_lo=[float(v) for v in rec["bbox_min_m"]],
            fluid_hi=[float(v) for v in rec["bbox_max_m"]],
            cells_across=cells_across, levels=levels,
            background_m=(two or {}).get("background_m"),
            fine_cell_pinned_m=pinned_cell_m, under_planner_floor=under_planner_floor)
    except ConjugateError as exc:
        # SAID LATER, NOT HERE. At this point nothing yet knows whether the ports will be CUT out
        # of a face - that is decided forty lines down, once the mouths have been read - so a line
        # here claiming the mesh has no named ports is a line written before the answer exists.
        # Measured on the coupon plate, 2026-10-29: this logged "meshing without named ports" and
        # the mouths step then logged "coolant_in and coolant_out are cut out of zmax, 0.903 mm and
        # 0.903 mm across" four lines later. The reporting block at the end of this function is
        # where `ports_refused` is turned into words, and it knows.
        ports_refused = str(exc)
        plan, lo, hi = PL.plan_for(
            narrowest_m=narrowest, fluid_volume_m3=float(rec["volume_m3"]),
            passages=int(rec["passages"]), solid_lo=bounds[0], solid_hi=bounds[1],
            fluid_lo=[float(v) for v in rec["bbox_min_m"]],
            fluid_hi=[float(v) for v in rec["bbox_max_m"]],
            cells_across=cells_across, levels=levels, no_ports=True,
            background_m=(two or {}).get("background_m"),
            fine_cell_pinned_m=pinned_cell_m, under_planner_floor=under_planner_floor)
    # WHERE IT RUNS (RUN-1, contracts/mesh_machine). Six engines ask for a bigger machine when a
    # mesh will not fit the Cloud Run job's 32 GiB; this one did not, so every conjugate mesh
    # landed on Cloud Run whatever it needed and a part that wanted more was simply refused for
    # being too big. The memory is now priced from the estimate with the per-cell figure measured
    # on three builds, and the decision travels in the workspace for the executor to read.
    from meshpipeline.contracts import mesh_machine as MM

    machine = MM.choose(process_gib=B.peak_gib(estimate), workspace_gib=B.written_gib(estimate),
                        why="conjugate cell estimate")
    MM.write(ws, machine)
    if machine.get("where") == "gce":
        logger.info("conjugate: %s - job_id=%s", machine.get("words"), job_id)

    # A MOUTH THAT STOPS INSIDE THE METAL IS STILL A PORT. The conjugate case takes its inlet and
    # outlet from the block's six named faces, which works while a part's mouths lie ON those
    # faces - eight of the nine parts in the package, because a channel milled across a plate
    # ends flush with the side it was cut on. A Corintis cold plate's mouths are fitting BORES and
    # the block is sized by the fittings' own bodies, so the coolant stops inside the metal on
    # every side and the whole job died on a question about SOLVING rather than about meshing.
    #
    # `mouths.py` has been in this package, written and tested against that exact part, imported
    # by nothing but its own unit test. Two steps make it real: a stub out to the face so the
    # coolant reaches one, and a patch CUT where it crosses rather than a renamed face - because
    # both of that plate's fittings point +x, so both stubs land on xmax and a face carries one
    # name. See conjugate.render_port_patch_dicts.
    port_patches = None
    ports_said = ""
    if rec.get("ports"):
        from meshpipeline.engines.cfmesh_conjugate import mouths as MO

        # ON A SHELL THERE IS NOTHING TO STUB OUT TO. The mouths are already on the domain's own
        # surface, so each is a patch cut where it is - `mouths.shell_stubs`, written against this
        # exact assembly and until now called by nothing. On a box the mouths are inside it and
        # have to reach a face first, which is what `stubs_for` does.
        stubs, mouth_said = ((MO.shell_stubs(rec["ports"]), [])
                             if shell else MO.stubs_for(rec["ports"], lo, hi))
        for line in mouth_said:
            logger.info("conjugate: %s - job_id=%s", line, job_id)
        if stubs:
            from meshpipeline.cad.stl_io import read_stl_triangles, write_stl_binary

            fluid_stl = ws / PL.FLUID_STL
            grown, added = MO.extend(read_stl_triangles(fluid_stl), stubs)
            write_stl_binary(fluid_stl, grown)
            # THE NAMES THE USER APPROVED, bound to the mouths by position. Without this the
            # cut ports carry this module's own defaults and the patch contract refuses the
            # finished mesh: the coupon plate meshed, came home, and failed with "The mesh has
            # no boundary called 'inlet' or 'outlet' (it has 'coolant_in' and 'coolant_out') ...
            # so the mesh does not match what you approved" (2026-10-29). The mesh was fine.
            from meshpipeline.engines.workspace_facts import port_declaration

            _declared = [d for d in port_declaration(ws)
                         if isinstance(d, dict) and d.get("name")]
            _named, _named_why = MO.declared_for(stubs, _declared)
            if _named_why:
                logger.info("conjugate: %s - job_id=%s", _named_why, job_id)
            port_patches, ports_said = MO.port_rows(stubs, named=_named)
            logger.info("conjugate: %d stub(s) added %.4f mm3 to the coolant; %s - job_id=%s",
                        len(stubs), added * 1e9, ports_said, job_id)
            # the coolant is a different shape now, so what the split is checked against moves
            plan = dataclasses.replace(
                plan, expect_fluid_volume_m3=float(plan.expect_fluid_volume_m3 or 0.0) + added)

    names, named_said = declared_port_names(ws, plan)
    if names:
        plan = dataclasses.replace(plan, patch_names=PL._as_lists(names))
    if fine_surface is not None:
        plan = dataclasses.replace(plan, fine_surfaces=(fine_surface,))
    if stretch_zone is not None:
        plan = dataclasses.replace(plan, stretch_zones=(stretch_zone,))
    if port_patches:
        plan = dataclasses.replace(plan, port_patches=tuple(port_patches))

    # CAN A DROP OF COOLANT GET FROM ONE PORT TO THE OTHER? Read here, on the surface about to be
    # meshed and after the stubs have grown it, because this is the last moment it costs
    # milliseconds instead of hours. The coolant's piece count is already checked after the mesh,
    # against `expect_fluid_pieces` - but that expectation is a count of THIS SAME SURFACE's
    # pieces, so a coolant already severed meets it and passes. Connectivity between the two named
    # ports is the question that cannot be satisfied by agreeing with the defect.
    #
    # MEASURED, job b6762d5c, 2026-11-13: ColdPlateAssembly_CLEAN.step was meshed to 326,631,400
    # cells over 2 h 29 m on a 64-core machine before anything noticed that its inlet and its
    # outlet are on pieces 0.5 mm of metal apart.
    from meshpipeline.cad.fluid_passages import ports_on_one_passage
    from meshpipeline.cad.stl_io import read_stl_triangles

    try:
        severed = ports_on_one_passage(read_stl_triangles(ws / PL.FLUID_STL),
                                       [dict(p) for p in (plan.port_patches or ())])
    except Exception:  # noqa: BLE001 - a reading that cannot be taken is not a refusal
        logger.warning("conjugate: the coolant's connectivity could not be read - job_id=%s",
                       job_id, exc_info=True)
        severed = ""
    if severed:
        logger.info("conjugate: refusing to start - %s - job_id=%s", severed, job_id)
        # NOT PRICED OUT. Nothing about a budget, a resolution or a machine is in this, and the
        # four size refusals' sentences would all be false here - see `_say_why_before_meshing`.
        _say_why_before_meshing(ws, severed, job_id=job_id, priced_out=False, geometry=True)
        await run.fence("deliver builder outcome")
        return False, SEVERED_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                                   failure_marker=SEVERED_COOLANT)

    from meshpipeline.engines.cfmesh_conjugate import conjugate as C
    if shell:
        # the domain STL IS the surface cfMesh fills, under the one name a shell has
        (ws / PL.BLOCK_STL).write_bytes((ws / PL.DOMAIN_STL).read_bytes())
    else:
        (ws / PL.BLOCK_STL).write_text(C.block_surface_stl(lo, hi))
    # HOW MANY PROCESSES THE MESHER ACTUALLY GETS, which is NOT the count the gates were given.
    #
    # `ranks` above is a ceiling - the most this deployment will allow. This is the demand: what
    # the mesh's own size requires to stay under the per-process label limit. `ranks_for` returns
    # 1 for anything at or below OCTREE_BOXES_PER_PROCESS, so every part in the corpus and every
    # mesh already delivered keeps being built on one process. That is the point of doing it here
    # rather than passing the ceiling straight through: parallel CHANGES the mesh (measured on the
    # coupon - one cell moves between the regions, because topoSet classifies a cell on a
    # processor boundary differently), so a part that never needed a second process must not get
    # one and have its measured gates quietly mean something else.
    #
    # PRICED ON `raw`, not on the estimate. The estimate carries ESTIMATE_MARGIN to keep a run
    # inside a budget; the label ceiling is a hard limit on the cells that will exist, so sizing
    # the decomposition from the padded figure would ask for a rank the mesh does not need.
    #
    # AND THE STRETCH IS A SECOND DEMAND on the same number, measured rather than reasoned:
    # cfMesh rebuilds its octree after the back-scale, and that rebuild segfaults on two
    # processes. See conjugate.BACKSCALE_PROJECTION_RANKS for the two runs that proved it and the
    # three that did not hit it.
    _raw = int((two or {}).get("raw") or 0) if two else 0
    _cells = _raw or estimate
    _stretched = bool(plan.stretch_zones)
    _by_ceiling = C.ranks_for(_cells, available=ranks)
    _want = C.ranks_for(_cells, available=ranks, back_scaled=_stretched)
    if _want > 1 and _want > _by_ceiling:
        why = ("the stretch needs it: the octree cfMesh rebuilds after the back-scale has "
               f"only ever survived on {C.BACKSCALE_PROJECTION_RANKS} processes, and "
               f"segfaulted on {_by_ceiling}")
    elif _want > 1:
        why = (f"the mesh needs it ({_cells:,} cells against "
               f"{C.OCTREE_BOXES_PER_PROCESS:,} a process)")
    else:
        why = "the mesh fits in one"
    # CARRIED, not re-derived in the container: the two demands above are both the host's
    # arithmetic, and the stamp used to pick between them by guessing from the count.
    plan = dataclasses.replace(plan, ranks=_want, ranks_basis=why, sizing_basis=said)
    if plan.ranks > 1:
        logger.info("conjugate: meshing across %d processes - %s; %s - job_id=%s",
                    plan.ranks, ranks_basis, why, job_id)
    PL.write_plan(ws, plan)
    run.note_authoring()
    note_native_pass(ws, 1)
    # THE THIN SURFACE TRAVELS TOO, and a refinement whose surface the container cannot open is
    # one that silently does not happen - which is exactly how a zoned run of this part once
    # rebuilt the unzoned mesh and reported success.
    payload = [PL.PLAN_NAME, PL.BLOCK_STL, PL.FLUID_STL]
    if fine_surface is not None:
        payload.append(PL.FLUID_FINE_STL)
    note_native_payload(ws, payload)
    # ...AND THE REFUSAL IS NOT SAID ONCE THE PORTS WERE CUT. `plan_ports` refuses a part whose
    # mouths reach no opposite pair of block faces, which is true of the BLOCK and says nothing
    # about the plumbing - and the cutter exists for exactly that part. Measured on the coupon
    # plate, 2026-10-29: the run logged "coolant_in and coolant_out are cut out of zmax, 0.903 mm
    # and 0.903 mm across" and then, three lines later, "the mesh has no named inlet or outlet".
    # Both from the same run, and the second one false. A reader of that pair concludes the mesh
    # is unsolvable when it is the one shape this engine learned to solve.
    unnamed = "" if port_patches else ports_refused
    # `ports_said` is NOT here: the stub block above already logged it beside what the stubs
    # added, which is where it belongs and where a reader needs it.
    for line in (said, named_said, unnamed and
                 f"meshing without named ports, and the mesh has no named inlet or outlet: "
                 f"{unnamed}"):
        if line:
            logger.info("conjugate: %s - job_id=%s", line, job_id)

    await run.fence("start native mesh")
    # THE MACHINE'S WALL IS A CEILING ON THE RUN'S OWN BUDGET - the half of
    # `contracts/native_time` this engine was still missing, taken through the shared loop
    # (`native_time.run_off_the_wall`) in the shape `engines/snapgrid/driver.py:90` already uses,
    # which is the other engine with a deterministic driver and so the other one `policy.watched`
    # never reaches (the flag is read only inside the model-led `run_mesh`). The loop hands the
    # wall to the case, re-runs a Cloud Run pass that met it STILL WORKING on a VM - the same
    # case, nothing coarsened (RUN-5) - and steps one machine up for a run that came back out of
    # memory (RUN-3). See `_run_off_the_wall`, which is where the ceiling is applied and why it
    # is a ceiling rather than a replacement.
    #
    # WHAT IT FIXES, measured 2026-11-12. `run_timeout()` is 21,600 s and
    # `native_time.CLOUD_RUN_WALL_S` is 13,800 (MESH_TIMEOUT_SECONDS 14,400 less the margin to
    # hand the result back), so the engine's own deadline was 1.57x the platform's. The clamp this
    # replaced - min(21600, OPENFOAM_COMMAND_TIMEOUT 1500 x 6) = 9,000 - happened to fit 13,800
    # and 21,600 does not, and `mesh_machine.choose` on this engine's own budget model routes
    # 60,000 cells (peak 0.45 GiB) and 5,000,000 cells (peak 3.89 GiB) to CLOUD RUN. So a small
    # conjugate case that ran long was killed by Cloud Run with no result document at all, and
    # reached the customer as GONE ("stopped answering part way through the mesh") rather than as
    # a mesh that needs a coarser fidelity with this policy's own `timeout_hint`.
    #
    # AND THE WAITING WORKER IS FINE, which is the premise this was declined on last time.
    # `cloud_run_client.wait_s = timeout + 600 + VM_START_S` is NOT a clock that gives up: the
    # module says so in its own words at WAIT_STALL_S - "THE WAIT ENDS WHEN THE REMOTE IS GONE,
    # NOT ON A CLOCK" - and `_poll_gcs_json` only reaches its deadline branch when `silent`, which
    # a remote pulsing its beacon every 60 s never is.
    result = await asyncio.to_thread(_run_off_the_wall, ws, job_id=job_id)
    # BEFORE the result is judged: a superseded generation must not accept it
    await run.fence("accept native mesh output")
    if refused_identical_rerun(result):
        # NOTHING WAS LAUNCHED. The seam refused this case because an earlier attempt of this run
        # already ran it to a verdict. Judging it below as `rc != 0` announced the engine's own
        # build failure for a mesher that never started - the marker that tells the next reader
        # "the mesher stopped before it finished" - so the stop says what it is instead.
        logger.error("conjugate: %s", result.get("guidance")
                     or "an earlier attempt of this run already ran this exact case")
        run.note_native_run(produced_usable_mesh=False)
        await run.fence("deliver builder outcome")
        return False, STOP_IDENTICAL_RERUN, run.outcome(
            produced_deliverable=False, failure_marker=STOP_IDENTICAL_RERUN)
    ok = result.get("rc") == 0
    run.note_native_run(produced_usable_mesh=ok)
    if not ok:
        logger.warning("conjugate: the mesher did not deliver - job_id=%s rc=%s: %s", job_id,
                       result.get("rc"), str(result.get("log_tail") or "")[-300:])
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok,
                          failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


def _run_off_the_wall(ws: Path, *, job_id: str = "") -> dict:
    """Run the conjugate mesher under the LOWER of the machine's wall and this engine's own
    budget, and re-run a Cloud Run pass that met that wall still working on a VM (RUN-5).

    `contracts/native_time.run_off_the_wall` is the shared loop every watched engine uses and
    `engines/snapgrid/driver.py:90` is the adoption this copies. The one thing added is `min`,
    and both halves of it are measured.

    THE WALL HAS TO BIND ON CLOUD RUN. `wall_for` is 13,800 s there and `run_timeout()` is
    21,600 s, so without this the engine's own deadline was 1.57x the platform's and a small
    conjugate case that ran long came back as GONE with no result document instead of as a mesh
    that needs a coarser fidelity.

    AND THE BUDGET HAS TO BIND ON A VM, which is where taking the wall outright would have done
    harm rather than good. `wall_for` is 72,000 s on a VM, and three things measured on
    2026-11-12 say that is not this engine's deadline:

      * the plate's own container half FITS 21,600 with 1,115 s spare - chain 9,359 + checkMesh
        2,776 + export 7,200 + surface 144 + index 1,006 = 20,485 s. The 39,588 s figure that
        looks as though it does not fit is that sum plus the 19,103 s RETURN, and the return is
        not in this budget: `gce_vm.lifetime_seconds` is this cap PLUS `return_margin_s`, which
        prices the return separately and already covers it;
      * the machine's life is derived from this cap, so 72,000 would make the plate's
        maxRunDuration 72,000 + 22,831 = 94,831 s (26.3 h) - past both limits of the worker task
        that is waiting to collect the result, 86,400 s soft and 90,000 s hard
        (`adapters/pipeline_execution/celery.py:44-45`). The worker would be killed before the VM
        was, which loses a finished mesh rather than saving one;
      * and it would re-price a VM route whose arithmetic is settled and confirmed at 44,431 s,
        for no measured need.

    So a VM keeps exactly the figures it has - min(72,000, 21,600) = 21,600 - and Cloud Run is
    brought inside the platform wall, which is the end that was broken. RUN-5 still buys more
    time than the pass that met the wall: 21,600 against 13,800.
    """
    from meshpipeline.contracts.native_time import run_off_the_wall
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.runtime import get_engine

    engine = get_engine("cfmesh_conjugate")
    policy = get_spec("cfmesh_conjugate").run_policy
    budget = int(policy.run_timeout()) if policy is not None else 3600
    return run_off_the_wall(
        ws, lambda wall: engine.run_cartesian_mesh(ws, timeout=min(int(wall), budget)),
        engine="cfmesh_conjugate", job_id=job_id)


def _finer(fidelity: str) -> str:
    """One step finer, or "" when there is no finer step left to try.

    A review that rejected the finest mesh this engine offers has nothing left to ask for, and
    rebuilding the same plan would hand it back the mesh it just rejected.
    """
    ladder = sorted(B.CELLS_ACROSS_BY_FIDELITY, key=lambda k: B.CELLS_ACROSS_BY_FIDELITY[k])
    try:
        return ladder[ladder.index((fidelity or "").strip().lower()) + 1]
    except (ValueError, IndexError):
        return "" if (fidelity or "").strip().lower() == ladder[-1] else ladder[-1]


__all__ = ["BUILD_FAILED", "NO_COOLANT", "NOT_METRES", "NO_SOURCE", "SEVERED_COOLANT",
           "TOO_FINE", "cad_scale",
           "coolant_sits_inside", "declared_port_names", "drive", "find_source", "skin_bounds"]
