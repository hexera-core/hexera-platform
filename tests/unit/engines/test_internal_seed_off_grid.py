# Responsibility: Verify the internal snappy case never hands snappyHexMesh a locationInMesh lying
# on a cell face of its background grid, at any refinement level. A part symmetric about a plane
# (a centred tee or wye, a straight pipe) has its seed on that plane, and the box built round the
# part is symmetric about it too: snappyHexMesh stopped with "is not inside the mesh or on a face
# or edge" on every attempt at tee_wye_018_fluid, and the untouched box read back as a leak.
# Boundaries: the case dictionaries written to a test folder; no OpenFOAM run.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshpipeline.engines.snappy.snappy_runner import render_internal_case

LEVELS = 10


def _render(tmp_path: Path, seed, bbox_min, bbox_max, base_cell):
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    names = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}
    summary = render_internal_case(
        ws, names=names, features={k: f"{k}.eMesh" for k in names}, interior_point=seed,
        bbox_min=bbox_min, bbox_max=bbox_max, base_cell=base_cell, surface_level=2,
        feature_level=3, n_layers=3, wall_key="wall")
    return ws, summary


def _grid(ws: Path):
    text = (ws / "system" / "blockMeshDict").read_text()
    verts = [tuple(float(v) for v in m.split()) for m in re.findall(r"\(([-\d.e+ ]+)\)", text.split("vertices", 1)[1].split(";")[0])]
    div = [int(v) for v in re.search(r"hex \([\d ]+\) \((\d+) (\d+) (\d+)\)", text).groups()]
    return verts[0], verts[6], div


def _location(ws: Path):
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    return [float(v) for v in re.search(r"locationInMesh \(([^)]*)\)", text).group(1).split()]


@pytest.mark.parametrize("bbox_min,bbox_max,base_cell", [
    ((0.0, -0.27, -0.063), (0.745, 0.27, 0.063), 0.01),      # the tee's own box: 58 cells in y
    ((0.0, -0.27, -0.063), (0.745, 0.27, 0.063), 0.02),      # odd counts: the plane is a level-1 face
    ((0.0, -0.0867, -0.0867), (1.263, 0.0867, 0.0867), 0.04),
])
def test_a_seed_on_the_parts_symmetry_planes_is_moved_off_every_cell_face(tmp_path, bbox_min, bbox_max, base_cell):
    seed = (0.376714, 0.0, 0.0)
    ws, summary = _render(tmp_path, seed, bbox_min, bbox_max, base_cell)
    lo, hi, div = _grid(ws)
    loc = _location(ws)
    for i in range(3):
        finest = (hi[i] - lo[i]) / div[i] / 2 ** LEVELS
        assert abs(loc[i] - seed[i]) <= 0.5 * finest + 1e-12          # it barely moves
        for lvl in range(LEVELS + 1):
            h = (hi[i] - lo[i]) / div[i] / 2 ** lvl
            f = (loc[i] - lo[i]) / h
            # a clear distance from every face of every level: no less than 0.4 of a finest cell
            assert abs(f - round(f)) * h >= 0.4 * finest, f"axis {i}: on a level-{lvl} cell face"
    assert summary["location_in_mesh"] == pytest.approx(loc, abs=1e-8)     # what the manifest reports
