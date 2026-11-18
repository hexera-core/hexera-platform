# Responsibility: Describe the scene to the reviewer in words: where the camera is and what the colours mean.
# Boundaries: presentation of scene facts; it moves no camera.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import SceneContext
from meshpipeline.sandbox.picture_structure import PLAN_CELLS_ACROSS_A_WALL, WALL_CELLS_MIN
from meshpipeline.sandbox.review_session import (
    COUNTABLE_CELL_PX,
    COUNTABLE_CELL_ROWS,
    COUNTABLE_CELLS_ON_A_ROW,
    EVIDENCE_INK_FLOOR,
)

# Reproduced verbatim from `get_navigation_context()`. It is prose, so it belongs here rather
# than in the renderer that used to emit it.
_NAV_NOTE = (
    "move_camera distance defaults to pan_step_mm if omitted. "
    "zoom() uses zoom_step if factor omitted. "
    "Call set_navigation_defaults() to change either."
)


def navigation_context(scene: SceneContext) -> dict:
    bbox: dict = {}
    if scene.bounds is not None:
        b = scene.bounds
        bbox = {
            "xmin": b.xmin, "xmax": b.xmax,
            "ymin": b.ymin, "ymax": b.ymax,
            "zmin": b.zmin, "zmax": b.zmax,
            "units": scene.mesh_units,
        }
    return {
        "bbox_mm": bbox,
        "pan_step_mm": scene.pan_step_mm,
        "zoom_step": scene.zoom_step,
        "note": _NAV_NOTE,
    }


def patch_colour_legend(scene: SceneContext) -> str:
    parts = [f"{e.patch_id}={e.color}" for e in scene.patch_legend]
    return ", ".join(parts) if parts else "see mesh colours"


def go_to_coordinates_text(
    scene: SceneContext,
    *,
    x: float,
    y: float,
    z: float,
    span: float,
    preset: str = "",
    patch_name: str = "",
) -> str:
    u = scene.mesh_units
    preset_note = f" preset={preset}," if preset else ""
    isolate_note = f" (isolated: {patch_name})" if patch_name else ""
    return (
        f"Camera{preset_note} centred on ({x:.4g}, {y:.4g}, {z:.4g}) {u}, "
        f"span={span:.4g} {u}.{isolate_note} "
        f"Pan step recalibrated to {scene.pan_step_mm:.4g} {u}."
    )


# prepared cuts: pictures of one plane, with the window stated
#
# A CUT IS NOT THE VOLUME, and every line below exists to keep the reviewer from reading one as
# the other. A section cut from the volume can be re-aimed and zoomed; a prepared frame is one
# plane at one magnification, taken where the mesh was built because the interior was too large
# to bring here. Told nothing, a model describes the part from the frame it was handed and is
# right by accident or wrong without knowing. So each frame is described by what it covers, at
# what resolution, and how many pixels across the mesh's own cell that works out to.

#: Said about a frame no cell could be counted in. Capitalised because it is the one sentence in
#: this block whose loss turns an honest "I could not see the cells" into a confident cell count.
CANNOT_COUNT = ("CELLS ARE NOT COUNTABLE IN THIS FRAME - do not state a cell count, a layer "
                "count or a channel width from it")


def cut_frame_line(cut) -> str:
    """One frame, as a line: what it covers, WHERE, what was measured in it, and what it cannot
    answer.

    EVERY FIGURE HERE IS EITHER THE WINDOW THE PRODUCER STATES OR SOMETHING MEASURED OFF THE
    DRAWN PNG, and the line says which is which. That distinction is the whole change. A frame
    used to be introduced with "cells are countable in this frame" on the strength of arithmetic
    about the plan - and that arithmetic cannot come out any other way, so three pictures with no
    passage in them between them were all introduced with those words. If a reader of this line
    cannot tell a grid of cells from a picture of one port bore, this function has failed.
    """
    bits = [f"covers {cut.window_m[0] * 1e3:.4g} x {cut.window_m[1] * 1e3:.4g} mm of the plane"]
    # WHERE IT SITS. The producer re-aims the zoom at the densest patch of the cut AFTER the plan
    # is written, so this is the only record of where the camera ended up - and a reader of a
    # 4.5% window who is not told WHICH 4.5% cannot check anything in it.
    place = cut.centre_text
    if place:
        bits.append(f"centred at {place}"
                    + (f", which is {cut.centre_source}" if cut.centre_source else ""))
    else:
        bits.append("WHERE IN THE PLANE THIS WINDOW SITS WAS NOT PUBLISHED, so what is in it "
                    "cannot be put back on the part")
    bits.append(f"drawn {cut.pixels[0]}x{cut.pixels[1]} px at {cut.um_per_px:.4g} um per pixel")
    # WHAT THE PLAN SAYS, NAMED AS THE PLAN. This figure is quality.near_wall_cell_m, which the
    # engine derives from the plan and not from the mesh; this mesh's cells span a factor of 87.
    # "The mesh's own cell" was never a true description of it.
    if cut.cell_m:
        clause = (f"a cell of the size the PLAN pinned ({cut.cell_m * 1e6:.4g} um) would be "
                  f"{cut.cell_px:.2f} px across here")
        lo, hi = cut.cell_span_m
        if lo and hi:
            clause += (f", though that is a plan figure and not a measurement of this mesh, "
                       f"whose cells run {lo * 1e6:.4g} to {hi * 1e6:.4g} um")
        bits.append(clause)
    # Only when the window was sized from something OTHER than that cell. The producer sizes its
    # zoom from the cell itself, and printing one number twice in different words is how a reader
    # starts believing two independent facts agree.
    if cut.feature_m and abs(cut.feature_m - cut.cell_m) > 1e-12:
        bits.append(f"the {cut.feature_m * 1e6:.4g} um feature this window was sized from is "
                    f"{cut.feature_px:.2f} px across")
    bits.append(_measured_clause(cut))
    if cut.note:
        bits.append(cut.note)
    bits.append("cells are countable in this frame" if cut.counts_cells
                else f"{CANNOT_COUNT} ({_why_not(cut)})")
    return f"frame '{cut.scope}': " + "; ".join(bits) + "."


