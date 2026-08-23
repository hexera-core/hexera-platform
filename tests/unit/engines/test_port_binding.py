# Responsibility: Pin the binder's every rule with table-driven fixtures, including the exact
# scenarios the adversarial review proved could bind wrong: the counterbore swap, the twin-feed
# swap, the surplus plug, the wrong-frame hint, and the unbound wall.
from __future__ import annotations

import math
import random
import re

import pytest

from meshpipeline.engines.port_binding import (
    BindError,
    DeclaredPatch,
    apply_binding,
    bind_ports,
)


def circle_area_m2(d_mm: float) -> float:
    return math.pi * (d_mm / 2.0) ** 2 * 1e-6


def make_t(openings: dict, bbox=((-0.2, -0.2, -0.05), (0.3, 0.2, 0.05))) -> dict:
    return {
        "stls": {"wall": "/w/wall.stl", **{k: f"/w/{k}.stl" for k in openings}},
        "interior_point": [0.1, 0.0, 0.0],
        "bbox_min": list(bbox[0]),
        "bbox_max": list(bbox[1]),
        "openings": {k: {"area": a, "centroid": list(c)} for k, (a, c) in openings.items()},
        "n_wall_faces": 1000,
    }


WALL = DeclaredPatch("pipe_wall", "wall")

# The live failure, job 7f838805: two 40 mm feeds combine into a 60 mm collector. The engine
# named the LARGEST opening "inlet" - the collector, which is physically the outlet.
WYE_T = make_t({
    "inlet":    (circle_area_m2(60), (0.25, 0.0, 0.0)),
    "outlet_1": (circle_area_m2(40), (-0.173, 0.1, 0.0)),
    "outlet_2": (circle_area_m2(40), (-0.173, -0.1, 0.0)),
})
WYE_DECLARED = [
    WALL,
    DeclaredPatch("inlet_1", "inlet", diameter_mm=40, interchangeable_with=("inlet_2",)),
    DeclaredPatch("inlet_2", "inlet", diameter_mm=40, interchangeable_with=("inlet_1",)),
    DeclaredPatch("outlet", "outlet", diameter_mm=60),
]


class TestTheWyeBindsCorrectly:
    def test_the_users_outlet_lands_on_the_engines_misnamed_inlet(self):
        b = bind_ports(WYE_DECLARED, WYE_T)
        # the inversion, fixed: the 60 mm collector the engine guessed to be the inlet is
        # bound to the user's declared OUTLET.
        assert b.port_map["outlet"] == "inlet"
        assert set(b.port_map) == {"inlet_1", "inlet_2", "outlet"}
        assert b.wall_name == "pipe_wall"
        assert b.folded_into_wall == ()

    def test_interchangeable_twins_bind_deterministically_in_centroid_order(self):
        b = bind_ports(WYE_DECLARED, WYE_T)
        # names lexicographic onto centroids (x, y, z)-sorted: inlet_1 -> the -y feed.
        assert b.port_map["inlet_1"] == "outlet_2"
        assert b.port_map["inlet_2"] == "outlet_1"

    def test_the_binding_is_identical_under_any_input_order(self):
        rng = random.Random(7)
        for _ in range(10):
            declared = list(WYE_DECLARED)
            rng.shuffle(declared)
            keys = list(WYE_T["openings"])
            rng.shuffle(keys)
            t = make_t({k: (WYE_T["openings"][k]["area"],
                            tuple(WYE_T["openings"][k]["centroid"])) for k in keys})
            assert bind_ports(declared, t).port_map == bind_ports(WYE_DECLARED, WYE_T).port_map

    def test_apply_binding_rekeys_everything_to_user_names(self):
        b = bind_ports(WYE_DECLARED, WYE_T)
        out = apply_binding(WYE_T, b)
        assert set(out["stls"]) == {"pipe_wall", "inlet_1", "inlet_2", "outlet"}
        assert out["stls"]["pipe_wall"] == "/w/wall.stl"
        assert out["stls"]["outlet"] == "/w/inlet.stl"
        assert set(out["openings"]) == {"inlet_1", "inlet_2", "outlet"}
        assert out["binding"]["wall_name"] == "pipe_wall"
        assert WYE_T["openings"] != out["openings"]  # original untouched


