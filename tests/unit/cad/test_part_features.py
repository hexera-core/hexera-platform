# Responsibility: Verify NOTE-6 / PLAN-fea-32: the areas inside a part (holes and alike-hole groups, bores, fillets, bosses, thin walls, large flat faces) are read off the measurement, each face in one area at most; the words tie a phrase to an area as to a part, "everything else" never takes an area; and an area's size wins over its part's on its faces, with a zone of its own, never setting the base.
# Boundaries: records and surfaces written here; no CAD reader, no model, no mesher.
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.cad import part_features as PF
from meshpipeline.cad import part_words as PW
from meshpipeline.engines import fea_part_sizes as PS
from meshpipeline.engines import fea_plan as P


def _surface(n_faces: int):
    xyz, tris, label = [], [], []
    for k in range(n_faces):
        base = len(xyz)
        xyz += [[k * 10.0, 0, 0], [k * 10.0 + 5, 0, 0], [k * 10.0, 5, 0]]
        tris.append([base, base + 1, base + 2])
        label.append(k)
    return SimpleNamespace(xyz=np.array(xyz), tris=np.array(tris), label=np.array(label),
                           names=[f"Face_{k}" for k in range(1, n_faces + 1)])


RECORD = {
    "faces": {"Face_1": {"kind": "cylinder", "radius_mm": 1.0, "concave": True, "area_mm2": 20},
              "Face_2": {"kind": "cylinder", "radius_mm": 1.01, "concave": True, "area_mm2": 20},
              "Face_3": {"kind": "torus", "radius_mm": 2.0, "concave": True, "area_mm2": 10},
              "Face_4": {"kind": "cylinder", "radius_mm": 6.0, "concave": False, "area_mm2": 200},
              "Face_5": {"kind": "plane", "area_mm2": 5000, "thickness_median_mm": 10},
              "Face_6": {"kind": "plane", "area_mm2": 4000, "thickness_median_mm": 10},
              "Face_7": {"kind": "plane", "area_mm2": 50, "thickness_median_mm": 2},
              "Face_8": {"kind": "plane", "area_mm2": 60, "thickness_median_mm": 10}},
    "hot_spots": [{"type": "hole", "radius_mm": 1.0, "faces": ["Face_1"]},
                  {"type": "hole", "radius_mm": 1.01, "faces": ["Face_2"]},
                  {"type": "fillet", "radius_mm": 2.0, "faces": ["Face_3"]}]}


def test_areas_are_read_off_the_measurement_each_face_once():
    rows, faces = PF.features(RECORD, _surface(8))
    names = [r["name"] for r in rows]
    assert names[0] == "2 holes, 2 mm across" and faces["Feature_1"] == ["Face_1", "Face_2"]
    assert "a fillet, radius 2 mm" in names and "a boss or shaft, 12 mm across" in names
    assert any(n.startswith("thin walls") for n in names)
    assert any(n.startswith("a large flat face, 5,000") for n in names)
    seen = [f for fs in faces.values() for f in fs]
    assert len(seen) == len(set(seen))
    assert all(r["feature"] and r["in_part"] == "Part_1" and r.get("diag_mm") for r in rows)


def test_everything_else_never_takes_an_area():
    table = [{"id": "Part_1", "diag_mm": 100.0}, {"id": "Feature_1", "feature": True, "diag_mm": 8.0}]
    notes = "0.5 mm at the two small holes, 3 mm elsewhere"
    answer = {"phrases": [
        {"parts": ["Feature_1"], "everything_else": False, "ask": "size", "min": 0.5, "max": 0.5, "unit": "mm",
         "quote": "0.5 mm at the two small holes", "why": "the two small holes", "confidence": 0.9},
        {"parts": [], "everything_else": True, "ask": "size", "min": 3, "max": 3, "unit": "mm",
         "quote": "3 mm elsewhere", "why": "the rest", "confidence": 0.9}], "words": []}
    entries, dropped, _w = PW.check(json.dumps(answer), table, notes)
    assert not dropped
    sizes = PS.sizes_of({"status": "used", "entries": entries, "features": {"Feature_1": ["Face_1", "Face_2"]},
                         "parts": [{"id": "Feature_1", "feature": True, "name": "2 holes, 2 mm across"}]})
    assert sizes["Feature_1"]["faces"] == ["Face_1", "Face_2"] and sizes["Feature_1"]["name"] == "2 holes, 2 mm across"
    assert sizes["Part_1"]["rest"] and "faces" not in sizes["Part_1"]


def test_an_areas_size_wins_on_its_faces_and_sets_no_base():
    zones = [{"id": "H1", "kind": "hole", "faces": ["Face_1"], "size_m": 0.0002, "radius_m": 0.001},
             {"id": "W5", "kind": "wall", "faces": ["Face_5"], "size_m": 0.002, "thickness_m": 0.01}]
    sizes = {"Part_1": {"min_m": 0.003, "max_m": 0.003},
             "Feature_1": {"min_m": 0.0005, "max_m": 0.0005, "faces": ["Face_1", "Face_2"]}}
    out = {z["id"]: z for z in P.user_sized(zones, sizes)}
    assert out["H1"]["user"] == "Feature_1" and out["H1"]["size_m"] == pytest.approx(0.0005)
    assert out["W5"]["user"] == "Part_1" and out["W5"]["size_m"] == pytest.approx(0.003)
    area = out["U1"]
    assert area["kind"] == "area" and area["faces"] == ["Face_1", "Face_2"] and area["size_m"] == pytest.approx(0.0005)
    assert P.user_base(sizes) == pytest.approx(0.003)


def test_the_pictures_name_areas_and_grey_the_rest():
    _rows, faces = PF.features(RECORD, _surface(8))
    shown = PF.labelled(_surface(8), faces, parts=False)
    assert shown.names[0].startswith("Feature_1_Face_") and any(n.startswith("Rest_Face_") for n in shown.names)
    from meshpipeline.render.part_pictures import number
    assert number("Feature_3") == "F3" and number("Part_3") == "3"
