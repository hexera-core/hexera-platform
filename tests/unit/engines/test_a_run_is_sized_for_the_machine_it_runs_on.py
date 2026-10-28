# Responsibility: Verify a run's resolution is chosen against the refinement BAND and the octree
# depth, that the depth is varied, and that a part which cannot fit is refused rather than started.
# Boundaries: arithmetic on measured numbers. No mesher, no geometry, no OpenFOAM.
#
# WHY THIS FILE EXISTS. The old pre-flight estimate prices the whole block at the finest cell. It
# divides the background count by eight per octree level and multiplies its own refinement factor
# by eight, so its answer is IDENTICAL at every depth - and depth is the one knob that buys a
# coarse background under a fine coolant surface. On a customer cold plate that collapsed draft,
# standard, fine and max to the same five cells across, and then, once the narrowest passage was
# read correctly as 98 um rather than 2.652 mm, it asked for about 64 billion cells and the floor
# branch meshed anyway.
import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B

#: The Corintis cold plate, measured: block, coolant wetted area, and both readings of its
#: narrowest passage - what the sizing step used to see, and what is actually there.
LO = (-0.06157654, 0.02011018, -0.008590401)
HI = (0.1159235, 0.06194618, 0.0924096)
AREA_M2 = 60588.1e-6
MANIFOLD_M = 2.652e-3
MICRO_M = 0.098e-3


def _fit(across, levels=B.LEVELS, narrowest=MANIFOLD_M):
    return B.band_estimate_cells(block_lo=LO, block_hi=HI, narrowest_m=narrowest,
                                 cells_across=across, wetted_area_m2=AREA_M2, levels=levels)


# the band

def test_the_band_gets_deeper_as_the_octree_span_widens():
    """The band is a count of GRADING SHELLS, so the span sets it, not the cell size.

    This test used to assert the opposite - that the band deepens as the cells get finer - from a
    fit on two readings 2.6x apart in cell size that BOTH sat at a span of 3. With the span fixed,
    a finer cell is a wider span, so that fit could not tell the two apart; extrapolated to the
    0.044007 mm cell a real customer part needs it predicted 20.0 cells of depth where the run
    measured 6.9, and it refused meshes that would have built.

    MEASURED with the cell size HELD FIXED, which is what settled it: same part, same thin surface,
    same 0.044007 mm cell, only the background moved. Span 5 gave 204,543,984 octree boxes and span
    3 gave 102,152,624 - 2.4x, at identical resolution.
    """
    # 11.86 is the LARGEST of the three span-3 readings, not their mean: under-predicting sizes a
    # machine for less than the run needs, and the mean under-predicted the 17,447,384-cell
    # calibration run below by 3.8%. See DEPTH_AT_FITTED_SPAN.
    assert B.band_depth(3) == pytest.approx(11.86, rel=0.02)
    assert B.band_depth(5) == pytest.approx(11.86 + 2 * B.DEPTH_PER_LEVEL, rel=0.02)
    assert B.band_depth(3) >= 11.86, "the anchor must not slip below the reading it pins"
    assert B.band_depth(5) > B.band_depth(3) > B.band_depth(1)
    # and it never claims a band thinner than one cell either side of the surface
    assert B.band_depth(0) == pytest.approx(B.MIN_BAND_DEPTH)
    assert B.band_depth(-4) == pytest.approx(B.MIN_BAND_DEPTH)


def test_the_two_runs_that_measured_the_depth_are_predicted_by_it():
    """The model is checked as whole meshes, not as depths, because a mesh is what was measured.

    The thin surface and the coolant surface sit at different spans in both runs, so this exercises
    the span term twice per run at two different cell sizes.
    """
    area_fine, area_broad = 21712.67e-6, 38813.50e-6
    cell_fine, cell_broad = 4.4007e-05, 1.7603e-04
    block = (HI[0] - LO[0]) * (HI[1] - LO[1]) * (HI[2] - LO[2])
    for background, span_fine, span_broad, measured in ((3.5206e-4, 3, 1, 102_152_624),
                                                        (1.40822e-3, 5, 3, 204_543_984)):
        got = (area_fine * B.band_depth(span_fine) / cell_fine ** 2
               + area_broad * B.band_depth(span_broad) / cell_broad ** 2
               + block / background ** 3)
        # HIGH ON PURPOSE AND NEVER LOW. A prediction under the mesh that was built is the one
        # that gets a run killed hours in; a prediction over it refuses a run that might have
        # built. The estimate is held to a factor of two, and these sit at 1.49x and 1.27x.
        assert got >= measured, (
            f"span {span_fine}: predicted {got:,.0f} UNDER the {measured:,} that was built")
        assert got / measured < 2.0, (
            f"span {span_fine}: predicted {got:,.0f} against {measured:,} measured")


