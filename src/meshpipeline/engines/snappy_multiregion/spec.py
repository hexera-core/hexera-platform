# Responsibility: Declare multi-region snappyHexMesh: capabilities, input, deliverable, gates and review needs.
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
    ParamSpec,
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
from meshpipeline.engines.snappy_multiregion._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.snappy_multiregion.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.snappy_multiregion.pack import SNAPPY_MULTIREGION_SYSTEM
    return SNAPPY_MULTIREGION_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.snappy_multiregion.pack import SNAPPY_MULTIREGION_TOOL_NAMES
    return SNAPPY_MULTIREGION_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.snappy_multiregion.gates import MULTIREGION_GATES
    return MULTIREGION_GATES


def _authoring_tool():
    from meshpipeline.engines.snappy_multiregion.authoring import AUTHORING_TOOL
    return AUTHORING_TOOL


def _authoring_validate():
    from meshpipeline.engines.snappy_multiregion.authoring import validate
    return validate


def _recommend():
    from meshpipeline.engines.snappy_multiregion.authoring import recommend
    return recommend


def _review_rubric():
    from meshpipeline.engines.snappy_multiregion.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.snappy_multiregion.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.snappy_multiregion.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _review_renderer():
    from meshpipeline.engines.snappy_multiregion.review_renderer import RENDERER
    return RENDERER


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    m = manifest or {}
    obs = [TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch")]
    regions = m.get("regions") or m.get("inspection_regions") or []
    names = [r.get("name") for r in regions if isinstance(r, dict) and r.get("name")]
    if names:
        obs.append(TargetObligation(
            kind=TargetKind.REGION, exact_ids=tuple(f"region:{n}" for n in names),
            provenance="declared regions/interfaces"))
    else:
        obs.append(TargetObligation(kind=TargetKind.REGION, min_count=1,
                                    provenance="fluid-solid interfaces"))
    return tuple(obs)


def _viewer_surface():
    from meshpipeline.engines.snappy_multiregion.viewer import polymesh_viewer
    return polymesh_viewer


SPEC = EngineSpec(

        name="snappy_multiregion",
        validation_notes=(
            "solid-assembly→multiregion-volume: CLEAN T5 2026-07-12 (job bb5c9179, plate+"
            "derived-air): actual regions reconcile EXACTLY with the declared plan (no "
            "undeclared regions - enforced by the regions_split gate), conformal interface, "
            "visual PASS, delivered attempt 1. Earlier delivery cf88cbe0 shipped an inert "
            "domain0 (fixed: zero-pad enclosing-fluid background + reconciliation gate). "
            "19-solid as1 remains a stress fixture."),
        descriptor=(
            "Multi-region body-fitted volume meshing (snappyHexMesh + splitMeshRegions). From a "
            "multi-solid CAD assembly it builds ONE body-fitted background mesh, tags each region "
            "with a cellZone, then splitMeshRegions cuts it into a per-region polyMesh with "
            "CONFORMAL interfaces between touching regions (auto-created coupled patches) plus "
            "constant/regionProperties. PRODUCES a coupled multi-region OpenFOAM case - whatever "
            "the user's PURPOSE needs it for: conjugate heat transfer (chtMultiRegionFoam), "
            "multi-material / bonded-solid analysis, or fluid-structure interaction. Needs a "
            "clean multi-solid assembly whose regions share conformal faces; a single-solid body "
            "is a single-region job (use snappyHexMesh / cfMesh / Gmsh)."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        implemented=True,
        export_formats=("openfoam_polymesh_multiregion",),
        intake_guidance=(
            "Multi-region users think in OpenFOAM multi-region terms: ask which solids in "
            "the assembly are the FLUID region(s) and which are SOLIDS, how the fluid enters/"
            "leaves (inlet/outlet for an internal passage, or the far-field for an external "
            "flow), which external faces carry the boundary conditions the user's analysis "
            "applies, and how finely the near-interface region must be resolved (the coupling - "
            "thermal, structural, or otherwise - concentrates right at the interface) - asked as "
            "a multi-region OpenFOAM engineer would phrase it."
        ),
        intake_advisories=(
            "Each region is built with the same snappyHexMesh inflation, so prism boundary layers "
            "can collapse at concave junctions and thin regions - the same near-wall "
            "coverage caveat as single-region snappyHexMesh, now per region.",
            "The regions must share CONFORMAL interfaces (matching faces on both sides) or "
            "they cannot exchange across the interface; poorly separated solids in the "
            "assembly can leak into one another or fail to couple.",
            "It needs a clean multi-solid assembly whose regions actually touch - a single "
            "solid, or solids with gaps/overlaps at their shared faces, will not split into "
            "the coupled regions you expect.",
        ),
        intake_params=(
            ParamSpec(
                key="fluid_topology", values=("internal", "external"), default="internal",
                ask="Is the fluid region an INTERNAL passage through/around the solids "
                    "(a channel, jacket or core, meshed as a cavity - the usual multi-region "
                    "topology), or an EXTERNAL flow around a solid body immersed in a "
                    "far-field box?",
            ),
        ),
        # a multi-solid assembly -> a coupled multi-region volume mesh (fluid + solids)
        # topologies=() - a multi-region case declares its own `fluid_topology` param;
        # the CHT purpose does not fix it.
        capabilities=(MeshCapability("solid-assembly", "multiregion-volume"),),
        input_contract=InputContract(
            dimensionalities=("3D",),
            input_kind="solid",
            min_thickness_ratio=0.0,
            rationale="A coupled multi-region case is inherently 3D and needs a multi-solid CAD "
                      "assembly (one closed solid per region) whose regions share conformal "
                      "faces, so splitMeshRegions can cut them apart with matching interfaces. A "
                      "single solid or a bare surface cannot express the coupling.",
        ),
        # PROVEN by structure: this bundle meshes VOLUME regions (fluid/solid) conformally, each
        # contributing its own named boundary patches - cht_concentric_pipe.step reads as 2 named
        # regions and the runner takes them as a mapping. A different mechanism from naming several
        # walls on one body, and it does not constrain wall arity.
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "per-region checkMesh fatal-topology (every region a valid, closed polyMesh)",
                "each split region must itself be a topologically valid mesh - a fatal defect in ANY "
                "region makes the coupled case unsolvable; blocked by the per-region manifest_valid gate"),
            _VC(_VA.QUALITY, "per-region skewness + non-orthogonality, and interface face-match count",
                "skew/non-ortho are weighed per region as for any snappy mesh; additionally the coupled "
                "interface patches must carry matching face counts on both sides or the regions cannot "
                "exchange across the interface"),
            _VC(_VA.SOLVABILITY, "per-region mesh validity - every split region is a closed, non-degenerate polyMesh",
                "a coupled multi-region case is solvable iff every region is itself a valid mesh (a fluid "
                "region assembles its FV operators, a solid region its conduction/mechanics operator); enforced "
                "per region by the manifest_valid + regions_split gates, then coupled by the downstream "
                "multi-region solver - so there is no separate whole-case solve stage here"),
            _VC(_VA.CONFORMANCE, "every declared region present after the split + fluid/solid roles + coupled interface patches",
                "the delivered case must expose exactly the regions the user declared, tagged fluid vs solid "
                "in regionProperties, with the interface patches between touching regions created - the "
                "multi-region contract"),
        ),
        review_rationale=(
            "visual: the value a multi-region mesh adds over a single-region one is the region "
            "SPLIT and the conformal INTERFACES - whether each solid meshed as its own region, "
            "whether the interfaces between touching regions are continuous, and whether the "
            "near-interface region is resolved on both sides. Those read from a sectioned "
            "render; the reviewer also weighs the per-region checkMesh metrics."
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
            marker="constant/regionProperties",
            bundle="openfoam_multiregion_case.tar.gz", prefix="openfoam_multiregion_case",
            label="OpenFOAM case (multi-region)",
            # bundling constant/ carries every region's polyMesh + regionProperties; the
            # recipe dicts ship too so the case is re-runnable end to end.
            members=(
                M("constant", kind="dir"),
                M("system/blockMeshDict"),
                M("system/snappyHexMeshDict"),
                # as with snappy: each region's surface is constant/triSurface/<region>.stl.
                # `geom.stl` is cfMesh's assembled-surface name and is never written here.
                M("constant/triSurface", kind="dir"),
                # region splitting only writes a topoSetDict when explicit cellZones are used
                M("system/topoSetDict", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            # FACTUAL consumers of a split multi-region OpenFOAM case - not a domain claim
            solvers=("chtMultiRegionFoam (conjugate heat transfer)",
                     "solids4Foam (fluid-structure interaction)"),
        ),
        run_policy=RunPolicy(
            required_files=("system/blockMeshDict", "system/snappyHexMeshDict"),
            # a multi-region build (background snappy + split of several regions)
            # is costlier than a single-region snappy run; stays under the service timeout.
            run_timeout=lambda: min(3000, rtcfg.OPENFOAM_COMMAND_TIMEOUT * 5),
            timeout_hint="Lower max_cells and/or the per-region surface levels, then run_mesh again.",
            ok_guidance="Every declared region split cleanly (no fatal defects) - call submit_mesh.",
            fail_label="multi-region snappyHexMesh / splitMeshRegions failed",
            fail_hint=("Adjust the strategy via configure_mesh (coarser refinement, fewer "
                       "layers, or fix a region whose zone leaked) and run_mesh again."),
            submit_marker="constant/regionProperties",
            submit_ok_key="regions",
            submit_hint=("No multi-region case built yet. configure_mesh writes the dicts, then "
                         "run_mesh (which meshes, then splits into regions); only submit once "
                         "run_mesh reports every region split with no fatal defects."),
        ),
)
