# Responsibility: Declare the conjugate mesher: capabilities, input, deliverable, gates and review needs.
# Boundaries: the single declaration the pipeline reads for this engine.
# Collaborates with: engines/base.py for the shape, and the modules in this package that fill each hook in.
from __future__ import annotations

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.base import (
    Deliverable,
    DeliveredMesh,
    DownstreamTarget,
    EngineSpec,
    FlowSupport,
    InputContract,
    MeshCapability,
    RunPolicy,
    TurfRow,
)
from meshpipeline.engines.base import (
    DeliverableMember as M,
)
from meshpipeline.engines.base import (
    ValidationAxis as _VA,
)
from meshpipeline.engines.base import (
    ValidationCoverage as _VC,
)
from meshpipeline.engines.cfmesh_conjugate._shared import (
    _INSPECTION_TARGETS,
    _RENDER_ARTIFACTS,
)
from meshpipeline.engines.cfmesh_conjugate.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.cfmesh_conjugate.pack import CONJUGATE_SYSTEM
    return CONJUGATE_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.cfmesh_conjugate.pack import CONJUGATE_TOOL_NAMES
    return CONJUGATE_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.cfmesh_conjugate.gates import CONJUGATE_GATES
    return CONJUGATE_GATES


def _review_rubric():
    from meshpipeline.engines.cfmesh_conjugate.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.cfmesh_conjugate.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.cfmesh_conjugate.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _review_renderer():
    from meshpipeline.engines.cfmesh_conjugate.review_renderer import RENDERER
    return RENDERER


def _viewer_surface():
    from meshpipeline.engines.cfmesh_conjugate.viewer import conjugate_viewer
    return conjugate_viewer


def _build_driver():
    from meshpipeline.engines.cfmesh_conjugate.driver import drive
    return drive


#: What the renderer draws for this engine, and therefore what a PATCH obligation can name. The
#: conjugate surface is ONE physical group per REGION (cfmesh_conjugate/review_surface) rather
#: than one per boundary patch, so the renderable entities are the region names themselves - the
#: coolant and the metal - and `patch:<region>` is the id a toggle of one of them covers.
def _region_names(manifest) -> list[str]:
    """The regions the DELIVERED mesh has, off checkMesh's own per-region rows.

    Trusted job data, as every obligation resolver must be: the export is the thing that fails,
    so reading the regions off the quality block (measured on constant/<region>/polyMesh) keeps
    the obligation independent of whatever the review surface managed to carry.
    """
    m = manifest or {}
    rows = (m.get("quality") or {}).get("regions") or []
    names = [str(r.get("name")).strip() for r in rows
             if isinstance(r, dict) and str(r.get("name") or "").strip()]
    return sorted(dict.fromkeys(names))


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import (
        PATCH_INSPECTION_FLOOR,
        TargetObligation,
    )
    m = manifest or {}

    # ONE PATCH IS NOT EVERY PATCH, AND ON A TWO-BODY MESH IT IS HALF THE MESH.
    # This was `TargetObligation(kind=PATCH)` - kind-only, min_count falling through to 1 - under
    # a provenance string reading "every renderable boundary patch". One inspection of one entity
    # discharged it. Measured on job 575f76e6: the review surface carried the solid alone
    # ("patches: ['solid']"), the reviewer toggled it, the obligation was satisfied, and the
    # coolant boundary being absent from the file went unremarked all the way to
    # verdict=passed concerns=[].
    #
    # EXACT IDS, ONE PER REGION, because this engine's renderable entities ARE its regions and
    # there are exactly two of them. The cost is two toggles out of a review's view budget, and
    # the names come from the delivered mesh rather than from discovery, so a surface that lost
    # a region cannot satisfy the obligation by not offering it: discovery comes up short and
    # eligibility.missing_target_obligations refuses before the model is asked.
    regions = _region_names(m)
    if regions:
        # inspect_at_least is the shared floor (TargetObligation.PATCH_INSPECTION_FLOOR), which
        # on two regions is two: this engine is already under it and nothing about it changes.
        # Passed rather than left at 0 so one rule covers every engine and the conjugate case is
        # not a second, quieter rule that happens to agree.
        obs = [TargetObligation(
            kind=TargetKind.PATCH, exact_ids=tuple(f"patch:{r}" for r in regions),
            inspect_at_least=PATCH_INSPECTION_FLOOR,
            provenance="each delivered region's own boundary - the coolant's and the metal's")]
    else:
        # No measured region rows: a manifest this engine did not write, or a fixture. Fall back
        # to the kind-only floor rather than inventing names, and say that is what happened.
        obs = [TargetObligation(kind=TargetKind.PATCH,
                                provenance="a renderable boundary (no per-region rows in the "
                                           "manifest to name)")]

    names = [r.get("name") for r in (m.get("inspection_regions") or [])
             if isinstance(r, dict) and r.get("name")]
    if names:
        obs.append(TargetObligation(
            kind=TargetKind.REGION, exact_ids=tuple(f"region:{n}" for n in names),
            provenance="the cuts a cooled solid is read through"))
    else:
        obs.append(TargetObligation(kind=TargetKind.REGION, min_count=1,
                                    provenance="the coolant and the metal around it"))
    return tuple(obs)


