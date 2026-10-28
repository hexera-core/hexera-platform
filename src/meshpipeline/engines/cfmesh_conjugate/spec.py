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


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    m = manifest or {}
    obs = [TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch")]
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
    TurfRow("n_solids", "==", 1, "home",
            "One solid with coolant passages through it: the coolant is the space inside the "
            "part, found from the part's own faces, and the metal is what is left.",
            flow="multi-region"),
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
            run_timeout=lambda: min(int(rtcfg.CONJUGATE_RUN_TIMEOUT_S),
                                    rtcfg.OPENFOAM_COMMAND_TIMEOUT * 6),
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
