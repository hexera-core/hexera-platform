# Responsibility: Verify narrow passages are refined locally, level by level, and only there - never by sizing the whole part from its narrowest passage or its inlet.
# On the Fluent aorta (20 mm inlet, 1.2-3 mm vessels) snappy sized from the inlet came in at 6 cells across the
# vessels and at 11.2 after refining the whole part to 6.7 M cells; cfMesh sized from the narrowest vessel made no
# mesh in 47 minutes (2026-10-04).
from __future__ import annotations

import numpy as np
import pytest

import meshpipeline.engines.passage as P


def _line(start, direction, n, spacing):
    d = np.asarray(direction, dtype=float)
    return np.asarray(start, dtype=float) + np.outer(np.arange(n) * spacing, d / np.linalg.norm(d))


def test_only_the_narrow_branch_is_boxed_at_the_level_it_needs():
    trunk = _line([0, 0, 0], [1, 0, 0], 200, 0.005)                # 1 m trunk, r = 50 mm
    branch = _line([0.5, 0.06, 0], [0, 1, 0], 40, 0.0025)          # 100 mm branch, r = 5 mm
    pts = np.concatenate([trunk, branch])
    r = np.concatenate([np.full(len(trunk), 0.05), np.full(len(branch), 0.005)])
    cell = 2 * 0.05 / P.PASSAGE_CELLS_ACROSS                       # 13 across the trunk
    boxes = P.narrow_passage_regions(pts, r, cell_m=cell)
    assert boxes, "the branch is narrower than the trunk's cell carries"
    for b in boxes:
        lo, hi = np.asarray(b["min"]), np.asarray(b["max"])
        assert b["radius_m"] == pytest.approx(0.005)
        assert b["cell_needed_m"] == pytest.approx(2 * 0.005 / P.PASSAGE_CELLS_ACROSS)
        assert b["level_bump"] == 4                                 # 7.7 mm -> 0.48 mm, capped
        # nowhere near the trunk's far ends
        assert hi[0] < 0.6 and lo[0] > 0.4
    inside = np.zeros(len(branch), dtype=bool)
    for b in boxes:
        inside |= np.all((branch >= np.asarray(b["min"])) & (branch <= np.asarray(b["max"])), axis=1)
    assert inside.all(), "every narrow wall point is inside a box"


def test_each_level_is_boxed_on_its_own():
    a = _line([0, 0, 0], [1, 0, 0], 40, 0.002)
    b = _line([1, 0, 0], [1, 0, 0], 40, 0.002)
    cell = 0.01
    r_a = np.full(len(a), 0.6 * 6.5 * cell)       # 7.8 across: one halving reaches 15.6
    r_b = np.full(len(b), 0.3 * 6.5 * cell)       # 3.9 across: two halvings reach 15.6
    boxes = P.narrow_passage_regions(np.concatenate([a, b]), np.concatenate([r_a, r_b]), cell_m=cell)
    levels = {(round(bx["min"][0], 1), bx["level_bump"]) for bx in boxes}
    assert {bx["level_bump"] for bx in boxes} == {1, 2}
    assert all(lvl == 1 for x, lvl in levels if x < 0.5) and all(lvl == 2 for x, lvl in levels if x > 0.5)


def test_a_wide_enough_passage_and_reading_noise_are_left_alone():
    pts = _line([0, 0, 0], [1, 0, 0], 100, 0.01)
    r = 0.05 * (1 + 0.02 * np.sin(np.arange(100)))                # +-2 % noise on a 100 mm bore
    assert P.narrow_passage_regions(pts, r, cell_m=2 * 0.05 / 13) == []
    assert P.narrow_passage_regions(pts, r, cell_m=0.0) == []


def test_the_budget_lowers_the_levels_then_drops_boxes():
    pts = _line([0, 0, 0], [1, 0, 0], 100, 0.01)
    r = np.full(100, 0.0108)                      # 13 across needs cell / 6: three halvings
    areas = np.full(100, 1e-4)
    cell = 0.01
    free = P.narrow_passage_regions(pts, r, cell_m=cell, areas=areas)
    assert free and not free.note and max(b["level_bump"] for b in free) == 3
    held = P.narrow_passage_regions(pts, r, cell_m=cell, areas=areas, budget_cells=2_000)
    assert held.note and max(b["level_bump"] for b in held) < 3
    # what the budget leaves is what cfMesh is asked for: never finer than the level reached
    assert all(b["cell_needed_m"] >= b["cell_m"] for b in held)
    starved = P.narrow_passage_regions(pts, r, cell_m=cell, areas=areas, budget_cells=1)
    assert "not refined" in starved.note and len(starved) < len(free)


