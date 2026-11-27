# Responsibility: Measure whether a DRAWN FRAME holds the periodic passage array a cut exists to
# show, out of that frame's own pixels and nothing else.
# Owns: the ink threshold and the repeat / period / autocorrelation / extent bars a frame has to
# clear, and the one plan figure the period is held against.
# Boundaries: numpy and nothing else. It reads no file, renders nothing, and imports nothing from
# meshpipeline - so the side that DREW a frame and the side that READS it run the same code over
# the same pixels, and neither has to take the other's word for the answer.
# Collaborates with: engines/cfmesh_conjugate/slices.py (which drew these frames on the mesh VM
# and published what it measured) and sandbox/review_session.py (which measures the frame again
# on its own copy and decides whether it discharges a region).
#
# WHY THIS IS NOT IN THE ENGINE ANY MORE. Every line below used to live in
# engines/cfmesh_conjugate/slices.py and run only on the mesh VM, which published its verdict as
# `frames[].content.shows_structure` in the manifest. The home side read that boolean and let it
# DISCHARGE A REGION: `review_session.ResolvedCut.shows_cells` returned False the moment it was
# absent or false and otherwise took it as satisfied, so the one measurement doing the
# load-bearing work was a claim made by the untrusted side of a container boundary.
#
# MEASURED, 2026-11-17, through the production ArtifactResolver and ResolvedCut, on frames I drew:
# a frame of SOLID METAL - a uniform 4 px cell grid filling the whole frame, 100.0000% ink, no
# passage anywhere - resolved 319 enclosed interiors at a 4.0 px pitch, came out at exactly
# 4.00 px per planned cell, and was handed back counts_cells True with `shows_structure: true`
# beside it. The byte-identical PNG with `shows_structure: false` was refused. Every number the
# home side measured was identical in both runs. The manifest boolean was the only separator.
#
# AND THE SAME CODE, RUN FROM THE READING SIDE, SEPARATES THEM. Measured here, 2026-11-17, over
# the real plate's own frames (job 0ff6f9e5, cuts of fluid_to_solid.vtp) and two frames I drew:
#
#     frame                               what is in it              repeats  period  strength
#     solid metal (drawn)                 a plenum-shaped grid             0    0.0      0.000
#     near-blank, 525 px of 1,228,800     one 25x21 patch                  0    0.0      0.000
#     through_the_plate_zoom   REAL       one circular port bore           0   32.0      0.727
#     across_the_flow_zoom     REAL       three plenum rectangles          3   80.0      0.305
#     through_the_plate_whole  REAL       almost nothing                   1    2.0      0.783
#     across_the_flow_whole    REAL       scattered traces                40   86.0      0.238
#     along_the_flow_whole     REAL       scattered traces                38   36.0      0.331
#     ... the bar sits here ...
#     along_the_flow_zoom      REAL       the channel array               79   16.0      1.000
#     a fluid-only volume cut (drawn)     passages, metal between         39   32.0      1.000
#
# THE STRENGTH COLUMN WAS RE-MEASURED on 2026-11-17 after the normalisation below was corrected,
# on the real plate's own six PNGs; the repeats and the periods did not move at all. See the
# ROUND 7 note at the foot of this header.
#
# The two the reading side could not tell from a mesh are the first two, and this refuses both.
# It is pure numpy over a greyscale array, and the reader already holds the same PNG bytes.
#
# AND MOVING THE MEASUREMENT HERE WAS NOT ENOUGH, which is what the constants added below are
# for. An independent review then MEASURED three more pictures with no passage in them that still
# discharged a region, all through the production resolver, all at 1280x960 with 4 px cells:
#
#     frame                                  ink      rep  period  str    best rows  what it is
#     plenum, bare 2 px wall every 32 px  93.7500%      39   32.0  1.000   240  720  metal
#     plenum, bare 2 px wall every 64 px  96.8750%      19   64.0  1.000   280  720  wrong feature
#     12 bands of cells, 384 x 64 px       1.0000%      11   32.0  0.902    36   48  a speck
#     HONEST passages, 16 px of every 32  50.0000%      39   32.0  1.000   120  720  the real shape
#
# THE FIRST AND THE LAST DIFFER IN NOTHING BUT THE INK. Same repeats, same period, same strength,
# same drawn cell, same row count - so the question "is there SOME periodic structure here" was
# answered yes for a field of metal with a rib in it, and no period test can tell those two
# apart. What separates them is the METAL BETWEEN THE PASSAGES (see WALL_CELLS_MIN), and what
# separates the speck is the FOOTPRINT of the repeat (see EXTENT_MIN).
#
# AND THE PERIOD ITSELF IS NO LONGER HELD TO A BAND, which is this round's deletion. Rounds 4 and
# 5 held the period counted in drawn cells to PLAN_CELLS_ACROSS_A_WALL within PERIOD_MATCH, which
# is a bar on PASSAGE WIDTH PLUS WALL WIDTH - and the plan fixes only the second of those. The
# band admitted a 1.43x window in pitch and REFUSED true pictures of real passages: measured,
# SYNTHETIC, VOLUME, passages 4 cells wide with 8, 12 and 20 cells of metal between them, and
# passages 8 and 12 cells wide with 4 cells of metal, were all refused for their pitch while
# showing more wall than the part has. It also could not be satisfied alongside its own write-up,
# which listed a 4.00-drawn-cell reading as the part while the wall floor refused it. The plan
# constrains the wall, so the wall is what is held to the plan and the pitch is reported.
#
# AND THE GROUND THAT DELETION STOOD ON IS REFUTED, which is why the sentence a reviewer reads no
# longer says whose array it is. Round 6 deleted the band on the ground that the wall floor keeps
# this plate's other regular features out. MEASURED, 2026-11-17, SYNTHETIC, VOLUME, 1280x960 at
# 4 px per cell, through the production ArtifactResolver: drawn as a field of coolant with an
# 8 px bare rib - TWO drawn cells, which clears WALL_CELLS_MIN exactly - the 402.525 um feature
# comes back 87.50% inked with a 2.00-cell wall and DISCHARGES its region, and the 805.050 um one
# comes back 93.75% inked with a 2.00-cell wall and DISCHARGES its region. Their periods measure
# 402.525 um and 805.050 um, four and eight times this plate's passage pitch. So nothing on the
# READING side holds the measured pitch to the part, and `agents/reviewer/scene_text.py` says so
# in the words it hands the reviewer instead of opening with "THIS PART'S PASSAGE ARRAY IS IN
# THIS FRAME". PERIOD_MATCH still holds one cut of a mesh to another cut of the SAME mesh, on the
# producer's side, which is a different claim and is still measured.
#
# ROUND 7: THE ARITHMETIC THAT WAS REFUSING TRUE PICTURES. Three figures above were measured
# with defects in them, all arithmetic, none of them a threshold.
#
# 1. THE AUTOCORRELATION WAS NORMALISED OVER THE WHOLE PROFILE AND SUMMED OVER THE OVERLAP, so
#    every lag paid a factor of (1 - k/n) and the peak drifted to the shortest lags. Lag 2 wins
#    outright once passage x wall exceeds about 2n, and the frame is then refused for "the ink
#    repeats every 2.0 px, under the 8 px a passage and its wall need". MEASURED, SYNTHETIC,
#    VOLUME, 1280x960 at 4 px per cell, before and after the fix:
#
#      passage / wall, in drawn cells    8/24   12/16  12/20  12/24  16/12  16/16  16/20  16/24
#      BEFORE: period px                  2.0     2.0    2.0    2.0    2.0    2.0    2.0    2.0
#      BEFORE: strength                 0.918   0.929  0.935  0.939  0.928  0.939  0.945  0.949
#      AFTER:  period px               128.0   112.0  128.0  144.0  112.0  128.0  144.0  160.0
#      AFTER:  strength                1.000   1.000  1.000  1.000  1.000  1.000  1.000  1.000
#
#    Every one of those is a TRUE PICTURE of real passages with real metal between them, and all
#    eight were refused. THE BOUNDARY MOVED WITH THE DUTY CYCLE, which is the signature of this
#    arithmetic and of nothing in any part: a 104 px pitch passed at duty 0.2 and 0.8 and failed
#    at 0.4, 0.5 and 0.6, and a 128 px pitch failed at EVERY duty from 0.2 to 0.8. AND IT MADE
#    THE WHOLE VERTICAL AXIS UNREACHABLE - that profile is 960 samples, so the bound bites sooner
#    - which is the axis round 6 added a cell-pitch measurement down columns for: vertical pitches
#    of 64, 96 and 128 px all read 2.0 px and declined, and all three now read their own pitch at
#    1.000.
#
# 2. `repeat_span` RAN FROM THE FIRST RISING EDGE TO THE LAST, so a non-periodic field supplied
#    both ends of the figure that is meant to disclose it. See `_periodic_run`.
#
# 3. THE WALL WAS THE MEDIAN BARE RUN, which is a width no wall in a bimodal picture has. See
#    `_bare_runs`.
#
# WHAT DID NOT CHANGE, and it is checked rather than assumed: every frame with no metal in it is
# still refused, because that is an occupancy test and none of the three touches it. MEASURED
# through the production ArtifactResolver after all three fixes: the truncated fields of coolant
# at 200, 400 and 470 px, the 2 px and 4 px ribs in a plenum at 32 px and the 2 px rib at 64 px
# all still measure 0.00, 0.50, 1.00 and 0.50 cells of wall and all still decline.
#
# AND HERE IS EVERY METAL-FREE PICTURE THE FIXTURES CAN DRAW, BY NAME AND BY PARAMETER, because
# the count had got away from the pictures. It has been written up as eleven, and two independent
# re-drives found ten and ten - no fixture draws eleven. RE-MEASURED 2026-11-18, SYNTHETIC,
# VOLUME, 1280x960, by two proofs taken off the pixels and not off this module's own wall figure:
# the number of lines along the repeat axis carrying NO INK AT ALL, and the number of 4-connected
# bodies the ink forms (scipy.ndimage.label on the ink mask, 4-connected structure). A picture
# with no metal in it has ZERO blank lines and ONE body.
#
#     picture                                                       ink      inked px   bodies
#     write_truncated_plenum(deep_px=200)                       60.4167%      742,400        1
#     write_truncated_plenum(deep_px=400)                       70.8333%      870,400        1
#     write_truncated_plenum(deep_px=470)                       74.4792%      915,200        1
#     write_wireframe_lattice(cells_px=8)                       23.4375%      288,000        1
#     write_wireframe_lattice(cells_px=4)                       43.7500%      537,600        1
#     write_passages(period_px=10**9, duty=1.0)                100.0000%    1,228,800        1
#
# SIX DISTINCT PICTURES, blank lines 0 of 1280 on every one of them, and all six measure a
# 0.00-cell wall and decline.
#
# ROUND 11: AND THOSE TWO PROOFS DO NOT SEPARATE THE REAL PART, which is this round's finding and
# is why the table above can no longer be read as a certificate. MEASURED, 2026-11-19, REAL
# PLATE, VOLUME, job 6dd6703c-c8d2-43cd-882c-a01c7375d014's through_the_plate zoom, by the SAME
# two proofs: blank lines along the repeat axis 0 of 1280, 4-connected bodies 1. Identical to
# every row above. That frame is a TRUE picture of this plate's channel array - 640 metal columns
# in 41 runs of 16.0 px, which at 6.28945 um/px is 100.63 um, 1.0000 of the plan's own
# 100.6312 um wall - and the two measurements that were being used to prove a picture metal-free
# say exactly the same of it as of a field of coolant. What makes them agree is a TRANSVERSE
# MANIFOLD: every metal column carries 33 inked pixels, all 33 in one band at rows 462..494, so
# no column of 960 rows is bare and no body is separate. The proofs are kept because they are
# what the six rows above were measured with and they are honest about those six; they are not
# evidence that a frame has no metal in it, and this round stops them being quoted as though they
# were. What tells the two apart is WHERE the metal is bare - see `_metal_mask`.
#
# AND THE WALL IS NO LONGER SUMMED DOWN THE WHOLE FRAME, which is the defect that found. Every
# figure in every table above is unmoved: measured through the production ArtifactResolver over
# 28 frames - the six metal-free pictures, the five sub-floor ribs, the anti-aliased frame, the
# speck, the two plenum-beside shapes, the three magnification/anisotropy frames, the vertical
# repeat, and a 35-frame sweep of passages 2..24 drawn cells by walls 1..32 - NOT ONE bare run
# and NOT ONE verdict changed, and no metal-free picture got through. What moved is the real
# plate's own frame, from a 0.0 px wall to 16.0 px.
#
# AND IT WAS NOT ANTI-ALIASING, which is what that refusal looked like. The real frame holds 179
# distinct grey levels and NOT ONE PIXEL between 193 and 254 - its pixel counts under 193, 200,
# 210, 220, 230, 240, 248 and 254 are all exactly 635,520 - so nothing in it blends toward the
# background and no ink threshold could have moved the answer. A SURFACE cut is five greys
# blending to white (see INK_MAX); a VOLUME cut of this engine's own render is a shaded fill
# hard-edged against the background, and the two are different pictures.
#
# THE HEADING CLAIMED MORE THAN THE BODY ESTABLISHED, which is what round 10 corrects here. It
# read "EVERY METAL-FREE PICTURE THE FIXTURES DRAW" over a list of FIVE, and the lattice fixture
# takes its cell size as a parameter: at a FOUR-pixel cell it is the sixth row above, measured on
# the same sweep and refused like the rest. Nothing in the list moved and no verdict moved - a
# heading that overstates its own table is exactly the kind of claim this module's header exists
# to stop, and the fixture's parameter is where it was hiding. "CAN DRAW" is now the claim,
# because that is the one the table supports: it is the parameter space, not the call sites.
#
# AND THE DRAWN-INSTANCE COUNT IS A DIFFERENT NUMBER AND DID NOT CHANGE. The six are driven TEN
# times across tests/unit/sandbox and tests/unit/agents/reviewer - the three truncation depths
# twice over (in `test_a_frame_with_no_metal_in_it_is_still_refused` and in
# `test_the_depth_it_is_truncated_at_changes_nothing`), the 470 one twice more (in
# `test_a_field_of_coolant_with_no_metal_in_it_does_not_discharge_its_region` and in
# `test_the_words_name_the_axis_and_the_metal`), the lattice at cells_px=8 once and the solid
# field once - and ten DRAWN INSTANCES is what the figure of ten counts. THE cells_px=4 LATTICE IS
# DRAWN BY NO TEST, which is why adding it leaves ten at ten: it is drawable and not driven.
# Verified 2026-11-18 by grepping every `write_wireframe_lattice` call in tests/ and src/ - two
# call sites, both `cells_px=8`. All three numbers are stated because none of them can be checked
# against the fixtures alone: six is what can be drawn, ten is what runs, and the gap between
# them is a fixture parameter nobody drives.
#
# THE RIBS ARE NOT IN THAT LIST AND MUST NOT BE COUNTED INTO IT. A rib is real metal that is too
# thin - it leaves blank lines, 20 to 680 of them - and it is refused by WALL_CELLS_MIN on a
# different measurement. MEASURED on the same sweep, 21 frames carry a wall of more than zero
# and under two drawn cells (ribs of 1, 2, 4 and 6 px, walls of one drawn cell, and the bimodal
# pairs whose narrowest run is 1 or 2 px) and all 21 decline.
#
# THE READER HANDS IT THE MINIMUM-CHANNEL ARRAY, not PIL's luma, because that is the array
# `review_session._measure_png` already builds a row at a time while it counts cell runs.
# MEASURED on all twelve frames above: the two arrays give the SAME repeats, the SAME period and
# the SAME strength to three decimals on every one of them, so nothing turns on which is used.
from __future__ import annotations

