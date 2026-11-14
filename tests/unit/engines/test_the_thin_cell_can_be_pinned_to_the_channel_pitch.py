"""The thin cell can be pinned to the channel pitch, and nothing the ladder reaches divides it.

WHY THIS EXISTS. In `plan_two_scales` the thin cell is never chosen. It is the broad cell halved
`extra` times and the broad cell is the broad passage width over an integer, so the reachable thin
cells are `broad_width / (n * 2**extra)` and NOTHING about the thin passages enters except an
acceptance window. On the first customer cold plate that set is

    13.2021  14.6690  16.5026  18.8602  22.0035  24.0038  26.4042  29.3380
    33.0053  37.7203  44.0070  48.0077   (um)

and the pitch of its channel array measures 201.2785 um. Not one of the twelve divides it; the
cell the planner picks, 22.0035 um, puts 9.1476 cells on a pitch. A pitch that is not a whole
number of cells is a pitch whose channels do not all snap to the grid the same way, and the
conjugate interface lies on octree cell faces, so that snapping IS the channel width the solver
will see.

WHAT IS AND IS NOT CLAIMED HERE. Nothing in this file says a commensurate cell makes a better
mesh. `engines/staircase_error` is the standing warning against arguing a sizing change from a
table - on its corpus seven cells across came out fourteen times worse than six on one part, and
the error grew under refinement on another. These tests pin the GAP and the mechanism for
measuring it on a real part. The rule, if it becomes one, needs the run.
"""
import pytest

import meshpipeline.engines.determinism as det
from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.engines.cfmesh_conjugate import plan as PL
from meshpipeline.settings.env import ConfigurationError

#: The customer cold plate, as cad/scales measured it on 2026-10-28 - the same fixture as
#: test_a_two_scale_part_is_sized_at_two_cell_sizes, deliberately, so the two cannot drift.
LO = (-0.06157654, 0.02011018, -0.008590401)
HI = (0.1159235, 0.06194618, 0.0924096)
CUSTOMER = {
    "split_m": 4.883e-4,
    "broad": {"width_m": 2.1123723e-3, "area_m2": 38813.50e-6},
    "fine": {"width_m": 0.0982e-3, "area_m2": 21712.67e-6},
}

#: The pitch of that plate's channel array, measured 2026-11-14 on the staged fine coolant surface
#: the product itself writes (`constant/triSurface/fluid_fine.stl` of job b6762d5c): 12,409
#: channel-and-fin pairs taken one ray at a time so a channel is never paired with a fin from
#: somewhere else, median 201.2785 um, CV 5.03%. Independently within 0.008% of the 201.26253 um
#: measured on the delivered mesh by a different method.
PITCH_M = 201.2785e-6

#: What the planner chooses for this part at the limit job b6762d5c ran under.
BUILT_CELL_M = 22.0035e-6
RUN_LIMIT = 400_000_000


def _reachable() -> list[float]:
    """Every thin cell `plan_two_scales` can construct on this part, in metres."""
    broad_w = CUSTOMER["broad"]["width_m"]
    out = {round(broad_w / (n * 2 ** e), 15)
           for n in range(B.FLOOR_CELLS_ACROSS, B.BROAD_ACROSS_CAP + 1)
           for e in B.FINE_EXTRA_LEVELS}
    return sorted(out)


def _pinned(cell_m, limit=RUN_LIMIT, ranks=8, axis=1):
    return B.two_scales_at_fine_cell(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=limit,
                                     fine_cell_m=cell_m, prismatic_axis=axis, ranks=ranks)


# ---------------------------------------------------------------- the gap, pinned

def test_not_one_cell_the_ladder_reaches_puts_an_even_count_on_the_channel_pitch():
    """The defect itself. If this ever fails, the ladder has changed and the pitch argument below
    needs re-deriving rather than re-asserting.

    WHY EVEN, which the first version of this test screened for without saying so. On this part
    channel ~= fin ~= pitch/2, so an EVEN number of cells per pitch is what divides evenly into
    both and puts the channel on exactly k/2 cells with no rounding. An odd count lands the channel
    on x.5 cells and is no better than an incommensurate one.

    It also checks the weaker claim - that nothing reaches a whole number at all - and reports it
    separately, because a reachable odd divisor would be a different and more interesting finding
    than the even ones this test is named for.
    """
    even, whole = [], []
    for cell in _reachable():
        k = PITCH_M / cell
        if not (4.0 <= k <= 16.0) or abs(k - round(k)) >= 1e-3:
            continue
        whole.append((cell, k))
        if round(k) % 2 == 0:
            even.append((cell, k))
    assert not even, (
        "a reachable cell now puts an even count on the pitch, so the planner can be commensurate "
        f"without pinning: {even}")
    assert not whole, (
        f"a reachable cell divides the pitch a whole number of times, though oddly: {whole}")


