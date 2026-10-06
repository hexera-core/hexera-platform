# Responsibility: Verify a multi-region case gives a thin solid region (a pipe wall) enough cells across its wall to keep its cellZone.
# s_duct_001_cht (lab, 2026-10-06): a 7.7 mm pipe wall around a 0.4 m bore, meshed at the global
# surface level (7-14 mm cells), came out of the region split as five unzoned pieces
# (domain1..domain5) and no wall_solid at all. The per-solid scale read the wall's smallest extent -
# the pipe's 0.41 m diameter - so nothing asked for more.
from __future__ import annotations

import json
import math

import pytest

from meshpipeline.engines.snappy_multiregion import multiregion_runner as R

D, T, L = 0.3996, 0.0077, 2.135          # s_duct_001_cht: bore, wall, length (m)
WALL = {"index": 1, "volume": math.pi * (D + T) * T * L, "area": 2 * math.pi * (D + T / 2) * L,
        "bbox_min": [0.0, -0.2075, -0.1446], "bbox_max": [2.135, 0.2075, 0.5649]}
FLUID = {"index": 0, "volume": math.pi * D * D / 4 * L, "area": math.pi * D * L,
         "bbox_min": [0.0, -0.1998, -0.1369], "bbox_max": [2.135, 0.1998, 0.5572]}
BASE = 2.288 / 40.0                       # the assembly diagonal / 40, as configure sizes it


def test_a_pipe_wall_is_read_by_its_thickness_and_a_block_rod_or_plate_by_its_extent():
    assert R.wall_thickness(WALL) == pytest.approx(T, rel=0.02)
    assert R.wall_thickness(FLUID) is None                    # 2V/A = D/2: a rod
    assert R.wall_thickness({"volume": 1.0, "area": 6.0, "bbox_min": [0, 0, 0],
                             "bbox_max": [1, 1, 1]}) is None  # a cube: 2V/A = 1/3 of its side
    assert R.wall_thickness({"volume": 0.01, "area": 2.04, "bbox_min": [0, 0, 0],
                             "bbox_max": [1, 1, 0.01]}) is None   # a plate: its extent is its wall
    assert R.wall_thickness({"index": 3, "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1]}) is None


def test_the_level_that_puts_two_cells_across_the_wall():
    assert R.level_for(BASE, T, 2.0) == 4                   # 57 mm / 16 = 3.6 mm: 2.2 across 7.7 mm
    assert R.level_for(1.0, 0.5, 2.0) == 2                  # exactly 2 across at 1/4
    assert R.level_for(1.0, 4.0, 2.0) == 0


def test_only_a_region_left_under_two_cells_across_is_raised():
    rmap = R.region_map([{"name": "fluid", "type": "fluid", "solids": [0]},
                         {"name": "wall_solid", "type": "solid", "solids": [1]}])
    by_index = {0: FLUID, 1: WALL}
    got = R.thin_region_levels(rmap, by_index, BASE, (2, 2))
    assert set(got) == {"wall_solid"} and got["wall_solid"]["level"] == 4
    # a builder that already asked for the level is left as it is
    assert R.thin_region_levels(rmap, by_index, BASE, (2, 2), {"wall_solid": [4, 4]}) == {}


def _ws(tmp_path, solids):
    (tmp_path / "_assembly").mkdir()
    tri = ("solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
           "endloop\nendfacet\nendsolid s\n")
    for s in solids:
        p = tmp_path / "_assembly" / f"solid_{s['index']}.stl"
        p.write_text(tri)
        s["stl"] = str(p)
    (tmp_path / "_assembly" / "solids.json").write_text(json.dumps(solids))
    return tmp_path


def test_configure_raises_the_thin_wall_region_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: (1.0, 0.0, 0.2))
    ws = _ws(tmp_path, [dict(FLUID), dict(WALL)])
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "fluid", "type": "fluid", "solids": [0]},
        {"name": "wall_solid", "type": "solid", "solids": [1]}]})
    assert out["thin_regions"] == {"wall_solid": {"wall_thickness_mm": pytest.approx(7.7, rel=0.02),
                                                  "level": 4}}
    shm = (ws / "system" / "snappyHexMeshDict").read_text()
    assert "level (2 2);" in shm                 # the fluid keeps the global level
    assert "level (4 5);" in shm                 # the wall: 4, + the interface refinement


def test_a_wall_too_thin_to_afford_is_said_not_meshed_into_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_region_interior_point", lambda stl, mn, mx: (1.0, 0.0, 0.2))
    foil = dict(WALL, volume=math.pi * (D + 1e-5) * 1e-5 * L)    # a 0.01 mm skin
    ws = _ws(tmp_path, [dict(FLUID), foil])
    out = R.configure_mesh(ws, strategy={"regions": [
        {"name": "fluid", "type": "fluid", "solids": [0]},
        {"name": "wall_solid", "type": "solid", "solids": [1]}]})
    assert "thin_regions" not in out
    assert out["thin_regions_unresolved"]["wall_solid"]["level"] > R.MAX_THIN_REGION_LEVEL
    assert "wall_solid (wall 0.01 mm" in out["note"]


def test_the_geometry_report_gives_the_wall_its_level(tmp_path):
    ws = tmp_path
    (ws / "_assembly").mkdir()
    (ws / "_assembly" / "solids.json").write_text(json.dumps([
        {**FLUID, "centroid": [1, 0, 0.2]}, {**WALL, "centroid": [1, 0, 0.2]}]))
    rep = R.inspect_stl(ws)
    row = {p["index"]: p for p in rep["per_solid_scale"]}
    assert row[1]["wall_thickness"] == pytest.approx(T, rel=0.02) and row[1]["needed_level"] == 4
    assert "wall_thickness" not in row[0]
