# Responsibility: Declare what Netgen is: its capabilities, input contract, deliverable, gates and review requirements.
# Boundaries: the single declaration the pipeline reads for this engine.
# Collaborates with: engines/base.py for the shape, and the modules in this package that fill each hook in.
from __future__ import annotations

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.cad.fluid_passages import MAX_PASSAGES
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.base import (
    Deliverable,
    DeliveredMesh,
    DownstreamTarget,
    EngineSpec,
    FlowSupport,
    InputContract,
    MeshCapability,
    ParamSpec,
    RunPolicy,
    TurfRow,
)
from meshpipeline.engines.base import DeliverableMember as M
from meshpipeline.engines.base import ValidationAxis as _VA
from meshpipeline.engines.base import ValidationCoverage as _VC
from meshpipeline.engines.netgen._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.netgen.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.netgen.pack import NETGEN_SYSTEM
    return NETGEN_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.netgen.pack import NETGEN_TOOL_NAMES
    return NETGEN_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.netgen.gates import NETGEN_GATES
    return NETGEN_GATES


def _build_driver():
    from meshpipeline.engines.netgen.driver import drive
    return drive


def _review_rubric():
    from meshpipeline.engines.netgen.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.netgen.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _workspace_scaffold():
    from meshpipeline.engines.netgen.case_scaffold import write_case_skeleton
    return write_case_skeleton


def _review_renderer():
    from meshpipeline.engines.netgen.review_renderer import RENDERER
    return RENDERER