# WHERE THIS ENGINE IS AT HOME. A part whose coolant is the space found INSIDE it rather than a
# body in the file. That is the whole distinction from the other two multi-region engines, and it
# is the reason this one exists: cfMesh meshes a single connected domain and cannot mesh two, so
# the part is meshed once as solid metal and the one mesh is cut in two along faces that already
# existed.
#
# IT USED TO SAY "ONE SOLID", AND THAT COST IT THE FIRST REAL CUSTOMER PART IT WAS EVER SHOWN.
# Measured on a Corintis cold plate, 2026-10-27: six solids - lid, cooling circuit, I/O splitter,
# microfluidic core and two fittings - brazed into one piece with 52.14 ml of coolant that the
# split cut out of it cleanly, no leaks. On `n_solids >= 2 -> outside` this engine declared itself
# OUTSIDE ITS OWN TURF, and the snap grid, which is for electronics models, was recommended for a
# STEP cold plate instead. The engine's own confirmation wording had been right all along ("The
# part is N solids bonded into one piece, with coolant passages through it"); the row contradicted
# it. A brazed or diffusion-bonded plate is a base and a lid, and that is the commonest way one is
# made.
HOME_TURF = (
    TurfRow("coolant_inside", "is", True, "home",
            "The coolant is the space inside the part, cut out of the part's own faces, and the "
            "metal is what is left. One solid or several bonded into one piece: a brazed or "
            "diffusion-bonded cold plate is a base and a lid and is this engine's case.",
            flow="multi-region"),
    # THERE IS NO `n_solids == 1` HOME ROW ANY MORE, and for the same reason there is no `closed`
    # row below: it answered the coolant question off something that is not the coolant. The row
    # read "One solid with coolant passages through it", but one solid is all it tested - so it
    # claimed this engine's HOME TURF on any single-solid part in a multi-region case whether a
    # split had ever looked inside it or not, including the one part the engine's own advisory
    # says it cannot mesh: "A part with open-topped grooves and no lid has no enclosed coolant to
    # find - send it with its lid on." On such a plate the row claimed home while the driver
    # (driver.coolant_sits_inside) was going to refuse the run.
    #
    # NOTHING THE ROW WAS CARRYING IS LOST. Measured on the live ranker, 2026-11-12, a one-solid
    # cold plate with the check not run, after the intake's own format filter stops offering the
    # snap grid for a STEP file (agents/intake/measured_choice.able_engines):
    #
    #     1. cfmesh_conjugate [RECOMMENDED]   2. snappy_multiregion (outside its own turf)
    #
    # It leads on the other engine's declaration rather than on a claim of its own, which is the
    # honest order for a part nobody has measured. With the coolant measured, the row above holds
    # and says home on the fact the claim is actually about.
    TurfRow("coolant_inside", "is", False, "outside",
            "The fluid is a body in the file rather than the space inside the part, so the "
            "assembly already has one body per region - mesh it with multi-region snappyHexMesh, "
            "or with the snap grid for an electronics model.",
            flow="multi-region"),
    # THERE IS NO `closed` ROW, DELIBERATELY. There was one, outside: "a solid that is not closed
    # bounds no space: there is nothing to cut out." `closed` is read off the WELDED TESSELLATION
    # of the whole file, and `coolant_inside` is read off the faces themselves, so the two answer
    # the same question and one of them answers it properly. The Corintis plate came back
    # closed=False from two skipped null triangulations while the split cut 52.14 ml out of it with
    # no leaks at all, and almost any real assembly with fittings reads the same way - a penalty on
    # every honest part rather than a test that tells parts apart. Kept as `weak` it was no better:
    # weak outranks home in home_turf.verdict, so the engine stayed below the snap grid on the very
    # part it is for. What the row was guarding is covered: a split that ran and found nothing is
    # `coolant_inside is False` above, and a split that has not run leaves BOTH rows silent, which
    # is the neutral verdict it should be. The run-time refusals are the real guard either way
    # (driver.coolant_sits_inside, and the eight gates).
    TurfRow("cells_across_at_budget", "<", 4.0, "weak",
            "The cell budget puts fewer than 4 cells across the narrowest passage. Measured on "
            "eleven cold plates, the coolant's volume error at 2 and 3 cells across runs to 27% "
            "and 32%, which no claim about the coolant survives."),
)