def test_the_cell_the_planner_picks_cuts_the_pitch_in_nine_and_a_bit():
    """The run that happened, and the number that makes it the wrong lattice."""
    got, said = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=RUN_LIMIT,
                                  asked_across=B.CELLS_ACROSS_BY_FIDELITY["standard"],
                                  prismatic_axis=1, ranks=8)
    assert got is not None, said
    assert got["fine_cell_m"] == pytest.approx(BUILT_CELL_M, rel=1e-4)
    assert got["broad_across"] == 12
    assert (got["broad_levels"], got["fine_levels"]) == (1, 4)
    k = PITCH_M / got["fine_cell_m"]
    assert k == pytest.approx(9.1476, abs=5e-4), f"{k} cells per pitch"


def test_pinning_is_off_unless_the_setting_says_otherwise():
    """Every part that plans today must keep planning exactly as it did."""
    assert det.forced_fine_cell() == 0.0
    assert det.CONJUGATE_FINE_CELL_M == ""


def test_the_planner_is_untouched_when_nothing_is_pinned(monkeypatch):
    monkeypatch.setattr(det,"CONJUGATE_FINE_CELL_M", "")
    got, _ = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=RUN_LIMIT,
                               asked_across=B.CELLS_ACROSS_BY_FIDELITY["standard"],
                               prismatic_axis=1, ranks=8)
    assert got["fine_cell_m"] == pytest.approx(BUILT_CELL_M, rel=1e-4)


# ---------------------------------------------------------------- what pinning delivers

def test_a_pinned_cell_comes_back_exactly_and_divides_the_pitch():
    cell = PITCH_M / 10.0
    got, said = _pinned(cell, limit=500_000_000)
    assert got is not None, said
    assert got["fine_cell_m"] == cell, "the pinned cell must not be rounded onto the old ladder"
    assert PITCH_M / got["fine_cell_m"] == pytest.approx(10.0, abs=1e-9)
    assert "FORCED" in said


def test_the_background_is_derived_from_the_pinned_cell_and_not_the_other_way_round():
    """The whole inversion. `background = cell * 2**fine_levels` and the broad cell falls out of
    it, which is what makes a cell off the ladder reachable at all."""
    cell = PITCH_M / 10.0
    got, said = _pinned(cell, limit=500_000_000)
    assert got is not None, said
    assert got["background_m"] == pytest.approx(cell * 2 ** got["fine_levels"], rel=1e-12)
    extra = got["fine_levels"] - got["broad_levels"]
    assert got["broad_cell_m"] == pytest.approx(cell * 2 ** extra, rel=1e-12)


def test_a_pinned_cell_may_resolve_the_broad_side_finer_than_the_search_would_ask():
    """BROAD_ACROSS_CAP bounds how fine the SEARCH may ask for. Here the broad resolution is an
    OUTCOME of the pinned cell, so capping it would refuse a plan for being better than asked on
    the broad side. It is reported instead, and the limit is what stops a runaway."""
    cell = PITCH_M / 10.0
    got, said = _pinned(cell, limit=500_000_000)
    assert got is not None, said
    assert got["broad_across"] > B.BROAD_ACROSS_CAP
    assert got["broad_across"] >= B.FLOOR_CELLS_ACROSS
    assert f"{got['broad_across']:.2f} across" in said


# ---------------------------------------------------------------- what pinning refuses