class TestUnbindableDeclarationsDiePreMesh:
    def test_twin_feeds_without_interchangeable_confirmation_are_refused(self):
        declared = [WALL,
                    DeclaredPatch("inlet_1", "inlet", diameter_mm=40),
                    DeclaredPatch("inlet_2", "inlet", diameter_mm=40),
                    DeclaredPatch("outlet", "outlet", diameter_mm=60)]
        with pytest.raises(BindError, match="cannot be told apart"):
            bind_ports(declared, WYE_T)

    def test_same_size_ports_of_different_roles_are_refused_even_if_marked_interchangeable(self):
        t = make_t({"inlet": (circle_area_m2(40), (0.0, 0.0, 0.0)),
                    "outlet": (circle_area_m2(40), (0.4, 0.0, 0.0))})
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40,
                                  interchangeable_with=("drain",)),
                    DeclaredPatch("drain", "outlet", diameter_mm=40,
                                  interchangeable_with=("feed",))]
        with pytest.raises(BindError, match="cannot be told apart"):
            bind_ports(declared, t)

    def test_the_counterbore_swap_fixture_is_refused_never_bound(self):
        # The review's verified F5 arithmetic: declared 40 mm (1257 mm2) and 50 mm (1963 mm2);
        # a counterbored 40 measures 1810 mm2 and a reduced-bore 50 measures 1452 mm2. Pure
        # area matching binds each port to exactly one opening with no ambiguity - and it is
        # the swap. The declared sizes are inside the inseparable band, so the binder must
        # refuse up front and ask for locations.
        t = make_t({"inlet": (1810e-6, (0.0, 0.0, 0.0)),
                    "outlet": (1452e-6, (0.4, 0.0, 0.0))})
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40),
                    DeclaredPatch("drain", "outlet", diameter_mm=50)]
        with pytest.raises(BindError, match="too close to tell apart"):
            bind_ports(declared, t)

    def test_an_opening_on_the_shared_tolerance_boundary_is_refused(self):
        # Rule 3b guarantees classes are separated by at least HI/LO, which makes an opening
        # inside BOTH tolerance bands reachable only exactly ON the shared boundary (1.25x the
        # small class = 0.75x the large one). The bounds are inclusive, so that point is
        # genuinely ambiguous - band-uniqueness is the backstop that refuses it.
        t = make_t({"inlet": (1250e-6, (0.0, 0.0, 0.0)),
                    "outlet": (1000 * 5 / 3 * 1e-6, (0.4, 0.0, 0.0))})
        declared = [WALL,
                    DeclaredPatch("a", "inlet", area_mm2=1000),
                    DeclaredPatch("b", "outlet", area_mm2=1000 * 5 / 3)]
        with pytest.raises(BindError, match="matches more than one declared size"):
            bind_ports(declared, t)

    def test_a_port_with_no_size_and_no_location_is_refused_by_name(self):
        declared = [WALL, DeclaredPatch("mystery", "inlet")]
        with pytest.raises(BindError, match="'mystery' has no size and no location"):
            bind_ports(declared, make_t({"inlet": (circle_area_m2(40), (0, 0, 0))}))

    def test_two_sizing_forms_on_one_port_are_refused(self):
        declared = [WALL, DeclaredPatch("p", "inlet", diameter_mm=40, area_mm2=1257)]
        with pytest.raises(BindError, match="more than one size form"):
            bind_ports(declared, make_t({"inlet": (circle_area_m2(40), (0, 0, 0))}))

    def test_a_declared_port_matching_no_opening_is_an_explicit_error(self):
        t = make_t({"inlet": (circle_area_m2(40), (0.0, 0.0, 0.0)),
                    "outlet": (circle_area_m2(60), (0.4, 0.0, 0.0))})
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40),
                    DeclaredPatch("drain", "outlet", diameter_mm=120)]
        with pytest.raises(BindError, match="no opening"):
            bind_ports(declared, t)

    def test_missing_wall_and_double_wall_are_refused(self):
        with pytest.raises(BindError, match="exactly one wall"):
            bind_ports([DeclaredPatch("p", "inlet", diameter_mm=40)],
                       make_t({"inlet": (circle_area_m2(40), (0, 0, 0))}))
        with pytest.raises(BindError, match="exactly one wall"):
            bind_ports([WALL, DeclaredPatch("wall2", "wall"),
                        DeclaredPatch("p", "inlet", diameter_mm=40)],
                       make_t({"inlet": (circle_area_m2(40), (0, 0, 0))}))