#: A pixel is INK when it is not the background. The background is set to white, so anything under
#: this is something that was drawn.
#:
#: NOT A DARKNESS THRESHOLD, and that is measured rather than chosen. A cut of the interface
#: SURFACE is a set of lines, and the six frames of job 0ff6f9e5 hold exactly five grey levels -
#: 255, 241, 227, 211 and 193 - because the line colour is lightsteelblue (luma 193) blended
#: toward white by the anti-aliasing. NOTHING in any of them is under 193. A threshold of 128,
#: which is the obvious one, finds zero ink in all six and reports every frame as blank.
#:
#: A cut of a VOLUME is the other shape: filled cells at 193 with their edges blending to 87 and,
#: off the aligned phase, down to 0. There a threshold of 128 finds the edges and loses the cells
#: - the opposite error. One threshold has to see both shapes, and "not the background" is the
#: only one that does.
INK_MAX = 250

#: HOW MANY TIMES THE STRUCTURE MUST REPEAT in the frame before the frame is said to show it.
#:
#: This is the number that rejects the two bad frames above. A single port bore gives one run; the
#: three plenum rectangles give three. The real array gives 79, one for every 100.631 um of wall
#: across an 8.05 mm window, so six is a floor far under anything honest and far over anything
#: accidental.
#:
#: AND IT IS WHAT REJECTS THE DANGEROUS CASE, which no scale and no ink fraction can. A fluid-only
#: volume slice through a PLENUM comes back as a dense, healthy-looking grid of cells with no
#: channel in it. Its ink fraction is high, its cells are four pixels across, its scale is right -
#: and every column of it is inked, so it has one run and no period. That picture is the shape of
#: the failure this whole file exists to stop, and counting repeats is what sees it.
MIN_REPEATS = 6

#: A PERIOD UNDER THIS IS NOT A RESOLVED ARRAY. The structure is a passage and the metal beside it,
#: so one period holds at least two cells; at CELL_PIXELS per cell that is eight pixels. Below it
#: the "period" being measured is as likely to be the cell grid itself, or the rasteriser's own
#: line spacing, as anything in the part. Measured on this plate the period is 16.0 px, twice the
#: bar, over 79 repeats.
#:
#: THE EIGHT IS WRITTEN OUT HERE rather than computed, and that is the one thing this move
#: changed. It was `2 * CELL_PIXELS`, and CELL_PIXELS belongs to the side that SIZES A WINDOW
#: (engines/cfmesh_conjugate/slices.CELL_PIXELS); this module must not import an engine. The four
#: it doubles is the same four `sandbox/review_session.COUNTABLE_CELL_PX` divides a window back
#: by, and the two were already pinned to each other by a test. Both links are pinned again from
#: the reader's side, so a drift fails a test instead of silently refusing every zoom frame the
#: producer draws.
MIN_PERIOD_PX = 8

#: HOW PERIODIC THE INK HAS TO BE, as the peak of the profile's own autocorrelation (0 to 1).
#:
#: THE RUN SPACING ALONE IS NOT ENOUGH and the first version of this used its spread, which was
#: REFUTED on the part it was written for. A real array on a real cold plate is not a perfect
#: grating: the z-normal cut of job 0ff6f9e5 holds a bank of fins, plainly a regular array when
#: you look at it, whose run-spacing spread is 1.33 because the fins come in pairs - against the
#: 2.67 of a whole-part frame that holds nothing but scattered traces. The bar I first set, 0.35,
#: refused both, and there is only a factor of two between them to put a bar in.
#:
#: The autocorrelation asks the right question - how much of this profile repeats at SOME lag -
#: and it separates the same frames with a wider band. MEASURED over twelve frames of this plate,
#: every one looked at by eye, on the axis that also passes the repeat and period bars:
#:
#:     frame                               what it is                      peak
#:     through_the_plate_zoom (y-normal)   the channel array               0.987
#:     along_the_flow_zoom (round 2)       the channel array               0.987
#:     across_the_flow_zoom (z-normal)     a regular bank of coarse fins   0.572
#:     ... nothing at all lands in here ...
#:     along_the_flow_whole                scattered traces                0.219
#:     across_the_flow_whole (round 2)     scattered traces                0.214
#:     along_the_flow_zoom (x-normal)      scattered traces                0.187
#:
#: 0.40 sits in the middle of the 0.219 to 0.572 band. The three frames that pass are the three
#: that hold an array; a port bore and three plenum rectangles never reach this test at all,
#: because they are one run and three. Whether an array is THE array is PERIOD_MATCH's question,
#: not this one - the fins are refused there, at eight times the channel figure.
#:
#: AND THE BAND IS WIDER AFTER ROUND 7, measured, so this bar does not move. The figures above
#: were taken with the biased normalisation (see the header); re-measured on the real plate's own
#: PNGs with the overlap normalisation, the two scattered-trace frames rise from 0.219 and 0.214
#: to 0.331 and 0.238 and the array frame rises from 0.987 to 1.000 - a band of 0.331 to 1.000
#: where it was 0.324 to 0.987. 0.40 still sits inside it, clearing the worst scattered frame by
#: 1.21x where it cleared it by 1.23x, and the array by 2.50x where it was 2.47x. The fins row is
#: NOT re-measured: that drawing is not among the six frames this plate's archive kept, and it is
#: left stated as it was taken rather than guessed at.
ACF_MIN = 0.40

