"""The shell sizing rules (engines/shell_plan.py): plain numbers in, sizes and the rule that set them out."""
from __future__ import annotations

import pytest

from meshpipeline.engines import shell_plan as P


def test_a_panel_gets_twenty_elements_across_for_its_modes():
    # a 152 mm wide wall of a 3.2 mm box tube (sheet size 844 mm): 4 across left its first modes 21% stiff
    h, rule = P.base_size(wall=3.175, size=844.0, width=152.4)
    assert rule == "SHMESH-15" and h == pytest.approx(152.4 / P.PANEL_ACROSS)


def test_a_narrow_face_keeps_four_across():
    # a 40 mm flange of a 3 mm sheet is under 20 walls wide: it does not bend across in the first modes
    h, rule = P.base_size(wall=3.0, size=300.0, width=40.0)
    assert rule == "SHMESH-1" and h == pytest.approx(40.0 / P.MIN_ACROSS)


def test_the_sheet_size_still_binds_when_it_is_finer():
    h, rule = P.base_size(wall=1.0, size=100.0, width=1000.0)
    assert rule == "SHMESH-1" and h == pytest.approx(100.0 / P.BASE_DIVISIONS)


def test_the_floor_holds_under_every_rule():
    h, rule = P.base_size(wall=10.0, size=1000.0, width=10.0)
    assert rule == "SHMESH-2" and h == pytest.approx(P.FLOOR_WALLS * 10.0)


def test_a_wall_that_varies_is_sized_by_its_thinnest_point():
    # SHELL-9b: a wedge 2 to 5.5 mm, 100 mm wide: 50 of its thinnest walls wide, a panel (20 across); read at its
    # thickest wall it was 18 walls wide and got 4 across
    rec = {"mids": [{"id": "Mid_1", "pair": "P1", "body": "Part_1", "thickness_range_mm": [2.0, 5.5]}],
           "pairs": [{"id": "P1", "thickness_mm": 5.5, "size_mm": 300.0}]}
    from meshpipeline.engines import shell_plan as plan

    row = plan.face_plan(rec, "Mid_1", width=100.0, radii=[])
    assert row["wall_mm"] == 2.0 and row["size_mm"] == 5.0 and row["rule"] == "SHMESH-15"


# SHMESH-23: the user's element count, as the solid path reads it (fea_driver.element_target)

def test_an_about_count_scales_every_size_by_one_factor():
    aim = P.count_aim(20_000, {"elements": 50_000, "mode": "about"})
    assert aim == 50_000
    # the count goes as the inverse square of the size on a surface: 2.5 times the count, sizes / sqrt(2.5)
    assert P.count_scale(20_000, aim) == pytest.approx((20_000 / 50_000) ** 0.5)
    assert P.count_scale(20_000, None) == 1.0


def test_a_count_the_rules_already_meet_leaves_the_plan_alone():
    assert P.count_aim(60_000, {"elements": 100_000, "mode": "at_most"}) is None
    assert P.count_aim(60_000, {"elements": 50_000, "mode": "at_least"}) is None
    assert P.count_aim(60_000, {"elements": 70_000, "mode": "range", "low": 50_000, "high": 90_000}) is None
    assert P.count_aim(60_000, None) is None and P.count_aim(60_000, {"unread": True}) is None
    # outside a bound: aimed a little inside it
    assert P.count_aim(200_000, {"elements": 100_000, "mode": "at_most"}) == pytest.approx(90_000)
    assert P.count_aim(20_000, {"elements": 50_000, "mode": "at_least"}) == pytest.approx(55_000)


def test_the_user_count_beats_the_floor_but_not_a_misread_by_a_thousand():
    # finer than half the wall is allowed (said, never refused); a factor past SCALE_RANGE is held there
    lo, hi = P.SCALE_RANGE
    assert P.count_scale(1_000, 1e9) == lo and P.count_scale(1e9, 1) == hi


def test_a_built_count_is_met_by_mode():
    about = {"elements": 50_000, "mode": "about"}
    assert P.count_met(45_000, about) and P.count_met(60_000, about) and not P.count_met(30_000, about)
    assert P.count_met(80_000, {"elements": 100_000, "mode": "at_most"})
    assert not P.count_met(40_000, {"elements": 50_000, "mode": "at_least"})
    assert P.count_met(1, None)


def test_the_count_is_said_in_plain_words_with_its_warnings():
    assert P.count_words(None) == ""
    said = P.count_words({"asked": 50_000, "built": 48_200, "scale": 0.6, "under_floor": True})
    assert said.startswith("Sized to the 50,000 elements you asked for (48,200 made).") and "half the wall" in said
    coarse = P.count_words({"asked": 2_000, "built": 2_100, "scale": 2.0, "rules_elements": 9_000})
    assert "coarser than our accuracy rules ask for (about 9,000 elements)" in coarse
    assert P.count_words({"asked": 50_000, "kept_rules": True}).startswith("Your count (50,000 elements) was already met")
