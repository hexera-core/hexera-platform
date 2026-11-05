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


# The thin-ring failure, job 11b50253 (duct_circ_bend_red): a flanged duct, wall 2 mm,
# bore 796 mm. Every end face is an annular RING - the duct wall's own end ring measures
# 5014 mm² of metal around the 497,644 mm² bore the declaration states, so pure face-area
# matching finds nothing. The opening is what the ring's INNER WIRE encloses; the flange
# annuli around the same mouth enclose nearly the same hole and must still lose to the
# ring that hugs it. Candidate values below are the real measured ones.
def ring(area_mm2, wh_mm, c=(0.0, 0.0, 0.0)):
    return {"area_m2": area_mm2 * 1e-6, "centroid": c,
            "wh_m": tuple(v * 1e-3 for v in wh_mm)}


BORE_796_MM2 = 497644.0            # declared: pi * (796/2)^2
FLANGED_DUCT_FACES = [
    # the duct wall's end rings: 5014 mm² of metal, inner wire the 796 bore
    (1, 5014e-6, (0.0, 0.0, 0.0), ring(497059, (795.7, 795.8))),
    (3, 5014e-6, (0.6, -0.6, 0.0), ring(497059, (795.7, 795.8), (0.6, -0.6, 0.0))),
    # flange annuli at the same stations: inner wire the 800 mm flange bore
    (6, 64795e-6, (0.0, 0.0, 0.0), ring(502067, (799.5, 799.6))),
    (7, 64795e-6, (0.002, 0.0, 0.0), ring(502067, (799.5, 799.6), (0.002, 0.0, 0.0))),
    (13, 65864e-6, (0.6, -0.6, 0.0), ring(502067, (799.5, 799.6), (0.6, -0.6, 0.0))),
    (15, 65864e-6, (0.0, 0.0, 0.0), ring(502067, (799.5, 799.6))),
]


class TestTheThinRingBore:
    DECLARED = [
        {"name": "inlet", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796,
         "near_m": (0.0, 0.0, 0.0)},
        {"name": "outlet", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796,
         "near_m": (0.6, -0.6, 0.0)},
    ]

    def test_a_declared_bore_matches_the_ring_by_its_inner_wire(self):
        chosen = select_declared_openings(FLANGED_DUCT_FACES, self.DECLARED)
        assert sorted(chosen) == [1, 3]

    def test_the_flange_annulus_is_not_preferred_over_the_true_end_ring(self):
        # every x=0 face is concentric AND coplanar - location cannot separate them.
        # The ring whose inner wire agrees best with the declaration wins; the flange
        # annuli around the same mouth (800 mm bore vs the declared 796) lose.
        chosen = select_declared_openings(
            [f for f in FLANGED_DUCT_FACES if f[0] in (1, 6, 15)],
            [{"name": "inlet", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796,
              "near_m": (0.0, 0.0, 0.0)}])
        assert chosen == [1]

    def test_noise_equal_agreement_prefers_the_ring_hugging_the_opening(self):
        # a flange bored EXACTLY to the duct bore: both inner wires measure the declared
        # size within noise. The thin end ring (the face that rims the opening tightest)
        # must win, not the broad annulus - deliberately, not by luck of iteration order.
        faces = [
            (9, 64795e-6, (0.0, 0.0, 0.0), ring(497500, (795.9, 796.0))),  # flange first
            (12, 5014e-6, (0.0, 0.0, 0.0), ring(497059, (795.7, 795.8))),  # the end ring
        ]
        chosen = select_declared_openings(
            faces, [{"name": "inlet", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796,
                     "near_m": (0.0, 0.0, 0.0)}])
        assert chosen == [12]

    def test_sizes_alone_still_pick_the_end_rings_without_hints(self):
        declared = [{"name": "a", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796},
                    {"name": "b", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796}]
        chosen = select_declared_openings(FLANGED_DUCT_FACES, declared)
        assert sorted(chosen) == [1, 3]

    def test_a_solid_disc_port_keeps_its_face_area_behaviour(self):
        # a fluid-domain (solid model) end cap has no inner wire: its own area is the
        # opening, exactly as before ring awareness existed
        faces = [(0, 0.0040, (0.0, 0.04, 0.025)),
                 (1, 5014e-6, (0.5, 0.0, 0.0), ring(497059, (795.7, 795.8)))]
        chosen = select_declared_openings(
            faces, [{"name": "inlet", "area_m2": 0.0040, "near_m": None}])
        assert chosen == [0]

    def test_a_truly_unmatchable_declaration_still_refuses_actionably(self):
        declared = [{"name": "feed", "area_m2": 1.0, "near_m": None}]   # a 1 m² port
        with pytest.raises(ValueError) as e:
            select_declared_openings(FLANGED_DUCT_FACES, declared)
        msg = str(e.value)
        assert "feed" in msg and "matches none" in msg
        # the listing now says what each ring's inner wire encloses, so the user can see
        # why neither the metal area nor the bore fit the declaration
        assert "ring face; inner opening" in msg

    def test_a_circle_declaration_does_not_bind_a_rectangular_opening(self):
        # same enclosed area, wrong shape: a 796 mm circle is not a 705 x 705 mm square
        faces = [(2, 5014e-6, (0.0, 0.0, 0.0), ring(497000, (705.0, 705.0)))]
        declared = [{"name": "inlet", "area_m2": BORE_796_MM2 * 1e-6, "d_m": 0.796,
                     "near_m": None}]
        with pytest.raises(ValueError, match="matches none"):
            select_declared_openings(faces, declared)


