# Responsibility: Resolve every declared review artifact inside the job workspace, and pass on only what qualified.
# Owns: the confinement fence, format sniffing by content, and cancellation-safe session teardown.
# Boundaries: a path escaping the workspace is refused here, and diagnostics name the manifest key, not the path.
from __future__ import annotations

import asyncio
import logging
import math
import zlib
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshpipeline.contracts.review_evidence import (
    NON_RENDERING_COMMANDS,
    ArtifactFormat,
    EvidenceItem,
    RenderArtifactRequirement,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewEvidenceFailure,
    ReviewRenderError,
    SessionUpdateResult,
)
from meshpipeline.sandbox.picture_structure import ink_structure

logger = logging.getLogger(__name__)

# A FULL-RESOLUTION BLANK FRAME IS THE LARGEST CLASS OF WORTHLESS EVIDENCE, and the byte-size
# band below is guaranteed to pass it: a blank 1280x960 PNG is several KB, comfortably inside the
# band, and on job 575f76e6 eight of them were filed as eight interior inspections. Size says the
# file is a plausible image; only the pixels say anything was drawn.
#
# The measure is deliberately the crudest one that cannot be argued with: the share of pixels
# that are not the frame's MOST COMMON colour, which is whatever the renderer cleared to. Below
# this share nothing was drawn - 1 in 10,000 of a 1280x960 frame is 122 pixels, and 122 pixels
# are not a mesh at any zoom. It is NOT a quality bar: a legitimate view with the model small in
# frame is thousands of pixels and passes.
#
# THE MOST COMMON COLOUR, not the corner pixel. Reading the background off a corner costs
# nothing and is right nearly always, and is catastrophically wrong the one time the model
# reaches that corner: the measure inverts and a full frame reads as blank. A mode over the
# whole frame assumes nothing about where the model sits.
EMPTY_FRAME_PIXEL_SHARE = 1e-4


def frame_shows_nothing(path: Path) -> str | None:
    """Why this frame is blank, or None - which also covers "could not measure it".

    A measurement that cannot be taken is not evidence of blankness, so an unreadable or
    undecodable frame falls through to the checks that already own those cases. Pillow and numpy
    are imported here rather than at module scope: this module is on the import path of
    render_inputs and must stay importable without the render stack.
    """
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGB"))
    except Exception as exc:  # noqa: BLE001 - see the docstring; never fail a review on this
        logger.debug("review session: could not measure frame %s: %s", path.name, exc)
        return None
    if arr.size == 0:
        return "the frame has no pixels"
    rgb = arr.astype(np.uint32)
    packed = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    total = int(packed.size)
    background = int(np.unique(packed.ravel(), return_counts=True)[1].max())
    drawn = total - background
    if drawn > max(1, int(total * EMPTY_FRAME_PIXEL_SHARE)):
        return None
    return (f"the frame is blank - {drawn} of {total} pixels differ from the background, so "
            "nothing was drawn in it")


# The sandbox is AUTHORITATIVE on limits. An engine declaration may ask for less; it can never
# raise these. (RenderContext carries the request; these are the ceilings.)
MAX_IMAGES_PER_SESSION = 64
MAX_IMAGE_BYTES = 12 * 1024 * 1024

#: HOW MANY PREPARED FRAMES ONE SESSION WILL DECODE, which is a bound on WORK and not on evidence.
#:
#: WHY A SECOND COUNTER WHEN MAX_IMAGES_PER_SESSION EXISTS. That one bounds the views a reviewer
#: TAKES, in `ReviewSession._validate`. Prepared frames do not go through it: they are decoded in
#: `ArtifactResolver.resolve_cuts` before a session exists, straight off an UNTRUSTED manifest,
#: and `_MAX_PNG_BYTES` and `_MAX_FRAME_PIXELS` bound each FILE while nothing bounded the NUMBER.
#:
#: MEASURED, 2026-11-17, in this venv. `_measure_png` un-filters every scanline in pure Python:
#: a 4096x4096 greyscale PNG whose every scanline is Paeth-filtered is 22,548 BYTES on disk and
#: costs 2.687 s to measure - 119 microseconds of CPU per byte of file. A manifest naming 100 of
#: them is 2.2 MB of files and 268.7 s. At the producer's own 1280x960 it is 0.33 s a frame, and a
#: manifest of 10 regions x 10 frames measured 33.5 s for 10 frames kept.
#:
#: AND IT COUNTS DECODES, NOT FRAMES KEPT, because those are different numbers. `resolve_cuts`
#: de-duplicates by scope AFTER `_resolve_cut` has already read the file, so 20 frames declared
#: under one region's "zoom" scope measured 6.68 s to keep exactly one. A cap on what is kept
#: would not have bounded that at all.
#:
#: 64, MIRRORING MAX_IMAGES_PER_SESSION, because the two bound the same resource from two sides
#: and a reviewer that may take 64 views has no use for more than 64 prepared ones. The producer
#: draws six (three cuts x two frames), so this is ten times anything honest.
MAX_FRAMES_MEASURED_PER_SESSION = MAX_IMAGES_PER_SESSION

# THERE IS DELIBERATELY NO TOTAL SESSION DEADLINE. A `SESSION_DEADLINE_S = 600.0` sat here,
# unenforced and unread - a trap, because activating it would have looked like turning on a
# limit that already existed. It never did.
# A review runs up to REVIEWER_MAX_ROUNDS (30) rounds against a provider with a 1800s
# timeout, and the session stays open across all of them. A 600s wall-clock deadline would
# expire a HEALTHY session while it sat idle waiting for the model to think, and the failure
# would surface as a renderer fault. Renderer timing must keep session-open, per-operation,
# provider-wait and reviewer-node budgets distinct; a future total or idle deadline needs its own
# design, starting with whether model-wait counts.

# The magic bytes a declared format must actually start with. Extensions are attacker-controlled
# (the manifest is untrusted), so a declared format is checked against CONTENT where content
# says anything at all - a .msh that is really a zip is refused rather than handed to a loader.
_FORMAT_SNIFF: dict[ArtifactFormat, tuple[bytes, ...]] = {
    ArtifactFormat.VTK_VTP: (b"<?xml", b"<VTKFile"),
    ArtifactFormat.VTK_VTU: (b"<?xml", b"<VTKFile"),
    ArtifactFormat.GMSH_MSH: (b"$MeshFormat",),
    ArtifactFormat.JSON: (b"{", b"["),
}


# prepared cuts: the pictures a cut comes home as when the interior cannot
#
# WHY THERE IS A SECOND KIND OF EVIDENCE AT ALL. A 283,410,955-cell conjugate plate's fluid
# interior is 10,366,611,525 bytes. The worker that would slice it is capped at 16 GiB and
# `sandbox/backend.py::_load_volume` reads the file whole, so shipping it home turns an honest
# "evidence missing" into an OOM kill of the Celery worker. The cut is therefore taken where the
# mesh is - on a 512 GB machine - and only the frames travel.
#
# THE MANIFEST SHAPE this module reads. It is authored by
# engines/cfmesh_conjugate/slices.review_slices and rendered by that engine's native half, which
# runs on the mesh VM; everything below is the READER's half of that contract:
#   inspection_regions: [ { name, kind, normal, origin, clip_box, label, why, plane_axes,
#                           frames: [ { scope: "whole" | "zoom",
#                                       image: "<workspace-relative .png>",
#                                       window_m: [width, height],   # metres, what it covers
#                                       metres_per_pixel: <float>,   # as PLANNED
#                                       um_per_pixel: <float>,
#                                       why_frame: "<text>",
#                                       feature_m: <optional, what the window was sized from>,
#                                       feature_pixels: <optional> } ] } ]
#
# `window_m` is the one field the home side can neither measure nor derive, and it is the field
# the whole scheme turns on. A picture of a plane with no stated window supports any claim at all:
# the same frame is a channel 15 pixels across or a channel under one pixel across depending on a
# number that is nowhere written down. So a frame without a usable window is NOT OFFERED.
CUT_FRAMES: tuple[str, ...] = ("whole", "zoom")
FRAME_WHOLE, FRAME_ZOOM = CUT_FRAMES

#: Where a frame's file is named. `image` is what the producer writes (native._review_cuts sets
#: `image` and `image_bytes` after the screenshot lands); `path` is accepted as the obvious
#: alternate so a hand-written manifest reads too. Two spellings for one field is a concession to
#: a contract whose halves live on opposite sides of a container boundary, not a pattern:
#: everything else here has exactly one name.
_PATH_KEYS = ("image", "path")

#: HOW MANY PIXELS A CELL MUST BE ACROSS before a frame counts as an inspection of its region.
#: engines/cfmesh_conjugate/slices.py says what each cut exists to answer: "how many cells are
#: across each channel" and "how many cells are through the channel's height". Both are counting
#: questions about CELLS, and a cell drawn one pixel wide has no countable edges - the renderer
#: draws an edge as a line about a pixel thick, so at 1 px/cell the line IS the cell. Two pixels
#: is the Nyquist limit and still not countable; four is the first size at which a cell reads as
#: an edge, an interior and an edge.
#:
#: MEASURED, AND IT REFUTES THE OBVIOUS WINDOW. Sizing the window for 4 px across the part's
#: FEATURE gives window = px * feature/4, so um/px = feature/4 - and the mesher pins about four
#: cells across that same feature, which makes feature/4 the cell. On job 0ff6f9e5 that is
#: 100.6312 um / 4 = 25.16 um/px against a pinned cell of 25.1578 um: EXACTLY ONE PIXEL PER CELL.
#: A window sized for four pixels across the channel therefore cannot answer either question the
#: cut exists for. Sizing from the CELL instead (px * cell / 4 = 8.05 mm on this part) puts the
#: same channel 15.6 px across and every cell in it 4 px across.
#:
#: THE COST OF GETTING THIS WRONG, both ways. Too low and the reviewer satisfies its coverage
#: floor on a frame where a channel is sub-pixel and returns a confident verdict about cells it
#: never saw - which is job 575f76e6, twice. Too high and a sound mesh gets no verdict. The
#: refusal names the number and the window that would fix it, so the second failure is loud and
#: repairable while the first is silent and shipped.
#:
#: THE PRODUCER SIZES ITS ZOOM WINDOW TO THIS SAME NUMBER (cfmesh_conjugate/slices.CELL_PIXELS)
#: and a test pins the two together, because the two halves of this contract live on opposite
#: sides of a container boundary: if they drift apart, every zoom frame the producer draws is
#: refused by the reader that asked for it, and the symptom is "no region offered" - which is
#: indistinguishable from the defect this whole path was written to fix.
COUNTABLE_CELL_PX = 4.0


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: HOW MANY CELLS MUST BE RESOLVED SIDE BY SIDE IN THE DRAWN PICTURE before a frame counts as an
#: inspection of its region. Counted along one scan row as ENCLOSED INTERIORS: a stretch of
#: lighter fill with a darker ink pixel on both sides of it, which is what one cell looks like
#: when both of its boundaries actually reached the image.
#:
#: WHY A SECOND FLOOR AT ALL, when COUNTABLE_CELL_PX already states one. Because the first one
#: cannot fail. The producer sizes its zoom window as `plan_cell / COUNTABLE_CELL_PX * width_px`
#: and this module divides that window back by `quality.near_wall_cell_m`, and on every mesh this
#: engine writes those are THE SAME NUMBER: near_wall_cell_m is background_cell_m / 2**levels,
#: which is what the planner pinned the fine cell to. Measured on job 0ff6f9e5,
#: 0.0004025248 / 2**4 = 2.51578e-05 = fine_cell_pinned_m, bit for bit, so `cell_px` comes out at
#: exactly 4.0 for every zoom frame ever drawn, whatever is in it. A floor that is satisfied by
#: construction is not a floor, it is a sentence that reads like one.
#:
#: MEASURED, job 0ff6f9e5, 2026-11-17, on the three zoom frames the producer actually draws (a
#: cut of the fluid/solid INTERFACE SURFACE - the interior was pruned before anyone read it, so
#: no cut of a real volume exists yet):
#:
#:     frame                     ink in frame   cells on the best row   what is in the picture
#:     along_the_flow_zoom          3.2585%               0             channel walls, no cells
#:     across_the_flow_zoom         0.5921%               1             three plenum rectangles
#:     through_the_plate_zoom       0.4142%               0             one circular port bore
#:
#: All three are declared countable by the scale alone. Two of them have no passage in them at
#: all. Against a VOLUME of known cell size drawn through the same renderer, this same count
#: comes back 320 on a 1280 px row at 4 pixels per cell and 0 at 2 pixels per cell, and 319 or
#: 320 at every one of 16 sub-pixel camera phases - so the floor separates the two cases by a
#: factor of eighty and does not wobble with where the camera happens to land.
#:
#: FOUR, BECAUSE THE QUESTION IS "HOW MANY CELLS ACROSS THIS PASSAGE". One or two enclosed
#: interiors is where a line happened to fall, not a passage anyone can count across; four is one
#: passage's worth, and it is the same four the planner holds a fine feature to (on this part
#: fine_surfaces[0].thickness_m / fine_cell_pinned_m is 4.0 exactly).
#:
#: THE COST OF GETTING THIS WRONG. Too low and the review discharges a region on a picture of a
#: port bore and returns a confident verdict, which is job 575f76e6 for the third time and is
#: strictly worse than the honest "evidence missing" the same job returns today. Too high and a
#: sound mesh declines with its reason named in the log and in the reviewer's own text.
COUNTABLE_CELLS_ON_A_ROW = 4