def test_the_estimate_tells_two_depths_apart():
    """THE WHOLE POINT, and the DIRECTION is the correction.

    This test used to assert the count FALLS as the octree deepens, on the belief that depth is
    free at the coolant surface and only divides the background. Measured, it is the other way
    round on a part where the band dominates: the extra levels are extra grading shells, and on
    this plate the band is 1.96M cells against 9,818 of background, so a deeper octree costs more.
    What matters either way is that the estimate MOVES with depth - the old whole-block estimate
    could not tell any two depths apart, which is why this file exists.
    """
    counts = [_fit(5, levels=n) for n in B.LEVELS_LADDER]
    assert len(set(counts)) == len(counts), (
        f"the estimate still cannot tell the depths apart: {counts}")
    assert counts == sorted(counts), (
        f"on a band-dominated part a deeper octree has to cost MORE, not less: {counts}")


def test_the_old_estimate_can_never_prefer_a_deeper_octree():
    """Stated as a test so the reason this change exists cannot quietly stop being true.

    IT IS WORSE THAN BEING BLIND. In the continuum the whole-block estimate is exactly invariant -
    it divides the background count by eight per level and multiplies its own refinement factor by
    eight - but the count is a ceil() per axis, and the rounding grows with the background cell.
    So on the customer plate it goes 6,451,200 at three and four levels, 8,110,080 at five and
    11,796,480 at six: deepening the octree makes it look MORE expensive. A ladder reading that
    number would walk away from the one knob that buys a coarse background under a fine coolant
    surface, which is the knob the whole refinement exists to provide.
    """
    old = [B.estimate_cells(block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M,
                            cells_across=5, levels=n) for n in B.LEVELS_LADDER]
    assert old == sorted(old), f"the whole-block estimate is no longer monotone in depth: {old}"
    assert old[-1] >= old[0], (
        "the whole-block estimate now rewards a deeper octree, so this change is moot and the "
        "ladder could read it instead")
    # The band estimate rises with depth too, but for a reason that is measured rather than an
    # artifact of ceil() rounding: the grading shells are real cells. What separates the two is
    # that the band estimate's spread is the cost of the shells (2.4x, measured on two runs of one
    # part at one resolution) where the whole-block estimate's is the rounding of a count.
    band = [_fit(5, levels=n) for n in B.LEVELS_LADDER]
    assert len(set(band)) == len(band), "the band estimate must tell the depths apart"


def test_a_band_estimate_needs_the_wetted_area():
    with pytest.raises(ValueError):
        B.band_estimate_cells(block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M,
                              cells_across=5, wetted_area_m2=0.0)


# what the ladder picks

def test_the_fidelities_stop_collapsing_onto_each_other():
    """They used to come out at the same four cells across whatever was asked for.

    This asserted `across == 8` for max, which the optimistic depth model reached and the measured
    one does not: max now lands at 7 across inside an 8M limit (6,291,608 cells against 8,222,000
    for 8 across). The point of the file was never the number 8 - it was that asking for more
    detail gets more detail - so that is what is asserted.
    """
    got = {}
    for name, asked in sorted(B.CELLS_ACROSS_BY_FIDELITY.items(), key=lambda kv: kv[1]):
        across, levels, est, _said = B.plan_for_budget(
            block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
            limit=8_000_000, asked_across=asked)
        assert est <= 8_000_000, f"{name} came out over the limit"
        assert levels in B.LEVELS_LADDER
        got[name] = across
    assert got["max"] > got["standard"] > got["draft"], (
        f"the fidelities collapsed again: {got}")
    assert got["draft"] >= B.FLOOR_CELLS_ACROSS


