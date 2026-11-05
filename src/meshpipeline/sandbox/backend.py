# Responsibility: Own the render scene: what is displayed, where the camera is, and what a screenshot shows.
# Boundaries: it renders handles it was given. It resolves no path, reads no manifest and dispatches on no engine name.
# Collaborates with: sandbox/mesh_reader.py, capture.py and camera_math.py.
from __future__ import annotations

import os as _os

# Headless SOFTWARE rendering - deterministic and GPU-FREE, so it runs identically on a
# laptop, a CPU-only cloud box, or CI. VTK uses an EGL render window backed by Mesa's
# llvmpipe software rasteriser; no GPU, no X server. There is no GPU/OSMesa fallback
# "switch" - we always render in software, which is plenty for offscreen mesh
# screenshots (a few seconds each). MUST run before pyvista/VTK is imported.
_os.environ["VTK_DEFAULT_RENDER_BACKEND"] = "egl"   # Mesa EGL → llvmpipe (software)
_os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"          # force software GL even if a GPU is present
_os.environ["GALLIUM_DRIVER"] = "llvmpipe"          # the Mesa software rasteriser
_os.environ.pop("DISPLAY", None)                    # no X server

import io
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyvista as pv
from PIL import Image

from meshpipeline.render import review_palette
from meshpipeline.sandbox.camera_math import (
    bounds_center_span,
    camera_right,
    camera_true_up,
    rodrigues,
)
from meshpipeline.sandbox.capture import SessionCapture, screenshot_array
from meshpipeline.sandbox.mesh_reader import read_mesh
from meshpipeline.sandbox.render_inputs import (
    RenderLimits,
    RenderMetadata,
    ResolvedRegion,
    ResolvedRenderInputs,
    legend_pairs,
)
from meshpipeline.sandbox.review_session import ResolvedArtifact

logger = logging.getLogger(__name__)


def _bound(method: Any) -> Callable[..., Any]:
    return cast("Callable[..., Any]", method)


_INV_SQRT3 = 1.0 / math.sqrt(3.0)
_PRESETS: dict[str, dict[str, np.ndarray]] = {
    "front":  {"dir": np.array([-1.0,  0.0,  0.0]), "up": np.array([0.0, 0.0, 1.0])},
    "rear":   {"dir": np.array([ 1.0,  0.0,  0.0]), "up": np.array([0.0, 0.0, 1.0])},
    "left":   {"dir": np.array([ 0.0, -1.0,  0.0]), "up": np.array([0.0, 0.0, 1.0])},
    "right":  {"dir": np.array([ 0.0,  1.0,  0.0]), "up": np.array([0.0, 0.0, 1.0])},
    "top":    {"dir": np.array([ 0.0,  0.0,  1.0]), "up": np.array([1.0, 0.0, 0.0])},
    "bottom": {"dir": np.array([ 0.0,  0.0, -1.0]), "up": np.array([1.0, 0.0, 0.0])},
    "iso":    {
        "dir": np.array([-_INV_SQRT3, -_INV_SQRT3, _INV_SQRT3]),
        "up":  np.array([0.0, 0.0, 1.0]),
    },
}

_BG_COLOR = (255, 255, 255)


