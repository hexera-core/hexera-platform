# Responsibility: Verify PLAN-fea-29/30/31 and AI-fea-9: the sizes the user asked for per part hold every size in those parts inside [min, max] - past the accuracy minimums (said), never past the validity limits (said) - the count moves only the parts they did not size, a count that builds off what was asked is re-aimed once on the built count, the estimate reads thin walls at coarse sizes, and a plan the user's choices made is never short "by mistake" (no ladder tightening).
# Boundaries: zones written here and one plate with a hole planned for real (gmsh + OCP, skipped without them). Nothing is meshed.
from __future__ import annotations

import math

import pytest

from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_ladder as L
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import fea_size_field as SF

Q = math.pi / 2.0
WALL = {"id": "W9.3", "kind": "wall", "faces": ["Part_9_Face_3"], "size_m": 0.0006, "reach_m": 0.0012,
        "thickness_m": 0.0012}
FILLET = {"id": "H8", "kind": "fillet", "faces": ["Part_7_Face_1"], "size_m": Q * 0.00045 / 6, "reach_m": 0.0002,
          "radius_m": 0.00045, "thickness_m": 0.0094, "spot": "H8"}
HOLE = {"id": "H22", "kind": "hole", "faces": ["Part_9_Face_27"], "size_m": Q * 0.00245 / 6, "reach_m": 0.001,
        "radius_m": 0.00245, "spot": "H22"}
PART9 = {"id": "P9", "kind": "part", "faces": ["Part_9_Face_1"], "size_m": 0.0533, "reach_m": 0.0, "part": "Part_9",
         "diag_m": 0.533}
PART7 = {"id": "P7", "kind": "part", "faces": ["Part_7_Face_1"], "size_m": 0.00458, "reach_m": 0.0, "part": "Part_7",
         "diag_m": 0.0458}
OTHER = {"id": "W4.1", "kind": "wall", "faces": ["Part_4_Face_1"], "size_m": 0.002, "reach_m": 0.004,
         "thickness_m": 0.004}
SIZES = {"Part_9": {"min_m": 0.003, "max_m": 0.004}, "Part_7": {"min_m": 0.0015, "max_m": 0.002}}


def _by(zs):
    return {z["id"]: z for z in zs}


def test_a_sized_part_holds_every_size_inside_what_was_asked():
    zs = _by(P.user_sized([WALL, FILLET, HOLE, PART9, PART7, OTHER], SIZES))
    # the part's own base: the middle of the range
    assert zs["P9"]["size_m"] == pytest.approx(0.0035) and zs["P7"]["size_m"] == pytest.approx(0.00175)
    # a wall the rules want at 0.6 mm stops at the 3 mm asked: under 2 through it, named, and the mesh check is held
    # to what 3 mm gives (so the ladder does not tighten it back)
    assert zs["W9.3"]["size_m"] == pytest.approx(0.003) and zs["W9.3"]["crossed"] == "PLAN-fea-3"
    assert zs["W9.3"]["wall_elements"] == pytest.approx(0.4, rel=1e-3)
    # a hole of radius 2.45 mm: 3 mm is 1.28 per quarter-turn, under the 3 valid elements need - held there, said
    assert Q * HOLE["radius_m"] / zs["H22"]["size_m"] == pytest.approx(P.VALID_PER_QUARTER, rel=1e-3)
    assert zs["H22"]["valid_held"] == "curve" and zs["H22"]["below_accuracy"]
    assert zs["H8"]["valid_held"] == "curve"
    # a part the user did not size is the rules' own
    assert zs["W4.1"]["size_m"] == pytest.approx(OTHER["size_m"]) and not zs["W4.1"].get("user")
    words = " ".join(P.user_words({"user_sizes": SIZES, "zones": list(zs.values())}))
    assert "You asked for 3-4 mm elements on Part_9" in words
    assert "fewer than 2 elements through them" in words and "cannot be built valid" in words
    assert "PLAN-fea" not in words


