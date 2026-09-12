# Responsibility: Declare what Gmsh is: its capabilities, input contract, deliverable, gates and review requirements.
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
from meshpipeline.engines.gmsh._shared import _INSPECTION_TARGETS, _RENDER_ARTIFACTS
from meshpipeline.engines.gmsh.builder_guidance import BRIEFING


def _prompt() -> str:
    from meshpipeline.engines.gmsh.pack import GMSH_SYSTEM
    return GMSH_SYSTEM


def _tools() -> set[str]:
    from meshpipeline.engines.gmsh.pack import GMSH_TOOL_NAMES
    return GMSH_TOOL_NAMES


def _recommend():
    from meshpipeline.engines.gmsh.authoring import recommend
    return recommend


def _gates() -> tuple:
    from meshpipeline.engines.gmsh.gates import GMSH_GATES
    return GMSH_GATES


def _review_renderer():
    from meshpipeline.engines.gmsh.review_renderer import RENDERER
    return RENDERER


# ACTIVE: the gmsh review is hybrid. The composed axes require the iso opening view plus a
# named-group inspection (criteria.REVIEW_AXES), and _target_obligations below holds the
# reviewer to inspecting every declared physical group. Everything required here is served by
# the gmsh render session (open screenshot + toggle_patch group isolation) - and NOTHING more
# may be required: the session exposes no REGION/slice targets, so no axis, obligation or
# manifest promise on this engine may demand interior-slice evidence
# (tests/unit/engines/test_gmsh_review_evidence_contract.py pins this satisfiability).


def _target_obligations(manifest, engine_params, purpose):
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    m = manifest or {}
    declared = m.get("physical_groups") or m.get("patch_types") or m.get("patches") or {}
    n = len(declared) if isinstance(declared, (dict, list, tuple)) else 0
    if n > 0:
        return (TargetObligation(kind=TargetKind.GROUP, min_count=n,
                                 provenance=f"{n} declared physical group(s)"),)
    return ()


def _viewer_surface():
    from meshpipeline.engines.gmsh.viewer_surface import surface_patches
    from meshpipeline.render.viewer_pack import stl_response

    def _hook(workspace, *, roles, units, skip_names=()):
        # named boundary groups from mesh.msh → the shared kind=stl payload
        return stl_response(surface_patches(workspace), roles, units, skip_names)
    return _hook


def _review_rubric():
    from meshpipeline.engines.gmsh.criteria import REVIEW_AXES
    return REVIEW_AXES


def _criteria():
    from meshpipeline.engines.gmsh.criteria import CRITERIA_ROWS
    return CRITERIA_ROWS