def test_the_cell_is_nudged_onto_whole_halvings_only_when_that_is_cheap():
    # the aorta: 0.839 mm bore cell, 1.23 mm-radius vessels
    aligned = P.octree_aligned_cell(0.839e-3, 1.23e-3)
    assert aligned == pytest.approx(4 * 2 * 1.23e-3 / 13)        # two halvings -> 13 across
    assert 0.839e-3 / aligned <= P.OCTREE_ALIGN_MAX
    # 1.9x finer would be needed: not worth it, unchanged
    assert P.octree_aligned_cell(1.0e-3, 13 * (1.0e-3 / 1.9) / 2) == 1.0e-3
    # a passage the cell already carries: unchanged
    assert P.octree_aligned_cell(1.0e-3, 0.05) == 1.0e-3


def test_the_ports_hold_the_reading_beside_them():
    pts = np.array([[0.0, 0.0, 0.0], [0.05, 0.0, 0.0], [0.5, 0.0, 0.0]])
    r = np.array([0.19, 0.19, 0.19])
    decl = [{"name": "in", "type": "inlet", "width_mm": 384, "height_mm": 140, "near_mm": [0, 0, 0]},
            {"name": "w", "type": "wall"}]
    ports = P.declared_port_widths(decl)
    assert ports == [((0.0, 0.0, 0.0), pytest.approx(0.07))]
    held = P.port_corrected_radius(pts, r, ports)
    assert held[0] == held[1] == pytest.approx(0.07) and held[2] == 0.19
    # the bound opening's measured centroid wins over the declared location
    moved = P.declared_port_widths(decl, {"in": {"centroid": [0.5, 0, 0]}})
    assert moved[0][0] == (0.5, 0.0, 0.0)


def test_the_band_is_read_by_area_not_by_point():
    # one big triangle at r = 10, many small ones at r = 1: the small ones are most of the
    # points and almost none of the wall
    big = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
    small = np.array([[2 + 0.01 * i, 0, 0] for i in range(30)] + [[2 + 0.01 * i, 0.01, 0] for i in range(30)])
    pts = np.concatenate([big, small])
    faces = [[0, 1, 2]] + [[3 + i, 4 + i, 33 + i] for i in range(29)]
    r = np.concatenate([np.full(3, 10.0), np.full(60, 1.0)])
    st = P.field_radius_stats(pts, np.asarray(faces), r)
    assert np.median(r) == 1.0 and st["band"] == 10.0 and st["median"] == 10.0
    assert st["p05"] == 1.0, "the narrow end stays by point: it is what the floor measures"


def _box_duct(tmp_path, w=0.384, h=0.14, length=1.0):
    import pyvista as pv
    # a rectangular duct along x as a CAD tessellation gives it: two long triangles a side, no
    # point inside any face, open at both ends
    y0, y1, z0, z1 = -w / 2, w / 2, -h / 2, h / 2
    corners = np.array([[0, y0, z0], [0, y1, z0], [0, y1, z1], [0, y0, z1],
                        [length, y0, z0], [length, y1, z0], [length, y1, z1], [length, y0, z1]])
    quads = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = []
    for a, b, c, d in quads:
        tris += [[a, b, c], [a, c, d]]
    tris = np.asarray(tris)
    pv.PolyData(corners.astype(float), np.hstack([np.full((len(tris), 1), 3), tris]).ravel()).save(
        str(tmp_path / "wall.stl"))
    return [[0.0, 0.0, 0.0], [length, 0.0, 0.0]]


