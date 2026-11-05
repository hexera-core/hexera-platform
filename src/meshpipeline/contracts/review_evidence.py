# Responsibility: Declare what an engine requires a reviewer to look at, and what a renderer may be asked for.
# Owns: artifact requirements, inspection targets, view specifications, and the renderer vocabulary.
# Boundaries: requirements only; it resolves no path, renders nothing, and judges nothing.
# Collaborates with: engines/*/spec.py which declare requirements, and sandbox/ which satisfies them.
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# artifacts: declared by KEY, resolved by the trusted layer


class ArtifactFormat(str, Enum):

    GMSH_MSH = "gmsh_msh"
    VTK_VTP = "vtk_vtp"
    VTK_VTU = "vtk_vtu"
    STL = "stl"
    OPENFOAM_POLYMESH = "openfoam_polymesh"
    JSON = "json"


@dataclass(frozen=True)
class RenderArtifactRequirement:

    artifact_key: str                       # e.g. "mesh_paths.surface" - a manifest key
    allowed_formats: tuple[ArtifactFormat, ...]
    purpose: str                            # why this renderer needs it, in engine terms
    required: bool = True                   # False => a supporting artifact; absence degrades


# what an engine declares it can show


class TargetKind(str, Enum):

    PATCH = "patch"                  # a named boundary patch (the OpenFOAM engines)
    REGION = "region"                # an internal inspection slice
    GROUP = "group"                  # an FEA physical group (gmsh)
    SLICE = "slice"                  # a declared cut plane
    INTERFACE = "interface"          # where two regions meet (multiregion)
    OPENING = "opening"              # an inlet/outlet cap (vmtk)
    BRANCH = "branch"                # a centerline branch (vmtk)
    LAYER_REGION = "layer_region"    # a near-wall layer stretch (vmtk, the OpenFOAM engines)
    COORDINATE_REGION = "coordinate_region"   # a declared bounded box of interest


@dataclass(frozen=True)
class ViewSpec:

    view_id: str
    label: str                              # human-readable, shown to the reviewer
    purpose: str                            # the defect class(es) this view may reveal
    reveals: tuple[str, ...] = ()           # defect ids this view can expose
    cannot_establish: tuple[str, ...] = ()  # HONEST limits: what it must not be read as proving


@dataclass(frozen=True)
class InspectionTarget:

    target_id: str
    kind: TargetKind
    label: str
    purpose: str
    required: bool = True                   # part of this engine's coverage floor


# typed evidence requirements
# The CLOSED vocabulary a ReviewAxis uses to declare what evidence must exist before it can be
# judged. Shared TYPES; the concrete requirements are authored in each engine (or purpose) bundle,
# never here. Hybrid assurance means the complete engine review requires all relevant modalities -
# not that every axis requires every one. An axis may require metrics only, spatial only, both, or
# a gate plus either. These are what the AssurancePlan composes and the ledger satisfies.
@dataclass(frozen=True)
class HardGateRequirement:

    key: str


@dataclass(frozen=True)
class MetricRequirement:

    key: str


@dataclass(frozen=True)
class RenderViewRequirement:

    view_id: str


@dataclass(frozen=True)
class RenderTargetRequirement:

    kind: TargetKind
    selector: str


@dataclass(frozen=True)
class SpatialInspectionRequirement:

    kind: TargetKind


# The union an axis may declare. Closed: a new requirement kind is a visible contract change.
EvidenceRequirement = (
    HardGateRequirement | MetricRequirement | RenderViewRequirement
    | RenderTargetRequirement | SpatialInspectionRequirement
)


class CommandKind(str, Enum):

 # rendering commands: each MUST return a validated EvidenceItem
    SELECT_VIEW = "select_view"
    INSPECT_TARGET = "inspect_target"
    TOGGLE_ENTITY = "toggle_entity"
    GO_TO_COORDINATES = "go_to_coordinates"
    CAMERA_OP = "camera_op"
    APPLY_CLIP = "apply_clip"
    CAPTURE = "capture"
 # the one NON-rendering command: mutates session state, returns no image
    # It exists because the reviewer really has such an operation (set_navigation_defaults
    # changes pan/zoom step sizes and returns text). Squeezing it into CAMERA_OP would make it
    # return evidence with no image - which the lifecycle correctly reads as a FAILED render.
    # A successful configure would then be recorded as a failure, corrupting the exact signal
    # the coverage floor depends on. The distinction is structural, not a blank-field
    # convention.
    CONFIGURE_SESSION = "configure_session"


RENDERING_COMMANDS: frozenset[CommandKind] = frozenset({
    CommandKind.SELECT_VIEW, CommandKind.INSPECT_TARGET, CommandKind.TOGGLE_ENTITY,
    CommandKind.GO_TO_COORDINATES, CommandKind.CAMERA_OP, CommandKind.APPLY_CLIP,
    CommandKind.CAPTURE,
})
"""Commands that MUST produce a validated image. The lifecycle refuses a non-evidence result
from any of these - otherwise a failed render could hide behind a "successful" session update."""