#: HOW MANY SCAN ROWS MUST CARRY A COUNTABLE RUN before the picture is of an ARRAY rather than of
#: a line. `cell_rows` has been measured and printed to the reviewer since round 2 and was never
#: compared to anything: the gate used `cells_on_best_row` alone, so ONE row out of 960 granted a
#: frame.
#:
#: TWELVE, AND IT IS A MEASUREMENT, NOT A ROUND NUMBER. Measured 2026-11-17 through this module's
#: own `_measure_png`, on grids I drew at 1280x960: a square patch of cells k on a side at
#: COUNTABLE_CELL_PX pixels per cell comes back at EXACTLY `k * (COUNTABLE_CELL_PX - 1)` rows -
#: 12, 15, 18, 24, 36, 48, 96, 192, 480, 720 for k = 4, 5, 6, 8, 12, 16, 32, 64, 160, 240. Three
#: rows per cell row, because the fourth is the drawn edge. So the smallest patch that answers
#: BOTH questions a cut exists for - COUNTABLE_CELLS_ON_A_ROW across AND the same number through -
#: is 4 * 3 = 12 rows, and this floor is set exactly there. It is the minimum over pitch, not an
#: accident of four: at 5, 6, 8 and 16 px per cell the same four-cell-deep patch measured 16, 20,
#: 28 and 60 rows, all above the bar.
#:
#: WHAT IT CATCHES, measured on the real plate (job 0ff6f9e5, cuts of fluid_to_solid.vtp): the
#: along_the_flow zoom drawn with a two-pixel edge resolves 39 enclosed interiors on its best row
#: and ONE row carries them. That is the forty-channel frame CELL_PITCH_TOLERANCE was written
#: for, caught here by a second, independent reading. The same plate's along_the_flow whole frame
#: resolves 54 on its best row over 4 rows - also refused. A fluid-only volume cut drawn through
#: the same renderer measured 720 rows, sixty times the bar.
#:
#: THE COST OF GETTING THIS WRONG. Too high and a genuinely thin cut - a passage four cells deep
#: and nothing else in frame - declines with its reason named. Too low and a single scan row that
#: happened to cross something discharges a region, which is the defect this closes.
#:
#: AND THIS FLOOR IS NO LONGER THE BINDING ONE ON A THIN CUT, which is stated here because the
#: paragraph above would otherwise read as the whole truth. `picture_structure.EXTENT_MIN`
#: measures the FOOTPRINT of the repeat and refuses a 1280 x 16 px strip at 0.0163 of the frame,
#: which is exactly the four-cell-deep patch this floor was set at: measured 2026-11-17, the
#: thinnest full-width patch that still discharges is 100 px (25 cell rows), not 16. It cannot be
#: otherwise - the speck EXTENT_MIN exists to refuse is 384 x 64 px, 0.0184 of the frame, so no
#: footprint bar separates the two - and EXTENT_MIN carries the measurement.
COUNTABLE_CELL_ROWS = 12

#: HOW MUCH OF A FRAME MUST HAVE ANYTHING DRAWN IN IT AT ALL before a count taken off it is a
#: count of the region. `ink_fraction` has been computed since round 2 (and printed to the
#: reviewer inside the refusal sentence) and was never compared to anything.
#:
#: ONE PART IN A THOUSAND, which is 1,229 pixels of a 1280x960 frame. MEASURED 2026-11-17, all of
#: it by me, through the production ArtifactResolver:
#:
#:     frame                                      ink        what it is
#:     a 25x21 px patch of 4 px cells        0.0427%    THE FRAME THAT SLIPPED THROUGH: 525 inked
#:                                                      pixels of 1,228,800, measured 6 enclosed
#:                                                      interiors at a 4.0 px pitch on its best
#:                                                      row, and came back counts_cells True
#:     through_the_plate_whole      REAL        0.0335%  198 sub-points: nothing to look at
#:     ---- this floor, 0.1000% ----
#:     across_the_flow_whole        REAL        0.3682%  scattered traces
#:     through_the_plate_zoom       REAL        0.4142%  one circular port bore
#:     across_the_flow_zoom         REAL        0.5921%  three plenum rectangles
#:     along_the_flow_whole         REAL        0.6834%  scattered traces
#:     along_the_flow_zoom          REAL        3.2585%  THE CHANNEL ARRAY - the one real frame
#:                                                      whose own pixels pass the structure test
#:     a fluid-only volume cut (drawn)         53.1250%  passages with metal between them
#:     a 4 px grid filling the frame (drawn)  100.0000%  solid metal
#:
#: So it clears the worst thing it has to refuse by 2.34x and sits 3.68x under the poorest real
#: frame, 32.6x under the only real frame that holds the array, and 531x under a volume cut. It is
#: also exactly ten times EMPTY_FRAME_PIXEL_SHARE, the blank-frame gate the slipped frame cleared
#: at 4.27e-4 - this is the next decade up, applied to a stronger claim.
#:
#: AND IT IS DELIBERATELY NOWHERE NEAR 25%. The brief this was written from put a real cut at
#: "25% to 100%", which is true of a VOLUME cut and is REFUTED for the frames that exist: the one
#: real frame on this part whose pixels hold the channel array is 3.2585% inked, because a cut of
#: the fluid/solid INTERFACE SURFACE is lines on white and not filled cells. A floor at 25% would
#: refuse the only honest frame anybody has drawn off this plate.
#:
#: IT IS NOT A QUALITY BAR and it cannot stand alone: solid metal is 100% inked and is worthless.
#: It only removes the class nothing else sees - a frame that is essentially empty.
EVIDENCE_INK_FLOOR = 1e-3

#: HOW FAR THE DRAWN SPACING MAY BE FROM THE CLAIMED CELL before what was found is not cells.
#: A factor of two either way.
#:
#: WHY THIS TEST EXISTS, and it is not a refinement of the count above - it is the difference
#: between counting cells and counting channels. MEASURED, job 0ff6f9e5, 2026-11-17: the same
#: along_the_flow zoom frame drawn with a two-pixel edge instead of a one-pixel one resolves 39
#: enclosed interiors on its best row, which sails over the floor above. Their measured spacing
#: is 32.0 px, and at that frame's 6.28945 um/px that is 201.26 um - the CHANNEL PITCH of this
#: part, eight times the 25.1578 um cell the same frame claims is 4 px across. What the picture
#: showed was forty channels, and a reviewer told "39 cells were resolved" would have counted
#: them as cells and discharged the region on a cut of a surface that has no cells in it at all.
#:
#: So the measured spacing is held against the claimed one. Measured through this same code: a
#: volume of known cell size at 4 pixels per cell comes back at 4.0 px spacing, a ratio of 1.00,
#: while the channel case above is 4.00 - the two are nowhere near each other and the band is
#: wide enough that a real mesh's cells, which are not all one size, still land inside it.
#:
#: THAT RATIO WAS WRITTEN HERE AS 8.00 AND IS NOW 4.00, re-measured 2026-11-18 on the same frame
#: (REAL PLATE, SURFACE, job 0ff6f9e5): the cell-run scan reads 16.0 px on it where the form this
#: note was written against read 32.0 px (see `_cell_runs`, which carries both measurements). The
#: frame is refused on the same test either way - 4.00 is outside this band by 2.67x where 8.00
#: was outside it by 5.33x - and the paragraph above is corrected rather than deleted because the
#: frame it describes is the reason this constant exists.
#:
#: NARROWED FROM 2.0 TO 1.5, AND IT IS THE WHOLE REASON THE PERIOD BANDS COULD GO. At 2.0 this
#: band admitted, exactly on its own boundary, the one frame nothing else here can refuse: an
#: array drawn at 8 px cells and DECLARED at a window that makes a cell 4 px, which shows a
#: coarser feature of this plate with the part's own shape and would have been read at twice the
#: magnification it really has. `ResolvedCut.period_cells_claimed`'s band was what caught it, and
#: that band also refused true pictures of real passages for their pitch (see
#: `shows_metal_between_passages`). The scale lie is this test's business, not a period test's.
#:
#: 1.5 IS WHERE THE MEASUREMENTS PUT IT. SYNTHETIC, VOLUME, through the production resolver: every
#: honest frame reads a drawn-to-claimed ratio of exactly 1.00, because the producer sizes the
#: window so one planned cell is exactly COUNTABLE_CELL_PX across - that is arithmetic, not luck.
#: The measured bad cases are exactly 2.00 (the declared-coarser frame), 4.00 (forty channels
#: counted as cells on the real plate's own along_the_flow zoom, REAL PLATE, SURFACE - 8.00 until
#: it was re-measured, see above) and 0.50 (a frame drawn at HALF the magnification it declares,
#: SYNTHETIC, VOLUME: cells 2 px across on the axis its structure repeats on against a claimed
#: 4.00). Nothing measured lands between 1.00 and 2.00, or between 0.50 and 1.00. At 1.5 the
#: honest frames clear by 1.50x and a factor of two either way is refused by 1.33x.
CELL_PITCH_TOLERANCE = 1.5

#: A sample below this is INK. The frames are drawn on a white background (the producer sets it),
#: so 255 is "nothing was drawn here" and anything darker is something that was.
_INK_MAX = 240

#: How much lighter a cell's interior has to be than the edge beside it before the two are
#: separable at all. Under this the row is a FLAT BAR: as inked as a correctly drawn one, with no
#: boundary in it that anyone could read. Measured: a four-cell channel at some camera phases
#: renders as one uniform 16 px bar with every pixel at the same grey.
_EDGE_CONTRAST_MIN = 24

#: DECODE CAPS, because the manifest is untrusted and zlib will inflate a few kilobytes into
#: gigabytes on request. Both the file and the pixel count are bounded BEFORE anything is
#: decompressed. 4096x4096 is sixteen times the 1280x960 the producer draws; the cost of refusing
#: something larger is one region declining with its reason logged, against an unbounded
#: allocation inside the very worker this whole change exists to keep alive.
_MAX_FRAME_PIXELS = 4096 * 4096
_MAX_PNG_BYTES = 64 * 1024 * 1024

_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


@dataclass(frozen=True)
class FrameInk:
    """What was MEASURED IN THE DRAWN PICTURE, as against what was computed about the plan.

    Every field here came out of the PNG's own pixels. Nothing in it can be stated by a manifest,
    which is the whole point: the scale a frame is drawn at is arithmetic the producer controls,
    and whether a cell survived to the image is not.
    """

    ink_fraction: float        # of the whole frame, how much of it has anything drawn in it
    cells_on_best_row: int     # the most enclosed cell interiors found on any one scan row
    cell_rows: int             # how many rows carried at least COUNTABLE_CELLS_ON_A_ROW of them
    cell_pitch_px: float       # the drawn spacing of those cells ALONG A ROW, 0 => none found
    # AND WHETHER THE PASSAGE ARRAY IS IN IT, measured HERE by `picture_structure.ink_structure`
    # over this side's own copy of the PNG. This is the field that used to be a boolean read out
    # of the manifest; see that module's header for the frame of solid metal that was discharged
    # on it. The four fields above say whether CELLS are resolved and these say whether the frame
    # landed on the PASSAGES at all - neither contains the other, which is why both are measured.
    shows_structure: bool = False
    structure_why: str = ""    # why not, in the detector's own words, "" when it found one
    repeats: int = 0           # how many times the structure repeats across the frame
    period_px: float = 0.0     # the lag it repeats at, in pixels
    strength: float = 0.0      # the peak of the ink profile's own autocorrelation, 0 to 1
    # HOW MUCH OF THE FRAME THE REPEAT REACHES ACROSS, and how wide the metal between two
    # passages is. Both come off the same two profiles and both are measured here; see
    # `picture_structure.EXTENT_MIN` for the 1.0000%-inked speck that cleared EVIDENCE_INK_FLOOR
    # ten times over and discharged a region, and WALL_CELLS_MIN for the plenum with ribs whose
    # every other figure - period included - is identical to the honest frame's.
    extent_fraction: float = 0.0
    bare_run_px: float = 0.0   # the TYPICAL metal between two passages, in px along that axis
    # AND THE NARROWEST WALL IN THE FRAME, because the median of a bimodal set is a width no wall
    # in the picture has: walls alternating 16 px and 1 px measure a median of 8.5 px and 2.125
    # drawn cells, and clear a floor neither of the two walls in them clears. This plate's own
    # z-normal cut holds fins that come in PAIRS, so that is its shape and not a contrived one.
    # The narrowest is what the floor is applied to and the median is published beside it; see
    # `picture_structure._bare_runs`.
    bare_run_min_px: float = 0.0
    # AND WHICH AXIS EVERY FIGURE ABOVE WAS READ ALONG, because two of them used to be read along
    # DIFFERENT ones and divided into each other. `repeat_axis` is "horizontal" when the structure
    # repeats across the frame's width and "vertical" when it repeats down its height;
    # `cell_pitch_down_px` is the drawn cell spacing scanned DOWN A COLUMN, which is the cell the
    # period belongs to when the repeat is vertical. MEASURED, SYNTHETIC, VOLUME, 1280x960:
    # passages running across the frame at a 32 px VERTICAL period, drawn with cells 4 px wide,
    # reported 8.00 drawn cells per period and a 3.996-cell wall off the HORIZONTAL cell pitch and
    # discharged the region.
    #
    # BOTH SCANS RUN ON EVERY FRAME. This comment said "it is only measured when the repeat is
    # vertical" and that has been untrue since round 7, which made the column scan unconditional
    # - `_measure_png` takes `down_pitch` before it knows the axis, and that function's own
    # docstring forty lines away says so in its heading. The stale sentence mattered because
    # round 8 put this field under the VERDICT: `ResolvedCut.repeat_pitch_px` reads it for every
    # vertically repeating frame and `cells_resolved` is checked against what it returns, so a
    # reader working from this comment would have believed the gate was reading a field that is
    # not measured on the frame in front of it. MEASURED, REAL PLATE, SURFACE, job 0ff6f9e5, all
    # six delivered frames: two of the frames whose `repeat_axis` is HORIZONTAL publish a
    # non-zero `cell_pitch_down_px` - along_the_flow whole at 8.0 px and through_the_plate zoom
    # at 63.0 px - which cannot happen at all if the column scan is conditioned on a vertical
    # repeat.
    repeat_axis: str = ""
    cell_pitch_down_px: float = 0.0
    # AND WHETHER THE FRAME COULD MEASURE ITS OWN PERIOD AT ALL. True means `period_px` above is
    # SILENCE and not a measurement: the lag that correlated best was shorter than the spacing
    # between the frame's own ink runs, which no periodic profile's period can be, so the
    # structure in the frame is coarser than the frame can measure. `max_lag_px` is the longest
    # repeat the search was able to test - about a sixth of the profile. MEASURED, SYNTHETIC,
    # VOLUME, 1280x960: honest passages at pitches of 216, 240, 256, 320 and 400 px all published
    # "period 2.0 px" at 0.966 to 0.981 of a perfect array. It is NOT `period_was_measured`, which
    # asks the different question of whether there is a drawn cell to count a period IN.
    period_unmeasurable: bool = False
    max_lag_px: int = 0
    # AND HOW FAR APART THE FRAME'S OWN INK RUNS STAND, which is the only figure that says what
    # window WOULD hold an array when no period could be measured. Carried because the session
    # reports the repair and not only the refusal: MIN_REPEATS of this spacing is the window that
    # would let the same cut prove the array it is already drawn on.
    run_spacing_px: float = 0.0