#: HOW FAR A FRAME'S PERIOD MAY BE FROM THE PART'S OWN, as a ratio either way.
#:
#: A regular array is not automatically THE array. MEASURED on job 0ff6f9e5: the cut normal to y
#: shows the microchannel walls repeating every 100.631 um, and two other windows on the same part
#: - both regular, both with enough repeats, both passing every test above - repeat at 402.525 um
#: and 805.050 um, exactly four and eight times the channel figure. They are a coarser feature
#: elsewhere in the plate, and a reviewer asked "how many cells across a passage" would have
#: answered from one of them about the wrong passage.
#:
#: The yardstick is MEASURED, not planned: it is the period read out of the first frame that
#: showed an array, and the other cuts are held to it because they are all meant to be crossing
#: the same passages from different directions. With no such frame there is no yardstick and any
#: resolved array stands.
#:
#: WHY IT IS AS LOOSE AS A FACTOR OF TWO AND A HALF. The same array does not measure the same from
#: every direction: a plane normal to the array sees its WALLS edge-on, every half pitch, while a
#: plane across it sees the PASSAGES, every full pitch. On this plate those are 100.631 um and
#: 201.262 um, measured, so any bar under 2.0 refuses a true picture of the channels for being a
#: picture of the channels. A first attempt at 1.25 did exactly that. The measured features on
#: this part are at 1.0, 1.5, 2.0, 4.0 and 8.0 times the channel figure, so 2.5 sits in the gap
#: between the second and third group and is not near any of them.
#:
#: WHOSE BAR THIS IS NOW. It belongs to `period_matches`, and so to the PRODUCER, which holds one
#: frame's measured period as the yardstick for the other cuts of the same mesh (see
#: `cfmesh_conjugate/native.py`). THE READING SIDE NO LONGER HAS A PERIOD BAND AT ALL: the
#: `period_is_the_parts` form of this, which held a period counted in drawn cells against
#: PLAN_CELLS_ACROSS_A_WALL, is deleted - see the header for the true pictures of real passages it
#: was measured to refuse.
PERIOD_MATCH = 2.5

#: HOW CLOSE A LAG HAS TO BE TO A WHOLE MULTIPLE OF THE RUN SPACING to be read as its harmonic
#: rather than as a period of its own. Job 606af55e's through_the_plate zoom measured a peak at
#: lag 185 against a 62.0 px spacing, which is 2.98, and the synthetic frame built from it
#: (`write_passages_in_groups_of_three`) reads 186 against 62.0, exactly 3.00. A tenth of a period
#: is room for both; a lag at 2.5x or 3.4x of the spacing is not a harmonic of it and keeps the lag.
HARMONIC_TOLERANCE = 0.1
#: ...AND HOW ALIKE THE SPACINGS HAVE TO BE for their median to be believed over the lag. `spread`
#: is (p90 - p10) / median, so this says the middle eight tenths of the spacings sit within a
#: quarter of the median.
#:
#: THIS IS THE BAR THAT DOES THE WORK, and the measured numbers say why it rather than the multiple
#: test above. Job 606af55e's frame read a spread of 0.016 - 21 spacings, every one alike - and the
#: synthetic one reads 0.000. The doublet fins this must not disturb read 0.612, and their lag of
#: 49 px against a 24.5 px median is a whole multiple too, EXACTLY 2.00, so the multiple test alone
#: would have believed the median that was measured the hard way to be wrong. Regularity is what
#: separates the two cases, and it separates them by a factor of forty.
SPACING_REGULAR = 0.25


def is_harmonic(lag: float, spacing: float, spread: float) -> bool:
    """Whether a correlation peak at `lag` is a harmonic of a run spacing rather than a period.

    A profile periodic at p is periodic at 2p and 3p as well, and the smallest-lag-at-the-peak
    rule in `_profile_period` only orders those when they TIE - a part with a group-of-three
    super-structure correlates BETTER at 3p than at p, so the peak lands on the harmonic outright.

    Two conditions, and the measured cases show both are needed: the lag divides by the spacing a
    whole number of times (within HARMONIC_TOLERANCE), AND the spacings are alike enough
    (SPACING_REGULAR) for their median to be worth believing. The doublet fins of job 0ff6f9e5
    satisfy the first exactly and fail the second.
    """
    med, lag = float(spacing), float(lag)
    if med <= 0.0 or lag <= 0.0 or float(spread) > SPACING_REGULAR:
        return False
    ratio = lag / med
    whole = round(ratio)
    return whole >= 2 and abs(ratio - whole) <= HARMONIC_TOLERANCE

#: HOW MUCH OF THE FRAME THE REPEAT ITSELF HAS TO REACH ACROSS, as a fraction of the frame.
#:
#: WHY AN INK FRACTION IS THE WRONG INSTRUMENT FOR THIS, measured 2026-11-17 through the
#: production `ArtifactResolver`. Twelve bands of cells 16 px wide at a 32 px period, 64 px tall,
#: in a 1280x960 frame: 1.0000% ink, 11 repeats every 32.0 px at 0.898 of a perfect array, 36
#: enclosed interiors on the best row over 48 rows. It cleared `review_session.
#: EVIDENCE_INK_FLOOR` ten times over and discharged its region. At the producer's 6.28945 um/px
#: that patch is 2.42 x 0.40 mm of a 170 mm plate. A 20-band variant filling 640 x 32 px cleared
#: the same floor at 0.8333%. The ink fraction asks how many pixels were painted; a speck of a
#: real array paints enough of them, and a plenum paints nearly all of them.
#:
#: WHAT IS MEASURED INSTEAD is the FOOTPRINT of the periodic structure: how far the repeat
#: reaches along the axis it repeats on, times how far the ink reaches across that axis. Both
#: come off the two profiles this module already builds. An array of passages filling the frame
#: is ~1.0 whatever its duty cycle, which is the whole difference from an ink fraction: a 50%-duty
#: array is 0.50 inked and 0.98 in extent.
#:
#: ONE TENTH, AND IT IS ANCHORED ON THE REAL PLATE. MEASURED, 2026-11-17, over the six frames of
#: job 0ff6f9e5 and frames I drew at 1280x960:
#:
#:     frame                                      extent    what it is
#:     12 bands, 64 px tall      (drawn)          0.0184    THE SPECK THAT DISCHARGED A REGION
#:     20 bands, 640x32 px       (drawn)          0.0158    the same defect, re-parameterised
#:     passages 4 cells deep, full width (drawn)  0.0163    a cut that barely grazes the coolant
#:     ---- this floor, 0.1000 ----
#:     along_the_flow_zoom       REAL             0.2471    THE ONE REAL FRAME THAT HOLDS THE
#:                                                          ARRAY: 79 repeats over 0.9883 of the
#:                                                          width, on 0.2500 of the height
#:     passages, quarter frame   (drawn)          0.2379    an honestly part-filled frame
#:     passages, half frame      (drawn)          0.4758    an honestly half-filled frame
#:     passages, whole frame     (drawn)          0.9758    the honest frame
#:     a plenum with ribs        (drawn)          0.9758    refused elsewhere, not here
#:
#: So it clears the real plate's own array frame by 2.47x and the poorest honest frame I could
#: draw by 2.38x, and refuses the two specks by 5.4x and 6.3x.
#:
#: AND THE TWO CASES CANNOT BE SEPARATED BY SHAPE, which is measured and is why this is a floor
#: on the FOOTPRINT and not on either direction alone. The speck is 384 x 64 px of a 1280x960
#: frame and an honest array four cells deep across the whole width is 1280 x 16 px: 2.00% and
#: 1.67% of the frame, 0.0184 and 0.0163 of extent, 1.0000% and 0.8333% of ink. No bar on the
#: footprint can refuse the first and keep the second, and a bar on the scan axis alone would
#: have to sit at 0.60 - which keeps the thin strip, refuses the 640 px speck by only 1.26x, and
#: refuses every honestly half-filled zoom at 0.4758. This form is the one anchored on the real
#: plate's own array frame, at 2.47x.
#:
#: WHAT THE FLOOR ADMITS, measured through the production resolver, 2026-11-17: full width and
#: 96 px deep is 0.0976 and declines; 100 px deep (25 cell rows) is 0.1017 and discharges. A
#: SQUARE patch of honest passages discharges from 384 x 384 px (0.1103), which at the producer's
#: 6.28945 um/px is 2.42 x 2.42 mm inside an 8.05 x 6.04 mm window. The speck it refuses is
#: 2.42 x 0.40 mm - the same width and a sixth of the height.
#:
#: AND THE TABLE ABOVE DID NOT MOVE WHEN `repeat_span` WAS CORRECTED, which is measured rather
#: than hoped: the span is now the longest chain of rising edges with no gap longer than one
#: period in it (see `_periodic_run`), and on every frame in this table - the two specks, the
#: thin strip, the 96 px and 100 px frames either side of the floor, the quarter, half and whole
#: frames, the 384 px square, the plenum with ribs and the real plate's own array frame - the
#: extent and the span come out IDENTICAL in both arms, to four decimals. It could not be
#: otherwise: an honest array has no non-periodic ink in it to drop. What the correction moves is
#: the frame this table has no row for - an array with a PLENUM BESIDE IT, where the old figure
#: rose as the array shrank.
#:
#: THE COST OF GETTING THIS WRONG. Too low and a region is discharged on 0.4 mm of a 170 mm
#: plate, which is the measured defect. Too high and a genuinely thin cut declines with its
#: reason named, and that cost is really paid: a passage array four cells deep across the whole
#: width used to discharge (`review_session.COUNTABLE_CELL_ROWS` is set at exactly that patch)
#: and now does not. The producer sizes its zoom at 8.05 mm across, so a cut whose coolant
#: intersection is a 100 um ribbon inside that window is a plane grazing the part rather than an
#: inspection of it - but it is a narrowing, from four cell rows to twenty-five, and it is stated
#: here rather than discovered later.
EXTENT_MIN = 0.10

