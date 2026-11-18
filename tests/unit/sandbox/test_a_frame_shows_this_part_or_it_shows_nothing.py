# Responsibility: Verify a prepared frame discharges a region only when the periodic structure
# measured in its own pixels is THE STRUCTURE OF THE PART THE PLAN DESCRIBES, and only when that
# structure reaches across enough of the frame to be a picture of the region.
# Boundaries: the home side only, and only what can be measured off a PNG plus the one plan
# figure. Nothing here renders and nothing here reads a manifest for a size.
#
# THE DEFECT THIS IS WRITTEN FROM. Round 4 moved the array measurement to this side, so a frame
# of solid metal with `shows_structure: true` beside it is refused. An independent review then
# measured THREE more pictures with no passage in them that still discharged, and they share one
# cause: THIS SIDE'S GRANT WAS STILL UNCONDITIONAL. `shows_passages` asked whether SOME periodic
# structure was in the frame; nothing asked whether it was the part's, and
# `picture_structure.period_matches` - the only thing that asks the second question - had zero
# callers in review_session.py.
#
# MEASURED, 2026-11-17, through the production ArtifactResolver, on frames drawn here. Every one
# of these came back `counts_cells` TRUE with no claim in the manifest at all:
#
#   frame                                   ink      rep  period  str   best rows  what it is
#   plenum, bare 2 px wall every 32 px  93.7500%      39   32.0  0.975   240  720  metal
#   plenum, bare 4 px wall every 32 px  87.5000%      39   32.0  0.975   240  720  metal
#   plenum, bare 2 px wall every 64 px  96.8750%      19   64.0  0.950   280  720  wrong feature
#   12 bands, 384 x 64 px, blank round   1.0000%      11   32.0  0.898    36   48  a speck
#   20 bands, 640 x 32 px, blank round   0.8333%      19   32.0  0.925    60   24  the same speck
#   HONEST passages, 16 px of every 32  50.0000%      39   32.0  0.975   120  720  the real shape
#
# THE FIRST TWO ARE THE SAME PERIOD AS THE HONEST FRAME, which is measured above and is why no
# period test can refuse them: 32.0 px either way, 39 repeats either way, 0.975 either way, a 4.0
# px drawn cell either way, 720 rows either way. The WALL is what is missing.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED, except the one test that pins the floors to the
# production constants. A test that cannot be COLLECTED against the old tree proves only that a
# constant is new, and the claim here is about behaviour.
#
# WHAT ROUND 6 CHANGED UNDER THIS FILE, because the prose below still names machinery that is
# gone and every verdict it asserts is unchanged. `period_is_the_parts` - the band that held a
# period counted in drawn cells to PLAN_CELLS_ACROSS_A_WALL within PERIOD_MATCH - is DELETED: it
# was a bar on passage width plus wall width where the plan fixes only the wall, and it refused
# true pictures of real passages (see
# `test_the_metal_between_passages_is_what_is_measured.py`). Every frame refused below is still
# refused, by the WALL instead, and the wall is now the median run of samples with no ink in them
# over the cell drawn on the axis the structure repeats on, rather than a period times an ink
# DENSITY share. `CELL_PITCH_TOLERANCE` went from 2.0 to 1.5, which is what now refuses the
# declared-coarser frame that `period_cells_claimed`'s band was carrying.
from __future__ import annotations

from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_anti_aliased_passages,
    write_passages,
    write_plenum_with_ribs,
    write_speck_of_array,
)

from meshpipeline.sandbox.render_adapter import expand_region_targets
from meshpipeline.sandbox.render_inputs import resolve_regions
from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05              # quality.near_wall_cell_m, and fine_cell_pinned_m
THICKNESS_M = 0.0001006312        # conjugate_plan.fine_surfaces[0].thickness_m, the thin wall
FRAME_PX = (1280, 960)
#: The producer's own zoom scale: one planned cell four pixels across. 6.28945 um/px.
MPP = CELL_M / 4.0