class TestCountMismatch:
    def test_fewer_openings_than_ports_lists_the_frame_and_the_remedy(self):
        declared = [WALL,
                    DeclaredPatch("in", "inlet", diameter_mm=40),
                    DeclaredPatch("out_1", "outlet", diameter_mm=60),
                    DeclaredPatch("out_2", "outlet", diameter_mm=90)]
        t = make_t({"inlet": (circle_area_m2(60), (0.25, 0.0, 0.0)),
                    "outlet": (circle_area_m2(40), (-0.173, 0.1, 0.0))})
        with pytest.raises(BindError) as e:
            bind_ports(declared, t)
        msg = str(e.value)
        assert "declared 3 ports but only 2 openings" in msg
        assert "opening_faces" in msg          # the remedy is named
        assert "bbox_min" in msg               # the coordinate frame is published
        assert "centroid=(0.2500, 0.0000, 0.0000)" in msg

    def test_a_surplus_plug_matching_no_declared_size_folds_into_the_wall(self):
        t = make_t({"inlet": (circle_area_m2(60), (0.25, 0.0, 0.0)),
                    "outlet_1": (circle_area_m2(40), (-0.173, 0.1, 0.0)),
                    "outlet_2": (circle_area_m2(10), (0.0, 0.0, -0.05))})  # a blind plug
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40),
                    DeclaredPatch("drain", "outlet", diameter_mm=60)]
        b = bind_ports(declared, t)
        assert b.folded_into_wall == ("outlet_2",)
        out = apply_binding(t, b)
        assert out["folded_stls"] == {"outlet_2": "/w/outlet_2.stl"}
        assert "outlet_2" not in out["openings"]

    def test_a_surplus_opening_matching_a_declared_size_is_refused_not_folded(self):
        # folding a port-sized opening would SEAL a probable port - the y-junction one-branch-
        # capped failure all over again. Refuse instead.
        t = make_t({"inlet": (circle_area_m2(60), (0.25, 0.0, 0.0)),
                    "outlet_1": (circle_area_m2(40), (-0.173, 0.1, 0.0)),
                    "outlet_2": (circle_area_m2(40), (-0.173, -0.1, 0.0))})
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40),
                    DeclaredPatch("drain", "outlet", diameter_mm=60)]
        with pytest.raises(BindError, match="surplus opening cannot be folded"):
            bind_ports(declared, t)


