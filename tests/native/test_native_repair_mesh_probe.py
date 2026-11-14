# Responsibility: Verify the gmsh mesh probe against the real gmsh and OCC kernels.
# Boundaries: the native tier; the error classifier's string handling is unit-tested separately.
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402

from meshpipeline.cad.repair import mesh_probe  # noqa: E402
from meshpipeline.cad.repair.mesh_probe import (  # noqa: E402
    EDGE_THROUGH_FACE,
    OPEN_LOOP,
    OVERLAPPING_FACETS,
    PERIODIC_SURFACE,
    TIMEOUT,
    probe_file,
)


def _box(x=0.0, y=0.0, z=0.0, size=10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), size, size, size).Shape()


def _write(shape, path):
    assert BRepTools.Write_s(shape, str(path))
    return path


def test_box_meshes(tmp_path):
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is True
    assert result.volume_elements > 0
    assert result.failure is None


_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


@pytest.mark.parametrize("name, diagonal, kind", [
    ("abc/00500400_bce216c87d2d17025db6ab36_step_000.step", 132.3, OVERLAPPING_FACETS),
    ("occt/bug32596_abc_0_13.step", 59.5, PERIODIC_SURFACE),
    ("occt/trj10_ch1-id-chs.stp", 463.5, OPEN_LOOP),
    ("abc/00506550_b5806bb84542de8e2656bc88_step_114.step", 870.6, EDGE_THROUGH_FACE),
])
def test_corpus_failures_are_classified_and_located(name, diagonal, kind):
    result = probe_file(_corpus(name), diagonal_mm=diagonal)
    assert result.meshed is False
    assert result.failure is not None
    assert result.failure.kind == kind
    assert result.failure.surfaces
    assert result.tag_boxes


def test_given_surfaces_skip_the_volume(tmp_path):
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3, surfaces=(1,))
    assert result.failure is None
    assert result.volume_elements == 0
    assert len(result.tag_boxes) == 6


def test_unreadable_part_is_a_failure_not_an_exception(tmp_path):
    bad = tmp_path / "bad.brep"
    bad.write_text("this is not a BREP")
    result = probe_file(bad, diagonal_mm=10.0)
    assert result.meshed is False
    assert result.failure is not None and result.failure.message


def test_tag_boxes_cover_every_surface(tmp_path):
    shape = _box()
    faces = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        faces += 1
        explorer.Next()
    path = _write(shape, tmp_path / "box.brep")
    full = probe_file(path, diagonal_mm=17.3)
    only_boxes = probe_file(path, diagonal_mm=17.3, surfaces=())
    assert len(full.tag_boxes) == faces == 6
    assert len(only_boxes.tag_boxes) == faces
    assert only_boxes.volume_elements == 0


def test_probe_survives_child_crash(monkeypatch, tmp_path):
    def crash(*_args):
        os._exit(139)

    monkeypatch.setattr(mesh_probe, "_probe_child", crash)
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is False
    assert result.failure is not None and result.failure.kind == TIMEOUT


def test_budget_is_honoured():
    path = _corpus("occt/bug32239.STEP")   # takes ~12 s to fail unbounded
    start = time.monotonic()
    result = probe_file(path, diagonal_mm=153.3, budget_s=1.0)
    assert time.monotonic() - start < 3.0   # budget + 2 s
    assert result.meshed is False
    assert result.failure is not None and result.failure.kind == TIMEOUT