def _measured_clause(cut) -> str:
    """What was MEASURED in the drawn picture, as against computed about the plan."""
    ink = getattr(cut, "ink", None)
    if ink is None:
        return ("MEASURED IN THE PICTURE: nothing - this frame could not be read, so no claim "
                "about what is in it can be checked")
    out = (f"MEASURED IN THE PICTURE ITSELF: {ink.ink_fraction * 100.0:.2f}% of the frame has "
           f"anything drawn in it at all, and {ink.cells_on_best_row} cell(s) are resolved on "
           "its best row")
    # THE DRAWN SPACING, which is the figure that tells cells from something eight times bigger.
    # A frame of this part's channels resolves 39 shapes 201 um apart and calls none of them a
    # cell.
    #
    # AND IT NAMES THE AXIS IT WAS READ ALONG, WHICH IS ROUND 10'S CORRECTION AND THE LAST PLACE
    # THIS DEFECT WAS LIVE. This clause published `ink.cell_pitch_px` - the ROW scan, whatever
    # axis the structure in the frame repeats on - and `drawn_cell_m`, which is that same row
    # reading in metres, as "what was resolved is drawn N.N px apart, which is N.NN um", naming
    # no axis at all. Every other figure on this line is counted in the cell drawn on the axis
    # the structure REPEATS on: `cells_resolved` is that cell against the window's, and
    # `wall_cells` and `period_cells` both divide by it. So on a vertically repeating frame the
    # opening of the line quoted one axis and the rest of it quoted the other, and the sentence
    # named neither - which means nothing in it was false and nothing in it could be checked.
    #
    # MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the production
    # ArtifactResolver: cells 8 px ACROSS by 4 px DOWN repeating VERTICALLY, at passages of 4, 8
    # and 12 cells by walls of 2, 4 and 8 cells, are ADMITTED at all nine shapes and published
    # "drawn 8.0 px apart, which is 50.32 um" - exactly twice the planned cell - while the cell
    # the verdict was taken on, and the cell the wall and the period on the same line are counted
    # in, is 4.0 px and 25.16 um. A reader asking whether the drawn cell matches the window was
    # being told NO on a frame the gate admitted as YES. This is the same defect class round 8
    # closed in the gate and round 9 closed in the refusal log, surviving in the one place that
    # speaks to the reviewer about an ADMITTED frame.
    #
    # BOTH READINGS, EACH NAMED, rather than a swap. The row figure is a real measurement of the
    # picture and the two disagreeing is how this part's anisotropy becomes visible at all, so
    # the repeat-axis figure leads - it is the one everything else on the line is counted in -
    # and the row figure follows it, named as the row, only when it is a different number. ON AN
    # ISOTROPIC FRAME NOTHING IS ADDED: the two readings are equal and the second clause is
    # silent. NO GATE IS TOUCHED. Nothing in the product read `drawn_cell_m` except this clause,
    # and `drawn_cell_repeat_m` is `repeat_pitch_px` in metres, which was already under the
    # verdict before this round.
    repeat_px = getattr(cut, "repeat_pitch_px", 0.0) or 0.0
    repeat_m = getattr(cut, "drawn_cell_repeat_m", 0.0) or 0.0
    # `repeat_axis` IS NOT EVIDENCE THAT ANYTHING REPEATS, and reading it as though it were put a
    # claim on 28 of 374 frames that the same line had already refused. `picture_structure`
    # publishes the scan name UNCONDITIONALLY - its chooser is
    # `next((f for f in found if f["passes"]), max(found, key=...))`, and the `max` arm runs
    # precisely when NO axis passes - so the field is non-empty on every frame that has pixels at
    # all, measured 374 of 374. The fact being asserted here is that a structure was FOUND, and
    # the only field that carries it is `shows_structure`. Measured against it, the 28 frames that
    # read "NO PERIODIC STRUCTURE IS IN THIS FRAME as measured here" and then named the axis it
    # repeated on - 18 of them publishing a period of 0.0 and 4 a wall of 0.0 - now take the
    # honest branch below instead, which was unreachable while `axis_name` guarded it.
    axis_name = (getattr(ink, "repeat_axis", "") or "") if ink.shows_structure else ""
    if ink.cell_pitch_px or repeat_px:
        out += f", {ink.cell_rows} row(s) carry a countable run"
        if repeat_px > 0.0 and axis_name:
            out += (f", and what was resolved is drawn {repeat_px:.1f} px apart along the "
                    f"{axis_name} axis the structure repeats on")
            if repeat_m:
                out += f", which is {repeat_m * 1e6:.4g} um"
            out += (" - and that is the cell this frame's own period and wall are counted in, "
                    "and the one held against the size the window declares")
            if ink.cell_pitch_px and abs(ink.cell_pitch_px - repeat_px) > 0.01:
                out += (f" (along its ROWS the same ink is drawn {ink.cell_pitch_px:.1f} px "
                        "apart"
                        + (f", which is {cut.drawn_cell_m * 1e6:.4g} um" if cut.drawn_cell_m
                           else "")
                        + ", so the cells here are not square; the two readings are of two "
                        "different directions and only the first bears on anything else in "
                        "this line)")
        elif ink.cell_pitch_px:
            # NO CELL ON THE REPEAT AXIS, or no measured structure to have one. Either way the
            # row reading is all there is, so it is published AS the row reading and the line
            # says plainly that it is not a figure this frame's period or wall can be counted
            # in - which is the same thing `period_was_measured` says further down, said here
            # because this is where the number appears.
            out += (f", and what was resolved is drawn {ink.cell_pitch_px:.1f} px apart ALONG "
                    "ITS ROWS")
            if cut.drawn_cell_m:
                out += f", which is {cut.drawn_cell_m * 1e6:.4g} um"
            if axis_name:
                out += (f" - and no cell was resolved along the {axis_name} axis the structure "
                        "repeats on, so this figure is not one this frame's period or wall "
                        "could be counted in")
            else:
                out += (" - and no periodic structure was measured in this frame, so there is "
                        "no repeat axis for a cell to be read along")
    # AND WHETHER THE PASSAGE ARRAY IS IN IT, which is the other half of the question: the count
    # above says whether CELLS are resolved, this says whether the frame landed on the PASSAGES at
    # all. A frame can hold forty channels and not one resolvable cell, and a slice through a
    # plenum can hold a healthy grid of cells and no passage - so neither reading covers the
    # other, and a reader told only one of them can still be badly wrong about what it is at.
    #
    # SAID AS THIS SIDE'S OWN MEASUREMENT, because that is what it now is. It used to be quoted
    # as "the side that drew this frame measured...", read out of the manifest, and that boolean
    # was what discharged the region. It is measured here now, on this side's copy of the PNG.
    #
    # AND THERE ARE FOUR ANSWERS TO IT, NOT TWO, which is the correction this round makes. The
    # old sentence had one branch for "the PASSAGE ARRAY IS IN THIS FRAME" and one for "NO
    # PASSAGE ARRAY", and the first fired on ANY periodic structure. MEASURED: a wireframe
    # lattice at 8 px per cell (23.4% ink, 159 repeats, period 8.0 px, 0.994 of a perfect array,
    # ZERO enclosed cell interiors) was introduced to the reviewer as "The PASSAGE ARRAY IS IN
    # THIS FRAME as measured here: 159 repeats, every 8.0 px, at 0.994 of a perfect array".
    # There is no passage in it at all and the 8.0 px period is the cell lattice.
    #
    # So the three states a reader has to be able to tell apart are named apart: nothing periodic
    # here; a periodic structure with NOT ENOUGH METAL between its passages to be a picture of
    # them; and a periodic structure with real metal between its repeats and countable cells
    # across it. And a fourth, which is silence rather than either answer - something periodic in
    # a frame with no drawn cell to count its period in, where the question cannot be asked at all.
    #
    # AND NONE OF THEM SAYS THE ARRAY IS THIS PART'S ANY MORE, which is this round's correction
    # and is a deletion rather than a softening. The grant branch opened "THIS PART'S PASSAGE
    # ARRAY IS IN THIS FRAME" after round 6 deleted the period band, on the ground that the wall
    # floor kept this plate's other regular features out. THAT GROUND IS REFUTED, measured,
    # SYNTHETIC, VOLUME, 1280x960: drawn with an 8 px rib - two drawn cells - BOTH of the other
    # features `picture_structure.PERIOD_MATCH` names discharge, 402.525 um at 87.5% ink with a
    # 2.00-cell wall and 805.050 um at 93.75% ink with a 2.00-cell wall. Nothing on this side now
    # holds the pitch to the part, so the sentence says what was MEASURED - an array, its pitch,
    # its wall - and says plainly that whose array it is was not measured. A claim no surviving
    # measurement supports is worse than no claim: it is the one a reviewer would rely on.
    #
    # AND THE OPENING NO LONGER CALLS IT A PASSAGE ARRAY EITHER, which is round 8's correction to
    # the same sentence. The clause above denied whose array it was while the words in front of it
    # still said "A PASSAGE ARRAY WITH METAL BETWEEN ITS PASSAGES IS IN THIS FRAME" - and the
    # frames that refute the claim are not passage arrays at all. They are fields of coolant with
    # a two-cell rib, which have no passage in them. What is measured is a periodic structure, a
    # pitch, a countable cell and a run of metal between two repeats, and that is now what the
    # opening says.
    if not ink.shows_structure:
        out += (". NO PERIODIC STRUCTURE IS IN THIS FRAME as measured here"
                + (f" ({ink.structure_why})" if ink.structure_why else ""))
    else:
        # EVERY FIGURE BELOW NAMES THE AXIS IT CAME OFF, which is the correction this round makes
        # and is not cosmetic. The repeat is found on whichever of the frame's two axes shows it
        # and the drawn cell used to be measured only along ROWS, so a VERTICAL period was being
        # divided by a HORIZONTAL cell: measured, passages running across the frame at a 32 px
        # vertical period drawn with cells 4 px wide read 8.00 drawn cells per period where the
        # truth in the direction the structure repeats is half a cell. The sentence that reported
        # it named neither axis, so nothing in it was false and nothing in it could be checked.
        axis = getattr(ink, "repeat_axis", "") or "measured"
        seen = (f"{ink.repeats} repeats along the {axis} axis of this frame, every "
                f"{ink.period_px:.1f} px, at {ink.strength:.3f} of a perfect array, over "
                f"{ink.extent_fraction * 100.0:.2f}% of the frame")
        pitch = getattr(cut, "repeat_pitch_px", 0.0)
        if not getattr(cut, "period_was_measured", False):
            out += (f". A PERIODIC STRUCTURE IS IN THIS FRAME as measured here: {seen}. WHETHER "
                    "IT IS THIS PART'S PASSAGE ARRAY WAS NOT MEASURED - no cell interior was "
                    f"resolved along the {axis} axis, so there is no drawn cell to count that "
                    "period in, and the period itself may be anything the picture happens to "
                    "repeat at including the cell lattice. Do not call it a passage")
        elif getattr(cut, "shows_metal_between_passages", False):
            # WHAT WAS ESTABLISHED, AND NOT ONE WORD MORE. This branch used to open "A PASSAGE
            # ARRAY WITH METAL BETWEEN ITS PASSAGES IS IN THIS FRAME", and the three things it
            # has actually measured are that the ink repeats at a measured pitch, that cells are
            # countable across it, and that a measured run of metal stands between two repeats.
            # "A PASSAGE ARRAY" is a fourth thing, and it is the one nothing here establishes:
            # MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell, through the production
            # ArtifactResolver, a solid field of coolant with an 8 px (two-cell) BARE RIB every
            # 32, 64 and 128 px reads 39, 19 and 9 repeats at 1.000 of a perfect array with a
            # 2.00-cell wall and discharges its region - and 64 px and 128 px are this plate's own
            # 402.525 um and 805.050 um features, four and eight times its passage pitch. There is
            # no bank of passages in any of the three. The clause after the figures has said so
            # since round 7; the OPENING still asserted the thing the clause denied.
            # AND NOT A WORD ABOUT COUNTABILITY HERE EITHER, which is where the first draft of
            # this sentence went wrong. "AND CELLS ARE COUNTABLE ACROSS IT" in the opening
            # contradicted the end of the very same line on a frame drawn at twice the
            # magnification it declares: that frame has metal between its repeats and a 4.00-cell
            # wall, so it reaches this branch, and it closes with "CELLS ARE NOT COUNTABLE IN
            # THIS FRAME". Countability is said once, at the end of the line, where it is
            # conditioned on `counts_cells`.
            #
            # AND WHEN NO CELL WAS COUNTED THE FIGURES ARE IN PIXELS, which is round 9's
            # correction to this branch and is a NARROWING of what one line asserts, not a
            # change to what any frame is worth. THE GATE MOVED TO THE REPEAT AXIS IN ROUND 8
            # AND THESE WORDS DID NOT FOLLOW IT. `shows_metal_between_passages` is measured in
            # `repeat_pitch_px` and says nothing about whether that drawn cell is the size this
            # frame's window declares; `cells_resolved` is the test that asks THAT, and it is
            # what `counts_cells` - the end of this same line - is conditioned on. So this
            # branch opened on a frame the gate REFUSES, published a period and a wall IN DRAWN
            # CELLS, held the wall against "the 4.0 cells the plan puts across this part's thin
            # wall", and then closed with "CELLS ARE NOT COUNTABLE IN THIS FRAME - do not state
            # a cell count". MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell,
            # through the production ArtifactResolver: `write_passages(cells_px=8,
            # period_px=64)`, a cell 8 px SQUARE declared at a window that makes a planned cell
            # 4 px, reaches here with a 4.00-cell wall and `cells_resolved` False. A cell count
            # divided by a drawn cell the same line says is wrong is not a measurement, so when
            # the cell was not resolved every figure below is a PIXEL figure: the period in px,
            # the narrowest metal in px, no "- N.NN cell(s)" suffix, no "N.NN drawn cell(s) per
            # period", and no comparison against the plan's cell count. Nothing about which
            # frames discharge their region moves: both forms of the sentence are said about
            # exactly the frames this branch was already said about.
            whose = (" WHETHER THAT PITCH IS THIS PART'S PASSAGE PITCH WAS NOT ESTABLISHED "
                     "HERE: nothing in this frame holds the pitch to the part, and this plate's "
                     "other regular features - 402.525 um and 805.050 um, four and eight times "
                     "its passage pitch - clear every test above when they are drawn with a "
                     "two-cell rib, as does a field of coolant with a two-cell rib in it and no "
                     "passage at all.")
            if getattr(cut, "cells_resolved", False):
                out += (f". A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME "
                        f"as measured here: {seen}, which is "
                        f"{cut.period_cells:.2f} drawn cell(s) per period at the {pitch:.1f} px "
                        f"cell drawn on that axis, and the narrowest metal between two of its "
                        f"repeats is {_bare_min(ink):.1f} px - {cut.wall_cells:.2f} cell(s)"
                        + _typical(cut, ink)
                        + f", against the {PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts "
                        "across this part's thin wall." + whose + " The pitch, the cell and the "
                        "wall are reported; whether they are the passages you were asked about "
                        "is yours to judge from them and the plan")
            else:
                out += (f". A PERIODIC STRUCTURE WITH METAL BETWEEN ITS REPEATS IS IN THIS FRAME "
                        f"as measured here: {seen}, and the narrowest metal between two of its "
                        f"repeats is {_bare_min(ink):.1f} px" + _typical_px(ink)
                        + ". EVERY FIGURE IN THIS SENTENCE IS IN PIXELS AND NONE OF THEM IS IN "
                        "CELLS, because no cell of the size this frame's window declares was "
                        f"resolved along the {axis} axis: neither the period nor the metal can "
                        "be counted in cells here, and neither can be held against the cell "
                        "count the plan puts across this part's thin wall." + whose + " The "
                        "pitch and the metal are reported in pixels and nothing in them is a "
                        "cell count; what this frame is of is yours to judge from them and the "
                        "plan")
        else:
            # AND THIS BRANCH IS SPLIT ON `cells_resolved` TOO, WHICH IS ROUND 10'S CORRECTION
            # AND IS THE SAME NARROWING ROUND 9 MADE TO THE GRANT BRANCH TWENTY LINES ABOVE.
            # Round 9 split that one and did not reach this one. The reason is identical:
            # `shows_metal_between_passages` is measured in `repeat_pitch_px`, the cell actually
            # drawn on the repeat axis, and says NOTHING about whether that cell is the size the
            # window declares; `cells_resolved` is the test that asks THAT, and it is what
            # `counts_cells` - the end of this same line - is conditioned on. So this branch was
            # publishing "N.NN drawn cell(s) per period", a "- N.NN cell(s)" wall suffix and TWO
            # cell-count comparisons on a line that ends "CELLS ARE NOT COUNTABLE IN THIS FRAME
            # - do not state a cell count". Three false cell figures per frame.
            #
            # MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the
            # production ArtifactResolver, on the unsplit form: eight frames reach here with
            # `cells_resolved` False and all eight carry the three figures -
            # `write_row_coarse_col_fine(cell_w=2, cell_h=4)` at passage 8c and 12c by a
            # one-cell wall (9.00 and 13.00 drawn cells per period, 1.00-cell wall, at a 2.0 px
            # drawn cell against a claimed 4.00), `write_anisotropic_passages` at cell_w 8 and
            # 16, and `write_horizontal_passages(cell_w=4, cell_h=8 and 16)` repeating
            # VERTICALLY. A 322-frame sweep hit this branch zero times, which is why round 9
            # missed it, so it is pinned by a test of its own
            # (`test_the_words_claim_only_what_was_established`).
            #
            # WHAT THE PIXEL FORM DROPS AND WHAT IT KEEPS. It drops the period in drawn cells,
            # the "- N.NN cell(s)" suffix and both comparisons - against WALL_CELLS_MIN and
            # against PLAN_CELLS_ACROSS_A_WALL - because all three are divided by a drawn cell
            # the same line says is not the one the window declares, and uses `_typical_px` for
            # the two-kinds-of-wall clause. It KEEPS the verdict in the opening, because the
            # verdict is not in doubt: the floor was applied to the cell actually drawn, which is
            # a measurement of the picture, and the frame is refused a second time over by the
            # drawn cell disagreeing with the window. NOTHING ABOUT WHICH FRAMES DISCHARGE THEIR
            # REGION MOVES: both forms are said about exactly the frames this branch was already
            # said about.
            if getattr(cut, "cells_resolved", False):
                out += (f". A PERIODIC STRUCTURE IS IN THIS FRAME BUT THERE IS NOT ENOUGH METAL "
                        f"BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM: {seen}, which is "
                        f"{cut.period_cells:.2f} drawn cell(s) per period at the {pitch:.1f} px "
                        f"cell drawn on that axis, and the metal between two passages measures "
                        f"{_bare_min(ink):.1f} px at its narrowest - {cut.wall_cells:.2f} "
                        f"cell(s)" + _typical(cut, ink)
                        + f", against the {WALL_CELLS_MIN:.1f} cells of wall a picture of this "
                        f"part has to show and the {PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan "
                        "puts across its thin wall. A field of coolant with a rib in it repeats "
                        "exactly like a bank of passages, so a count taken off this would be a "
                        "count of nothing")
            else:
                out += (f". A PERIODIC STRUCTURE IS IN THIS FRAME BUT THERE IS NOT ENOUGH METAL "
                        f"BETWEEN ITS PASSAGES TO BE A PICTURE OF THEM: {seen}, and the metal "
                        f"between two passages measures {_bare_min(ink):.1f} px at its "
                        f"narrowest" + _typical_px(ink)
                        + ". EVERY FIGURE IN THIS SENTENCE IS IN PIXELS AND NONE OF THEM IS IN "
                        "CELLS, because no cell of the size this frame's window declares was "
                        f"resolved along the {axis} axis: neither the period nor the metal can "
                        "be counted in cells here, so neither can be held against either of the "
                        "cell-count floors a picture of this part is judged by. The floor this "
                        "frame falls under was applied to the cell ACTUALLY DRAWN along that "
                        "axis, which is not the cell the window declares - so the shortfall is "
                        "established and the figure behind it is not a cell count anything can "
                        "be checked against. A field of coolant with a rib in it repeats exactly "
                        "like a bank of passages, so a count taken off this would be a count of "
                        "nothing")
            if _bare_min(ink) <= 0.0:
                out += (f" (and no sample along the {axis} axis is bare at all, so there is no "
                        "metal in this frame anywhere - whatever varies in it, it is not a wall)")
        # AND WHEN THE STATED SCALE DISAGREES WITH THE DRAWN ONE, say so in EITHER case, which is
        # a correction: this used to be said only when the frame was already being called not the
        # part's, and the frame it was written for now reads as holding the array. A coarser array
        # drawn with the right number of cells per period reads 8.00 drawn cells - the part's own
        # shape, with the part's own wall - and 16.00 in the cell the stated window implies, so
        # the figures above look like agreement and the magnification is what is wrong.
        #
        # AND IT TOO IS SAID IN PIXELS WHEN NO CELL WAS COUNTED, for the reason the branch above
        # is: on such a frame the two cell counts this used to quote are both divided by a drawn
        # cell the same line says disagrees with the window, so neither is a measurement of
        # anything. The DISAGREEMENT is what this clause exists to publish and it is measurable
        # without either of them - the drawn cell in pixels against the pixels the stated window
        # makes a planned cell - so that is what is published, and the sentence that follows is
        # unchanged because it was already the true one.
        claimed = getattr(cut, "period_cells_claimed", 0.0)
        if (getattr(cut, "period_was_measured", False) and claimed
                and abs(claimed - cut.period_cells) > 0.01):
            if getattr(cut, "cells_resolved", False):
                out += (f" (and the stated window makes that period {claimed:.2f} cell(s), not "
                        f"{cut.period_cells:.2f}, so the two readings of this frame's own scale "
                        "do not agree: one of the drawn cell and the declared window is wrong, "
                        "and nothing in the picture says which)")
            else:
                out += (f" (and what was resolved along the {axis} axis is drawn {pitch:.1f} px "
                        f"apart while the stated window makes a planned cell {cut.cell_px:.2f} "
                        "px, so the two readings of this frame's own scale do not agree: one of "
                        "the drawn cell and the declared window is wrong, and nothing in the "
                        "picture says which)")
    # AND WHAT THE SIDE THAT DREW IT SAID, named as theirs and only when they disagree with this
    # side or refused the frame. A claim that agrees adds nothing a reader needs.
    if cut.claimed_structure is False:
        out += (". The side that drew this frame REFUSED it as evidence"
                + (f": {cut.claimed_why}" if cut.claimed_why else ""))
    elif cut.claimed_structure is None:
        out += (". The side that drew this frame published no measurement of its own, which "
                "neither adds to nor subtracts from the figures above")
    return out


