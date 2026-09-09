# Responsibility: Verify an annular port can be declared as bore + centre-body diameters, and that a
# ring face is measured by the disc its outer wire encloses as well as by its own area and its
# inner opening.
# The live failure, job a1593cdc (blade_row_passage_001 through snappy as a fluid domain): the
# intake could only say 'diameter_mm' or 'area_mm2', invented 31,403 mm2 for a 104,603 mm2
# annulus, and the binder refused a 3x size disagreement. A fluid-solid's port face IS the
# annulus - the "pipe bore diameter" the user quotes is its OUTER wire.
from __future__ import annotations

import math

import pytest

from meshpipeline.engines.port_binding import BindError, DeclaredPatch, bind_ports

R_SHROUD, R_HUB = 226.65, 134.44                 # mm, blade_row_passage_001
ANNULUS_MM2 = math.pi * (R_SHROUD ** 2 - R_HUB ** 2)   # 104,603
HUB_MM2 = math.pi * R_HUB ** 2                          # 56,776


def _passage_t() -> dict:
    def ring(c):
        return {"area": ANNULUS_MM2 * 1e-6, "centroid": list(c),
                "opening": {"area": HUB_MM2 * 1e-6, "centroid": list(c), "wh": [0.2689, 0.2689]}}
    return {
        "stls": {"wall": "/w/wall.stl", "inlet": "/w/inlet.stl", "outlet": "/w/outlet.stl"},
        "interior_point": [-0.076, 0.18, 0.0],
        "bbox_min": [-0.119, -0.227, -0.227], "bbox_max": [0.113, 0.227, 0.227],
        "openings": {"inlet": ring((-0.119, 0.0, 0.0)), "outlet": ring((0.113, 0.0, 0.0))},
        "n_wall_faces": 1000,
    }


WALL = DeclaredPatch("wall", "wall")


def test_the_declared_annulus_is_the_ring_between_the_two_diameters():
    p = DeclaredPatch("inlet", "inlet", diameter_mm=2 * R_SHROUD, inner_diameter_mm=2 * R_HUB)
    assert p.declared_area_m2() == pytest.approx(ANNULUS_MM2 * 1e-6, rel=1e-9)
    assert DeclaredPatch.from_intake({"name": "inlet", "type": "inlet", "diameter_mm": 453.3,
                                      "inner_diameter_mm": 268.88}).declared_area_m2() == \
        pytest.approx(ANNULUS_MM2 * 1e-6, rel=1e-3)


def test_an_inner_diameter_that_is_not_inside_the_bore_is_ignored():
    disc = math.pi * (R_SHROUD * 1e-3) ** 2
    for bad in (0.0, -5.0, 2 * R_SHROUD, 3 * R_SHROUD):
        p = DeclaredPatch("inlet", "inlet", diameter_mm=2 * R_SHROUD, inner_diameter_mm=bad)
        assert p.declared_area_m2() == pytest.approx(disc, rel=1e-9)


def test_bore_plus_centre_body_binds_a_fluid_solid_ring_face():
    declared = [WALL,
                DeclaredPatch("inlet", "inlet", diameter_mm=453.3, inner_diameter_mm=268.88,
                              near_mm=(-118.96, 0, 0)),
                DeclaredPatch("outlet", "outlet", diameter_mm=453.3, inner_diameter_mm=268.88,
                              near_mm=(113.46, 0, 0))]
    b = bind_ports(declared, _passage_t())
    assert b.port_map == {"inlet": "inlet", "outlet": "outlet"}


def test_the_bore_alone_binds_through_the_ring_faces_outer_disc():
    # the user quoted only "the pipe bore diameter 453.3 mm": that disc is the annulus plus the
    # hub it surrounds - the outer wire - so the size agrees
    declared = [WALL,
                DeclaredPatch("inlet", "inlet", diameter_mm=453.3, near_mm=(-118.96, 0, 0)),
                DeclaredPatch("outlet", "outlet", diameter_mm=453.3, near_mm=(113.46, 0, 0))]
    b = bind_ports(declared, _passage_t())
    assert b.port_map == {"inlet": "inlet", "outlet": "outlet"}