def _viewer_surface():
    from meshpipeline.engines.netgen.viewer import polymesh_viewer
    return polymesh_viewer


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    obs = [TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch")]
    regions = (manifest or {}).get("inspection_regions") or []
    names = [r.get("name") for r in regions if isinstance(r, dict) and r.get("name")]
    if names:
        obs.append(TargetObligation(kind=TargetKind.REGION,
                                    exact_ids=tuple(f"region:{n}" for n in names),
                                    provenance="authored inspection regions"))
    return tuple(obs)


# WHERE NETGEN IS AT HOME (engines/home_turf.py reads these). Netgen fills the inside of a staged
# fluid boundary with tetrahedra sized from the measured local passage and grows prism layers on the
# wall - any internal passage it can be given closed at its openings. It is home NOWHERE by
# declaration (home_classes is empty): where it is recommended is the measured record's to say
# (engines/fitness.py ENG-4, the lab and e2e runs in fitness_table.json).
HOME_TURF = (
    TurfRow("flow", "!=", "internal", "outside",
            "Netgen fills the inside of a staged fluid passage; it builds no far field around a "
            "body and no solid regions."),
    TurfRow("ports", "<", 2, "outside",
            "Netgen meshes a passage closed at its openings: one with fewer than an inlet and an "
            "outlet is not one it can be given."),
    TurfRow("neck", "<", 0.35, "home",
            "Branches of very different sizes: every wall point gets its own cell size from its "
            "own passage, so a small branch is not starved and a wide one not flooded.",
            flow="internal"),
    TurfRow("slenderness", ">", 100.0, "weak",
            "Long and thin for its gap (a narrow annulus, a flat slot): tetrahedra 14 across the "
            "gap are as long as they are wide, so the cell count grows with the length; hexahedral "
            "meshers stretch their cells along it.", flow="internal"),
)


SPEC = EngineSpec(
        name="netgen",
        validation_notes=(
            "body-surface/fluid-domain -> fluid-volume INTERNAL: tetrahedra with prism layers from "
            "the staged fluid boundary, delivered as an OpenFOAM polyMesh through gmshToFoam; lab "
            "and e2e evidence in engines/fitness_table.json."),
        descriptor=(
            "Tetrahedral volume meshing with prism boundary layers (Netgen, LGPL). From the staged "
            "CLOSED fluid boundary of an internal passage it remeshes the wall with the cell size "
            "measured from the LOCAL passage at every point (about 14 cells across every passage, "
            "so a narrow branch and a wide plenum each get their own size), grows prism layers on "
            "the wall and fills the inside with tetrahedra. Built for internal flow of any shape - "
            "branching manifolds, vessel trees, transitions, junctions. PRODUCES an OpenFOAM "
            "polyMesh (tetrahedra + wall prisms) with every patch under its declared name. It "
            "fills the INSIDE of the passage, so it is not a far-field external mesher."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        implemented=True,
        export_formats=("openfoam_polymesh", "gmsh_msh", "stl_surface"),
        intake_guidance=(
            "Netgen users think in terms of cells across a passage and wall layers: ask whether "
            "near-wall prism layers are needed (and how many) and which openings are inlets and "
            "outlets. The cell size follows the passage itself; there is no size factor to ask."
        ),
        intake_advisories=(
            "One layer stack is grown over the whole wall, sized for its narrowest passage, so the "
            "layers are thinner than a wide pipe needs; the census on the delivered mesh says how "
            "much of the wall got every layer.",
            "Tetrahedra need about twice the cells hexahedra do for the same resolution, so a long, "
            "thin passage costs more cells than on a hex-dominant mesher.",
        ),
        intake_params=(
            ParamSpec(
                key="wall_layers", values=("on", "off"), default="on",
                ask="Should the mesh grow NEAR-WALL prism layers on the wall (needed whenever wall "
                    "shear stress or the near-wall gradient matters), or is a plain tetrahedral "
                    "fill enough?",
            ),
        ),
        capabilities=(MeshCapability("body-surface", "fluid-volume", topologies=("internal",)),
                      MeshCapability("fluid-domain", "fluid-volume", topologies=("internal",))),
        # SEVERAL PASSAGES IN ONE FILL: Netgen meshes every closed piece of the staged boundary in one
        # volume (a left and a right carotid tree), each its own region with its own openings, and the
        # solvability check pins one pressure per region (engines/netgen/solvability.py)
        accepts=(FlowSupport("internal", ("cad",),
                             designed_for="internal passages of any shape: manifolds, vessel "
                                          "trees, transitions", max_passages=MAX_PASSAGES),),
        # a tetrahedral filler: after the hex meshers and VMTK on the internal ladder
        ladder_rank=35,
        home_turf=HOME_TURF,
        home_classes=(),
        input_contract=InputContract(
            internal_from_surface=True,
            dimensionalities=("3D",),
            input_kind="surface",
            min_thickness_ratio=0.0,
            rationale="Netgen remeshes and fills a CLOSED staged fluid boundary (the shared internal "
                      "staging closes it at the confirmed openings); the cell size follows the "
                      "measured passage, so no hard thin-feature floor is set.",
        ),
        supports_multiple_wall_patches=True,
        boundary_limits=(("internal", (("wall", 1), ("symmetry", 0), ("empty", 0))),),
        validation_coverage=(
            _VC(_VA.INTEGRITY, "checkMesh fatal-topology (negative-volume / open / mis-oriented cells)",
                "a fatal topological defect is universally invalid - blocked by the manifest_valid "
                "gate; a rung whose mesh has one is never delivered (the ladder moves on)"),
            _VC(_VA.QUALITY, "max non-orthogonality + skewness (checkMesh) + layer census",
                "the measured OpenFOAM criteria the reviewer weighs, and the layers counted on the "
                "delivered mesh against the request"),
            _VC(_VA.SOLVABILITY, "FV pressure-Poisson operator assembled from owner/neighbour and solved (PCG+AMG)",
                "a checkMesh-valid FV mesh can still be numerically unsolvable; the actual solve is "
                "the ground-truth usability check"),
            _VC(_VA.CONFORMANCE, "patch-role contract + port areas + boundary types",
                "the mesh must expose the declared wall and every inlet/outlet at its declared size, "
                "typed as the solver needs"),
        ),
        review_rationale=(
            "metrics + patch views: a tetrahedral fill's quality is numeric (checkMesh, the layer "
            "census, cells across the passage); the render answers whether the remeshed wall still "
            "follows the part and each patch landed on its opening."
        ),
        _load_gates=_gates,
        _load_build_driver=_build_driver,
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
            bundle="netgen_case.tar.gz", prefix="netgen_case",
            label="OpenFOAM case",
            members=(
                M("constant/polyMesh", kind="dir"),
                M("fluid_boundary.stl"),
                M("netgen_spec.json"),
                M("netgen_report.json", required=False),
                M("mesh_volume.msh", required=False),
                M("system/controlDict", required=False),
                # the Fluent mesh and the EnSight case written beside it (engines/cfd_exports.py), when
                # they were - Everything carries every solver file, as an FEA mesh's does
                M("export_fluent.zip", required=False),
                M("export_ensight.zip", required=False),
            ),
        ),
        downstream=DownstreamTarget(solvers=("OpenFOAM",)),
        delivered_mesh=DeliveredMesh(cells="tetrahedral", walls="body-fitted", prism_layers=True),
        run_policy=RunPolicy(
            required_files=("netgen_spec.json",),
            run_timeout=lambda: min(3000, rtcfg.OPENFOAM_COMMAND_TIMEOUT * 3),
            watched=True,
            timeout_hint=("A fill that makes no progress for the stall window is stuck, not slow; "
                          "the ladder's thinner and layer-free rungs come next."),
            ok_guidance="Valid mesh (no fatal defects) - call submit_mesh.",
            fail_label="Netgen failed",
            fail_hint="No rung of the Netgen ladder produced a valid polyMesh.",
            submit_marker="constant/polyMesh/owner",
            submit_ok_key="polymesh",
            submit_hint="No mesh built yet: the Netgen driver runs the plan before submitting.",
        ),
)
