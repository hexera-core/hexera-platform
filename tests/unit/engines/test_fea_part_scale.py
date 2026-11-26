# Responsibility: Verify PLAN-fea-36 - "finer" or "coarser" on a whole part with no number (read by cad/part_words and shown on the request card) reaches the solid plan: the part's own size halved or doubled, nothing in it raised past what the rules make finer, and said in the plan's words.
# Boundaries: pure over plain data; no model, no mesher.
from __future__ import annotations

import math

from meshpipeline.engines import fea_part_sizes as PS
from meshpipeline.engines import fea_plan as P

READING = {"status": "used", "features": {}, "parts": [],
           "entries": [{"ask": "coarsen", "parts": ["Part_2"], "quote": "coarser on the frame"},
                       {"ask": "refine", "parts": ["Part_3"], "quote": "refine the bracket"},
                       {"ask": "size", "parts": ["Part_4"], "min_mm": 1.0, "max_mm": 2.0, "quote": "1-2 mm on the pin"}]}
ZONES = [{"id": "P2", "kind": "part", "part": "Part_2", "size_m": 0.004},
         {"id": "W2", "kind": "wall", "faces": ["Part_2_Face_3"], "size_m": 0.001},
         {"id": "P3", "kind": "part", "part": "Part_3", "size_m": 0.004},
         {"id": "C3", "kind": "curve", "faces": ["Part_3_Face_1"], "size_m": 0.003},
         {"id": "H3", "kind": "hot_spot", "faces": ["Part_3_Face_2"], "size_m": 0.0005},
         {"id": "P4", "kind": "part", "part": "Part_4", "size_m": 0.004}]


def test_a_part_asked_finer_or_coarser_is_no_longer_dropped():
    # before PLAN-fea-36 only the size entry reached the plan (cad/part_words.per_part keeps "size" asks only)
    sizes = PS.sizes_of(READING)
    assert sizes["Part_2"]["scale"] == 2.0 and sizes["Part_3"]["scale"] == 0.5
    assert sizes["Part_4"]["min_m"] == 0.001 and "scale" not in sizes["Part_4"]


def test_coarser_doubles_the_part_and_keeps_its_finer_features():
    sizes = PS.resolve_scales(PS.sizes_of(READING), ZONES, base_m=0.004)
    zones = {z["id"]: z for z in P.user_sized(ZONES, sizes)}
    assert math.isclose(zones["P2"]["size_m"], 0.008)            # its own size doubled
    assert math.isclose(zones["W2"]["size_m"], 0.001)            # the wall keeps what its accuracy needs


def test_finer_halves_the_part_and_never_coarsens_a_feature():
    sizes = PS.resolve_scales(PS.sizes_of(READING), ZONES, base_m=0.004)
    zones = {z["id"]: z for z in P.user_sized(ZONES, sizes)}
    assert math.isclose(zones["P3"]["size_m"], 0.002)
    assert math.isclose(zones["C3"]["size_m"], 0.002)            # coarser than the new size: brought down to it
    assert math.isclose(zones["H3"]["size_m"], 0.0005)           # already finer: kept


def test_the_whole_meshs_size_is_the_base_when_the_notes_give_one():
    scaled = {k: v for k, v in PS.sizes_of(READING).items() if "scale" in v}
    whole = {"Part_2": {"min_m": 0.0025, "max_m": 0.0075, "whole": True}}
    got = PS.resolve_scales(scaled, ZONES, base_m=0.004, whole=whole)
    assert math.isclose(0.5 * (got["Part_2"]["min_m"] + got["Part_2"]["max_m"]), 0.01)


def test_the_plan_says_what_was_done():
    sizes = PS.resolve_scales(PS.sizes_of(READING), ZONES, base_m=0.004)
    words = P.user_words({"user_sizes": sizes, "zones": P.user_sized(ZONES, sizes)})
    assert "You asked for coarser elements on Part_2: its own size is doubled, from 4 mm to 8 mm" in words[0]
    assert "You asked for finer elements on Part_3: its own size is halved, from 4 mm to 2 mm" in words[1]


# an AREA asked finer or coarser with no number (NOTE-6): fea0254's "make the mesh finer at the fillets", round 7
AREA_READING = {"status": "used", "features": {"Feature_2": ["Face_7", "Face_8"]},
                "parts": [{"id": "Feature_2", "feature": True, "name": "2 rounded edges (fillets), radius 0.141 mm"}],
                "entries": [{"ask": "refine", "parts": ["Feature_2"], "quote": "make the mesh finer at the fillets"}]}
AREA_ZONES = [{"id": "P1", "kind": "part", "part": "Part_1", "size_m": 0.003},
              {"id": "W7", "kind": "wall", "faces": ["Face_7"], "size_m": 0.0008, "thickness_m": 0.0016},
              {"id": "R7", "kind": "round", "faces": ["Face_7"], "size_m": 0.0000554},
              {"id": "R8", "kind": "round", "faces": ["Face_8"], "size_m": 0.0000554},
              {"id": "W9", "kind": "wall", "faces": ["Face_8", "Face_9"], "size_m": 0.0009}]


def test_an_area_the_rules_already_make_finer_than_half_the_mesh_round_it_keeps_its_sizes():
    """Reading A (Areen, 25 Nov: "finer than the mesh around it"): fea0254's fillets are at 0.0554 mm by their rounds,
    far under half of the part's 3 mm - so they keep the rules' sizes and get no zone of their own (before, each zone
    on them was halved, and the knuckle's fillets went from 1 to 10 million elements). A zone on them coarser than the
    target (the wall's through-thickness size) is sized as off the area, and the user is told."""
    sizes = PS.sizes_of(AREA_READING)
    assert sizes["Feature_2"]["scale"] == 0.5 and sizes["Feature_2"]["faces"] == ["Face_7", "Face_8"]
    sizes = PS.resolve_scales(sizes, AREA_ZONES, base_m=0.003)
    got = sizes["Feature_2"]
    assert got["kept"] and math.isclose(got["target_m"], 0.0015) and math.isclose(got["there_m"], 0.0000554)
    zones = {z["id"]: z for z in P.user_sized(AREA_ZONES, sizes)}
    assert zones["R7"]["size_m"] == 0.0000554 and zones["R8"]["size_m"] == 0.0000554 and zones["W7"]["size_m"] == 0.0008
    assert zones["W9"]["size_m"] == 0.0009 and "user" not in zones["W9"] and zones["P1"]["size_m"] == 0.003
    assert not [z for z in zones.values() if z["kind"] == "area"]
    words = P.user_words({"user_sizes": sizes, "zones": list(zones.values())})
    assert words[0] == ("You asked for finer elements at 2 rounded edges (fillets), radius 0.141 mm: the mesh there is "
                        "already about 0.055 mm, finer than half of the part's 3 mm, so it keeps that size. Give a "
                        "size if you want it finer still.")