NON_RENDERING_COMMANDS: frozenset[CommandKind] = frozenset({CommandKind.CONFIGURE_SESSION})


@dataclass(frozen=True)
class RenderCapabilities:

    views: tuple[ViewSpec, ...]
    targets: tuple[InspectionTarget, ...]
    entities: tuple[str, ...] = ()          # toggleable patch/region/group ids
    operations: frozenset[CommandKind] = frozenset()

    @property
    def view_ids(self) -> frozenset[str]:
        return frozenset(v.view_id for v in self.views)

    @property
    def target_ids(self) -> frozenset[str]:
        return frozenset(t.target_id for t in self.targets)

    @property
    def required_target_ids(self) -> frozenset[str]:
        return frozenset(t.target_id for t in self.targets if t.required)

    def supports(self, kind: CommandKind) -> bool:
        return kind in self.operations


@dataclass(frozen=True)
class RenderCommand:

    kind: CommandKind
    view_id: str = ""
    target_id: str = ""
    entity_id: str = ""
    visible: bool = True
    coordinates: tuple[float, float, float] | None = None
    span: float | None = None
    camera: str = ""                        # preset/direction/axis - validated by the renderer
    amount: float | None = None
    # CONFIGURE_SESSION settings. Named fields, never a settings dict: a dict is arbitrary
    # attribute assignment wearing a type, and the whole point of a closed command vocabulary
    # is that a reviewer cannot name something the engine never declared. A future setting is
    # an explicit contract extension, which is the cost that keeps this safe.
    pan_step_mm: float | None = None
    zoom_step: float | None = None


# evidence


@dataclass(frozen=True)
class EvidenceItem:

    evidence_id: str                        # stable within a session; deterministic
    seq: int                                # deterministic ordering - part of reproducibility
    label: str
    purpose: str
    image_ref: str = ""                     # "" => the render failed; see diagnostics
    view_id: str = ""                       # set for opening views
    command_kind: str = ""                  # set for interactive frames
    target_ref: str = ""                    # the target/entity the command addressed
    patches: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    axis_ids: tuple[str, ...] = ()          # the review axes this evidence supports
    artifact_key: str = ""                  # provenance: which declared artifact it came from
    diagnostics: tuple[str, ...] = ()
    covers_target: str = ""                 # non-empty => advances the coverage floor

    @property
    def rendered(self) -> bool:
        return bool(self.image_ref)


@dataclass(frozen=True)
class SessionUpdateResult:

    command_kind: str
    message: str                            # the exact user/model-visible text, unchanged
    seq: int = 0
    pan_step_mm: float | None = None        # the RESULTING settings, normalised
    zoom_step: float | None = None
    diagnostics: tuple[str, ...] = ()



class ReviewEvidenceFailure(str, Enum):

    RENDERER_UNAVAILABLE = "renderer_unavailable"    # cannot start/crashed/timed out/dep missing
    EVIDENCE_MISSING = "evidence_missing"            # artifact absent/unparseable/coverage impossible
    EVIDENCE_CONFLICT = "evidence_conflict"          # metric vs visual disagree, no deterministic rule


@dataclass(frozen=True)
class CoverageState:

    required: frozenset[str] = frozenset()
    visited: frozenset[str] = frozenset()

    @property
    def outstanding(self) -> frozenset[str]:
        return self.required - self.visited

    @property
    def complete(self) -> bool:
        return not self.outstanding


@dataclass
class EvidenceLedger:

    required_targets: frozenset[str] = frozenset()
    required_axes: frozenset[str] = frozenset()
    items: list[EvidenceItem] = field(default_factory=list)
    _next_seq: int = 0

    def record(self, item: EvidenceItem) -> EvidenceItem:
        stamped = EvidenceItem(**{**item.__dict__, "seq": self._next_seq})
        self._next_seq += 1
        self.items.append(stamped)
        return stamped

    def ordered(self) -> tuple[EvidenceItem, ...]:
        return tuple(sorted(self.items, key=lambda i: i.seq))

    def coverage(self) -> CoverageState:
        visited = {i.covers_target for i in self.items if i.covers_target and i.rendered}
        return CoverageState(required=self.required_targets, visited=frozenset(visited))

    def axes_with_evidence(self) -> frozenset[str]:
        return frozenset(a for i in self.items if i.rendered for a in i.axis_ids)

    def axes_missing_evidence(self) -> frozenset[str]:
        return self.required_axes - self.axes_with_evidence()

    def verdict_eligibility(self) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        cov = self.coverage()
        if not cov.complete:
            reasons.append(
                f"inspection incomplete: {sorted(cov.outstanding)} not visited with a rendered view")
        missing_axes = self.axes_missing_evidence()
        if missing_axes:
            reasons.append(f"review axes without evidence: {sorted(missing_axes)}")
        return (not reasons), tuple(reasons)