class MeshRenderBackend:

    # `metadata` is REQUIRED. It used to default to a fabricated `RenderMetadata()`, which carried
    # `mesh_units="mm"` - so a backend constructed without metadata narrated a metre mesh in
    # millimetres. Both real callers build it from the manifest; nothing legitimately renders a
    # dimensioned scene without knowing the artifact's unit.
    def __init__(self, inputs: ResolvedRenderInputs,
                 metadata: RenderMetadata,
                 save_dir: str | Path | None = None,
                 limits: RenderLimits | None = None,
                 regions: dict[str, ResolvedRegion] | None = None) -> None:
        self._inputs = inputs
        self._meta = metadata
        self._limits = limits or RenderLimits()

        # Handles, not strings. There is no path here that the sandbox did not prove.
        self._surface: ResolvedArtifact = inputs.surface
        self._volume_artifact: ResolvedArtifact | None = inputs.volume
        self.msh_path: str = str(inputs.surface.path)

        self._isolated_patch: str | None = None

        self._zoom_step: float = 1.5
        self._current_preset: str = "iso"

        # Capture owns the save directory outright - creating it, numbering into it, and the
        # path of the last frame written. The backend does not read any of those.
        self._capture = SessionCapture(Path(save_dir) if save_dir else None)

        # Everything the scene is built FROM, read back from the file itself. The gmsh session,
        # the bounding box, the physical groups and the patch surfaces all belong to the reader;
        # what follows here is the VTK scene assembled from what it found.
        loaded = read_mesh(self.msh_path, self._meta.mesh_units)
        self._bbox = loaded.bbox
        self._pan_step = loaded.pan_step
        self._entity_tags_by_name = loaded.entity_tags_by_name
        self._physical_groups = loaded.physical_groups
        self._patch_meshes: dict[str, pv.PolyData] = loaded.patch_meshes
        self._visible = dict.fromkeys(self._entity_tags_by_name, True)

        pv.global_theme.allow_empty_mesh = True
        self._plotter = pv.Plotter(off_screen=True, window_size=[self._limits.screenshot_w, self._limits.screenshot_h])
        _bound(self._plotter.set_background)([c / 255.0 for c in _BG_COLOR])

        try:
            _rw_class = type(self._plotter.render_window).__name__
            logger.info("render backend: render window backend = %s", _rw_class)
            if "EGL" not in _rw_class:
                logger.warning(
                    "render backend: render window is %s, NOT EGL - repeated render() "
                    "calls may hit X_GLXMakeCurrent BadAccess. Verify DISPLAY is "
                    "unset and VTK_DEFAULT_RENDER_BACKEND=egl is set BEFORE pyvista "
                    "is imported.", _rw_class,
                )
        except Exception as exc:
            logger.debug("render backend: could not inspect render window class: %s", exc)

        # THE RENDERER ASSIGNS THE COLOURS. Deterministic, from the patch's own identity and
        # engine-declared role - never from the manifest, which is downstream of the component
        # whose mesh is being reviewed (render/review_palette.py).
        self._patch_rgb: dict[str, tuple[int, int, int]] = review_palette.assign(
            list(self._patch_meshes.keys()), self._meta.patch_roles)
        self._actors: dict[str, pv.Actor] = {}
        self._detail_actors: dict[str, pv.Actor | None] = {}
        for name, mesh in self._patch_meshes.items():
            rgb = self._patch_rgb.get(name, review_palette.REVIEW_CONTEXT_COLOR)
            color_f = tuple(c / 255.0 for c in rgb)
            actor = self._plotter.add_mesh(
                mesh,
                color=color_f,
                show_edges=True,
                edge_color="black",
                line_width=0.5,
                opacity=1.0,
                smooth_shading=False,
                lighting=False,
                backface_culling=False,
            )
            import vtk as _vtk
            bp = _vtk.vtkProperty()
            bp.SetColor(*color_f)
            bp.LightingOff()
            actor.SetBackfaceProperty(bp)
            self._actors[name] = actor
            self._detail_actors[name] = None

        # The volume is loaded lazily on first slice. Regions arrive ALREADY VALIDATED: the
        # backend never resolves a region name against manifest data at command time, so by
        # the time a region reaches it there is nothing left to look up and no untrusted
        # geometry left to trust.
        self._regions: dict[str, ResolvedRegion] = dict(regions or {})
        self._volume = None

        self._apply_preset_and_fit("iso")

        logger.info(
            "render backend (PyVista): loaded %s - patches: %s",
            self.msh_path, sorted(self._entity_tags_by_name.keys()),
        )

    def physical_groups(self) -> list[tuple[int, int, str, tuple[int, ...]]]:
        return list(self._physical_groups)

    def has_geometry(self) -> bool:
        return any(
            getattr(m, "n_points", 0) for m in (self._patch_meshes or {}).values()
        )

    def patch_names(self) -> list[str]:
        return sorted(self._actors)

    def _full_bounds(self) -> tuple[float, ...]:
        b = self._bbox
        return (
            b.get("xmin", 0.0), b.get("xmax", 1.0),
            b.get("ymin", 0.0), b.get("ymax", 1.0),
            b.get("zmin", 0.0), b.get("zmax", 1.0),
        )

    def _patch_bounds(self, name: str) -> tuple[float, ...] | None:
        mesh = self._patch_meshes.get(name)
        if mesh is None or mesh.n_points == 0:
            return None
        return tuple(mesh.bounds)

    def _apply_preset_and_fit(self, preset: str,
                              bounds: tuple[float, ...] | None = None) -> None:
        p = _PRESETS.get(preset, _PRESETS["iso"])
        direction: np.ndarray = p["dir"]
        up:        np.ndarray = p["up"]

        target_bounds = bounds if bounds is not None else self._full_bounds()
        center, span  = bounds_center_span(target_bounds)
        dist = span * 3.0

        self._plotter.camera.position    = tuple(center + direction * dist)
        self._plotter.camera.focal_point = tuple(center)
        self._plotter.camera.up          = tuple(up)

        if bounds is not None:
            _bound(self._plotter.reset_camera)(bounds=bounds)
        else:
            _bound(self._plotter.reset_camera)()

        self._current_preset = preset

    @property
    def last_shot_path(self) -> Path | None:
        return self._capture.last_shot_path

    def take_screenshot(self) -> str:
        return self._capture.screenshot(self._plotter)

    # internal inspection: slice the VOLUME mesh  #
    def _load_volume(self):
        if self._volume is not None:
            return self._volume
        if self._volume_artifact is None:
            return None
        # Already confined, existence-checked and format-checked by the resolver. There is no
        # join here any more: `self._workspace / <untrusted manifest value>` silently discarded
        # the workspace whenever the manifest value was absolute.
        vp = self._volume_artifact.path
        try:
            self._volume = pv.read(str(vp))
            logger.info("render backend: loaded volume %s (%d cells)", vp, self._volume.n_cells)
        except Exception as exc:
            logger.warning("render backend: failed to read volume %s: %s", vp, exc)
            self._volume = None
        return self._volume

    def inspect_region(self, region: ResolvedRegion) -> str:
        vol = self._load_volume()
        if vol is None:
            # No volume: nothing to cut. The caller decides what that MEANS - the backend only
            # reports that it produced no sectional evidence.
            return self.take_screenshot()
        normal = list(region.normal)
        origin = list(region.origin) if region.origin is not None else None
        try:
            sl = vol.slice(normal=normal, origin=origin)
            cb = region.clip_box
            if cb:
                sl = sl.clip_box(list(cb), invert=False)
        except Exception as exc:
            logger.warning("render backend: slice failed for %s: %s",
                           region.region_id, exc)
            return self.take_screenshot()
        p = pv.Plotter(off_screen=True, window_size=[self._limits.screenshot_w, self._limits.screenshot_h])
        _bound(p.set_background)([c / 255.0 for c in _BG_COLOR])
        if sl.n_cells:
            p.add_mesh(sl, show_edges=True, color=(0.80, 0.85, 0.92),
                       edge_color="black", line_width=1.0, lighting=False)
        ax = int(np.argmax(np.abs(normal)))
        _bound({0: p.view_yz, 1: p.view_xz, 2: p.view_xy}[ax])()
        try:
            img = screenshot_array(p)
        finally:
            try:
                p.close()
            except Exception:
                pass
        buf = io.BytesIO()
        Image.fromarray(img.astype(np.uint8)).save(buf, format="PNG")
        return self._capture.save_and_encode(buf.getvalue())


    def scene_facts(self) -> dict:
        return {
            "has_geometry": self.has_geometry(),
            "bounds": ({k: float(self._bbox[k])
                        for k in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")}
                       if self._bbox else None),
            "mesh_units": self._meta.mesh_units,
            "pan_step_mm": float(self._pan_step),
            "zoom_step": float(self._zoom_step),
            # the legend states the colour the reviewer is ACTUALLY looking at, as a hex
            # value the review-evidence contract accepts
            "patch_legend": legend_pairs(
                {n: review_palette.to_hex(c) for n, c in self._patch_rgb.items()},
                self._entity_tags_by_name),
            # RENDERER-DERIVED framing, from the geometry loaded above
            "patch_views": review_palette.derive_patch_views(
                self.patch_bounds(), self._meta.patch_roles),
        }

    def patch_bounds(self) -> dict[str, tuple[float, ...]]:
        # The bounds the renderer ACTUALLY loaded, per patch. Camera framing is derived from
        # this - never from a manifest, which is downstream of the component under review.
        out: dict[str, tuple[float, ...]] = {}
        for name, mesh in (self._patch_meshes or {}).items():
            b = getattr(mesh, "bounds", None)
            if b is not None and len(tuple(b)) >= 6:
                out[name] = tuple(float(v) for v in tuple(b)[:6])
        return out

    def get_patch_colour_legend(self) -> str:
        parts = [
            f"{name}={review_palette.to_hex(rgb)}"
            for name, rgb in self._patch_rgb.items()
            if name in self._entity_tags_by_name
        ]
        return ", ".join(parts) if parts else "see mesh colours"

    def get_navigation_context(self) -> dict:
        return {
            "bbox_mm":     self._bbox,
            "pan_step_mm": self._pan_step,
            "zoom_step":   self._zoom_step,
            "note": (
                "move_camera distance defaults to pan_step_mm if omitted. "
                "zoom() uses zoom_step if factor omitted. "
                "Call set_navigation_defaults() to change either."
            ),
        }

    def set_camera_preset(self, preset: str) -> None:
        if preset not in _PRESETS:
            logger.warning("set_camera_preset: unknown preset '%s'", preset)
            return
        self._apply_preset_and_fit(preset)

    def set_navigation_defaults(self, pan_step_mm: float | None = None,
                                zoom_step: float | None = None) -> dict:
        if pan_step_mm is not None and pan_step_mm > 0:
            self._pan_step = float(pan_step_mm)
        if zoom_step is not None and zoom_step > 0:
            self._zoom_step = float(zoom_step)
        logger.info("render backend: nav defaults → pan=%.1f zoom=%.2f",
                    self._pan_step, self._zoom_step)
        return {"pan_step_mm": self._pan_step, "zoom_step": self._zoom_step}

    def move_camera(self, direction: str, distance_mm: float | None = None) -> None:
        d = float(distance_mm) if distance_mm is not None else self._pan_step
        b = self._bbox
        domain_span = max(
            abs(b.get("xmax", 1) - b.get("xmin", 0)),
            abs(b.get("ymax", 1) - b.get("ymin", 0)),
            abs(b.get("zmax", 1) - b.get("zmin", 0)),
            1e-6,
        )
        d = min(abs(d), domain_span * 2.0) * (1 if d >= 0 else -1)

        pos   = np.array(self._plotter.camera.position)
        focal = np.array(self._plotter.camera.focal_point)
        right = camera_right(pos, focal, self._plotter.camera.up)
        up    = camera_true_up(pos, focal, self._plotter.camera.up)

        if direction == "right":
            delta = right * d
            self._plotter.camera.position    = tuple(pos   + delta)
            self._plotter.camera.focal_point = tuple(focal + delta)
        elif direction == "left":
            delta = right * d
            self._plotter.camera.position    = tuple(pos   - delta)
            self._plotter.camera.focal_point = tuple(focal - delta)
        elif direction == "up":
            delta = up * d
            self._plotter.camera.position    = tuple(pos   + delta)
            self._plotter.camera.focal_point = tuple(focal + delta)
        elif direction == "down":
            delta = up * d
            self._plotter.camera.position    = tuple(pos   - delta)
            self._plotter.camera.focal_point = tuple(focal - delta)
        elif direction == "forward":
            fwd_n = (focal - pos) / max(np.linalg.norm(focal - pos), 1e-12)
            self._plotter.camera.position = tuple(pos + fwd_n * d)
        elif direction == "back":
            fwd_n = (focal - pos) / max(np.linalg.norm(focal - pos), 1e-12)
            self._plotter.camera.position = tuple(pos - fwd_n * d)

    def rotate_camera(self, axis: str, degrees: float) -> None:
        pos   = np.array(self._plotter.camera.position)
        focal = np.array(self._plotter.camera.focal_point)
        if np.linalg.norm(pos - focal) < 1e-9:
            logger.warning("rotate_camera: camera coincides with focal point - skipping")
            return
        up    = np.array(self._plotter.camera.up)
        right = camera_right(pos, focal, up)
        fwd   = focal - pos

        if axis == "yaw":
            rot_axis = np.array([0.0, 0.0, 1.0])
        elif axis == "pitch":
            rot_axis = right
        elif axis == "roll":
            rot_axis = fwd / max(np.linalg.norm(fwd), 1e-12)
        else:
            logger.warning("rotate_camera: unknown axis '%s'", axis)
            return

        angle = math.radians(degrees)
        v_rot  = rodrigues(pos - focal, rot_axis, angle)
        up_rot = rodrigues(up,          rot_axis, angle)

        self._plotter.camera.position = tuple(focal + v_rot)
        self._plotter.camera.up       = tuple(up_rot)

    def zoom(self, factor: float | None = None) -> None:
        f = float(factor) if factor is not None else self._zoom_step
        f = max(f, 1e-3)
        self._plotter.camera.zoom(f)

    def go_to_coordinates(self, x: float, y: float, z: float, span: float,
                          preset: str | None = None,
                          patch_name: str | None = None) -> None:
        if patch_name is not None:
            for name in self._actors:
                vis = (name == patch_name)
                self._actors[name].visibility = vis
                self._visible[name] = vis
                detail = self._detail_actors.get(name)
                if detail is not None:
                    if vis:
                        self._actors[name].visibility = False
                        detail.visibility = True
                    else:
                        detail.visibility = False
            self._isolated_patch = patch_name

            bounds = self._patch_bounds(patch_name)
            if bounds is not None:
                center, patch_span = bounds_center_span(bounds)
                x, y, z = float(center[0]), float(center[1]), float(center[2])
                span = max(patch_span * 1.5, float(span))
                logger.info(
                    "MeshSandbox.go_to_coordinates: centroid override for '%s' "
                    "→ (%.3f, %.3f, %.3f) span=%.1f",
                    patch_name, x, y, z, span,
                )

        target_preset = preset if preset in _PRESETS else self._current_preset
        p         = _PRESETS[target_preset]
        direction = p["dir"]
        up        = p["up"]

        dist    = max(float(span), 1e-6) * 3.0
        cam_pos = np.array([x, y, z]) + direction * dist

        self._plotter.camera.position    = tuple(cam_pos)
        self._plotter.camera.focal_point = (x, y, z)
        self._plotter.camera.up          = tuple(up)

        if patch_name is not None:
            bounds = self._patch_bounds(patch_name)
            if bounds is not None:
                _bound(self._plotter.reset_camera)(bounds=bounds)
        else:
            _bound(self._plotter.reset_camera)()

        self._current_preset = target_preset
        self._pan_step = round(max(float(span), 1e-6) * 0.10, 6)

        logger.info(
            "MeshSandbox.go_to_coordinates: preset=%s target=(%.1f, %.1f, %.1f) span=%.1f",
            target_preset, x, y, z, span,
        )

    def zoom_to_region(self, screen_x: float = 0.5, screen_y: float = 0.5,
                       magnification: float = 4.0) -> str:
        f   = max(int(magnification), 2)
        w, h = self._limits.screenshot_w, self._limits.screenshot_h

        self._plotter.camera_set = True
        img_arr = screenshot_array(self._plotter, scale=f)
        self._plotter.image_scale = 1

        img = Image.fromarray(img_arr.astype(np.uint8))
        hi_w, hi_h = img.width, img.height

        px = int(max(0.0, min(1.0, screen_x)) * hi_w)
        py = int(max(0.0, min(1.0, screen_y)) * hi_h)

        left   = max(0,    px - w // 2)
        top    = max(0,    py - h // 2)
        right  = min(hi_w, left + w)
        bottom = min(hi_h, top  + h)

        img = img.crop((left, top, right, bottom))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return self._capture.save_and_encode(buf.getvalue())

    def toggle_patch(self, patch_name: str, visible: bool) -> None:
        if patch_name not in self._actors:
            logger.warning("toggle_patch: unknown patch '%s'", patch_name)
            return
        self._visible[patch_name] = visible
        self._actors[patch_name].visibility = visible

    def reset_view(self) -> None:
        for name in self._actors:
            self._actors[name].visibility = True
            self._visible[name] = True
        self._isolated_patch = None
        self._apply_preset_and_fit("iso")

    def close(self) -> None:
        self._capture.assemble_video()
        try:
            self._plotter.close()
        except Exception as exc:
            logger.warning("render backend: plotter.close() failed: %s", exc)