class TestLocationHints:
    DUCT_T = make_t({"inlet": (4000e-6, (0.0, 0.04, 0.025)),
                     "outlet": (4000e-6, (0.4, 0.04, 0.025))},
                    bbox=((0.0, 0.0, 0.0), (0.4, 0.08, 0.05)))

    def test_hints_resolve_samesize_ports_the_area_test_cannot(self):
        declared = [WALL,
                    DeclaredPatch("in", "inlet", area_mm2=4000, near_mm=(0, 40, 25)),
                    DeclaredPatch("out", "outlet", area_mm2=4000, near_mm=(400, 40, 25))]
        b = bind_ports(declared, self.DUCT_T)
        assert b.port_map == {"in": "inlet", "out": "outlet"}

    def test_a_hint_between_two_nearby_openings_is_refused_for_lack_of_margin(self):
        # two branch outlets 60 mm apart; the hint sits 25 mm from one and 35 mm from the
        # other - close enough to be plausible, not decisive enough to bind (d1 > 0.5 * d2).
        t = make_t({"inlet": (circle_area_m2(60), (0.0, 0.0, 0.0)),
                    "outlet_1": (circle_area_m2(40), (0.4, 0.03, 0.0)),
                    "outlet_2": (circle_area_m2(40), (0.4, -0.03, 0.0))},
                   bbox=((0.0, -0.06, -0.03), (0.4, 0.06, 0.03)))
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=60),
                    DeclaredPatch("out_a", "outlet", diameter_mm=40, near_mm=(400, 5, 0)),
                    DeclaredPatch("out_b", "outlet", diameter_mm=40, near_mm=(400, -30, 0))]
        with pytest.raises(BindError, match="does not decisively pick one opening"):
            bind_ports(declared, t)

    def test_a_hint_far_outside_the_part_is_refused(self):
        declared = [WALL,
                    DeclaredPatch("in", "inlet", area_mm2=4000, near_mm=(5000, 40, 25)),
                    DeclaredPatch("out", "outlet", area_mm2=4000, near_mm=(400, 40, 25))]
        with pytest.raises(BindError, match="does not plausibly refer to any opening"):
            bind_ports(declared, self.DUCT_T)

    def test_a_hint_contradicting_the_declared_size_is_refused(self):
        # the wrong-frame guard: a hint landing on SOME opening must still agree with the
        # port's declared size, or the location evidence silently overrides the size evidence.
        t = make_t({"inlet": (circle_area_m2(60), (0.25, 0.0, 0.0)),
                    "outlet": (circle_area_m2(40), (-0.173, 0.1, 0.0))})
        declared = [WALL,
                    DeclaredPatch("feed", "inlet", diameter_mm=40, near_mm=(250, 0, 0)),
                    DeclaredPatch("drain", "outlet", diameter_mm=60)]
        with pytest.raises(BindError, match="the location and the size disagree"):
            bind_ports(declared, t)


class TestToleranceBoundaries:
    @pytest.mark.parametrize("measured_mm2, binds", [
        (750.0, True),     # exactly 0.75 - inclusive
        (1250.0, True),    # exactly 1.25 - inclusive
        (740.0, False),
        (1260.0, False),
    ])
    def test_the_ratio_bound_is_inclusive_at_its_edges(self, measured_mm2, binds):
        t = make_t({"inlet": (measured_mm2 * 1e-6, (0.0, 0.0, 0.0))})
        declared = [WALL, DeclaredPatch("p", "inlet", area_mm2=1000.0)]
        if binds:
            assert bind_ports(declared, t).port_map == {"p": "inlet"}
        else:
            with pytest.raises(BindError):
                bind_ports(declared, t)


class TestNameSafety:
    def test_schema_legal_names_are_fixed_points_of_the_snappy_sanitizer(self):
        # snappy_runner rewrites '[^A-Za-z0-9_]' to '_' at STL-write time. Any name the intake
        # schema admits must survive that sanitizer UNCHANGED, or the gates (which compare the
        # raw string) diverge from the mesh. This pins the two regexes to each other.
        schema = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
        sanitize = lambda s: re.sub(r"[^A-Za-z0-9_]", "_", s)  # noqa: E731
        for name in ["inlet_1", "pipe_wall", "Outlet", "coolant_feed_2", "a", "Z9_"]:
            assert schema.match(name), name
            assert sanitize(name) == name, name
        for bad in ["inlet-1", "inlet 1", "2in_feed", "café", ""]:
            assert not schema.match(bad), bad

    def test_from_intake_builds_the_declared_patch(self):
        p = DeclaredPatch.from_intake({
            "name": "inlet_1", "type": "inlet", "diameter_mm": 40,
            "near_mm": [0, 200, 0], "interchangeable_with": ["inlet_2"]})
        assert p.role == "inlet"
        assert p.near_mm == (0, 200, 0)
        assert p.interchangeable_with == ("inlet_2",)
        assert p.declared_area_m2() == pytest.approx(circle_area_m2(40))

    def test_rectangular_ports_declare_width_and_height(self):
        p = DeclaredPatch("duct_in", "inlet", width_mm=80, height_mm=50)
        assert p.declared_area_m2() == pytest.approx(4000e-6)