def test_the_count_moves_only_the_parts_the_user_did_not_size():
    zs = P.user_sized([WALL, OTHER, PART9], SIZES)
    scaled = _by(P.scaled(zs, 2.0, 3.0))
    assert scaled["W9.3"]["size_m"] == pytest.approx(0.003) and scaled["P9"]["size_m"] == pytest.approx(0.0035)
    assert scaled["W4.1"]["size_m"] > OTHER["size_m"] * 2


def test_past_the_accuracy_minimums_only_validity_stops_a_zone():
    zs = _by(P.scaled([WALL, HOLE, PART9], 2.0, 10.0))
    # the wall is at its accuracy minimum already (2 through), so only the second factor moves it
    assert zs["W9.3"]["size_m"] == pytest.approx(WALL["size_m"] * 10) and zs["W9.3"]["crossed"] == "PLAN-fea-3"
    assert Q * HOLE["radius_m"] / zs["H22"]["size_m"] == pytest.approx(P.VALID_PER_QUARTER, rel=1e-3)
    assert zs["P9"]["size_m"] == pytest.approx(0.533 / P.VALID_DIVISIONS, rel=1e-3)
    assert P.base_size(1.0, 2.0, 10.0) == pytest.approx(1.0 / P.VALID_DIVISIONS)
    assert P.base_size(1.0, 2.0, 1.0) == pytest.approx(0.2)


def test_a_plan_of_the_users_choice_is_never_tightened_by_the_ladder():
    facts = {"valid": True, "over": False, "short": ["W9.3"], "out_of_memory": False, "min_sicn": 0.3, "cells": 1,
             "limit": 0}
    assert L.lever_options({**facts, "built": True, "pass": 1, "rc": 0, "bad_surfaces": [], "bad_faces": []}, [],
                           {"user_sizes": SIZES}) == []
    assert L.by_choice({"target": {"scale": 2.0}}) and not L.by_choice({"target": {"scale": 1.0, "kept_rules": True}})


@pytest.mark.parametrize(("t", "cells", "est", "want"), [
    ({"asked": 500_000, "mode": "about"}, 700_000, 500_000, 1.4 ** FD.REAIM_DAMP),           # 40% over: re-aimed
    ({"asked": 500_000, "mode": "about"}, 530_000, 500_000, None),          # within 10%: delivered
    ({"asked": 500_000, "mode": "about", "lowest": True}, 900_000, 700_000, None),   # the lowest valid: nothing lower
    ({"asked": 500_000, "mode": "about", "kept_rules": True}, 900_000, 700_000, None),
    ({"asked": 500_000, "mode": "at_most"}, 520_000, 480_000, None),
    ({"asked": 500_000, "mode": "range", "low": 400_000, "high": 600_000}, 650_000, 500_000, 1.3 ** FD.REAIM_DAMP),
])
def test_a_built_count_off_the_ask_is_re_aimed_once(t, cells, est, want):
    k = FD.reaim_factor({"target": t, "estimate": {"elements": est}}, {"valid": True, "cells": cells})
    assert (k is None) if want is None else k == pytest.approx(want)
    assert FD.reaim_factor({"target": t, "estimate": {"elements": est}}, {"valid": False, "cells": cells}) is None


def test_the_count_line_says_asked_planned_and_built_honestly():
    t = {"asked": 500_000, "mode": "about"}
    assert P.count_line(t, 510_000, 520_000) == "you asked for about 500k elements; planned about 510k, built 520,000."
    assert "not reached: more than asked" in P.count_line(t, 510_000, 1_730_000)
    assert "Used the requested" not in P.count_line(t, 510_000, 1_730_000)
    assert "lowest this part can go" in P.count_line({**t, "lowest": True}, 900_000, 950_000)


