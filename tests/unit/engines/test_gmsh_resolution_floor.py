# The baseline corpus shipped 14 gmsh "passes" at 0.3-5% of budget - transition_006_fluid
# at 6,455 cells, s_duct_001_fluid 132x coarser than its solid twin - because gmsh sizes
# from a factor of the part DIAGONAL, and a long thin duct's diagonal is its length, so
# 4% of it exceeds the bore. The cells are well-shaped (they clear sicn) but only a
# handful span the flow: silent under-resolution and training-label poison. The
# resolution_floor gate rejects them; it is self-contained (reads size_h + bounds that
# quality.json already carries), so it needs no mesh-image change to take effect.
from __future__ import annotations

from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.gmsh import gates as G


def _ctx(quality):
    c = GateCtx(workspace=None, engine="gmsh", domain="", intake_patches=[],
                engine_params={})
    c.manifest_or_load = lambda: {"quality": quality}  # type: ignore[method-assign]
    return c


# a 1 m duct, 50 mm bore, meshed at h = 40 mm -> ~1.25 cells across the bore
_UNDER = {"size_h": 0.040, "bounds": [0, 0, 0, 1.0, 0.05, 0.05]}
# same duct meshed at h = 5 mm -> 10 cells across the bore
_GOOD = {"size_h": 0.005, "bounds": [0, 0, 0, 1.0, 0.05, 0.05]}


def test_under_resolved_duct_is_rejected():
    ok, fb = G._gate_resolution_floor(_ctx(_UNDER))
    assert not ok
    assert "[RESOLUTION]" in fb and "narrowest" in fb
    # the message must point at the real fix (absolute sizing), not just "coarsen/refine"
    assert "absolute" in fb


def test_well_resolved_duct_passes():
    ok, fb = G._gate_resolution_floor(_ctx(_GOOD))
    assert ok, fb


def test_driver_supplied_cells_across_wins_over_the_bbox_estimate():
    # when the driver recorded its own count, the gate trusts it (it knows the meshed
    # extent exactly) - here a good bbox ratio but an honestly-low recorded count fails
    q = {"size_h": 0.005, "bounds": [0, 0, 0, 1.0, 0.05, 0.05], "cells_across_min": 3.0}
    ok, fb = G._gate_resolution_floor(_ctx(q))
    assert not ok and "3.0 elements" in fb


def test_missing_fields_do_not_fire_the_floor():
    # an older deck without size_h/bounds is not judged here (other gates still apply)
    assert G._gate_resolution_floor(_ctx({}))[0]
    assert G._gate_resolution_floor(_ctx({"size_h": 0.01}))[0]
    assert G._gate_resolution_floor(_ctx({"bounds": [0, 0, 0, 1, 1, 1]}))[0]


def test_the_floor_is_wired_into_the_gmsh_chain():
    from meshpipeline.engines.registry import get_spec
    keys = [g.key for g in get_spec("gmsh").gates]
    assert "resolution_floor" in keys
    # after soundness, before naming
    assert keys.index("sicn_floor") < keys.index("resolution_floor")
    assert keys.index("resolution_floor") < keys.index("patch_contract")


def _diag(ext):
    return sum(e * e for e in ext) ** 0.5


def test_driver_clamp_forces_min_cells_across_the_narrow_dimension():
    # the driver's own extent rule: a factor-of-diagonal size that under-resolves the
    # bore is tightened so MIN_CELLS_ACROSS elements span the narrowest real extent
    from meshpipeline.engines.gmsh.driver import MIN_CELLS_ACROSS, narrowest_extent
    ext = [1.0, 0.05, 0.05]          # 1 m duct, 50 mm bore
    diag = _diag(ext)
    h_req = 0.04 * diag             # gmsh default factor of the diagonal
    min_ext = narrowest_extent(ext, diag)
    assert min_ext == 0.05
    h = min(h_req, min_ext / MIN_CELLS_ACROSS)
    assert h < h_req                                   # the clamp engaged
    assert min_ext / h >= MIN_CELLS_ACROSS - 1e-9      # enough cells across the bore


def test_a_planar_case_has_no_thickness_to_clamp_against():
    # The 2D fixture's box is 0.2 x 0.1 x ~1e-9. The old rule clamped to eight cells
    # across the 1e-9 "thickness" and the mesh never finished. A planar case spans its
    # in-plane extents.
    from meshpipeline.engines.gmsh.driver import narrowest_extent
    ext = [0.2, 0.1, 1e-9]
    assert narrowest_extent(ext, _diag(ext), planar=True) == 0.1
    # a tilted planar face shows three real box extents: still no thickness
    tilted = [0.2, 0.1, 0.03]
    assert narrowest_extent(tilted, _diag(tilted), planar=True) == 0.1


def test_bbox_noise_is_not_a_dimension_even_in_3d():
    from meshpipeline.engines.gmsh.driver import narrowest_extent
    ext = [1.0, 1.0, 1e-9]
    assert narrowest_extent(ext, _diag(ext)) == 1.0


def test_a_genuinely_thin_3d_part_still_counts_its_thickness():
    # a 0.5 mm plate in a 2 m box is 2.5e-4 of the diagonal - real, and the clamp
    # must still put cells across it
    from meshpipeline.engines.gmsh.driver import narrowest_extent
    ext = [2.0, 0.5, 0.0005]
    assert narrowest_extent(ext, _diag(ext)) == 0.0005