def test_a_coarse_rectangular_duct_reads_its_short_side(tmp_path):
    ports = _box_duct(tmp_path)
    field = P.passage_field_of_stls([tmp_path / "wall.stl"], port_centroids=ports)
    assert field is not None
    pts, faces, r = field
    assert len(pts) > 100, "long edges are halved so every face is read"
    st = P.field_radius_stats(pts, faces, r)
    # half the 140 mm side - never the 192 mm half of the long one (bend_elbow_003, 2026-10-04)
    assert 0.06 < st["band"] < 0.08, st


def test_cfmesh_refines_the_narrow_branch_in_a_box_and_sizes_the_band_from_the_typical_passage(tmp_path):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    trunk = _line([0, 0, 0], [1, 0, 0], 200, 0.005)
    branch = _line([0.5, 0.06, 0], [0, 1, 0], 40, 0.0025)
    pts = np.concatenate([trunk, branch])
    r = np.concatenate([np.full(len(trunk), 0.05), np.full(len(branch), 0.005)])
    areas = np.concatenate([np.full(len(trunk), 1.5e-3), np.full(len(branch), 8e-5)])
    out = R.render_cfmesh_case(
        tmp_path, surface_file="geom.fms", wall_patch="wall", patches=[{"name": "wall", "type": "wall"}],
        body_bbox=([0, -0.05, -0.05], [1, 0.3, 0.05]), L=1.0,
        domain_min=[0, -0.05, -0.05], domain_max=[1, 0.3, 0.05], strategy={}, cell_budget=2_000_000,
        passage_radius={"min": 0.005, "p05": 0.005, "median": 0.05, "band": 0.05},
        passage_field=(pts, r, areas))
    text = (tmp_path / "system" / "meshDict").read_text()
    assert out["wall_cell_size"] == pytest.approx(2 * 0.05 / 13, rel=1e-3), "band from the trunk"
    assert "narrowPassage0" in text and "objectRefinements" in text
    assert out["narrow_regions"] and all(
        b["cell_size"] == pytest.approx(2 * 0.005 / 13, rel=1e-3) for b in out["narrow_regions"])


def test_snappy_turns_the_narrow_boxes_into_thin_regions_over_the_wall_level():
    from meshpipeline.engines.snappy import drivers as D
    pts = _line([0, 0, 0], [1, 0, 0], 50, 0.002)
    r = np.full(50, 0.002)
    field = (pts, r, np.full(50, 1e-5))
    boxes = D._narrow_passage_boxes(field, wall_cell=0.001, budget_cells=1e7)
    assert boxes and all({"min", "max", "level_bump"} <= set(b) for b in boxes)
    assert D._narrow_passage_boxes(None, wall_cell=0.001, budget_cells=1e7) == []
    # 13 across a 2 mm radius is 0.31 mm: from 0.4 mm that is 1.3x finer - too dear, it stays;
    # from 0.33 mm it is 1.07x finer, and the wall cell becomes it
    assert D._aligned_wall_cell(0.4e-3, field) == 0.4e-3
    assert D._aligned_wall_cell(0.33e-3, field) == pytest.approx(2 * 0.002 / 13)
    assert D._aligned_wall_cell(0.4e-3, None) == 0.4e-3


def test_a_lid_reads_its_hydraulic_diameter(tmp_path):
    import pyvista as pv
    # annular_001's annulus: a 151 mm bore around a 125 mm rod - the flow crosses a 13 mm gap
    ring = pv.Disc(inner=0.0625, outer=0.0755, r_res=4, c_res=180).triangulate()
    ring.save(str(tmp_path / "ring.stl"))
    disc = pv.Disc(inner=0.0, outer=0.05, r_res=6, c_res=180).triangulate()
    disc.save(str(tmp_path / "disc.stl"))
    assert P.lid_hydraulic_diameter(tmp_path / "ring.stl") == pytest.approx(0.026, rel=0.02)
    assert P.lid_hydraulic_diameter(tmp_path / "disc.stl") == pytest.approx(0.1, rel=0.01)
    dh = P.lid_hydraulic_diameters({"wall": "w.stl", "inlet": str(tmp_path / "ring.stl")}, "wall")
    assert set(dh) == {"inlet"}
    # the lid only narrows the port reading: a wall solid's lid can be its whole flanged end
    openings = {"inlet": {"area": 3.14159 * 0.0755 ** 2}, "outlet": {"area": 3.14159 * 0.05 ** 2}}
    st = P.port_radius_stats(openings, {"inlet": 0.026, "outlet": 0.3})
    assert st["min"] == pytest.approx(0.013, rel=1e-3) and st["max"] == pytest.approx(0.05, rel=1e-3)