#: HOW MANY CELLS THE PLANNER PUTS ACROSS THE THIN WALL OF A TWO-SCALE PART.
#:
#: THIS IS THE ONE PLAN FIGURE IN THIS FILE and every test built on it is otherwise pure pixels.
#: `cfmesh_conjugate/plan.ConjugatePlan.fine_surfaces` carries "the thin side of a two-scale
#: part's wall" as `thickness_m`, and the planner pins the fine cell AT that thickness over its
#: four-cell floor - so `thickness_m / near_wall_cell_m` is 4 by construction. On job 0ff6f9e5
#: 0.0001006312 / 2.51578e-05 = 4.0 EXACTLY, and a test pins that arithmetic.
#:
#: IT IS NOT `review_session.COUNTABLE_CELL_PX`, which is also 4, and the two must not be folded
#: together however tempting that is. That one is PIXELS PER DRAWN CELL and belongs to whoever
#: sizes a window; this one is CELLS PER PLANNED WALL and belongs to whoever sized the mesh. They
#: are the same number here because `slices.CELL_PIXELS` was deliberately set to the planner's own
#: four-cell floor, and that file says so - but they measure different things, and one number
#: wearing two names is how two independent facts start looking as though they agree.
PLAN_CELLS_ACROSS_A_WALL = 4.0

#: HOW LITTLE METAL MAY STAND BETWEEN TWO DRAWN PASSAGES before the picture is of a plenum.
#:
#: THE CASE THIS CLOSES, and no period test can close it. MEASURED, 2026-11-17, through the
#: production resolver: a solid field of 4 px fluid cells with a bare 2 px wall every 32 px comes
#: back 93.7500% inked, 39 repeats, period 32.0 px, 0.975 of a perfect array, 240 enclosed
#: interiors on its best row over 720 rows - and it discharged its region. An honest cut across
#: this plate's passages, drawn through the same code at the same 32 px period with half of each
#: period bare, measures 39 repeats, period 32.0 px, 0.975, 4.0 px pitch, 720 rows. EVERY FIGURE
#: EXCEPT THE INK IS IDENTICAL, including the period. The period of a plenum with ribs in it IS
#: the part's period; what is missing is the part.
#:
#: AND HOW IT IS MEASURED WAS WRONG UNTIL NOW, which is the correction this round makes and the
#: reason `duty` is gone. The wall used to be "one period, less the share of that period whose
#: ink stood above half the profile's own 95th percentile" - a per-column ink DENSITY contrast.
#: MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4.00 px per cell, through the production
#: ArtifactResolver: a field of 4 px coolant cells covering the WHOLE frame with every other
#: 16 px band truncated partway down reads 39 repeats, period 32.0 px, 0.975, duty 0.5004 and a
#: wall of 3.9968 cells - BETTER than the honest frame - and discharged its region, at all three
#: truncation depths of 200, 400 and 470 px. It contains no metal at all: zero columns with no
#: ink in them, widest all-background column run 0 px, and all 915,200 inked pixels in ONE
#: 4-connected body. A plenum whose section merely varies in depth by more than about 2x reads
#: its shallow columns as metal.
#:
#: SO THE WALL IS NOW AN OCCUPANCY MEASUREMENT: the median run of samples along the repeat axis
#: that carry NO INK AT ALL, in pixels, divided by the cell drawn along that same axis. Metal
#: between passages means columns with no coolant in them, not columns with less coolant.
#: MEASURED on the same frames, SYNTHETIC, VOLUME:
#:
#:     frame                                      wall    what it is
#:     plenum, every other band truncated  (drawn)   0.00    THE FRAME THAT DISCHARGED ON DENSITY
#:     plenum, 2 px bare wall every 32 px  (drawn)   0.50    the round-5 frame, refused as before
#:     plenum, 2 px bare wall every 64 px  (drawn)   0.50    and the 402.5 um one beside it
#:     plenum, 4 px bare wall every 32 px  (drawn)   1.00    the same defect at twice the rib
#:     ---- this floor, 2.00 cells ----
#:     passages 4 cells, wall 2 cells      (drawn)   2.00    exactly on the bar, and admitted now
#:     passages 4 cells, wall 4 cells      (drawn)   4.00    THE HONEST FRAME (4.0000 exactly,
#:                                                           where the duty form read 3.9968)
#:
#: HALF THE PLANNED WALL, because the planner guarantees the whole of it: a thin wall gets
#: PLAN_CELLS_ACROSS_A_WALL cells by construction, so a picture showing under half that has not
#: resolved the wall it is being read for. AND NO FLOOR CAN BE LOWER, which is measured and not
#: argued: a plenum with a 1-cell rib (coolant 28 px, bare 4 px) and an honest array of passages
#: 7 cells wide with a 1-cell wall (coolant 28 px, bare 4 px) are the SAME DRAWING four pixels
#: apart - measured, every figure either one yields is identical, down to 87.5000% ink, 39
#: repeats, period 32.0 px, 0.975, extent 0.9758, a 4.0 px drawn cell, 240 interiors over 720
#: rows and a 4.0 px bare run. A floor of one cell would have to admit both or refuse both, so
#: "a true picture of a 1-cell wall" and "a plenum with a 1-cell rib" cannot be separated by
#: anything, and they are refused together.
#:
#: AND IT IS APPLIED TO THE NARROWEST WALL IN THE FRAME, not to the typical one, which is round
#: 7's second correction here. A median over a bimodal set of walls is a width no wall in the
#: picture has: walls alternating 16 px and 1 px read 8.5 px and 2.125 cells and cleared this
#: floor, with half of their walls at a quarter of a cell - the 1-cell rib the paragraph above
#: refuses. Both figures are published and the floor is on the narrower; see `_bare_runs`.
#:
#: WHAT ANTI-ALIASING COSTS IT, measured rather than assumed: a rasteriser paints a trace of ink
#: into the column straddling each edge, so a bare run reads about 1 px narrower than drawn. On
#: the honest frame the wall reads 4.00 cells hard-edged and 3.75 anti-aliased, clearing this
#: floor by 2.00x and 1.87x. A wall drawn exactly two cells thick reads 2.00 hard-edged and is
#: admitted, and 1.75 anti-aliased and is not - so two cells is the edge of the window and three
#: is inside it, and that is stated here rather than discovered on a run.
#:
#: AND WHERE THE OCCUPANCY IS TAKEN IS NOT DOWN THE WHOLE FRAME ANY MORE, which is round 11 and
#: is the only thing about this floor that moved. The paragraphs above all stand: the floor is
#: still half the planned wall, it is still applied to the narrowest wall, and every frame they
#: name still measures what they say it does. What changed is the mask the runs are counted in -
#: a sample is metal where it is bare AT THE ENDS of the structure's cross extent, not where it
#: is bare on every line of the frame. MEASURED, REAL PLATE, VOLUME, job 6dd6703c's
#: through_the_plate zoom: 0.00 cells before, 4.00 cells after, on a wall that measures 100.63 um
#: against the plan's own 100.6312 um. See `_metal_mask` for the transverse manifold that made a
#: whole-frame sum read a true picture of this part's wall as a field of coolant, and for the
#: measured table showing that a per-line majority alone would have admitted the plenum.
WALL_CELLS_MIN = PLAN_CELLS_ACROSS_A_WALL / 2.0

#: Which way the scan runs, as a reader is told it.
SCAN_AXES = ("horizontal", "vertical")


