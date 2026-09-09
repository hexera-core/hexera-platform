# Responsibility: Verify the gmsh resolution clamp and floor measure the smallest declared port, not
# only the bounding box, so a wide part with a narrow bore is sized for the bore.
# volute_scroll_005 (job ff0b121c) shipped 9,293 cells with 83.6 degrees of non-orthogonality: its
# box is wide every way, so eight cells across the narrowest box extent was met by a 21 mm element
# on a part whose outlet bore is a few times that. The flow crosses the declared ports.
from __future__ import annotations

import math

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.gmsh import gates as G
from meshpipeline.engines.gmsh.driver import (
    MIN_CELLS_ACROSS,
    port_flow_width_mm,
    resolution_extent,
    smallest_port_extent,
)


def _diag(ext):
    return math.sqrt(sum(e * e for e in ext))


def test_a_bore_is_its_diameter_and_a_rectangle_its_shorter_side():
    assert port_flow_width_mm({"type": "outlet", "diameter_mm": 60.0}) == 60.0
    assert port_flow_width_mm({"type": "inlet", "width_mm": 205.0, "height_mm": 174.4}) == 174.4


def test_an_annulus_is_its_radial_gap_whichever_way_it_was_filed():
    ring = {"type": "inlet", "diameter_mm": 453.3, "inner_diameter_mm": 268.88}
    assert port_flow_width_mm(ring) == (453.3 - 268.88) / 2
    filed_as_gap = {"type": "inlet", "diameter_mm": 62.39, "inner_diameter_mm": 187.14,
                    "outer_diameter_mm": 311.92}
    assert port_flow_width_mm(filed_as_gap) == (311.92 - 187.14) / 2
    gap_only = {"type": "inlet", "diameter_mm": 49.91, "inner_diameter_mm": 208.6}
    assert port_flow_width_mm(gap_only) == 49.91


def test_an_area_alone_reads_as_its_equivalent_diameter_and_walls_do_not_count():
    a = port_flow_width_mm({"type": "outlet", "area_mm2": math.pi * 30.0 ** 2})
    assert abs(a - 60.0) < 1e-9
    assert port_flow_width_mm({"type": "wall"}) is None
    assert smallest_port_extent([{"name": "wall", "type": "wall"}]) is None
    assert smallest_port_extent([{"name": "inlet", "type": "inlet", "diameter_mm": None}]) is None


def test_the_smallest_declared_port_is_named_in_metres():
    ports = [{"name": "inlet", "type": "inlet", "diameter_mm": 120.0},
             {"name": "outlet", "type": "outlet", "diameter_mm": 60.0},
             {"name": "wall", "type": "wall"}]
    assert smallest_port_extent(ports) == (0.06, "outlet")


def test_a_volute_is_sized_for_its_outlet_bore_not_its_casing():
    # a 0.45 x 0.45 x 0.17 m casing whose outlet bore is 60 mm
    ext = [0.45, 0.45, 0.17]
    ports = [{"name": "outlet", "type": "outlet", "diameter_mm": 60.0}]
    min_ext, basis = resolution_extent(ext, _diag(ext), ports=ports)
    assert (min_ext, basis) == (0.06, "port:outlet")
    h_req = 0.03 * _diag(ext)                      # the factor sizing that shipped 9,293 cells
    h = min(h_req, min_ext / MIN_CELLS_ACROSS)
    assert h == 0.06 / MIN_CELLS_ACROSS and h < h_req


def test_a_port_wider_than_the_box_leaves_the_box_in_charge():
    ext = [1.0, 0.05, 0.05]
    ports = [{"name": "inlet", "type": "inlet", "diameter_mm": 50.0}]   # the bore IS the box
    assert resolution_extent(ext, _diag(ext), ports=ports) == (0.05, "bbox")
    assert resolution_extent(ext, _diag(ext), ports=None) == (0.05, "bbox")


def _ctx(quality):
    c = GateCtx(workspace=None, engine="gmsh", domain="", intake_patches=[], engine_params={})
    c.manifest_or_load = lambda: {"quality": quality}  # type: ignore[method-assign]
    return c


def test_the_floor_names_the_port_when_the_port_set_the_extent():
    q = {"size_h": 0.0215, "bounds": [0, 0, 0, 0.45, 0.45, 0.17],
         "min_extent": 0.06, "min_extent_basis": "port:outlet", "cells_across_min": 2.8}
    ok, fb = G._gate_resolution_floor(_ctx(q))
    assert not ok
    assert "declared port outlet" in fb and "60.0 mm" in fb and "absolute" in fb


def test_the_floor_still_speaks_of_the_box_without_a_port_basis():
    q = {"size_h": 0.040, "bounds": [0, 0, 0, 1.0, 0.05, 0.05]}
    ok, fb = G._gate_resolution_floor(_ctx(q))
    assert not ok and "narrowest dimension" in fb
