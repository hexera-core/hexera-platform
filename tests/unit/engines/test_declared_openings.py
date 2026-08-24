# Responsibility: Pin declaration-guided opening selection: when the planar-face rule finds
# MORE flat faces than the user declared ports (a box duct finds six), the declaration's sizes
# and locations pick the real openings and everything else becomes wall - deterministically,
# with an actionable refusal when nothing matches.
from __future__ import annotations

import pytest

from meshpipeline.cad.cad_tessellate import select_declared_openings

# a box duct 400 x 80 x 50 mm in metres: six planar faces - two 80x50 ends (the real ports)
# and four long sides the naive rule also calls "openings"
BOX_FACES = [
    (0, 0.0040, (0.0, 0.04, 0.025)),    # x=0 end        80x50  <- inlet
    (1, 0.0040, (0.4, 0.04, 0.025)),    # x=400 end      80x50  <- outlet
    (2, 0.0320, (0.2, 0.04, 0.0)),      # bottom         400x80
    (3, 0.0320, (0.2, 0.04, 0.05)),     # top            400x80
    (4, 0.0200, (0.2, 0.0, 0.025)),     # side           400x50
    (5, 0.0200, (0.2, 0.08, 0.025)),    # side           400x50
]

DECLARED = [
    {"name": "inlet", "area_m2": 0.0040, "near_m": (0.0, 0.04, 0.025)},
    {"name": "outlet", "area_m2": 0.0040, "near_m": (0.4, 0.04, 0.025)},
]


class TestTheBoxDuctCase:
    def test_declared_ends_are_chosen_and_sides_become_wall(self):
        chosen = select_declared_openings(BOX_FACES, DECLARED)
        assert sorted(chosen) == [0, 1]

    def test_sizes_alone_suffice_when_they_are_unambiguous(self):
        declared = [{"name": "inlet", "area_m2": 0.0040, "near_m": None},
                    {"name": "outlet", "area_m2": 0.0040, "near_m": None}]
        chosen = select_declared_openings(BOX_FACES, declared)
        assert sorted(chosen) == [0, 1]    # only two faces are anywhere near 0.0040

    def test_hints_break_ties_between_samesize_faces(self):
        declared = [{"name": "a", "area_m2": 0.0200, "near_m": (0.2, 0.0, 0.025)}]
        chosen = select_declared_openings(BOX_FACES, declared)
        assert chosen == [4]

    def test_a_port_matching_nothing_refuses_with_the_face_list(self):
        declared = [{"name": "feed", "area_m2": 0.5, "near_m": None}]
        with pytest.raises(ValueError) as e:
            select_declared_openings(BOX_FACES, declared)
        msg = str(e.value)
        assert "feed" in msg and "80" in msg    # names the port, lists faces in mm2

    def test_two_ports_cannot_claim_one_face(self):
        declared = [{"name": "a", "area_m2": 0.0040, "near_m": (0.0, 0.04, 0.025)},
                    {"name": "b", "area_m2": 0.0040, "near_m": (0.01, 0.04, 0.025)}]
        chosen = select_declared_openings(BOX_FACES, declared)
        assert sorted(chosen) == [0, 1]    # b falls through to the other 0.0040 face

    def test_selection_is_deterministic_regardless_of_input_order(self):
        a = select_declared_openings(BOX_FACES, DECLARED)
        b = select_declared_openings(list(reversed(BOX_FACES)), list(reversed(DECLARED)))
        assert sorted(a) == sorted(b)
