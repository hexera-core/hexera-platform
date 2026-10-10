# Responsibility: Define the contract every meshing engine implements, so the pipeline treats five meshers uniformly.
# Owns: the engine specification type and its parts.
# Boundaries: declaration only; no engine's mechanics, no native process, and no knowledge of which engines exist.
# Collaborates with: engines/registry.py, and each engines/<name>/spec.py.
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from meshpipeline.contracts.review_evidence import (
    InspectionTarget,
    RenderArtifactRequirement,
    ReviewRenderer,
)
from meshpipeline.engines.ground_plane import ground_patch_name, is_ground
from meshpipeline.engines.purposes import PURPOSES, Purpose, is_compatible, topology_of  # noqa: F401

logger = logging.getLogger(__name__)


# THE common standard: what "validated & complete, ready for the user" MEANS.
# Identical for EVERY engine - the pipeline enforces it uniformly, regardless of
# mesh type (FV flow, tet FEA, …). An engine cannot lower this bar; it can only
# declare WHICH quality metrics and WHICH review modality are appropriate to its
# mesh (see EngineSpec.visual_review / review_rationale).
# A mesh is VALIDATED iff ALL THREE hold:
#   1. executor_success  - every declared executor gate passed (GateSpec chain):
#                          the ground-truth "a real, non-fatal mesh exists" gate.
#   2. reviewer PASS      - the SAME reviewer (submit_findings + application-derived
#                          protocol) returned PASS on the engine's evidence. The
#                          modality differs (visual render for flow meshes whose
#                          defects are visible; metric-only for solid FEA whose
#                          quality is numeric) but the agent, protocol and bar do
#                          not. (pipeline.outcome.normalize_verdict drives this.)
#   3. deliverable        - the engine's declared Deliverable was produced and
#                          shipped as the mesh_bundle artifact.
# Enforcement points, in code: (1) pipeline.executor + run_gates; (2) agents.
# reviewer / reviewer_metric via llm_router.call_reviewer_with_tools; (3) the
# delivery gate guarded by executor_success. This constant is the WRITTEN source
# of truth those points implement; it is not a second implementation.
VALIDATED_STANDARD = (
    "A mesh is validated & ready iff: (1) executor_success - every applicable "
    "validation axis (integrity, quality, solvability, conformance) passed; AND "
    "(2) the reviewer's accepted findings derived PASS (same submit_findings "
    "protocol for every engine; modality is engine-declared and justified); AND "
    "(3) the engine's declared deliverable was produced and shipped. The bar is "
    "identical across engines; only the CONCRETE check per axis, the applicable "
    "quality metrics, and the review modality vary by mesh type."
)


class ValidationAxis(str, Enum):
    INTEGRITY   = "integrity"     # valid topology - no fatal / degenerate defects
    QUALITY     = "quality"       # element / cell quality within usable bounds
    SOLVABILITY = "solvability"   # fit for the downstream consumer - it actually works
    CONFORMANCE = "conformance"   # matches the user's declared contract (regions / domain)


@dataclass(frozen=True)
class ValidationCoverage:
    axis: ValidationAxis
    check: str            # the concrete mechanism, in the engine's own vocabulary
    rationale: str        # why this satisfies the axis (or, if not applicable, why N/A)
    applicable: bool = True


@dataclass(frozen=True)
class InputContract:
    dimensionalities: tuple     # supported Dimensionality VALUES ("2D" / "3D")
    input_kind: str             # what the engine meshes: "solid" | "surface"
    min_thickness_ratio: float  # smallest resolvable thin_gap/diag; 0.0 = no floor declared
    rationale: str              # why these preconditions hold for this engine
    # A surface that passes through itself cannot bound a volume. An engine that FILLS a
    # closed surface (TetGen-backed, e.g. vmtk) requires this; a wrap-then-fill engine that
    # tolerates a dirty surface (snappy/cfMesh) does not - so it is opt-in per engine.
    require_no_self_intersection: bool = False
    # INTERNAL FLOW FROM A TRIANGLE SURFACE (STL, OBJ, PLY, ...): the engine's internal path takes
    # the staged surface and the openings the user confirmed on it (cad/internal_surface: lids on
    # open ends, capped faces kept, the fluid's side of a thick wall) instead of needing a CAD
    # solid. An engine without it meshes internal flow from a CAD solid only.
    internal_from_surface: bool = False


@dataclass(frozen=True)
class ParamSpec:
    key: str
    ask: str                             # the question, engine vocabulary
    values: tuple[str, ...]              # allowed answers (closed enum)
    default: str                         # dispatch-time default (intake skipped)
    required: bool = True                # intake may not submit without it


@dataclass(frozen=True)
class Briefing:
    geometry_line: str        # what geometry is staged, and which file to mesh
    workflow: str             # the tool workflow line, engine vocabulary
    contract_title: str       # region/patch-contract heading
    contract_guidance: str    # how contracted names map onto the engine's artifact
    domain_default: str       # task label fallback when intake supplied none


@dataclass(frozen=True)
class DeliverableMember:

    path: str                 # workspace-relative; never absolute, never traversing
    kind: str = "file"        # "file" | "dir"
    required: bool = True     # absent required member = the bundle is not deliverable


@dataclass(frozen=True)
class Deliverable:
    marker: str               # rel path proving the deliverable exists
    bundle: str               # object name of the tar.gz
    prefix: str               # arcname prefix inside the tar
    members: tuple[DeliverableMember, ...]
    # WHAT THE USER IS DOWNLOADING, in their words ("OpenFOAM case", "Abaqus deck").
    # Engine-declared, because the engine is what produces it - the UI used to hardcode
    # "OpenFOAM case" for every engine, which quietly mislabelled gmsh's Abaqus deck.
    label: str = "Mesh case"

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(m.path for m in self.members if m.required)

    @property
    def optional(self) -> tuple[str, ...]:
        return tuple(m.path for m in self.members if not m.required)


@dataclass(frozen=True)
class DownstreamTarget:
    solvers: tuple    # solvers/consumers that can read this mesh's delivered format(s)


#: How closely a mesh's wall follows the body, weakest first. A body-fitted wall delivers
#: everything a staircased one does and more, never the other way round.
WALL_FITS: tuple[str, ...] = ("staircased", "body-fitted")
CELL_KINDS: tuple[str, ...] = ("hex-dominant", "tetrahedral")


@dataclass(frozen=True)
class DeliveredMesh:
    """WHAT THE USER RECEIVES from this engine, in the terms an approval can promise.

    The fallback ladder (pipeline/engine_fallback.py) moves a run to another engine on its own
    only when that engine delivers what the approved one would have: the same file, the same
    kind of cells, a wall at least as true to the body, and prism layers where the brief asked
    for them. Anything less is offered to the user, never delivered silently. Declared here
    with the rest of the engine's capability, never inferred from its name."""

    cells: str            # one of CELL_KINDS - what fills the volume
    walls: str            # one of WALL_FITS - how the cells meet the body
    prism_layers: bool    # can deliver the near-wall prism layers a brief asks for, reliably


