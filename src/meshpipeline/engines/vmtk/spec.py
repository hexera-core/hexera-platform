# Responsibility: Declare what VMTK is: its capabilities, input contract, deliverable, gates and review requirements.
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
from meshpipeline.engines.vmtk._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.vmtk.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.vmtk.pack import VMTK_SYSTEM
    return VMTK_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.vmtk.pack import VMTK_TOOL_NAMES
    return VMTK_TOOL_NAMES


def _gates() -> tuple:
    from meshpipeline.engines.vmtk.gates import VMTK_GATES
    return VMTK_GATES


def _run_enricher():
    from meshpipeline.engines.vmtk.vmtk_runner import run_enricher
    return run_enricher


def _authoring_tool():
    from meshpipeline.engines.vmtk.authoring import AUTHORING_TOOL
    return AUTHORING_TOOL


def _authoring_validate():
    from meshpipeline.engines.vmtk.authoring import validate
    return validate


def _recommend():
    from meshpipeline.engines.vmtk.authoring import recommend
    return recommend


def _review_rubric():
    from meshpipeline.engines.vmtk.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.vmtk.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


def _review_renderer():
    from meshpipeline.engines.vmtk.review_renderer import RENDERER
    return RENDERER


# vmtk ships the richest artifact set in the product - the lumen it was given, the centerlines
# it computed, the remeshed surface and the volume mesh. Those are exactly the artifacts its
# advertised validation coverage ("surface fidelity to the input lumen", layers "inflated or
# collapsed") needs, and none of them are reachable from aggregate numbers alone.

# LATENT: vmtk renders, but its composed axes request no render, so its verdict stays
# metric-only and nothing here is required yet. These record the defects the accepted audit
# found UNCOVERED by vmtk's current rubric - lumen smoothing, distorted stenoses, openings,
# disconnected anatomy have no axis at all today, and branch loss is judged by a COUNT.
# Activation belongs to the quality-closure phase, with known-truth fixtures.


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    m = manifest or {}
    ep = engine_params or {}
    n_open = m.get("n_open_profiles")
    if not isinstance(n_open, int):
        geom = m.get("geometry")
        n_open = geom.get("n_open_profiles") if isinstance(geom, dict) else None
    obs = [TargetObligation(
        kind=TargetKind.OPENING,
        min_count=(n_open if isinstance(n_open, int) and n_open > 0 else 1),
        provenance="inlet/outlet caps of the lumen")]
    try:
        if int(ep.get("boundary_layers", 0) or 0) > 0:
            obs.append(TargetObligation(kind=TargetKind.LAYER_REGION, min_count=1,
                                        provenance="boundary layers requested"))
    except (TypeError, ValueError):
        pass
    if (m.get("mesh_paths") or {}).get("centerlines"):
        obs.append(TargetObligation(kind=TargetKind.BRANCH, min_count=1,
                                    provenance="centerlines produced"))
    return tuple(obs)


def _viewer_surface():
    from meshpipeline.engines.vmtk.viewer_surface import surface_patches
    from meshpipeline.render.viewer_pack import stl_response

    def _hook(workspace, *, roles, units, skip_names=()):
        # the tet mesh's boundary surface (mesh.vtu) → the shared kind=stl payload
        return stl_response(surface_patches(workspace, named=True), roles, units, skip_names)
    return _hook