def _bare_min(ink) -> float:
    """The NARROWEST bare run in the frame, in pixels, with the median as the fallback.

    The fallback is for a replayed measurement taken before the narrowest was recorded: the two
    are equal wherever the walls are uniform, which is every frame measured before this round.
    """
    v = float(getattr(ink, "bare_run_min_px", 0.0) or 0.0)
    return v if v > 0.0 else float(getattr(ink, "bare_run_px", 0.0) or 0.0)


def _typical(cut, ink) -> str:
    """ ", typically N px - M cell(s)" when the walls in this frame are NOT all one width.

    SAID ONLY WHEN THE TWO FIGURES DISAGREE, because that disagreement is the whole content: it
    says the walls here are of two kinds, and the floor is applied to the narrower. MEASURED,
    SYNTHETIC, VOLUME, 1280x960 at 4 px per cell: walls alternating 16 px and 1 px read 1.0 px
    and 0.25 cells at their narrowest against 8.5 px and 2.125 typical - and the typical figure,
    the median of a bimodal set, is a width neither wall in the picture has. A reader shown only
    it is being shown a wall that is not there; a reader shown only the narrowest cannot see that
    most of the frame is sound.
    """
    median = float(getattr(cut, "wall_cells_median", 0.0) or 0.0)
    if median <= 0.0 or abs(median - float(getattr(cut, "wall_cells", 0.0) or 0.0)) <= 0.01:
        return ""
    return (f", though the walls here are not all one width and a typical one is "
            f"{float(getattr(ink, 'bare_run_px', 0.0) or 0.0):.1f} px - {median:.2f} cell(s)")