#: THE ONE PLAN FIGURE every test below leans on, restated here rather than imported so that each
#: test runs against the OLD tree too and fails there on its assertion rather than on its import.
#: Pinned to the production constant by `test_the_floors_are_the_production_ones`.
PLAN_CELLS_ACROSS_A_WALL = 4.0
PERIOD_FACTOR = 2.5
WALL_CELLS_MIN = 2.0
EXTENT_MIN = 0.10

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _manifest(ws: Path, frames: list[dict]) -> dict:
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
            "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                        "max_cell_m": 0.0006788786304584814},
            "inspection_regions": [dict(REGION, frames=frames)]}


def _frame(rel: str, *, mpp: float = MPP, pixels=FRAME_PX, content=None) -> dict:
    out = {"scope": FRAME_ZOOM, "image": rel,
           "window_m": [mpp * pixels[0], mpp * pixels[1]],
           "metres_per_pixel": mpp,
           "centre_m": [0.0309152940, 0.0242716710],
           "centre_source": "the densest window-sized patch of this cut"}
    if content is not None:
        out["content"] = content
    return out


def _cut(ws: Path, draw, *, mpp: float = MPP, content=None, **kw):
    """Draw one frame with `draw`, declare it, and resolve it as production does."""
    rel = "review/cuts/across_the_flow_zoom.png"
    draw(ws / rel, *FRAME_PX, **kw)
    man = _manifest(ws, [_frame(rel, mpp=mpp, content=content)])
    got = ArtifactResolver(ws, man).resolve_cuts().get("across_the_flow", ())
    assert got, "the frame was not offered at all, so nothing below is being tested"
    return got[0]


 # A. the period, held to the part the plan describes

def test_a_plenum_with_a_rib_in_it_does_not_discharge_its_region(tmp_path):
    """A SOLID FIELD OF FLUID with a bare 2 px wall every 32 px. 93.75% of it is coolant.

    MEASURED, and this is the whole reason the period cannot be what refuses it: this frame and
    the honest one below report the SAME 39 repeats, the SAME 32.0 px period, the SAME 0.975 of a
    perfect array, the SAME 4.0 px drawn cell and the SAME 720 rows. One period of this picture
    holds 7.5 drawn cells of coolant and 0.5 of metal; one period of the honest picture holds
    4 and 4. The plan puts PLAN_CELLS_ACROSS_A_WALL cells across this part's thin wall, so half a
    cell of metal is not a picture of that wall.
    """
    cut = _cut(tmp_path, write_plenum_with_ribs, wall_px=2, period_px=32)
    assert cut.ink is not None
    # every gate that existed before this round passes on this frame
    assert cut.ink.shows_structure, "a periodic structure really is in it"
    assert cut.ink.repeats == 39 and cut.ink.period_px == 32.0, (cut.ink.repeats,
                                                                 cut.ink.period_px)
    assert cut.ink.strength >= 0.975
    assert cut.ink.cells_on_best_row >= 240 and cut.ink.cell_rows == 720
    assert cut.ink.cell_pitch_px == 4.0 and cut.cell_px == 4.0
    assert cut.ink.ink_fraction == 0.9375, cut.ink.ink_fraction
    # and the picture is of metal, so it cannot cover the region
    assert not cut.counts_cells, (
        "93.75% of this frame is coolant and half a drawn cell of metal stands between its "
        "'passages' - it is a plenum with a rib in it, and it must not discharge a region")


def test_a_rib_one_cell_thick_is_still_a_plenum(tmp_path):
    """The same defect at twice the rib: a bare 4 px wall every 32 px, 87.50% inked.

    One drawn cell of metal between passages, against the PLAN_CELLS_ACROSS_A_WALL the plan puts
    across the thin wall. WALL_CELLS_MIN is half of that, so this is refused by a factor of two.
    """
    cut = _cut(tmp_path, write_plenum_with_ribs, wall_px=4, period_px=32)
    assert cut.ink is not None and cut.ink.ink_fraction == 0.875, cut.ink.ink_fraction
    assert cut.ink.shows_structure and cut.ink.cell_rows == 720
    assert cut.ink.cells_on_best_row >= 240
    assert not cut.counts_cells, (
        "one drawn cell of metal between passages is not the wall the plan resolved four cells "
        "deep")


