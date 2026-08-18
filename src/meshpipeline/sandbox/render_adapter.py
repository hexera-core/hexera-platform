# Responsibility: Turn an engine's declared review requirements into a render session over resolved artifacts.
# Owns: target expansion, identifier normalisation and the session the reviewer drives.
# Boundaries: it receives resolved artifacts and resolves none itself, which keeps the confinement fence meaningful.
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from meshpipeline.contracts.review_evidence import (
    CommandKind,
    EvidenceItem,
    InspectionTarget,
    PatchLegendEntry,
    PatchView,
    RenderCapabilities,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewEvidenceFailure,
    ReviewRenderError,
    SceneBounds,
    SceneContext,
    SessionUpdateResult,
    TargetKind,
    ViewSpec,
)
from meshpipeline.sandbox.render_inputs import (
    RenderMetadata,
    ResolvedRenderInputs,
    resolve_regions,
)

logger = logging.getLogger(__name__)

SURFACE_KEY = "mesh_paths.surface"
VOLUME_KEY = "mesh_paths.volume"

# A mesh with more boundary patches than this is not a review problem, it is a build problem - and
# expanding an unbounded patch list into targets would let a malformed mesh exhaust the session.
MAX_PATCHES = 512

# The nine rendering operations, as the reviewer's tools map onto the typed vocabulary.
_CAMERA_PRESETS = ("front", "rear", "left", "right", "top", "bottom", "iso")

# WHERE EACH OPERATION'S IMAGE COMES FROM. Enumerated because assuming it is how a crop command
# came to return a full-view screenshot: `zoom_to_region` PRODUCES an image (it magnifies a region
# and returns that crop), but it was routed through the generic "mutate, then screenshot" helper,
# so the crop was written to disk and then overwritten in the evidence by an unrelated later
# full view. The reviewer would have been handed the whole model labelled "4x magnification".
#   OWN_IMAGE - the backend call returns the image it just made; use exactly that.
#   THEN_SHOT - the call mutates the scene and returns None; a screenshot follows.
# Verified against backend.py: the three OWN_IMAGE operations are the three that call
# `_save_and_encode` themselves. A contract test re-derives this from the source, so an
# operation that changes category fails rather than silently misreporting.
OWN_IMAGE = "own_image"
THEN_SHOT = "then_shot"

IMAGE_SOURCE: dict[CommandKind, str] = {
    CommandKind.SELECT_VIEW: THEN_SHOT,
    CommandKind.CAMERA_OP: THEN_SHOT,
    CommandKind.GO_TO_COORDINATES: THEN_SHOT,
    CommandKind.TOGGLE_ENTITY: THEN_SHOT,
    CommandKind.CAPTURE: THEN_SHOT,
    CommandKind.APPLY_CLIP: OWN_IMAGE,        # zoom_to_region returns its crop
    CommandKind.INSPECT_TARGET: OWN_IMAGE,    # inspect_region returns its slice
}


def _view_specs() -> tuple[ViewSpec, ...]:
    return tuple(
        ViewSpec(view_id=p, label=f"{p} view",
                 purpose="orient the camera to a standard direction")
        for p in _CAMERA_PRESETS
    )


def normalize_patch_id(name: str) -> str:
    return f"patch:{name}"


def normalize_region_id(name: str) -> str:
    return f"region:{name}"


def expand_patch_targets(
    patch_names: Sequence[str],
    *,
    required: bool,
    purpose: str,
) -> tuple[InspectionTarget, ...]:
    seen: dict[str, str] = {}
    out: list[InspectionTarget] = []
    for name in patch_names:
        if not isinstance(name, str) or not name.strip():
            continue
        target_id = normalize_patch_id(name)
        if target_id in seen:
            # Two distinct patches normalising onto one id would silently merge coverage: one
            # toggle would mark both looked-at. Refuse the collision rather than absorb it.
            logger.warning("render adapter: patch id collision %r vs %r -> %s",
                           seen[target_id], name, target_id)
            continue
        seen[target_id] = name
        out.append(InspectionTarget(target_id=target_id, kind=TargetKind.PATCH,
                                    label=name, purpose=purpose, required=required))
        if len(out) >= MAX_PATCHES:
            logger.warning("render adapter: patch list truncated at %d", MAX_PATCHES)
            break
    return tuple(out)