@dataclass(frozen=True)
class MeshCapability:
    input_kind: str    # submitted geometry: "solid-body" | "fluid-domain" | "body-surface"
    output_kind: str   # produced mesh: "solid-volume" | "fluid-volume"
    # WHICH fluid region this capability produces. "fluid-volume" alone cannot say whether
    # the mesh fills the cavity INSIDE the surface or the far field AROUND it - and those
    # are different machines. vmtk tetrahedralizes a lumen; it cannot build a far field.
    # Without this the (engine × purpose) gate - whose whole job is to reject physical
    # impossibility - accepted `vmtk + external_cfd`.
    # () = the distinction does not apply: a solid-volume mesh, or a multi-region case
    # whose fluid topology is its own declared param.
    topologies: tuple[str, ...] = ()
    # the dimensionalities THIS capability serves; () = whatever the engine's InputContract takes.
    # gmsh builds the fluid around a 3D body, but its 2D path is a planar FE mesh, not a far field.
    dimensionalities: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowSupport:
    """ONE FLOW this bundle is designed for, and the canonical geometry forms its staging can
    consume for it (engines/capability.py: GEOMETRY_FORMS, FLOW_KINDS).

    Declared, never inferred: the proposal, the fallback ladder and the offers all read it, so an
    engine is offered only for a file and flow it can actually take, and a new engine is offered
    exactly where its own spec says it fits. Claim the TRUE path on main - the forms the staging
    reaches the mesher with today - and widen it when that path is proven, never before."""

    flow: str                     # one of capability.FLOW_KINDS
    forms: tuple[str, ...]        # the subset of capability.GEOMETRY_FORMS it consumes for `flow`
    #: The shapes this flow's method is BUILT for, in plain words ("tubular passages: vessels,
    #: pipes, ducts"). A heads-up the proposal and the offers say out loud; never a gate.
    designed_for: str = ""