def test_the_wrong_feature_of_the_right_part_does_not_discharge(tmp_path):
    """A REGULAR ARRAY AT FOUR TIMES THE PART'S PITCH: a bare 2 px wall every 64 px.

    MEASURED: 19 repeats, period 64.0 px, 0.950 of a perfect array, 280 interiors on the best row
    over 720 rows. At this frame's 6.28945 um/px that period is 402.52 um, and job 0ff6f9e5 really
    does carry regular arrays at 402.525 um and 805.050 um - four and eight times its 100.631 um
    channel wall. A reviewer asked how many cells are across a passage would have answered from
    this one about the wrong passage.

    The period here is 16.0 DRAWN CELLS against the PLAN_CELLS_ACROSS_A_WALL the plan puts across
    the thin wall, which is 4.0x out and so outside PERIOD_FACTOR either way.
    """
    cut = _cut(tmp_path, write_plenum_with_ribs, wall_px=2, period_px=64)
    assert cut.ink is not None
    assert cut.ink.shows_structure and cut.ink.repeats == 19, cut.ink.repeats
    assert cut.ink.period_px == 64.0 and cut.ink.cell_pitch_px == 4.0
    assert cut.ink.strength >= 0.95
    assert cut.ink.cells_on_best_row >= 280 and cut.ink.cell_rows == 720
    assert cut.ink.period_px / cut.ink.cell_pitch_px > PLAN_CELLS_ACROSS_A_WALL * PERIOD_FACTOR
    assert not cut.counts_cells, (
        "a regular array four times the part's own pitch is a different feature of the part, and "
        "a count taken across it is a count across the wrong thing")


def test_a_stated_window_cannot_buy_a_frame_the_structure_it_does_not_have(tmp_path):
    """THE SAME WRONG ARRAY, declared at a window half the size, is refused just the same.

    WHY THIS MATTERS. The metres form of the same question - the measured period times the stated
    metres per pixel, against the plan's feature size - is defeated here. Halving the stated
    window doubles `cell_px` to 8.00 and halves the period in metres to 201.26 um, which is
    exactly this plate's full channel pitch. What refuses both is the wall: the bare rib is 2 px
    on a 4.0 px drawn cell either way, which is half a cell, and the drawn cell is a pixel figure
    that no stated window can reach. (Round 6: the halved frame is ALSO refused on the drawn cell
    against the claimed one, now that `CELL_PITCH_TOLERANCE` is 1.5 and no longer admits the
    factor of two this frame declares.)
    """
    honest = _cut(tmp_path / "a", write_plenum_with_ribs, wall_px=2, period_px=64)
    halved = _cut(tmp_path / "b", write_plenum_with_ribs, wall_px=2, period_px=64, mpp=MPP / 2.0)
    assert honest.cell_px == 4.0 and halved.cell_px == 8.0, (honest.cell_px, halved.cell_px)
    assert honest.ink is not None and halved.ink is not None
    assert honest.ink.period_px == halved.ink.period_px == 64.0
    assert honest.ink.cell_pitch_px == halved.ink.cell_pitch_px == 4.0, (
        "the picture is byte-identical: only the stated window differs")
    # the halved window puts the period in METRES at this plate's own 201.26 um full pitch
    assert abs(halved.ink.period_px * halved.m_per_px - 2.01262e-4) < 1e-7
    assert not honest.counts_cells and not halved.counts_cells, (
        "a stated window may not buy a frame the structure it does not have")


