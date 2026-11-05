# Responsibility: Verify the snappy background grid has cubic cells in every case, fits its
# budget, and holds the wall cell the recommender intended (one level per doubling).
# Boundaries: the blockMesh divisions and levels this system writes; the mesher is the lab's.
from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshpipeline.engines.snappy import snappy_runner as R


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.mark.parametrize("ext", [(2.06, 5.13, 1.54), (11.0, 7.0, 7.0), (40.0, 3.0, 3.0),
                                 (0.5, 0.5, 0.5)])
def test_the_background_cells_are_cubic_and_the_box_fits_the_budget(ext):
    base = 0.0053
    div, k = R.background_grid(ext, base)
    cells = [e / d for e, d in zip(ext, div)]
    assert div[0] * div[1] * div[2] <= R.BG_CELL_BUDGET and max(div) <= R.BG_MAX_DIV
    assert max(cells) / min(cells) < 1.15, (
        f"{ext} -> {div}: cells {cells} - a background trimmed on its longest axis alone gives "
        "every refinement level the same stretched cell")
    # every doubling is one level the wall gets back: the wall cell the recommender intended
    for c in cells:
        assert c / (2 ** k) == pytest.approx(base, rel=0.15)


def test_the_supra_box_is_no_longer_a_cube_count_on_a_stretched_box():
    # job 9548829e: 2.06 x 5.13 x 1.54 m around a 128 mm car came out 82 x 82 x 82
    div, k = R.background_grid((2.05656, 5.12818, 1.53648), 0.00534121363375)
    assert div != [82, 82, 82]
    assert k == 3 and div == [48, 120, 36]


def test_a_box_that_fits_keeps_its_count_and_needs_no_bump():
    div, k = R.background_grid((1.0, 0.5, 0.5), 0.05)
    assert k == 0 and div == [20, 12, 12]


def test_a_slab_axis_stays_on_the_minimum_count():
    div, _ = R.background_grid((10.0, 10.0, 0.05), 0.05)
    assert div[2] == R.BG_MIN_DIV


def test_a_long_pipe_gets_cubic_background_cells(tmp_path):
    ws = _ws(tmp_path)
    s = R.render_internal_case(
        ws, names={"wall": "wall", "inlet": "inlet", "outlet": "outlet"},
        features={"wall": "wall.eMesh", "inlet": "inlet.eMesh", "outlet": "outlet.eMesh"},
        interior_point=[0.5, 0.0, 0.0], bbox_min=[0.0, -0.05, -0.05], bbox_max=[1.0, 0.05, 0.05],
        base_cell=0.1 / 24, surface_level=2, feature_level=3, n_layers=5)
    blk = (ws / "system" / "blockMeshDict").read_text()
    pad = max(2.0 * 0.1 / 24, 0.03)
    dext = [1.0 + 2 * pad, 0.1 + 2 * pad, 0.1 + 2 * pad]
    cells = [e / d for e, d in zip(dext, s["divisions"])]
    assert max(s["divisions"]) <= 120
    assert max(cells) / min(cells) < 1.15, (s["divisions"], cells)
    assert f"({s['divisions'][0]} {s['divisions'][1]} {s['divisions'][2]})" in blk
    # the wall cell is held: one bump per doubling the 120 cap took
    wall = max(cells) / 2 ** s["surface_level"][0]
    assert math.isclose(wall, (0.1 / 24) / 4, rel_tol=0.2)