class TestTheRectangularRing:
    # duct_radius_elbow: declared RECTANGULAR 600 x 400 mm; the end faces are rings of
    # 52,500 mm² whose inner wires enclose exactly 400 x 600 - and the duct wall's own
    # 2 mm end ring (4024 mm², inner 396 x 596) sits 2 mm off-centroid at the same end
    ELBOW_FACES = [
        (3, 52500e-6, (0.0, 0.0, 0.0), ring(240000, (400.0, 600.0))),
        (4, 52500e-6, (0.002, 0.0, 0.0), ring(240000, (400.0, 600.0), (0.002, 0.0, 0.0))),
        (10, 4024e-6, (0.0, -0.0021, 0.0), ring(235966, (396.0, 596.0))),
        (24, 37601e-6, (0.55, -0.55, 0.0), ring(97397, (246.0, 396.0), (0.55, -0.55, 0.0))),
        (19, 34720e-6, (0.55, -0.548, 0.0), ring(100295, (250.5, 400.5), (0.55, -0.548, 0.0))),
    ]

    def test_declared_w_x_h_binds_the_ring_whose_inner_wire_encloses_it(self):
        declared = [
            {"name": "inlet", "area_m2": 0.24, "wh_m": (0.6, 0.4), "near_m": (0.0, 0.0, 0.0)},
            {"name": "outlet", "area_m2": 0.10, "wh_m": (0.4, 0.25),
             "near_m": (0.55, -0.55, 0.0)},
        ]
        chosen = select_declared_openings(self.ELBOW_FACES, declared)
        assert chosen == [3, 24]

    def test_w_x_h_agreement_is_order_free(self):
        declared = [{"name": "inlet", "area_m2": 0.24, "wh_m": (0.4, 0.6),
                     "near_m": (0.0, 0.0, 0.0)}]
        assert select_declared_openings(self.ELBOW_FACES, declared) == [3]

    def test_a_rect_declaration_does_not_bind_a_circular_opening_of_equal_area(self):
        faces = [(5, 5000e-6, (0.0, 0.0, 0.0), ring(240000, (552.8, 552.8)))]
        declared = [{"name": "inlet", "area_m2": 0.24, "wh_m": (0.6, 0.4), "near_m": None}]
        with pytest.raises(ValueError, match="matches none"):
            select_declared_openings(faces, declared)