def _png_header(blob: bytes) -> tuple[int, int, int, int, int] | None:
    """(width, height, bit depth, colour type, interlace) from the IHDR, or None.

    READ FROM THE FILE, NEVER FROM THE MANIFEST. Every number the reviewer is told about a
    prepared cut is the stated window divided by these pixels, so a claimed frame size would let
    an untrusted manifest state any resolution it liked over a picture of anything.
    """
    if len(blob) < 33 or blob[:8] != _PNG_MAGIC or blob[12:16] != b"IHDR":
        return None
    w = int.from_bytes(blob[16:20], "big")
    h = int.from_bytes(blob[20:24], "big")
    if w <= 0 or h <= 0:
        return None
    return w, h, blob[24], blob[25], blob[28]


def _png_rows(blob: bytes, w: int, h: int, nch: int):
    """Yield each un-filtered scanline of an 8-bit, non-interlaced PNG, or nothing at all."""
    stride = w * nch
    want = (stride + 1) * h
    dec = zlib.decompressobj()
    parts: list[bytes] = []
    total = 0
    i = 8
    while i + 12 <= len(blob) and total < want:
        n = int.from_bytes(blob[i:i + 4], "big")
        typ = blob[i + 4:i + 8]
        if typ == b"IEND":
            break
        if typ == b"IDAT":
            # BOUNDED EVERY TIME ROUND. A `max_length` of 0 means UNLIMITED to zlib, which is the
            # opposite of what a cap is for, so the loop condition keeps the remainder positive.
            chunk = dec.decompress(blob[i + 8:i + 8 + n], want - total)
            parts.append(chunk)
            total += len(chunk)
        i += 12 + n
    data = b"".join(parts)
    if len(data) < want:
        return
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        f = data[pos]
        pos += 1
        cur = bytearray(data[pos:pos + stride])
        pos += stride
        if f == 1:
            for k in range(nch, stride):
                cur[k] = (cur[k] + cur[k - nch]) & 255
        elif f == 2:
            for k in range(stride):
                cur[k] = (cur[k] + prev[k]) & 255
        elif f == 3:
            for k in range(stride):
                a = cur[k - nch] if k >= nch else 0
                cur[k] = (cur[k] + ((a + prev[k]) >> 1)) & 255
        elif f == 4:
            for k in range(stride):
                a = cur[k - nch] if k >= nch else 0
                b = prev[k]
                c = prev[k - nch] if k >= nch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[k] = (cur[k] + (a if (pa <= pb and pa <= pc)
                                    else (b if pb <= pc else c))) & 255
        elif f != 0:
            return                        # an undefined filter: this is not a PNG we can read
        prev = cur
        yield cur


def _measure_png(path: Path) -> tuple[tuple[int, int], FrameInk] | None:
    """The frame's size AND what is drawn in it, both measured from the file. None if unreadable.

    ABOUT A SECOND FOR A 1280x960 FRAME, measured in this venv on a frame where every pixel is
    inked, which is the worst case. Six frames is a few seconds once, at session open, against a
    review that runs for minutes - and against the alternative, which is believing an untrusted
    number about a picture that nobody looked at.

    None, NOT an empty measurement, for anything that will not decode. The caller turns None into
    "this frame does not count", so a picture that cannot be read declines instead of passing.

    AND THE PASSAGE ARRAY IS MEASURED HERE TOO, off the SAME greyscale array this builds while it
    un-filters the PNG, by `picture_structure.ink_structure` - the producer's own detector, moved
    to a module both sides import. It used to run only on the mesh VM and arrive as a boolean in
    the manifest. One array of w*h bytes, bounded by `_MAX_FRAME_PIXELS`: 1.2 MB at the size the
    producer draws, 16.8 MB at the largest this will decode at all.

    BOTH SCANS RUN ON EVERY FRAME, which is this round's change. The column scan used to run only
    when the repeat was vertical, because that was the only question it was asked - and the
    measurement it takes decides a second one it was not being run for. MEASURED, SYNTHETIC,
    VOLUME, 1280x960, a claimed 4.00 px cell: an HONEST frame with the planner's own anisotropy,
    cells 8 px across by 4 px down, and the MAGNIFICATION LIE it is refused with, cells 8 px
    square declared at a window that makes a cell 4 px, read the SAME 8.0 px along their rows -
    and 4.0 and 8.0 down their columns. The scan separates them exactly, and
    `cell_pitch_down_px` was published 0.0 in both because the repeat in both is horizontal.
    See `ResolvedCut.cells_resolved`.

    WHAT THE SECOND SCAN COSTS, measured in this venv rather than estimated: 1.678 s against
    1.321 s on a frame of honest passages and 1.476 s against 1.083 s on a near-solid grid of
    cells, so about 0.35 s a frame - and nothing at all on a frame whose repeat is vertical
    (1.261 against 1.216), because that one was already taking it. Six frames at session open is
    about two seconds, against a review that runs for minutes, and against the alternative, which
    is refusing an honestly anisotropic frame and sending back "evidence missing".
    """
    try:
        if path.stat().st_size > _MAX_PNG_BYTES:
            return None
        blob = path.read_bytes()
    except OSError:
        return None
    head = _png_header(blob)
    if head is None:
        return None
    w, h, depth, ctype, interlace = head
    if depth != 8 or ctype not in _PNG_CHANNELS or interlace != 0 or w * h > _MAX_FRAME_PIXELS:
        return None
    nch = _PNG_CHANNELS[ctype]
    stride = w * nch
    ink_total = 0
    seen_rows = 0
    grey = bytearray()
    for row in _png_rows(blob, w, h, nch):
        seen_rows += 1
        if nch >= 3:
            # The MINIMUM channel, so a coloured fill on white reads as ink and a black edge
            # reads as the darkest thing on the row whatever the fill happens to be tinted.
            s = bytes(min(row[x], row[x + 1], row[x + 2]) for x in range(0, stride, nch))
        else:
            s = bytes(row[x] for x in range(0, stride, nch))
        grey += s
        ink_total += sum(1 for v in s if v < _INK_MAX)
    if seen_rows < h:
        return None                       # truncated or corrupt: measured nothing, claim nothing
    structure = _frame_structure(grey, w, h)
    if structure is None:
        # THE STRUCTURE MEASUREMENT IS LOAD-BEARING NOW, so a frame it could not be taken on is a
        # frame nothing is claimed about - the same answer this function already gives a PNG it
        # cannot un-filter. It fails closed rather than falling back on the manifest's boolean,
        # because falling back on the manifest's boolean IS the defect.
        return None
    # GENERATORS, not lists: one scan line is held at a time, so this adds w bytes and not w*h.
    best, cell_rows, best_pitch = _cell_runs(
        (grey[y * w:(y + 1) * w] for y in range(h)), COUNTABLE_CELLS_ON_A_ROW)
    # AND DOWN THE COLUMNS, ALWAYS. The period published above is read along whichever axis the
    # repeat is on, and dividing it by the cell drawn along the OTHER one is the defect
    # `repeat_axis` exists to close (see `ResolvedCut.repeat_pitch_px`); the cell down a column is
    # ALSO the one figure that separates an honestly anisotropic frame from a magnification lie
    # (see `ResolvedCut.cells_resolved`), and that question is asked of every frame whatever axis
    # it repeats on. A second pass over 1280x960 in pure Python costs about as much as the first,
    # which is a measured fraction of a second on a review that runs for minutes.
    axis = str(structure.get("scan") or "")
    down_pitch = _cell_runs((grey[x::w] for x in range(w)), COUNTABLE_CELLS_ON_A_ROW)[2]
    return (w, h), FrameInk(
        ink_fraction=ink_total / float(w * h) if w and h else 0.0,
        cells_on_best_row=best, cell_rows=cell_rows, cell_pitch_px=best_pitch,
        shows_structure=bool(structure.get("shows_structure")),
        structure_why=str(structure.get("why") or ""),
        repeats=int(structure.get("repeats") or 0),
        period_px=float(structure.get("period_px") or 0.0),
        strength=float(structure.get("strength") or 0.0),
        extent_fraction=float(structure.get("extent") or 0.0),
        bare_run_px=float(structure.get("bare_run_px") or 0.0),
        bare_run_min_px=float(structure.get("bare_run_min_px") or 0.0),
        repeat_axis=axis,
        cell_pitch_down_px=down_pitch,
        period_unmeasurable=bool(structure.get("period_unmeasurable")),
        max_lag_px=int(structure.get("max_lag_px") or 0),
        run_spacing_px=float(structure.get("run_spacing_px") or 0.0))


