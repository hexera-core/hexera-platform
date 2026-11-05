# Responsibility: Render the views a reviewer needs to judge a VMTK mesh.
# Boundaries: it renders artifacts the review session already resolved and confined.
# Collaborates with: sandbox/render_adapter.py and sandbox/review_session.py.
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.review_evidence import (
    CommandKind,
    EvidenceItem,
    RenderCapabilities,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewEvidenceFailure,
    ReviewRenderError,
    ReviewRenderSession,
    SessionUpdateResult,
    ViewSpec,
)
from meshpipeline.engines.vmtk.review_targets import (
    VmtkBranchTarget,
    VmtkSceneAudit,
    audit_cross_artifacts,
    discover_branch_targets,
    discover_surface_targets,
    resolve_target,
)

_CAMERA_PRESETS = ("front", "rear", "left", "right", "top", "bottom", "iso")
SURFACE_KEY = "mesh_paths.surface"
LUMEN_KEY = "mesh_paths.lumen"
CENTERLINES_KEY = "mesh_paths.centerlines"


class VmtkReviewSession:

    def __init__(self, backend: Any, targets, audit: VmtkSceneAudit,
                 *, required: bool, purpose: str) -> None:
        self._backend = backend
        self._targets = tuple(targets)
        self._audit = audit
        self._required = required
        self._purpose = purpose
        self._seq = 0
        self._closed = False
        self._opening: tuple[EvidenceItem, ...] | None = None

    def capabilities(self) -> RenderCapabilities:
        self._require_open()
        ops = {
            CommandKind.SELECT_VIEW, CommandKind.CAMERA_OP, CommandKind.GO_TO_COORDINATES,
            CommandKind.APPLY_CLIP, CommandKind.TOGGLE_ENTITY, CommandKind.CAPTURE,
            CommandKind.CONFIGURE_SESSION,
        }
        targets = tuple(t.to_inspection_target(required=self._required, purpose=self._purpose)
                        for t in self._targets)
        return RenderCapabilities(
            views=tuple(ViewSpec(view_id=p, label=f"{p} view",
                                 purpose="orient the camera to a standard direction")
                        for p in _CAMERA_PRESETS),
            targets=targets,
            entities=self._toggle_tokens(),
            operations=frozenset(ops))

    def _toggle_tokens(self) -> tuple[str, ...]:
        toks = [t.target_id for t in self._targets]
        from collections import Counter
        names = [t.patch_name for t in self._targets if getattr(t, "patch_name", None)]
        counts = Counter(names)
        toks += [n for n in names if counts[n] == 1]
        return tuple(dict.fromkeys(toks))

    def scene_context(self):
        self._require_open()
        from meshpipeline.sandbox.render_adapter import BackendRenderSession
        # scene_context is pure neutral mechanics - reuse it rather than duplicate the fact-to-type
        # mapping. It reads bounds/units/legend from the backend; no vmtk meaning is involved.
        return BackendRenderSession.scene_context(self)

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        self._require_open()
        if self._opening is None:
            self._opening = (self._evidence(
                image=self._render(self._backend.take_screenshot),
                label="opening view", purpose="the delivered lumen mesh, wall and caps as built",
                command_kind="open", view_id="iso", covers_target=""),)
        return self._opening

    def scene_audit(self) -> VmtkSceneAudit:
        return self._audit

 # execution
    def execute(self, command: RenderCommand):
        self._require_open()
        if command.kind is CommandKind.CONFIGURE_SESSION:
            applied = self._backend.set_navigation_defaults(
                command.pan_step_mm, command.zoom_step)
            return SessionUpdateResult(
                command_kind=command.kind.value,
                message=(f"Navigation defaults updated: "
                         f"pan_step={applied['pan_step_mm']:.0f} mm, "
                         f"zoom_step={applied['zoom_step']:.2f}×."),
                pan_step_mm=applied["pan_step_mm"], zoom_step=applied["zoom_step"])
        if command.kind is CommandKind.TOGGLE_ENTITY:
            return self._inspect_named(command)
        handler = {
            CommandKind.SELECT_VIEW: self._select_view,
            CommandKind.CAMERA_OP: self._camera_op,
            CommandKind.GO_TO_COORDINATES: self._go_to_coordinates,
            CommandKind.APPLY_CLIP: self._zoom_to_region,
            CommandKind.CAPTURE: self._capture,
        }.get(command.kind)
        if handler is None:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"vmtk review does not support {command.kind.value}")
        return handler(command)

    def _inspect_named(self, command: RenderCommand) -> EvidenceItem:
        target, err = resolve_target(self._targets, command.entity_id)
        if target is None:
            return self._failed(command, err or "unknown target")
        if not target.renderable:
            return self._failed(command, f"{target.target_id} has no renderable geometry")
        if isinstance(target, VmtkBranchTarget):
            return self._inspect_branch(command, target)
        return self._isolate_patch(command, target)

    def _isolate_patch(self, command: RenderCommand, target) -> EvidenceItem:
        name = target.patch_name
        if command.visible:
            self._backend.toggle_patch(name, True)
            action = "shown"
        else:
            for other in self._targets:
                on = getattr(other, "patch_name", None)
                if on and on != name:
                    self._backend.toggle_patch(on, True)
            self._backend.toggle_patch(name, False)
            action = "hidden"
        return self._evidence(
            image=self._render(self._backend.take_screenshot),
            label=f"{target.label} {action}",
            purpose="isolate a cap or the wall to check where it landed",
            command_kind=command.kind.value, target_ref=target.target_id,
            covers_target=target.target_id, artifact_key=SURFACE_KEY)

    def _inspect_branch(self, command: RenderCommand, target: VmtkBranchTarget) -> EvidenceItem:
        x, y, z = target.point
        self._backend.go_to_coordinates(x, y, z, target.span_mm, preset="iso")
        return self._evidence(
            image=self._render(self._backend.take_screenshot),
            label=f"{target.label} in view",
            purpose="confirm a centerline branch survived as an open passage",
            command_kind=command.kind.value, target_ref=target.target_id,
            covers_target=target.target_id, artifact_key=SURFACE_KEY)

    def _select_view(self, command: RenderCommand) -> EvidenceItem:
        if command.view_id not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {command.view_id!r}")
        self._backend.set_camera_preset(command.view_id)
        return self._shot(command, f"{command.view_id} view", "orient the camera",
                          view_id=command.view_id)

    def _camera_op(self, command: RenderCommand) -> EvidenceItem:
        op = command.camera
        if op in ("up", "down", "left", "right", "forward", "back"):
            self._backend.move_camera(op, command.amount)
        elif op in ("yaw", "pitch", "roll"):
            if command.amount is None:
                return self._failed(command, f"{op} requires an angle")
            self._backend.rotate_camera(op, command.amount)
        elif op == "zoom":
            self._backend.zoom(command.amount)
        elif op == "reset":
            self._backend.reset_view()
        else:
            return self._failed(command, f"unknown camera operation {op!r}")
        return self._shot(command, f"camera {op}", "reposition the camera")

    def _go_to_coordinates(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None or command.span is None:
            return self._failed(command, "coordinates and span are required")
        preset = command.view_id or None
        if preset is not None and preset not in _CAMERA_PRESETS:
            return self._failed(command, f"unknown camera preset {preset!r}")
        self._backend.go_to_coordinates(c[0], c[1], c[2], command.span, preset=preset)
        return self._shot(command, f"at ({c[0]:g}, {c[1]:g}, {c[2]:g})",
                          "inspect a specific location", view_id=preset or "")

    def _zoom_to_region(self, command: RenderCommand) -> EvidenceItem:
        c = command.coordinates
        if c is None:
            return self._failed(command, "a screen region is required")
        image = self._render(
            lambda: self._backend.zoom_to_region(c[0], c[1], command.span or 0.25))
        return self._evidence(image=image, label="zoomed to screen region",
                              purpose="magnify part of the current view",
                              command_kind=command.kind.value, artifact_key=SURFACE_KEY)

    def _capture(self, command: RenderCommand) -> EvidenceItem:
        return self._shot(command, "current view", "record the current view")

 # evidence + lifecycle (small, bundle-owned)
    def _render(self, call) -> str:
        call()
        path = self._backend.last_shot_path
        return str(path) if path is not None else ""

    def _shot(self, command, label, purpose, view_id="") -> EvidenceItem:
        return self._evidence(image=self._render(self._backend.take_screenshot), label=label,
                              purpose=purpose, command_kind=command.kind.value,
                              view_id=view_id, artifact_key=SURFACE_KEY)

    def _evidence(self, *, image, label, purpose, command_kind, view_id="", target_ref="",
                  covers_target="", artifact_key=SURFACE_KEY) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command_kind}-{self._seq:03d}", seq=self._seq, label=label,
            purpose=purpose, image_ref=image or "", view_id=view_id, command_kind=command_kind,
            target_ref=target_ref, groups=(target_ref,) if target_ref else (), axis_ids=(),
            artifact_key=artifact_key, covers_target=covers_target if image else "")

    def _failed(self, command, why) -> EvidenceItem:
        self._seq += 1
        return EvidenceItem(
            evidence_id=f"{command.kind.value}-{self._seq:03d}", seq=self._seq,
            label="command refused", purpose="report a command that could not be honoured",
            image_ref="", command_kind=command.kind.value,
            target_ref=command.entity_id or command.target_id, diagnostics=(why,),
            covers_target="")

    def _require_open(self) -> None:
        if self._closed:
            raise ReviewRenderError(ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                                    "the review session is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._backend.close()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("vmtk session close failed", exc_info=True)


class VmtkReviewRenderer:

    def required_artifacts(self):
        from meshpipeline.engines.vmtk._shared import _RENDER_ARTIFACTS
        return _RENDER_ARTIFACTS

    def open(self, context: RenderContext,
             artifacts: Mapping[str, ResolvedArtifact]) -> ReviewRenderSession:
        # Neutral construction: the shared factory resolves + confines + loads the delivered
        # surface. vmtk assigns the vascular meaning immediately after, from the backend's raw
        # patch names and the line cells of the centerlines it was handed.
        from meshpipeline.sandbox.polyline_source import count_boundary_loops, load_polylines
        from meshpipeline.sandbox.render_adapter import build_backend

        backend, _inputs, _regions = build_backend(context, artifacts)

        openings, layers = discover_surface_targets(backend.patch_names())

        centerlines = artifacts.get(CENTERLINES_KEY)
        branches = discover_branch_targets(
            load_polylines(centerlines.path) if centerlines is not None else ())

        lumen = artifacts.get(LUMEN_KEY)
        loops = count_boundary_loops(lumen.path) if lumen is not None else -1
        audit = audit_cross_artifacts(openings, loops, branches)

        from meshpipeline.engines.vmtk._shared import _INSPECTION_TARGETS
        rule = {t.target_id: t for t in _INSPECTION_TARGETS}.get("opening:*")
        return VmtkReviewSession(
            backend, (*openings, *layers, *branches), audit,
            required=bool(rule and rule.required),
            purpose=(rule.purpose if rule else "confirm the lumen's caps, wall and branches "
                     "survived into the delivered mesh"))


RENDERER = VmtkReviewRenderer()