@dataclass(frozen=True)
class Diagnostic:
    severity: str      # "error" | "warning"
    path: str          # the offending field in the engine's vocabulary, e.g. "surface_level[1]"
    message: str       # what's wrong and how to fix it

    def as_dict(self) -> dict:
        return {"severity": self.severity, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class RunPolicy:
    required_files: tuple[str, ...]   # authored before run_mesh may run
    run_timeout: Callable[[], int]    # cap in seconds (callable: may read cfg)
    timeout_hint: str                 # coaching when the run exceeds the cap
    ok_guidance: str                  # next step after a valid run
    fail_label: str                   # what a bare failure is called
    fail_hint: str                    # coaching after an invalid run
    submit_marker: str                # physical deliverable gating submit_mesh
    submit_ok_key: str                # key set true in the submit payload
    submit_hint: str                  # coaching when submit is premature


def _label(name: str) -> str:
    from meshpipeline.contracts.display_names import display_name
    return display_name("mesh_engine", name)


def _mm(v: object) -> str:
    try:
        x = float(v) * 1000.0      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return f"{x:.3g}" if abs(x) < 100 else f"{x:.0f}"


def _crossing_words(report: object) -> str:
    """Where a self-intersection is and how big, in millimetres, from the measured report
    (cad/surface_checks.self_intersection_report) - '' when there is none."""
    if not isinstance(report, dict):
        return ""
    try:
        n = int(report.get("pairs") or 0)
        at = [_mm(v) for v in (report.get("first_at_m") or [])][:3]
        region = [_mm(v) for v in (report.get("region_m") or [])][:3]
    except (TypeError, ValueError):
        return ""
    if n <= 0 or len(at) != 3:
        return ""
    places = f"{n}{' or more' if report.get('more') else ''} place{'s' if n != 1 else ''}"
    where = f" in {places} - first near x={at[0]}, y={at[1]}, z={at[2]} mm"
    if len(region) == 3:
        where += f", within about {region[0]} x {region[1]} x {region[2]} mm"
    return where


def _region_source(evidence) -> str:
    return str((evidence.surface_analysis or {}).get("region_source") or "")


def _region_staged(spec, evidence) -> bool:
    # Whether this engine's staging KEEPS the regions the file distinguishes. A region the staged
    # surface no longer carries is a region the mesher never sees: snappyHexMesh and cfMesh mesh a
    # CAD file as one flattened surface, so the parts a STEP assembly names reach them as one body.
    # An unstated source (a caller that supplied a bare count) is taken as kept, as before.
    source = _region_source(evidence)
    return not source or source in spec.keeps_regions_from


def _region_count(evidence, spec=None) -> int | None:
    facts = evidence.surface_analysis or {}
    if "region_count" not in facts:
        return None
    if spec is not None and not _region_staged(spec, evidence):
        return min(1, int(facts["region_count"]))
    return int(facts["region_count"])


def _region_names(evidence) -> list[str]:
    return [str(n) for n in ((evidence.surface_analysis or {}).get("region_names") or [])]


def _unmatched_walls(evidence, walls: list[str]) -> list[str]:
    # Declared wall patches with no region of that name in the geometry. Names are what the mesher
    # writes the patches as, so a count alone proves nothing: six regions called a..f cannot become
    # "fuselage" and "wing" however many of them there are. Compared as the mesher will write them -
    # mesh-safe and case-blind - because a declared name is made mesh-safe where it enters ("Wing
    # Left" is declared as Wing_Left) and a CAD exporter's capitalisation is not a difference the
    # user meant; the engines stage each region under the declared spelling it matched.
    from meshpipeline.contracts.patch_names import same_patch_name
    available = _region_names(evidence)
    if not available:
        return []
    return [w for w in walls if not any(same_patch_name(w, n) for n in available)]


def _unassigned_regions(evidence, walls: list[str]) -> list[str]:
    # The file's named parts that no declared wall matches. On an engine whose staging writes every
    # part as a wall patch, such a part becomes a patch nobody approved - and nothing but the user
    # can say which wall it belongs to.
    from meshpipeline.contracts.patch_names import same_patch_name
    return [n for n in _region_names(evidence)
            if not any(same_patch_name(w, n) for w in walls)]


def _wall_limit(spec, evidence) -> int | None:
    return spec.boundary_limits_for(topology_of(evidence.purpose or "")).get("wall")


def _supplies_fewer_regions(spec, evidence, walls: list[str]) -> bool:
    # THREE questions, and a refusal needs only one answered no: can this engine deliver several
    # named wall patches at all (for this flow topology), does THIS geometry distinguish enough
    # parts in a form the engine's staging keeps, and are the parts it distinguishes the ones the
    # user asked for? An engine that can do it is never blocked on a file that carries the regions -
    # and no engine can name a part the file does not contain.
    if not spec.supports_multiple_wall_patches:
        return True
    limit = _wall_limit(spec, evidence)
    if limit is not None and len(walls) > limit:
        return True
    count = _region_count(evidence, spec)
    # Unknown says nothing: a caller that supplied no geometry facts is not asserting there are
    # none, and refusing on silence would block a request that may be perfectly deliverable.
    if count is None:
        return False
    if count < len(walls) or _unmatched_walls(evidence, walls):
        return True
    # ...and, where every staged part becomes a wall, is every part one of the declared walls?
    return bool(spec.parts_become_walls and _region_staged(spec, evidence)
                and _unassigned_regions(evidence, walls))


def _wall_patch_cause(spec, evidence, walls: list[str]) -> str:
    # WHOSE limit this is. Separate named patches need an engine this system can drive region-wise
    # AND a geometry that distinguishes the regions. Reporting the engine when the file carries one
    # unnamed body tells a user their setup is unsupported when what they have is a CAD export that
    # named nothing - a different problem, with a different fix, that they own.
    if not spec.supports_multiple_wall_patches:
        return (f"{spec.name} delivers one wall patch for the body. "
                + (spec.single_wall_patch_reason + " " if spec.single_wall_patch_reason else ""))
    limit = _wall_limit(spec, evidence)
    if limit is not None and len(walls) > limit:
        return (f"{spec.name} delivers {limit} wall patch{'es' if limit != 1 else ''} in "
                f"{topology_of(evidence.purpose or '')} flow - everything that is not an opening "
                "is one wall. ")
    if not _region_staged(spec, evidence) and (_region_count(evidence) or 0) > 1:
        return (f"Your CAD file names its parts ({', '.join(_region_names(evidence)[:8])}), but "
                f"{spec.name} meshes a CAD file as one surface, so the parts reach it as one "
                "body. An STL export whose parts are separate named solids keeps them apart. ")
    count = _region_count(evidence)
    if count is not None and count <= 1:
        return ("Your geometry is one region with no component names, so nothing downstream can "
                f"tell the parts apart - {spec.name} itself can keep named regions separate when "
                "the file distinguishes them. ")
    offered = ", ".join(_region_names(evidence)[:8])
    if count is not None and count < len(walls):
        return (f"Your geometry distinguishes {count} regions ({offered}), fewer than the "
                f"{len(walls)} wall patches you declared. ")
    missing = _unmatched_walls(evidence, walls)
    unassigned = _unassigned_regions(evidence, walls) if spec.parts_become_walls else []
    if not missing and unassigned:
        return (f"Your geometry names its parts {offered}; the walls you declared cover "
                f"{len(_region_names(evidence)) - len(unassigned)} of them, so "
                f"{', '.join(unassigned)} would become a patch nobody approved. Add a wall patch "
                "for each of those parts too, if they are meant to be kept apart. ")
    # A count that fits but names that do not: the mesher writes each patch under its REGION's
    # name, so a part the file never names cannot come back under a name the user chose. Saying
    # which names the file does offer is the actionable half - they rename their declaration to
    # match the CAD, or fix the export.
    return (f"Your geometry names its parts {offered}, which does not include "
            f"{', '.join(missing)}. Patches are written under the names the file carries. ")


@dataclass(frozen=True)
class EngineSpec:
    name: str
    # The registry KEY. Internal: it is the tool enum, the workspace marker and the record
    # field, and it is not English - `snappy_multiregion` must never reach a user.
    descriptor: str                      # condensed capability gist (intake menu)
    _load_prompt: Callable[[], str]
    _load_tools:  Callable[[], set[str]]
    # Two-state contract: every row in the release registry is implemented AND
    # T5-backed - this flag stays as the selection-surface guard (engine_names/
    # catalog_menu filter on it), not as an invitation to register planned rows.
    # What this engine is CALLED in front of a user: the name its own documentation uses.
    # Every user-facing surface renders this; `name` stays the internal key.
    implemented: bool = True
    # TWO-STATE CONTRACT: a capability either appears in `capabilities` - meaning it is
    # production-supported, T5-backed, artifact-reconciled and selectable - or it does
    # not exist as a capability and admission rejects the request. There is no
    # experimental / wrapper-supported / hidden middle state; unfinished work lives on
    # a development branch or in roadmap prose, never in this registry.
    validation_notes: str = ""              # evidence citations for the supported capabilities
    # Export formats this engine can DELIVER, first = native default. This is
    # the user-facing "which language do you want your mesh in" axis - engines
    # decide what the mesh IS; formats decide what file it becomes. The future
    # cross-format layer builds on meshio (PINNED/vendored - format breadth is
    # unmatched but upstream is frozen since 2024) + actively-maintained
    # PyVista/VTK for VTK-family output + the Gmsh API for .msh.
    export_formats: tuple = ("openfoam_polymesh",)
    # Engine-NATIVE intake questions: once the user picks this engine, intake
    # asks in the engine's own concepts (a snappy user thinks in
    # snappyHexMeshDict terms) - never through a generic abstraction layer.
    intake_guidance: str = ""
    # SOFT LIMITATIONS the engine can HIT but does not ALWAYS hit - the "may occur",
    # not the "will always fail" that admission rejects. Each is a short, user-facing
    # statement of an ENGINE-MECHANICAL tendency (how the mesher works and where it
    # struggles), NOT domain-physics advice. intake surfaces the relevant ones as a
    # NON-BLOCKING heads-up before a long run, so a case like "wall-resolved y+~1 prism
    # coverage on a complex aircraft junction with snappy" is flagged up front instead of
    # discovered after a 2-hour build. Advisory only: never a gate (that is admit()), and
    # never a hardcoded request→outcome lookup (the LLM applies judgement to the user's
    # actual request - the retired RAG/playbook system is not coming back).
    intake_advisories: tuple[str, ...] = ()
    # Declared intake parameters (consumers: intake mechanical validation,
    # worker resolve_engine_params, engine resolution via engines_allowing).
    intake_params: tuple = ()
    # (Region-contract vocabulary moved OFF the engine - it is owned by the
    #  user-declared Purpose now; see engines.purposes. Engines are physical tools.)
    # PHYSICAL CAPABILITY - the (submitted geometry → produced mesh) records this
    # engine supports (MeshCapability tuple). gmsh supports BOTH solid-body→
    # solid-volume and fluid-domain→fluid-volume, so it serves structural AND CFD;
    # the flow engines support only body-surface→fluid-volume. A Purpose's
    # requires_mesh_kind is matched against these outputs for the compatibility gate
    # (engines.purposes.is_compatible) - declared capability, NOT a domain claim.
    capabilities: tuple = ()
    #: WHAT FILE THIS BUNDLE CAN TAKE, PER FLOW (FlowSupport tuple): the flows it is designed for
    #: and, for each, the canonical geometry forms ("cad", "surface") its staging consumes. The
    #: one declaration every offer of an engine is derived from (engines/capability.py). Every
    #: implemented engine declares it, and its flows agree with `capabilities` (test-enforced).
    accepts: tuple[FlowSupport, ...] = ()
    #: WHERE THIS ENGINE STANDS ON THE FALLBACK LADDER, lowest first: the most robust input
    #: handling first, then the body-fitted hex meshers, then the tetrahedral engines. A new
    #: engine that declares nothing goes last. The ladder and the start-of-run choice read it
    #: (capability.ladder_order); nothing lists engines by hand.
    ladder_rank: int = 100

    def forms_for(self, flow: str) -> tuple[str, ...] | None:
        """The geometry forms this bundle consumes for `flow`, or None when it is not designed
        for that flow at all.

        INTERNAL FLOW FROM A SURFACE is declared in ONE place, the input contract's
        `InputContract.internal_from_surface` (the shared internal-surface staging,
        cad/internal_surface): an engine that declares it takes a surface for internal flow. Its
        `accepts` internal row names the CAD form only, so the fact is never stated twice."""
        for fs in self.accepts:
            if fs.flow == flow:
                forms = tuple(fs.forms)
                if (flow == "internal" and "surface" not in forms
                        and self.input_contract is not None
                        and self.input_contract.internal_from_surface):
                    forms = (*forms, "surface")
                return forms
        return None
    # THE THREE AUDIT SEAMS (stage C): builder briefing fragments, the user
    # deliverable recipe, and the run/submit tool policy. Implemented rows
    # must declare all three (enforced); planned rows leave them None.
    briefing: Briefing | None = None
    deliverable: Deliverable | None = None
    run_policy: RunPolicy | None = None
    # DOWNSTREAM TARGET - what the delivered mesh FEEDS (solver + analysis class).
    # Structures descriptor prose so intake names the user's next step from a
    # declaration, not hardcoded OpenFOAM strings (consumer: agents.intake.outcome).
    downstream: DownstreamTarget | None = None
    # WHAT THE DELIVERED MESH IS (cells, wall fit, prism layers) - the terms the fallback ladder
    # compares two engines in before it lets one stand in for the other. Implemented rows declare
    # it (test-enforced); an engine without it is never switched to without asking.
    delivered_mesh: DeliveredMesh | None = None
    # Optional engine-owned hooks (consumers: builder_tools run_mesh
    # enrichment; agents.builder.agent build dispatch; the viewer surface endpoint).
    # None → generic behavior.
    _load_run_enricher: Callable[[], Callable | None] = lambda: None
    _load_build_driver: Callable[[], Callable | None] = lambda: None
    _load_viewer_surface: Callable[[], Callable | None] = lambda: None
    # REVIEW RENDERER - the engine's own answer to "how is this mesh shown to a reviewer".
    # Same lazy-load precedent as viewer_surface, and for the same reason: the module pulls a
    # render stack, and a spec must stay importable without it.
    # This exists because rendering-for-review was the ONE review-critical capability that was
    # not engine-owned: a single global gmsh-coupled renderer, with `visual_review: bool` as
    # the escape hatch for the engines it could not serve. Every other such capability - gates,
    # criteria, the deliverable bundle, the browser viewer surface, the review rubric - is
    # declared here. A future engine becomes reviewable inside its own bundle.
    # Capability is DECLARED, never inferred. Not from the engine name, not from
    # `visual_review`, not from a stray mesh.msh on disk, and not from the browser viewer
    # (which produces polygon soup for vtk.js - same artifacts, a different product).
    _load_review_renderer: Callable[[], ReviewRenderer | None] = lambda: None
    # What the renderer needs, BY MANIFEST KEY - never a workspace path the engine composed.
    # The sandbox resolves and confines it (see contracts.review_evidence).
    render_artifacts: tuple[RenderArtifactRequirement, ...] = ()
    # The engine's stable inspection targets - the coverage floor the reviewer must satisfy.
    inspection_targets: tuple[InspectionTarget, ...] = ()
    # ENGINE-OWNED APPLICABILITY. The RESOLVED target obligations a given job must satisfy, from
    # trusted job data (manifest, engine_params, purpose) - NOT from what runtime discovery found.
    # This is the guard against a kind-only check becoming a loophole: an engine materializes the
    # STRONGEST obligation it can (exact ids, an expected count, else kind-only), so PARTIAL loss
    # (3 groups expected, 1 discovered) cannot slip through. Default (below) = a kind-only
    # obligation per inspection target this engine declares required; an engine that can assert
    # something stronger, or whose applicability is conditional, provides its own resolver.
    _load_target_obligations: Callable[[], Callable[..., tuple] | None] = lambda: None

    def expected_target_obligations(self, manifest=None, engine_params=None,
                                    purpose: str = "") -> tuple:
        from meshpipeline.contracts.evidence_ledger import TargetObligation
        resolver = self._load_target_obligations()
        if resolver is not None:
            return tuple(resolver(manifest or {}, engine_params or {}, purpose))
        return tuple(TargetObligation(kind=t.kind, provenance=f"{t.target_id} (declared required)")
                     for t in self.inspection_targets if t.required)

    @property
    def review_renderer(self):
        return self._load_review_renderer()

    @property
    def renders_for_review(self) -> bool:
        return self._load_review_renderer() is not None

    @property
    def viewer_surface(self):
        return self._load_viewer_surface()

    @property
    def run_enricher(self):
        return self._load_run_enricher()

    @property
    def build_driver(self):
        return self._load_build_driver()

    # (Review modality is no longer an authored flag. Every engine enters ONE unified
    # hybrid-assurance review; whether rendered evidence is obliged is DERIVED -
    # AssurancePlan.requires_render, from typed requirements + renderer capability.
    # `visual_review` was deleted in the C2 cutover.)
    # WHY this engine's review emphasis is the right one - a one-line justification,
    # still required of every implemented engine (enforced in test_engine_catalog).
    review_rationale: str = ""
    # VALIDATION COVERAGE - the standardized axes (ValidationAxis) this engine's
    # meshes are checked on. Part of the engine PACKAGE: every implemented engine
    # declares ALL FOUR (a concrete check, or a justified applicable=False), so a
    # mesh is validated to the fullest extent no matter the engine selected
    # (enforced in test_engine_catalog). This is the DECLARATION that maps onto the
    # implementation - the gate chain, the solvability solve, the extent gate.
    validation_coverage: tuple = ()

    @property
    def validation_axes(self) -> set:
        return {c.axis for c in self.validation_coverage}

    # INPUT CONTRACT - the geometry this engine can CONSUME (the mirror of
    # validation_coverage). Enforced up front: the dimensionality axis at intake
    # (agents.intake.validation), the measured thinness axis at the builder's first
    # geometry inspection (geometry_unsuitable, below). Implemented engines declare
    # it (completeness-enforced in test_engine_catalog).
    input_contract: InputContract | None = None

    # PRODUCIBLE PATCH TYPES - a `symmetry` role is a valid EXTERNAL/INTERNAL CFD boundary
    # (it lives in the purpose vocabulary), but producing one requires the engine to mesh
    # only half the domain with a symmetryPlane on the cut. Only snappy does (a half model's cut,
    # a slab's two ends); an engine whose box is all far field would mesh, fail the patch
    # contract with "zero faces: [symmetry]", and burn every retry. Declared here (default
    # False) so intake rejects it BEFORE any compute. An engine that flips it True must also
    # record the planes it places in the manifest (geometry.symmetry_faces), or the extent gate
    # will judge the cut as far field touching the body.
    supports_symmetry_plane: bool = False
    # GROUND PLANE - a body standing on the ground (a car on a road) needs the far-field box's
    # floor laid at the body's lowest point and written as a wall patch named ground
    # (engines.ground_plane). That is the box builder's job, not the geometry's. Default False:
    # an engine whose box is all far field would deliver the ground with zero faces after a full
    # build, so intake refuses it up front, with the way on stated.
    supports_ground_plane: bool = False
    # Can this engine's workflow emit MORE THAN ONE distinct wall patch - i.e. separate
    # a body into named wall regions (wing vs fuselage)? Default True: engines that split
    # on named surface regions / groups (cfMesh solids, gmsh groups) can. snappy's external
    # workflow wraps a single input surface as ONE wall patch and cannot separate it, so it
    # declares False - and a request for two wall patches on it is rejected UP FRONT
    # (declared phase) instead of building for hours and failing the patch contract.
    #: Can THIS BUNDLE deliver several separately named wall patches for one body? A capability
    #: claim about the whole path - the mesher, the surface this system prepares for it, and the
    #: dict it authors - not about the upstream tool's documented features. Flip it when the path
    #: is proven end to end, and only then; every engine states it explicitly rather than
    #: inheriting a default, so a claim is always something someone decided.
    supports_multiple_wall_patches: bool = True
    #: WHY this engine yields one wall patch, in its own words - required when the flag is False.
    #: The rule below is generic and cannot know whether the mesher is incapable or this system's
    #: path to it cannot supply named regions, and naming the wrong one sends a user to revise a
    #: requirement that was fine.
    single_wall_patch_reason: str = ""
    #: Whether this bundle's staging writes EVERY named part of the input surface as a wall patch
    #: (declared_boundary.stage_regions: snappy, cfMesh). With several walls declared, a part none
    #: of them matches would become a patch nobody approved, so admission asks about it. An engine
    #: that assigns surfaces to groups itself (gmsh) leaves such parts to its default group.
    parts_become_walls: bool = False
    #: Which kinds of region structure this bundle's STAGING keeps apart (cad/regions.py's
    #: region_source: "assembly" and "roots" for a CAD file's named parts, "stl-solids" for an STL's
    #: named solids). A region the staged surface no longer carries is one the mesher never sees, so
    #: the wall-arity rule counts it as merged. Every source by default.
    keeps_regions_from: tuple[str, ...] = ("assembly", "roots", "stl-solids")
    #: The UPLOADED FORMATS this engine reads itself (contracts/intake_formats keys), not through
    #: the canonical STEP/STL every other engine reads. Empty = any accepted format (the canonical
    #: form is what it meshes). Non-empty = only these: the snap-grid mesher reads the ECXML
    #: thermal model's own boxes and cylinders, so a STEP or an STL gives it nothing to place.
    #: Admission refuses another format from the upload's facts (cad/regions.py source_format).
    reads_source_formats: tuple[str, ...] = ()
    #: THE BOUNDARIES THIS BUNDLE'S CASE WRITER CAN BUILD, per flow topology: role -> the most
    #: patches of that role it delivers (0 = none). A role the purpose allows but this engine's path
    #: for that topology cannot build - a second far-field patch on a box that is one surface, a
    #: symmetry plane inside a carved duct - comes back with zero faces after a full run, so
    #: admission refuses it up front with the way on stated. A role not listed is not limited here.
    boundary_limits: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()

    def boundary_limits_for(self, topology: str) -> dict[str, int]:
        for topo, limits in self.boundary_limits:
            if topo == topology:
                return dict(limits)
        return {}

    # USER-BOUNDARY CONTRACT PARITY (application-owned invariant): an engine that consumes the
    # user's approved intake_patches MUST prove the delivered mesh preserves them (no merge, rename,
    # drop, or re-role) via a gate that runs the shared engines.contract.check_contract. This field
    # names that gate's key; a registry-driven test asserts the key is present in the executable
    # gate chain. An engine that genuinely does NOT consume patch declarations opts out EXPLICITLY
    # by setting `user_contract_optout_reason` instead - silence is a test failure, not a default.
    user_contract_gate_key: str | None = "patch_contract"
    user_contract_optout_reason: str = ""

    def param_problems(self, given: dict | None) -> list[str]:
        known = {p.key for p in self.intake_params}
        problems = [f"unknown param {k!r} for engine {self.name!r}"
                    for k in (given or {}) if k not in known]
        for p in self.intake_params:
            v = str((given or {}).get(p.key, "") or "").strip().lower()
            if not v:
                if p.required:
                    problems.append(f"missing required param {p.key!r} - ask the user: {p.ask}")
            elif v not in p.values:
                problems.append(f"param {p.key!r} must be one of {list(p.values)}, got {v!r}")
        return problems

    def admit(self, evidence) -> tuple:
        out: list = []
        out.extend(self._admit_declared(evidence))
        if evidence.surface_analysis is not None:
            out.extend(self._admit_measured(evidence))
        return tuple(out)

    def _admit_declared(self, evidence) -> list:
        from meshpipeline.engines.admission import Rejection

        out: list = []
        purpose = evidence.purpose or ""
        ik = evidence.input_kind

        # (engine × purpose × input) CAPABILITY - the core "can it produce this at all". The file's
        # form is passed so a surface for internal flow is read as the one input it is, whichever
        # kind the user confirmed (purposes.kinds_admitted_as).
        if purpose in PURPOSES:
            if not is_compatible(self, purpose, ik, form=str(evidence.geometry_form or "")):
                if not is_compatible(self, purpose):
                    out.append(Rejection(
                        code="purpose_incompatible", phase="declared", field="purpose",
                        actual=purpose,
                        message=f"{self.name} cannot produce a {purpose} mesh - it is "
                                "physically incapable of it.",
                        fix_hint="use an engine whose capability produces this purpose's mesh kind"))
                elif ik:
                    req = PURPOSES[purpose].requires_mesh_kind
                    req = (req,) if isinstance(req, str) else tuple(req)
                    topo = PURPOSES[purpose].flow_topology
                    # only what this engine takes for THIS flow: gmsh takes a body for external
                    # flow, and "submit a body-surface" to a body-surface internal case was a lie
                    ok = sorted({c.input_kind for c in self.capabilities if c.output_kind in req
                                 and (not topo or topo in c.topologies)})
                    out.append(Rejection(
                        code="input_kind_incompatible", phase="declared", field="input_kind",
                        actual=ik, expected=ok,
                        message=f"{self.name} cannot produce a {purpose} mesh from a '{ik}' "
                                f"geometry - for {purpose} on {self.name}, submit a "
                                f"{' or '.join(ok)} geometry (no fluid-domain generation/prep "
                                "step exists to derive it here).",
                        fix_hint=f"submit a {' or '.join(ok)} geometry"))

        # THE FILE'S FORM this bundle can take for this flow (declared in `accepts`). A surface
        # for internal flow on an engine whose internal carve needs a CAD solid is refused HERE,
        # before anything is built - not by the driver minutes in, where it read as a crash and
        # was retried unchanged (aorta STL, 2026-10-03). Unknown form or flow: nothing is claimed.
        from meshpipeline.engines.capability import flow_of, form_words, refusal
        _flow, _form = flow_of(purpose), str(evidence.geometry_form or "")
        _forms = self.forms_for(_flow) if _flow else None
        if _form and _forms is not None and _form not in _forms:
            out.append(Rejection(
                code="geometry_form_unsupported", phase="declared", field="geometry",
                actual=_form, expected=list(_forms),
                message=refusal(self, _flow, _form),
                fix_hint=(f"supply {' or '.join(form_words(f) for f in _forms)}, or use an "
                          f"engine that takes {form_words(_form, short=True)} for this flow")))

        # PATCH NAMES the mesher can write. The intake makes every name mesh-safe where it enters;
        # this is the same rule at the last gate before a mesh runs, so a name that slipped past
        # (an old snapshot, a direct API caller) costs one refusal instead of a whole run that
        # ends with "patch has zero faces".
        from meshpipeline.contracts.patch_names import (
            is_mesh_safe,
            is_reserved,
            mesh_safe,
            unreserved,
        )
        for p in evidence.patches:
            if p.name and not is_mesh_safe(p.name):
                out.append(Rejection(
                    code="patch_name_unsafe", phase="declared", field="patches",
                    actual=p.name, expected=mesh_safe(p.name),
                    message=f"the patch name {p.name!r} cannot be written into a mesh - a boundary "
                            "name must start with a letter and hold only letters, digits and "
                            f"underscores. Use {mesh_safe(p.name)!r} instead.",
                    fix_hint=f"rename the patch to {mesh_safe(p.name)!r}"))
            elif p.name and is_reserved(p.name):
                # the mesher writes a boundary of its own under this name, and one of the two
                # would come back empty or merged
                out.append(Rejection(
                    code="patch_name_reserved", phase="declared", field="patches",
                    actual=p.name, expected=unreserved(p.name),
                    message=f"the patch name {p.name!r} is one the meshers use for a boundary of "
                            f"their own. Use {unreserved(p.name)!r} instead.",
                    fix_hint=f"rename the patch to {unreserved(p.name)!r}"))

        # BOUNDARIES THIS ENGINE'S CASE WRITER CAN BUILD for this flow topology (declared on the
        # spec). Walls are counted by the wall-arity rule below, which also knows the geometry.
        _topology = topology_of(purpose)
        _limits = self.boundary_limits_for(_topology)
        _counts: dict[str, list[str]] = {}
        for p in evidence.patches:
            _counts.setdefault(p.type, []).append(p.name)
        for role, most in sorted(_limits.items()):
            names = _counts.get(role, [])
            if role == "wall" or len(names) <= most:
                continue
            if most == 0:
                msg = (f"a '{role}' patch was declared ({', '.join(names)}), but {self.name} "
                       f"cannot build a {role} boundary in {_topology} flow. Drop it, or use an "
                       f"engine that builds one.")
                hint = f"drop the '{role}' patch"
            else:
                msg = (f"{len(names)} '{role}' patches were declared ({', '.join(names)}), but "
                       f"{self.name} builds at most {most} in {_topology} flow - "
                       + ("its far field is one box around the body, so it is one patch. "
                          if role == "farfield" else "")
                       + f"Declare {'ONE' if most == 1 else most} '{role}' patch"
                       + ("" if most == 1 else "es") + ".")
                hint = f"declare at most {most} '{role}' patch" + ("" if most == 1 else "es")
            out.append(Rejection(
                code="boundary_count_unsupported", phase="declared", field="patches",
                actual=names, expected=most, message=msg, fix_hint=hint))

        # DIMENSIONALITY the engine can consume (declared InputContract axis).
        ic = self.input_contract
        if evidence.dimensionality and ic is not None and \
                evidence.dimensionality not in ic.dimensionalities:
            out.append(Rejection(
                code="dimensionality_unsupported", phase="declared", field="dimensionality",
                actual=evidence.dimensionality, expected=list(ic.dimensionalities),
                message=f"{self.name} cannot mesh {evidence.dimensionality} geometry - it "
                        f"supports {', '.join(ic.dimensionalities)}.",
                fix_hint="use an engine that supports this dimensionality"))

        # ...and the capability that serves THIS purpose from THIS geometry must serve it too
        if (evidence.dimensionality and purpose in PURPOSES and ik
                and not any(r.code == "dimensionality_unsupported" for r in out)):
            from meshpipeline.engines.purposes import serving_capabilities
            caps = serving_capabilities(self, purpose, ik)
            if caps and all(c.dimensionalities and evidence.dimensionality not in c.dimensionalities
                            for c in caps):
                dims = sorted({d for c in caps for d in c.dimensionalities})
                out.append(Rejection(
                    code="dimensionality_unsupported", phase="declared", field="dimensionality",
                    actual=evidence.dimensionality, expected=dims,
                    message=f"{self.name} cannot produce a {purpose} mesh from {evidence.dimensionality} "
                            f"'{ik}' geometry - it does that in {', '.join(dims)} only.",
                    fix_hint="use an engine that supports this dimensionality"))

        # SYMMETRY-plane production - a declared symmetry patch needs half-domain meshing.
        if any(p.type == "symmetry" for p in evidence.patches) and not self.supports_symmetry_plane:
            out.append(Rejection(
                code="symmetry_unsupported", phase="declared", field="patches",
                message=f"a 'symmetry' patch was declared, but the {self.name} engine does not "
                        "produce symmetry-plane patches yet. For a full-span body, mesh the whole "
                        "domain with wall + farfield and drop 'symmetry'; for a half-model, "
                        "symmetry-plane meshing is not yet available on this engine.",
                fix_hint="drop 'symmetry' (full domain) or use an engine with symmetry-plane meshing"))

        # GROUND PLANE - a wall named ground in an external flow is the floor of the far-field
        # box. The DOMAIN makes it, so it is not a region the geometry has to supply, and only an
        # engine whose box builder lays that floor can deliver it.
        _ground = (ground_patch_name(evidence.patches)
                   if topology_of(purpose) == "external" else None)
        if _ground and not self.supports_ground_plane:
            out.append(Rejection(
                code="ground_plane_unsupported", phase="declared", field="patches",
                actual=_ground,
                message=f"a ground plane was declared ('{_ground}'), but {self.name} cannot lay "
                        "the far-field box's floor under the body as a wall - every face of its "
                        "box is far field. To proceed, mesh the body free in the flow (drop "
                        f"'{_ground}'), or use an engine that builds a ground plane.",
                fix_hint=f"drop '{_ground}' (body free in the flow) or use an engine that builds "
                         "a ground plane"))

        # WALL-PATCH ARITY this setup can produce - the engine and the geometry together. A patch
        # the pair cannot deliver comes back with zero faces, and the patch contract rejects it
        # AFTER a full build; catching it here, from declared metadata, fails in seconds instead.
        # (An IMPOSSIBILITY of the declared combination, not a quality or feasibility judgement -
        # exactly what admission is for.) The ground plane is not counted: the domain makes it,
        # so a one-region car with car + ground + farfield asks the geometry for ONE wall.
        _walls = [p.name for p in evidence.patches if p.type == "wall"
                  and not (_ground and is_ground(p.name, p.type))]
        if len(_walls) > 1 and _supplies_fewer_regions(self, evidence, _walls):
            out.append(Rejection(
                code="multiple_wall_patches_unsupported", phase="declared", field="patches",
                actual=_walls,
                message=f"you declared {len(_walls)} wall patches ({', '.join(_walls)}), which "
                        f"cannot be delivered here. " + _wall_patch_cause(self, evidence, _walls)
                        + "Declare ONE wall patch for the whole body to proceed.",
                fix_hint="declare a single wall patch for the body"))

        # STRUCTURAL patch requirements of a FLOW mesh (purpose-driven, knowable from declared
        # patches) - skipped when no patches are declared yet (intake flags the shape first).
        is_flow = purpose in PURPOSES and PURPOSES[purpose].requires_mesh_kind == "fluid-volume"
        if is_flow and evidence.patches:
            types = {p.type for p in evidence.patches}
            if "wall" not in types or (_ground and not _walls):
                out.append(Rejection(
                    code="missing_wall_patch", phase="declared", field="patches",
                    message="patches must include at least one entry of type 'wall' (the solid surface)."
                            + (f" '{_ground}' is the floor the domain builds, not the body - "
                               "add a wall patch for the body itself." if _ground else ""),
                    fix_hint="add a wall patch"))
            if not (("inlet" in types and "outlet" in types) or "farfield" in types):
                out.append(Rejection(
                    code="missing_flow_boundaries", phase="declared", field="patches",
                    message="patches must include either both 'inlet'+'outlet' or a single "
                            "'farfield' (the flow boundaries).",
                    fix_hint="add inlet+outlet, or a farfield"))

        # 2D/3D empty-patch consistency (OpenFOAM convention; needs dimensionality + patches).
        roles = set(PURPOSES[purpose].boundary_roles) if purpose in PURPOSES else set()
        types = {p.type for p in evidence.patches}
        if (evidence.dimensionality == "2D" and "empty" in roles
                and evidence.patches and "empty" not in types):
            out.append(Rejection(
                code="missing_empty_patch_2d", phase="declared", field="patches",
                message="dimensionality is '2D' but no patch has type 'empty'. True 2D OpenFOAM "
                        "cases require the front/back faces of the thin spanwise slab to be a "
                        "SINGLE patch of type 'empty' (e.g. 'frontAndBack'). Either add such a "
                        "patch or declare the case '3D' (a thin slab meshed in full 3D uses "
                        "symmetry patches, not 'empty').",
                fix_hint="add an 'empty' front/back patch or switch dimensionality"))
        if evidence.dimensionality == "3D" and "empty" in types:
            out.append(Rejection(
                code="empty_patch_in_3d", phase="declared", field="patches",
                message="dimensionality is '3D' but patches include a type 'empty' entry. "
                        "'empty' is OpenFOAM's 2D-case convention and only applies when "
                        "dimensionality='2D'. For a 3D case, remove the 'empty' patch (or switch "
                        "dimensionality to '2D').",
                fix_hint="remove the 'empty' patch or switch dimensionality to '2D'"))

        # engine_params validity against THIS engine's declared ParamSpecs - run even for an
        # empty mapping so a MISSING required param is caught, not just a bad value.
        for p in self.param_problems(dict(evidence.engine_params)):
            out.append(Rejection(
                code="engine_param_invalid", phase="declared", field="engine_params",
                message=f"engine_params: {p}"))
        return out

    def _admit_measured(self, evidence) -> list:
        from meshpipeline.engines.admission import Rejection
        facts = evidence.surface_analysis or {}
        # A format this engine does not read is a physical impossibility, not a quality call. Only
        # judged when the facts NAME the upload's format: an older caller that never measured it
        # says nothing, and the build driver refuses a missing source on its own.
        fmt = str(facts.get("source_format") or "")
        if self.reads_source_formats and fmt and fmt not in self.reads_source_formats:
            wants = " or ".join(self.reads_source_formats).upper()
            return [Rejection(
                code="source_format_unsupported", phase="measured", field="geometry",
                actual=fmt, expected=list(self.reads_source_formats),
                message=f"{self.name} reads only {wants} files - it meshes the model the file "
                        f"describes (its parts, their materials and positions), and a "
                        f"{fmt.upper()} file carries no such model.",
                fix_hint=f"upload the {wants} export of this model")]
        reason = self.geometry_unsuitable(facts)
        if reason:
            return [Rejection(code="geometry_unsuitable", phase="measured", field="surface_analysis",
                              message=reason,
                              fix_hint="repair or replace the input surface upstream")]
        return []

    def geometry_unsuitable(self, analysis: dict) -> str:
        ic = self.input_contract
        if ic is None:
            return ""
        # A self-intersecting surface cannot bound a volume - a fill engine physically
        # cannot mesh it, so it is rejected here rather than after wasted build attempts.
        # The flag is set upstream (builder geometry_report) only for engines that require it.
        if ic.require_no_self_intersection and analysis.get("self_intersecting"):
            return ("[GEOMETRY_UNSUITABLE] the input surface self-intersects"
                    + _crossing_words(analysis.get("self_intersection"))
                    + f": there its triangles pass through each other, so it encloses no single "
                    f"volume and {_label(self.name)} cannot fill it. No mesh parameter changes "
                    "that. Fix the surface where it crosses - in a mesh tool, delete the crossing "
                    "triangles and close the gap (MeshLab's self-intersecting-face selection, "
                    "then Close Holes) - or export it again from the source model.")
        if ic.min_thickness_ratio <= 0:
            return ""
        diag = float(analysis.get("diag") or 0.0)
        thin = analysis.get("thin_gap")
        if diag > 0 and thin is not None:
            ratio = float(thin) / diag
            if ratio < ic.min_thickness_ratio:
                return (f"[GEOMETRY_UNSUITABLE] thinnest feature is {ratio:.4f}×the body "
                        f"diagonal - below {self.name}'s {ic.min_thickness_ratio:.4f} floor. "
                        f"{ic.rationale}")
        return ""

    # Declared executor gates (GateSpec tuple) - the deterministic blocking
    # verification chain for this engine's meshes; the executor iterates
    # whatever is declared here (consumer: pipeline.executor via run_gates).
    # Loaded lazily to keep the catalog import-light for planned rows.
    _load_gates: Callable[[], tuple] = lambda: ()

    # AUTHORING SURFACE - the engine's OWN configure_mesh block palette (the tool
    # schema the model fills) + its validator. Owned by the engine so the AGENT
    # knows zero mesh vocabulary: the builder composes the configure_mesh tool from
    # `authoring_tool` and validates the model's strategy via `validate_authoring`.
    # None → the engine authors its spec another way (gmsh: write_file + driver).
    _load_authoring_tool: Callable[[], dict | None] = lambda: None
    _load_authoring_validate: Callable[[], Callable | None] = lambda: None
    # measured-scale → recommended-strategy translation, in the engine's OWN palette
    # vocabulary (cfMesh reads SIZES, snappy reads LEVELS/bands). Keeps measure_scales
    # engine-agnostic: it measures the body, the ENGINE phrases the recommendation.
    _load_recommend: Callable[[], Callable | None] = lambda: None
    # REVIEW RUBRIC - the engine's MESH-CLASS review axes (ReviewAxis tuple): what THIS
    # mesh class can fail at, phrased as interpretation-needing concerns (not thresholds
    # - those are criteria.py). The reviewer runtime unions these with the user-declared
    # Purpose's use-case axes; the shared reviewer PROMPT stays neutral (no engine/domain
    # persona). Used by BOTH protocols (visual walks them with renders, metric with
    # evidence). Every implemented engine declares a non-empty rubric (test-enforced).
    _load_review_rubric: Callable[[], tuple] = lambda: ()
    # CRITERIA - the engine's machine-checkable production-grade rows (Criterion
    # tuple, engines/<name>/criteria.py). Exposed on the SPEC so the shared QA
    # machinery (engines.quality_criteria) asks the engine instead of keeping a
    # hand-edited engine→rows dict that a new engine could silently miss. Every
    # implemented engine declares non-empty rows (test-enforced).
    _load_criteria: Callable[[], tuple] = lambda: ()
    # WORKSPACE SCAFFOLD - engine-owned case skeleton written into every fresh
    # attempt workspace (flow engines: the OpenFOAM system/ dicts; gmsh: nothing).
    # None → no scaffold. Shared workspace setup owns only generic mechanics.
    _load_workspace_scaffold: Callable[[], Callable | None] = lambda: None

    @property
    def gates(self) -> tuple:
        return self._load_gates()

    @property
    def criteria(self) -> tuple:
        return tuple(self._load_criteria())

    def scaffold_workspace(self, workspace) -> None:
        _s = self._load_workspace_scaffold()
        if _s:
            _s(workspace)

    @property
    def authoring_tool(self) -> dict | None:
        return self._load_authoring_tool()

    def validate_authoring(self, strategy: dict) -> list:
        _v = self._load_authoring_validate()
        return list(_v(strategy)) if _v else []

    def recommend_authoring(self, analysis: dict, *, fidelity: str = "") -> dict:
        from meshpipeline.pipeline.enums import authoring_tier
        _r = self._load_recommend()
        if not _r:
            return {}
        tier = authoring_tier(fidelity).value
        try:
            return dict(_r(analysis, fidelity=tier))
        except TypeError:
            # An engine recommender that does not (yet) take a tier keeps working unchanged.
            return dict(_r(analysis))

    @property
    def review_rubric(self) -> tuple:
        return tuple(self._load_review_rubric())

    @property
    def system_prompt(self) -> str:
        return self._load_prompt()

    @property
    def tool_names(self) -> set[str]:
        return self._load_tools()


@runtime_checkable
class MeshEngine(Protocol):

    name: str

    # geometry
    # `context` is the builder's typed execution geometry (BuilderToolContext). Every engine
    # accepts the SAME shape, so the seam that decides physical size is uniform across bundles
    # rather than five slightly different arrangements. Optional at the protocol so a caller that
    # genuinely has no geometry - a programmatic submit - is expressible; a bundle that needs it
    # refuses rather than inventing a scale.
    def inspect_stl(self, workspace, geometry_file: str = "input.stl", *,
                    context=None) -> dict: ...
    #: `prepared` is the typed OCC-output coordinate state. Every bundle accepts it; the ones
    #: whose native path is still unit-blind say so at their own definition.
    def tessellate_to_stl(self, geom_path, out_stl, *, context=None, prepared=None) -> Path: ...
    def read_stl_solids(self, path: Path) -> dict: ...

    # meshing
    def prepare_surface(self, workspace, *, geometry_file: str, domain_min, domain_max,
                        wall_patch: str, farfield_patch: str,
                        feature_angle: float = 30.0, mirror_y_half: bool = False) -> dict: ...
    def run_cartesian_mesh(self, workspace, *, timeout: int, context=None) -> dict: ...
    def check_mesh(self, workspace) -> dict: ...

    # review / artifacts
    def export_volume_vtk(self, workspace) -> str | None: ...
    def build_review_msh(self, workspace, patches_tris: dict): ...
    def write_manifest(self, workspace, **kwargs): ...
    def finalize(self, workspace_dir, intake_patches, engine, domain, internal_flow, engine_params): ...