SPEC = EngineSpec(

        name="gmsh",
        validation_notes=(
            "solid-body→solid-volume (structural): full-pipeline e2e on the real elbow90 STEP "
            "(13,985 order-2 tets delivered, 2026-07-10). Fluid domain→fluid-volume (CFD on a "
            "prepared fluid domain): full-pipeline e2e 2026-07-11 - elbow90 supplied AS the "
            "fluid domain, delivered with contract-exact inlet/outlet/wall groups. "
            "planar-domain→surface-mesh (2D plane FEA): full-pipeline e2e on the "
            "committed plate-with-hole fixture (see docs/engines/overview.md + engines/validation_evidence.json)."),
        descriptor=(
            "Closed-volume tetrahedralization. Gmsh: the canonical open-source "
            "volume mesher (the mesher inside FreeCAD FEM; .msh is the interchange "
            "for Elmer/SU2/dolfinx). Meshes a closed CAD volume into quality "
            "second-order tetrahedra; artifact = re-runnable gmsh_spec.json. The "
            "meshed volume depends on the workflow: for STRUCTURAL it is the solid "
            "body; for CFD it must be an already-prepared closed fluid domain - this "
            "system does not generate the surrounding fluid domain from a bare body, "
            "so a CFD run requires a Fluid domain geometry as input. Exports Abaqus "
            ".inp NATIVELY (drives Abaqus and CalculiX) plus Nastran .bdf and .unv. "
            "Also the 2D FE engine: a planar face/sheet body meshes into plane-"
            "stress/strain triangles (CPS elements), boundary conditions on its "
            "edges. Needs a valid closed volume (3D) or flat face (2D); not a hex "
            "mesher."
        ),
        _load_prompt=_prompt,
        _load_tools=_tools,
        _load_recommend=_recommend,
        export_formats=("abaqus_inp", "nastran_bdf", "gmsh_msh", "unv"),
        intake_guidance=(
            "Gmsh users think in element terms: for a structural deck ask which "
            "analysis it feeds (static stress, modal), whether they need "
            "second-order elements (the accuracy default) or first-order, roughly "
            "how fine (a size factor of the part's diagonal), and which faces carry "
            "constraints/loads - those become named groups (fixed/load/contact) "
            "in the .inp."
        ),
        intake_advisories=(
            "Delaunay tetrahedral meshing can leave sliver elements (thin, poorly-shaped "
            "tets) at sharp corners and in high-aspect or very thin regions; the optimizer "
            "removes most but does not guarantee every element meets the quality target, so "
            "such geometry may need local sizing.",
            "A dirty or non-watertight CAD import (gaps, slivers, tiny faces) can make the "
            "volume fail to mesh cleanly - a repair pass on the geometry may be needed "
            "first.",
        ),
        intake_params=(
            ParamSpec(
                key="element_order", values=("1", "2"), default="2",
                ask="First-order (linear) or second-order (quadratic) "
                    "tetrahedra? Second-order is the stress-accuracy default; "
                    "first-order gives smaller, faster decks.",
            ),
        ),
        # gmsh meshes ANY closed CAD volume → it serves structural (mesh the solid
        # body) AND CFD (mesh a supplied fluid domain). Two capabilities, not a
        # domain lock.
        capabilities=(
            MeshCapability("solid-body", "solid-volume"),   # topologies N/A: a solid mesh
            # the user PREPARED the closed fluid domain; gmsh meshes whatever it is,
            # so it serves either flow topology.
            MeshCapability("fluid-domain", "fluid-volume",
                           topologies=("internal", "external")),
            # 2D plane-stress/strain FEA: a FLAT face/sheet body meshed into
            # triangles (CPS3/CPS6 in the .inp) with boundary groups on curves.
            MeshCapability("planar-domain", "surface-mesh"),
        ),
        input_contract=InputContract(
            dimensionalities=("2D", "3D"),
            input_kind="solid",
            min_thickness_ratio=0.0,
            rationale="3D: a valid closed CAD solid → second-order tetrahedra. "
                      "2D: a FLAT (planar) face/sheet body → plane-stress/strain "
                      "triangles with boundary groups on the curves. Element sizing "
                      "adapts to thin solids, so no hard thin-feature floor is set.",
        ),
        # PROVEN: the STEP is imported into gmsh's own kernel and each volume's boundary is
        # named as its own physical surface group; both groups survive 3D mesh generation
        # (cht_concentric_pipe.step -> 2 volumes -> wall_1, wall_2). The B-rep is staged, so
        # CAD topology reaches the groups rather than a flattened surface.
        supports_multiple_wall_patches=True,
        validation_coverage=(
            _VC(_VA.INTEGRITY, "gmsh element validity - no inverted / degenerate tets (fatal in check_mesh)",
                "an inverted or zero-volume element is universally invalid for FE assembly"),
            _VC(_VA.QUALITY, "min SICN floor + low-SICN fraction",
                "SICN (scaled signed Jacobian) is the standard element-quality metric for 2nd-order tets; the sicn_floor gate blocks poorly-conditioned elements"),
            _VC(_VA.SOLVABILITY, "positive-Jacobian element-validity guarantee (FE stiffness assembly)",
                "FEA solvability reduces to element validity: a valid positive-Jacobian tet mesh assembles a well-posed stiffness matrix. FE assembly is robust where the FV pressure operator is fragile, so no separate linear solve is run - the SICN>0 guarantee IS this engine's solvability check"),
            _VC(_VA.CONFORMANCE, "named-group region contract + case-level far-field extent",
                "the deck must expose the contracted named groups (structural: fixed/load/contact/free; a supplied fluid domain: wall/farfield); the far-field domain-extent check is CASE-level - it runs only when the case declares far-field extents AND the manifest records a domain box, so a solid-body mesh (which records none) simply no-ops. Applicability is decided by the case/artifact, never by the engine name.", ),
        ),
        review_rationale=(
            "metrics + group views: element quality is INTERIOR and numeric (SICN / "
            "low-SICN fraction) and is judged directly from the measured report - there "
            "are no interior slice views on this engine, so do not look for them. The "
            "render lane answers the one question numbers cannot: whether each named "
            "physical group landed on the intended geometry, checked from the overview "
            "and by isolating groups (toggle_patch)."
        ),
        _load_gates=_gates,
        _load_viewer_surface=_viewer_surface,
        _load_review_renderer=_review_renderer,
        render_artifacts=_RENDER_ARTIFACTS,
        inspection_targets=_INSPECTION_TARGETS,
        _load_target_obligations=lambda: _target_obligations,
        _load_review_rubric=_review_rubric,
        _load_criteria=_criteria,
        briefing=BRIEFING,
        deliverable=Deliverable(
            marker="mesh.inp",
            bundle="gmsh_case.tar.gz", prefix="gmsh_case",
            label="Abaqus deck (.inp) + Nastran / UNV",
            members=(
                M("mesh.inp"),
                M("gmsh_spec.json"),
                # the CAD the deck was built from, and the alternate decks: gmsh writes these
                # only when the corresponding export is requested, so they are conditional.
                M("geometry.step", required=False),
                M("mesh.bdf", required=False),
                M("mesh.unv", required=False),
            ),
        ),
        downstream=DownstreamTarget(
            # a tet mesh feeds structural, CFD AND multiphysics solvers alike -
            # capability, not a domain claim (its .inp/.msh/.bdf/.unv exports)
            solvers=("CalculiX", "Abaqus", "Nastran", "SU2", "Elmer", "FEniCS/dolfinx"),
        ),
        run_policy=RunPolicy(
            required_files=("gmsh_spec.json",),
            # 25 MINUTES: at industry density the passage field (driver.PASSAGE_CELLS_ACROSS)
            # fills a volute in 7-8 min of single-threaded gmsh (volute_scroll_007: 2.57M tets in
            # 442 s), right at the old 420 s; the builder loop is 3600 s, validated >= 2x this.
            run_timeout=lambda: min(1500, rtcfg.OPENFOAM_COMMAND_TIMEOUT),
            timeout_hint=("The passage field holds ~13 cells across every passage, so a fill that "
                          "does not finish in 25 min is a domain too large for that density "
                          "(a long flat duct): raise size.value only if the brief allows a coarser "
                          "mesh, otherwise report the limit; then run_mesh again."),
            ok_guidance="Valid mesh (no fatal defects) - call submit_mesh.",
            fail_label="gmsh driver failed",
            fail_hint=("Adjust gmsh_spec.json (raise size.value to coarsen; keep "
                       "optimize=true) and run_mesh again."),
            submit_marker="mesh.inp",
            submit_ok_key="deck",
            submit_hint=("No FEA deck built yet. Write gmsh_spec.json, then call run_mesh; "
                         "only submit once it reports mesh_ok."),
        ),
)