def test_thin_walls_at_coarse_sizes_are_counted():
    """PLAN-fea-31: a 1 mm wall of 0.01 m2 at 6 mm elements - the volume alone counts 21 elements, gmsh builds about
    ten times that (lab: 523 on a 60 x 40 x 1 plate)."""
    hist = SF.Histogram(rows=[(math.inf, 1e-5)], surface=[(math.inf, 0.001, 0.02)])
    assert SF.elements(SF.Histogram(rows=[(math.inf, 1e-5)]), 0.006, 4.5) == pytest.approx(4.5 * 1e-5 / 0.006 ** 3)
    n = SF.elements(hist, 0.006, 4.5)
    assert n > 8 * 4.5 * 1e-5 / 0.006 ** 3
    # a thick part at a fine size: the surface adds its layer only
    thick = SF.Histogram(rows=[(math.inf, 1e-3)], surface=[(math.inf, 0.1, 0.06)])
    assert SF.elements(thick, 0.005, 4.5) == pytest.approx(4.5 * 1e-3 / 0.005 ** 3
                                                          + SF.SURFACE_PER_AREA * 0.06 / 0.005 ** 2)


def test_a_real_part_sized_by_the_user(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    rules = FD.choose_plan(record, surface, diag)
    sizes = {"Part_1": {"min_m": 0.006, "max_m": 0.008}}
    plan = FD.choose_plan(record, surface, diag, sizes=sizes)
    assert plan["base_size_m"] == pytest.approx(0.007)
    for z in plan["zones"]:
        assert z["user"] == "Part_1"
        assert z["size_m"] >= 0.006 * (1 - 1e-6) or z.get("valid_held")
    assert plan["estimate"]["elements"] != rules["estimate"]["elements"]
    assert any("You asked for 6-8 mm elements on Part_1" in w for w in plan["words"])
    assert any(d["rule"] == "PLAN-fea-29" for d in plan["decisions"])
    # with a count as well, the sizes come first and the words say so
    both = FD.choose_plan(record, surface, diag, sizes=sizes, target={"elements": 100, "mode": "about",
                                                                      "source": "ai"})
    assert both["target"]["sizes_first"] and both["base_size_m"] == pytest.approx(0.007)
    assert any("Your sizes per part come first" in w for w in both["words"])


@pytest.mark.parametrize("asked", [10, 5_000_000_000])
def test_absurd_counts_plan_the_closest_valid_mesh_and_say_so(tmp_path, asked):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.choose_plan(record, surface, diag, target={"elements": asked, "mode": "about", "source": "ai"})
    assert plan["fits"]
    t = plan["target"]
    if asked < 1000:
        assert t["lowest"] and any("The lowest this part can go is about" in w for w in plan["words"])
        assert "not reached" in P.target_remark(plan)
    else:
        assert t["machine"] and plan["estimate"]["elements"] <= P.LIMIT_AIM * plan["limit"]["elements"]


def test_the_count_search_runs_to_the_validity_limits_not_the_base():
    """A wall has no validity limit: past the accuracy minimums it grows until a third of the part, even when the
    base reached its own cap first (the 9-part assembly stopped at 543k this way while its sizes built 172k)."""
    zs = [dict(WALL), dict(HOLE)]
    u = FD.beyond_full(zs, base_m=0.533 / P.VALID_DIVISIONS, diag_m=0.533)
    assert u == pytest.approx((0.533 / P.VALID_DIVISIONS) / WALL["size_m"], rel=1e-5)
    assert FD.beyond_full([dict(HOLE)], base_m=0.1, diag_m=0.533) == pytest.approx(
        max((0.533 / 3) / 0.1, P.QUARTER * HOLE["radius_m"] / P.VALID_PER_QUARTER / HOLE["size_m"]), rel=1e-5)


def test_after_a_rebuild_the_build_closer_to_the_ask_is_delivered():
    passes = [{"pass": 1, "valid": True, "cells": 721_000, "off_count": True, "short": [], "over": False, "min_sicn": 0.2},
              {"pass": 2, "valid": True, "cells": 1_500_000, "short": [], "over": False, "min_sicn": 0.3}]
    FD.closer_of_reaimed(passes, 1_000_000)
    assert L.best_pass(passes)[0] == 1
    passes[1]["cells"] = 1_040_000
    FD.closer_of_reaimed(passes, 1_000_000)
    assert L.best_pass(passes)[0] == 2
