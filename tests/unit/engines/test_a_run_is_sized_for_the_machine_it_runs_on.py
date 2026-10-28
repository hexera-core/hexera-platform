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

def test_the_band_gets_deeper_as_the_cells_get_finer():
    """Measured on one part at two resolutions: 8.56 cells deep at 0.53 mm, 11.86 at 0.204 mm.

    A single depth under-predicts at fine resolutions, which is the direction that gets a run
    killed hours in rather than refused up front.
    """
    assert B.band_depth(0.5304e-3) == pytest.approx(8.56, rel=0.05)
    assert B.band_depth(0.2040e-3) == pytest.approx(11.86, rel=0.05)
    assert B.band_depth(0.02e-3) > B.band_depth(0.5e-3)


def test_the_estimate_falls_as_the_octree_deepens():
    """THE WHOLE POINT. The old estimate cannot tell these apart; this one must."""
    counts = [_fit(5, levels=n) for n in B.LEVELS_LADDER]
    assert counts == sorted(counts, reverse=True), (
        f"deepening the octree did not lower the count: {counts}")
    assert counts[0] > counts[-1]


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
    band = [_fit(5, levels=n) for n in B.LEVELS_LADDER]
    assert band[-1] < band[0], "and the band estimate must still fall where that one rises"


def test_a_band_estimate_needs_the_wetted_area():
    with pytest.raises(ValueError):
        B.band_estimate_cells(block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M,
                              cells_across=5, wetted_area_m2=0.0)


# what the ladder picks

def test_max_fidelity_is_actually_delivered_on_the_customer_part():
    """It used to collapse to five cells across at every fidelity. Eight is what max asks for."""
    across, levels, est, _said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=8_000_000, asked_across=8)
    assert across == 8, f"max fidelity came out at {across} cells across"
    assert est <= 8_000_000
    assert levels in B.LEVELS_LADDER


def test_the_resolution_is_held_and_the_depth_moved_first():
    """Depth costs nothing at the coolant surface, so it is spent before resolution is given up."""
    across, levels, _est, _said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=8_000_000, asked_across=8)
    shallower = _fit(across, levels=B.LEVELS_LADDER[0])
    if levels != B.LEVELS_LADDER[0]:
        assert shallower > 8_000_000, (
            "the depth was raised although the shallowest one already fitted")


def test_a_tighter_limit_gives_up_resolution_only_after_depth():
    coarse_across, _l, est, said = B.plan_for_budget(
        block_lo=LO, block_hi=HI, narrowest_m=MANIFOLD_M, wetted_area_m2=AREA_M2,
        limit=2_000_000, asked_across=8)
    assert coarse_across <= 8 and est <= 2_000_000
    assert said, "a run that did not get what it asked for said nothing about it"


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
    assert levels == B.LEVELS_LADDER[-1], "it gave up before spending every octree level"
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