def _typical_px(ink) -> str:
    """The same disagreement, IN PIXELS ONLY, for a frame that resolved no cell to count in.

    It says exactly what `_typical` says - the walls in this frame are of two kinds and the floor
    is applied to the narrower - with the cell figure left out, because on a frame whose drawn
    cell does not agree with the window it declares there is no cell for either figure to be
    counted in. The comparison it is made on is the two PIXEL runs, not the two cell counts, so
    it answers on exactly the frames `_typical` answers on wherever a cell exists at all.
    """
    typical = float(getattr(ink, "bare_run_px", 0.0) or 0.0)
    narrowest = _bare_min(ink)
    if typical <= 0.0 or abs(typical - narrowest) <= 0.01:
        return ""
    return (f", though the walls here are not all one width and a typical one is "
            f"{typical:.1f} px")


def _why_not(cut) -> str:
    """Which of the tests this frame failed, because the repairs are all different ones."""
    ink = getattr(cut, "ink", None)
    if ink is None:
        return "the frame could not be read"
    if cut.cell_px < COUNTABLE_CELL_PX:
        return (f"a cell of the planned size is only {cut.cell_px:.2f} px across in this window, "
                f"under the {COUNTABLE_CELL_PX:.0f} px counting one needs")
    if not ink.shows_structure:
        return ("the window is sized correctly and this frame's own pixels hold no passage "
                "array, so the cut did not land on the thing it exists to show"
                + (f": {ink.structure_why}" if ink.structure_why else ""))
    if cut.claimed_structure is False:
        return ("the window is sized correctly and an array is in the picture, but the side that "
                "drew this frame refused it - and its refusal is taken"
                + (f": {cut.claimed_why}" if cut.claimed_why else ""))
    if (getattr(cut, "cells_resolved", False)
            and not getattr(cut, "shows_metal_between_passages", True)):
        # THE CELLS ARE HERE AND THE METAL IS NOT, and the repair is a different one again: the
        # window is right, the cells are drawn, the plane crossed SOMETHING regular - and there is
        # no wall in it. Said in PIXELS as well as cells, and with the axis named, because a bare
        # run of 0 px used to read as a four-cell wall. AFTER the producer's veto above, because a
        # vetoed frame has no measured array on this side either and would otherwise be reported
        # here under the wrong reason.
        # A FRAME WITH NO CELL ON THE AXIS ITS STRUCTURE REPEATS ON DOES NOT REACH HERE ANY MORE,
        # and the sentence it used to be given has moved to the tail of this function. Round 8
        # made `cells_resolved` read the cell drawn on the repeat axis, so a frame with no cell on
        # that axis fails it - which is the honest answer, because nothing in such a frame can be
        # counted in cells at all. Keeping the branch here would be unreachable text.
        axis = getattr(ink, "repeat_axis", "") or "measured"
        return (f"the cells are in the picture and the metal between passages is not: along the "
                f"{axis} axis the structure repeats every {cut.period_cells:.2f} drawn cell(s) "
                f"and the narrowest metal between two passages measures {_bare_min(ink):.1f} px, "
                f"which is {cut.wall_cells:.2f} cell(s)" + _typical(cut, ink) + f", against the "
                f"{WALL_CELLS_MIN:.1f} a picture of this part's wall has to show and the "
                f"{PLAN_CELLS_ACROSS_A_WALL:.1f} cells the plan puts across it, so this is a "
                "field of coolant and not a bank of passages")
    if ink.ink_fraction < EVIDENCE_INK_FLOOR:
        # THE FRAME IS ESSENTIALLY EMPTY. Named separately because every other refusal here is
        # about what was drawn and this one is about how little of it there is: a frame of 525
        # inked pixels in 1,228,800 resolved six interiors and discharged a region.
        return (f"only {ink.ink_fraction * 100.0:.4f}% of this frame has anything drawn in it at "
                f"all, under the {EVIDENCE_INK_FLOOR * 100.0:.4f}% a cut of a region needs, so "
                "whatever was counted in it was counted off a nearly blank picture")
    if ink.cells_on_best_row < COUNTABLE_CELLS_ON_A_ROW:
        return ("the window is sized correctly and the cells are not in the picture: "
                f"{ink.cells_on_best_row} cell(s) were resolved anywhere in it against the "
                f"{COUNTABLE_CELLS_ON_A_ROW} one passage needs, so whatever this frame is of, "
                "it is not an array of cells")
    if ink.cell_rows < COUNTABLE_CELL_ROWS:
        # A LINE, NOT AN ARRAY. The pitch test below catches the same frame from the other side on
        # this part; this one catches it even when the spacing happens to agree.
        return (f"the cells were resolved on only {ink.cell_rows} scan row(s) of "
                f"{cut.pixels[1]}, under the {COUNTABLE_CELL_ROWS} an array "
                f"{COUNTABLE_CELLS_ON_A_ROW} cells deep draws, so this is a picture of a line "
                "and not of an array")
    # THE DRAWN CELL THAT FAILED IS THE ONE ON THE AXIS THE STRUCTURE REPEATS ON, and this
    # sentence used to quote the ROW whatever axis the frame repeated on. MEASURED, SYNTHETIC,
    # VOLUME, 1280x960: a frame of passages running across the frame with cells 4 px wide and
    # 64 px tall resolves no interior down any column, so there is no cell on the axis its wall
    # and its period are counted on - and the sentence read "what was resolved is drawn 4.0 px
    # apart against the 4.00 px a cell should be, so it is not cells", which quotes two figures
    # that AGREE and concludes from them that they do not.
    axis = getattr(ink, "repeat_axis", "") or "measured"
    pitch = getattr(cut, "repeat_pitch_px", 0.0)
    if pitch <= 0.0:
        return (f"the cells are in the picture along its rows and NOT along the {axis} axis the "
                f"structure repeats on: {ink.bare_run_px:.1f} px of metal stands between its "
                "passages and there is no cell drawn in that direction to count it in, so "
                "neither the period nor the wall could be put in cells at all")
    # AND WHAT THAT DISAGREEMENT ESTABLISHES IS THE DISAGREEMENT, NOT WHICH SIDE OF IT IS WRONG.
    # This used to close "so it is not cells - it is something larger or smaller that happens to
    # repeat", and no measurement supports that: on an HONEST frame with this part's own
    # anisotropy what was resolved IS cells, drawn at a size the declared window does not match.
    # MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell:
    # `write_anisotropic_passages(cell_w=8, cell_h=4)` - the planner pins the fine cell AT the
    # thin wall and lets it run long the other two ways, so this is the part's ordinary shape -
    # reads 8.0 px along the axis it repeats on and was told "it is not cells". It is cells. What
    # the measurement establishes is that the drawn reading and the declared one disagree, and
    # `_measured_clause` twenty lines above has said that correctly since round 6: one of the two
    # is wrong and nothing in the picture says which. The same words are used here. The frame is
    # refused either way, so no verdict moves with this sentence.
    return (f"what was resolved along the {axis} axis the structure repeats on is drawn "
            f"{pitch:.1f} px apart against the {cut.cell_px:.2f} px the stated window makes a "
            "planned cell, so the two readings of this frame's own scale do not agree: one of "
            "the drawn cell and the declared window is wrong, and nothing in the picture says "
            "which - and the wall and the period this frame would publish are counted in the "
            "drawn one")


