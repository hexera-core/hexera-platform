# Responsibility: Pin PLAN-fea-5's cap in an assembly: a sharp inside corner whose faces read no wall of their own is capped against the thinnest wall of ITS OWN PART, never another part's (PLAN-fea-17: every part sized by its own rules); a single part keeps its own thinnest wall as before.
# Boundaries: engines/fea_plan.zones on hand-written measurement records.
from __future__ import annotations

from meshpipeline.engines import fea_plan as P

# THE DEFECT (job bf863215, 26 Oct): a tube's corners read no wall of their own and were capped at a
# quarter of the WHOLE ASSEMBLY's thinnest reading - a 0.0055 mm membrane artefact - 1.4 um elements
# along three edges. Any thin part (a 0.2 mm shim) would do the same to every other part's corners.


def _corner(faces):
    return {"id": "H1", "type": "sharp_corner", "faces": faces, "angle_deg": 90.0, "capped": True}


def _asm() -> dict:
    return {"thickness": {"min_mm": 0.2},
            "faces": {"Part_1_Face_1": {"thickness_min_mm": 3.0}, "Part_1_Face_2": {},
                      "Part_1_Face_3": {}, "Part_2_Face_1": {"thickness_min_mm": 0.2}},
            "part_bases": [{"n": 1, "id": "Part_1", "diag_mm": 50.0, "faces": ["Part_1_Face_1"]},
                           {"n": 2, "id": "Part_2", "diag_mm": 10.0, "faces": ["Part_2_Face_1"]}],
            "hot_spots": [_corner(["Part_1_Face_2", "Part_1_Face_3"])]}


def test_an_assembly_corner_is_capped_by_its_own_parts_wall():
    c = next(z for z in P.zones(_asm()) if z["kind"] == "corner")
    assert abs(c["size_m"] - 3.0 / P.CORNER_DIVISIONS * 1e-3) < 1e-12
    assert abs(c["thickness_m"] - 3.0e-3) < 1e-12


def test_a_part_with_no_wall_falls_back_to_the_assemblys_thinnest():
    rec = _asm()
    rec["faces"]["Part_1_Face_1"] = {}
    c = next(z for z in P.zones(rec) if z["kind"] == "corner")
    assert abs(c["thickness_m"] - 0.2e-3) < 1e-12


def test_a_single_part_keeps_its_own_thinnest_wall():
    rec = {"thickness": {"min_mm": 0.5}, "faces": {"Face_1": {"thickness_min_mm": 0.5}, "Face_2": {}, "Face_3": {}},
           "hot_spots": [_corner(["Face_2", "Face_3"])]}
    c = next(z for z in P.zones(rec) if z["kind"] == "corner")
    assert abs(c["thickness_m"] - 0.5e-3) < 1e-12


def test_a_corner_with_a_wall_of_its_own_keeps_it():
    rec = _asm()
    rec["hot_spots"] = [{**_corner(["Part_1_Face_2", "Part_1_Face_3"]), "cap_basis_mm": 1.0}]
    c = next(z for z in P.zones(rec) if z["kind"] == "corner")
    assert abs(c["thickness_m"] - 1.0e-3) < 1e-12
