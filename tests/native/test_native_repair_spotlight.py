# Responsibility: Verify the spotlight gives every located defect a finite camera, and that a rim's
#                 camera looks along the bore it bounds.
# Boundaries: the native tier, because only here does the CAD kernel exist. Real STEP files read
#             back and scanned; nothing is asserted about the console or the review items.
from __future__ import annotations

import math
from pathlib import Path

import pytest
from tests.cad_fixtures import write_loose_faces_step, write_missing_bore_wall_step

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")


def _scanned(path: Path):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read_shape(path)
    defects, _ = localize_scan(shape)
    return shape, defects


def _part_box(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box.Get()


def test_every_located_defect_gets_a_finite_camera(tmp_path):
    from meshpipeline.cad.repair.spotlight import camera_for

    shape, defects = _scanned(write_loose_faces_step(tmp_path / "loose.step"))
    assert len(defects) == 6, [d.to_dict()["entity"] for d in defects]
    x0, y0, z0, x1, y1, z1 = _part_box(shape)
    diagonal = math.dist((x0, y0, z0), (x1, y1, z1))
    for defect in defects:
        cam = camera_for(shape, defect)
        c, n = cam["centroid_mm"], cam["normal"]
        assert len(c) == 3 and all(math.isfinite(v) for v in c)
        for v, lo, hi in zip(c, (x0, y0, z0), (x1, y1, z1), strict=True):
            assert lo - 1e-6 <= v <= hi + 1e-6
        assert math.isclose(math.hypot(*n), 1.0, abs_tol=1e-6)
        assert 0.0 < cam["extent_mm"] <= diagonal


def test_a_rim_camera_points_along_the_bore_axis(tmp_path):
    from meshpipeline.cad.repair.spotlight import camera_for

    shape, defects = _scanned(write_missing_bore_wall_step(tmp_path / "bore.step"))
    rims = [d for d in defects if d.entity_type in ("edge", "wire")]
    assert rims, [d.to_dict()["entity"] for d in defects]
    for defect in rims:
        n = camera_for(shape, defect)["normal"]
        # the bore runs along z; a camera looking down or up the bore is within 15 degrees
        assert abs(n[2]) >= math.cos(math.radians(15)), (defect.entity_type, n)


def test_an_unresolvable_entity_falls_back_to_the_shape_centre(tmp_path):
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect
    from meshpipeline.cad.repair.spotlight import camera_for

    shape, _ = _scanned(write_loose_faces_step(tmp_path / "loose.step"))
    ghost = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                         entity_type="face", entity_index=999, message="gone")
    cam = camera_for(shape, ghost)
    x0, y0, z0, x1, y1, z1 = _part_box(shape)
    assert cam["centroid_mm"] == pytest.approx([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2])
    assert cam["extent_mm"] == pytest.approx(math.dist((x0, y0, z0), (x1, y1, z1)) / 20)
    assert math.isclose(math.hypot(*cam["normal"]), 1.0, abs_tol=1e-6)


def test_a_face_camera_points_away_from_the_part_centre(tmp_path):
    """Direction, not just axis: on a box a face's outward normal agrees with centre-to-face."""
    from meshpipeline.cad.repair.spotlight import camera_for

    shape, defects = _scanned(write_loose_faces_step(tmp_path / "loose.step"))
    x0, y0, z0, x1, y1, z1 = _part_box(shape)
    centre = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect

    # the scan reports this fixture's defects as edges; also aim straight at each of its faces
    faces = [EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                          entity_type="face", entity_index=i, message="face")
             for i in range(1, 7)]
    for defect in [*faces, *defects]:
        cam = camera_for(shape, defect)
        outward = [c - m for c, m in zip(cam["centroid_mm"], centre, strict=True)]
        assert sum(a * b for a, b in zip(cam["normal"], outward, strict=True)) > 0
        # and it is a real face normal: axis-aligned, which the centre-to-centroid fallback is
        # only by coincidence of a centred box
        assert max(abs(c) for c in cam["normal"]) == pytest.approx(1.0, abs=1e-6)


def test_the_rim_normal_comes_from_the_adjacent_face_not_the_fallback(tmp_path):
    """Both fallbacks answer +z, the bore axis, so the axis test alone cannot see them. Pin the
    real path directly: the face lookup must answer for every rim, and the camera must go
    through it."""
    from meshpipeline.cad.repair import spotlight
    from meshpipeline.cad.repair.operations import box_of, sub_shape

    shape, defects = _scanned(write_missing_bore_wall_step(tmp_path / "bore.step"))
    rims = [d for d in defects if d.entity_type in ("edge", "wire")]
    assert rims
    for defect in rims:
        sub = sub_shape(shape, defect.entity_type, defect.entity_index)
        n = spotlight._face_normal(shape, defect.entity_type, sub, box_of(sub)["centroid"])
        assert n is not None, "no adjacent face answered; the camera would be a fallback"
        assert abs(n[2]) == pytest.approx(1.0, abs=1e-6)
