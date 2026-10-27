# Responsibility: Pin PLAN-fea-12's rounds (27 Oct): a round kept only for its shape (PLAN-fea-7) under 1/1,000 of its own part is an edge break - no zone of its own, named in the plan - while every larger round, and every fillet or hole however small (a stress raiser, PLAN-fea-4), keeps its zone.
# Boundaries: engines/fea_plan on hand-written measurement records.
from __future__ import annotations

from meshpipeline.engines import fea_plan as P

# THE DEFECT (corpus sweep, 27 Oct): a 127 mm coil's 0.06 mm edge rounds asked for 0.024 mm elements - 31.6M.


def _record(radii: dict[str, float], diag_mm: float = 100.0, spots=()):
    return {"diag_mm": diag_mm, "thickness": {"min_mm": 5.0},
            "faces": {f: {"radius_mm": r, "thickness_min_mm": 5.0} for f, r in radii.items()},
            "hot_spots": list(spots)}


def test_a_shape_round_under_a_thousandth_of_its_part_gets_no_zone_and_is_named():
    rec = _record({"Face_1": 0.06, "Face_2": 0.5})
    rounds = {z["faces"][0] for z in P.zones(rec) if z["kind"] == "round"}
    assert rounds == {"Face_2"}
    assert P.micro_rounds(rec) == [{"face": "Face_1", "radius_mm": 0.06}]


def test_an_assembly_part_is_judged_by_its_own_size():
    rec = _record({"Part_1_Face_1": 0.06, "Part_2_Face_1": 0.06}, diag_mm=1000.0)
    rec["part_bases"] = [{"n": 1, "id": "Part_1", "diag_mm": 10.0, "faces": ["Part_1_Face_1"]},
                         {"n": 2, "id": "Part_2", "diag_mm": 600.0, "faces": ["Part_2_Face_1"]}]
    rounds = {z["faces"][0] for z in P.zones(rec) if z["kind"] == "round"}
    assert rounds == {"Part_1_Face_1"}           # 0.06 mm on a 10 mm part is a real round


def test_a_small_fillet_is_still_a_stress_raiser():
    spot = {"id": "H1", "type": "fillet", "shape": "fillet", "faces": ["Face_1"], "radius_mm": 0.06,
            "local_thickness_mm": 5.0}
    rec = _record({"Face_1": 0.06}, spots=[spot])
    assert "fillet" in [z["kind"] for z in P.zones(rec) if z["faces"] == ["Face_1"]]
    assert P.micro_rounds(rec) == []
