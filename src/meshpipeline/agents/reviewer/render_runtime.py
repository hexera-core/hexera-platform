# Responsibility: Give the reviewer its rendering session and the typed results of each view.
# Boundaries: it opens a session over artifacts the sandbox already resolved and confined; it resolves no path itself.
# Collaborates with: sandbox/review_session.py and sandbox/render_adapter.py.
from __future__ import annotations

import base64
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshpipeline.agents.reviewer.scene_text import (
    go_to_coordinates_text,
    move_camera_text,
    navigation_context,
    patch_colour_legend,
)
from meshpipeline.agents.reviewer.visual_surface import OpeningContext
from meshpipeline.contracts.mesh_units import completed_mesh_unit
from meshpipeline.contracts.review_evidence import (
    CommandKind,
    EvidenceItem,
    RenderCommand,
    RenderContext,
    SessionUpdateResult,
    TargetKind,
)
from meshpipeline.sandbox.execution_lane import RendererExecutionLane
from meshpipeline.sandbox.review_session import open_review_session

logger = logging.getLogger(__name__)

# `fn` is not a viewer tool - the caller owns it (submit_findings / unknown).
# Mirrors navigation.NOT_VIEWER_TOOL's role.
NOT_VIEWER_TOOL: Any = object()


@dataclass(frozen=True)
class TypedToolResult:

    content: Any
    evidence: EvidenceItem | None
    is_viewer_tool: bool

VIEWER_RENDERING_TOOLS = frozenset({
    "set_camera_preset", "move_camera", "rotate_camera", "zoom", "go_to_coordinates",
    "zoom_to_region", "inspect_region", "toggle_patch", "reset_view",
})
VIEWER_CONFIGURE_TOOLS = frozenset({"set_navigation_defaults"})
# The non-viewer half of the roster. `submit_findings` is composed in unified.py rather than
# declared here, so REVIEWER_TOOLS is exactly the viewer tools.
VERDICT_TOOLS: frozenset[str] = frozenset()
VIEWER_TOOLS = VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS


def _safe_float(val: Any, default: float | None) -> float | None:
    try:
        result = float(val)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _required_float(val: Any, default: float) -> float:
    out = _safe_float(val, default)
    return default if out is None else out


def _image_part(image_ref: str) -> dict:
    data = Path(image_ref).read_bytes()
    return {"type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(data).decode()}"}}