def test_a_pin_just_under_the_planners_floor_is_taken_and_says_so_in_words():
    """pitch/8 on this part is 25.16 um. Against the channel's nominal width - pitch/2 - that is
    4.0000 cells across exactly; against `scales.fine.width_m`, the 1st percentile of a
    radius-derived width, it is 3.90, and that 2.5% of conservatism is the whole of what puts it
    under a floor of 4.

    So it is taken, and the shortfall travels in the sentence. A mesh built there must never read
    as a resolution the product chose for the part."""
    cell = PITCH_M / 8.0
    assert (PITCH_M / 2.0) / cell == pytest.approx(4.0, abs=1e-9), "pitch/2 over pitch/8 is 4"
    got, said = _pinned(cell, limit=900_000_000)
    assert got is not None, said
    assert got["fine_across"] < B.FINE_ACROSS_FLOOR
    assert got["under_planner_floor"] is True
    assert f"UNDER the {B.FINE_ACROSS_FLOOR}-cell floor" in said
    assert "pinned anyway" in said


def test_a_pin_below_the_band_is_refused_outright():
    """Under PINNED_ACROSS_FLOOR there is nothing worth measuring: the corpus put 3 cells across
    at 32.30% volume error and 2 across at 27.54% on a single prismatic channel."""
    cell = CUSTOMER["fine"]["width_m"] / 3.0
    got, said = _pinned(cell, limit=900_000_000)
    assert got is None
    assert f"{B.PINNED_ACROSS_FLOOR} to {B.FINE_ACROSS_CAP} cells" in said


def test_a_pin_finer_than_the_cap_is_refused():
    cell = CUSTOMER["fine"]["width_m"] / (B.FINE_ACROSS_CAP + 2)
    got, said = _pinned(cell, limit=4_000_000_000)
    assert got is None
    assert f"{B.PINNED_ACROSS_FLOOR} to {B.FINE_ACROSS_CAP} cells" in said


def test_the_planners_own_floor_is_not_moved_by_any_of_this():
    """The band is reachable ONLY by a pin. `plan_two_scales` must still hold 4 cells across."""
    assert B.FINE_ACROSS_FLOOR == 4
    assert B.PINNED_ACROSS_FLOOR < B.FINE_ACROSS_FLOOR
    got, _ = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=900_000_000,
                               asked_across=B.CELLS_ACROSS_BY_FIDELITY["standard"],
                               prismatic_axis=1, ranks=8)
    assert got is None or got["fine_across"] >= B.FINE_ACROSS_FLOOR


def test_pitch_eight_reaches_the_background_the_fast_mesh_was_built_at():
    """cbox8 was built by hand on 2026-11-08 at `maxCellSize 402.525` with the coolant surface four
    octree levels under it - 270,424,307 cells, Mesh OK on all three regions, 65.4 minutes end to
    end with layers. The pinned planner has to arrive at that same configuration from the pitch
    alone, or it is not reproducing the mesh this run exists to compare against.

    AT THE LIMIT THE PRODUCT RUN WILL USE, which matters: `STRETCH_LADDER` is searched for the
    CHEAPEST stretch that fits, so a larger budget buys a SMALLER stretch and a different winner.
    At 900,000,000 this part plans a 805 um background instead."""
    got, said = _pinned(PITCH_M / 8.0, limit=RUN_LIMIT)
    assert got is not None, said
    assert got["background_m"] * 1e6 == pytest.approx(402.525, rel=2e-4)
    assert got["fine_levels"] == 4
    assert got["stretch"] == 3
    assert got["raw"] == pytest.approx(270_424_307, rel=0.25), (
        f"{got['raw']:,} against the 270,424,307 cells that mesh actually has")


def test_pitch_eight_fits_the_budget_the_last_run_already_had():
    """No setting has to move for it. The run it is compared against ran at this same limit, so
    the two differ in the lattice and in nothing else."""
    got, said = _pinned(PITCH_M / 8.0, limit=RUN_LIMIT)
    assert got is not None, said
    assert got["estimate"] <= RUN_LIMIT


def test_the_refusal_names_the_gate_that_was_actually_binding():
    """A refusal is read by someone about to change a setting. At the limit job b6762d5c ran
    under, every octree that reaches pitch/10 clears the broad floor comfortably and is refused on
    the CELL BUDGET - so the sentence has to say the cell count and the limit, and must not say
    the broad passages need more cells across."""
    got, said = _pinned(PITCH_M / 10.0, limit=RUN_LIMIT)
    assert got is None
    assert f"{RUN_LIMIT:,}" in said
    assert "cells against a limit of" in said
    assert "cells across the broad" not in said


