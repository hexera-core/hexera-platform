# A layer pass puts its stack INSIDE the interface cell, so the picture draws what is left of
# that cell - and both sides of the container boundary were still using the cell the plan pinned.
#
# JOB b8f09ae9 LOST ITS WHOLE REVIEW TO THIS. 279,294,127 cells, every quality gate passed, and
# the evidence gate fired because the one prepared interior cut it was obliged to see was refused:
#
#   review session: inspection_regions.through_the_plate.frames.zoom is sized correctly (4.00 px
#   across the planned 25.16 um cell) and a cell is still not countable in it: what was resolved
#   along the horizontal axis the structure repeats on is drawn 2.0 px apart against the 4.00 px
#   the stated window makes a planned cell, outside the factor of 1.5 ...
#
# The 4.00 px was an identity on BOTH sides: slices sized the window to plan_cell / CELL_PIXELS
# and review_session divided by quality.near_wall_cell_m, which is background_cell_m / 2**levels -
# the same number. So every zoom frame declared itself countable whatever was drawn in it, and the
# one thing that could disagree was the ink.
#
# IT IS A ONE-PIXEL KNIFE EDGE. Jobs 22ac7054 and cba7ac69 are the same mesh as each other and
# read a pitch of 3.0 px against the same 4.00 claim, inside the band, and PASSED. Their PNG is
# 459,927 B against b8f09ae9's 452,410 B and every other figure agrees to six decimals.
#
# AND THE TWO HALVES OF THE FIX ARE JOINTLY NECESSARY, which is what this file is really for.
# Measured against job b8f09ae9's own 452,410-byte PNG with the production resolver:
#   * stating the drawn cell but keeping the plan's window -> "6.29 um/px, which is 2.08 px across
#     the planned 13.06 um cell - under the 4.0 px a cell must be to be counted". The existing
#     pictures cannot be rescued by relabelling: they genuinely do not resolve the real cells.
#   * shrinking the window but not stating the drawn cell -> the measured ~3.85 px pitch is
#     compared against the plan cell's 7.71 px, ratio 0.50, outside the 1.5 band the other way.
# Only both together pass, and neither alone does.
from __future__ import annotations

import copy

import pytest
from _passage_pictures import write_cut_picture  # noqa: E402  (this dir is on sys.path)

import meshpipeline.engines.cfmesh_conjugate.slices as S
from meshpipeline.sandbox.render_adapter import expand_region_targets, resolve_regions
from meshpipeline.sandbox.review_session import ArtifactResolver

#: quality.near_wall_cell_m on job b8f09ae9 - background_cell_m / 2**levels, a plan figure.
PLAN_CELL_M = 2.51578e-05
#: quality.layer_thickness_m and quality.layers_in_mesh, as that job's layer pass measured them.
STACK_M, LAYERS_IN_MESH = 1.21e-05, 2.0
#: What the picture therefore draws: 2.51578e-05 - 1.21e-05.
DRAWN_CELL_M = 1.30578e-05
#: report.review.bbox - the box the real frames were drawn against.
BBOX = (-0.061679501086473465, 0.020110199227929115, -0.008694685064256191,
        0.11601100116968155, 0.06193649768829346, 0.09251250326633453)
#: content.period_m on the real frame: the passage pitch, 201.262 um.
PERIOD_M = 0.0002012624

ZOOM_REGION = "through_the_plate"


def _build(tmp_path, *, drawn_m, cells_px, state_drawn, stated=None):
    """Cuts drawn through the production renderer, with real PNGs beside them.

    `drawn_m` is what the producer sized its zoom window from; `cells_px` is how many pixels a
    cell is actually drawn at in that window; `state_drawn` is whether the frame publishes the
    key the reader now looks for.
    """
    cuts = S.review_slices(BBOX[:3], BBOX[3:], cell_m=PLAN_CELL_M, drawn_m=drawn_m)
    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    for c in cuts:
        for f in c["frames"]:
            rel = f"review/cuts/{c['name']}_{f['scope']}.png"
            if f["scope"] == "zoom":
                # the passage pitch, in the pixels THIS window makes of it
                period_px = max(8, int(round(PERIOD_M / float(f["metres_per_pixel"]))))
                write_cut_picture(tmp_path / rel, S.FRAME_W, S.FRAME_H,
                                  cells_px=int(cells_px), period_px=period_px)
                if not state_drawn:
                    f.pop("drawn_cell_m", None)
                elif stated is not None:
                    f["drawn_cell_m"] = float(stated)
            else:
                write_cut_picture(tmp_path / rel, S.FRAME_W, S.FRAME_H)
            f["image"] = rel                  # what the VM's render adds, and nothing else
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": PLAN_CELL_M, "layer_thickness_m": STACK_M,
                       "layers_in_mesh": LAYERS_IN_MESH, "layers_asked": 3,
                       "layer_patch": "fluid_to_solid"},
           "inspection_regions": cuts}
    return man


