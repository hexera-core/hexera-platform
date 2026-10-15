# Responsibility: Verify the multi-region snappy case never hands snappyHexMesh a locationInMesh lying
# on a cell face of its background grid, at any refinement level - the rule the internal snappy case
# already keeps (test_internal_seed_off_grid). A region's seed is the centre of its box when that lies
# inside it, which on a part symmetric about a plane (a straight pipe, a reducer, a centred tee) is ON
# the plane, and the background box padded evenly round the assembly is symmetric about it too.
# Boundaries: configure_mesh on a staged surface assembly in a test folder; no OpenFOAM run.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshpipeline.cad.stl_io import _box_triangles, write_stl_solids
from meshpipeline.engines.snappy_hexmesh import SEED_OFF_GRID_LEVEL, off_grid_point
from meshpipeline.engines.snappy_multiregion import multiregion_runner as R

REGIONS = [{"name": "fluid", "type": "fluid", "solids": [0]},
           {"name": "wall_solid", "type": "solid", "solids": [1]}]


def _assembly(ws: Path, fluid_lo, fluid_hi, solid_lo, solid_hi) -> None:
    ws.mkdir(parents=True)
    write_stl_solids(ws / "input.stl", {"fluid": _box_triangles(fluid_lo, fluid_hi),
                                        "wall_solid": _box_triangles(solid_lo, solid_hi)})


def _grid(ws: Path):
    text = (ws / "system" / "blockMeshDict").read_text()
    body = text.split("vertices", 1)[1].split(";", 1)[0]
    verts = [tuple(float(v) for v in m.split()) for m in re.findall(r"\(([-\d.e+ ]+)\)", body)]
    hexes = re.search(r"hex \([\d ]+\) \((\d+) (\d+) (\d+)\)", text)
    assert hexes is not None
    return verts[0], verts[6], [int(v) for v in hexes.groups()]


def _location(ws: Path):
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    found = re.search(r"locationInMesh \(([^)]*)\)", text)
    assert found is not None
    return [float(v) for v in found.group(1).split()]


@pytest.mark.parametrize("fluid_lo,fluid_hi,solid_lo,solid_hi", [
    # a fluid block beside its wall block, both centred on y = z = 0: the seed is ON two planes
    ((-0.05, -0.05, -0.05), (0.05, 0.05, 0.05), (0.05, -0.05, -0.05), (0.15, 0.05, 0.05)),
    # odd counts: the centre plane is a face of the level-1 cells
    ((-0.05, -0.0675, -0.0675), (0.05, 0.0675, 0.0675), (0.05, -0.0675, -0.0675),
     (0.15, 0.0675, 0.0675)),
])
def test_a_region_seed_on_the_parts_symmetry_planes_is_moved_off_every_cell_face(
        tmp_path, fluid_lo, fluid_hi, solid_lo, solid_hi):
    ws = tmp_path / "case"
    _assembly(ws, fluid_lo, fluid_hi, solid_lo, solid_hi)
    out = R.configure_mesh(ws, strategy={"regions": REGIONS, "n_layers": 0})
    assert out.get("success", True), out
    lo, hi, div = _grid(ws)
    loc = _location(ws)
    seed = [(a + b) / 2 for a, b in zip(fluid_lo, fluid_hi)]       # the region's own box centre
    levels = SEED_OFF_GRID_LEVEL
    for i in range(3):
        finest = (hi[i] - lo[i]) / div[i] / 2 ** levels
        assert abs(loc[i] - seed[i]) <= 0.5 * finest + 1e-12          # it barely moves
        for lvl in range(levels + 1):
            h = (hi[i] - lo[i]) / div[i] / 2 ** lvl
            f = (loc[i] - lo[i]) / h
            assert abs(f - round(f)) * h >= 0.4 * finest, f"axis {i}: on a level-{lvl} cell face"
        assert fluid_lo[i] < loc[i] < fluid_hi[i]                      # still in the fluid


def test_the_grid_the_seed_is_placed_against_is_the_grid_written():
    lo, hi, div = R.background_box((0.0, -0.05, -0.05), (0.2, 0.05, 0.05), 0.01, pad=0.15)
    text = R.render_block_mesh((0.0, -0.05, -0.05), (0.2, 0.05, 0.05), 0.01, pad=0.15)
    assert f"hex (0 1 2 3 4 5 6 7) ({div[0]} {div[1]} {div[2]})" in text
    assert f"({lo[0]:.6g} {lo[1]:.6g} {lo[2]:.6g})" in text
    assert f"({hi[0]:.6g} {hi[1]:.6g} {hi[2]:.6g})" in text


def test_the_shared_rule_is_the_one_the_internal_case_uses():
    from meshpipeline.engines.snappy import snappy_runner
    assert snappy_runner._off_grid is off_grid_point