def test_a_pin_in_micrometres_refuses_rather_than_meshing_a_metre_wide_cell(monkeypatch):
    monkeypatch.setattr(det,"CONJUGATE_FINE_CELL_M", "20.1")
    with pytest.raises(ConfigurationError) as got:
        det.forced_fine_cell()
    assert "METRES" in str(got.value)


@pytest.mark.parametrize("bad", ["banana", "0", "-2e-5", "1e3"])
def test_a_malformed_pin_is_refused_and_never_silently_dropped(monkeypatch, bad):
    """A knob that is quietly ignored makes a run report a cell size nobody chose."""
    monkeypatch.setattr(det,"CONJUGATE_FINE_CELL_M", bad)
    with pytest.raises(ConfigurationError):
        det.forced_fine_cell()


# ------------------------------------------------- what the MESHER gets, not what the planner said

def test_the_pinned_cell_is_what_the_plan_actually_delivers():
    """THE TEST THAT WAS MISSING, and the change was broken underneath it until 2026-11-14.

    The driver hands `two["broad_across"]` to plan_for. On the ladder path that is an integer and
    `background_for` reproduced the planner's background exactly; on a pinned plan the broad
    resolution is an OUTCOME and arrives as a float, and `int(cells_across)` truncated it. Asked
    for 25.1598 um and 8.0000 cells on the pitch, the container was handed 26.4047 um and 7.6228 -
    rung 7 of the very ladder the pin exists to leave - and the run was still reported under the
    pinned cell's name. Every test passed, because none of them looked past the planner's dict.
    """
    for k, limit in ((8, RUN_LIMIT), (10, 900_000_000)):
        cell = PITCH_M / k
        two, said = _pinned(cell, limit=limit)
        assert two is not None, said
        plan, delivered = _delivered(two, pinned=cell)
        assert delivered == pytest.approx(cell, rel=1e-12), (
            f"pitch/{k}: pinned {cell * 1e6:.4f} um, the plan delivers "
            f"{delivered * 1e6:.4f} um")
        assert PITCH_M / delivered == pytest.approx(float(k), abs=1e-9), (
            f"pitch/{k}: the delivered lattice is {PITCH_M / delivered:.4f} cells per pitch")
        assert plan.background_cell_m == pytest.approx(two["background_m"], rel=1e-12)


def test_the_ladder_path_carries_its_background_too_rather_than_relying_on_a_coincidence():
    """It was only ever right by the accident that background_for reproduces the planner's own
    arithmetic for a whole number of cells. Now it is carried, so the accident is not load-bearing
    and the two paths cannot drift."""
    two, said = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=RUN_LIMIT,
                                  asked_across=B.CELLS_ACROSS_BY_FIDELITY["standard"],
                                  prismatic_axis=1, ranks=8)
    assert two is not None, said
    plan, delivered = _delivered(two)
    assert plan.background_cell_m == pytest.approx(two["background_m"], rel=1e-12)
    assert delivered == pytest.approx(BUILT_CELL_M, rel=1e-4)
    assert plan.fine_cell_pinned_m == 0.0
    assert plan.under_planner_floor is False


def test_a_pinned_plan_survives_the_trip_to_the_container(tmp_path):
    cell = PITCH_M / 8.0
    two, said = _pinned(cell, limit=RUN_LIMIT)
    assert two is not None, said
    plan, _ = _delivered(two, pinned=cell)
    PL.write_plan(tmp_path, plan)
    back = PL.read_plan(tmp_path)
    assert back.background_cell_m == pytest.approx(cell * 2 ** two["fine_levels"], rel=1e-12)
    assert back.fine_cell_pinned_m == pytest.approx(cell, rel=1e-12)
    assert back.under_planner_floor is True


def test_the_broad_resolution_is_reported_rounded_not_truncated():
    """20.99 cells across is better reported as 21 than as 20, and `cells_across` is published as
    `cells_across_asked`. Nothing changes on the ladder path, where it is already whole."""
    two, said = _pinned(PITCH_M / 8.0, limit=RUN_LIMIT)
    assert two is not None, said
    plan, _ = _delivered(two, pinned=PITCH_M / 8.0)
    assert plan.cells_across == round(two["broad_across"])
    assert plan.cells_across != int(two["broad_across"]), (
        "this fixture is meant to have a fractional broad resolution, or it tests nothing")


