# Responsibility: Verify that every zone of an FEA assembly plan has its own id, so a lever, a check row or an AI choice that names a zone names one part's zone (PLAN-fea-17, rulebook NEXT).
# Boundaries: pure over hand-written measurement records; no geometry, no engine.
"""ONE ID, ONE ZONE (FEA hunt, 27 Oct).

Every part of an assembly numbers its faces from 1, and the wall, thin-spot and round zones were
named from the face number alone: Part_1_Face_9 and Part_2_Face_9 were both "W9". 44 of the hunt's
first 46 assembly plans had such twins. LAD-fea-3 tightens zones BY ID, the check's short list names
them by id and the AI refines them by id - so a fix meant for the 5 mm glass of a bus door also
refined the door's 80 mm wall, and "W9 is short" did not say which part.
"""
from __future__ import annotations

from collections import Counter

from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P

ASSEMBLY = {
    "bodies": 2,
    "thickness": {"min_mm": 5.0, "face": "Part_2_Face_9"},
    "faces": {
        "Part_1_Face_9": {"kind": "plane", "thickness_min_mm": 80.0},
        "Part_1_Face_10": {"kind": "cylinder", "thickness_min_mm": 150.0, "radius_mm": 30.0},
        "Part_2_Face_9": {"kind": "plane", "thickness_min_mm": 5.0,
                          "thin_spots": [{"thickness_mm": 3.0, "radius_mm": 4.0, "at_mm": [0, 0, 0]}]},
        "Part_2_Face_10": {"kind": "cylinder", "thickness_min_mm": 5.0, "radius_mm": 30.0},
    },
    "hot_spots": [],
}
SINGLE = {
    "bodies": 1,
    "thickness": {"min_mm": 5.0, "face": "Face_3"},
    "faces": {"Face_1": {"kind": "plane", "thickness_min_mm": 200.0},
              "Face_3": {"kind": "plane", "thickness_min_mm": 5.0}},
    "hot_spots": [],
}


def test_no_two_zones_of_an_assembly_share_an_id():
    zs = P.zones(ASSEMBLY)
    ids = Counter(z["id"] for z in zs)
    assert all(n == 1 for n in ids.values()), ids
    walls = {z["faces"][0]: z["id"] for z in zs if z["kind"] == "wall"}
    assert walls["Part_1_Face_9"] == "W1.9" and walls["Part_2_Face_9"] == "W2.9"


def test_a_single_part_keeps_its_ids():
    assert {z["id"] for z in P.zones(SINGLE) if z["kind"] == "wall"} == {"W1", "W3"}


def test_a_lever_naming_a_zone_tightens_that_part_only():
    lever = {"lever": "LAD-fea-3", "zones": ["W2.9"]}
    plain = {z["id"]: z["size_m"] for z in P.zones(ASSEMBLY)}
    tight = {z["id"]: z["size_m"] for z in P.zones(ASSEMBLY, tighten=L.tightened([lever]))}
    assert tight["W2.9"] < plain["W2.9"]
    assert tight["W1.9"] == plain["W1.9"]


def test_zones_come_part_by_part():
    walls = [z["faces"][0] for z in P.zones(ASSEMBLY) if z["kind"] == "wall"]
    assert walls == sorted(walls, key=lambda f: (int(f.split("_")[1]), int(f.rsplit("_", 1)[1])))
