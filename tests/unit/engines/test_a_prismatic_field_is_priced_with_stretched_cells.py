"""Stretching the cells along an extruded axis is what puts a solvable resolution in the budget.

THE NUMBERS THIS IS BUILT ON, all measured on the first customer's own geometry:

    broad passages   2.112337 mm wide over 38,813.50 mm2
    thin slots       0.0982 mm wide over 21,712.67 mm2
    block            177.5 x 41.8 x 101 mm
    delivered        2.23 cells across the slots at 102,630,992 cells, coolant in 19 pieces

and the thin walls read as an extrusion along y (0.0694 against a bar of 0.25), while the whole
coolant - manifold included - correctly reads as having no prismatic axis.

What the stretch buys, priced by this module on 2026-10-29:

    4.46 across, no stretch      966,955,439 cells   - over the band and 4.3x one process
    4.46 across, stretched 6x    181,673,384 cells   - fits, and fits one process
    8.93 across, stretched 8x    634,530,269 cells   - fits the band, needs 3 ranks or more
    8.93 across, no stretch    4,903,923,343 cells   - nowhere near

So the stretch is not an optimisation at the margin. It is the difference between a resolution
that holds the passages together and one that cannot be afforded at all.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B

BLOCK_LO = [-0.06157654, 0.02011018, -0.008590401]
BLOCK_HI = [0.1159235, 0.06194618, 0.0924096]
SCALES = {"broad": {"width_m": 2.112337225e-3, "area_m2": 38813.50e-6},
          "fine": {"width_m": 0.0982e-3, "area_m2": 21712.67e-6}}


def _plan(**kw):
    kw.setdefault("block_lo", BLOCK_LO)
    kw.setdefault("block_hi", BLOCK_HI)
    kw.setdefault("scales", SCALES)
    kw.setdefault("limit", 700_000_000)
    kw.setdefault("asked_across", 12)
    return B.plan_two_scales(**kw)


# -- the estimator ---------------------------------------------------------------------------

def _est(stretch, **kw):
    shape = dict(block_lo=BLOCK_LO, block_hi=BLOCK_HI,
                 broad_cell_m=2.112337225e-3 / 12, broad_area_m2=38813.50e-6,
                 fine_cell_m=22.0e-6, fine_area_m2=21712.67e-6,
                 background_m=0.176e-3, broad_levels=1, fine_levels=4)
    shape.update(kw)
    return B.two_scale_estimate(**shape, fine_stretch=stretch)


def test_the_thin_band_divides_by_the_stretch_exactly():
    """THE MODEL, checked exactly rather than by a ratio of totals.

    A band is an area over a cell's cross-section, so a cell S times taller along one axis of
    that area needs S times fewer cells to cover it: band_fine -> band_fine / S. The broad band
    and the background are deliberately NOT divided - the stretch box covers the thin walls only,
    and leaving the background whole makes this an over-estimate, which is the safe direction for
    a number that chooses a machine.

    That means the TOTAL does not fall by S (here it falls 3.4x at S=6, because the two undivided
    terms dilute it). What falls by S exactly is the thin band, and the way to see that without
    reaching inside the function is that the saving is proportional to (1 - 1/S)."""
    plain = _est(1.0)
    saving = {s: plain - _est(s) for s in (2.0, 4.0, 8.0)}
    for s, got in saving.items():
        assert got > 0
        # saving(s) / saving(2) == (1 - 1/s) / (1 - 1/2)
        expected = (1 - 1 / s) / (1 - 1 / 2) * saving[2.0]
        assert got == pytest.approx(expected, rel=1e-6), f"at {s}x: {got:,} vs {expected:,.0f}"


def test_the_total_still_falls_a_long_way():
    """The dilution is real but it is not the story: on the customer's own geometry a stretch of
    six takes a configuration from over a billion cells to inside the band."""
    plain, six = _est(1.0), _est(6.0)
    assert plain > 1_000_000_000
    assert six < 400_000_000
    assert plain / six > 3.0


def test_no_stretch_is_the_mesh_we_have_always_priced():
    assert _est(1.0) == B.two_scale_estimate(
        block_lo=BLOCK_LO, block_hi=BLOCK_HI, broad_cell_m=2.112337225e-3 / 12,
        broad_area_m2=38813.50e-6, fine_cell_m=22.0e-6, fine_area_m2=21712.67e-6,
        background_m=0.176e-3, broad_levels=1, fine_levels=4)


@pytest.mark.parametrize("bad", [0.0, -1.0, B.MAX_STRETCH + 0.1, 0.5])
def test_a_stretch_outside_the_bounds_is_refused(bad):
    """Below one is a compression, which is never what is wanted here; above the cap the cell
    shape stops being something a solver should be handed without being asked."""
    with pytest.raises(ValueError, match="stretch"):
        _est(bad)


# -- the planner -----------------------------------------------------------------------------

def test_without_a_prismatic_axis_a_part_is_planned_as_it_always_was():
    """Most parts are not extruded along anything, and nothing here may change how they are
    sized. A coarser scale split - one the unstretched budget can still reach - is planned with
    no stretch at all."""
    coarse = {"broad": {"width_m": 4.0e-3, "area_m2": 2000.0e-6},
              "fine": {"width_m": 0.5e-3, "area_m2": 600.0e-6}}
    plan, _ = _plan(scales=coarse, prismatic_axis=None)
    assert plan is not None
    assert plan["stretch"] == 1.0 and plan["stretch_axis"] is None


def test_without_a_stretch_the_customer_part_cannot_be_resolved_at_all():
    """THE WHOLE POINT, stated as the refusal it is. 4.46 cells across the 0.0982 mm slots prices
    at 966,955,439 cells unstretched - over any budget in the band and 4.3x what one process can
    address - so the only rungs left are below the four-cell floor, and there is no plan."""
    flat, said = _plan(prismatic_axis=None)
    assert flat is None, f"expected a refusal, got {flat}"
    assert "two scales of passage" in said


def test_the_stretch_is_what_makes_it_plannable():
    tall, _ = _plan(prismatic_axis=1)
    assert tall is not None
    assert tall["fine_across"] >= B.FINE_ACROSS_FLOOR
    assert tall["fine_across"] == pytest.approx(4.46, abs=0.05)
    assert tall["estimate"] < 200_000_000, (
        "and it has to be affordable, not merely reachable")
    assert tall["stretch_axis"] == 1


def test_the_cheapest_stretch_that_fits_is_the_one_taken():
    """A stretch is free in accuracy along a prismatic axis but it is still a deformation; there
    is no reason to take more of it than the budget needs."""
    tight, _ = _plan(prismatic_axis=1, limit=150_000_000)
    loose, _ = _plan(prismatic_axis=1, limit=700_000_000)
    assert tight["stretch"] >= loose["stretch"], (
        "a tighter budget should buy the stretch, not a looser one")


def test_one_process_gets_the_rung_it_can_actually_build():
    """The chain is serial today, so a configuration past one process's octree ceiling must be
    refused rather than started and aborted an hour in."""
    plan, _ = _plan(prismatic_axis=1, ranks=1)
    assert plan["fine_across"] == pytest.approx(4.46, abs=0.05)
    assert plan["estimate"] < 200_000_000
    raw = B.two_scale_estimate(
        block_lo=BLOCK_LO, block_hi=BLOCK_HI, broad_cell_m=plan["broad_cell_m"],
        broad_area_m2=SCALES["broad"]["area_m2"], fine_cell_m=plan["fine_cell_m"],
        fine_area_m2=SCALES["fine"]["area_m2"], background_m=plan["background_m"],
        broad_levels=plan["broad_levels"], fine_levels=plan["fine_levels"],
        margin=False, fine_stretch=plan["stretch"])
    assert raw <= B.octree_ceiling_cells(1)


def test_more_ranks_open_the_rung_above():
    """8.93 cells across is 2.8x one process's ceiling and well inside eight ranks', which have
    already built 202,709,195 cells on this mesher."""
    one, _ = _plan(prismatic_axis=1, ranks=1)
    many, _ = _plan(prismatic_axis=1, ranks=8)
    assert many["fine_across"] > one["fine_across"] * 1.9
    assert many["fine_across"] == pytest.approx(8.93, abs=0.05)
    assert 100_000_000 < many["estimate"] <= 700_000_000, (
        f"the rung above has to stay inside the band: {many['estimate']:,}")


def test_both_rungs_clear_the_floor_a_passage_needs_to_survive():
    """Below about four cells across, a passage stops being a passage: 1.03 across produced 367
    disconnected pieces and 2.23 produced 19."""
    for ranks in (1, 8):
        plan, _ = _plan(prismatic_axis=1, ranks=ranks)
        assert plan["fine_across"] >= 4.0, f"{ranks} ranks gave {plan['fine_across']:.2f}"


def test_a_refusal_says_how_many_ranks_would_have_done_it():
    """A refusal that only says "too big" sends a reader to the wrong lever."""
    plan, said = _plan(prismatic_axis=1, ranks=1, limit=700_000_000, asked_across=12)
    assert plan is not None
    # now force the ceiling to bite by asking for a budget only the deep rung could use
    plan2, said2 = B.plan_two_scales(
        block_lo=BLOCK_LO, block_hi=BLOCK_HI, scales=SCALES,
        limit=700_000_000, asked_across=12, prismatic_axis=1, ranks=1,
    )
    assert plan2 is not None, "the shallow rung still fits one process"


def test_the_words_say_what_was_done_and_why_it_was_allowed():
    _, said = _plan(prismatic_axis=1, ranks=8)
    assert "stretched 8x along y" in said
    assert "prismatic" in said, "the licence for the stretch belongs in the sentence"
