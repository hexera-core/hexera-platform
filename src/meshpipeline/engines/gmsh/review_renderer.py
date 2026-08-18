# Responsibility: Render the views a reviewer needs to judge a Gmsh mesh.
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
from meshpipeline.engines.gmsh.review_targets import (
    GmshGroupTarget,
    discover_group_targets,
    resolve_target,
)

_CAMERA_PRESETS = ("front", "rear", "left", "right", "top", "bottom", "iso")
SURFACE_KEY = "mesh_paths.surface"


class GmshReviewSession:

    def __init__(self, backend: Any, targets: tuple[GmshGroupTarget, ...],
                 *, required: bool, purpose: str) -> None:
        self._backend = backend
        self._targets = targets
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
            # The tokens a reviewer may name a group by: its stable id and (for a uniquely-named
            # group) its name. The session resolves these authoritatively; this list only lets the
            # neutral runtime pre-check pass a plausibly-valid token through.
            entities=self._toggle_tokens(),
            operations=frozenset(ops))

    def _toggle_tokens(self) -> tuple[str, ...]:
        toks = [t.target_id for t in self._targets]
        from collections import Counter
        name_counts = Counter(t.name for t in self._targets if t.name)
        toks += [t.name for t in self._targets if t.name and name_counts[t.name] == 1]
        return tuple(toks)

    def scene_context(self):
        self._require_open()
        from meshpipeline.sandbox.render_adapter import BackendRenderSession
        # scene_context is pure neutral mechanics - reuse it rather than duplicate the fact-to-type
        # mapping. It reads bounds/units/legend from the backend; no Gmsh meaning is involved.
        return BackendRenderSession.scene_context(self)

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        self._require_open()
        if self._opening is None:
            self._opening = (self._evidence(
                image=self._render(self._backend.take_screenshot),
                label="opening view", purpose="the mesh and its physical groups as delivered",
                command_kind="open", view_id="iso", covers_target=""),)
        return self._opening

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
            return self._toggle_group(command)
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
                f"gmsh review does not support {command.kind.value}")
        return handler(command)

    def _toggle_group(self, command: RenderCommand) -> EvidenceItem:
        target, err = resolve_target(self._targets, command.entity_id)
        if target is None:
            return self._failed(command, err or "unknown physical group")
        if not target.renderable:
            return self._failed(command, f"physical group {target.target_id} has no renderable "
                                         "members")
        # The backend toggles a named set of dim-2 actors - neutral. Isolation is engine-owned:
        # when hiding others, we show every renderable group's name then hide this one.
        if command.visible:
            self._backend.toggle_patch(target.name, True)
            action = "shown"
        else:
            for other in self._targets:
                if other.name and other.name != target.name:
                    self._backend.toggle_patch(other.name, True)
            self._backend.toggle_patch(target.name, False)
            action = "hidden"
        return self._evidence(
            image=self._render(self._backend.take_screenshot),
            label=f"{target.label} {action}",
            purpose="isolate a physical group to check where it landed",
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
            logging.getLogger(__name__).warning("gmsh session close failed", exc_info=True)


class GmshReviewRenderer:

    def required_artifacts(self):
        from meshpipeline.engines.gmsh._shared import _RENDER_ARTIFACTS
        return _RENDER_ARTIFACTS

    def open(self, context: RenderContext,
             artifacts: Mapping[str, ResolvedArtifact]) -> ReviewRenderSession:
        # Neutral construction: the shared factory resolves + confines + loads. Gmsh assigns the
        # meaning immediately after, from the backend's raw physical-group facts.
        from meshpipeline.sandbox.render_adapter import build_backend
        backend, _inputs, _regions = build_backend(context, artifacts)

        targets = discover_group_targets(backend.physical_groups())

        from meshpipeline.engines.gmsh._shared import _INSPECTION_TARGETS
        rule = {t.target_id: t for t in _INSPECTION_TARGETS}.get("group:*")
        return GmshReviewSession(
            backend, targets,
            required=bool(rule and rule.required),
            purpose=(rule.purpose if rule else "confirm the physical group landed on the "
                     "intended geometry"))


RENDERER = GmshReviewRenderer()
