# Responsibility: Verify the internal-flow sizing never reads a passage wider than the narrowest declared port.
# On 72 corpus walls the chord reading came out 8-173% wider than the narrowest port in 20 cases
# (a hollow part's outer skin; the long side of a 140 x 384 mm rectangular duct), and cfMesh sized
# its wall band for that: 6.9-11.4 cells across where the floor is 12 (2026-10-04).
from __future__ import annotations

import pytest

import meshpipeline.engines.passage as P


def test_the_flow_width_of_each_port_shape():
    assert P.declared_port_half_width([{"name": "in", "type": "inlet", "diameter_mm": 100}]) \
        == pytest.approx(0.05)
    # a rectangle is crossed along its SHORT side
    assert P.declared_port_half_width([{"name": "in", "type": "inlet", "width_mm": 384,
                                        "height_mm": 140}]) == pytest.approx(0.07)
    # an annulus is crossed along its radial gap
    assert P.declared_port_half_width([{"name": "in", "type": "inlet", "diameter_mm": 300,
                                        "inner_diameter_mm": 200}]) == pytest.approx(0.025)
    # an area alone: the equivalent circle
    assert P.declared_port_half_width([{"name": "in", "type": "inlet", "area_mm2": 3.14159265 * 2500}]) \
        == pytest.approx(0.05, rel=1e-6)
    # the narrowest port wins; walls and unsized ports say nothing
    assert P.declared_port_half_width([
        {"name": "a", "type": "inlet", "diameter_mm": 200},
        {"name": "b", "type": "outlet", "diameter_mm": 80},
        {"name": "w", "type": "wall"},
        {"name": "c", "type": "outlet"}]) == pytest.approx(0.04)
    assert P.declared_port_half_width([{"name": "w", "type": "wall"}]) is None
    assert P.declared_port_half_width([]) is None


def test_a_wide_reading_is_capped_at_the_port_and_a_throat_stands():
    wide = {"min": 0.19, "p05": 0.19, "median": 0.19, "max": 0.19, "points": 200, "source": "chord"}
    capped = P.cap_at_ports(wide, 0.07)
    assert capped["p05"] == capped["min"] == 0.07 and capped["capped_at_declared_port"]
    assert capped["median"] == 0.19, "the background is still sized from the typical passage"
    throat = {"min": 0.02, "p05": 0.03, "median": 0.06, "max": 0.1, "points": 500}
    assert P.cap_at_ports(throat, 0.07) == throat, "a narrower passage inside is kept"
    assert P.cap_at_ports(wide, None) == wide
    assert P.cap_at_ports(None, 0.07)["p05"] == 0.07
    assert P.cap_at_ports(None, None) is None
    # never a median below the narrow end
    assert P.cap_at_ports({"p05": 0.2, "median": 0.1, "min": 0.1}, 0.15)["median"] == 0.15


def test_the_wall_band_is_sized_from_the_capped_radius():
    caps = P.size_caps(P.cap_at_ports({"p05": 0.19, "median": 0.19, "min": 0.19}, 0.07))
    # 13 cells across the 140 mm side, not across the 384 mm one
    assert caps["wall_cell"] == pytest.approx(2 * 0.07 / P.PASSAGE_CELLS_ACROSS)