def expand_region_targets(
    regions: Mapping[str, Any],
    *,
    required: bool,
    purpose: str,
    volume_available: bool,
) -> tuple[InspectionTarget, ...]:
    if not volume_available:
        if regions:
            logger.info("render adapter: %d region(s) not offered - no volume artifact",
                        len(regions))
        return ()
    return tuple(
        InspectionTarget(target_id=normalize_region_id(r.region_id), kind=TargetKind.REGION,
                         label=r.region_id, purpose=purpose, required=required)
        for r in regions.values()
    )


class BackendRenderSession:

    def __init__(
        self,
        backend: Any,
        *,
        inputs: ResolvedRenderInputs,
        regions: Mapping[str, Any],
        patch_targets: tuple[InspectionTarget, ...],
        region_targets: tuple[InspectionTarget, ...],
        opening_purpose: str,
    ) -> None:
        self._backend = backend
        self._inputs = inputs
        self._regions = dict(regions)
        self._patch_targets = patch_targets
        self._region_targets = region_targets
        self._opening_purpose = opening_purpose
        self._seq = 0
        self._closed = False
        self._opening: tuple[EvidenceItem, ...] | None = None

    def capabilities(self) -> RenderCapabilities:
        ops = {
            CommandKind.SELECT_VIEW, CommandKind.CAMERA_OP, CommandKind.GO_TO_COORDINATES,
            CommandKind.APPLY_CLIP, CommandKind.TOGGLE_ENTITY, CommandKind.CAPTURE,
            CommandKind.CONFIGURE_SESSION,
        }
        # INSPECT_TARGET is offered only when there is something to cut. A capability that cannot
        # succeed must not be advertised: the reviewer would spend calls on it and read the
        # resulting surface screenshot as an interior it inspected.
        if self._inputs.has_volume and self._region_targets:
            ops.add(CommandKind.INSPECT_TARGET)
        return RenderCapabilities(
            views=_view_specs(),
            targets=self._patch_targets + self._region_targets,
            entities=tuple(t.label for t in self._patch_targets),
            operations=frozenset(ops),
        )

    def scene_context(self) -> SceneContext:
        self._require_open()
        facts = self._backend.scene_facts()
        b = facts.get("bounds")
        return SceneContext(
            has_geometry=bool(facts["has_geometry"]),
            mesh_units=facts["mesh_units"],
            pan_step_mm=facts["pan_step_mm"],
            zoom_step=facts["zoom_step"],
            bounds=SceneBounds(**b) if b else None,
            patch_legend=tuple(PatchLegendEntry(patch_id=n, label=n, color=c)
                               for n, c in facts["patch_legend"]),
            patch_views=tuple(
                PatchView(patch_id=n, x=float(v["x"]), y=float(v["y"]), z=float(v["z"]),
                          span=float(v["span"]), preset=str(v["preset"]),
                          isolate=bool(v.get("isolate", True)))
                for n, v in sorted((facts.get("patch_views") or {}).items())),
        )

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        if self._opening is None:
            self._require_open()
            image = self._render(self._backend.take_screenshot)
            self._opening = (self._evidence(
                image=image, label="opening view", purpose=self._opening_purpose,
                command_kind="open", view_id="iso", covers_target=""),)
        return self._opening

 # execution
    def execute(self, command: RenderCommand) -> EvidenceItem | SessionUpdateResult:
        self._require_open()
        if command.kind is CommandKind.CONFIGURE_SESSION:
            return self._configure(command)
        handler = {
            CommandKind.SELECT_VIEW: self._select_view,
            CommandKind.CAMERA_OP: self._camera_op,
            CommandKind.GO_TO_COORDINATES: self._go_to_coordinates,
            CommandKind.APPLY_CLIP: self._zoom_to_region,
            CommandKind.INSPECT_TARGET: self._inspect_target,
            CommandKind.TOGGLE_ENTITY: self._toggle_entity,
            CommandKind.CAPTURE: self._capture,
        }.get(command.kind)
        if handler is None:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"this renderer does not support {command.kind.value}")
        return handler(command)

    def _configure(self, command: RenderCommand) -> SessionUpdateResult:
        applied = self._backend.set_navigation_defaults(
            command.pan_step_mm, command.zoom_step)
        return SessionUpdateResult(
            command_kind=command.kind.value,
            message=(f"Navigation defaults updated: "
                     f"pan_step={applied['pan_step_mm']:.0f} mm, "
                     f"zoom_step={applied['zoom_step']:.2f}×."),
            pan_step_mm=applied["pan_step_mm"], zoom_step=applied["zoom_step"])

    def _select_view(self, command: RenderCommand) -> EvidenceItem:
        if command.view_id not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {command.view_id!r}")
        self._backend.set_camera_preset(command.view_id)
        return self._shot(command, label=f"{command.view_id} view",
                          purpose="orient the camera", view_id=command.view_id)

    def _camera_op(self, command: RenderCommand) -> EvidenceItem:
        op = command.camera
        if op in ("up", "down", "left", "right", "forward", "back"):
            self._backend.move_camera(op, command.amount)
            label = f"camera moved {op}"
        elif op in ("yaw", "pitch", "roll"):
            if command.amount is None:
                return self._failed(command, f"{op} requires an angle")
            self._backend.rotate_camera(op, command.amount)
            label = f"camera rotated {op} {command.amount:g}°"
        elif op == "zoom":
            self._backend.zoom(command.amount)
            label = "camera zoomed"
        elif op == "reset":
            self._backend.reset_view()
            label = "view reset"
        else:
            return self._failed(command, f"unknown camera operation {op!r}")
        return self._shot(command, label=label, purpose="reposition the camera")

    def _go_to_coordinates(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None or command.span is None:
            return self._failed(command, "coordinates and span are required")

        preset = command.view_id or None
        if preset is not None and preset not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {preset!r}")

        patch_name = command.entity_id or None
        if patch_name is not None and patch_name not in {t.label for t in self._patch_targets}:
            # An unknown patch must not reach the backend, and must not produce text claiming an
            # isolation of something that is not there.
            return self._failed(command, f"unknown patch {patch_name!r}")

        self._backend.go_to_coordinates(c[0], c[1], c[2], command.span,
                                        preset=preset, patch_name=patch_name)
        return self._shot(command, label=f"at ({c[0]:g}, {c[1]:g}, {c[2]:g})",
                          purpose="inspect a specific location",
                          view_id=preset or "")

    def _zoom_to_region(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None:
            return self._failed(command, "a screen region is required")
        image = self._render(
            lambda: self._backend.zoom_to_region(c[0], c[1], command.span or 0.25))
        return self._evidence(image=image, label="zoomed to screen region",
                              purpose="magnify part of the current view",
                              command_kind=command.kind.value, artifact_key=SURFACE_KEY)

    def _inspect_target(self, command: RenderCommand) -> EvidenceItem:
        name = command.target_id.removeprefix("region:")
        region = self._regions.get(name)
        if region is None:
            # Unknown names FAIL. They do not quietly render the surface and count.
            return self._failed(command, f"unknown inspection region {command.target_id!r}")
        if region.requires_volume and not self._inputs.has_volume:
            return self._failed(
                command, f"region {command.target_id!r} needs the volume artifact, "
                         "which this job did not provide")
        image = self._render(lambda: self._backend.inspect_region(region))
        return self._evidence(
            image=image, label=f"section at {name}", purpose="reveal internal structure",
            command_kind=command.kind.value, target_ref=command.target_id,
            regions=(name,), covers_target=normalize_region_id(name),
            artifact_key=VOLUME_KEY)

    def _toggle_entity(self, command: RenderCommand) -> EvidenceItem:
        known = {t.label for t in self._patch_targets}
        if command.entity_id not in known:
            # A failure, never a screenshot: a returned image would let the coverage floor count
            # a patch as "inspected" because it was named wrong.
            return self._failed(command, f"unknown patch {command.entity_id!r}")
        self._backend.toggle_patch(command.entity_id, command.visible)
        state = "shown" if command.visible else "hidden"
        return self._evidence(
            image=self._render(self._backend.take_screenshot),
            label=f"{command.entity_id} {state}",
            purpose="isolate a patch to check it directly",
            command_kind=command.kind.value, target_ref=command.entity_id,
            patches=(command.entity_id,),
            covers_target=normalize_patch_id(command.entity_id),
            artifact_key=SURFACE_KEY)

    def _capture(self, command: RenderCommand) -> EvidenceItem:
        return self._shot(command, label="current view", purpose="record the current view")

 # evidence construction
    def _render(self, call) -> str:
        call()
        path = self._backend.last_shot_path
        return str(path) if path is not None else ""

    def _shot(self, command: RenderCommand, *, label: str, purpose: str,
              view_id: str = "") -> EvidenceItem:
        return self._evidence(image=self._render(self._backend.take_screenshot), label=label,
                              purpose=purpose, command_kind=command.kind.value,
                              view_id=view_id, artifact_key=SURFACE_KEY)

    def _evidence(self, *, image: str, label: str, purpose: str, command_kind: str,
                  view_id: str = "", target_ref: str = "", patches: tuple[str, ...] = (),
                  regions: tuple[str, ...] = (), covers_target: str = "",
                  artifact_key: str = SURFACE_KEY) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command_kind}-{self._seq:03d}", seq=self._seq,
            label=label, purpose=purpose, image_ref=image or "",
            view_id=view_id, command_kind=command_kind, target_ref=target_ref,
            patches=patches, regions=regions,
            # axis_ids stays EMPTY: evidence-to-axis binding arrives in commit 4, and inventing
            # a relationship now would be a claim nothing has established.
            axis_ids=(),
            artifact_key=artifact_key,        # provenance by KEY, never an absolute path
            covers_target=covers_target if image else "")

    def _failed(self, command: RenderCommand, why: str) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command.kind.value}-{self._seq:03d}", seq=self._seq,
            label="command refused", purpose="report a command that could not be honoured",
            image_ref="", command_kind=command.kind.value,
            target_ref=command.target_id or command.entity_id,
            diagnostics=(why,), covers_target="")

 # lifecycle
    def _require_open(self) -> None:
        if self._closed:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE, "the review session is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._backend.close()
        except Exception:  # noqa: BLE001
            logger.warning("render adapter: backend close failed", exc_info=True)


def build_backend(
    context: RenderContext,
    artifacts: Mapping[str, ResolvedArtifact],
) -> tuple[Any, ResolvedRenderInputs, dict[str, Any]]:
    surface = artifacts.get(SURFACE_KEY)
    if surface is None:
        raise ReviewRenderError(
            ReviewEvidenceFailure.EVIDENCE_MISSING,
            f"required artifact {SURFACE_KEY!r} was not resolved for this session")

    inputs = ResolvedRenderInputs(surface=surface, volume=artifacts.get(VOLUME_KEY))
    regions = resolve_regions(context.manifest)

    # Imported HERE, not at module scope: backend.py pulls in gmsh and pyvista, and the API
    # process imports engine specs through the registry and must stay light.
    from meshpipeline.sandbox.backend import MeshRenderBackend

    backend = MeshRenderBackend(
        inputs=inputs,
        metadata=RenderMetadata.from_manifest(context.manifest),
        save_dir=Path(context.save_dir) if context.save_dir else None,
        regions=regions,
    )
    return backend, inputs, regions