def test_a_frame_drawn_coarser_than_it_claims_is_refused_too(tmp_path):
    """AND THE DRAWN-CELL RATIO ALONE IS NOT ENOUGH EITHER, which is measured and not argued.

    An honest-looking array drawn at 8 px cells on a 64 px period, declared at the honest
    6.28945 um/px window, reports exactly the part's own SHAPE: 8.00 cells per period and 4.00
    cells of metal between passages, the same figures as the honest frame below it. Its period in
    metres is 402.52 um - a different feature of this plate - and nothing in the pixels can say
    so, because a picture has no absolute scale in it.

    What fixes the scale is `cell_px`, the stated window divided into the manifest's own cell, and
    `CELL_PITCH_TOLERANCE` admits a drawn pitch of 8.0 against a claimed 4.00 exactly, at its
    boundary - so the existing floor does NOT catch this. The period counted in the CLAIMED cell
    reads 16.00 and does, and it is required as well as the drawn one. Each is blind to precisely
    what the other sees.
    """
    coarse = _cut(tmp_path / "a", write_passages, cells_px=8, period_px=64, duty=0.5)
    honest = _cut(tmp_path / "b", write_passages, cells_px=4, period_px=32, duty=0.5)
    assert coarse.ink is not None and honest.ink is not None
    # the shapes measured off the two pictures are identical
    assert coarse.ink.period_px / coarse.ink.cell_pitch_px == 8.0
    assert honest.ink.period_px / honest.ink.cell_pitch_px == 8.0
    assert coarse.ink.shows_structure and coarse.ink.cell_rows > 100
    # the stated window is the honest one in BOTH, and the drawn pitch differs by exactly 2x
    assert coarse.cell_px == honest.cell_px == 4.0
    assert coarse.ink.cell_pitch_px == 8.0 and honest.ink.cell_pitch_px == 4.0
    assert abs(coarse.ink.period_px * coarse.m_per_px - 4.02525e-4) < 1e-7, (
        "this frame's period really is the plate's 402.5 um feature")
    assert not coarse.counts_cells, (
        "a frame drawn at twice the magnification it claims shows a coarser feature with the "
        "right shape, and must not discharge a region")
    assert honest.counts_cells


 # B. how little of a frame can carry the array

def test_a_speck_of_array_in_a_blank_frame_does_not_discharge_its_region(tmp_path):
    """TWELVE BANDS OF HONEST PASSAGES, 384 x 64 px of a 1280x960 frame, the rest blank.

    MEASURED: 1.0000% ink - ten times EVIDENCE_INK_FLOOR - 11 repeats every 32.0 px at 0.898 of a
    perfect array, 36 enclosed interiors on the best row over 48 rows, a 4.0 px drawn cell. Every
    bar passes and the structure covers 1.84% of the frame. At 6.28945 um/px that patch is
    2.42 x 0.40 mm of a 170 mm plate.
    """
    cut = _cut(tmp_path, write_speck_of_array, bands=12, band_px=16, period_px=32, tall_px=64)
    assert cut.ink is not None
    assert cut.ink.ink_fraction == 0.01, cut.ink.ink_fraction
    assert cut.ink.repeats == 11 and cut.ink.period_px == 32.0
    assert cut.ink.strength >= 0.898
    assert cut.ink.cells_on_best_row == 36 and cut.ink.cell_rows == 48
    assert cut.ink.cell_pitch_px == 4.0
    # the period IS the part's, which is why no period test refuses this one
    assert cut.ink.period_px / cut.ink.cell_pitch_px <= PLAN_CELLS_ACROSS_A_WALL * PERIOD_FACTOR
    assert not cut.counts_cells, (
        "99% of this frame is blank and the array in it is 0.40 mm tall - an ink floor cannot "
        "see that and the extent of the repeat can")


def test_the_same_speck_spread_wider_and_thinner_does_not_discharge_either(tmp_path):
    """0.8333% ink, 20 bands over 640 x 32 px: the same defect re-parameterised.

    EVIDENCE_INK_FLOOR is cleared 8.3x here and 10x above, which is what "the near-blank defect
    was re-parameterised and not closed" means - the floor is a fraction of painted pixels and
    both of these paint enough of them.
    """
    cut = _cut(tmp_path, write_speck_of_array, bands=20, band_px=16, period_px=32, tall_px=32)
    assert cut.ink is not None
    assert cut.ink.ink_fraction > 8.0e-3, cut.ink.ink_fraction
    assert cut.ink.repeats == 19 and cut.ink.cell_rows == 24
    assert cut.ink.cells_on_best_row == 60 and cut.ink.cell_pitch_px == 4.0
    assert not cut.counts_cells