def _offered(tmp_path, man):
    cuts = ArtifactResolver(tmp_path, man).resolve_cuts()
    targets = expand_region_targets(resolve_regions(man), required=True, purpose="p",
                                    volume_available=False, cuts=cuts)
    # InspectionTarget carries `target_id`, already normalised by `normalize_region_id`.
    ids = {getattr(t, "target_id", "") for t in targets or ()}
    zoom = next((f for f in cuts.get(ZOOM_REGION, ()) if f.scope == "zoom"), None)
    return ids, zoom


def test_the_helper_takes_the_stack_out_of_the_cell_it_was_grown_in():
    """2.51578e-05 - 1.21e-05 = 1.30578e-05, which at job b8f09ae9's 6.28945e-06 m/px is 2.076 px
    - and the integer scan of the real PNG read 2.0. That is the measurement the plan's 4.00 px
    claim was 1.9 pixels away from."""
    assert S.drawn_cell_m(PLAN_CELL_M, layer_thickness_m=STACK_M,
                          layers_in_mesh=LAYERS_IN_MESH) == pytest.approx(DRAWN_CELL_M)
    # no measured pass, no stack inside the cell: every unlayered run stays byte-identical
    assert S.drawn_cell_m(PLAN_CELL_M) == PLAN_CELL_M
    assert S.drawn_cell_m(PLAN_CELL_M, layer_thickness_m=STACK_M, layers_in_mesh=0.0) == PLAN_CELL_M
    assert S.drawn_cell_m(PLAN_CELL_M, layer_thickness_m=0.0, layers_in_mesh=2.0) == PLAN_CELL_M
    # a stack at or over its own cell is two figures from different runs, not a measurement
    assert S.drawn_cell_m(PLAN_CELL_M, layer_thickness_m=9e-5, layers_in_mesh=2.0) == PLAN_CELL_M
    assert S.drawn_cell_m(None, layer_thickness_m=STACK_M, layers_in_mesh=2.0) == 0.0


def test_both_halves_together_offer_the_region(tmp_path):
    """The window sized from the drawn cell AND the frame stating what it drew."""
    man = _build(tmp_path, drawn_m=DRAWN_CELL_M, cells_px=4, state_drawn=True)
    ids, zoom = _offered(tmp_path, man)
    assert zoom is not None
    assert round(zoom.cell_px, 2) == 4.00, "the drawn cell is four pixels in its own window"
    assert zoom.cells_resolved, "the measured pitch agrees with what the frame says it drew"
    assert zoom.counts_cells
    assert f"region:{ZOOM_REGION}" in ids


def test_shrinking_the_window_without_saying_so_is_refused(tmp_path):
    """THE PRODUCER HALF ALONE. The picture now resolves the real cells, but the reader is still
    dividing by the plan's 25.16 um, so it makes a cell 7.71 px and measures 3.85 - ratio 0.50,
    outside the factor of 1.5, failing the opposite way round from the live job."""
    man = _build(tmp_path, drawn_m=DRAWN_CELL_M, cells_px=4, state_drawn=False)
    ids, zoom = _offered(tmp_path, man)
    assert zoom is not None
    assert not zoom.cells_resolved
    assert not zoom.counts_cells
    assert f"region:{ZOOM_REGION}" not in ids


def test_saying_so_without_shrinking_the_window_is_refused(tmp_path):
    """THE READER HALF ALONE, and this is the arm measured against the real PNG. In the plan's
    own window the drawn cell is 2.08 px, under the 4.0 px floor a cell has to be drawn at before
    it can be counted at all. Relabelling cannot rescue a picture that does not resolve."""
    man = _build(tmp_path, drawn_m=None, cells_px=2, state_drawn=True, stated=DRAWN_CELL_M)
    ids, zoom = _offered(tmp_path, man)
    assert zoom is not None
    assert round(zoom.cell_px, 2) == 2.08, "the real cell, in the window the plan sized"
    assert not zoom.counts_cells
    assert f"region:{ZOOM_REGION}" not in ids