def test_every_planner_sets_the_floor_flag_so_no_reader_has_to_know_which_one_it_got():
    """`plan_two_scales` never set the key, so the first reader to use it would KeyError on the
    default path - which is every single-scale and every unpinned part."""
    ladder, _ = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=CUSTOMER, limit=RUN_LIMIT,
                                  asked_across=5, prismatic_axis=1, ranks=8)
    pin, _ = _pinned(PITCH_M / 8.0, limit=RUN_LIMIT)
    assert ladder["under_planner_floor"] is False
    assert pin["under_planner_floor"] is True


# ---------------------------------------------------------------- what the stamp says about it

def _plan_at(cell_m, *, levels=4, sizing="", ranks=8, pinned=0.0, under_floor=False):
    """A plan carrying a resolution, as the driver would write it."""
    return PL.ConjugatePlan(
        background_cell_m=cell_m * 2 ** levels, levels=1,
        expect_fluid_volume_m3=5.3661786893e-05, expect_fluid_pieces=2, volume_tol=0.05,
        narrowest_m=CUSTOMER["broad"]["width_m"],
        fine_surfaces=({"name": "microchannels", "file": PL.FLUID_FINE_STL,
                        "levels": levels, "thickness_m": 4.0 * cell_m},),
        ranks=ranks, ranks_basis="the mesh needs it", sizing_basis=sizing,
        fine_cell_pinned_m=pinned, under_planner_floor=under_floor)


def _delivered(two, *, pinned=0.0):
    """The thin cell the PLAN delivers for a planner decision - what the mesher is handed.

    This is the step that was missing, and the whole change was broken underneath it: the driver
    hands `two["broad_across"]` to plan_for, and a test that asserts the planner's own dict never
    sees what happens next.
    """
    plan, _lo, _hi = PL.plan_for(
        narrowest_m=CUSTOMER["broad"]["width_m"], fluid_volume_m3=5.3661786893e-05,
        passages=2, solid_lo=LO, solid_hi=HI,
        fluid_lo=[-0.007164, 0.021522, 0.015886], fluid_hi=[0.053007, 0.033534, 0.067913],
        cells_across=two["broad_across"], levels=two["broad_levels"],
        background_m=two.get("background_m"), fine_cell_pinned_m=pinned,
        under_planner_floor=bool(two.get("under_planner_floor")), no_ports=True)
    return plan, plan.background_cell_m / 2 ** two["fine_levels"]


def test_the_stamp_says_what_resolution_the_mesh_was_built_at():
    """A rank count was stamped and a cell size was not, though the cell size changes the mesh far
    more. Two runs of one part at two resolutions were distinguishable only by an opaque input
    digest, which is no use to anyone comparing two meshes."""
    cell = PITCH_M / 8.0
    with det.recording() as rec:
        N.note_plan_settings(_plan_at(cell, sizing="meshed at a FORCED thin cell"))
    assert rec.settings["conjugate_background_cell_m"]["value"] == pytest.approx(cell * 16)
    assert rec.settings["conjugate_octree_levels"]["value"] == 1
    assert rec.settings["conjugate_fine_surface_levels"]["value"] == [4]
    assert "FORCED" in rec.settings["conjugate_background_cell_m"]["basis"]
    assert rec.settings["conjugate_ranks"]["value"] == 8


def test_the_stamp_distinguishes_a_cell_somebody_pinned_from_one_the_planner_chose():
    """The same number from two different decisions is not the same claim."""
    cell = PITCH_M / 8.0
    with det.recording() as chosen:
        N.note_plan_settings(_plan_at(cell))
    with det.recording() as pinned:
        N.note_plan_settings(_plan_at(cell, pinned=cell, under_floor=True))

    assert chosen.settings["conjugate_fine_cell_pinned_m"]["value"] is None
    assert "nothing pinned" in chosen.settings["conjugate_fine_cell_pinned_m"]["basis"]
    assert "conjugate_under_planner_floor" not in chosen.settings
    assert pinned.settings["conjugate_fine_cell_pinned_m"]["value"] == pytest.approx(cell)
    assert "did NOT choose" in pinned.settings["conjugate_fine_cell_pinned_m"]["basis"]
    assert pinned.settings["conjugate_under_planner_floor"]["value"] is True
    # `comparable` returns the keys that differ, and it compares only each setting's VALUE, never
    # its basis. So the pin had to be its own value rather than a sentence inside another row's
    # basis, or two runs at the same cell for different reasons would read as the same run.
    assert "settings" in det.comparable(det.stamp("cfmesh_conjugate", record=chosen),
                                        det.stamp("cfmesh_conjugate", record=pinned))