def test_the_extent_is_the_footprint_and_not_the_painted_share(tmp_path):
    """The two are different numbers and only one of them separates these frames.

    MEASURED: the honest frame paints 50.0000% of its pixels and its structure covers 0.9758 of
    the frame; the speck paints 1.0000% and covers 0.0184. A plenum paints 93.7500% and covers
    0.9758 - so a CEILING on the painted share would refuse the plenum and a FLOOR on it would
    not refuse the speck, while the footprint refuses the speck and says nothing about the
    plenum, which is refused on its wall instead.
    """
    honest = _cut(tmp_path / "a", write_passages, period_px=32, duty=0.5)
    speck = _cut(tmp_path / "b", write_speck_of_array, bands=12, tall_px=64)
    assert honest.ink is not None and speck.ink is not None
    assert honest.ink.ink_fraction == 0.5 and speck.ink.ink_fraction == 0.01
    assert honest.ink.shows_structure and not speck.ink.shows_structure
    assert "speck of the picture" in speck.ink.structure_why, speck.ink.structure_why
    assert honest.counts_cells and not speck.counts_cells


 # the gate is a gate: what still discharges

def test_the_honest_frame_still_discharges_its_region(tmp_path):
    """16 px of passage every 32 px at 4 px cells: this part's own shape, and it still counts.

    Its period is 8.00 drawn cells, inside PERIOD_FACTOR of the plan's PLAN_CELLS_ACROSS_A_WALL
    because a plane ACROSS an array sees the full pitch while a plane normal to it sees the walls
    at half - on this plate 201.262 um and 100.631 um, both measured. There is no more room than
    that: the nearest wrong feature is 16.00 cells, a factor of two away.
    """
    cut = _cut(tmp_path, write_passages, period_px=32, duty=0.5)
    assert cut.ink is not None and cut.ink.shows_structure
    assert cut.counts_cells, "the one honest shape must still cover its region"
    offered = expand_region_targets(
        resolve_regions(_manifest(tmp_path, [])), required=True, purpose="p",
        volume_available=False, cuts={"across_the_flow": (cut,)})
    assert {t.target_id for t in offered} == {"region:across_the_flow"}


def test_a_frame_with_no_cells_in_it_is_offered_as_no_region_at_all(tmp_path):
    """And a plenum with ribs is not advertised as inspectable, so no round is spent on it."""
    cut = _cut(tmp_path, write_plenum_with_ribs, wall_px=2, period_px=32)
    offered = expand_region_targets(
        resolve_regions(_manifest(tmp_path, [])), required=True, purpose="p",
        volume_available=False, cuts={"across_the_flow": (cut,)})
    assert offered == (), (
        "a region whose only frame is a picture of metal must not be advertised as inspectable")


 # D. the gap nobody had closed: a frame from a real renderer is anti-aliased