# the session + renderer ports


@dataclass(frozen=True)
class SceneBounds:

    xmin: float
    xmax: float
    ymin: float
    ymax: float
    zmin: float
    zmax: float

    def __post_init__(self) -> None:
        for name in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                # NaN bounds do not raise downstream - they produce a prompt describing a
                # location that does not exist, which reads exactly like a real one.
                raise ValueError(f"scene bounds {name} is not a finite number")


@dataclass(frozen=True)
class PatchLegendEntry:

    patch_id: str
    label: str
    color: str


@dataclass(frozen=True)
class PatchView:

    patch_id: str
    x: float
    y: float
    z: float
    span: float
    preset: str
    isolate: bool = True


@dataclass(frozen=True)
class SceneContext:

    has_geometry: bool
    mesh_units: str
    pan_step_mm: float
    zoom_step: float
    bounds: SceneBounds | None = None
    patch_legend: tuple[PatchLegendEntry, ...] = ()
    # Per-patch camera framing DERIVED BY THE RENDERER from the geometry it loaded
    # (render.review_palette.derive_patch_views). Presentation is renderer-owned: nothing
    # upstream of the review may choose where the reviewer looks. Typed, like the legend -
    # the scene context carries no open dictionaries.
    patch_views: tuple[PatchView, ...] = ()

    def __post_init__(self) -> None:
        for name in ("pan_step_mm", "zoom_step"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"navigation default {name} is not a number")
            if not math.isfinite(float(v)) or float(v) <= 0:
                raise ValueError(f"navigation default {name} is not a usable positive value")
        if not isinstance(self.mesh_units, str) or not self.mesh_units.strip():
            raise ValueError("mesh_units must be a non-empty string")

        seen: set[str] = set()
        for entry in self.patch_legend:
            if not isinstance(entry, PatchLegendEntry):
                raise ValueError("patch legend carries an untyped entry")
            if not entry.patch_id.strip():
                raise ValueError("patch legend entry has no id")
            if not isinstance(entry.label, str) or not entry.label.strip():
                raise ValueError(f"patch {entry.patch_id!r} has no display label")
            if "object at 0x" in entry.label or entry.label.startswith("<"):
                # A repr is not a label. It leaks object identity and a memory address into the
                # reviewer's prompt, and reads as a name until someone looks closely.
                raise ValueError(f"patch {entry.patch_id!r} label is an object repr")
            if entry.patch_id in seen:
                # Two entries under one id would render one legend line for two patches - the
                # reviewer would be told a colour that belongs to something else.
                raise ValueError(f"duplicate patch legend id {entry.patch_id!r}")
            seen.add(entry.patch_id)
            if not _is_color(entry.color):
                raise ValueError(f"patch {entry.patch_id!r} declares an invalid colour "
                                 f"{entry.color!r}")

        # GEOMETRY PRESENCE IS EXPLICIT, never inferred. "No required targets" and "no geometry"
        # are different states - gmsh and vmtk load real geometry and require no targets at all,
        # so conflating them would call their meshes blank.
        if self.has_geometry and self.bounds is None:
            raise ValueError("geometry is present but has no bounds - inconsistent scene state")


def _is_color(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip()
    if v.startswith("#"):
        return len(v) in (4, 7) and all(c in "0123456789abcdefABCDEF" for c in v[1:])
    return v.replace("_", "").replace("-", "").isalnum()


@dataclass(frozen=True)
class ResolvedArtifact:

    artifact_key: str
    path: Path
    fmt: ArtifactFormat


@dataclass(frozen=True)
class RenderContext:

    workspace: str
    manifest: dict[str, Any]
    engine_params: dict[str, Any] = field(default_factory=dict)
    save_dir: str = ""
    max_images: int = 0                     # 0 => the sandbox's default
    deadline_s: float = 0.0


class ReviewRenderError(RuntimeError):

    def __init__(self, category: ReviewEvidenceFailure, detail: str = "") -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"[{category.value}] {detail}".strip())


@runtime_checkable
class ReviewRenderSession(Protocol):
    def capabilities(self) -> RenderCapabilities: ...
    def scene_context(self) -> SceneContext:
        ...
    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        ...
    def execute(self, command: RenderCommand) -> EvidenceItem | SessionUpdateResult:
        ...
    def close(self) -> None:
        ...


@runtime_checkable
class ReviewRenderer(Protocol):

    def required_artifacts(self) -> tuple[RenderArtifactRequirement, ...]: ...
    def open(self, context: RenderContext,
             artifacts: Mapping[str, ResolvedArtifact]) -> ReviewRenderSession:
        ...
