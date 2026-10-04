# Responsibility: Verify the internal-flow sizing reads each declared port's flow width and never sizes the wall band for a passage wider than the ports.
# On 72 corpus walls the chord reading came out 8-173% wider than the narrowest port in 20 cases
# (a hollow part's outer skin; the long side of a 140 x 384 mm rectangular duct), and cfMesh sized
# its wall band for that: 6.9-11.4 cells across where the floor is 12 (2026-10-04).
from __future__ import annotations

import numpy as np
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


def test_the_wall_band_is_sized_from_the_band_radius_and_the_floor_from_the_narrowest():
    caps = P.size_caps({"p05": 0.01, "median": 0.07, "band": 0.07})
    assert caps["wall_cell"] == pytest.approx(2 * 0.07 / P.PASSAGE_CELLS_ACROSS)
    assert caps["refinement_thickness"] == pytest.approx(1.1 * 0.07)
    assert caps["wall_cell_floor"] == pytest.approx(2 * 0.01 / P.PASSAGE_CEILING_CELLS)
    # statistics with no band (the port radii, when the reading is not vouched for): the
    # narrowest passage sizes the band, as before
    assert P.size_caps({"p05": 0.05, "median": 0.1})["wall_cell"] == \
        pytest.approx(2 * 0.05 / P.PASSAGE_CELLS_ACROSS)


def test_cfmesh_never_bands_wider_than_the_widest_declared_port(monkeypatch):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    pts = np.array([[0.0, 0, 0], [1.0, 0, 0], [0.0, 1.0, 0], [1.0, 1.0, 0]])
    faces = np.array([[0, 1, 2], [1, 3, 2]])
    r = np.full(4, 0.19)                                   # the long side of the duct, everywhere
    monkeypatch.setattr(P, "staged_passage_field", lambda *_a, **_k: (pts, faces, r))
    decl = [{"name": "in", "type": "inlet", "width_mm": 384, "height_mm": 140},
            {"name": "out", "type": "outlet", "width_mm": 384, "height_mm": 140},
            {"name": "wall", "type": "wall"}]
    t = {"openings": {"in": {"area": 0.384 * 0.14, "centroid": [0, 0, 0]},
                      "out": {"area": 0.384 * 0.14, "centroid": [1, 0, 0]}}}
    chosen, field = R._passage_sizing(t, {}, "wall", decl)
    assert chosen["band"] == pytest.approx(0.07) and chosen["band_capped_at_declared_port"]
    assert field is not None and len(field) == 3
