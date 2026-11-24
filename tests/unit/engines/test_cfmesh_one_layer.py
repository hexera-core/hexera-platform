"""CFDREQ-31: cfMesh's one wall-function layer is planned on the cell it is built from.

cfMesh builds ONE layer at about half the cell it stands on whatever maxFirstLayerThickness says, so an internal
wall-function plan sizes the wall cell from the y+ layer (finer or coarser than the background), lands the background on
it, and says the nearest y+ where the passage's cells across do not allow that cell."""

from __future__ import annotations

from tests.unit.engines.test_cfd_wall_function_cells import _ask

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell


def _h(ask: CR.Ask, D: float) -> float:
    return CR.y_plus_height(50.0, ask.req["physics"], length_m=D, length_from="the bore")[0]


def test_a_thin_y_plus_layer_gets_a_finer_wall_cell_not_a_cap_cfmesh_ignores():
    # a 100 mm bore on a 6 mm background: the y+ 50 layer is under half the background cell, so the old plan asked
    # for it at ~0.3 of the cell and cfMesh built half the cell (00134075: 1.37 x the plan)
    D, ask = 0.1, _ask(typed=False, engine="cfmesh")
    h = _h(ask, D)
    assert h < 0.5 * 0.006
    wall, background = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.006)
    assert 1.0 <= CR.CFMESH_ONE_LAYER_SHARE * wall / h < 1.1          # what cfMesh builds is the layer asked
    assert abs(octree_cell(background, wall) - wall) / wall < 0.01     # the halvings land on the wall cell
    assert 0.006 / 2 ** 0.5 <= background <= 0.006 * 2 ** 0.5
    fit = CR.wall_fit(ask, h, wall, cap_rel=CR.CFMESH_STACK_REL)
    assert fit["treatment"] == "functions" and not fit["why"] and abs(fit["y_plus"] - 50.0) < 1.0
    assert "0.5 of the cell it stands on" in ask.wall_level_note


def test_a_thick_y_plus_layer_coarsens_a_background_that_would_hold_the_wall_finer():
    # the passage caps' background (3.16 mm) held a 3.65 mm wall-function plan finer (00184523: y+ 50 -> 41)
    D, ask = 0.06, _ask(typed=False, engine="cfmesh")
    h = _h(ask, D)
    wall, background = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.75 * 2 * h)
    assert wall >= 2 * h * (1 - 1e-6) or round(D / wall) == CR.WALL_FUNCTION_MIN_CELLS_ACROSS
    assert background >= wall * (1 - 1e-9)


def test_a_bore_too_narrow_for_the_cell_builds_the_nearest_y_plus_and_says_why():
    D, ask = 0.0186, _ask(typed=False, engine="cfmesh")          # 1.43 mm cells at 13 across, a 1.7 mm y+ 50 layer
    wall, _bg = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.001)
    assert abs(wall - D / CR.WALL_FUNCTION_MIN_CELLS_ACROSS) < 1e-9
    note = ask.wall_level_note
    assert "the nearest to the y+ asked that the passage allows" in note and "keeps 13 cells across" in note
    assert "y+ 50 wall-function layer" in note


def test_our_minimum_across_never_refines_a_wall_the_plan_had_coarser():
    # 1.43 mm at 13 across, but the plan's wall (its background) is 2 mm already: the wall stays 2 mm, not refined to
    # 1.43 (the first cut refined 00185618 from 102k to 503k cells and capped its layer thinner)
    D, ask = 0.0186, _ask(typed=False, engine="cfmesh")
    wall, background = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.002, wall_cell=0.002)
    assert abs(wall - 0.002) < 1e-9 and abs(background - 0.002) < 1e-9
    # a builder's fine wall cell under a coarse background is no floor: our 13 across is
    ask = _ask(typed=False, engine="cfmesh")
    wall, bg = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.0075, wall_cell=0.0007)
    assert abs(octree_cell(bg, wall) - D / CR.WALL_FUNCTION_MIN_CELLS_ACROSS) < 1e-9


def test_not_for_a_users_own_sizes_or_layer_count():
    ask = _ask(typed=False, engine="cfmesh")
    ask.n_layers = 3
    assert CR.cfmesh_one_layer_cells(ask, bore_D=0.1, max_cell=0.006) is None
    ask = _ask(typed=False, engine="cfmesh")
    ask.wall_m = 0.002
    assert CR.cfmesh_one_layer_cells(ask, bore_D=0.1, max_cell=0.006) is None


def test_narrow_passage_boxes_under_the_layer_make_the_y_plus_row_the_nearest():
    D, ask = 0.06, _ask(typed=False, engine="cfmesh")
    wall, _bg = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.004)
    h = _h(ask, D)
    rid = ask.rid("layers:y_plus")
    ask.rows[rid] = {"id": rid, "status": "applied", "built": "wall functions: first layer", "treatment": "functions"}
    CR.cfmesh_thin_spot_layer(ask, [wall * 2.0])                       # coarser boxes: nothing to say
    assert ask.rows[rid]["status"] == "applied"
    CR.cfmesh_thin_spot_layer(ask, [0.4 * wall, 0.5 * wall])
    row = ask.rows[rid]
    assert row["status"] == "nearest" and "2 narrow passages are meshed with cells down to" in row["built"]
    assert "too narrow for the y+ asked" in row["built"]               # 0.5 x 0.4 x wall ~ 0.4 h: y+ ~ 20
    assert h > 0
