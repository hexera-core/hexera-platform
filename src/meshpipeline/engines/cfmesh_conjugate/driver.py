# Responsibility: Build a conjugate mesh for one Builder attempt with no model in the loop: cut the coolant out of the part, choose the resolution, write the plan, run the mesher.
# Boundaries: deterministic; it authors nothing a model could get wrong. It publishes nothing itself - what the user reads comes through finalize's output and the manifest.
# Collaborates with: agents/builder/invoke.py (calls drive through spec.build_driver) and cfmesh_conjugate/plan.py.
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

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
BUILD_FAILED = "conjugate_build_failed"

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
    """What the CAD's own coordinates must be multiplied by to be metres.

    THE ONE SLIP THIS FILE COULD CARRY IS A UNIT, and it carried it: the extraction was called with
    scale=1.0 on a STEP that declares millimetres, so the manifold plate's coolant came back as
    4.56 m3 of fluid in a 100 mm channel. Every number downstream was off by a thousand - the cell
    size, the background cell, the cell estimate - and nothing in the chain had any way to notice,
    because every one of them was consistently wrong.

    The unit comes from the same reader the rest of the platform resolves units with
    (cad/unit_evidence.parser_applied_unit), never from the file's name or a default.
    """
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.geometry_units import scale_to_metres

    return float(scale_to_metres(parser_applied_unit(Path(source))))


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
            return (f"the coolant runs from {c_lo[k] * 1e3:.4g} to {c_hi[k] * 1e3:.4g} mm on "
                    f"axis {k} where the part runs {float(lo[k]) * 1e3:.4g} to "
                    f"{float(hi[k]) * 1e3:.4g} mm")
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
        STOP_REVIEWED_CASE_REPEATS,
        review_caused_retry,
    )
    from meshpipeline.cad.fluid_volume import surface_bounded
    from meshpipeline.engines.cfmesh_conjugate.conjugate import ConjugateError
    from meshpipeline.contracts.mesh_execution import note_native_pass, note_native_payload
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.runtime import get_engine

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

    # THE COOLANT, cut out of the part itself. This is the step that makes the case a conjugate
    # one: without it there is a solid and no fluid, and nothing to split the block along.
    fluid_stl = ws / PL.FLUID_STL
    fluid_stl.parent.mkdir(parents=True, exist_ok=True)
    rec = await asyncio.to_thread(surface_bounded, src, fluid_stl, scale=cad_scale(src),
                                  kind="cad")
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
        two, two_said = B.plan_two_scales(block_lo=bounds[0], block_hi=bounds[1], scales=scales,
                                          limit=limit, asked_across=asked,
                                          prismatic_axis=(None if prismatic is None
                                                          else int(prismatic)))
        if two is None:
            # REFUSED, and the reason is the part's own two scales rather than a cell count. Said
            # once, here, because the single-scale path would refuse the same part for a different
            # and less useful reason - its narrowest passage - and the user would be told the wrong
            # thing about their own geometry.
            logger.info("conjugate: refusing to start - %s - job_id=%s", two_said, job_id)
            await run.fence("deliver builder outcome")
            return False, NO_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                                  failure_marker=NO_COOLANT)
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
            await run.fence("deliver builder outcome")
            return False, NO_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                                  failure_marker=NO_COOLANT)
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
    octree_said = B.refuse_for_octree(estimate)
    if octree_said:
        logger.info("conjugate: refusing to start - %s - job_id=%s", octree_said, job_id)
        await run.fence("deliver builder outcome")
        return False, NO_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=NO_COOLANT)
    # ...AND WHETHER IT CAN COME BACK. The mesher runs off-box and its workspace returns as one
    # archive through a bounded extractor; a mesh too big for that archive is a mesh that builds
    # and is then lost at the last step, which is exactly what the coupon plate did. Same shape as
    # the two checks above: priced from the estimate, refused before anything runs, and the reason
    # names the setting rather than blaming the part.
    from meshpipeline.sandbox.safe_extract import default_limits as _extract_limits

    return_said = B.refuse_for_return(estimate,
                                      cap_bytes=_extract_limits().max_archive_bytes)
    if return_said:
        logger.info("conjugate: refusing to start - %s - job_id=%s", return_said, job_id)
        await run.fence("deliver builder outcome")
        return False, NO_COOLANT, run.outcome(produced_deliverable=False, exhausted=True,
                                              failure_marker=NO_COOLANT)
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
            cells_across=cells_across, levels=levels)
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
            cells_across=cells_across, levels=levels, no_ports=True)
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

        stubs, mouth_said = MO.stubs_for(rec["ports"], lo, hi)
        for line in mouth_said:
            logger.info("conjugate: %s - job_id=%s", line, job_id)
        if stubs:
            from meshpipeline.cad.stl_io import read_stl_triangles, write_stl_binary

            fluid_stl = ws / PL.FLUID_STL
            grown, added = MO.extend(read_stl_triangles(fluid_stl), stubs)
            write_stl_binary(fluid_stl, grown)
            port_patches, ports_said = MO.port_rows(stubs)
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

    from meshpipeline.engines.cfmesh_conjugate import conjugate as C
    (ws / PL.BLOCK_STL).write_text(C.block_surface_stl(lo, hi))
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
    policy = get_spec("cfmesh_conjugate").run_policy
    cap = int(policy.run_timeout()) if policy is not None else 3600
    result = await asyncio.to_thread(
        get_engine("cfmesh_conjugate").run_cartesian_mesh, ws, timeout=cap)
    # BEFORE the result is judged: a superseded generation must not accept it
    await run.fence("accept native mesh output")
    ok = result.get("rc") == 0
    run.note_native_run(produced_usable_mesh=ok)
    if not ok:
        logger.warning("conjugate: the mesher did not deliver - job_id=%s rc=%s: %s", job_id,
                       result.get("rc"), str(result.get("log_tail") or "")[-300:])
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=not ok,
                          failure_marker="" if ok else BUILD_FAILED)
    return outcome.produced_deliverable, (outcome.terminal_value or BUILD_FAILED), outcome


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


__all__ = ["BUILD_FAILED", "NO_COOLANT", "NO_SOURCE", "cad_scale", "coolant_sits_inside",
           "declared_port_names", "drive", "find_source", "skin_bounds"]
