# Responsibility: Declare what cfMesh is: its capabilities, input contract, deliverable, gates and review requirements.
# Boundaries: the single declaration the pipeline reads for this engine.
# Collaborates with: engines/base.py for the shape, and the modules in this package that fill each hook in.
from __future__ import annotations

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.review_evidence import (
    TargetKind,
)
from meshpipeline.engines.base import (
    Deliverable,
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
from meshpipeline.engines.cfmesh._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.cfmesh.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.cfmesh.pack import CFMESH_SYSTEM
    return CFMESH_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.cfmesh.pack import CFMESH_TOOL_NAMES
    return CFMESH_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.cfmesh.flow_gates import FLOW_GATES
    return FLOW_GATES


def _authoring_tool():
    from meshpipeline.engines.cfmesh.authoring import AUTHORING_TOOL
    return AUTHORING_TOOL


def _authoring_validate():
    from meshpipeline.engines.cfmesh.authoring import validate
    return validate


def _recommend():
    from meshpipeline.engines.cfmesh.authoring import recommend
    return recommend


def _review_rubric():
    from meshpipeline.engines.cfmesh.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.cfmesh.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _review_renderer():
    from meshpipeline.engines.cfmesh.review_renderer import RENDERER
    return RENDERER


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    obs = [TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch")]
    regions = (manifest or {}).get("inspection_regions") or []
    names = [r.get("name") for r in regions if isinstance(r, dict) and r.get("name")]
    if names:
        obs.append(TargetObligation(
            kind=TargetKind.REGION, exact_ids=tuple(f"region:{n}" for n in names),
            provenance="authored inspection regions"))
    return tuple(obs)


def _viewer_surface():
    from meshpipeline.engines.cfmesh.viewer import polymesh_viewer
    return polymesh_viewer


SPEC = EngineSpec(

        name="cfmesh",
        validation_notes=(
            "body-surface→fluid-volume INTERNAL: full-pipeline e2e on the real elbow90 STEP "
            "(delivered mesh, 2026-07-11, current code). EXTERNAL: full-pipeline e2e on the "
            "same geometry (2026-07-07; topology plumbing re-validated 2026-07-11). "
            "2D EXTERNAL (native cartesian2DMesh): full-pipeline e2e on the real "
            "NACA0012 STEP (delivered mesh, 2026-07-12, job c623c582 - boundary "
            "reconciles exactly: wall + farfield + merged empty planes). 2D internal "
            "profiles are REJECTED (outside the supported Cartesian envelope)."),
        descriptor=(
        "FLUID / CFD volume meshing. Fills a region with a Cartesian "
        "octree mesh (cfMesh cartesianMesh); the surface is STAIRCASED (cut-cell), not "
        "body-fitted. cartesianMesh is inside-out: it meshes the volume ENCLOSED by the "
        "supplied surface. So the region depends on the topology the user declares - "
        "INTERNAL meshes the cavity inside the geometry (ducts, pipes, manifolds); "
        "EXTERNAL wraps the body in a far-field box and meshes the fluid around it. "
        "Tolerates dirty/non-watertight geometry (gaps, thin trailing edges) and meshes quickly. "
        "Also the system's TRUE-2D engine: native cartesian2DMesh meshes a profile ribbon "
        "one cell thick with empty front/back planes (airfoil sections, 2D studies). "
        "PRODUCES an OpenFOAM polyMesh of the fluid. The staircased surface does not resolve "
        "the wall, so it does not support wall-resolved y+ or reliable near-wall boundary layers."
    ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        export_formats=("openfoam_polymesh", "stl_surface"),
        intake_guidance=(
            "cfMesh users think in meshDict terms: ask for maxCellSize intent "
            "(draft coarseness), any localRefinement surfaces and their cell "
            "sizes, boundaryLayers expectations (usually none), "
            "and patch names/types for renameBoundary."
        ),
        intake_advisories=(
            "The cut-cell surface is STAIRCASED by design (not body-fitted) - fine surface "
            "features and near-wall gradients may be under-resolved unless refined, and it "
            "is a poor fit when smooth wall-shear/boundary-layer resolution is the goal.",
            "Sharp feature edges have to be captured before meshing or they are rounded off; "
            "very thin regions and tight gaps between surfaces can leave bad cells that need "
            "coarser or more careful local sizing.",
        ),
        # NO `topology` param: it is redundant with the user's PURPOSE (external_cfd IS
        # external flow), and asking both admits a submission where the two disagree.
        # Topology is DERIVED from the purpose (engines.purposes.flow_topology).
        intake_params=(),
        # Consumes a body SURFACE and produces a FLUID volume → CFD only, in EITHER
        # region. Both are native to cartesianMesh: per the cfMesh user guide.4) the
        # workflows are "based on the inside-out meshing" and by default "keep only cells
        # in the template which are completely inside the geometry" - INTERNAL is the
        # default mode; EXTERNAL is the one needing a bounding box bolted on (what
        # surfaceGenerateBoundingBox exists to do).
        capabilities=(MeshCapability("body-surface", "fluid-volume",
                                     topologies=("internal", "external")),),
        input_contract=InputContract(
            dimensionalities=("2D", "3D"),
            input_kind="surface",
            min_thickness_ratio=0.0,
            rationale="cfMesh ships BOTH a 3D workflow (cartesianMesh) and a native 2D "
                      "workflow (cartesian2DMesh - meshes a profile ribbon one cell thick "
                      "with empty front/back planes); it wraps a surface STL and tolerates "
                      "non-watertight geometry. Raw thinness is handled by local "
                      "refinement, so no hard thin-feature floor is set.",
        ),
        # Named solids reach geom.fms and renameBoundary maps each to its own patch; proven
        # end to end from a STEP whose parts are named.
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "checkMesh fatal-topology (negative-volume / open / mis-oriented cells)",
                "a fatal topological defect is universally invalid - blocked by the manifest_valid gate"),
            _VC(_VA.QUALITY, "max skewness + max non-orthogonality (checkMesh)",
                "localized skew/non-ortho are measured criteria the reviewer weighs; a few skewed faces at a junction is production-grade, only fatal defects hard-block"),
            _VC(_VA.SOLVABILITY, "FV pressure-Poisson operator assembled from owner/neighbour and solved (PCG+AMG)",
                "a checkMesh-valid FV mesh can still be numerically unsolvable; the actual solve is the ground-truth usability check"),
            _VC(_VA.CONFORMANCE, "patch-role contract + far-field domain-extent gate",
                "the mesh must expose the requested patches (body/farfield) and a domain that is not clipped/undersized vs the request"),
        ),
        review_rationale=(
            "visual: an external far-field mesh has defect classes that measured "
            "metrics miss but a render exposes - a clipped/under-sized domain, a "
            "body not enclosed, mis-assigned patches. The reviewer weighs those "
            "renders alongside the same checkMesh metrics."
        ),
        _load_gates=_gates,
        _load_authoring_tool=_authoring_tool,
        _load_authoring_validate=_authoring_validate,
        _load_recommend=_recommend,
        _load_review_rubric=_review_rubric,
        _load_criteria=_criteria,
        _load_workspace_scaffold=_workspace_scaffold,
        _load_viewer_surface=_viewer_surface,
        _load_review_renderer=_review_renderer,
        render_artifacts=_RENDER_ARTIFACTS,
        inspection_targets=_INSPECTION_TARGETS,
        _load_target_obligations=lambda: _target_obligations,
        briefing=BRIEFING,
        deliverable=Deliverable(
            marker="constant/polyMesh/owner",
            bundle="openfoam_case.tar.gz", prefix="openfoam_case",
            label="OpenFOAM case",
            members=(
                # the mesh itself, and the recipe that produced it (run_policy requires meshDict)
                M("constant/polyMesh", kind="dir"),
                M("system/meshDict"),
                M("geom.stl"),
                # cfMesh's intermediate feature-edge surface: written only when the
                # surface-with-features path runs, so its absence is not a broken case.
                M("geom.fms", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            solvers=("OpenFOAM",),   # the polyMesh feeds OpenFOAM's FV solvers
        ),
        run_policy=RunPolicy(
            required_files=("system/meshDict",),
            # 20 MINUTES: at industry density (the passage caps put ~13 cells across every
            # passage and the resolution_floor gate refuses less) a production fill can reach
            # a few million hexes and 8-15 minutes of cartesianMesh; the old 420 s budget was
            # for draft meshes, where a too-fine dict was meant to fail fast so the builder
            # coarsens - coarsening is now bounded by the floor. Snappy's budget is 2400 s.
            run_timeout=lambda: min(1200, rtcfg.OPENFOAM_COMMAND_TIMEOUT),
            timeout_hint=("The wall band and background are held at ~13 cells across the passage "
                          "by the passage caps, so a fill that does not finish in 20 min is a domain "
                          "too large for that density or an objectRefinement that is far too fine: "
                          "drop or widen the objectRefinements first, then run_mesh again."),
            ok_guidance="Valid mesh (no fatal defects) - call submit_mesh.",
            fail_label="cartesianMesh failed",
            fail_hint=("Adjust the meshDict (fewer/thinner boundaryLayers, or "
                       "coarser/finer localRefinement) and run_mesh again."),
            submit_marker="constant/polyMesh/owner",
            submit_ok_key="polymesh",
            submit_hint=("No mesh built yet. Write system/meshDict, then call run_mesh; only "
                         "submit once run_mesh reports no fatal defects."),
        ),
        # NOTE: cartesian2DMesh ships in the open-source cfMesh edition
        # (verified 2026-07: openfoam.com v2112+ man page, integration-cfmesh
        # repo) - exposing it via run_mesh closes the true-2D gap, no new row.
)
