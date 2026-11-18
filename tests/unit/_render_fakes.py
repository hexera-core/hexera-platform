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


def write_cut_png(path, width: int = 1280, height: int = 960, cells_px: int = 4):
    """A REAL PNG of the stated size, with a GRID OF CELLS drawn in it.

    Real, not the sentinels above, for two reasons that both bite. The prepared-cut reader takes
    the frame's pixel size from its own IHDR rather than from the manifest, so a stub with no
    header has no size and is refused; and `review_session._measure_png` COUNTS THE CELLS that
    are actually drawn, so a frame that will not decode measures nothing and is refused too - a
    test passing on an unreadable file would be passing for the wrong reason.

    A GRID, NOT A FILLED RECTANGLE, and that is not cosmetic. This drew one flat dark rectangle,
    which is 25% of the frame inked with not a single cell in it - and that is exactly what two
    of the three frames this engine really produces look like (a port bore and three plenum
    rectangles, 0.41% and 0.59% inked, no passage in either). A fixture that draws one is a
    fixture for the case that must be REFUSED, so using it to stand for a good frame makes a
    test that cannot tell the two apart.

    `cells_px` is how many pixels across each drawn cell is, so a caller can make a frame that
    resolves cells (4, the floor), one that cannot (1), one whose drawn structure is far bigger
    than the cell the window claims (32 - a picture of channels, not of cells), or a flat
    rectangle with no cells at all (0).
    """
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (int(width), int(height)), (255, 255, 255))
    d = ImageDraw.Draw(im)
    box = [width // 4, height // 4, width * 3 // 4, height * 3 // 4]
    if cells_px <= 0:
        d.rectangle(box, fill=(20, 20, 20))
    else:
        # A light fill with dark edges ruled across it, which is what `show_edges=True` over a
        # coloured mesh produces: the reader counts the lighter interiors that have a darker
        # pixel on BOTH sides, so the fill and the lines both have to be there.
        d.rectangle(box, fill=(176, 196, 222))
        for x in range(box[0], box[2] + 1, int(cells_px)):
            d.line([x, box[1], x, box[3]], fill=(0, 0, 0), width=1)
        for y in range(box[1], box[3] + 1, int(cells_px)):
            d.line([box[0], y, box[2], y], fill=(0, 0, 0), width=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


class FakeBackend:

    def __init__(self, *, patches=None, shot=True, save_dir=None, empty_cut=False):
        self.patches = list(PATCHES if patches is None else patches)
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self.visible = dict.fromkeys(self.patches, True)
        self.pan_step, self.zoom_step = 10.0, 1.5
        self.preset = "iso"
        self.isolated = None
        #: make inspect_region report a cut that exposed nothing - see inspect_region
        self.empty_cut = bool(empty_cut)
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
        """(the cut's image, "") - or ("", why there is no cut), as the backend now returns.

        `empty_cut` is the second half, which the old fake could not express at all: it always
        returned an image, so the adapter path for a cut that shows nothing had no test. The real
        backend has three ways to get there - no readable volume, a slice that raised, and a
        slice of no cells - and a live conjugate run went through the third of them twice and
        had both blank frames filed as usable evidence for its two regions.
        """
        self.calls.append(("inspect_region", region.region_id))
        if self.empty_cut:
            return "", f"the cut at {region.region_id} passed through no cells"
        return self._write("slice", PNG_SLICE), ""

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