def ink_structure(gray, *, ink_max: int = INK_MAX, min_repeats: int = MIN_REPEATS,
                  min_period_px: float = MIN_PERIOD_PX, acf_min: float = ACF_MIN,
                  min_extent: float = EXTENT_MIN) -> dict:
    """What a DRAWN FRAME contains, measured out of its own pixels. `gray` is a 2-D greyscale array.

    This is the only thing in this file that is not arithmetic about the plan, and it is the only
    thing that can say whether a cut landed on the passages. See the note on CELL_PIXELS for the
    two frames that were handed back as countable evidence while holding a plenum rectangle and a
    port bore, and for why no scale can tell those apart from the real thing.

    HOW. The frame is reduced to one profile per axis - how much ink stands in each column, and in
    each row - and two things are asked of it. HOW OFTEN it alternates between material and void,
    from the runs of a profile cut at half its own 95th percentile; and HOW PERIODIC it is, from
    the peak of its own autocorrelation. A single blob, a bore, or a solid field of cells fails
    the first; scattered traces of everything a plane happened to cross pass the first and fail
    the second. The period published is the LAG the profile repeats at, with the run spacing
    beside it, and the caller turns the period into metres with the window it drew at.

    WHY BOTH. Measured on this plate: a port bore and three plenum rectangles are one run and
    three, so counting runs is what sees them. A whole-part frame of scattered traces gives 23 and
    38 runs and would pass that, and its autocorrelation is 0.22 against the 0.99 of the real
    array. Neither test alone does the job; see MIN_REPEATS and ACF_MIN for both tables.

    AND HOW MUCH OF THE FRAME THE REPEAT REACHES ACROSS, which is the third thing asked and the
    one an ink fraction cannot answer. See EXTENT_MIN for the speck of array in a blank frame
    that cleared every bar above and discharged a region.

    AND HOW WIDE THE METAL BETWEEN TWO PASSAGES IS, which is published and not asked here, because
    it is counted in the CELL the frame was drawn at and this module never sees a cell. It is the
    one figure measured by OCCUPANCY rather than by contrast - runs of samples with no ink in them
    at all - and it is published TWICE, as `bare_run_px` (the median) and `bare_run_min_px` (the
    narrowest), because a frame whose walls are of two widths has no single answer. See
    `_bare_runs` for why the two kinds of question need two kinds of instrument and why the floor
    goes on the narrower, and WALL_CELLS_MIN for the field of coolant that a density contrast
    measured a four-cell wall in.

    `scan` NAMES THE AXIS every figure above was read along, and the caller must use it: the
    period is on whichever axis shows the repeat, so a cell counted along the other one does not
    divide into it. See `review_session.ResolvedCut.repeat_pitch_px`.

    Returns the figures for both axes plus the better one, and `shows_structure`. Never raises on
    a strange frame: a blank or a solid one is a frame that shows no structure, which is an answer.
    """
    import numpy as np

    a = np.asarray(gray)
    if a.ndim != 2 or a.size == 0:
        return {"shows_structure": False, "why": "the frame is not a 2-D picture"}
    ink = a < int(ink_max)
    out: dict = {"pixels": [int(a.shape[1]), int(a.shape[0])],
                 "ink_fraction": round(float(ink.mean()), 6),
                 "ink_max_grey": int(ink_max)}
    found = []
    for name, axis in zip(SCAN_AXES, (0, 1)):
        # axis=0 sums down the columns, which is the profile ALONG the frame's width
        # THE METAL MASK NEEDS THE FRAME AND NOT THE PROFILE, so it is built here where both
        # directions are still in hand and handed down. `lines[k]` is one scan line ACROSS the
        # repeat axis: the frame's rows for the horizontal scan, its columns for the vertical.
        one = _profile_period(ink.sum(axis=axis).astype(float), min_period_px=min_period_px,
                              metal=_metal_mask(ink if axis == 0 else ink.T))
        one["scan"] = name
        one["edge_contrast"] = _line_contrast(a, ink, along_rows=(axis == 0))
        found.append(one)
    # THE FOOTPRINT, which needs BOTH profiles and so cannot be settled inside the loop above:
    # the repeat reaches `repeat_span` along the axis it repeats on, and the ink reaches
    # `ink_span` along the OTHER one. A speck of array is near 1.0 on the first and near 0 on the
    # second, which is why one of them alone would not have refused it.
    for one, other in ((found[0], found[1]), (found[1], found[0])):
        one["extent"] = round(float(one["repeat_span"]) * float(other["ink_span"]), 4)
        one["extent_across"] = float(other["ink_span"])
        one["passes"] = (one["repeats"] >= int(min_repeats)
                         and one["period_px"] >= float(min_period_px)
                         and one["strength"] >= float(acf_min)
                         and one["extent"] >= float(min_extent))
        out[one["scan"]] = {k: one[k] for k in
                            ("repeats", "period_px", "run_spacing_px", "spread", "strength",
                             "strength_lag_px", "edge_contrast", "bare_run_px",
                             "bare_run_min_px", "repeat_span", "extent_across", "extent",
                             "period_unmeasurable", "max_lag_px")}
    # THE AXIS THE STRUCTURE IS ON, which is a passing one if there is one: an array is read along
    # ONE direction and the other axis of the same frame is the length of the passages.
    best = next((f for f in found if f["passes"]),
                max(found, key=lambda f: (f["repeats"], f["strength"])))
    out.update({"scan": best["scan"], "repeats": best["repeats"],
                "period_px": best["period_px"], "run_spacing_px": best["run_spacing_px"],
                "spread": best["spread"], "strength": best["strength"],
                "strength_lag_px": best["strength_lag_px"],
                "edge_contrast": best["edge_contrast"],
                "bare_run_px": best["bare_run_px"],
                "bare_run_min_px": best["bare_run_min_px"],
                "repeat_span": best["repeat_span"],
                "extent": best["extent"],
                "period_unmeasurable": bool(best["period_unmeasurable"]),
                "max_lag_px": int(best["max_lag_px"]),
                "shows_structure": bool(best["passes"])})
    # THE PERIOD THIS FRAME COULD NOT MEASURE, said wherever the frame is refused for some OTHER
    # reason as well. The five coarse pitches this closes are all refused on their repeat count
    # first, and that refusal is the right one - but the record must not be silent about the
    # period being silence rather than a measurement.
    coarse = ""
    if best["period_unmeasurable"]:
        coarse = (f"no period is published for this frame at all: its ink runs stand "
                  f"{best['run_spacing_px']:.1f} px apart along the {best['scan']} axis while "
                  f"the strongest repeat measurable in it is at {best['strength_lag_px']} px, "
                  f"and the longest repeat a frame this size can test is {best['max_lag_px']} px "
                  "- so the structure in this frame is coarser than this frame can measure")
    if not best["passes"]:
        if out["ink_fraction"] <= 0.0:
            out["why"] = "nothing was drawn in this frame"
        elif best["repeats"] < int(min_repeats):
            out["why"] = (f"the ink forms {best['repeats']} repeat(s) across the frame, under the "
                          f"{int(min_repeats)} a periodic array has to show"
                          + (f", and {coarse}" if coarse else ""))
        elif best["period_unmeasurable"]:
            # SILENCE, SAID AS SILENCE. This frame published no period at all, so the sentence
            # below - "the ink repeats every 0.0 px" - would be a figure nobody measured. Every
            # figure the sentence gives is measured: how far apart the ink's own runs stand, the
            # lag that correlated best, and the longest repeat this profile was able to test.
            out["why"] = coarse
        elif best["period_px"] < float(min_period_px):
            out["why"] = (f"the ink repeats every {best['period_px']:.1f} px, under the "
                          f"{float(min_period_px):.0f} px a passage and its wall need")
        elif best["strength"] < float(acf_min):
            out["why"] = (f"the ink repeats at {best['strength']:.2f} of a perfect array against "
                          f"the {float(acf_min):.2f} a real one reaches, so these "
                          f"{best['repeats']} marks are scattered rather than an array")
        else:
            out["why"] = (
                f"the repeat reaches {best['repeat_span'] * 100.0:.2f}% of the frame along the "
                f"{best['scan']} axis and the ink reaches "
                f"{best['extent_across'] * 100.0:.2f}% across it, so the structure covers "
                f"{best['extent'] * 100.0:.2f}% of the frame against the "
                f"{float(min_extent) * 100.0:.2f}% a cut of a region needs - whatever repeats "
                "here, it is a speck of the picture and the rest of the frame is not it")
    return out


def _line_contrast(grey, ink, *, along_rows: bool) -> int:
    """How far apart the lightest and darkest things drawn are WITHIN one scan line, as the
    median over the lines that have anything drawn in them.

    PER LINE AND NOT PER FRAME, and that is measured rather than tidy. A camera in phase with the
    cell lattice drops the lines that run ACROSS the scan and keeps the ones that run along it, so
    a frame-wide figure reads 106 grey levels on a frame whose every scan row is a flat field of
    87 with not one cell boundary on it. Measured on a uniform grid of this plate's own cell: in
    phase, 0 per row; half a pixel off, 87. The frame-wide figure cannot tell those apart.
    """
    import numpy as np

    g = np.asarray(grey).astype(np.int16)
    axis = 1 if along_rows else 0
    hi = np.where(ink, g, -1).max(axis=axis)
    lo = np.where(ink, g, 256).min(axis=axis)
    swing = (hi - lo)[hi >= 0]
    return int(np.median(swing)) if swing.size else 0


