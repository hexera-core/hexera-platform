"""CFDREQ-31: cfMesh's one wall-function layer is planned on the cell it is built from.

cfMesh builds ONE layer at about half the cell it stands on whatever maxFirstLayerThickness says, so where the y+ layer
is thinner than that the internal wall-function plan refines the wall cell to it and lands the background on it; it
never coarsens the plan's cell, and says the nearest y+ where the layer is thicker than half the cell built."""

from __future__ import annotations

from tests.unit.engines.test_cfd_wall_function_cells import _ask

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines.cfmesh.cfmesh_runner import octree_cell


def _h(ask: CR.Ask, D: float) -> float:
    return CR.y_plus_height(50.0, ask.req["physics"], length_m=D, length_from="the bore")[0]


def test_a_thin_y_plus_layer_gets_a_finer_wall_cell_not_a_cap_cfmesh_ignores():
    # a 100 mm bore on 6 mm cells: the y+ 50 layer is under half the cell, so the old plan asked for it at ~0.3 of the
    # cell and cfMesh built half the cell (00134075: 1.37 x the plan)
    D, ask = 0.1, _ask(typed=False, engine="cfmesh")
    h = _h(ask, D)
    assert h < 0.5 * 0.006
    wall, background = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.006, wall_cell=0.006)
    assert 1.0 <= CR.CFMESH_ONE_LAYER_SHARE * wall / h < 1.01         # what cfMesh builds is the layer asked
    assert abs(octree_cell(background, wall) - wall) / wall < 0.01     # the halvings land on the wall cell
    assert 0.006 / 2 ** 0.5 <= background <= 0.006 * 2 ** 0.5
    fit = CR.wall_fit(ask, h, wall, cap_rel=CR.CFMESH_STACK_REL)
    assert fit["treatment"] == "functions" and not fit["why"] and abs(fit["y_plus"] - 50.0) < 1.0
    assert "0.5 of the cell it stands on" in ask.wall_level_note


def test_a_thick_y_plus_layer_never_coarsens_the_plans_cell_and_says_the_nearest():
    # a coarser wall puts the passages under the gap plan's cells across and its boxes refine them back (00185618: 102k
    # -> 503k cells when the first cut coarsened); the row says the cell BUILT (00184523 said 3.65 mm, built 3.16 mm)
    D, ask = 0.06, _ask(typed=False, engine="cfmesh")
    h = _h(ask, D)
    plan = 1.5 * h                                                     # under the 2 h the layer would need
    assert CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=plan, wall_cell=1.2 * plan) is None
    note = ask.wall_level_note
    assert f"the wall cells are {CR._mm(plan)}" in note and "the nearest the passage's cells allow" in note
    assert ask.cfmesh_one_layer["wall_m"] == plan


def test_a_layer_that_fits_the_plans_cell_is_left_alone():
    D, ask = 0.06, _ask(typed=False, engine="cfmesh")
    h = _h(ask, D)
    assert CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=2 * h, wall_cell=2 * h) is None
    assert ask.wall_level_note == ""


def test_not_for_a_users_own_sizes_or_layer_count():
    ask = _ask(typed=False, engine="cfmesh")
    ask.n_layers = 3
    assert CR.cfmesh_one_layer_cells(ask, bore_D=0.1, max_cell=0.006, wall_cell=0.006) is None
    ask = _ask(typed=False, engine="cfmesh")
    ask.wall_m = 0.002
    assert CR.cfmesh_one_layer_cells(ask, bore_D=0.1, max_cell=0.006, wall_cell=0.006) is None


def test_narrow_passage_boxes_under_the_layer_make_the_y_plus_row_the_nearest():
    D, ask = 0.1, _ask(typed=False, engine="cfmesh")
    wall, _bg = CR.cfmesh_one_layer_cells(ask, bore_D=D, max_cell=0.006, wall_cell=0.006)
    rid = ask.rid("layers:y_plus")
    ask.rows[rid] = {"id": rid, "status": "applied", "built": "wall functions: first layer", "treatment": "functions"}
    CR.cfmesh_thin_spot_layer(ask, [wall * 2.0])                       # coarser boxes: nothing to say
    assert ask.rows[rid]["status"] == "applied"
    CR.cfmesh_thin_spot_layer(ask, [0.4 * wall, 0.5 * wall])
    row = ask.rows[rid]
    assert row["status"] == "nearest" and "2 narrow passages are meshed with cells down to" in row["built"]
    assert "too narrow for the y+ asked" in row["built"]               # half of 0.4 x the wall cell: y+ ~ 20
