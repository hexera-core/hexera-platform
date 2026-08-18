# Responsibility: Declare snappyHexMesh: capabilities, input contract, deliverable, gates and review requirements.
# Boundaries: the single declaration the pipeline reads for this engine.
# Collaborates with: engines/base.py for the shape, and the modules in this package that fill each hook in.
from __future__ import annotations

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
from meshpipeline.engines.snappy._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.snappy.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.snappy.pack import SNAPPY_SYSTEM
    return SNAPPY_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.snappy.pack import SNAPPY_TOOL_NAMES
    return SNAPPY_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.snappy.flow_gates import FLOW_GATES
    return FLOW_GATES


def _authoring_tool():
    from meshpipeline.engines.snappy.authoring import AUTHORING_TOOL
    return AUTHORING_TOOL


def _authoring_validate():
    from meshpipeline.engines.snappy.authoring import validate
    return validate


def _recommend():
    from meshpipeline.engines.snappy.authoring import recommend
    return recommend


def _review_rubric():
    from meshpipeline.engines.snappy.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.snappy.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.snappy.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _viewer_surface():
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    return polymesh_viewer


def _review_renderer():
    # Lazy, like the viewer: the module pulls the render stack, and the spec must stay
    # importable without it.
    from meshpipeline.engines.snappy.review_renderer import RENDERER
    return RENDERER


# WHAT THIS ENGINE NEEDS TO BE SHOWN - declared by MANIFEST KEY, never a workspace path this
# engine composed. The sandbox resolves the key and confines the result, so a renderer cannot
# reach outside the workspace or follow a symlink out of it.

# THE COVERAGE FLOOR, declared. These reproduce EXACTLY what the reviewer derives today -
# every renderable patch, plus each manifest inspection region, so declaring them adds no
# new required target. The concrete ids are runtime-composed (they depend on the mesh), so the
# spec declares the KINDS it can be held to; the renderer enumerates the instances.


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


def _run_enricher():
    from meshpipeline.engines.snappy.drivers import run_enricher
    return run_enricher


def _build_driver():
    from meshpipeline.engines.snappy.drivers import drive
    return drive


