# Responsibility: Hold the face map (NOTE-17, cad/face_map) to its contract: every face of a CAD part named as the FEA
# deck names it, a triangle file's smooth regions as Region_k, a click on the skin resolved to the face under it (never
# a face when the click is off the part), the chip's plain words, the highlight triangles, and the signature a run
# re-checks - in the skin's unit even after the unit was confirmed differently.
# Boundaries: parts are built in the test (OpenCASCADE); nothing is stored.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.cad import face_map as FM

pytest.importorskip("OCP.BRepPrimAPI")


@pytest.fixture(scope="module")
def plate():
    from tests import fea_fixtures as fx
    # 120 x 60 x 4 mm, a 15 mm hole through the middle (centre 60, 30)
    return FM.from_cad(fx.plate_with_hole(), mm_per_unit=1.0, scale_to_m=0.001)


def _mm(*p):
    return [v / 1000.0 for v in p]


def test_a_cad_part_is_mapped_face_by_face_in_the_fea_decks_numbering(plate):
    from tests import fea_fixtures as fx

    from meshpipeline.cad.fea_measure import _brep_surface

    surface, _faces, _kinds = _brep_surface(fx.plate_with_hole(), 1.0)
    assert plate.names == list(surface.names) and plate.names[0] == "Face_1"
    assert plate.basis == "cad" and plate.xyz.dtype == np.float32
    assert len(plate.tris) == len(plate.label) and set(np.unique(plate.label)) <= set(range(len(plate.names)))
    # the skin is in metres: the plate is 0.12 m long
    assert float(plate.xyz[:, 0].max() - plate.xyz[:, 0].min()) == pytest.approx(0.12, rel=1e-3)


def test_a_click_on_the_bore_names_the_bore_in_plain_words(plate):
    k = FM.face_at(plate, _mm(60 + 7.5, 30, 2), [1, 0, 0])
    assert k is not None and plate.kind[k] == "cylinder"
    assert FM.label(plate, k) == f"{plate.names[k].replace('_', ' ')} · round Ø15 mm"


def test_a_click_on_the_top_names_a_flat_face_with_its_size(plate):
    k = FM.face_at(plate, _mm(10, 10, 4), [0, 0, 1])
    assert k is not None and plate.kind[k] == "plane"
    assert FM.label(plate, k).endswith("flat 120 × 60 mm")
    pts, tris = FM.highlight(plate, k)
    assert pts.dtype == np.float32 and tris.dtype == np.uint32 and len(tris) >= 2
    assert np.allclose(pts[:, 2], 0.004, atol=1e-7)


def test_a_click_off_the_part_names_no_face(plate):
    assert FM.face_at(plate, _mm(60, 30, 40), [0, 0, 1]) is None
    assert FM.face_at(plate, _mm(60, 30, 2), [0, 0, 1]) is None          # inside the hole, far from its wall
    assert FM.face_at(plate, [float("nan")] * 3) is None


def test_a_unit_confirmed_since_reads_the_click_in_the_new_unit(plate):
    # the skin was rescaled by 25.4 (inches confirmed): the same place on it is 25.4 times further out
    k = FM.face_at(plate, [v * 25.4 for v in _mm(10, 10, 4)], [0, 0, 1], scale=25.4)
    assert k is not None and plate.kind[k] == "plane"
    sig = FM.signature(plate, k, scale=25.4)
    assert sig["area_m2"] == pytest.approx(float(plate.area_m2[k]) * 25.4 ** 2)
    assert FM.same_face(plate, plate.names[k], sig, scale=25.4)


def test_a_face_is_the_same_only_with_its_own_area_and_place(plate):
    k = FM.face_at(plate, _mm(10, 10, 4), [0, 0, 1])
    sig = FM.signature(plate, k)
    assert FM.same_face(plate, plate.names[k], sig)
    assert not FM.same_face(plate, plate.names[k], {**sig, "area_m2": sig["area_m2"] * 1.5})
    assert not FM.same_face(plate, plate.names[k], {**sig, "centroid_m": [c + 0.01 for c in sig["centroid_m"]]})
    assert not FM.same_face(plate, "Face_999", sig)


def test_the_map_survives_storage_unchanged(plate):
    back = FM.from_bytes(FM.to_bytes(plate))
    assert back.names == plate.names and back.kind == plate.kind and back.basis == "cad"
    assert np.array_equal(back.tris, plate.tris) and np.array_equal(back.label, plate.label)
    assert back.scale_to_m == pytest.approx(0.001)
    assert FM.label(back, 0) == FM.label(plate, 0)


def test_a_triangle_file_is_mapped_by_its_smooth_regions():
    from tests import fea_fixtures as fx

    fm = FM.from_triangles(fx.stl_triangles(fx.plate_with_hole()), scale_to_m=0.001)
    assert fm.basis == "triangles" and all(n.startswith("Region_") for n in fm.names)
    k = FM.face_at(fm, _mm(10, 10, 4), [0, 0, 1])
    assert k is not None and bool(fm.flat[k])
    assert FM.label(fm, k) == f"{fm.names[k].replace('_', ' ')} · flat 120 × 60 mm"
    bore = FM.face_at(fm, _mm(60 + 7.5, 30, 2), [1, 0, 0])
    assert bore is not None and not bool(fm.flat[bore]) and FM.label(fm, bore).split(" · ")[1].startswith("curved")


def test_an_assemblys_faces_carry_their_part():
    from tests import fea_fixtures as fx

    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 10, 10, 10))
    fm =FM.from_cad(shape, mm_per_unit=1.0, scale_to_m=0.001)
    assert fm.names[0] == "Part_1_Face_1" and any(n.startswith("Part_2_Face_") for n in fm.names)
    k = FM.face_at(fm, _mm(25, 5, 10), [0, 0, 1])
    assert FM.label(fm, k).startswith("Part 2 face ")
