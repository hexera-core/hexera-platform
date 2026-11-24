"""CFDREQ-32 (rulebook 98): before a laminar stack goes past the count snappyHexMesh keeps (LAMINAR_LAYERS_KEPT), its
first layer is thinned (down to LAMINAR_CAP_THIN of the floor) so the wall cells inside the boundary layer make up the
cells across it. Batch 7: the same dictionaries with 10 layers instead of 5-8 kept the stack on 58-77% of the wall
where 5-8 had kept it on 85-90% (abc 00008237, 00008746, 00039457)."""
from __future__ import annotations

from meshpipeline.engines import cfd_requirements as CR


def test_a_layer_deep_enough_for_core_cells_is_met_on_the_kept_count():
    # a layer 2.6 wall cells deep: 8 layers from the floor put 8.95 across, so the old plan filled it with 10 layers
    delta, wc = 0.0026, 0.001
    floor = delta / CR.stack_height(1.0, 1.2, CR.BL_CELLS)
    n, first, across = CR.laminar_stack(delta, wc, 1.2)
    assert n == CR.LAMINAR_LAYERS_KEPT
    assert across >= CR.BL_CELLS - 1e-9
    assert CR.LAMINAR_CAP_THIN * floor * (1 - 1e-9) <= first < floor


def test_a_layer_too_thin_for_core_cells_still_fills_with_layers():
    # a layer 1.2 wall cells deep: even the thinned 8-layer stack cannot reach BL_CELLS, so the stack fills it as before
    n, first, across = CR.laminar_stack(0.0012, 0.001, 1.2)
    assert n == CR.BL_CELLS and across >= CR.BL_CELLS - 1e-9


def test_a_count_of_the_users_is_never_changed():
    assert CR.laminar_stack(0.0026, 0.001, 1.2, n_fixed=10)[0] == 10