def test_an_invented_size_is_still_refused():
    # the model's 200 mm circle from job a1593cdc matches none of the ring's three measures
    declared = [WALL,
                DeclaredPatch("inlet", "inlet", area_mm2=31403.5, near_mm=(-118.96, 0, 0)),
                DeclaredPatch("outlet", "outlet", area_mm2=31403.5, near_mm=(113.46, 0, 0))]
    with pytest.raises(BindError, match="the location and the size disagree"):
        bind_ports(declared, _passage_t())


def test_a_wall_shells_thin_ring_still_binds_by_its_bore():
    # the existing case (job 11b50253): 5,014 mm2 of metal around a 497,059 mm2 bore, declared
    # by bore diameter - the inner opening measure keeps carrying it
    def rec(c):
        return {"area": 5014.0e-6, "centroid": list(c),
                "opening": {"area": 497059.0e-6, "centroid": list(c), "wh": [0.7957, 0.7958]}}
    t = {"stls": {"wall": "/w/wall.stl", "inlet": "/w/inlet.stl", "outlet": "/w/outlet.stl"},
         "interior_point": [0.3, -0.1, 0.0], "bbox_min": [0.0, -0.6, -0.43],
         "bbox_max": [1.11, 0.51, 0.43],
         "openings": {"inlet": rec((0.0, 0.0, 0.0)), "outlet": rec((0.6, -0.6, 0.0))},
         "n_wall_faces": 1000}
    declared = [DeclaredPatch("duct_wall", "wall"),
                DeclaredPatch("inlet", "inlet", diameter_mm=796, near_mm=(0, 0, 0)),
                DeclaredPatch("outlet", "outlet", diameter_mm=796, near_mm=(600, -600, 0))]
    assert bind_ports(declared, t).port_map == {"inlet": "inlet", "outlet": "outlet"}


# ---- the bore filed as outer_diameter_mm, the gap as diameter_mm (job eea4fe22) --------------


def test_a_bore_named_outer_diameter_with_the_gap_in_diameter_is_the_ring():
    # blade_row_passage_006: "between the centre-body outside diameter 187.14 mm and the pipe bore
    # diameter 311.92 mm - a 62.39 mm radial gap"; the intake filed it as below
    p = DeclaredPatch.from_intake({"name": "inlet", "type": "inlet", "near_mm": [-139.94, 0, 0],
                                   "diameter_mm": 62.39, "inner_diameter_mm": 187.14,
                                   "outer_diameter_mm": 311.92})
    assert p.diameter_mm == pytest.approx(311.92)
    assert p.declared_area_m2() == pytest.approx(
        math.pi * ((311.92 / 2) ** 2 - (187.14 / 2) ** 2) * 1e-6, rel=1e-9)   # 48,905 mm2


def test_outer_diameter_alone_is_the_bore():
    p = DeclaredPatch.from_intake({"name": "inlet", "type": "inlet", "outer_diameter_mm": 453.3})
    assert p.diameter_mm == pytest.approx(453.3)


def test_a_smaller_outer_diameter_does_not_override_a_real_bore():
    p = DeclaredPatch.from_intake({"name": "inlet", "type": "inlet", "diameter_mm": 453.3,
                                   "outer_diameter_mm": 100.0})
    assert p.diameter_mm == pytest.approx(453.3)


def test_a_diameter_smaller_than_the_centre_body_is_the_radial_gap():
    # blade_row_passage_002 (job ac839f83): hub 208.6 mm, shroud 308.42 mm, "a 49.91 mm radial
    # gap" - the intake filed the gap as diameter_mm and named no bore at all
    p = DeclaredPatch.from_intake({"name": "inlet", "type": "inlet", "diameter_mm": 49.91,
                                   "inner_diameter_mm": 208.6})
    assert p.diameter_mm == pytest.approx(308.42)
    assert p.declared_area_m2() == pytest.approx(
        math.pi * ((308.42 / 2) ** 2 - (208.6 / 2) ** 2) * 1e-6, rel=1e-6)   # 40,530 mm2
