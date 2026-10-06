# cfMesh EXTERNAL flow with no wall size from the builder: the wall is sized from the measured
# wetted area and the cell budget, not L/20 (an airliner came out with 452 wall faces and 49k cells
# in a 4M budget; HOME-TURF lab, 2026-10-05).
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh import cfmesh_runner as R


def test_the_wall_spends_the_budget():
    # an Ahmed body: 1.6 m2 wetted, 1.044 m long, 2M cells, 2 layers
    cell = R.external_wall_cell(1.6, 2_000_000, 2, 1.044)
    faces = 1.6 / cell ** 2
    shell = faces * (R.EXTERNAL_SHELL_DEPTH + 2)
    assert shell == pytest.approx(R.EXTERNAL_WALL_BUDGET_SHARE * 2_000_000, rel=1e-6)
    assert cell < 1.044 / 20.0 / 10.0                     # far finer than the old L/20


def test_never_coarser_than_the_old_default_and_safe_without_facts():
    assert R.external_wall_cell(1e-6, 2_000_000, 2, 1.0) == pytest.approx(1e-6 * 0 + (1e-6 * 6 / 1e6) ** 0.5)
    assert R.external_wall_cell(1e6, 100, 2, 1.0) == pytest.approx(1.0 / 20.0)
    assert R.external_wall_cell(0.0, 2_000_000, 2, 1.0) == pytest.approx(1.0 / 20.0)
    assert R.external_wall_cell(1.6, 0, 2, 1.0) == pytest.approx(1.0 / 20.0)


def test_more_layers_or_less_budget_coarsen_the_wall():
    base = R.external_wall_cell(10.0, 4_000_000, 2, 10.0)
    assert R.external_wall_cell(10.0, 4_000_000, 5, 10.0) > base
    assert R.external_wall_cell(10.0, 1_000_000, 2, 10.0) > base


def test_the_render_lets_an_external_wall_sit_eight_halvings_below_the_background(tmp_path):
    out = R.render_cfmesh_case(tmp_path, surface_file="geom.fms", wall_patch="body",
                               patches=[{"name": "body", "type": "wall"}], body_bbox=None, L=1.0,
                               domain_min=[0, 0, 0], domain_max=[14, 11, 11],
                               strategy={"wall_cell": 0.004}, cell_budget=2_000_000,
                               max_halvings=R.EXTERNAL_MAX_HALVINGS)
    assert out["wall_cell_size"] == pytest.approx(max(0.004, out["max_cell_size"] / 256))
    old = R.render_cfmesh_case(tmp_path, surface_file="geom.fms", wall_patch="body",
                               patches=[{"name": "body", "type": "wall"}], body_bbox=None, L=1.0,
                               domain_min=[0, 0, 0], domain_max=[14, 11, 11],
                               strategy={"wall_cell": 0.004}, cell_budget=2_000_000)
    assert old["wall_cell_size"] == pytest.approx(old["max_cell_size"] / 16)