def prepared_cuts_block(cuts) -> str:
    """The INTERNAL CUTS section of the opening message, or "" when there are no prepared cuts."""
    if not cuts:
        return ""
    by_region: dict[str, list] = {}
    for c in cuts:
        by_region.setdefault(c.region_id, []).append(c)
    lines = [
        "\n\nINTERNAL CUTS (ALREADY TAKEN, where the mesh was built - this mesh's interior was "
        "too large to bring here, so what you can be shown is a PICTURE OF ONE PLANE, not the "
        "volume itself):",
        "  You cannot move the camera inside one of these frames, and anything outside a frame's "
        "window is not in it. inspect_region(region_name) gives you the WHOLE plane, which is "
        "that region's own question and is too coarse to count cells in. The close frame is a "
        "few millimetres of that plane and is the only one an inspection can be judged on: ask "
        "for it by name, inspect_region(region_name, scope=\"zoom\"). Each frame below is "
        "listed with what was MEASURED in the picture itself - how much of it has anything drawn "
        "in it, and how many cells were actually resolved. Judge only what a frame is measured "
        "to contain, and state which frame each finding came from by the evidence id you were "
        "given for it.",
    ]
    for region_id, frames in by_region.items():
        head = f"  {region_id}"
        label = next((f.region_label for f in frames if f.region_label), "")
        why = next((f.region_why for f in frames if f.region_why), "")
        if label:
            head += f" - {label}"
        if why:
            head += f": {why}"
        lines.append(head)
        lines.extend(f"      {cut_frame_line(f)}" for f in frames)
    # THERE IS NO "AND NONE OF THESE RESOLVES ANYTHING" BRANCH HERE, deliberately. A region whose
    # frames cannot show a cell is not offered as a target at all
    # (render_adapter.expand_region_targets says why), so it never reaches this block: that case
    # is a stated decline before the model is asked, in context.build_review_prompt. A branch
    # here would be unreachable text claiming to warn about something.
    return "\n".join(lines)


def move_camera_text(
    scene: SceneContext,
    *,
    direction: str,
    distance_mm: float | None,
) -> str:
    used = distance_mm if distance_mm is not None else scene.pan_step_mm
    return f"Camera moved {direction} by {used:g} {scene.mesh_units}."