def _metal_mask(lines):
    """WHICH SAMPLES ALONG THE REPEAT AXIS ARE METAL, as a 1-D boolean over that axis. `lines` is
    the frame's 2-D ink mask oriented so that `lines[k]` is ONE SCAN LINE across the repeat axis.

    A sample is METAL when it carries no ink on the FIRST and LAST line of the structure's own
    cross extent, and carries none on more than half the lines between.

    WHY THIS IS NOT A COLUMN SUM, and it is the defect this round closes. The wall used to be read
    off `profile <= 0.0` - the ink summed down the WHOLE frame - so a sample was metal only if it
    was bare on EVERY line. MEASURED, 2026-11-19, REAL PLATE, VOLUME, job 6dd6703c's
    through_the_plate zoom, the first cut of a real mesh INTERIOR that has ever been drawn: that
    frame holds 640 metal columns in 41 runs of 16.0 px, which at the producer's 6.28945 um/px is
    100.63 um - 1.0000 of the plan's own 100.6312 um wall, 4.00 planned cells. EVERY ONE OF THEM
    read 0.0 px, because the plate has a TRANSVERSE MANIFOLD crossing every channel at rows
    462..494 and 33 inked rows out of 960 put ink into all 1280 columns. The frame was refused
    with "the metal between two passages measures 0.0 px at its narrowest" while its metal
    measured exactly what the planner asked for.

    AND IT IS NOT ANTI-ALIASING, which is what that refusal looked like and is not what it was.
    MEASURED on the same PNG: the frame holds 179 distinct grey levels and NOT ONE PIXEL between
    193 and 254 - the counts under 193, 200, 220, 240, 248 and 254 are all exactly 635,520 - so
    there is no blend to the background anywhere in it and no brightness threshold could have
    changed the answer. The 33 pixels in a metal column are not a trace, they are DARK: that
    column's own histogram is {0: 3, 4: 2, 6: 1, 12: 1, 16: 1, 18: 1, 27: 1, 43: 1, 61: 1, 74: 1,
    78: 1, 79: 17, 186: 2, 255: 167}. They are a real feature of the part, in the right place, and
    the reduction over the whole height is what could not see past them.

    THE ENDS ARE WHAT KEEPS THE METAL-FREE FAMILY OUT, and that is measured and not assumed.
    `tests/unit/sandbox/_part_pictures.write_truncated_plenum` draws a field of coolant with NO
    metal in it by running every other band only `deep_px` down, so the top of the frame is solid
    coolant and the bottom looks like passages with walls. MEASURED, SYNTHETIC, VOLUME, 1280x960
    at 4 px per cell, the mask built four ways:

        frame                                  whole   ends only   majority only   ends AND maj
        REAL through_the_plate_zoom           0.00 px    16.00 px        16.00 px       16.00 px
        truncated_plenum(deep_px=200)         0.00 px     0.00 px        16.00 px        0.00 px
        truncated_plenum(deep_px=912)         0.00 px     0.00 px         0.00 px        0.00 px
        honest write_passages()              16.00 px    16.00 px        16.00 px       16.00 px

    A MAJORITY ALONE ADMITS THE PLENUM AT A FOUR-CELL WALL, which is the row that decides this:
    79.2% of that frame's lines show its 16 px gaps and a reduction that only counts lines cannot
    tell them from walls. What a wall has and a truncation has not is that it is there AT BOTH
    ENDS of the window - a wall runs the length of the passages and leaves the frame at both
    sides, while a pool that stops partway is bounded by coolant at one of them.

    AND THE MAJORITY CLAUSE IS NOT LOAD-BEARING ON ANY FRAME MEASURED, which is said rather than
    implied. `_profile_period` cuts its material mask at half the profile's own 95th percentile,
    so a sample inked on more than half the lines is already MATERIAL and supplies no rising edge:
    there is then no repeat and the frame is refused on its repeat count before the wall is
    reached. MEASURED, SYNTHETIC, VOLUME, honest passages with a full-width coolant band across
    the middle of the frame: a join of 479 rows of 960 is admitted at 16.00 px and a join of 480
    is refused at 0.00 - the transition is at exactly half, and it is that threshold's and not
    this one's. The clause is kept so this measurement states its own meaning instead of resting
    on that coincidence.

    THE COST, measured and stated rather than discovered later. A frame whose transverse join
    touches EITHER END of the cross extent is refused however narrow it is: measured on the same
    family, a 33-row join - the width of this plate's own manifold - is admitted at 16.00 px in
    the middle of the frame and refused at 0.00 px against the top edge. So a zoom window placed
    so that it clips a manifold at its edge declines, and one placed over the channels does not.
    The producer sizes that window at 8.05 mm of a 170 mm plate and centres it on the plan's fine
    zone, and this plate's channels run 53 mm, so the window has to be put on a manifold on
    purpose. ONE INK PIXEL in a metal column on the first or last line of the cross extent is
    enough to refuse the frame, which is the same shape of cost `bare_run_min_px` already carries
    for one narrow wall anywhere inside the repeat.
    """
    import numpy as np

    a = np.asarray(lines, dtype=bool)
    if a.ndim != 2 or a.size == 0:
        return np.zeros(0, dtype=bool)
    cross = a.sum(axis=1).astype(float)
    top = float(cross.max())
    if top <= 0.0:
        return np.zeros(a.shape[1], dtype=bool)
    # THE STRUCTURE'S OWN CROSS EXTENT, cut at half its maximum for the reason `ink_span` is: a
    # line carrying one stray pixel is nowhere near a line carrying the structure, and the ends
    # of the frame are not the ends of what is drawn in it.
    idx = np.flatnonzero(cross >= 0.5 * top)
    lo, hi = int(idx[0]), int(idx[-1])
    bare = ~a[lo:hi + 1]
    return bare[0] & bare[-1] & (bare.mean(axis=0) > 0.5)


def _bare_runs(bare) -> tuple[float, float]:
    """HOW WIDE THE METAL BETWEEN TWO PASSAGES IS, in samples along the repeat axis: (the MEDIAN
    run of METAL samples, the NARROWEST such run). (0.0, 0.0) when no sample is metal. `bare` is
    the boolean mask `_metal_mask` built, already cut to the repeat's own span by the caller.

    TWO FIGURES AND NOT ONE, because the median of a bimodal set is a width no wall in the picture
    has. MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell: passages 4 cells wide with walls
    that ALTERNATE 16 px and 1 px read a median bare run of 8.5 px - 2.125 drawn cells - and
    cleared a floor of 2.00 that neither of the two walls in the picture clears. Half the walls in
    it are a quarter of a cell thick, which is the 1-cell rib WALL_CELLS_MIN's own note says
    cannot be told from a 1-cell wall and is refused with it. THIS PART'S OWN z-normal cut holds
    fins THAT COME IN PAIRS (see ACF_MIN), so alternating walls are the shape this plate actually
    draws and not a contrived one.

    THE NARROWEST IS WHAT IS HELD TO THE FLOOR and the median is published beside it, because the
    two answer different questions: "is every wall in this picture a wall" and "what does a
    typical wall here measure". A frame refused on the first with a healthy second is told both
    figures, so the reader can see that most of the frame is sound and which part of it is not.
    THE COST, stated rather than discovered: one narrow wall anywhere inside the repeat refuses
    the whole frame. The alternative costs more - a frame whose narrowest wall is a rib in a
    plenum is admitted, and a count taken across that rib is a count of nothing.

    THE ONE FIGURE IN THIS FILE THAT IS AN OCCUPANCY TEST, and the rest are density tests
    deliberately. "Is there periodic structure here" is a question about CONTRAST and has to be,
    which is measured: the real plate's own array frame (job 0ff6f9e5's along_the_flow zoom, a
    SURFACE cut) has ink in every single column of its window - widest bare run 0 px - and still
    reads 79 repeats every 16.0 px at 0.987 off the density profile. An occupancy test there finds
    0 repeats and would report the only real frame on this part that holds the channel array as
    holding no structure at all.

    "IS THERE METAL BETWEEN THESE PASSAGES" IS THE OTHER KIND OF QUESTION, and a density contrast
    answers it wrongly by construction: a plenum whose section varies in depth reads its shallow
    columns as metal. See WALL_CELLS_MIN for the truncated field of coolant that measured a
    4-cell wall and discharged a region while containing one connected body of coolant and no
    metal anywhere. Metal is where there is NO coolant, so that is what is counted - and WHERE it
    has to be none is `_metal_mask`'s question, not this one's. This function only measures the
    runs of a mask it is handed.

    THE MEDIAN AND NOT THE MEAN, because a frame is not obliged to be uniform: a run of the
    coolant's own margin, or one wide bay among narrow walls, moves a mean and does not move
    this. It is taken inside the repeat's own span by the caller, so the blank margins of a frame
    whose structure sits in the middle of it are not counted as walls.
    """
    import numpy as np

    bare = np.asarray(bare, dtype=bool)
    if bare.ndim != 1 or not bare.any():
        return 0.0, 0.0
    edges = np.flatnonzero(np.diff(np.concatenate(([0], bare.astype(np.int8), [0]))))
    lengths = (edges[1::2] - edges[0::2]).astype(float)
    if not lengths.size:
        return 0.0, 0.0
    return round(float(np.median(lengths)), 2), round(float(lengths.min()), 2)


def _periodic_run(starts, period: float) -> tuple[int, int]:
    """THE STRETCH OF THE PROFILE THAT ACTUALLY REPEATS, as (first sample, last sample): the
    longest chain of rising edges with no gap in it longer than one period.

    WHAT THIS REPLACES AND WHY. `repeat_span` used to run from the FIRST rising edge of the
    material mask to the LAST, so a stretch of ink that repeats at nothing supplied both ends of
    the figure that is supposed to disclose it. MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per
    cell, a frame that is featureless coolant either side of an honest array:

        the array's share of the width     0.25      0.35      0.50
        repeat_span, first edge to last    0.6258    0.6758    0.7508
        repeat_span, the chain below       0.2258    0.3258    0.4758

    The old figure went UP as the real array SHRANK, and the sentence a reviewer reads said "over
    62.58% of the frame" about a frame that is three quarters plenum. Independently: the speck
    EXTENT_MIN exists to refuse reads 0.0184 of the frame alone, and 0.6137 with one non-periodic
    stripe added at the far side of the frame - the stripe does not have to be periodic, or even
    large, to hand the figure its end.

    ONE PERIOD IS THE BOUND, and it is not a tuned tolerance: a period of a periodic structure
    holds at least one rising edge BY DEFINITION, so a stretch with no rising edge in it for
    longer than that is not part of the repeat. The 10% is for raster jitter, measured on the
    anti-aliased frames where an edge lands a pixel either side of where it was drawn.

    SEVERAL RISING EDGES PER PERIOD ARE ORDINARY and a chain on the SPACING would refuse them.
    MEASURED on this plate's own shape: walls alternating 16 px and 1 px repeat every 49 px with
    TWO rising edges in each period, 32 px and 17 px apart, and a test that asked each gap to
    equal the period would have found no periodic part at all in a frame that is periodic over
    all of it. The bound is on the GAP and not on its agreement with anything.

    WHAT IT STILL CANNOT SEE, measured and stated rather than left to be found: a non-periodic
    field that is ADJACENT to the array contributes ONE rising edge at a legal distance, so the
    span bleeds one period into it. A frame half honest array and half featureless coolant reads
    0.5008 either way, which is the state
    `tests/unit/sandbox/test_the_metal_between_passages_is_what_is_measured.py` already records.
    """
    import numpy as np

    s = [int(v) for v in np.asarray(starts).ravel().tolist()]
    if not s:
        return 0, -1
    if len(s) == 1:
        return s[0], s[0]
    p = float(period or 0.0)
    if p <= 0.0:
        return s[0], s[-1]            # no period was found, so there is no chain to follow
    bound = p + max(2.0, 0.1 * p)
    best = (s[0], s[0])
    i = 0
    while i < len(s) - 1:
        j = i
        while j < len(s) - 1 and (s[j + 1] - s[j]) <= bound:
            j += 1
        if s[j] - s[i] > best[1] - best[0]:
            best = (s[i], s[j])
        i = j + 1 if j > i else i + 1
    return best