def test_a_frame_that_claims_a_coarser_cell_than_it_drew_is_still_refused(tmp_path):
    """THE PROTECTION THAT HAD TO SURVIVE. Letting a frame state its own cell must not let it
    state any cell: this one is drawn at the real 13.06 um and declares 52.23 um, four times
    coarser, which is the magnification lie the tolerance exists to catch."""
    man = _build(tmp_path, drawn_m=DRAWN_CELL_M, cells_px=4, state_drawn=True,
                 stated=DRAWN_CELL_M * 4.0)
    ids, zoom = _offered(tmp_path, man)
    assert zoom is not None
    assert not zoom.counts_cells
    assert f"region:{ZOOM_REGION}" not in ids


def test_a_frame_that_claims_a_finer_cell_than_it_drew_is_still_refused(tmp_path):
    """And the other direction, so the key cannot be used to manufacture countability either."""
    man = _build(tmp_path, drawn_m=DRAWN_CELL_M, cells_px=4, state_drawn=True,
                 stated=DRAWN_CELL_M / 4.0)
    _ids, zoom = _offered(tmp_path, man)
    assert zoom is not None
    assert not zoom.counts_cells


def test_an_unlayered_run_publishes_no_such_key_and_is_unchanged(tmp_path):
    """Every other engine, and every run of this one without a layer pass, must be untouched: the
    producer publishes drawn_cell_m equal to the plan cell and the reader's override never fires."""
    plain = S.review_slices(BBOX[:3], BBOX[3:], cell_m=PLAN_CELL_M)
    withdrawn = S.review_slices(BBOX[:3], BBOX[3:], cell_m=PLAN_CELL_M, drawn_m=PLAN_CELL_M)
    for a, b in zip(plain, withdrawn):
        assert a == b, "sizing from a drawn cell equal to the plan cell changes nothing"
    zoom = next(f for f in plain[0]["frames"] if f["scope"] == "zoom")
    assert zoom["drawn_cell_m"] == PLAN_CELL_M
    assert zoom["feature_m"] == PLAN_CELL_M
    assert round(zoom["metres_per_pixel"], 12) == round(PLAN_CELL_M / S.CELL_PIXELS, 12)


def test_the_window_the_live_job_needed_is_the_window_this_produces():
    """The size the production log itself asked for, in its own words: "A window of 4.18 mm across
    would"."""
    w, h, mpp = S.zoom_window(DRAWN_CELL_M)
    assert round(w * 1e3, 2) == 4.18
    assert round(h * 1e3, 2) == 3.13
    assert round(DRAWN_CELL_M / mpp, 2) == 4.00


def test_feature_m_still_reports_the_plan_so_the_re_aiming_yardstick_does_not_move():
    """native._expected_pitch_m derives the part's repeat pitch as `feature_m * CELL_PIXELS` and
    the planes are re-aimed off it, so the drawn cell had to arrive as a NEW key rather than by
    redefining this one."""
    cuts = S.review_slices(BBOX[:3], BBOX[3:], cell_m=PLAN_CELL_M, drawn_m=DRAWN_CELL_M)
    zoom = next(f for f in cuts[0]["frames"] if f["scope"] == "zoom")
    assert zoom["feature_m"] == PLAN_CELL_M
    assert zoom["feature_pixels"] == S.CELL_PIXELS
    assert zoom["drawn_cell_m"] == DRAWN_CELL_M
    assert "less the layer stack grown inside it" in zoom["why_frame"]


def test_the_two_sides_cannot_drift_apart_again():
    """The producer sizes to CELL_PIXELS and the reader judges against its own floor. If those two
    ever differ, every zoom frame is refused by the reader that asked for it - which is how this
    whole class of defect happens."""
    from meshpipeline.sandbox import review_session as RS

    assert S.CELL_PIXELS == RS.COUNTABLE_CELL_PX
    man = copy.deepcopy({"a": 1})            # keeps copy imported where the arms use it
    assert man == {"a": 1}