def test_cfmesh_does_not_size_from_an_outer_skin_the_ports_do_not_vouch_for(monkeypatch):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    pts = np.array([[0.0, 0, 0], [1.0, 0, 0], [0.0, 1.0, 0], [1.0, 1.0, 0]])
    faces = np.array([[0, 1, 2], [1, 3, 2]])
    skin = np.full(4, 0.21)                    # straight_reducer_015's outer skin: 210 mm
    monkeypatch.setattr(P, "staged_passage_field",
                        lambda *_a, **k: (pts, faces, skin) if k.get("field") is None
                        else k["field"])
    monkeypatch.setattr(P, "lid_hydraulic_diameters", lambda *_a, **_k: {})
    t = {"openings": {"in": {"area": 3.14159 * 0.0825 ** 2}, "out": {"area": 3.14159 * 0.165 ** 2}}}
    chosen, field = R._passage_sizing(t, {}, "wall", [])
    assert chosen["source"] == "ports" and field is None
    assert chosen["p05"] == pytest.approx(0.0825, rel=1e-3)


def test_cfmesh_bands_at_the_narrowest_passage_while_that_is_affordable(monkeypatch):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    pts, faces = (np.array([[0.0, 0, 0], [1.0, 0, 0], [0.0, 1.0, 0], [1.0, 1.0, 0]]),
                  np.array([[0, 1, 2], [1, 3, 2]]))
    r = np.array([0.05, 0.1, 0.1, 0.1])
    monkeypatch.setattr(P, "staged_passage_field",
                        lambda *_a, **k: (pts, faces, r) if k.get("field") is None else k["field"])
    monkeypatch.setattr(P, "lid_hydraulic_diameters", lambda *_a, **_k: {})
    t = {"openings": {"in": {"area": 3.14159 * 0.06 ** 2}}}
    cheap, _ = R._passage_sizing(t, {}, "wall", [])
    assert cheap["band_from"] == "narrowest" and cheap["band"] == pytest.approx(cheap["p05"])
    monkeypatch.setattr(P, "GLOBAL_BAND_MAX_CELLS", 1)
    dear, field = R._passage_sizing(t, {}, "wall", [])
    assert dear["band_from"] == "typical" and dear["band"] > dear["p05"] and field is not None


def test_the_declared_size_gives_each_port_its_hydraulic_diameter():
    dh = P.declared_hydraulic_diameters([
        {"name": "pipe", "type": "inlet", "diameter_mm": 100},
        {"name": "annulus", "type": "outlet", "diameter_mm": 151.19, "inner_diameter_mm": 124.75},
        {"name": "duct", "type": "outlet", "width_mm": 384, "height_mm": 140},
        {"name": "area_only", "type": "outlet", "area_mm2": 5000},
        {"name": "wall", "type": "wall"}])
    assert dh["pipe"] == pytest.approx(0.1)
    assert dh["annulus"] == pytest.approx(0.02644)        # annular_001: the gap twice, not the bore
    assert dh["duct"] == pytest.approx(2 * 0.384 * 0.14 / (0.384 + 0.14))
    assert set(dh) == {"pipe", "annulus", "duct"}


def test_snappy_sizes_its_bore_by_the_smaller_hydraulic_diameter(monkeypatch):
    from meshpipeline.engines.snappy import drivers as D
    monkeypatch.setattr(D, "_lid_hydraulic_diameters", lambda *_a, **_k: {"inlet": 0.037})
    decl = [{"name": "inlet", "type": "inlet", "diameter_mm": 151.19, "inner_diameter_mm": 124.75}]
    assert D._port_hydraulic_diameters({}, "wall", decl)["inlet"] == pytest.approx(0.02644)
    t = {"binding": {"ports": [{"name": "inlet", "role": "inlet", "area_m2": 0.0027,
                                "opening_area_m2": 0.0179},
                               {"name": "outlet", "role": "outlet", "area_m2": 0.0027}]}}
    assert D._bore_port_name(t) == "inlet"