class ReviewerRenderRuntime:

    def __init__(self, session: Any, mesh_units: str, lane: RendererExecutionLane) -> None:
        self._session = session
        self._mesh_units = mesh_units
        self._lane = lane
        self._closed = False
        # The typed EvidenceItem the most recent render produced, captured by `_exec`. `execute_typed`
        # reads it so the unified review records the SESSION's authoritative target identity, not the
        # model's requested arguments. None until a render occurs; reset per `execute_typed` call.
        self._last_evidence: EvidenceItem | None = None

 # scene facts
    async def scene(self):
        return await self._lane.run(self._session.scene_context)

    async def capabilities(self):
        return await self._lane.run(self._session.capabilities)

    async def patch_names(self) -> list[str]:
        caps = await self.capabilities()
        return [t.label for t in caps.targets if t.kind is TargetKind.PATCH]

    async def region_names(self) -> list[str]:
        caps = await self.capabilities()
        return [t.label for t in caps.targets if t.kind is TargetKind.REGION]

    async def has_geometry(self) -> bool:
        return (await self.scene()).has_geometry

 # the initial visual context
    async def initial_context(self) -> OpeningContext:
        scene = await self.scene()
        opening = await self._lane.run(self._session.opening_evidence)
        image = opening[0].image_ref if opening else ""
        return OpeningContext(
            nav_context=navigation_context(scene),
            patch_views=tuple(scene.patch_views),
            patch_colour_legend=patch_colour_legend(scene),
            initial_screenshot_b64=(
                base64.b64encode(Path(image).read_bytes()).decode() if image else None),
            has_geometry=scene.has_geometry,
            # THE FIFTH OPENING FACT. The reviewer filters its coverage floor down to patches that
            # actually loaded - a manifest names far-field and 2D front/back patches no review
            # geometry ever reaches, and requiring those made the floor unsatisfiable. An opening
            # fact of the same kind as the legend, so it belongs here rather than in a third
            # surface method.
            # ADDITIVE neutral inventory - every declared target as (kind, id, label). An engine
            # whose targets are not patches (Gmsh groups) is carried here without faking
            # patch_names.
            inspection_targets=tuple((await self.capabilities()).targets),
            patch_names=[t.label for t in (await self.capabilities()).targets
                         if t.kind is TargetKind.PATCH],
        )

 # tool execution
    async def execute_viewer_tool(self, fn: str, args: dict) -> Any:
        if fn not in VIEWER_TOOLS:
            return NOT_VIEWER_TOOL

        handler = getattr(self, f"_t_{fn}")
        return await handler(args)

 # the ten translations
    async def _t_set_camera_preset(self, args: dict) -> Any:
        preset = args.get("preset", "iso")
        ev = await self._exec(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id=preset))
        return await self._parts(ev, f"Camera moved to {preset} preset.")

    async def _t_move_camera(self, args: dict) -> Any:
        direction = args.get("direction", "right")
        raw = args.get("distance_mm")
        distance_mm = _safe_float(raw, None) if raw is not None else None
        ev = await self._exec(RenderCommand(kind=CommandKind.CAMERA_OP, camera=direction,
                                      amount=distance_mm))
        # The LIVE pan step, read AFTER the move: when no distance was given the backend used its
        # current default, and the text reports what actually happened.
        return await self._parts(ev, move_camera_text(await self.scene(), direction=direction,
                                                      distance_mm=distance_mm))

    async def _t_rotate_camera(self, args: dict) -> Any:
        axis = args.get("axis", "yaw")
        degrees = _required_float(args.get("degrees"), 45.0)
        ev = await self._exec(RenderCommand(kind=CommandKind.CAMERA_OP, camera=axis, amount=degrees))
        return await self._parts(ev, f"Camera rotated {degrees}° on {axis}.")

    async def _t_zoom(self, args: dict) -> Any:
        raw = args.get("factor")
        factor = _safe_float(raw, None) if raw is not None else None
        ev = await self._exec(RenderCommand(kind=CommandKind.CAMERA_OP, camera="zoom", amount=factor))
        factor_label = f"{factor}×" if factor is not None else "default step"
        return await self._parts(ev, f"Zoomed by {factor_label}.")

    async def _t_go_to_coordinates(self, args: dict) -> Any:
        x = _required_float(args.get("x"), 0.0)
        y = _required_float(args.get("y"), 0.0)
        z = _required_float(args.get("z"), 0.0)
        span = _required_float(args.get("span"), 1000.0)
        preset = args.get("preset")
        patch_name = args.get("patch_name")
        ev = await self._exec(RenderCommand(
            kind=CommandKind.GO_TO_COORDINATES, coordinates=(x, y, z), span=span,
            view_id=preset or "", entity_id=patch_name or ""))
        if not ev.image_ref:
            # The adapter refused (unknown preset or patch) - it never reached the backend, so the
            # text must not announce a preset or an isolation that did not happen.
            return self._refusal(ev)
        # Fresh snapshot: go_to_coordinates RECALIBRATES the pan step from the span it framed.
        return await self._parts(ev, go_to_coordinates_text(
            await self.scene(), x=x, y=y, z=z, span=span,
            preset=preset or "", patch_name=patch_name or ""))

    async def _t_zoom_to_region(self, args: dict) -> Any:
        screen_x = _required_float(args.get("screen_x"), 0.5)
        screen_y = _required_float(args.get("screen_y"), 0.5)
        magnification = _required_float(args.get("magnification"), 4.0)
        # APPLY_CLIP carries the magnification in `span`; the adapter returns the crop the
        # backend produced, not a later full view.
        ev = await self._exec(RenderCommand(kind=CommandKind.APPLY_CLIP,
                                      coordinates=(screen_x, screen_y, 0.0),
                                      span=magnification))
        return await self._parts(ev, f"Render crop at ({screen_x:.2f}, {screen_y:.2f}) - "
                               f"{magnification}× magnification, camera unchanged.")

    async def _t_inspect_region(self, args: dict) -> Any:
        region_name = str(args.get("region_name", ""))
        known = await self.region_names()
        if region_name not in known:
            # THE ONE APPROVED CORRECTION. Announcing a slice at an undeclared region would be a
            # false evidence claim, and the coverage floor would count it. This is a
            # tool-validation response, not a failure: the reviewer can correct and continue.
            return (
                f"No inspection region named '{region_name}' is declared for this mesh, so no "
                f"slice was taken. Declared regions: {', '.join(known) or 'none'}. "
                "Call inspect_region with one of those names, or judge the interior from the "
                "mesh metrics instead."
            )
        ev = await self._exec(RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                      target_id=f"region:{region_name}"))
        return await self._parts(ev, (
            f"Internal slice through the VOLUME mesh at region '{region_name}'. "
            "This cut exposes the interior cells - inspect the near-wall band and "
            "internal sizing yourself. The mesh script holds the exact numbers "
            "(layer count, sizes); use the slice to confirm what is actually there."))

    async def _t_toggle_patch(self, args: dict) -> Any:
        patch_name = args.get("patch_name", "")
        visible = bool(args.get("visible", True))
        # NEUTRAL: the session's declared entities, whatever KIND they are (patches, physical
        # groups, ...). The runtime never learns what patch_name means - the engine session owns
        # the authoritative resolution; this only screens a plainly-unknown token. For the visual
        # engines `entities` == the patch labels, so the text below is byte-identical.
        renderable = list((await self.capabilities()).entities)
        if patch_name not in renderable:
            return (
                f"Patch '{patch_name}' carries no review geometry and cannot be shown. "
                f"Renderable patches: {', '.join(renderable) or 'none'}. "
                "Do not draw conclusions about this patch from the viewer; judge it "
                "from the mesh metrics instead."
            )
        ev = await self._exec(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id=patch_name,
                                      visible=visible))
        action = "shown" if visible else "hidden"
        return await self._parts(ev, (
            f"Patch '{patch_name}' {action}. "
            "Toggle changes visibility only - zoom is unchanged. "
            "If the target patch is not filling the frame with visible cells, "
            "call go_to_coordinates to zoom in before drawing conclusions."))

    async def _t_reset_view(self, args: dict) -> Any:
        ev = await self._exec(RenderCommand(kind=CommandKind.CAMERA_OP, camera="reset"))
        return await self._parts(ev, "View reset to isometric - all patches visible.")

    async def _t_set_navigation_defaults(self, args: dict) -> Any:
        ps_raw = args.get("pan_step_mm")
        zs_raw = args.get("zoom_step")
        result = await self._lane.run(self._session.execute, RenderCommand(
            kind=CommandKind.CONFIGURE_SESSION,
            pan_step_mm=_safe_float(ps_raw, None) if ps_raw is not None else None,
            zoom_step=_safe_float(zs_raw, None) if zs_raw is not None else None))
        if not isinstance(result, SessionUpdateResult):
            raise TypeError("a session configuration returned evidence")
        return result.message

 # result shaping
    async def _exec(self, command: RenderCommand) -> EvidenceItem:
        self._require_open()
        ev = await self._lane.run(self._session.execute, command)
        # Capture the session's authoritative typed evidence for `execute_typed`. Only genuine
        # renders reach here; a validation refusal returns before `_exec` and leaves this None.
        if isinstance(ev, EvidenceItem):
            self._last_evidence = ev
        return ev

    async def execute_typed(self, fn: str, args: dict) -> TypedToolResult:
        self._last_evidence = None
        content = await self.execute_viewer_tool(fn, args)
        if content is NOT_VIEWER_TOOL:
            return TypedToolResult(content=content, evidence=None, is_viewer_tool=False)
        return TypedToolResult(content=content, evidence=self._last_evidence, is_viewer_tool=True)

    async def _parts(self, ev: EvidenceItem, text: str) -> Any:
        if not ev.image_ref:
            return self._refusal(ev)
        return [{"type": "text", "text": text}, _image_part(ev.image_ref)]

    def _refusal(self, ev: EvidenceItem) -> str:
        why = "; ".join(ev.diagnostics) if ev.diagnostics else "the renderer produced no image"
        return f"That view could not be produced: {why}."

 # lifecycle
    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("the reviewer render runtime is closed")

    @property
    def closed(self) -> bool:
        return self._closed


@asynccontextmanager
async def open_runtime(spec: Any, context: RenderContext):
    lane = RendererExecutionLane()
    try:
        async with open_review_session(spec, context, native_dispatch=lane.run) as session:
            runtime = ReviewerRenderRuntime(
                session=session,
                # THE reader. A manifest that cannot state its unit stops the runtime here
                # rather than letting the reviewer narrate a metre mesh in millimetres.
                mesh_units=completed_mesh_unit(context.manifest).value,
                lane=lane)
            try:
                yield runtime
            finally:
                # Refuse new commands BEFORE the lifecycle queues its close, so nothing races in
                # behind it. The lifecycle's close still runs: it uses the lane's final door.
                lane.begin_closing()
                runtime._closed = True
    finally:
        # Only after the session's close has drained. Failures here are secondary by
        # construction - they cannot mask whatever we are already unwinding.
        try:
            await lane.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("renderer lane shutdown failed", exc_info=True)