def _cell_runs(lines, countable: int) -> tuple[int, int, float]:
    """Scan a frame one line at a time for ENCLOSED CELL INTERIORS, and report the most found on
    any one line, how many lines carried a countable run, and the drawn spacing on the best line.

    THE SAME SCAN IN EITHER DIRECTION, which is the whole reason it is a function: `lines` is the
    frame's greyscale rows, or its columns, and nothing in here knows which. A cell interior is a
    run of ink LIGHTER THAN THAT LINE'S OWN MIDPOINT with ink on both sides of it - the midpoint
    is taken per line so an anti-aliased render's moving edge grey does not shift the count, which
    is measured over 24 offsets and edge widths in
    `tests/unit/sandbox/test_a_frame_shows_this_part_or_it_shows_nothing.py`.

    AND THE DRAWN CELL IS ONE CELL AND NOT A WALL JUMP, which is the defect this round fixes. The
    spacing used to be the median of the gaps between consecutive enclosed interiors on the best
    line - and an interior is only counted when ink closes it on BOTH sides, so the outermost
    interior of every passage is dropped and the gap from one passage to the next is a jump
    ACROSS THE BARE WALL. The population being taken a median of was two populations:

      - A PASSAGE TWO DRAWN CELLS WIDE leaves ONE enclosed interior per passage, so EVERY gap is
        a wall jump. MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell: the drawn cell read
        12 to 104 px against a claimed 4.00, and the frame was refused as a magnification lie at
        every wall from 1 to 24 cells - while its cells really are 4 px, which its own row bytes
        say: [0, 176, 176, 176, 0, 176, 176, 176, 255 x8, ...].
      - AT TWO INTERIORS PER PASSAGE the gaps alternate cell, wall, cell, wall and the median
        lands on whichever the band count makes it. MEASURED: passage 3 cells / wall 3 cells gives
        106 gaps and index 53 reads 20.0 px and was REFUSED; passage 3 / wall 2 gives 127 gaps and
        index 63 reads 4.0 px and was ADMITTED. A run was lost or kept on an off-by-one.

    SO THE CELL IS MEASURED WHERE A CELL IS, between the two ink runs that bound one interior:
    from the start of the ink run on its left to the start of the ink run on its right. That is
    one cell by construction - a boundary, a fill, the next boundary - and a wall jump cannot
    enter the population at all, because the samples between two passages are BACKGROUND and
    nothing spans them. It needs no statistic to separate the two groups and no tie to break.

    MEASURED, and THREE of these are figures the old form got right and this one does not move: a
    4 px grid filling the frame reads 4.0; cells drawn 8 px across read 8.0; an anti-aliased frame
    at a sub-pixel offset reads 4.0 - and the gap median reads 4.0, 8.0 and 4.0 on the same three
    frames.

    THE FOURTH FIGURE MOVED, and the heading above it claimed it had not. RE-MEASURED 2026-11-18,
    REAL PLATE, SURFACE, job 0ff6f9e5: the along_the_flow zoom drawn with a two-pixel edge reads
    16.0 px HERE, against 32.0 px under the gap median - the same 39 enclosed interiors on its
    best row, carried by ONE row, under both forms. The span between the two ink runs that bound
    one of those interiors is half the 32 px spacing between consecutive ones, and the spacing was
    what the old form took. 16.0 px is still FOUR TIMES the 4.00 px cell that frame claims, so
    `CELL_PITCH_TOLERANCE` refuses it on this reading for the same reason it refused the old one:
    what moved is the figure in the record, not the verdict.
    """
    best = 0
    cell_rows = 0
    best_pitch = 0.0
    for s in lines:
        n = len(s)
        lit = [v for v in s if v < _INK_MAX]
        if len(lit) < 2:
            continue
        dark, light = min(lit), max(lit)
        if light - dark < _EDGE_CONTRAST_MIN:
            continue                      # a flat bar: inked, with no boundary in it
        mid = (dark + light) / 2.0
        # One pass, as runs of BACKGROUND (0), BOUNDARY ink (1) and lighter INTERIOR ink (2).
        runs: list[tuple[int, int]] = []
        x = 0
        while x < n:
            v = s[x]
            kind = 0 if v >= _INK_MAX else (2 if v > mid else 1)
            runs.append((kind, x))
            x += 1
            while x < n:
                w = s[x]
                if (0 if w >= _INK_MAX else (2 if w > mid else 1)) != kind:
                    break
                x += 1
        # ENCLOSED ON BOTH SIDES. A run that leaves the line, or that fades straight into bare
        # background, is a cell one of whose boundaries was never drawn - and a cell with one
        # boundary is not a cell anybody counted.
        cells = sorted(runs[i + 1][1] - runs[i - 1][1]
                       for i in range(1, len(runs) - 1)
                       if runs[i][0] == 2 and runs[i - 1][0] == 1 and runs[i + 1][0] == 1)
        if len(cells) >= int(countable):
            cell_rows += 1
        if len(cells) > best:
            best = len(cells)
            best_pitch = float(cells[len(cells) // 2])
    return best, cell_rows, best_pitch


def _frame_structure(grey: bytearray, w: int, h: int) -> dict | None:
    """What `picture_structure.ink_structure` makes of this frame, or None if it could not run.

    numpy is imported HERE and not at module scope for the reason stated on
    `frame_shows_nothing`: this module is on the import path of render_inputs and must stay
    importable without the render stack. A frame nobody could measure is not evidence, so the
    caller turns None into "not offered" rather than into a default verdict.
    """
    try:
        import numpy as np

        return ink_structure(np.frombuffer(bytes(grey), dtype=np.uint8).reshape(h, w))
    except Exception as exc:  # noqa: BLE001 - never fail a review on a measurement of a picture
        logger.warning("review session: could not measure the structure of a %dx%d frame: %s",
                       w, h, exc)
        return None


def _finite_positive(value: Any) -> float:
    """A usable size, or 0.0 for anything that is not one.

    0.0 FOR EVERY BAD VALUE, including a NaN and an infinity, because every size here ends up as
    a divisor: a NaN window would make "micrometres per pixel" NaN and print as a number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    f = float(value)
    return f if math.isfinite(f) and f > 0.0 else 0.0


@dataclass(frozen=True)
class ResolvedCut:
    """One frame of one cut: a picture of a single plane, with the window it covers stated.

    A CUT IS NOT A VOLUME, and the reviewer has to be able to tell them apart. A volume is the
    thing itself, re-aimable; a frame is one plane at one fixed magnification, and anything
    outside its window is not in it. Everything needed to say that in words is here, so the
    claim a reviewer makes from a frame can be checked against the frame it made it from.
    """

    artifact_key: str                 # provenance by KEY, never a path, as for every artifact
    region_id: str
    scope: str                        # one of CUT_FRAMES
    path: Path
    pixels: tuple[int, int]           # measured from the PNG itself
    window_m: tuple[float, float]     # what the frame covers, as the producer states it
    feature_m: float = 0.0            # what the window was sized from, 0 => unstated
    cell_m: float = 0.0               # the PLANNED cell this window was sized to, 0 => unstated
    note: str = ""
    # WHAT WAS MEASURED IN THE PICTURE. None means the file could not be read and so nothing was
    # measured, which is NOT the same as a frame measured and found empty - but both decline.
    ink: FrameInk | None = None
    # WHERE THE WINDOW SITS, in the cut's own two in-plane axes, and who chose it. The producer
    # publishes both and its own code says why: a reader of a 4.5% window who is not told WHICH
    # 4.5% cannot check anything in it. The zoom is re-aimed at the densest patch of the cut
    # AFTER the plan was written, so this is the only record of where the camera ended up.
    centre_m: tuple[float, float] | None = None
    centre_source: str = ""
    # WHAT THE PRODUCER CLAIMED ABOUT THIS FRAME, from `frames[].content.shows_structure`.
    #
    # THREE-VALUED ON PURPOSE, and that is the whole correction. True, False and ABSENT are three
    # different facts and this used to collapse them into two: `shows_passages: bool` was
    # `content.get("shows_structure") is True`, so an ABSENT claim read as a denial and refused a
    # perfect frame - which breaks every archived or replayed manifest written before the field
    # existed. Absent is now NEUTRAL.
    #
    # IT MAY SUBTRACT AND IT MAY NOT ADD. A present False still vetoes: the producer had the whole
    # picture and the plan in hand and this side has one PNG, so its denial is worth taking. A
    # present True buys NOTHING, because the same measurement is now taken here off this side's
    # own copy of the pixels (see `FrameInk.shows_structure` and `picture_structure`). MEASURED:
    # a frame of solid metal with `shows_structure: true` beside it was discharging a region.
    claimed_structure: bool | None = None
    claimed_why: str = ""
    plane_axes: tuple[int, int] | None = None    # which world axes those two numbers are
    # The finest and coarsest cell anywhere in this mesh, for saying what `cell_m` is NOT. On
    # job 0ff6f9e5 they are 7.787 um and 678.88 um, a factor of 87 apart, while `cell_m` is one
    # number the PLAN chose - so "the mesh's own cell" was never a true description of it.
    cell_span_m: tuple[float, float] = (0.0, 0.0)
    # The region's own words, carried so whatever describes a frame to a reader does not have to
    # go back to the manifest for them. engines/cfmesh_conjugate/slices.py authors both: the
    # label is what the plane is, and `why` is the question the cut exists to answer.
    region_label: str = ""
    region_why: str = ""

    @property
    def m_per_px(self) -> float:
        # THE COARSER OF THE TWO AXES. A frame fits the limiting side of its window, so the other
        # side has margin; quoting the finer axis would state a resolution the picture does not
        # have everywhere in it.
        return max(self.window_m[0] / self.pixels[0], self.window_m[1] / self.pixels[1])

    @property
    def um_per_px(self) -> float:
        return self.m_per_px * 1e6

    @property
    def cell_px(self) -> float:
        return self.cell_m / self.m_per_px if self.cell_m else 0.0

    @property
    def feature_px(self) -> float:
        return self.feature_m / self.m_per_px if self.feature_m else 0.0

    @property
    def drawn_cell_m(self) -> float:
        """The size of the cells drawn in this frame ALONG ITS ROWS, measured off the picture.

        0.0 when no cell was resolved along a row. It is a real measurement of the picture -
        `cell_m` is a number the plan chose and this one came out of the image - but it is a
        measurement of ONE AXIS, and the axis is in the name because it used to not be.

        IT IS NOT "HOW BIG IS A CELL HERE", which is what this docstring said and is the defect
        round 10 closes. The structure in a frame repeats on whichever of its two axes it
        happens to repeat on, and every figure this frame is JUDGED on is counted in the cell
        drawn on THAT axis: `cells_resolved` compares `repeat_pitch_px` against `cell_px`, and
        `wall_cells` and `period_cells` both divide by it. On a vertically repeating frame this
        property is the other axis, so a line that published it as the frame's cell was
        publishing a figure off the axis nothing was decided on, under a docstring calling it
        the honest one.

        MEASURED, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the production
        ArtifactResolver: cells 8 px ACROSS by 4 px DOWN repeating VERTICALLY, at passages of 4,
        8 and 12 cells by walls of 2, 4 and 8 cells, are ADMITTED at all nine shapes - the cell
        on the repeat axis is 4.0 px and agrees with the window - and this property reads 8.0 px,
        50.32 um, EXACTLY TWICE the planned cell, while the figure the verdict was taken on and
        the figure the wall and the period on the same line are counted in is 4.0 px, 25.16 um.
        Anisotropic near-wall cells are ordinary on this part (the planner pins the fine cell at
        the thin wall and lets it run long the other two ways), so this is not a contrived shape.

        BOTH READINGS ARE PUBLISHED NOW AND EACH NAMES ITS AXIS; see `drawn_cell_repeat_m` for
        the one the verdict is taken on, and `scene_text._measured_clause` for the sentence. This
        property is kept, and kept as the row reading, because it IS a measurement of the picture
        and because a reader comparing the two is how anisotropy becomes visible at all. Nothing
        gated on it before this round and nothing gates on it now.
        """
        if self.ink is None or not self.ink.cell_pitch_px:
            return 0.0
        return self.ink.cell_pitch_px * self.m_per_px

    @property
    def drawn_cell_repeat_m(self) -> float:
        """The size of the cell drawn ALONG THE AXIS THE STRUCTURE REPEATS ON, in metres.

        THE FIGURE THE VERDICT WAS TAKEN ON. It is `repeat_pitch_px` in metres, and
        `repeat_pitch_px` is what `cells_resolved` holds against the cell the window declares and
        what `wall_cells` and `period_cells` divide by. A reader asking "does the drawn cell
        match the window this frame declares" is asking about THIS number and no other.

        0.0 WHEN NO CELL WAS RESOLVED ON THAT AXIS, which is silence and not a denial, exactly as
        `repeat_pitch_px` is: a frame of horizontal passages drawn one cell deep resolves no
        interior down any column, so there is nothing to publish and `period_was_measured` says
        so in those words. It is also 0.0 on a frame with no measured structure at all, where
        there is no repeat axis for a cell to be read along.
        """
        if self.ink is None:
            return 0.0
        pitch = self.repeat_pitch_px
        return pitch * self.m_per_px if pitch > 0.0 else 0.0

    @property
    def shows_passages(self) -> bool:
        """Whether THE PASSAGE ARRAY IS IN THIS FRAME, measured on THIS side's copy of the PNG.

        THE MEASUREMENT IS THIS SIDE'S AND THE CLAIM MAY ONLY SUBTRACT. `ink.shows_structure`
        comes from `picture_structure.ink_structure` run here, over the greyscale rows
        `_measure_png` already built. `claimed_structure` is the producer's own answer to the same
        question, arriving inside an untrusted manifest: a present False still vetoes, a present
        True buys nothing, and an ABSENT claim is neutral.

        WHY ABSENT IS NEUTRAL NOW. It used to refuse. A manifest written before `content` existed,
        or replayed out of an archive, carries no such field - and a perfect frame was being
        refused for the absence of a claim this side no longer needs.
        """
        if self.ink is None or not self.ink.shows_structure:
            return False
        return self.claimed_structure is not False

    @property
    def repeat_pitch_px(self) -> float:
        """The spacing of the drawn cells ALONG THE AXIS THE STRUCTURE REPEATS ON, in pixels.

        THE DEFECT THIS CLOSES. `picture_structure.ink_structure` reports the period on whichever
        of the frame's two axes shows the repeat, and `_measure_png` used to measure the drawn cell
        only along ROWS - so a VERTICAL period was being divided by a HORIZONTAL cell. MEASURED,
        SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the production
        ArtifactResolver: passages running across the frame at a 32 px vertical period, drawn with
        cells 4 px wide, reported 8.00 drawn cells per period and a 3.996-cell wall and DISCHARGED
        its region. In the direction the structure actually repeats there is half a cell per
        period. Cells 16, 32 and 64 px tall gave byte-for-byte identical frames and therefore
        identical figures, so the reading could not see the difference at all.

        Anisotropic near-wall cells are ordinary on this part - the planner pins the fine cell at
        the thin wall's thickness and lets it run long in the other two directions - so a frame
        whose cells are 4 px one way and 64 px the other is not a contrived case.

        0.0 WHEN THE CELL ON THAT AXIS WAS NOT RESOLVED, which is silence and not a denial: the
        defect frame above resolves no interior down any column, so there is no drawn cell to
        count its vertical period in and `period_was_measured` says so in those words.

        AND A LIMIT THIS DID NOT FIX, measured and stated rather than left to be found. A frame of
        horizontal passages drawn SEVERAL CELLS DEEP puts a full-width horizontal cell boundary on
        one row in every `cell_h`, and those rows carry four times the ink of the rows between
        them - so once they are more than about 5% of the frame the vertical profile's 95th
        percentile lands on them and the period measured is the CELL LATTICE, not the passage
        pitch. MEASURED, SYNTHETIC, VOLUME, 1280x960: passages 64 px deep on a 128 px vertical
        pitch with 16 px cells came back 7 repeats at a 2.0 px period, 0.940, and declined as
        holding no structure. That is the same class as the wireframe lattice `period_was_measured`
        was written for, it FAILS CLOSED, and it is not what any of the three defects this round
        closed were about - so it is recorded here and not patched.
        """
        ink = self.ink
        if ink is None:
            return 0.0
        return ink.cell_pitch_down_px if ink.repeat_axis == "vertical" else ink.cell_pitch_px

    @property
    def period_cells(self) -> float:
        """The frame's own repeat, counted in the CELLS DRAWN IN IT. 0.0 when it was not measured.

        PURE PIXELS ON BOTH SIDES, AND BOTH READ ALONG THE SAME AXIS. A period in metres is the
        measured period times `m_per_px`, and `m_per_px` is `window_m` divided by the frame's
        pixels - the window being a number the manifest states. This ratio has no manifest number
        in it at all, so the magnification cancels: the same structure drawn at any scale gives
        the same answer. Which axis it is read on is `repeat_pitch_px`.

        IT IS NO LONGER HELD TO A BAND. Rounds 4 and 5 required it to sit within PERIOD_MATCH of
        PLAN_CELLS_ACROSS_A_WALL, which is a bar on passage width PLUS wall width when the plan
        fixes only the wall; see `shows_metal_between_passages` for the true pictures that refused
        and the deletion. It is measured, published and put in front of the reviewer beside the
        plan's own figure, and it gates nothing.
        """
        from meshpipeline.sandbox.picture_structure import period_in_cells

        if self.ink is None:
            return 0.0
        return period_in_cells(self.ink.period_px, self.repeat_pitch_px)

    @property
    def period_cells_claimed(self) -> float:
        """The same repeat, counted in the cell THE STATED WINDOW SAYS this frame is drawn at.

        REPORTED, NOT GATED, and it used to be both. A frame drawn at twice the magnification it
        claims shows a coarser feature with the part's own shape - MEASURED, SYNTHETIC, VOLUME: an
        array drawn at 8 px cells on a 64 px period and declared at the honest 6.28945 um/px
        window reports 8.00 drawn cells per period and 4.00 cells of wall, the honest frame's
        figures exactly, while its period in metres is 402.52 um, a DIFFERENT feature of this
        plate. What catches that is the drawn cell against the claimed one (CELL_PITCH_TOLERANCE,
        now narrow enough to refuse a factor of two), not a band on this figure - and holding a
        band here was costing true pictures their run. It is published because the two readings
        disagreeing is the thing a reader needs to see.
        """
        from meshpipeline.sandbox.picture_structure import period_in_cells

        if self.ink is None:
            return 0.0
        return period_in_cells(self.ink.period_px, self.cell_px)

    @property
    def wall_cells(self) -> float:
        """How many drawn cells of METAL stand between the two passages WITH THE LEAST BETWEEN
        THEM. 0.0 when unmeasured.

        AN OCCUPANCY MEASUREMENT: the narrowest run of samples with NO INK IN THEM along the
        repeat axis, over the cell drawn on that same axis. It used to be the period times one
        minus a per-column ink DENSITY share, which read a shallow plenum column as metal; see
        `picture_structure.WALL_CELLS_MIN` for the field of coolant with no metal in it that
        measured a 3.9968-cell wall and discharged its region.

        THE NARROWEST AND NOT THE MEDIAN, which is the second correction. MEASURED, SYNTHETIC,
        VOLUME, 1280x960 at 4 px per cell: passages 4 cells wide whose walls ALTERNATE 16 px and
        1 px read a median bare run of 8.5 px - 2.125 cells - and cleared a floor of 2.00 that
        neither the 4-cell wall nor the quarter-cell one sits above. Half the walls in that
        picture are the 1-cell rib `WALL_CELLS_MIN` says cannot be told from a 1-cell wall. Where
        the walls really are uniform the two figures are identical, so nothing measured before
        this round moves: the honest 4-cell wall reads 4.00 either way.

        `wall_cells_median` is the typical wall beside it, and both are put in front of the
        reviewer, because a frame refused on its narrowest wall with a healthy median is a frame
        most of which is sound - and the reader is the one who can tell which part was asked about.
        """
        from meshpipeline.sandbox.picture_structure import wall_in_cells

        if self.ink is None:
            return 0.0
        return wall_in_cells(self.ink.bare_run_min_px, self.repeat_pitch_px)

    @property
    def wall_cells_median(self) -> float:
        """The TYPICAL metal between two drawn passages, in drawn cells. Reported, never gated.

        It is the figure `wall_cells` was until this round. It is published because the two
        disagreeing is what tells a reader that the walls in this frame are not all one width.
        """
        from meshpipeline.sandbox.picture_structure import wall_in_cells

        if self.ink is None:
            return 0.0
        return wall_in_cells(self.ink.bare_run_px, self.repeat_pitch_px)

    @property
    def period_was_measured(self) -> bool:
        """Whether the frame said anything about its own period in cells at all.

        FALSE IS NOT A DENIAL HERE, it is silence, and the two have to be told apart in the words
        a reviewer reads. A wireframe lattice at 8 px per cell resolves 159 repeats at 0.994 of a
        perfect array and NO enclosed cell interior, so there is no drawn cell to count the period
        in - and the frame was being introduced as "the PASSAGE ARRAY IS IN THIS FRAME ... every
        8.0 px", which is the cell lattice and not a passage.
        """
        return self.period_cells > 0.0

    @property
    def shows_metal_between_passages(self) -> bool:
        """Whether REAL METAL stands between the passages in this frame - the one thing that tells
        a cut of a passage array from a cut of a plenum.

        ONE MEASUREMENT AGAINST ONE PLAN FIGURE, and it used to be three. The frame's own repeat
        is reported and no longer gated; what is required is that the metal between two passages
        measures at least WALL_CELLS_MIN - half the PLAN_CELLS_ACROSS_A_WALL cells the planner
        puts across this part's thin wall - counted in the cells drawn along the axis the
        structure repeats on.

        WHAT THE WINDOW IS, in terms a reader can check against a plan: ANY pitch and ANY passage
        width, with at least two drawn cells of bare metal between passages. MEASURED, SYNTHETIC,
        VOLUME, 1280x960 at 4.00 px per cell, through the production ArtifactResolver, passages
        4 cells wide:

            wall, in cells    1     2     3     4     5     6     8     12    20
            round 5          no    no    YES   YES   YES   YES   no    no    no
            now              no    YES   YES   YES   YES   YES   YES   YES   YES

        and passages 8 cells / wall 4, and passages 12 cells / wall 4, both refused by round 5 for
        their pitch, are admitted. Round 5's window was 7 to 10 drawn cells of pitch - a factor of
        1.43 on one quantity - and every frame outside it above is a TRUE PICTURE of real passages
        with real metal between them. A gate that refuses one of those costs the user a run and
        sends back "evidence missing", which is the failure this whole line of work exists to
        remove.

        WHY THE PITCH BAND WENT AND THE WALL FLOOR STAYED. The plan pins the fine cell AT the thin
        wall's thickness over a four-cell floor, so the WALL is a quantity the plan fixes and the
        PITCH - passage plus wall - is not: the passage width is the part's, not the planner's.
        Round 5's own write-up could not be satisfied alongside its band either, listing a
        4.00-drawn-cell reading as the part while the wall floor refused it. Everything the band
        was measured to keep out is kept out by the wall: the 402.525 um and 805.050 um features
        of this plate were drawn as a field of coolant with a 2 px rib, which measures half a cell
        of wall.

        AND IT MAY ONLY SUBTRACT. Both sides of the figure are pixels measured off this side's own
        copy of the PNG, so there is no value any manifest can state that turns a no here into a
        yes. The extent is NOT checked here: it is a property of the frame and not of the part, so
        it is measured where the rest of the frame is, inside `ink_structure`, and arrives through
        `shows_passages`.
        """
        from meshpipeline.sandbox.picture_structure import WALL_CELLS_MIN

        if not self.shows_passages or self.ink is None:
            return False
        return self.wall_cells >= WALL_CELLS_MIN

    @property
    def shows_cells(self) -> bool:
        """Whether CELLS WERE FOUND IN THE PICTURE - measured, never derived from the plan.

        FOUR MEASURED FACTS, AND EVERY ONE OF THEM CAME OUT OF THIS SIDE'S COPY OF THE PNG.
        The passage array is in the frame (see `shows_passages`); enough of the frame has anything
        drawn in it at all (EVIDENCE_INK_FLOOR, which refuses the 525-inked-pixel frame that
        measured six interiors and discharged a region); enough enclosed interiors to count across
        a passage, on enough ROWS to be an array rather than a line (COUNTABLE_CELLS_ON_A_ROW and
        COUNTABLE_CELL_ROWS - one lucky row of 960 used to be enough); and a drawn spacing that
        agrees with the size this frame claims a cell is (CELL_PITCH_TOLERANCE, which is what
        stops a picture of forty channels being read as thirty-nine cells).

        An unstated cell size collapses the band to nothing and answers NO, which is the same
        fail-closed answer the scale test gives it.

        AND THERE IS METAL BETWEEN THE PASSAGES, which the four above do not contain: see
        `shows_metal_between_passages` for the plenum with ribs whose period, repeats, strength,
        drawn pitch and row count are identical to the honest frame's, and for the field of
        coolant with no metal anywhere in it that measured a four-cell wall. It is tested LAST
        because it is counted in the drawn cell, so the tests that establish there is a drawn cell
        have to have passed first - otherwise the reason reported would be about the metal in a
        frame that resolved no cell to measure it in.
        """
        return (self.shows_passages and self.cells_resolved
                and self.shows_metal_between_passages)

    @property
    def cells_resolved(self) -> bool:
        """Whether CELLS were resolved in this picture, said apart from whether they are the
        part's cells, because the two have different repairs and an operator needs to be told
        which one this frame needs."""
        ink = self.ink
        if ink is None:
            return False
        if ink.ink_fraction < EVIDENCE_INK_FLOOR:
            return False
        if ink.cells_on_best_row < COUNTABLE_CELLS_ON_A_ROW:
            return False
        if ink.cell_rows < COUNTABLE_CELL_ROWS:
            return False
        claimed = self.cell_px
        if claimed <= 0.0:
            return False
        # THE CLAIMED CELL IS HELD AGAINST THE CELL DRAWN ON THE AXIS THE STRUCTURE REPEATS ON,
        # and on that axis alone. It is the axis every other figure compared to a plan number is
        # counted on - `wall_cells` and `period_cells` both divide by `repeat_pitch_px` - so
        # granting here off a DIFFERENT axis makes the two halves of this test talk about two
        # different directions, and the figures published are then the wrong ones.
        #
        # MEASURED, SYNTHETIC, VOLUME, 1280x960, a claimed 4.00 px cell, through the production
        # ArtifactResolver, on the any-axis form this replaces: cells drawn 2 px ACROSS by 4 px
        # DOWN with honest bare metal between the passages, repeating HORIZONTALLY, were ADMITTED
        # at passage 8c/wall 2c, 4c/2c and 12c/2c because the COLUMN agreed with the claim while
        # the row - the repeat axis, the one the wall and the period are counted on - read 2.0 px
        # against a claimed 4.00, exactly the factor of two CELL_PITCH_TOLERANCE exists to refuse.
        # The record then carried doubled figures: the 8c/8c frame's 16.0 px of metal was
        # published as "8.00 cell(s)" and its period as "16.00 drawn cell(s)", both twice what the
        # declared scale makes them.
        #
        # WHAT THIS CANNOT DO, measured and stated rather than left to be found. An honestly
        # ANISOTROPIC frame - cells 8 px across by 4 px down, the planner's own shape - and the
        # MAGNIFICATION LIE it has to be told from - cells 8 px SQUARE declared at a window that
        # makes a cell 4 px - read the SAME 8.0 px along the axis they repeat on. On that axis
        # there is no measurement that separates them, so the anisotropic frame is refused with
        # the lie, at the drawn cell and not at the wall. Granting it off the column is what
        # admitted the fake above, and the two cannot be had at once.
        #
        # AND WHAT THAT COSTS IS AN ORIENTATION CLASS, NOT A LIST OF FRAMES, which is round 9's
        # correction to round 8's own write-up. That cost was recorded as a handful of further
        # frames, as though it were an enumerable set. IT IS A CLASS, and it is every frame whose
        # COARSE cell lies on the axis its structure repeats on, whatever its pitch and whatever
        # its wall. RE-MEASURED 2026-11-18, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px
        # cell, a 217-frame set driven through the production ArtifactResolver in both arms:
        # round 8 moved 27 frames, every one of them from admitted to refused and none the other
        # way. 10 of the 27 are the magnification fakes this test was written for, drawn 2.0 px
        # per cell on the repeat axis against a claimed 4.00. THE OTHER 17 ARE HONEST PICTURES
        # with their coarse cell on the repeat axis - cells 8 and 16 px across repeating
        # horizontally, and cells 8 and 16 px tall repeating vertically - at walls from 2.00 to
        # 8.00 cells. The same shape with its FINE cell on the repeat axis is untouched.
        #
        # SO ANISOTROPY DECIDES NOTHING HERE AND THE ORIENTATION DECIDES EVERYTHING, which is
        # the second correction: this was written up as a statement about CELL SHAPE - that a
        # frame with the fine cell on the repeat axis is still admitted - and a cell shape is
        # not a property a frame has on its own. MEASURED on the same set, cells 4 px ACROSS by
        # 8 px DOWN, one shape, both orientations, with honest metal between the passages:
        # repeating HORIZONTALLY it reads 4.0 px on the repeat axis and is ADMITTED at passage
        # 4c/wall 4c, 8c/2c and 12c/4c; repeating VERTICALLY the same shape reads 8.0 px on the
        # repeat axis and is REFUSED at pitches of 32, 48, 64, 80, 96, 112 and 128 px. Cells 4
        # by 2 and 4 by 16 are admitted too, on the same rule and not on their shape. There is
        # no cell shape this gate admits or refuses; there is one measurement, the cell drawn
        # along the axis the wall and the period are counted on, against the cell the window
        # declares.
        #
        # AND THE "7 OF 30 ADMITTED ON THE VERTICAL AXIS" FIGURE ROUND 9 REPORTED IS WITHDRAWN,
        # because the family it was taken on cannot be the protected window's. RE-DRIVEN
        # 2026-11-18, SYNTHETIC, VOLUME, 1280x960 at a claimed 4.00 px cell, through the
        # production ArtifactResolver, on ONE 30-frame grid - passages of 2, 4, 6, 8 and 12 cells
        # by walls of 1, 2, 3, 4, 6 and 8 cells - drawn three ways that differ ONLY in which cell
        # size lands on the axis the structure repeats on:
        #
        #     family                                              repeat-axis cell   admitted
        #     VERTICAL, cells 4x4 (the declared cell on that axis)      4.0 px        25 of 30
        #     VERTICAL, cells 4x8 (the COARSE cell on that axis)        8.0 px         0 of 30
        #     HORIZONTAL, cells 4x4, the same grid                      4.0 px        25 of 30
        #
        # THE VERTICAL AND HORIZONTAL RATES ARE THE SAME NUMBER on the same shape, 83.3%, and
        # the five refusals in each are exactly the wall=1 column - refused for the wall and not
        # for the axis, which is the protected record's own "the one-cell wall is the whole of
        # what is refused inside it". A family whose coarse cell lands on the repeat axis gives
        # 0 of 30, not 7. So 7 of 30 is neither of these two families and carries no information
        # about the protected window; what it is consistent with is a set mixing the two, which
        # is a statement about the arm's own frame set and not about this gate. The figure that
        # stands is 25 of 30 vertical, beside 109 of 120 horizontal and 44 of 56 vertical in the
        # independently verified record.
        drawn = self.repeat_pitch_px
        if drawn <= 0.0:
            return False                  # no cell on that axis states nothing: see `_cell_runs`
        lo, high = claimed / CELL_PITCH_TOLERANCE, claimed * CELL_PITCH_TOLERANCE
        return lo <= drawn <= high

    @property
    def counts_cells(self) -> bool:
        """Whether a cell is countable in this frame - and so whether it covers its region.

        TWO TESTS, AND THE SECOND ONE IS THE ONE THAT CAN FAIL. The first asks whether the window
        was sized finely enough for a cell to be four pixels across; on every mesh this engine
        writes that is true BY CONSTRUCTION and cannot come out any other way (see
        COUNTABLE_CELLS_ON_A_ROW for the arithmetic, which is exact). The second asks whether
        cells were actually found in the drawn picture, and on the three frames this engine
        produces today the answer is no for all three, two of which contain no passage at all.

        AN UNMEASURED FRAME ANSWERS NO. Nothing is being accused of being bad: the frame simply
        cannot be SHOWN to resolve the thing the cut exists to count, and a floor satisfied by a
        frame that cannot be shown to answer the question is not a floor. The cost of failing
        this way is today's honest "evidence missing". The cost of failing the other way is a
        confident verdict on a picture of a port bore.
        """
        return self.cell_px >= COUNTABLE_CELL_PX and self.shows_cells

    @property
    def centre_text(self) -> str:
        """Where this window sits, in words, or "" when the producer did not say.

        NAMED BY WORLD AXIS, not by "horizontal and vertical": a reader asked to check what is in
        a 4.5% window has to be able to put it back on the part, and "h = 30.92 mm" cannot be.
        """
        if self.centre_m is None:
            return ""
        names = ("x", "y", "z")
        ax = self.plane_axes if self.plane_axes else (0, 1)
        return ", ".join(f"{names[a]} = {v * 1e3:.4g} mm"
                         for a, v in zip(ax, self.centre_m, strict=False))


def _finite(value: Any) -> float | None:
    """Any finite number, sign and all. A centre is a coordinate, not a size: it may be negative
    and may legitimately be zero, so it cannot go through `_finite_positive`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _cut_pair(raw: Any) -> tuple[float, float] | None:
    """A published pair of coordinates, or None for anything that is not one."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    a, b = _finite(raw[0]), _finite(raw[1])
    return None if a is None or b is None else (a, b)


def _plane_axes(raw: Any) -> tuple[int, int] | None:
    """The two world axes a cut lies in, as the producer publishes them, or None."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        a, b = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    return (a, b) if 0 <= a <= 2 and 0 <= b <= 2 and a != b else None


def _cut_window(raw: Mapping[str, Any]) -> tuple[float, float] | None:
    win = raw.get("window_m")
    if isinstance(win, (list, tuple)) and len(win) == 2:
        w, h = _finite_positive(win[0]), _finite_positive(win[1])
    else:
        w, h = _finite_positive(raw.get("width_m")), _finite_positive(raw.get("height_m"))
    return (w, h) if w and h else None


def manifest_cut_claims(manifest: Mapping[str, Any] | None) -> dict[str, int]:
    """How many cut frames the manifest CLAIMS per region. Pure: it touches no file.

    The claim and the file are two different authorities, and engines/base.py needs the claim: it
    decides whether a run that published no volume still owes a region inspection, and it must do
    that without a filesystem.
    """
    out: dict[str, int] = {}
    for entry in (manifest or {}).get("inspection_regions") or []:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        frames = entry.get("frames")
        if isinstance(name, str) and name.strip() and isinstance(frames, (list, tuple)):
            # A FRAME WITH NO FILE IS NOT A CLAIM. slices.py publishes the windows whether or not
            # the render happened, so "frames exist" and "a picture exists" are different facts.
            n = sum(1 for f in frames if isinstance(f, Mapping)
                    and any(isinstance(f.get(k), str) and f[k].strip() for k in _PATH_KEYS))
            if n:
                out[name] = n
    return out


class ResolvedEvidence(dict):
    """The resolved artifact map, with the prepared cuts that came home beside it.

    A DICT SUBCLASS RATHER THAN A SECOND PARAMETER, and that is a boundary decision rather than a
    taste one. `ReviewRenderer.open(context, artifacts)` is declared in
    contracts/review_evidence.py and implemented by every engine bundle, so widening the port for
    one engine's extra evidence changes five files that have nothing to do with this change.
    Every ITEM of this mapping is still a ResolvedArtifact, so a renderer that knows nothing about
    cuts behaves exactly as it did, and `render_adapter.build_backend` is the only reader of the
    extra channel.
    """

    def __init__(self, *args: Any, cuts: Mapping[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.cuts: dict[str, tuple[ResolvedCut, ...]] = dict(cuts or {})


class ArtifactResolver:

    def __init__(self, workspace: Path, manifest: dict[str, Any]) -> None:
        # realpath the workspace ONCE: every later comparison is real-to-real, so a symlinked
        # workspace root cannot make an escape look like containment.
        self._root = Path(workspace).resolve(strict=False)
        self._manifest = manifest or {}

    def _lookup(self, key: str) -> str | None:
        node: Any = self._manifest
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) and node.strip() else None

    def confine(self, key: str, raw: str) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        # Resolve BEFORE comparing. `ws/link -> /etc` is inside the workspace by name and
        # outside it in fact; only the resolved path tells the truth.
        real = candidate.resolve(strict=False)
        try:
            real.relative_to(self._root)
        except ValueError:
            raise ReviewRenderError(
                ReviewEvidenceFailure.EVIDENCE_MISSING,
                # The key, never the path: an operator-visible error must not leak the box's
                # filesystem layout.
                f"artifact {key!r} resolves outside the job workspace") from None
        return real

    def resolve(self, req: RenderArtifactRequirement) -> ResolvedArtifact | None:
        return self.resolve_raw(req.artifact_key, self._lookup(req.artifact_key),
                                req.allowed_formats, required=req.required)

    def resolve_raw(self, key: str, raw: Any, allowed_formats: tuple[ArtifactFormat, ...],
                    *, required: bool) -> ResolvedArtifact | None:
        if not isinstance(raw, str) or not raw.strip():
            if required:
                raise ReviewRenderError(
                    ReviewEvidenceFailure.EVIDENCE_MISSING,
                    f"required artifact {key!r} is not in the manifest")
            return None

        # Unsafe raises out of here for required AND optional alike.
        path = self.confine(key, raw)

        # polyMesh is a DIRECTORY; every other declared format is a file.
        want_dir = ArtifactFormat.OPENFOAM_POLYMESH in allowed_formats
        if not (path.exists() if want_dir else path.is_file()):
            if required:
                raise ReviewRenderError(
                    ReviewEvidenceFailure.EVIDENCE_MISSING,
                    f"required artifact {key!r} does not exist")
            logger.info("artifact %s declared but not present - dependent views unavailable", key)
            return None

        if path.is_dir():
            return ResolvedArtifact(key, path, ArtifactFormat.OPENFOAM_POLYMESH)

        # Unusable content raises for required AND optional alike: an artifact that is present
        # but not what it claims is refused before any loader sees it, never quietly treated as
        # absent because it happened to be optional.
        fmt = self._sniff(key, path, allowed_formats)
        return ResolvedArtifact(artifact_key=key, path=path, fmt=fmt)

    def _sniff(self, key: str, path: Path,
               allowed_formats: tuple[ArtifactFormat, ...]) -> ArtifactFormat:
        try:
            head = path.open("rb").read(512)
        except OSError as exc:
            raise ReviewRenderError(
                ReviewEvidenceFailure.EVIDENCE_MISSING,
                f"artifact {key!r} could not be read") from exc

        for fmt in allowed_formats:
            sigs = _FORMAT_SNIFF.get(fmt)
            if sigs is None:
                return fmt          # a format with no signature (e.g. polyMesh dirs, STL text)
            if any(head.lstrip()[:len(s)] == s for s in sigs):
                return fmt
        raise ReviewRenderError(
            ReviewEvidenceFailure.EVIDENCE_MISSING,
            f"artifact {key!r} does not match any declared format "
            f"({', '.join(f.value for f in allowed_formats)})")

 # prepared cuts
    def _mesh_cell_m(self) -> float:
        """The finest cell the mesh's own measurements declare, for the countability floor.

        `quality.near_wall_cell_m` is the sizing the channels were pinned to and is the cell the
        cuts exist to count (25.1578 um on job 0ff6f9e5). It is already in every manifest this
        engine writes, so nothing new has to be published for the floor to be checkable.
        `min_cell_m` is deliberately NOT the fallback: it is the smallest cell anywhere in the
        mesh (7.79 um on that job, from a sliver at a port rim) and would demand a window three
        times tighter than the thing being looked at.
        """
        q = self._manifest.get("quality")
        if not isinstance(q, Mapping):
            return 0.0
        return _finite_positive(q.get("near_wall_cell_m")) or _finite_positive(
            q.get("background_cell_m"))

    def _cell_span_m(self) -> tuple[float, float]:
        """The finest and coarsest cell anywhere in this mesh, for saying what the planned
        figure is NOT. On job 0ff6f9e5 these are 7.787 um and 678.88 um - a factor of 87 - while
        `near_wall_cell_m` is one number the plan chose. (0, 0) when the mesh does not say."""
        q = self._manifest.get("quality")
        if not isinstance(q, Mapping):
            return (0.0, 0.0)
        return (_finite_positive(q.get("min_cell_m")), _finite_positive(q.get("max_cell_m")))

    def resolve_cuts(self) -> dict[str, tuple[ResolvedCut, ...]]:
        """Every prepared frame the manifest declares, confined and measured, by region name.

        WHAT A BAD ENTRY COSTS, AND WHY IT IS DROPPED RATHER THAN RAISED. The declared artifacts
        above raise for required and optional alike, because they are inputs a loader is about to
        be handed. A cut is the opposite: it is FINISHED evidence, and dropping one can only make
        a review decline where it would otherwise have judged - never pass where it would
        otherwise have refused, because the coverage floor is what the frames satisfy. So a frame
        that is absent, not a PNG, or missing its window is logged and left out, and a delivered
        mesh is not thrown away over a picture of it.

        A PATH THAT ESCAPES THE WORKSPACE STILL RAISES, from `confine`. That one is not about
        evidence quality, it is about the box: the manifest is untrusted, and a frame pointing at
        /etc is an attempt to read outside the job, not a frame worth skipping quietly.

        AND THE WORK IS BOUNDED, not just each file. See MAX_FRAMES_MEASURED_PER_SESSION: the
        per-file caps bound one decode and nothing bounded how many a manifest could ask for, and
        a decode is pure Python over every scanline. The budget counts DECODES ATTEMPTED, which is
        the number that was unbounded - frames declared under a scope already taken are measured
        before they are discarded.
        """
        cell_m = self._mesh_cell_m()
        cell_span = self._cell_span_m()
        budget = MAX_FRAMES_MEASURED_PER_SESSION
        out: dict[str, tuple[ResolvedCut, ...]] = {}
        for entry in self._manifest.get("inspection_regions") or []:
            if not isinstance(entry, Mapping):
                continue
            region_id = entry.get("name")
            raw_frames = entry.get("frames")
            if not (isinstance(region_id, str) and region_id.strip()):
                continue
            if not isinstance(raw_frames, (list, tuple)):
                continue
            frames: list[ResolvedCut] = []
            seen: set[str] = set()
            for raw in raw_frames:
                if budget <= 0:
                    # SPENT. Logged at error because a manifest this engine wrote declares six
                    # frames: reaching this means something declared an order of magnitude more,
                    # and the frames beyond it are not offered at all rather than half-measured.
                    logger.error(
                        "review session: the prepared-frame budget for this session (%d frames "
                        "measured) is spent at region %r, so no further frame is measured or "
                        "offered. This engine declares six; a manifest asking for more than %d "
                        "is either wrong or hostile - each frame is a pure-Python decode of every "
                        "scanline (2.687 s for a 22,548-byte 4096x4096 frame, measured).",
                        MAX_FRAMES_MEASURED_PER_SESSION, region_id,
                        MAX_FRAMES_MEASURED_PER_SESSION)
                    break
                budget -= 1
                cut = self._resolve_cut(region_id, raw, cell_m=cell_m, cell_span=cell_span,
                                        plane_axes=_plane_axes(entry.get("plane_axes")),
                                        label=str(entry.get("label") or ""),
                                        why=str(entry.get("why") or ""))
                if cut is None or cut.scope in seen:
                    if cut is not None:
                        # Two frames under one scope would make "the zoom frame" ambiguous, and
                        # the one the reviewer was shown would not be the one the window
                        # described. Keep the first and say so.
                        logger.warning("review session: region %r declares a second %r frame - "
                                       "ignored", region_id, cut.scope)
                    continue
                seen.add(cut.scope)
                frames.append(cut)
            if frames:
                # Whole before zoom: the order the reviewer is offered them in, and the order a
                # part is sensibly read in - where the window sits, then what is inside it.
                frames.sort(key=lambda c: CUT_FRAMES.index(c.scope))
                out[region_id] = tuple(frames)
            if budget <= 0:
                # Out of the REGION loop too, and not only the frame loop: the number of regions
                # is as unbounded as the number of frames in one, and one line saying the budget
                # is spent is worth more than one per region for the rest of the manifest.
                break
        return out

    def _resolve_cut(self, region_id: str, raw: Any, *, cell_m: float,
                     cell_span: tuple[float, float] = (0.0, 0.0),
                     plane_axes: tuple[int, int] | None = None,
                     label: str = "", why: str = "") -> ResolvedCut | None:
        if not isinstance(raw, Mapping):
            return None
        scope = raw.get("scope")
        if scope not in CUT_FRAMES:
            logger.info("review session: a frame of region %r declares scope %r, which is not one "
                        "of %s - not offered", region_id, scope, list(CUT_FRAMES))
            return None
        key = f"inspection_regions.{region_id}.frames.{scope}"
        # THE FRAME'S OWN STATEMENT OF WHAT IT WAS DRAWN AT BEATS THE PLAN'S.
        #
        # `cell_m` arrives from `_mesh_cell_m`, which reads `quality.near_wall_cell_m` - a
        # plan figure, `background_cell_m / 2**levels`, with no knowledge of the layer pass.
        # On a layered interface the stack sits inside that cell and the picture draws what is
        # left of it, so measuring the picture's pitch against the plan cell compares two
        # different lengths. Job b8f09ae9 lost its whole review on that difference: measured
        # pitch 2.0 px against a claimed 4.00, one integer outside the band, while two earlier
        # runs of the same part read 3.0 and passed.
        #
        # THIS LOOSENS NOTHING. CELL_PITCH_TOLERANCE is untouched and the upper side - a frame
        # drawn COARSER than it claims, which is the magnification lie - is still refused. The
        # producer computes this key from its own measured layer figures, and a frame that
        # states nothing keeps the plan cell exactly as before.
        stated_cell_m = _finite_positive(raw.get("drawn_cell_m"))
        if stated_cell_m and stated_cell_m != cell_m:
            logger.info("review session: %s was drawn at %.4f um, not the plan's %.4f um - "
                        "measuring it against what it was drawn at", key,
                        stated_cell_m * 1e6, float(cell_m or 0.0) * 1e6)
            cell_m = stated_cell_m
        path_raw = next((raw[k] for k in _PATH_KEYS
                         if isinstance(raw.get(k), str) and raw[k].strip()), None)
        if path_raw is None:
            # A PLANNED FRAME THAT WAS NEVER DRAWN, which is the ordinary case for a manifest
            # whose windows were computed but whose render did not happen. Nothing is wrong and
            # nothing can be shown.
            logger.info("review session: %s names no file - not offered", key)
            return None
        path = self.confine(key, path_raw)        # raises on an escape; see resolve_cuts
        # MEASURED HERE, ONCE, AND CARRIED. Both the frame's size and what is drawn in it come
        # out of the file, because every number the reviewer is later told is derived from one
        # or the other and the manifest is untrusted.
        measured = _measure_png(path)
        if measured is None:
            logger.info("review session: %s is missing, is not a readable 8-bit PNG, or is "
                        "larger than this process will decode - not offered", key)
            return None
        pixels, ink = measured
        window = _cut_window(raw)
        if window is None:
            logger.warning(
                "review session: %s states no window (window_m: [width, height], in metres), so "
                "nothing can be said about what is in it - not offered", key)
            return None
        content = raw.get("content")
        content = content if isinstance(content, Mapping) else {}
        # ABSENT, TRUE AND FALSE ARE THREE ANSWERS. `is True` collapsed the first two into a
        # denial; `isinstance(..., bool)` keeps absent as None so it can be neutral. A non-boolean
        # is an unreadable claim, which is absent by another route.
        claim = content.get("shows_structure")
        cut = ResolvedCut(
            claimed_structure=claim if isinstance(claim, bool) else None,
            claimed_why=str(content.get("why") or ""),
            artifact_key=key, region_id=region_id, scope=str(scope), path=path, pixels=pixels,
            window_m=window, feature_m=_finite_positive(raw.get("feature_m")), cell_m=cell_m,
            note=str(raw.get("why_frame") or raw.get("note") or ""),
            region_label=label, region_why=why, ink=ink,
            centre_m=_cut_pair(raw.get("centre_m")),
            centre_source=str(raw.get("centre_source") or ""),
            plane_axes=plane_axes, cell_span_m=cell_span)

        # THE PLANNED SCALE AGAINST THE DRAWN ONE. The producer computes its windows against a
        # 1280x960 frame (cfmesh_conjugate/slices.FRAME_W/H) and publishes the metres per pixel
        # that follows. If the render came out at another size, every published figure is wrong by
        # that ratio - so the number the reviewer is told is always the one DERIVED from this
        # file's own pixels, and a disagreement is reported rather than refused: the derived
        # figure is true of the picture either way, and a frame drawn finer than planned is a
        # better frame, not a broken one.
        planned = _finite_positive(raw.get("metres_per_pixel"))
        if planned and abs(planned - cut.m_per_px) > 1e-6 * planned:
            logger.warning(
                "review session: %s was planned at %.4f um/px and is %.4f um/px as drawn "
                "(%dx%d px) - the frame was not rendered at the size its window was computed "
                "for; the drawn figure is the one used",
                key, planned * 1e6, cut.um_per_px, pixels[0], pixels[1])
        if cut.cell_px < COUNTABLE_CELL_PX:
            # THE SCALE IS TOO COARSE, logged with the window that would fix it. The frame is
            # still offered and still shown; it just does not satisfy the region's inspection,
            # and an operator reading this line should not have to work it out.
            want = (f"{cut.pixels[0] * cell_m / COUNTABLE_CELL_PX * 1e3:.2f} mm"
                    if cell_m else "unknown without a cell size")
            logger.warning(
                "review session: %s is %.2f um/px, which is %.2f px across the planned %.4g um "
                "cell - under the %.1f px a cell must be to be counted, so this frame cannot "
                "satisfy region %r. A window of %s across would.",
                key, cut.um_per_px, cut.cell_px, cell_m * 1e6, COUNTABLE_CELL_PX, region_id, want)
        elif not ink.shows_structure and ink.period_unmeasurable and ink.run_spacing_px > 0.0:
            # THE FRAME IS TOO NARROW FOR THE PITCH DRAWN IN IT, which is a different defect from
            # the branch below and has the OPPOSITE repair. `period_unmeasurable` means the search
            # could not test a lag as long as the spacing between this frame's own ink runs, which
            # no periodic profile's period can be - so the array may be right there in the picture
            # and unprovable at this width.
            #
            # MEASURED, job 22ac7054, the real plate: `along_the_flow`'s zoom reported its ink runs
            # 394.0 px apart against the 212 px longest repeat a 1280 px profile can test, and the
            # branch below told the review "whatever the cut landed on, it is not the passages
            # region 'along_the_flow' exists to show". The detector's own words, quoted in that
            # same line, said the structure was coarser than the frame could measure. Those two
            # cannot both be read: one says the cut is in the wrong place, the other says the frame
            # cannot tell.
            #
            # THE FRAME STILL DOES NOT SATISFY THE REGION, and that was never in doubt: six periods
            # is what proves an array and this one cannot show them. What changes is that the repair
            # named is the WINDOW and not the placement, so nobody is sent to move a cut that is
            # already on the right thing - and the review is not handed a conclusion the pixels do
            # not carry.
            from meshpipeline.sandbox.picture_structure import MIN_REPEATS

            want = (f"{ink.run_spacing_px * MIN_REPEATS * cut.m_per_px * 1e3:.2f} mm"
                    if cut.m_per_px > 0.0 else "unknown without a window")
            logger.warning(
                "review session: %s is sized correctly for the cells and TOO NARROW FOR THE PITCH "
                "IN IT: its own ink runs stand %.1f px apart along the %s axis, past the %d px "
                "longest repeat a profile this size can test, so no period could be measured and "
                "the %d a periodic array has to show cannot be counted (%d repeat(s), %.3f of a "
                "perfect array)%s. That is the WINDOW and not the placement: the cut may be on the "
                "passages and unable to prove it here. A window of %s across the %s axis would "
                "hold %d of them. Region %r is not satisfied by this frame.",
                key, ink.run_spacing_px, ink.repeat_axis or "unnamed", ink.max_lag_px,
                MIN_REPEATS, ink.repeats, ink.strength,
                f"; the side that drew it said: {cut.claimed_why}" if cut.claimed_why else "",
                want, ink.repeat_axis or "repeat", MIN_REPEATS, region_id)
        elif not ink.shows_structure:
            # NO PASSAGE ARRAY IN THE PIXELS, measured HERE, AND THE FRAME COULD MEASURE A PERIOD.
            # The branch above takes the case where it could not. Logged apart from the two below
            # because the repair is a different one again: the window is right and the cells may
            # well be fine, the CUT IS IN THE WRONG PLACE. The detector's own words are quoted,
            # and the producer's are quoted beside them when it said anything, so an operator can
            # see whether the two sides agree.
            logger.warning(
                "review session: %s is sized correctly but its own pixels hold no passage array "
                "(%s; %d repeat(s), period %.1f px, %.3f of a perfect array)%s - so whatever the "
                "cut landed on, it is not the passages region %r exists to show, and this frame "
                "cannot satisfy it.",
                key, ink.structure_why or "measured here", ink.repeats, ink.period_px,
                ink.strength,
                f"; the side that drew it said: {cut.claimed_why}" if cut.claimed_why else "",
                region_id)
        elif cut.claimed_structure is False:
            # THE PRODUCER'S VETO, which it reached with the whole picture and the plan in hand.
            # This side measured an array and that side says it is not the right one - so the
            # frame declines, because a claim may subtract. Said separately because it is the one
            # refusal this side cannot reproduce from the picture alone.
            logger.warning(
                "review session: %s holds an array by this side's own measurement (%d repeats, "
                "period %.1f px) but the side that drew it refused the frame%s - a claim from "
                "the producer may veto and may not grant, so region %r is not satisfied by it.",
                key, ink.repeats, ink.period_px,
                f": {cut.claimed_why}" if cut.claimed_why else "", region_id)
        elif cut.cells_resolved and not cut.shows_metal_between_passages:
            # THE CELLS ARE IN THE PICTURE AND THE METAL IS NOT, the refusal with the quietest
            # symptom: every figure the log above would print is healthy. Said with the wall in
            # PIXELS as well as in drawn cells, and with the AXIS each figure was read on, because
            # both of those were wrong at once: a bare run of 0 px read as a four-cell wall, and a
            # vertical period was divided by a horizontal cell. THE NARROWEST WALL AND THE TYPICAL
            # ONE, because the floor is on the first and this line used to say "widest" while
            # printing the second.
            from meshpipeline.sandbox.picture_structure import (
                PLAN_CELLS_ACROSS_A_WALL,
                WALL_CELLS_MIN,
            )
            logger.warning(
                "review session: %s resolves cells (%d on its best row over %d rows at %.1f px) "
                "and there is NOT ENOUGH METAL BETWEEN ITS PASSAGES to be a picture of them: the "
                "structure repeats along the %s axis every %.1f px, which is %.2f drawn cell(s) "
                "of the %.1f px cell drawn on that axis (%.2f in the cell the stated window "
                "implies), and the NARROWEST metal between two passages is %.1f px - %.2f "
                "cell(s), with a typical one at %.1f px - %.2f cell(s) - against the %.1f cells "
                "of wall a picture of this part has to show and the %.1f cells the plan puts "
                "across its thin wall. %s. Region %r is not satisfied by it.",
                key, ink.cells_on_best_row, ink.cell_rows, ink.cell_pitch_px,
                ink.repeat_axis or "unnamed", ink.period_px, cut.period_cells,
                cut.repeat_pitch_px, cut.period_cells_claimed,
                ink.bare_run_min_px, cut.wall_cells,
                ink.bare_run_px, cut.wall_cells_median,
                WALL_CELLS_MIN, PLAN_CELLS_ACROSS_A_WALL,
                ("No column of this frame along that axis is bare, so there is no metal in it at "
                 "all - this is a field of coolant, whatever its section does"
                 if ink.bare_run_min_px <= 0.0
                 else "No cell was resolved along the axis the structure repeats on, so the metal "
                      "between these passages could not be counted in cells at all"
                 if cut.repeat_pitch_px <= 0.0
                 else "The metal is there and it is thinner than a picture of this part's wall - "
                      "a plenum with ribs in it, not a bank of passages"),
                region_id)
        elif not cut.shows_cells:
            # THE SCALE IS RIGHT AND THE PICTURE IS STILL NOT OF CELLS, which is the failure the
            # scale alone cannot see and the only one that has ever actually happened here. Said
            # separately from the lines above because the repair is a different one: the window is
            # fine and the plane or the aim is not.
            #
            # AND THE SENTENCE IS BUILT FROM THE TESTS THAT ACTUALLY FAILED, which is round 9's
            # correction and is LOGGING ONLY - no condition below decides anything, and the set
            # of frames that reach this branch is exactly what it was. It used to print all four
            # bars unconditionally, so an operator was told three comparisons had failed that had
            # not. MEASURED, REAL PLATE, SURFACE, job 0ff6f9e5, along_the_flow zoom: "only 0
            # cell(s) are resolved on its best row, under the 4 a passage needs, on 0 row(s)
            # against the 12 an array needs, at a drawn spacing of 0.0 px against the 4.00 px a
            # cell should be, and 3.2585% of the frame has anything drawn in it at all against
            # the 0.1000% a cut needs" - and 3.2585% is THIRTY-TWO TIMES the floor it is printed
            # as failing. The same line on a frame of 60 interiors over 840 rows at 50% ink
            # reports 60 as under 4, 840 as under 12 and 50% as under 0.1%.
            #
            # THE DRAWN SPACING IS THE ONE ON THE REPEAT AXIS, with the axis named, which is the
            # second correction here: the figure `cells_resolved` compares is `repeat_pitch_px`
            # and this line quoted `ink.cell_pitch_px`, the ROW reading, whatever axis the frame
            # repeats on. On a vertically repeating frame those are two different measurements
            # and the one that decided the refusal was not the one printed.
            bars: list[str] = []
            if ink.ink_fraction < EVIDENCE_INK_FLOOR:
                bars.append(f"only {ink.ink_fraction * 100.0:.4f}% of the frame has anything "
                            f"drawn in it at all, under the {EVIDENCE_INK_FLOOR * 100.0:.4f}% a "
                            "cut of a region needs")
            if ink.cells_on_best_row < COUNTABLE_CELLS_ON_A_ROW:
                bars.append(f"only {ink.cells_on_best_row} cell(s) are resolved on its best row, "
                            f"under the {COUNTABLE_CELLS_ON_A_ROW} a passage needs")
            if ink.cell_rows < COUNTABLE_CELL_ROWS:
                bars.append(f"the cells were resolved on only {ink.cell_rows} scan row(s), under "
                            f"the {COUNTABLE_CELL_ROWS} an array {COUNTABLE_CELLS_ON_A_ROW} "
                            "cells deep draws")
            axis = ink.repeat_axis or "unnamed"
            if cut.cell_px <= 0.0:
                bars.append("this frame states no cell size at all, so there is nothing for a "
                            "drawn cell to be checked against")
            elif cut.repeat_pitch_px <= 0.0:
                bars.append(f"no cell interior was resolved along the {axis} axis the structure "
                            "repeats on, which is the axis its wall and its period are counted "
                            "on, so there is no drawn cell there to check")
            elif not (cut.cell_px / CELL_PITCH_TOLERANCE <= cut.repeat_pitch_px
                      <= cut.cell_px * CELL_PITCH_TOLERANCE):
                bars.append(f"what was resolved along the {axis} axis the structure repeats on "
                            f"is drawn {cut.repeat_pitch_px:.1f} px apart against the "
                            f"{cut.cell_px:.2f} px the stated window makes a planned cell, "
                            f"outside the factor of {CELL_PITCH_TOLERANCE:g} the two readings of "
                            "one frame's own scale have to agree within")
            logger.warning(
                "review session: %s is sized correctly (%.2f px across the planned %.4g um "
                "cell) and a cell is still not countable in it: %s. The window is not the "
                "problem: what the cut exists to show is not inside it. Region %r is not "
                "satisfied by this frame.",
                key, cut.cell_px, cell_m * 1e6,
                "; ".join(bars) or "no test this side takes names a reason, which is a hole in "
                                   "this log and not a judgement about the frame",
                region_id)
        return cut


class ReviewSession:

    def __init__(self, inner: Any, root: Path, limits: _Limits) -> None:
        self._inner = inner
        self._root = root
        self._limits = limits
        self._closed = False
        self._images = 0
        self._opening: tuple[EvidenceItem, ...] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                "the review session is closed")

    def capabilities(self):
        self._require_open()
        return self._inner.capabilities()

    def scene_context(self):
        self._require_open()
        return self._inner.scene_context()

    def prepared_cuts(self) -> tuple[ResolvedCut, ...]:
        """The frames this session will serve, so the reviewer can be told what they are.

        `getattr` with a default rather than a direct call: `ReviewRenderSession` is a Protocol
        and a session that predates prepared cuts is still a valid one. An engine with no cuts
        answers with an empty tuple, which is what every engine but the conjugate one does today.
        """
        self._require_open()
        return tuple(getattr(self._inner, "prepared_cuts", lambda: ())())

    def opening_evidence(self) -> tuple[EvidenceItem, ...]:
        self._require_open()
        if self._opening is None:
            self._opening = tuple(self._validate(i) for i in self._inner.opening_evidence())
        return self._opening

    def execute(self, command: RenderCommand) -> EvidenceItem | SessionUpdateResult:
        self._require_open()
        caps = self._inner.capabilities()
        if not caps.supports(command.kind):
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"this engine's renderer does not support {command.kind.value}")

        result = self._inner.execute(command)

        if command.kind in NON_RENDERING_COMMANDS:
            if not isinstance(result, SessionUpdateResult):
                raise ReviewRenderError(
                    ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                    f"{command.kind.value} returned evidence - a session update renders "
                    "nothing and must never claim inspection")
            return result

        if isinstance(result, SessionUpdateResult):
            raise ReviewRenderError(
                ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
                f"{command.kind.value} is a rendering command but returned a session update - "
                "a failed render must be reported as failed evidence, not hidden")
        return self._validate(result)

    def _validate(self, item: EvidenceItem) -> EvidenceItem:
        if not item.image_ref:
            return item                                   # already an honest failure
        path = Path(item.image_ref)
        real = (path if path.is_absolute() else self._root / path).resolve(strict=False)
        try:
            real.relative_to(self._root)
        except ValueError:
            return self._demote(item, "image escaped the sandbox output area")
        if not real.is_file():
            return self._demote(item, "image file does not exist")
        size = real.stat().st_size
        if size == 0 or size > self._limits.max_image_bytes:
            return self._demote(item, f"image size {size}B is outside the permitted range")
        # THE PIXELS, NOT THE BYTES. See EMPTY_FRAME_PIXEL_SHARE: the band above is the only
        # emptiness check this path had, and a blank full-resolution frame sails through it.
        blank = frame_shows_nothing(real)
        if blank is not None:
            return self._demote(item, blank)
        self._images += 1
        if self._images > self._limits.max_images:
            # Refuse THIS view, never the whole review: raising here threw away every finding the
            # reviewer was about to submit. The refusal is a tool result the model reads.
            return self._demote(item, (
                f"the view budget for this review ({self._limits.max_images} pictures) is spent, "
                "so no more views can be taken - submit your findings from the views you "
                "already have"))
        return item

    def _demote(self, item: EvidenceItem, why: str) -> EvidenceItem:
        # `covers_target` deliberately SURVIVES here while `image_ref` does not: the frame stays
        # in the record as an attempt, and what stops it being counted is `rendered`, which
        # EvidenceLedger.coverage() and unified.record_operation_evidence both check first.
        # (Pinned by tests/unit/security: "the frame survives, its intent survives, it proves
        # nothing".) A prepared cut goes through this path like any other frame.
        logger.warning("review session: rejected evidence %s - %s", item.evidence_id, why)
        return EvidenceItem(**{**item.__dict__, "image_ref": "",
                               "diagnostics": (*item.diagnostics, f"rejected: {why}")})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._inner.close()
        except Exception:  # noqa: BLE001
            logger.warning("review session: renderer close failed", exc_info=True)


@dataclass(frozen=True)
class _Limits:
    max_images: int
    max_image_bytes: int


async def _inline_dispatch(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@asynccontextmanager
async def open_review_session(spec, context: RenderContext, native_dispatch=None):
    renderer = spec.review_renderer
    if renderer is None:
        raise ReviewRenderError(
            ReviewEvidenceFailure.RENDERER_UNAVAILABLE,
            "this engine declares no review renderer")

    root = Path(context.workspace).resolve(strict=False)
    resolver = ArtifactResolver(root, context.manifest)

    # Resolve EVERYTHING before the renderer runs: a missing required artifact must fail before
    # a render process exists, not halfway through one.
    resolved = ResolvedEvidence()
    for req in spec.render_artifacts:
        art = resolver.resolve(req)
        if art is not None:
            resolved[req.artifact_key] = art
        elif not req.required:
            logger.info("review session: optional artifact %s absent - views needing it will "
                        "not be offered", req.artifact_key)

    # THE PREPARED CUTS ARE RESOLVED HERE, beside the declared artifacts, and for the same reason
    # this module states in its own header: it receives resolved artifacts and resolves none
    # itself, which is what keeps the confinement fence meaningful. Resolving them in
    # `build_backend` instead would have put path handling on the far side of that fence, where
    # there is no resolver and no workspace root to compare against. They cannot go through
    # `resolve`/`_lookup`: that walks dotted keys and returns a value only for a string node, so
    # a region's list of cut mappings resolves to None.
    #
    # AND IT GOES THROUGH THE DISPATCH LANE, which is the one thing about this line that is not
    # about paths. Everything above it is pure pathlib and stays on the event loop; this is not.
    # `resolve_cuts` un-filters every scanline of every declared frame in pure Python -
    # 0.33 s for one 1280x960 frame and 2.687 s for a 4096x4096 one, both measured - so running
    # it inline BLOCKS the loop for as long as the manifest asks for. MAX_FRAMES_MEASURED_PER_
    # SESSION bounds how long that can be; the lane keeps it off the loop entirely. A caller with
    # no lane (`_inline_dispatch`, which every unit test uses) is exactly where it was.
    dispatch = native_dispatch or _inline_dispatch
    resolved.cuts = await dispatch(resolver.resolve_cuts)
    if resolved.cuts:
        logger.info("review session: %d region(s) carry prepared cuts (%d frame(s))",
                    len(resolved.cuts), sum(len(v) for v in resolved.cuts.values()))

    limits = _Limits(
        max_images=min(context.max_images or MAX_IMAGES_PER_SESSION, MAX_IMAGES_PER_SESSION),
        max_image_bytes=MAX_IMAGE_BYTES,
    )

    # NATIVE WORK STARTS HERE. From this line on we are touching gmsh and VTK, which are
    # thread-affine - so construction goes through the same lane every later call will use.
    # The resolved set is HANDED OVER. Resolving it and then leaving the renderer to find its
    # own paths would have made the confinement above decorative.
    inner = await dispatch(renderer.open, context, resolved)
    session = ReviewSession(inner, root, limits)
    try:
        yield session
    finally:
        # EVERY exit: success, renderer failure, caller exception, timeout, cancellation.
        # Cancellation propagates AFTER cleanup - never swallowed, and never turned into a
        # quality verdict.
        # Close goes through the SAME lane, so it queues behind any in-flight native call
        # instead of tearing down VTK from a second thread mid-render. Shielded because the
        # caller's cancellation must not interrupt native cleanup halfway.
        _final = getattr(native_dispatch, "__self__", None)
        _close = _final.run_final if _final is not None else dispatch
        await asyncio.shield(asyncio.ensure_future(_close(session.close)))