def test_an_anti_aliased_frame_at_a_sub_pixel_offset_still_discharges(tmp_path):
    """THIS ONE PASSES AGAINST THE OLD TREE TOO, and it is here because nothing had tried it.

    Every countable frame measured in rounds 1 to 4, the reviews included, is a hand-drawn
    lattice with exactly two grey levels and pixel-aligned edges. `_measure_png` counts an
    enclosed interior as a run of ink LIGHTER THAN THE ROW'S OWN MIDPOINT bounded by ink on both
    sides, and on an anti-aliased render the midpoint moves and a half-covered edge pixel could
    merge into the interior run.

    MEASURED, 2026-11-17, box-filtering an 8x supersampled drawing - which is what an
    anti-aliasing rasteriser applies - at every eighth of a pixel of offset from 0 to 7/8, at
    edge widths of 8/8, 6/8 and 4/8 of a pixel, and with the fill shaded down the frame: 120
    enclosed interiors on the best row at a 4.0 px pitch in EVERY ONE of those 24 frames,
    identical to the hard-edged drawing, with up to 218 distinct grey levels. The count survives,
    because `dark` and `light` are taken PER ROW so the midpoint moves with the anti-aliasing.
    The row count falls from 720 to 232 under the heaviest shading, which is 19x the floor.

    WHERE IT DOES NOT SURVIVE, measured in the same sweep: an edge thinner than about 0.35 px, an
    edge grey of 120 or lighter against a 176 fill, or a fill paler than about 245, each give
    ZERO interiors - every row reads as a flat bar under `_EDGE_CONTRAST_MIN`. That fails CLOSED:
    a faint render declines, it is never miscounted.
    """
    cut = _cut(tmp_path, write_anti_aliased_passages, period_px=32, duty=0.5,
               offset_px=0.375, edge_px=1.0, lit=90)
    assert cut.ink is not None
    assert cut.ink.cells_on_best_row == 120, cut.ink.cells_on_best_row
    assert cut.ink.cell_pitch_px == 4.0, cut.ink.cell_pitch_px
    assert cut.ink.period_px == 32.0 and cut.ink.repeats == 39
    assert cut.ink.cell_rows > 200, cut.ink.cell_rows
    assert cut.counts_cells, (
        "an anti-aliased frame of the honest shape must not be refused by any of this")


def test_an_anti_aliased_plenum_is_refused_just_the_same(tmp_path):
    """And the refusals are not an artefact of hard edges either: the same sweep over the plenum.

    Drawn anti-aliased at a sub-pixel offset with a duty of 30/32, which is the 2 px rib.
    """
    cut = _cut(tmp_path, write_anti_aliased_passages, period_px=32, duty=30.0 / 32.0,
               offset_px=0.375, edge_px=1.0, lit=90)
    assert cut.ink is not None and cut.ink.shows_structure
    assert cut.ink.cells_on_best_row > 200, cut.ink.cells_on_best_row
    assert not cut.counts_cells


 # C. the words, from the sandbox side: the detector's own refusal names the extent

def test_the_refusal_names_the_extent_it_measured(tmp_path):
    """An operator reading the log must be able to see WHICH reading failed and by how much."""
    cut = _cut(tmp_path, write_speck_of_array, bands=12, tall_px=64)
    assert cut.ink is not None
    why = cut.ink.structure_why
    assert "27.58% of the frame along the horizontal axis" in why, why
    assert "the ink reaches 6.67% across it" in why, why
    assert "covers 1.84% of the frame against the 10.00%" in why, why


 # the pinning test: the literals above are the production constants

def test_the_floors_are_the_production_ones():
    """The one test here that imports a name this round added, for the reason the round-4 file
    states: a test that cannot be collected against the old tree proves only that a name is new."""
    from meshpipeline.sandbox.picture_structure import (
        EXTENT_MIN as P_EXTENT,
    )
    from meshpipeline.sandbox.picture_structure import (
        PERIOD_MATCH as P_FACTOR,
    )
    from meshpipeline.sandbox.picture_structure import (
        PLAN_CELLS_ACROSS_A_WALL as P_CELLS,
    )
    from meshpipeline.sandbox.picture_structure import (
        WALL_CELLS_MIN as P_WALL,
    )

    assert (P_CELLS, P_FACTOR, P_WALL, P_EXTENT) == (
        PLAN_CELLS_ACROSS_A_WALL, PERIOD_FACTOR, WALL_CELLS_MIN, EXTENT_MIN)


def test_the_plan_figure_is_the_planners_own_four_cell_floor():
    """PLAN_CELLS_ACROSS_A_WALL is not a chosen number: it is job 0ff6f9e5's own plan arithmetic.

    `conjugate_plan.fine_surfaces[0].thickness_m` is the thin side of this part's wall and
    `quality.near_wall_cell_m` is the cell the planner pinned at that thickness over its
    four-cell floor. The division is exact, and if the planner's floor ever changes this test is
    what fails.
    """
    assert THICKNESS_M / CELL_M == PLAN_CELLS_ACROSS_A_WALL
    assert WALL_CELLS_MIN == PLAN_CELLS_ACROSS_A_WALL / 2.0
