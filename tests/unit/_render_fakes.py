# Responsibility: Be the shared render-backend fake for the unit tier.
# Boundaries: one fake honouring the backend's surface, so a test cannot pass against a shape production does not have.
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

PATCHES = ["inlet", "outlet", "wall"]
REGIONS = [{"name": "midspan", "kind": "slice", "normal": [0, 1, 0], "origin": [1.0, 2.0, 3.0]},
           {"name": "nearwall", "kind": "nearwall", "normal": [0, 0, 1]}]


# Per-operation PNG sentinels. ONE fixture image for every operation is how a crop command
# returning a full-view screenshot passed 39/39: both paths produced "a valid PNG", so nothing
# could tell them apart. These differ by content, so substituting one for another is visible.
def _png(tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag + b"\x00" * 64


PNG = _png(b"FULLVIEW")          # the generic screenshot
PNG_CROP = _png(b"CROP")         # zoom_to_region's magnified region
PNG_SLICE = _png(b"SLICE")       # inspect_region's section


class FakeBackend:

    def __init__(self, *, patches=None, shot=True, save_dir=None):
        self.patches = list(PATCHES if patches is None else patches)
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self.visible = dict.fromkeys(self.patches, True)
        self.pan_step, self.zoom_step = 10.0, 1.5
        self.preset = "iso"
        self.isolated = None
        self.closed = 0
        self.generic_shots = 0
        self._shot = shot
        self._n = 0
        self._dir = save_dir or Path(tempfile.mkdtemp())
        self.last_shot_path = None

    def patch_names(self): return list(self.patches)
    def has_geometry(self): return bool(self.patches)

    def _write(self, tag, data):
        self._n += 1
        if not self._shot:
            self.last_shot_path = None
            return ""
        path = self._dir / f"{tag}{self._n:03d}.png"
        path.write_bytes(data)
        self.last_shot_path = path
        return base64.b64encode(data).decode()

    def take_screenshot(self):
        self.calls.append(("take_screenshot",))
        self.generic_shots += 1
        return self._write("shot", PNG)

    def inspect_region(self, region):
        self.calls.append(("inspect_region", region.region_id))
        return self._write("slice", PNG_SLICE)

    def zoom_to_region(self, screen_x=0.5, screen_y=0.5, magnification=4.0):
        self.calls.append(("zoom_to_region", screen_x, screen_y, magnification))
        return self._write("crop", PNG_CROP)

    def set_camera_preset(self, p): self.preset = p; self.calls.append(("set_camera_preset", p))
    def move_camera(self, d, a=None): self.calls.append(("move_camera", d, a))
    def rotate_camera(self, a, d): self.calls.append(("rotate_camera", a, d))
    def zoom(self, f=None): self.calls.append(("zoom", f))

    def go_to_coordinates(self, x, y, z, span, preset=None, patch_name=None):
        # The full signature, kwargs included. A fake taking only four positional args made a
        # dropped preset/patch_name indistinguishable from a forwarded one.
        self.calls.append(("go_to_coordinates", x, y, z, span))
        self.kwargs.append({"preset": preset, "patch_name": patch_name})
        if preset: self.preset = preset
        if patch_name:
            self.isolated = patch_name
            self.visible = {p: (p == patch_name) for p in self.patches}
        self.pan_step = span * 0.10

    def reset_view(self):
        self.preset = "iso"; self.isolated = None
        self.visible = dict.fromkeys(self.patches, True)
        self.calls.append(("reset_view",))

    def toggle_patch(self, name, visible):
        self.visible[name] = visible
        self.calls.append(("toggle_patch", name, visible))

    def set_navigation_defaults(self, pan_step_mm=None, zoom_step=None):
        if pan_step_mm is not None and pan_step_mm > 0: self.pan_step = float(pan_step_mm)
        if zoom_step is not None and zoom_step > 0: self.zoom_step = float(zoom_step)
        self.calls.append(("set_navigation_defaults", pan_step_mm, zoom_step))
        return {"pan_step_mm": self.pan_step, "zoom_step": self.zoom_step}

    def get_navigation_context(self):
        # The REAL note, verbatim from the backend. A placeholder here would let the runtime's
        # prompt drift from production while the equivalence test compared "..." to "...".
        return {"bbox_mm": {}, "pan_step_mm": self.pan_step, "zoom_step": self.zoom_step,
                "note": ("move_camera distance defaults to pan_step_mm if omitted. "
                         "zoom() uses zoom_step if factor omitted. "
                         "Call set_navigation_defaults() to change either.")}

    def close(self): self.closed += 1