SPEC = EngineSpec(
        name="cfmesh_conjugate",
        validation_notes=(
            "solid-assembly->multiregion-volume: cold_plate_2ch (one solid, 2.0 x 4.0 x 1.0 mm, "
            "two roofed 0.3 x 0.4 mm through-channels) delivered through the native-to-terminal "
            "tier on 2026-10-26, job 67161f7d: the coolant cut out of the CAD exactly (0.960000 "
            "mm3 against 0.960000), 41,616 cells in two regions (fluid 4,080 in 2 pieces for 2 "
            "channels, solid 37,536 in 1), interface 2,992 faces on both sides, non-ortho 2.94, "
            "skew 0.035, the meshed coolant 9.95% from the CAD where 5 cells across allows "
            "19.89%; all eight declared gates passed, all three required artifacts delivered, "
            "and the bundle's constant/regionProperties byte-matched the mesher's own output. "
            "The tier drives the real engine, the real executor gates and the production terminal "
            "chain against real Postgres, Redis and MinIO, with the graph stubbed and the "
            "reviewer a deterministic PASS - so intake and the visual review are not yet "
            "evidenced for this engine. The whole six-engine matrix passes alongside it."),
        descriptor=(
            "Conjugate (coolant + metal) meshing for ONE solid with coolant passages through it: "
            "a cold plate, a water block, a cooled package, a heat-exchanger core. The coolant is "
            "not a body in the file - it is the space inside the part, and this engine finds it, "
            "measures it and meshes it. cfMesh meshes one connected domain and cannot mesh two, "
            "so nothing here is meshed twice: the whole part is meshed once as solid metal "
            "including the space the coolant occupies, the cells inside the coolant are tagged "
            "from the coolant's own surface, and splitMeshRegions cuts the one mesh into two "
            "regions along faces that already existed. The interface is conformal because it was "
            "never built - it was always there. The cells are sized from the MEASURED narrowest "
            "passage, and the meshed coolant is compared against the CAD's own coolant by volume "
            "and by how many connected pieces it is. PRODUCES a coupled OpenFOAM case: fluid and "
            "solid, each its own polyMesh, with regionProperties and a mapped interface."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        # THE ONE LEGAL INTERIM STATE, and this engine is in it: the capability it will register
        # is declared, and it is not registered until a job has delivered it end to end
        # (engines/validation_evidence.json; tests/unit/engines/test_two_state_contract.py
        # enforces both halves). `capabilities=()` would be the other way to write this and is
        # not legal - an engine that declares nothing cannot be reasoned about.
        implemented=True,
        export_formats=("openfoam_polymesh_multiregion",),
        intake_guidance=(
            "Confirm the run is a conjugate (cooled-solid) mesh of ONE part whose passages carry "
            "the coolant, and ask what to call the inlet and the outlet. Which faces of the part "
            "those are is measured from the passage itself - do not ask. The coolant's volume, "
            "how many connected passages it is and its narrowest width are all measured too, so "
            "there is nothing for the user to declare about the coolant."
        ),
        intake_advisories=(
            "The coolant is found inside the part: the part must be a closed solid, and its "
            "passages must reach the outside at two places or more. A part with open-topped "
            "grooves and no lid has no enclosed coolant to find - send it with its lid on.",
            "The cells are sized from the narrowest passage. How many go across it is reported "
            "with the mesh, together with how far the meshed coolant came out from the CAD's own "
            "coolant - that error is what the delivery is judged on, not the cell count.",
            "The two regions are always called fluid and solid, and the surface between them is "
            "written by the split rather than authored.",
        ),
        capabilities=(MeshCapability("solid-assembly", "multiregion-volume"),),
        # WHAT FILE IT TAKES: the solid itself, as CAD. A triangle file has no faces to ask which
        # of them bound the space inside the part, so the coolant cannot be cut out of one.
        accepts=(FlowSupport("multi-region", ("cad",)),),
        # after the two assembly meshers on the multi-region ladder: it is the specialist for the
        # one-solid case they are both outside of
        ladder_rank=65,
        input_contract=InputContract(
            dimensionalities=("3D",),
            input_kind="solid",
            min_thickness_ratio=0.0,
            rationale="A cooled solid is a 3D part whose passages are deliberately much smaller "
                      "than it is; the cells are sized from the narrowest of them, so thinness "
                      "relative to the part is the normal case rather than a limit.",
        ),
        # EMPTY, like every engine but the snap grid. This field is not "what geometry I
        # need" - it is "I read the UPLOADED FILE ITSELF rather than the canonical STEP or
        # STL the staging writes", which only the ECXML mesher does. Declaring the STEP
        # family here changed the admitted geometry form for every STEP upload in the
        # product, because capability._source_read_form asks whether ANY engine claims the
        # format. What this engine needs - a B-rep and not a triangle file - is already
        # declared on the right axis, by accepts=(FlowSupport("multi-region", ("cad",)),).
        supports_multiple_wall_patches=True,
        # the block's six faces carry the names the user gave the ports; the two region names are
        # the engine's own, and so is the interface
        patch_names_from_source=False,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "checkMesh on both regions (fatal topology)",
                "both regions are cut from one mesh whose cells and faces checkMesh reads; a "
                "fatal defect in either blocks delivery through the manifest_valid gate"),
            _VC(_VA.QUALITY, "non-orthogonality and skewness per region, interface face match",
                "the cells are cut-cell hexes, so skew appears only where a passage wall falls "
                "across a cell; both sides of the interface must carry the same faces"),
            _VC(_VA.SOLVABILITY, "two valid polyMeshes and a mapped interface between them",
                "a coupled case is solvable when each region is itself a valid mesh and the "
                "interface pairs one-to-one; enforced by manifest_valid, regions_split and "
                "interface, then coupled by chtMultiRegionFoam"),
            _VC(_VA.CONFORMANCE, "the meshed coolant against the CAD's coolant: volume and pieces",
                "the mesh is cut out by tagging cells, so the one way it can be wrong without "
                "being broken is tagging the wrong ones. Measured both ways - the volume and how "
                "many connected pieces the coolant is - by the coolant_volume and "
                "coolant_pieces gates"),
        ),
        review_rationale=(
            "visual: what this engine adds is WHICH CELLS ARE THE COOLANT, and a mesh that tagged "
            "the wrong ones is a perfectly valid mesh of a part nobody sent. The volume gate "
            "catches a wrong amount; only looking catches the wrong shape - a channel stopping "
            "short, a slot that joins two levels missing, a web between channels meshed away. "
            "The reviewer also weighs the per-region checkMesh numbers and the resolution used."
        ),
        _load_gates=_gates,
        _load_review_rubric=_review_rubric,
        _load_criteria=_criteria,
        _load_workspace_scaffold=_workspace_scaffold,
        _load_review_renderer=_review_renderer,
        _load_build_driver=_build_driver,
        _load_viewer_surface=_viewer_surface,
        render_artifacts=_RENDER_ARTIFACTS,
        inspection_targets=_INSPECTION_TARGETS,
        # THE INTERIOR IS THE PRODUCT HERE, not a view of it. A conformal two-region split and
        # one merged block agree on every number the gates print - cells, faces, non-ortho, skew
        # - and differ only in a cut through the coolant. So a run that published no volume has
        # produced nothing this review can judge, and base.expected_target_obligations must not
        # waive the region obligation off an absent `mesh_paths.volume`: the review says the
        # evidence is missing instead of passing a mesh it never looked inside.
        review_requires_the_interior=True,
        _load_target_obligations=lambda: _target_obligations,
        home_turf=HOME_TURF,
        briefing=BRIEFING,
        deliverable=Deliverable(
            marker="constant/regionProperties",
            bundle="openfoam_conjugate_case.tar.gz", prefix="openfoam_conjugate_case",
            label="OpenFOAM case (conjugate: coolant + metal)",
            # constant/ carries both regions' polyMesh and regionProperties, system/ the
            # dictionaries every stage read; the report and the plan ship beside them so the
            # delivered case says what it was meshed from and at what resolution
            members=(
                M("constant", kind="dir"),
                M("system", kind="dir"),
                M("conjugate_report.json"),
                M("conjugate_plan.json"),
                M("mesh.msh", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            solvers=("chtMultiRegionFoam (conjugate heat transfer)",),
        ),
        # cut-cell hexes: the octree's own cells everywhere except where a passage wall crosses
        # one, and those are cut to the wall. No prism layers.
        delivered_mesh=DeliveredMesh(cells="hex-dominant", walls="body-fitted",
                                     prism_layers=False),
        run_policy=RunPolicy(
            required_files=("conjugate_plan.json", "constant/triSurface/block.stl",
                            "constant/triSurface/fluid.stl"),
            # mesh, tag, split, check both regions, export: five OpenFOAM runs in a row
            # AN HOUR WAS THE SMALL-PART ANSWER. Every mesh this engine had built took under two
            # minutes, so a ceiling of one hour was six times the worst case and never bound. The
            # measured cost of a fill-the-box build on a 125 GB machine is about an hour and a half
            # of mesher alone, before topoSet walks every cell centre and splitMeshRegions writes
            # two full meshes - and >85% of a large build is that serial tail, not the mesher. A
            # ceiling that cuts a run after it has paid for the expensive part is the worst place
            # to put one.
            #
            # THIS IS THE WHOLE RUN'S BUDGET, AND IT IS THE ONLY STATEMENT OF IT THERE IS. The
            # same number is spent twice, by two things that read it differently, and until
            # 2026-11-12 they disagreed:
            #
            #   * `conjugate._foam` applied it PER STAGE, and the parallel chain is eight stages
            #     (preparePar, cartesianMesh, topoSet, splitMeshRegions, reconstructParMesh,
            #     regionDicts, topoSetPorts, createPatch) before two checkMesh runs and an export;
            #   * `adapters/mesh_execution/gce_vm` spends it ONCE, as the machine's whole life:
            #     maxRunDuration = timeout + the return's own priced margin (return_margin_s).
            #
            # THE CLAMP WAS THE DEFECT. `min(CONJUGATE_RUN_TIMEOUT_S 21600,
            # OPENFOAM_COMMAND_TIMEOUT 1500 x 6)` came out 9,000 s - so one stage's allowance was
            # 71% of the 12,600 s the VM was given to live, and foamToVTK's own EXPORT_TIMEOUT_S
            # (7,200 s, foam_exec.py) was another 57% of it on top. The VM would have deleted
            # itself holding a finished mesh. For scale, the comparable hand-run chain measured
            # 9,359 s at 413 M cells and 10,796 s at 506 M BEFORE any review aid ran.
            #
            # OPENFOAM_COMMAND_TIMEOUT IS A PER-COMMAND FIGURE AND THIS IS NOT ONE COMMAND, which
            # is the whole reason the clamp was wrong rather than merely tight: multiplying it by
            # the five-run serial chain of the era it was written in described a shape this engine
            # no longer has. The setting beside it already says what it is for in its own words
            # (settings/inventory: "how long ONE conjugate build may take, the mesher plus the
            # serial tail after it"), so the setting is believed and the clamp is gone. What makes
            # that safe is that the budget is now spent as a DEADLINE rather than handed out per
            # stage (conjugate.build, native._run_conjugate_local), so no chain can exceed it and
            # the machine's life derived from it is honest.
            #
            # WHY `watched=True` IS STILL NOT DECLARED HERE, THOUGH THE WALL IS NOW HONOURED -
            # and the previous version of this note was wrong about both halves, so it is worth
            # being exact. `contracts/native_time` is the adopted house rule - "NO GUESSED TIME
            # LIMITS ... stopped only when it has made NO progress for STALL_WINDOW_S, or at the
            # hard wall of the machine it runs on" - and snapgrid, snappy, snappy_multiregion,
            # cfmesh, gmsh and vmtk all declare the flag.
            #
            # THE FLAG IS UNREADABLE FROM WHERE THIS ENGINE RUNS, and that part was and is true.
            # The only two places that read it are `agents/builder/tools/meshing.py:348` (the cap
            # becomes the machine's wall) and `:486` (the RUN-5 re-run loop), both inside
            # `run_mesh`, the MODEL-LED tool. This engine has a deterministic driver
            # (`_load_build_driver` above), so `pipeline/executor.py:179` and
            # `agents/builder/invoke.py:142` route it to `driver.drive`, which never sees
            # `policy.watched`. Declaring it would change nothing on this route.
            #
            # SO THE DRIVER ADOPTS THE WALL ITSELF, the way `engines/snapgrid/driver.py:90` does:
            # `driver._run_off_the_wall` runs the case through `native_time.run_off_the_wall` with
            # min(`wall_for(workspace)`, this budget) as its cap. That is the RUN-5 loop and the
            # machine's wall, reached without the flag.
            #
            # AND THIS BUDGET IS STILL THE THING THAT BINDS ON A VM, deliberately: `wall_for` is
            # 72,000 s there, the plate's whole container half measures 20,485 s, and a cap of
            # 72,000 would put the machine's own life at 94,831 s - past the worker task's 86,400 s
            # soft limit (`adapters/pipeline_execution/celery.py:44`). `driver._run_off_the_wall`
            # carries that arithmetic. On Cloud Run the wall is 13,800 s and binds, which is the
            # end that was broken.
            #
            # WHAT THE OLD NOTE GOT WRONG, recorded because it was the stated reason for declining
            # this: it said `cloud_run_client.py:683` sizes the waiting worker's patience from this
            # number, "so a container that took VM_WALL_S for its own deadline would be meshing
            # hours after the worker stopped listening". `wait_s` is not a clock that gives up. The
            # module says so in its own words at WAIT_STALL_S - "THE WAIT ENDS WHEN THE REMOTE IS
            # GONE, NOT ON A CLOCK" - and `_poll_gcs_json` reaches its deadline branch only when
            # `silent`, which a remote pulsing its beacon every 60 s never is.
            #
            # THE OTHER HALF OF THE RULE IS INSIDE THE STAGES. `conjugate._foam` runs every stage
            # through the same `sandbox/safe_exec.run_guarded(stall_s=native_time.STALL_WINDOW_S)`
            # the six use, so a stage is told apart as stuck, out of memory or still working, and
            # a hung stage gives the rest of the chain its time back instead of sitting on the
            # budget.
            run_timeout=lambda: int(rtcfg.CONJUGATE_RUN_TIMEOUT_S),
            timeout_hint="Ask for a coarser mesh fidelity (draft) and run again.",
            ok_guidance="Both regions are meshed and checked, and the coolant matches the CAD - "
                        "call submit_mesh.",
            fail_label="conjugate meshing failed",
            fail_hint=("conjugate_report.json says why: no enclosed coolant in the part, a "
                       "coolant surface that does not close, more than two regions out of the "
                       "split, or a checkMesh problem in one of them."),
            submit_marker="constant/regionProperties",
            submit_ok_key="regions",
            submit_hint="No conjugate case built yet - run_mesh builds it.",
        ),
)
