# Responsibility: Describe the scene to the reviewer in words: where the camera is and what the colours mean.
# Boundaries: presentation of scene facts; it moves no camera.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import SceneContext

# Reproduced verbatim from `get_navigation_context()`. It is prose, so it belongs here rather
# than in the renderer that used to emit it.
_NAV_NOTE = (
    "move_camera distance defaults to pan_step_mm if omitted. "
    "zoom() uses zoom_step if factor omitted. "
    "Call set_navigation_defaults() to change either."
)


def navigation_context(scene: SceneContext) -> dict:
    bbox: dict = {}
    if scene.bounds is not None:
        b = scene.bounds
        bbox = {
            "xmin": b.xmin, "xmax": b.xmax,
            "ymin": b.ymin, "ymax": b.ymax,
            "zmin": b.zmin, "zmax": b.zmax,
            "units": scene.mesh_units,
        }
    return {
        "bbox_mm": bbox,
        "pan_step_mm": scene.pan_step_mm,
        "zoom_step": scene.zoom_step,
        "note": _NAV_NOTE,
    }


def patch_colour_legend(scene: SceneContext) -> str:
    parts = [f"{e.patch_id}={e.color}" for e in scene.patch_legend]
    return ", ".join(parts) if parts else "see mesh colours"


def go_to_coordinates_text(
    scene: SceneContext,
    *,
    x: float,
    y: float,
    z: float,
    span: float,
    preset: str = "",
    patch_name: str = "",
) -> str:
    u = scene.mesh_units
    preset_note = f" preset={preset}," if preset else ""
    isolate_note = f" (isolated: {patch_name})" if patch_name else ""
    return (
        f"Camera{preset_note} centred on ({x:.4g}, {y:.4g}, {z:.4g}) {u}, "
        f"span={span:.4g} {u}.{isolate_note} "
        f"Pan step recalibrated to {scene.pan_step_mm:.4g} {u}."
    )


def move_camera_text(
    scene: SceneContext,
    *,
    direction: str,
    distance_mm: float | None,
) -> str:
    used = distance_mm if distance_mm is not None else scene.pan_step_mm
    return f"Camera moved {direction} by {used:g} {scene.mesh_units}."