def _profile_period(profile, *, min_period_px: float = MIN_PERIOD_PX, metal=None) -> dict:
    """One ink profile: how often it alternates, at what spacing, how periodic it really is, how
    far the repeat reaches, and how wide the metal between two passages is.

    `repeat_span` is the stretch THAT ACTUALLY REPEATS, as a fraction of the profile - the
    longest chain of rising edges with no gap longer than one period in it (see `_periodic_run`
    for the plenum that used to supply both ends of this figure). It is how far the periodic
    structure itself reaches, not how far any ink reaches.
    `ink_span` is the other thing, the stretch this profile has real material in, and the caller
    uses the OTHER axis's `ink_span` to finish the footprint (see EXTENT_MIN).

    `ink_span` IS CUT AT HALF THE PROFILE'S OWN MAXIMUM, not at "any ink at all", and that is a
    hole closed rather than a style. A speck of array with ONE stray ink pixel at the top of the
    frame and one at the bottom reaches every line of the cross profile, so an any-ink span reads
    1.0 and the footprint collapses to the repeat's own reach - which passed EXTENT_MIN. At half
    the maximum a line carrying one pixel is nowhere near a line carrying the structure.
    Measured on the one real frame that holds the array (job 0ff6f9e5's along_the_flow zoom) the
    two definitions agree to four decimals at 0.2500, and they agree on every synthetic frame in
    EXTENT_MIN's table.

    `bare_run_px` is the metal between two passages, measured INSIDE `repeat_span` and not over
    the whole profile, for the same reason `duty` was: a speck of honest array in a blank frame
    has 448 px of bare margin on either side of it, and the margin of a frame is not a wall. On
    THAT frame the two definitions happen to agree, measured - the median of eleven 16 px runs
    and two 448 px ones is still 16.0 - so the span is what makes the figure right rather than
    lucky, and a frame with more margin than structure would not be so forgiving.

    IT REPLACES `duty`, which was the share of one period whose ink stood above half the
    profile's own 95th percentile; see `_bare_run` and WALL_CELLS_MIN for the field of coolant
    that duty measured a 4-cell wall in.

    `period_unmeasurable` IS SET, AND `period_px` IS 0.0, when the lag that correlates best is
    SHORTER than the median spacing between the profile's own rising edges - a lag no periodic
    profile can have. `max_lag_px` is the longest lag the search was able to test. See the
    silence inside for the five pitches that published "period 2.0 px" at 0.97 of a perfect
    array, and for why the ceiling of the search is not what this is tested against.
    """
    import numpy as np

    n = profile.size
    hi = float(np.percentile(profile, 95)) if n else 0.0
    empty = {"repeats": 0, "period_px": 0.0, "run_spacing_px": 0.0, "spread": 0.0,
             "strength": 0.0, "strength_lag_px": 0,
             "edge_contrast": 0, "bare_run_px": 0.0, "bare_run_min_px": 0.0,
             "repeat_span": 0.0, "ink_span": 0.0,
             # AND WHETHER THIS PROFILE COULD MEASURE ITS OWN PERIOD AT ALL, with the longest
             # lag it was able to test. See the silence below.
             "period_unmeasurable": False, "max_lag_px": 0}
    def _span(mask) -> float:
        idx = np.flatnonzero(mask)
        return round((int(idx[-1] - idx[0]) + 1) / n, 4) if idx.size else 0.0

    top = float(profile.max()) if n else 0.0
    if n < 4 or hi <= 0.0:
        # NO MATERIAL THRESHOLD TO SET, so there is no repeat and no wall - but there may still be
        # ink, and a frame whose structure is on the other axis needs this one's reach. A 32 px
        # band in a 960 px frame puts 96.7% of the rows at zero, which is exactly where the 95th
        # percentile goes to nothing.
        return dict(empty, ink_span=_span(profile >= 0.5 * top) if top > 0.0 else 0.0)
    material = profile >= 0.5 * hi
    rising = material & ~np.concatenate(([False], material[:-1]))
    starts = np.flatnonzero(rising)
    periods = np.diff(starts).astype(float)
    out = dict(empty)
    out["ink_span"] = _span(profile >= 0.5 * top) if top > 0.0 else 0.0
    med = 0.0
    if periods.size:
        med = float(np.median(periods))
        spread = (float(np.percentile(periods, 90) - np.percentile(periods, 10)) / med
                  if med else 0.0)
        out.update(repeats=int(periods.size), run_spacing_px=round(med, 2),
                   period_px=round(med, 2), spread=round(spread, 3))
    # HOW MUCH OF THE PROFILE REPEATS, at the lag that repeats best. Lags start at the shortest
    # period worth believing and stop at a sixth of the frame, so the "period" found is one that
    # is seen at least six times - the same bar MIN_REPEATS sets, applied to the other statistic.
    x = profile - profile.mean()
    denom = float(np.dot(x, x))
    # THE SEARCH STARTS BELOW THE FLOOR, deliberately, so a structure too fine to resolve is
    # MEASURED as too fine rather than reported as a coarser one. Starting at min_period_px, a
    # grating of 4 px came back as an 8 px period - two of its own periods - and cleared the
    # floor it was supposed to fail.
    lo, high = 2, max(int(min_period_px) + 1, n // 6)
    # NORMALISED OVER THE OVERLAP THE LAG ACTUALLY HAS, which is the arithmetic this round fixes
    # and the one line everything in the table above turned on. The numerator is a sum over
    # n - k samples; dividing it by the WHOLE profile's variance charges every lag a factor of
    # (1 - k/n), so the peak drifts toward the shortest lags and lag 2 wins outright once
    # passage x wall exceeds about 2n. That is an arithmetic bias and not a property of any part:
    # it MOVES WITH THE DUTY CYCLE, so the same pitch passed at duty 0.2 and failed at 0.5. The
    # cumulative energy below is the exact energy of each half of the overlap - e[n - k] is
    # x[:-k] and e[n] - e[k] is x[k:] - so this is the ordinary normalised cross-correlation of a
    # profile with itself, which is 1.000 at the lag of an exactly periodic profile whatever that
    # lag is. See the header for the eight true pictures of real passages the biased form refused
    # and for the whole vertical axis it made unreachable.
    e = np.concatenate(([0.0], np.cumsum(x * x)))
    if denom > 0.0 and high > lo:
        at = np.zeros(high, dtype=float)
        for k in range(lo, high):
            scale = float(e[n - k]) * float(e[n] - e[k])
            if scale > 0.0:
                at[k] = float(np.dot(x[:-k], x[k:])) / (scale ** 0.5)
        peak = float(at.max())
        # AND THE PERIOD IS THE SMALLEST LAG THAT REACHES THAT PEAK, which the biased form never
        # had to say out loud. A profile periodic at p is periodic at 2p and 3p as well, and the
        # period is the SMALLEST of them; the old arithmetic hid the choice by charging the longer
        # lags (1 - k/n) and so ordering the harmonics for free. MEASURED on the real plate's one
        # array frame (job 0ff6f9e5's along_the_flow zoom, REAL PLATE, SURFACE), overlap
        # normalised: lag 16 reads 0.9999, lag 32 1.0000, lag 48 0.9999, lag 64 1.0000 - the same
        # figure to every decimal this module publishes - so "the strict maximum" would have moved
        # this plate's published period from 100.63 um to 201.26 um on a difference of 1e-4.
        # TIED AT THE PUBLISHED PRECISION IS TIED: `strength` is rounded to three decimals, so
        # lags whose correlation rounds to the same number cannot be told apart by this
        # instrument, and the fundamental is the one reported.
        lag = int(np.flatnonzero(at >= peak - 0.0005)[0]) if peak > 0.0 else 0
        out.update(strength=round(peak, 3), strength_lag_px=int(lag),
                   max_lag_px=int(high - 1))
        if lag and med > float(lag) + max(2.0, 0.1 * float(lag)):
            # NO PERIOD RATHER THAN A WRONG ONE, which is the defect this closes. The lag search
            # stops at a sixth of the profile, so no pitch over about 212 px in a 1280 px frame
            # can be found at all - and the peak then lands at the shortest lag the search starts
            # at, which any smooth profile correlates with. MEASURED, SYNTHETIC, VOLUME, 1280x960
            # at 4 px per cell, through the production ArtifactResolver: honest passages at
            # pitches of 216, 240, 256, 320 and 400 px published "period 2.0 px" at strengths of
            # 0.966, 0.969, 0.972, 0.978 and 0.981, and a VERTICAL pitch of 240 px in the 960 px
            # direction published the same 2.0 px at 0.971. REAL PLATE, job 0ff6f9e5, SURFACE:
            # through_the_plate_whole published 2.0 px off ONE run, with its runs 243.0 px apart.
            # None of those frames was admitted - the refusal they got names the repeat count, and
            # correctly - but the figure that went into the record was false.
            #
            # AND THE PROFILE'S OWN RUNS ARE WHAT REFUTE THE LAG, not the ceiling of the search,
            # because the ceiling is not the only way this happens. A period of a periodic profile
            # holds AT LEAST ONE rising edge by definition, so the median spacing between rising
            # edges cannot exceed the period: a lag SHORTER than that spacing is not the period of
            # anything in the profile. That bound catches the cases a ceiling test cannot - an
            # honest 32 px array over 20% of a frame of featureless coolant (runs 32.0 px apart,
            # lag 2, 0.861) and the speck with one stripe beside it (32.0 px apart, lag 2, 0.878)
            # both publish 2.0 px with their structure well inside the searchable band. The 10%
            # with a 2 px floor is the raster jitter `_periodic_run` already allows, and it is
            # what keeps a true period measured one pixel under its drawn value: a 160 px VERTICAL
            # pitch in a 960 px frame reads lag 159 against runs 160.0 px apart, and is published.
            #
            # AND THE RULE IS DOCUMENTED FROM A REAL FRAME IT SILENCED, not only from the
            # degenerate 2.0 px family above, because the two are not the same case and only the
            # degenerate one had ever been written down. RE-MEASURED 2026-11-18, REAL PLATE,
            # SURFACE, job 0ff6f9e5, across_the_flow zoom, through the production
            # ArtifactResolver: this frame published period 80.0 px before and publishes SILENCE
            # now. Its own ink runs stand 120.0 px apart along the vertical axis and the longest
            # lag the search could test in it is 159 px, so 80 px sat INSIDE the searchable band
            # with room over - it is not the ceiling artefact the five synthetic pitches above
            # are, and a ceiling test would have left it standing. What refutes it is the bound
            # this branch is written on and nothing else: a period holds at least one rising edge,
            # so a lag of 80 px cannot be the period of a profile whose own runs are 120.0 px
            # apart. THE FIGURE IS A LOSS AND IT IS WORTH STATING AS ONE - 80.0 px at 6.28945
            # um/px is 503.2 um, which is a real length and is gone from the record - and what
            # replaces it is the detector's own sentence naming all three numbers (120.0, 80, 159)
            # so a reader can see exactly what was and was not established. NO VERDICT MOVES WITH
            # IT: that frame shows 3 repeats against the 6 `MIN_REPEATS` asks for and is refused
            # on the repeat count in both arms.
            out["period_unmeasurable"] = True
            out["period_px"] = 0.0
        elif lag:
            # THE PERIOD IS THE LAG, not the median spacing between runs, and this was measured
            # the hard way. The z-normal cut of job 0ff6f9e5 holds a coarse fin array whose fins
            # come in PAIRS: nine of them across the frame, truly 128 px apart, but the doublets
            # put enough short spacings in the list that the median came out 24.0 px. Published as
            # 150.9 um, that figure sat inside a factor of two of this plate's real 100.631 um
            # passages and the frame was accepted as a picture of them. At the lag it is 805.0 um,
            # eight times the passages, and it is refused. The run spacing stays published beside
            # it because it is what a reader counting marks on a scan row would get.
            out["period_px"] = float(lag)
            # ...UNLESS THE LAG IS A HARMONIC OF A REGULAR SPACING, which is the same mistake in
            # the other direction and cost job 606af55e its review. A profile periodic at p is
            # periodic at 2p and 3p, and the smallest-lag-at-the-peak rule above only orders them
            # when they TIE: a part with a group-of-three super-structure correlates better at 3p
            # than at p, so the peak lands on the harmonic outright.
            #
            # MEASURED, that job's through_the_plate zoom: 21 run spacings of 62.0 px with a
            # spread of 0.016 - every one alike - and a peak at lag 185, which is 2.98 times the
            # spacing. 62 px at that frame's 3.2645 um/px is 202.4 um, the plate's real passage
            # pitch; 185 px is 603.92 um, and the ratio test saw six times the plan's 100.631 um
            # feature pitch and called a clean picture of 21 passages "a different feature of the
            # same part". It only appeared once the zoom window was sized from the post-layer cell,
            # which is what made the super-structure resolvable at all.
            #
            # WHAT TELLS THE TWO CASES APART IS ALREADY ON THE FRAME, and `is_harmonic` holds the
            # arithmetic: the doublet fins alternate short and long spacings, so their median lies
            # and `spread` is of order one (0.612 measured); this plate's spacings are all alike at
            # 0.016. Note that the doublets' lag is a whole multiple of their median TOO, exactly
            # 2.00, so regularity rather than the multiple is what does the separating here.
            if is_harmonic(lag, med, out["spread"]):
                out["period_px"] = round(med, 2)
    # AND THE SPAN OF THE PART THAT REPEATS, which needs the period and so cannot be settled
    # before it. The wall is measured INSIDE that span for the reason the margins of a frame are
    # not walls, and now for a second one: the ink either side of the repeat is not the repeat.
    if starts.size > 1:
        # THE RUN SPACING IS THE BOUND WHEN NO PERIOD WAS PUBLISHED, which is what this function
        # used before a lag was measured at all. Silencing the period must not silence the span
        # as well: `_periodic_run` reads a period of 0.0 as "no period was found" and hands back
        # the first rising edge to the last, which is the inflated figure `_periodic_run` exists
        # to refuse. Every frame whose period is silenced is refused on `period_px` either way, so
        # this only decides which figure the record carries beside the refusal.
        first, last = _periodic_run(starts, out["period_px"] or med)
        if last > first:
            out["repeat_span"] = round((last - first + 1) / n, 4)
            # THE METAL IS A 2-D QUESTION AND THIS FUNCTION ONLY HOLDS A PROFILE, which is why
            # the mask arrives from `ink_structure` rather than being cut out of `profile` here.
            # See `_metal_mask` for the real plate's transverse manifold that put ink into every
            # column of a frame whose metal measures the plan's wall exactly. With no mask - which
            # is only the case for a caller that has no frame, i.e. a direct unit drive of this
            # function - the old whole-profile reading stands, so nothing silently changes shape.
            m = ((np.asarray(profile) <= 0.0) if metal is None
                 else np.asarray(metal, dtype=bool))
            out["bare_run_px"], out["bare_run_min_px"] = _bare_runs(m[first:last + 1])
    return out


def worst_contrast(content) -> int:
    """The edge contrast of the WEAKER of a frame's two directions.

    THE WEAKER ONE, because a camera in phase with the cell lattice weakens the boundaries that
    run one way more than the others. Measured over sixteen sub-pixel phases of a uniform grid of
    this plate's own cell, through the producer's own renderer, it never falls under 53 grey
    levels at 4 pixels per cell or 106 at 8 - so nothing measured so far needs this, and it is
    published because a frame whose cells cannot be told apart is a frame nothing can be counted
    in, and that is worth being able to see in the record rather than infer.

    `content` is what `ink_structure` returned. 0 for anything that is not one.
    """
    if not isinstance(content, dict):
        return 0
    both = [content.get(k) for k in SCAN_AXES]
    seen = [int(d.get("edge_contrast") or 0) for d in both if isinstance(d, dict)]
    return min(seen) if seen else int(content.get("edge_contrast") or 0)


def period_matches(period_m: float, expect_m: float, *, factor: float = PERIOD_MATCH) -> bool:
    """Whether a frame's measured period is the same structure as the part's own measured one.

    IN METRES, WHICH IS THE FORM FOR THE SIDE THAT DREW THE FRAME and not for the side that
    reads it. The producer knows the magnification because it set the camera; the reader has one
    PNG and a stated window, and a period in metres is the measured period times that stated
    window. See `period_in_cells` for the form the reader uses and why.

    True when nothing is expected: with no yardstick there is nothing to disagree with, and
    refusing every frame on a part whose first cut showed nothing would turn a missing yardstick
    into a missing mesh.
    """
    p, e, f = float(period_m or 0.0), float(expect_m or 0.0), float(factor)
    if e <= 0.0:
        return True
    if p <= 0.0 or f <= 1.0:
        return False
    return (e / f) <= p <= (e * f)


def period_in_cells(period_px: float, drawn_cell_px: float) -> float:
    """The frame's own period, counted in the CELLS DRAWN IN IT. 0.0 when either is unmeasured.

    BOTH SIDES OF THIS ARE PIXELS MEASURED OFF THE PICTURE, and that is the whole reason it is
    the reader's form of the question. `period_px` is the lag the ink profile repeats at;
    `drawn_cell_px` is the spacing of the enclosed cell interiors. Neither is derived from the
    stated window, so the magnification cancels exactly: draw the same structure at any scale and
    this number does not move.

    AND BOTH MUST BE READ ALONG THE SAME AXIS, which is the caller's job and was the defect:
    `ink_structure` reports the period on whichever axis the structure repeats on, and the reader
    used to measure the drawn cell only along ROWS. See
    `review_session.ResolvedCut.repeat_pitch_px`.
    """
    p, c = float(period_px or 0.0), float(drawn_cell_px or 0.0)
    return p / c if p > 0.0 and c > 0.0 else 0.0


def wall_in_cells(bare_run_px: float, drawn_cell_px: float) -> float:
    """How much METAL stands between two drawn passages, in the cells drawn in the frame.

    BOTH SIDES ARE PIXELS MEASURED OFF THE PICTURE, along the SAME axis. `bare_run_px` is one of
    `ink_structure`'s two runs of samples with no ink in them, taken on the axis the structure
    repeats on - the caller passes `bare_run_min_px` for the figure the floor is applied to and
    `bare_run_px` for the typical wall published beside it (see `_bare_runs`). `drawn_cell_px` is
    the spacing of the drawn cells on that same axis. 0.0 when either is unmeasured, which is
    silence and not a denial.

    WHY IT IS NOT `period_cells * (1 - duty)` ANY MORE. That form multiplied a period by a
    DENSITY contrast, and the density contrast read a shallow plenum column as metal: see
    WALL_CELLS_MIN for the field of coolant it measured a 3.9968-cell wall in. It also drifted on
    arithmetic that should be exact - an honest 4-cell wall read 3.9968 and a 2-cell wall 1.9986,
    so a wall drawn exactly on the floor fell under it. This form reads 4.00 and 2.00.

    AND THE AXIS IS WHY `drawn_cell_px` IS AN ARGUMENT rather than a field looked up here. The
    caller knows which axis the structure repeats on and which drawn cell belongs to it; see
    `review_session.ResolvedCut.repeat_pitch_px` for the frame of horizontal passages whose
    vertical period was being divided by a horizontal cell.
    """
    b, c = float(bare_run_px or 0.0), float(drawn_cell_px or 0.0)
    if b <= 0.0 or c <= 0.0:
        return 0.0
    return b / c


__all__ = ["ACF_MIN", "EXTENT_MIN", "HARMONIC_TOLERANCE", "INK_MAX", "MIN_PERIOD_PX",
           "MIN_REPEATS", "PERIOD_MATCH",
           "PLAN_CELLS_ACROSS_A_WALL", "SCAN_AXES", "SPACING_REGULAR", "WALL_CELLS_MIN",
           "ink_structure", "is_harmonic",
           "period_in_cells", "period_matches", "wall_in_cells", "worst_contrast"]
