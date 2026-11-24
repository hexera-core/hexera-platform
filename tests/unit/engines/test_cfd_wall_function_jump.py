"""CFDREQ-24 (extended, rulebook 86): a wall-function layer at plan under a jump - the cell above more than twice
thinner - is never counted in the y+ share, whatever its shape."""

from __future__ import annotations

from tests.unit.engines.test_layer_census import _write_column_mesh

from meshpipeline.engines import layer_census as LC


def test_a_flat_layer_at_plan_under_a_jump_leaves_the_y_plus_share(tmp_path):
    pm = tmp_path / "flat" / "polyMesh"
    _write_column_mesh(pm, [0.5, 0.1, 0.1, 1.0])              # a flat layer at plan, the cell above 5x thinner
    w = LC.census(pm, 1, ["wall"], planned_first_m=0.5)[0]["wall"]
    assert w["area_at_plan"] == 1.0                           # CFDREQ-21 still counts the layer by shape
    assert w["area_under_jump"] == 1.0 and w["area_first_at_plan"] == 0.0
    smooth = tmp_path / "smooth" / "polyMesh"
    _write_column_mesh(smooth, [0.5, 0.45, 0.45, 1.0])
    assert LC.census(smooth, 1, ["wall"], planned_first_m=0.5)[0]["wall"]["area_first_at_plan"] == 1.0