SPEC = EngineSpec(

        name="vmtk",
        validation_notes=(
            "body-surface→fluid-volume INTERNAL: FULL PIPELINE DELIVERED on the real open "
            "aorta (vmtk-test-data, 2026-07-11, job ecbb788a): 57,254 tets, metric review "
            "PASS, bundle uploaded. Admission-rejection path e2e-proven on a self-"
            "intersecting lumen. Known limit: boundary layers on tight lumens invert tets "
            "(the repair ladder drops layers first). See docs/engines/overview.md + engines/validation_evidence.json."),
        descriptor=(
            "Centerline-based tetrahedral volume meshing (VMTK). From a CLOSED LUMEN SURFACE it "
            "computes the centerlines, remeshes the surface with a RADIUS-ADAPTIVE edge length "
            "(cells scale with the local vessel/duct radius), and generates a tetrahedral volume "
            "mesh with optional near-wall boundary layers. This is the meshing approach built for "
            "TUBULAR / BRANCHING internal geometry, where a uniform cell size either starves the "
            "narrow branches or explodes the wide ones - e.g. patient-specific blood vessels, "
            "ducts, manifolds. PRODUCES a VTK-native tetrahedral volume mesh (.vtu) for VTK-family "
            "solvers. Consumes a surface (.vtp / .stl); a CAD solid is tessellated to a surface "
            "first. It fills the INSIDE of the surface, so it is not a far-field external-box "
            "mesher, and it emits tets rather than an OpenFOAM polyMesh (use snappyHexMesh / cfMesh for "
            "a hex-dominant polyMesh)."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        implemented=True,
        export_formats=("vtu", "vtp", "gmsh_msh", "stl_surface"),
        intake_guidance=(
            "VMTK users think in centerline/radius terms: ask how fine the mesh should be "
            "RELATIVE TO THE LOCAL RADIUS (the edge-length factor - VMTK's defining knob), "
            "whether near-wall boundary layers are needed (and how many), whether the open "
            "profiles at the ends of the lumen should be CAPPED into inlet/outlet patches, and "
            "which of those openings are the inlet(s) vs outlet(s) - asked as a VMTK user would "
            "phrase it, not through a generic wrapper."
        ),
        intake_advisories=(
            "The sizing is centerline-based and assumes a TUBULAR lumen with comparable "
            "inlet/outlet sizes - a non-tubular, heavily branched, or aneurysmal shape can "
            "produce a poor centerline and uneven sizing.",
            "Near-wall layers can collapse at tight bifurcations and in very small vessels or "
            "stenoses, where a sparse surface triangle count may also need subdivision "
            "before meshing - so layer coverage may fall short where the lumen is narrowest.",
        ),
        intake_params=(
            ParamSpec(
                key="wall_layers", values=("on", "off"), default="on",
                ask="Should the mesh inflate NEAR-WALL boundary layers inside the lumen wall "
                    "(needed whenever wall shear stress or the near-wall gradient matters), or "
                    "is a plain tetrahedral fill enough?",
            ),
        ),
        # fills the INSIDE of a supplied closed surface -> a fluid volume mesh. Same physical
        # capability as the wrap-and-fill flow engines; the technique + output format differ.
        # INTERNAL only: vmtk tetrahedralizes the lumen ENCLOSED by the surface,
        # sized off its centerline. There is no far-field construction in vmtk.
        capabilities=(MeshCapability("body-surface", "fluid-volume",
                                     topologies=("internal",)),
                      # A DECLARED FLUID DOMAIN (a CAD solid of the fluid region itself): its
                      # boundary IS the lumen surface - the solid is tessellated to that surface
                      # and the inside is filled, exactly the body-surface path. Registered so
                      # the intake stops refusing VMTK for the fluid twins (HEX-11); the delivery
                      # that backs it is in engines/validation_evidence.json.
                      MeshCapability("fluid-domain", "fluid-volume",
                                     topologies=("internal",))),
        input_contract=InputContract(
            dimensionalities=("3D",),
            input_kind="surface",
            min_thickness_ratio=0.0,
            # TetGen fills the interior, so a self-intersecting boundary is fatal (Invalid PLC)
            # - reject it at inspection instead of after inverted-tet build attempts.
            require_no_self_intersection=True,
            rationale="vmtk fills the interior of a CLOSED 3D surface and sizes cells from the "
                      "centerline radius, so it needs a watertight tubular/branching surface, not "
                      "a solid or a 2D section. Thin branches are resolved by the radius-adaptive "
                      "edge length, so no hard thin-feature floor is set.",
        ),
        # PROVEN: CellEntityIds carry per-region identity through meshing. A capped lumen whose
        # wall was split into two ids came back from vmtkMeshGenerator with both still distinct
        # (ids 1 and 90, alongside the cap ids), so several named wall regions survive - the caps
        # are additional patches, not a limit of one wall.
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "tetrahedral element validity - no inverted / zero-volume cells in mesh.vtu",
                "an inverted or degenerate tet is universally invalid for any FE/FV assembly; blocked by "
                "the manifest_valid gate"),
            _VC(_VA.QUALITY, "minimum tet aspect-ratio/dihedral quality + boundary-layer coverage",
                "tet quality is the standard numeric bar for an unstructured volume mesh; layer coverage "
                "reports whether the near-wall prisms inflated or collapsed along the lumen wall"),
            _VC(_VA.SOLVABILITY, "positive-volume element-validity guarantee (operator assembly)",
                "for an unstructured tet mesh, solvability reduces to element validity: a positive-volume "
                "tet mesh assembles a well-posed operator. Enforced in this engine's gate chain, so there "
                "is no separate post-gate linear solve - unlike the OpenFOAM engines' FV pressure-Poisson"),
            _VC(_VA.CONFORMANCE, "boundary-patch contract (wall / inlet / outlet caps) + surface fidelity to the input lumen",
                "the delivered mesh must expose the contracted patches - the wall plus the capped open "
                "profiles the user named inlet/outlet - and its boundary must follow the submitted lumen "
                "surface rather than a smoothed-away approximation of it"),
        ),
        review_rationale=(
            "metric-only: a tetrahedral volume mesh has no surface-visible defect class - its "
            "quality is INTERIOR and numeric (min aspect ratio / dihedral angle, boundary-layer "
            "coverage, radius-adaptive size distribution), which the reviewer judges directly "
            "from the measured evidence. Surface fidelity is measured as a deviation from the "
            "submitted lumen rather than eyeballed, so a screenshot would add cost, not signal."
        ),
        _load_gates=_gates,
        _load_run_enricher=lambda: _run_enricher(),
        _load_authoring_tool=_authoring_tool,
        _load_authoring_validate=_authoring_validate,
        _load_recommend=_recommend,
        _load_review_rubric=_review_rubric,
        _load_criteria=_criteria,
        _load_viewer_surface=_viewer_surface,
        _load_review_renderer=_review_renderer,
        render_artifacts=_RENDER_ARTIFACTS,
        inspection_targets=_INSPECTION_TARGETS,
        _load_target_obligations=lambda: _target_obligations,
        briefing=BRIEFING,
        deliverable=Deliverable(
            marker="mesh.vtu",
            bundle="vmtk_case.tar.gz", prefix="vmtk_case",
            label="OpenFOAM case",
            # the re-runnable spec + the derived surfaces ship with the mesh
            members=(
                M("mesh.vtu"),
                M("vmtk_spec.json"),
                M("lumen.vtp"),
                # the repair ladder can complete a mesh without centerlines, and the remeshed
                # surface and .msh export are conditional secondary outputs.
                M("centerlines.vtp", required=False),
                M("surface_remeshed.vtp", required=False),
                M("mesh.msh", required=False),
                # the same volume as an OpenFOAM case (gmshToFoam in the mesh image, right after
                # the fill) - the label promises one; a customer got a .vtu and a conversion to do
                M("mesh_volume.msh", required=False),
                M("openfoam_case/constant/polyMesh", kind="dir", required=False),
                M("openfoam_case/system/controlDict", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            # FACTUAL consumers of a VTK-native tet volume mesh - not a domain claim
            solvers=("OpenFOAM", "SimVascular/svSolver", "FEniCS/dolfinx", "SU2", "Elmer"),
        ),
        run_policy=RunPolicy(
            required_files=("vmtk_spec.json",),
            # ONE HOUR: at industry density (13 cells across, 5 layers) the sweep's largest case
            # (transition_013, a 1 m flat duct) fills in over 30 min; the Cloud Run job allows 4 h.
            run_timeout=lambda: min(3600, rtcfg.OPENFOAM_COMMAND_TIMEOUT * 3),
            timeout_hint=("Raise edge_length_factor (coarser cells relative to the radius) - but "
                          "not past 0.16, the 12-cells-across floor a CFD mesh must keep - then "
                          "run_mesh again."),
            ok_guidance="Valid tetrahedral mesh (no fatal defects) - call submit_mesh.",
            fail_label="vmtk pipeline failed",
            fail_hint=("Adjust the strategy via configure_mesh (coarser edge_length_factor, fewer "
                       "boundary_layers, or enable capping if the lumen has open profiles) and "
                       "run_mesh again."),
            submit_marker="mesh.vtu",
            submit_ok_key="volume_mesh",
            submit_hint=("No volume mesh built yet. configure_mesh writes vmtk_spec.json, then call "
                         "run_mesh; only submit once run_mesh reports a valid mesh.vtu."),
        ),
)
