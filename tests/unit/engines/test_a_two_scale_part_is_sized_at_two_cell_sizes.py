"""A part whose passages come in two very different sizes is sized at two cell sizes.

WHY THIS EXISTS. The cell size follows from the narrowest passage and the refinement band is priced
over the WHOLE wetted area at that size. On a part with two scales that is the wrong bill by orders
of magnitude. The first real customer cold plate is a manifold of 2.1 mm channels bonded to a layer
of 0.098 mm slots: the slots are 35.9% of the wetted area inside 6.0% of the volume, and banding
all 60,526 mm2 at a slot-sized cell estimates about three billion cells.

So the broad side is held at what it needs - 12 cells across a 2.1 mm channel is plenty, and every
cell finer than that is spent over 38,813 mm2 - and the remaining budget goes to the thin side,
which is the opposite of sizing the whole part from its narrowest feature.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B

#: The customer cold plate, as cad/scales measured it on 2026-10-28.
LO = (-0.06157654, 0.02011018, -0.008590401)
HI = (0.1159235, 0.06194618, 0.0924096)
CUSTOMER = {
    "split_m": 4.883e-4,
    "broad": {"width_m": 2.1123723e-3, "area_m2": 38813.50e-6},
    "fine": {"width_m": 0.0982e-3, "area_m2": 21712.67e-6},
}


def _plan(scales=None, limit=700_000_000, asked=12, prismatic_axis=None, ranks=1):
    return B.plan_two_scales(block_lo=LO, block_hi=HI, scales=scales or CUSTOMER,
                             limit=limit, asked_across=asked,
                             prismatic_axis=prismatic_axis, ranks=ranks)


# ---------------------------------------------------------------- the estimate, twice over

def test_the_two_bands_are_priced_at_their_own_spans():
    """The whole point of the split: each surface at its OWN cell size and its OWN octree span.

    A thin surface four levels below the background carries four levels of grading shells, and
    those shells are most of the mesh - see DEPTH_AT_FITTED_SPAN.
    """
    kw = dict(block_lo=LO, block_hi=HI, broad_cell_m=1.7603e-4, broad_area_m2=38813.50e-6,
              fine_cell_m=4.4007e-5, fine_area_m2=21712.67e-6, background_m=3.5206e-4,
              broad_levels=1)
    shallow = B.two_scale_estimate(**kw, fine_levels=3)
    deep = B.two_scale_estimate(**kw, fine_levels=5)
    assert deep > shallow, "the span has to cost something, or nothing was learned on 2026-10-28"
    assert deep / shallow > 1.3


def test_the_measured_run_is_bracketed_by_the_estimate():
    """The run that built: 102,152,624 octree boxes. The estimate must sit ABOVE it and inside a
    factor of two - under would size a machine for less than the run needs."""
    est = B.two_scale_estimate(
        block_lo=LO, block_hi=HI, broad_cell_m=1.7603e-4, broad_area_m2=38813.50e-6,
        fine_cell_m=4.4007e-5, fine_area_m2=21712.67e-6, background_m=3.5206e-4,
        broad_levels=1, fine_levels=3)
    assert est >= 102_152_624, f"the estimate {est:,} is under the mesh that was built"
    assert est < 2 * 102_152_624


@pytest.mark.parametrize("bad", [
    dict(broad_cell_m=0.0), dict(fine_cell_m=0.0), dict(background_m=-1.0),
    dict(fine_cell_m=1.0),          # finer than the broad cell? no
])
def test_a_plan_that_is_not_a_plan_is_refused(bad):
    kw = dict(block_lo=LO, block_hi=HI, broad_cell_m=1.7603e-4, broad_area_m2=38813.50e-6,
              fine_cell_m=4.4007e-5, fine_area_m2=21712.67e-6, background_m=3.5206e-4,
              broad_levels=1, fine_levels=3)
    kw.update(bad)
    with pytest.raises(ValueError):
        B.two_scale_estimate(**kw)


# ---------------------------------------------------------------- what the planner picks

#: The configuration that actually BUILT this part on mesh-bench, 2026-10-28: cartesianMesh,
#: topoSet and splitMeshRegions all completed on an octree of 102,152,624 internal boxes, 3060 s,
#: peak 185 GB of 251. This is the row the planner has to be able to reach by itself.
BUILT = {"background_m": 3.5206e-4, "broad_levels": 1, "fine_levels": 3,
         "fine_cell_m": 4.4007e-5, "boxes": 102_152_624}


def test_the_configuration_that_actually_built_is_now_refused_for_being_too_coarse():
    """IT BUILT, AND IT DOES NOT SOLVE, and those are different questions.

    The planner used to derive this configuration exactly - same background, same two spans, same
    fine cell - and reaching it was the whole point of the two-scale work. What it reaches is 2.23
    cells across the 0.098 mm slots, and the mesh that came out of it has its coolant in 19
    disconnected pieces. The run's own report says so: "this mesh will not solve even though
    checkMesh calls it OK."

    FINE_ACROSS_FLOOR was two while four was unaffordable. It is four now, because stretching the
    cells along the axis the channels are extruded along brings 4.46 across from 966,955,439 cells
    to 181,673,384. So the rung that built is below the floor and is refused, which is the
    behaviour that was wanted all along and could not be had.
    """
    plan, said = _plan(limit=700_000_000)
    assert plan is None, (
        f"2.23 cells across is below the {B.FINE_ACROSS_FLOOR}-cell floor and must be refused; "
        f"the planner offered {plan}")
    assert "two scales of passage" in said


def test_with_the_stretch_it_reaches_a_resolution_that_holds_the_passages_together():
    """The same part, the same budget, with the licence to stretch along its extruded axis."""
    plan, said = _plan(limit=700_000_000, prismatic_axis=1)
    assert plan is not None, f"the stretched plan has to be reachable: {said}"
    assert plan["background_m"] == pytest.approx(BUILT["background_m"], rel=1e-3)
    assert plan["broad_levels"] == BUILT["broad_levels"]
    # one level finer than the run that built, which is what doubles the cells across
    assert plan["fine_levels"] == BUILT["fine_levels"] + 1
    assert plan["fine_cell_m"] == pytest.approx(BUILT["fine_cell_m"] / 2.0, rel=1e-3)
    assert plan["fine_across"] == pytest.approx(4.46, abs=0.02)
    assert plan["fine_across"] >= B.FINE_ACROSS_FLOOR
    assert plan["stretch"] > 1.0 and plan["stretch_axis"] == 1
    assert "two cell sizes" in said and "prismatic" in said


def test_the_estimate_for_that_configuration_stays_above_the_mesh_and_under_the_ceiling():
    """The two bounds that have to hold at once for this part to be plannable at all, and the
    reason the margin is dropped for one of them."""
    plan, _said = _plan(limit=700_000_000, prismatic_axis=1)
    raw = B.two_scale_estimate(
        block_lo=LO, block_hi=HI, broad_cell_m=plan["broad_cell_m"],
        broad_area_m2=CUSTOMER["broad"]["area_m2"], fine_cell_m=plan["fine_cell_m"],
        fine_area_m2=CUSTOMER["fine"]["area_m2"], background_m=plan["background_m"],
        broad_levels=plan["broad_levels"], fine_levels=plan["fine_levels"], margin=False,
        fine_stretch=plan["stretch"])
    assert raw >= BUILT["boxes"], (
        f"the unmargined estimate {raw:,} is UNDER the {BUILT['boxes']:,} that was built")
    assert raw <= B.octree_ceiling_cells(), (
        f"the unmargined estimate {raw:,} is over the ceiling {B.octree_ceiling_cells():,}, so a "
        "plan that is known to build would be refused")
    # with the margin on it is higher still, which is what sizes the machine
    assert plan["estimate"] > raw


def test_the_default_deployment_still_refuses_this_part():
    """It needs about a hundred million cells, and the default cell limit is eight million. The
    refusal names the part's two scales rather than its narrowest passage, because a user told
    "your 0.098 mm feature is too fine" has been told the wrong thing about their own geometry."""
    plan, said = _plan(limit=8_000_000)
    assert plan is None
    assert "two scales of passage" in said
    assert "2.11 mm" in said and "0.0982 mm" in said, "the refusal has to name both scales"
    assert f"{B.octree_ceiling_cells():,}" in said


def test_a_part_whose_thin_layer_is_small_is_planned_rather_than_refused():
    """The same part with a tenth of the thin area - a few slots rather than a bonded layer. The
    broad side is unchanged, so this isolates what the thin side costs."""
    scales = {**CUSTOMER, "fine": {**CUSTOMER["fine"], "area_m2": CUSTOMER["fine"]["area_m2"] / 10}}
    plan, said = _plan(scales=scales, limit=700_000_000)
    assert plan is not None, f"a thin layer a tenth the size is still refused: {said}"
    assert plan["fine_across"] >= B.FINE_ACROSS_FLOOR
    assert plan["fine_cell_m"] < plan["broad_cell_m"], "the fine cell has to be the finer one"
    assert plan["estimate"] <= B.octree_ceiling_cells()
    assert plan["fine_levels"] > plan["broad_levels"]
    assert "two cell sizes" in said


def test_the_broad_side_is_not_given_a_cell_it_does_not_need():
    """Holding the broad passages at what they need is where the budget for the thin ones comes
    from. 12 across a 2.11 mm channel is 0.176 mm; a slot-sized cell over that same 38,813 mm2 is
    what made the single-scale plan unaffordable."""
    scales = {**CUSTOMER, "fine": {**CUSTOMER["fine"], "area_m2": CUSTOMER["fine"]["area_m2"] / 10}}
    plan, _said = _plan(scales=scales, limit=700_000_000)
    assert plan["broad_across"] <= 12, "it spent more on the broad side than was asked for"
    assert plan["broad_cell_m"] >= CUSTOMER["broad"]["width_m"] / 12


def test_the_thin_side_is_resolved_as_far_as_the_budget_reaches():
    """Ties go to the plan that resolves the THIN side furthest, since that is the reason to split
    the sizes at all."""
    scales = {**CUSTOMER, "fine": {**CUSTOMER["fine"], "area_m2": CUSTOMER["fine"]["area_m2"] / 50}}
    generous, _ = _plan(scales=scales, limit=700_000_000)
    assert generous is not None
    mean = {**scales, "fine": {**scales["fine"], "area_m2": scales["fine"]["area_m2"] * 8}}
    tight, _ = _plan(scales=mean, limit=700_000_000)
    if tight is not None:
        assert generous["fine_across"] >= tight["fine_across"], (
            "a part with less thin area to cover should get at least as fine a thin cell")


def test_a_split_carrying_a_nonsense_number_is_refused_not_guessed():
    for broken in ({"width_m": 0.0, "area_m2": 1.0}, {"width_m": 1e-4, "area_m2": 0.0}):
        plan, said = _plan(scales={**CUSTOMER, "fine": broken})
        assert plan is None and "not positive" in said