def test_the_resolution_is_held_and_the_cheapest_depth_taken():
    """Resolution is what a customer asked for; depth is ours to choose, so depth moves first.

    The ladder walks the depths in order and takes the first that fits, so with the cost now rising
    with depth it takes the SHALLOWEST that fits - which is the cheap one, measured. This test used
    to assert the opposite preference, from the belief that depth was free.
    """
    across, levels, est, _said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=8_000_000, asked_across=8)
    assert est <= 8_000_000
    cheaper = [n for n in B.LEVELS_LADDER
               if n < levels and _fit(across, levels=n) <= 8_000_000]
    assert not cheaper, (
        f"it chose {levels} octree levels when {cheaper} also fitted and cost less")


def test_a_tighter_limit_gives_up_resolution_and_says_so():
    """A limit this part cannot have at 8 across: the resolution steps down and the run says it did.

    The limit used to be 2,000,000 here, which the measured depth model turns into a REFUSAL on
    this part (4 cells across at 3 levels is about 2,049,690 - 2.5% over), so it no longer
    exercises the step-down it was written for. 4,000,000 does.
    """
    coarse_across, _l, est, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=4_000_000, asked_across=8)
    assert coarse_across < 8, "the resolution was not given up although the limit was tighter"
    assert coarse_across >= B.FLOOR_CELLS_ACROSS
    assert est <= 4_000_000
    assert said, "a run that did not get what it asked for said nothing about it"


def test_a_refusal_is_priced_at_the_cheapest_configuration():
    """A refusal states the BEST case, because its number answers "could this ever be meshed here".

    This branch read LEVELS_LADDER[-1] while depth was believed free, which was then the cheapest
    option and is the dearest now it is measured: on this part it priced the refusal at 4,601,986
    cells where the cheapest configuration is 2,049,690, overstating it 2.2x and refusing by a
    margin that was the planner's own choice rather than the part's.
    """
    across, levels, est, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=2_000_000, asked_across=8)
    assert across == B.FLOOR_CELLS_ACROSS
    assert levels == B.LEVELS_LADDER[0], (
        f"the refusal was priced at {levels} octree levels, not the cheapest "
        f"{B.LEVELS_LADDER[0]}")
    assert est == min(_fit(B.FLOOR_CELLS_ACROSS, levels=n) for n in B.LEVELS_LADDER), (
        "a cheaper configuration than the one quoted exists")
    assert "does not fit" in said


def test_the_floor_is_never_crossed():
    for limit in (1, 1000, 100_000):
        across, _l, _e, _s = B.plan_for_budget(
            block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
            limit=limit, asked_across=8)
        assert across == B.FLOOR_CELLS_ACROSS


# the refusal

def test_a_part_that_cannot_fit_says_so_with_the_number():
    """The microchannels at the engine's own floor. Starting this spends hours and ends in a kill."""
    across, levels, est, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MICRO_M, wetted_area_m2=AREA_M2,
        limit=8_000_000, asked_across=8)
    assert across == B.FLOOR_CELLS_ACROSS
    # THE CHEAPEST, NOT THE DEEPEST. This asserted LEVELS_LADDER[-1] - "it gave up before spending
    # every octree level" - which was right while depth was believed to be free and is backwards
    # now that the grading shells are measured. A refusal quotes the best case available.
    assert levels == B.LEVELS_LADDER[0], "the refusal was not priced at the cheapest octree"
    assert est > 1_000_000_000, f"the microchannels came out at only {est:,} cells"
    assert "does not fit" in said
    assert "8,000,000" in said, f"the refusal does not name the limit it hit: {said}"


def test_the_refusal_is_not_reached_for_a_part_that_does_fit():
    _a, _l, est, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=8_000_000, asked_across=8)
    assert "does not fit" not in said
    assert est <= 8_000_000


def test_asking_for_the_default_at_the_default_depth_says_nothing():
    """A run that got exactly what it asked for does not explain itself."""
    _a, _l, _e, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=10_000_000_000, asked_across=8)
    assert said == ""


# against what was actually built

@pytest.mark.parametrize("across,delivered", [(13, 17_447_384)])
def test_the_estimate_brackets_the_mesh_that_was_built(across, delivered):
    """The calibration run on mesh-bench: 17,447,384 cells at 13 across, 0.204 mm.

    The estimate carries ESTIMATE_MARGIN, so it should sit ABOVE the delivered count and within a
    factor of two of it. Below would mean sizing a machine for less than the run needs.
    """
    est = _fit(across)
    assert est >= delivered, f"the estimate {est:,} is under the delivered {delivered:,}"
    assert est <= 2 * delivered, f"the estimate {est:,} is more than twice {delivered:,}"
