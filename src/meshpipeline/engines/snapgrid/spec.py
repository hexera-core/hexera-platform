# Responsibility: Declare the snap-grid (placed-parts) ECXML mesher: capabilities, input, deliverable, gates and review needs.
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
    InputContract,
    MeshCapability,
    RunPolicy,
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
from meshpipeline.engines.snapgrid._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.snapgrid.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.snapgrid.pack import SNAPGRID_SYSTEM
    return SNAPGRID_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.snapgrid.pack import SNAPGRID_TOOL_NAMES
    return SNAPGRID_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.snapgrid.gates import SNAPGRID_GATES
    return SNAPGRID_GATES


def _review_rubric():
    from meshpipeline.engines.snapgrid.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.snapgrid.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.snapgrid.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _review_renderer():
    from meshpipeline.engines.snapgrid.review_renderer import RENDERER
    return RENDERER


def _viewer_surface():
    from meshpipeline.engines.snapgrid.viewer import snapgrid_viewer
    return snapgrid_viewer


def _build_driver():
    from meshpipeline.engines.snapgrid.driver import drive
    return drive


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    m = manifest or {}
    obs = [TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch")]
    regions = m.get("inspection_regions") or []
    names = [r.get("name") for r in regions if isinstance(r, dict) and r.get("name")]
    if names:
        obs.append(TargetObligation(
            kind=TargetKind.REGION, exact_ids=tuple(f"region:{n}" for n in names),
            provenance="declared inspection slices"))
    else:
        obs.append(TargetObligation(kind=TargetKind.REGION, min_count=1,
                                    provenance="interfaces between placed parts"))
    return tuple(obs)


SPEC = EngineSpec(
        name="snapgrid",
        validation_notes=(
            "ecxml solid-assembly->multiregion-volume: the ECXML test set (Flotherm, Icepak, "
            "6SigmaET exports and generated boards of 10-2,000 parts, a 1U server) meshes with "
            "every box volume exact to round-off, every layer kept (25 um die attach: 2 cells), "
            "contacts as the file makes them, and checkMesh clean on every region (OpenFOAM 11 "
            "locally, v2412 on the lab VMs; 3.3M cells for 2,000 parts). Not yet run through a "
            "full pipeline job."),
        descriptor=(
            "Placed-parts multi-region meshing for ECXML electronics-cooling models (JEDEC "
            "JEP181: Flotherm, Icepak, 6SigmaET exports). The way those tools mesh: no part is "
            "glued or joined - each stays where the file puts it, a hexahedral grid is laid with "
            "every part plane on a grid plane (refined only near the parts), and every cell "
            "belongs to one part or air space, so the interfaces are conformal by construction. "
            "Thin layers (die attach, pads, copper) always keep cells through them; round parts "
            "are snapped onto their circles. PRODUCES a coupled multi-region OpenFOAM case (one "
            "region per part and per air space, mappedWall interfaces, regionProperties) plus "
            "the file's physics. Reads ECXML only - it meshes the file's own model."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        implemented=True,
        export_formats=("openfoam_polymesh_multiregion",),
        intake_guidance=(
            "An ECXML model already names its parts, materials, power and openings, so there is "
            "little to ask: confirm the run is a conjugate heat-transfer (electronics cooling) "
            "mesh, and how fine it should be (draft for a quick look, standard, or max). The "
            "region and patch names come from the file - do not ask the user to name them."
        ),
        intake_advisories=(
            "Round parts (cylinders, round vents) are snapped onto their circles; their side "
            "area is within about 1-2% of the file's and is reported per part.",
            "Every thin layer keeps at least one cell through it. When the cell budget cannot "
            "give a layer the cells asked for, the layer is listed in the build report, never "
            "merged away.",
            "It reads ECXML only: a STEP or STL export of the same board carries no parts, "
            "materials or power for it to place.",
        ),
        capabilities=(MeshCapability("solid-assembly", "multiregion-volume"),),
        input_contract=InputContract(
            dimensionalities=("3D",),
            input_kind="solid",
            min_thickness_ratio=0.0,
            rationale="An ECXML thermal model is a 3D assembly of axis-aligned parts the file "
                      "places itself; the snap grid resolves any thinness by putting grid planes "
                      "on both faces of every layer, so no thin-feature floor applies.",
        ),
        reads_source_formats=("ecxml",),
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "checkMesh on the whole mesh and on every region (fatal topology)",
                "every region is cut from one mesh whose cells and faces checkMesh reads; a fatal "
                "defect anywhere blocks delivery through the manifest_valid gate"),
            _VC(_VA.QUALITY, "skewness + non-orthogonality on every region, interface face match",
                "the grid is hexahedral; hanging-node blocks keep non-orthogonality under 65 "
                "degrees, and only faces snapped onto a round part can skew. Both sides of each "
                "interface must carry the face count the grid wrote"),
            _VC(_VA.SOLVABILITY, "every region a closed, valid polyMesh with mappedWall interfaces",
                "a coupled case is solvable when every region is itself a valid mesh and its "
                "interfaces pair one-to-one; enforced by manifest_valid, regions_split and "
                "interfaces, then coupled by the downstream multi-region solver"),
            _VC(_VA.CONFORMANCE, "the file's model in the mesh: volumes, objects, contacts, regions",
                "box volumes match the file to round-off, the cells fill the domain, every "
                "active object is placed once, and two parts share faces exactly where the file "
                "makes them touch - checked before the mesh is written (file_fidelity gate)"),
        ),
        review_rationale=(
            "visual: what this engine adds is the PLACEMENT - each part its own region where the "
            "file puts it, thin layers with cells through them, round parts on their circles, "
            "and interfaces only where parts touch. Those read from a sectioned render; the "
            "reviewer also weighs the per-region checkMesh numbers and the build report."
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
        briefing=BRIEFING,
        deliverable=Deliverable(
            marker="constant/regionProperties",
            bundle="openfoam_multiregion_case.tar.gz", prefix="openfoam_multiregion_case",
            label="OpenFOAM case (multi-region)",
            # constant/ carries every region's polyMesh and regionProperties, system/ every
            # region's fvSchemes/fvSolution; the report and the physics sidecar ship beside them
            members=(
                M("constant", kind="dir"),
                M("system", kind="dir"),
                M("snapgrid_report.json"),
                M("thermal_model.json"),
                M("source.ecxml"),
                M("snapgrid_viewer.npz", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            solvers=("chtMultiRegionFoam (conjugate heat transfer)",),
        ),
        # hexahedral cells with hanging nodes; every box face lies on a grid plane and every
        # round side is snapped onto its circle, so the walls fit the parts; no prism layers
        delivered_mesh=DeliveredMesh(cells="hex-dominant", walls="body-fitted", prism_layers=False),
        run_policy=RunPolicy(
            required_files=("source.ecxml",),
            # placing, gridding, writing every region and checking them; stays under the
            # service timeout
            run_timeout=lambda: min(3600, rtcfg.OPENFOAM_COMMAND_TIMEOUT * 6),
            timeout_hint="Choose a coarser mesh fidelity (draft) and run again.",
            ok_guidance="Every region is meshed and checked clean - call submit_mesh.",
            fail_label="snap-grid meshing failed",
            fail_hint=("The build report (snapgrid_report.json) and log.snapgrid say why: a file "
                       "that cannot be placed, a model whose layers need more cells than the "
                       "budget, or a checkMesh problem."),
            submit_marker="constant/regionProperties",
            submit_ok_key="regions",
            submit_hint="No snap-grid case built yet - run_mesh builds it.",
        ),
)