def test_the_stamp_never_asks_its_own_environment_what_the_host_decided(monkeypatch):
    """IT INVERTED ON EVERY HOSTED RUN. The stamp read `forced_fine_cell()` - this process's
    environment - for a decision taken on the host. The container's environment is assembled from
    declared pair lists rather than forwarded wholesale, so on Cloud Run the knob is simply absent
    and the stamp said "nothing pinned" on precisely the runs that were pinned.

    Both directions are checked here: a pinned plan in a container with no knob set must still
    stamp as pinned, and an unpinned plan in a container that somehow HAS the knob set must not."""
    cell = PITCH_M / 8.0
    monkeypatch.setattr(det, "CONJUGATE_FINE_CELL_M", "")
    with det.recording() as hosted:
        N.note_plan_settings(_plan_at(cell, pinned=cell))
    assert hosted.settings["conjugate_fine_cell_pinned_m"]["value"] == pytest.approx(cell)

    monkeypatch.setattr(det, "CONJUGATE_FINE_CELL_M", repr(cell))
    with det.recording() as leaked:
        N.note_plan_settings(_plan_at(cell))
    assert leaked.settings["conjugate_fine_cell_pinned_m"]["value"] is None


def test_a_malformed_knob_cannot_reach_the_stamp_at_all(monkeypatch):
    """A stamp is a record, never a prerequisite - and now it reads the plan, so a bad value in the
    environment cannot raise inside it however it got there."""
    monkeypatch.setattr(det, "CONJUGATE_FINE_CELL_M", "banana")
    with det.recording() as rec:
        N.note_plan_settings(_plan_at(PITCH_M / 8.0))
    assert rec.settings["conjugate_fine_cell_pinned_m"]["value"] is None


def test_the_sizing_basis_survives_the_trip_to_disk_and_back(tmp_path):
    """read_plan filters to known fields, so a field the driver sets and the container cannot read
    is a field nobody reads."""
    said = "meshed at a FORCED thin cell of 25.1598 um; this is UNDER the 4-cell floor"
    PL.write_plan(tmp_path, _plan_at(PITCH_M / 8.0, sizing=said))
    back = PL.read_plan(tmp_path)
    assert back.sizing_basis == said
    assert back.ranks_basis == "the mesh needs it"


def test_the_pinned_background_is_what_the_mesher_is_actually_told(tmp_path):
    """The whole seam, with no mesher: the pinned cell decides the background, the plan carries it
    to disk, and the renderer puts it in the dictionary cfMesh reads. A 78-minute run once rebuilt
    an identical mesh because a renderer argument was left off one caller."""
    cell = PITCH_M / 8.0
    plan = _plan_at(cell)
    PL.write_plan(tmp_path, plan)
    back = PL.read_plan(tmp_path)
    text = C.render_meshdict(block_file="block.stl", fluid_file="fluid.stl",
                             background_cell_m=back.background_cell_m, levels=back.levels,
                             fine_surfaces=[dict(s) for s in (back.fine_surfaces or ())])
    assert "maxCellSize" in text
    wanted = f"{cell * 2 ** 4:.6g}"
    assert wanted in text or f"{cell * 2 ** 4:.8g}" in text, (
        f"the dictionary does not carry the {cell * 2 ** 4 * 1e6:.4f} um background:\n{text[:400]}")


def test_the_driver_takes_one_branch_or_the_other_and_never_falls_back():
    """A pinned cell that cannot be reached must REFUSE, not quietly mesh at the planner's own
    choice and be reported under the pinned cell's name."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D)
    assert "two_scales_at_fine_cell" in src
    forced = src.index("_forced = det.forced_fine_cell()")
    guard = src.index("if _forced > 0.0:", forced)
    call = src.index("B.two_scales_at_fine_cell", guard)
    other = src.index("B.plan_two_scales", guard)
    assert guard < call < other, "the pinned branch must be taken instead of the ladder, not before it"
    assert "else:" in src[call:other], "the two must be exclusive, or a refusal falls through"
