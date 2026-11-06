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