SPEC = EngineSpec(

        name="snappy",
        validation_notes=(
            "body-surface→fluid-volume EXTERNAL (3D): full-pipeline e2e - half-CRM "
            "symmetry-plane delivery 2026-07-11 (gates PASS; the envelope limit is prism-"
            "layer coverage at hard wing-body junctions, not mesh validity); DPW4/CRM mesh "
            "live. INTERNAL: full-pipeline e2e on the real elbow90 STEP (2026-07-11). "
            "2D/2.5D: REJECTED - snappy is a 3D engine, full stop; the retired "
            "pseudo-2D NACA evidence predates this contract (real 2D → cfmesh)."),
        descriptor=(
        "FLUID / CFD volume meshing. Body-FITTED hex-dominant mesh: snaps the cells ONTO the "
        "surface (no staircasing) and inflates prism BOUNDARY LAYERS off the wall "
        "(snappyHexMesh). Resolves the surface and the near-wall region, so it supports "
        "wall-resolved y+ work (drag/lift, wings, wing-body, vehicles). Multi-scale via local "
        "refinement DERIVED from the geometry. PRODUCES an OpenFOAM polyMesh of the fluid. "
        "Slower than cfMesh and needs cleaner geometry. Does NOT produce a solid-volume mesh "
        "of the body interior (it fills the fluid AROUND a surface, not the solid), and has no "
        "true-2D mode."
    ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        export_formats=("openfoam_polymesh", "stl_surface"),
        intake_guidance=(
            "snappyHexMesh users think in snappyHexMeshDict terms: ask about "
            "refinement regions/levels (surface + volume), prism layer targets "
            "(nSurfaceLayers, y+ or first-layer thickness), feature-edge "
            "resolution, patch names/types, and whether the run is external "
            "(far-field box) or internal (castellated cavity) - asked as an "
            "OpenFOAM engineer would phrase it, not through a generic wrapper."
        ),
        intake_advisories=(
            "Prism boundary layers are inflated AFTER surface snapping, so they cannot grow "
            "where the base cells are compressed, two walls sit close together, or the "
            "surface is highly curved - at concave junctions (a wing-body corner) and thin "
            "trailing edges layers locally collapse, so a wall-resolved y+~1 case can fall "
            "short of full coverage and may need a relaxed coverage target.",
            "Hex cells snap toward the surface, so sharp edges and fine features come out "
            "faceted or rounded unless captured with explicit feature refinement; imperfect "
            "feature snapping also drives local non-orthogonality that can disable layer "
            "growth right there.",
            "Surface capture depends on adequate base refinement at features; very thin or "
            "highly curved regions may need extra refinement to be resolved faithfully.",
        ),
        # NO `topology` param - it was redundant with the user's PURPOSE (external_cfd IS
        # external flow), so the two answers could disagree: `internal_cfd` +
        # `topology=external` was an accepted submission. DERIVED from the purpose now
        # (engines.purposes.flow_topology).
        intake_params=(),
        # body-fits a surface and fills a FLUID region → CFD only, in EITHER region
        capabilities=(MeshCapability("body-surface", "fluid-volume",
                                     topologies=("internal", "external")),),
        input_contract=InputContract(
            # UPSTREAM TRUTH: snappyHexMesh is a 3D hex/split-hex mesher. The former
            # pseudo-2D workflow (thin slab + extrudeMesh collapse + empty retype) was a
            # wrapper invention and is REMOVED from supported capabilities - real 2D
            # Cartesian meshing belongs to cfMesh's native cartesian2DMesh.
            dimensionalities=("3D",),
            input_kind="surface",
            min_thickness_ratio=0.0,
            rationale="snappyHexMesh is a 3D hex/split-hex mesher - 3D ONLY. It has no "
                      "2D mode (true 2D belongs to cfMesh's native cartesian2DMesh), and a "
                      "thin slab is simply a 3D case with symmetry patches. It body-fits a "
                      "surface STL and wants cleaner geometry than cfMesh. Thin features are "
                      "resolved by surface refinement level, so no hard thin-feature floor.",
        ),
        # half-model meshing: a declared symmetry patch becomes a symmetryPlane box face on
        # the cut (the driver detects the plane and rejects a full-span body up front).
        supports_symmetry_plane=True,
        # PROVEN end to end: a STEP whose parts are named tessellates to named STL solids, the
        # dict declares them under regions{} with per-region patchInfo and layer entries, and
        # snappyHexMesh returns them as separate wall patches carrying their prism layers.
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "checkMesh fatal-topology (negative-volume / open / mis-oriented cells)",
                "a fatal topological defect is universally invalid - blocked by the manifest_valid gate"),
            _VC(_VA.QUALITY, "skewness, non-orthogonality + prism layer-coverage %",
                "layer coverage tells whether the prism layers inflated or collapsed into slivers; skew/non-ortho are reviewer-weighed, only fatal defects hard-block"),
            _VC(_VA.SOLVABILITY, "FV pressure-Poisson operator assembled from owner/neighbour and solved (PCG+AMG)",
                "a checkMesh-valid FV mesh can still be numerically unsolvable; the actual solve is the ground-truth usability check"),
            _VC(_VA.CONFORMANCE, "patch-role contract + far-field domain-extent gate",
                "the mesh must expose the requested patches (wall/farfield) and a domain that is not clipped/undersized vs the request"),
        ),
        review_rationale=(
            "visual: the value snappy adds over a metric check is body-fitted "
            "surface capture and prism boundary-layer coverage - whether layers "
            "actually inflated or collapsed into slivers, and whether the domain "
            "clips the body. Those read from a render; the reviewer also weighs "
            "layer_coverage and checkMesh metrics."
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
            # the RECIPE dicts ship too - the shared flow bundle used to list
            # only system/meshDict, so a snappy user received no recipe
            members=(
                M("constant/polyMesh", kind="dir"),
                M("system/blockMeshDict"),
                M("system/snappyHexMeshDict"),
                # snappy's surface IS constant/triSurface/<wall>.stl. `geom.stl` is cfMesh's name
                # for its assembled surface and snappy_runner never writes one, so requiring it
                # here failed delivery on every real snappy run with "deliverable contract not
                # satisfied - geom.stl: missing". The manifest has to describe what THIS engine
                # produces, not what its neighbour does.
                M("constant/triSurface", kind="dir"),
            ),
        ),
        downstream=DownstreamTarget(
            solvers=("OpenFOAM",),   # the polyMesh feeds OpenFOAM's FV solvers
        ),
        run_policy=RunPolicy(
            required_files=("system/blockMeshDict", "system/snappyHexMeshDict"),
            # a production snappy mesh legitimately takes 20-35 min off-box
            # (Cloud Run, 8 vCPU); stays under the service timeout (3600s).
            run_timeout=lambda: 2400,
            timeout_hint="Lower max_cells (and/or the surface level), then run_mesh again.",
            ok_guidance="Valid mesh (no fatal defects) - call submit_mesh.",
            fail_label="snappyHexMesh failed",
            fail_hint=("Adjust the strategy via configure_mesh (coarser refinement, "
                       "fewer layers) and run_mesh again."),
            submit_marker="constant/polyMesh/owner",
            submit_ok_key="polymesh",
            submit_hint=("No mesh built yet. configure_mesh writes the dicts, then call "
                         "run_mesh; only submit once run_mesh reports no fatal defects."),
        ),
        _load_run_enricher=_run_enricher,
        _load_build_driver=_build_driver,
)
