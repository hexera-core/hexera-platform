# Responsibility: Verify the investigator's view really draws the region it was pointed at - a
#                 decodable, correctly sized, non-blank PNG from a `spotlight.camera_for` camera.
# Boundaries: the native tier, because only here do the CAD kernel and an off-screen GL context
#             exist. Nothing is written to the repository: every image stays in memory.
#             `_render_kernel` is called DIRECTLY here, because it is the part under test; the
#             bounded-child wrapper (`render_region`, a fresh interpreter - never a fork) is
#             exercised in the one test below, on every platform.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")
pytest.importorskip("pyvista", reason="off-screen rendering is a native-tier dependency")

def _box(dx: float = 10.0, dy: float = 20.0, dz: float = 30.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def _decoded(blob: bytes):
    import io

    from PIL import Image

    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(blob)).convert("RGB")


def _not_blank(img, pixels: int) -> None:
    colours = img.getcolors(maxcolors=pixels)
    assert colours is not None and len(colours) > 1, "the render is a single flat colour"
    busiest = max(count for count, _ in colours)
    assert busiest < 0.98 * pixels, "almost the whole frame is background"


def test_render_box_from_camera():
    from meshpipeline.cad.repair.investigator_tools import _render_kernel

    camera = {"centroid_mm": [5.0, 20.0, 15.0], "normal": [0.0, 1.0, 0.0], "extent_mm": 20.0}
    views = _render_kernel(_box(), [camera], (256, 256))
    assert len(views) == 1
    img = _decoded(views[0])
    assert img.size == (256, 256)
    _not_blank(img, 256 * 256)


def test_two_cameras_give_two_different_views():
    from meshpipeline.cad.repair.investigator_tools import _render_kernel

    cams = [{"centroid_mm": [5.0, 20.0, 15.0], "normal": [0.0, 1.0, 0.0], "extent_mm": 20.0},
            {"centroid_mm": [5.0, 10.0, 30.0], "normal": [0.0, 0.0, 1.0], "extent_mm": 20.0}]
    views = _render_kernel(_box(), cams, (128, 128))
    assert len(views) == 2
    for blob in views:
        img = _decoded(blob)
        assert img.size == (128, 128)
        _not_blank(img, 128 * 128)
    assert views[0] != views[1], "both cameras drew the same picture"


def test_a_camera_on_a_real_defect_renders(tmp_path):
    """The seam `look` actually uses: localize's defects -> `camera_for` -> a PNG."""
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.investigator_tools import _render_kernel
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.spotlight import camera_for

    shape = _read_shape(write_loose_faces_step(tmp_path / "loose.step"))
    defects, _ = localize_scan(shape)
    assert defects
    views = _render_kernel(shape, [camera_for(shape, defects[0])], (128, 128))
    assert len(views) == 1
    img = _decoded(views[0])
    assert img.size == (128, 128)
    _not_blank(img, 128 * 128)


def test_an_empty_shape_gives_no_view():
    """A shape with nothing to tessellate is an empty list, not an exception and not a blank."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair.investigator_tools import _render_kernel

    empty = TopoDS_Compound()
    BRep_Builder().MakeCompound(empty)
    camera = {"centroid_mm": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0], "extent_mm": 1.0}
    assert _render_kernel(empty, [camera], (64, 64)) == []


def test_render_region_draws_in_a_bounded_child():
    from meshpipeline.cad.repair.investigator_tools import render_region

    camera = {"centroid_mm": [5.0, 20.0, 15.0], "normal": [0.0, 1.0, 0.0], "extent_mm": 20.0}
    views = render_region(_box(), [camera], size=(128, 128), budget_s=120.0)
    assert len(views) == 1
    assert _decoded(views[0]).size == (128, 128)


def test_render_region_gives_nothing_rather_than_raising_when_there_is_no_time():
    """A budget of zero is silence, not an exception - `look` must still answer in text."""
    from meshpipeline.cad.repair.investigator_tools import render_region

    camera = {"centroid_mm": [5.0, 20.0, 15.0], "normal": [0.0, 1.0, 0.0], "extent_mm": 20.0}
    assert render_region(_box(), [camera], size=(64, 64), budget_s=0.0) == []
