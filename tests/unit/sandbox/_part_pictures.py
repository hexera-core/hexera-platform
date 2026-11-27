# Responsibility: Draw the four shapes an independent review MEASURED discharging a region with
# no passage in them, plus the honest frame they have to be told apart from.
# Boundaries: pixels only. Nothing here asserts; nothing here imports the product.
#
# WHY A SECOND PICTURE FILE BESIDE `_passage_pictures.py`. That one draws bands of cells with
# BARE metal between them, which is the honest shape and the only one round 4 needed. The shapes
# below are the ones that slipped: a frame can be a solid field of fluid cells with a one-pixel
# rib in it and measure the same period, the same repeats, the same strength, the same drawn cell
# pitch and the same row count as the honest frame. The ink is the only figure that differs, and
# nothing was reading it.
#
# MEASURED through the production ArtifactResolver, 2026-11-17, all at 1280x960 with 4 px cells:
#
#   shape                                      ink      repeats  period  strength  interiors rows
#   plenum, bare 2 px wall every 32 px     93.7500%        39     32.0     0.975      240     720
#   plenum, bare 4 px wall every 32 px     87.5000%        39     32.0     0.975      240     720
#   plenum, bare 2 px wall every 64 px     96.8750%        19     64.0     0.950      280     720
#   12 bands 16 px wide, 32 px pitch,
#       64 px tall, in a blank frame        1.0000%        11     32.0     0.898       36      48
#   a wireframe lattice at 8 px            23.4375%       159      8.0     0.994        0       0
#   HONEST: 16 px of passage every 32 px   50.0000%        39     32.0     0.975      120     720
#
# The first three and the honest one differ in NOTHING a cell count is taken from except the ink.
from __future__ import annotations

FILL = (176, 196, 222)      # lightsteelblue, the colour the producer draws a cut in
EDGE = (0, 0, 0)
BACKGROUND = (255, 255, 255)


def _grid(d, box, cells_px: int, cells_down_px: int = 0) -> None:
    """A light fill with dark cell boundaries ruled across it, which is what show_edges over a
    coloured mesh produces and what the reader counts enclosed interiors in.

    `cells_down_px` is the cell's height when it differs from its width, which is ordinary on
    this part: the planner pins the fine cell AT the thin wall's thickness and lets it run long
    in the other two directions. 0 means square.
    """
    down = int(cells_down_px) or int(cells_px)
    d.rectangle(box, fill=FILL)
    if cells_px > 0:
        for x in range(box[0], box[2] + 1, cells_px):
            d.line([x, box[1], x, box[3]], fill=EDGE, width=1)
    if down > 0:
        for y in range(box[1], box[3] + 1, down):
            d.line([box[0], y, box[2], y], fill=EDGE, width=1)


def write_truncated_plenum(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                           band_px: int = 16, period_px: int = 32, deep_px: int = 470):
    """A FIELD OF COOLANT WITH NO METAL IN IT AT ALL, whose SECTION VARIES: every other 16 px
    band of a full-frame grid of cells stops `deep_px` down instead of running the whole height.

    THE SHAPE THE DENSITY FORM OF THE WALL CANNOT SEE. Measured through the production
    ArtifactResolver at 1280x960 with 4 px cells, at deep_px of 200, 400 and 470: 39 repeats,
    period 32.0 px, 0.975 of a perfect array, extent 0.9758, 319 enclosed interiors on the best
    row over 720 rows - and a duty of 0.5004 giving a wall of 3.9968 cells, which is BETTER than
    the honest frame's. Every column of it carries ink, the widest all-background column run is
    0 px, and at deep_px=470 all 915,200 inked pixels are one 4-connected body.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    for x0 in range(0, w, int(period_px)):
        _grid(d, [x0, 0, min(x0 + int(band_px) - 1, w - 1), h - 1], int(cells_px))
        x1 = x0 + int(band_px)
        if x1 < w:
            _grid(d, [x1, 0, min(x1 + int(band_px) - 1, w - 1), int(deep_px) - 1], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_horizontal_passages(path, width: int = 1280, height: int = 960, *, cell_w: int = 4,
                              cell_h: int = 64, band_px: int = 16, period_px: int = 32):
    """PASSAGES RUNNING ACROSS THE FRAME: the repeat is VERTICAL at `period_px`, and the drawn
    cell is `cell_w` wide by `cell_h` tall.

    THE SHAPE A MEASUREMENT TAKEN ON TWO DIFFERENT AXES CANNOT SEE. Measured through the
    production ArtifactResolver at 1280x960 with a claimed 4.00 px cell: 29 repeats, period
    32.0 px vertical, 0.967, extent 0.9677, 319 interiors on the best row over 450 rows - and
    8.00 drawn cells per period with a 3.996-cell wall, off the HORIZONTAL cell pitch. In the
    direction the structure repeats there is half a cell per period at cell_h=64. Cell heights of
    16, 32 and 64 px give BYTE-FOR-BYTE IDENTICAL frames at band_px=16, because a 16 px band has
    no room for a second horizontal cell line - so the whole of the evidence for the cell's
    height is that it was not drawn, and a reading that comes out the same for all three is a
    reading that never looked.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    for y0 in range(0, h, int(period_px)):
        _grid(d, [0, y0, w - 1, min(y0 + int(band_px) - 1, h - 1)], int(cell_w), int(cell_h))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_cell_passages(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                        passage_cells: int = 4, wall_cells: int = 4):
    """THE HONEST SHAPE, STATED IN CELLS: `passage_cells` of coolant, then `wall_cells` of bare
    metal, repeated across the frame.

    `write_passages` takes a period and a duty, which is the producer's form; this takes the two
    quantities a reader checks against a plan, because the window that matters is stated in them.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    run = int(passage_cells) * int(cells_px)
    period = run + int(wall_cells) * int(cells_px)
    for bx in range(0, w, period):
        _grid(d, [bx, 0, min(bx + run - 1, w - 1), h - 1], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_half_array_half_plenum(path, width: int = 1280, height: int = 960, *,
                                 cells_px: int = 4, period_px: int = 32):
    """HALF AN HONEST ARRAY AND HALF A FEATURELESS FIELD OF COOLANT.

    Measured, 1280x960, 4 px cells: 75.0000% ink, 20 repeats, period 32.0 px, extent 0.5008 and
    a 4-cell wall, and it DISCHARGES in both arms. The repeat span stops at the plenum's first
    run, so every figure is measured on the array half and the plenum half is invisible.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    half = w // 2
    run = int(period_px) // 2
    for bx in range(0, half, int(period_px)):
        _grid(d, [bx, 0, min(bx + run - 1, half - 1), h - 1], int(cells_px))
    _grid(d, [half, 0, w - 1, h - 1], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_plenum_with_ribs(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                           wall_px: int = 2, period_px: int = 32):
    """A PLENUM WITH SUB-STRUCTURE: a solid field of fluid cells, crossed by a bare wall.

    The wall is BARE, not drawn, because in a fluid-only cut the metal is simply absent - which
    is what makes the ink fraction exactly `1 - wall_px/period_px` and the period exactly
    `period_px`. One rib, tie bar, support wall or refinement band turns a featureless plenum
    into something with a period, and `MIN_REPEATS` cannot see it.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    _grid(d, [0, 0, w - 1, h - 1], cells_px)
    for x in range(0, w, int(period_px)):
        d.rectangle([x, 0, x + int(wall_px) - 1, h - 1], fill=BACKGROUND)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_speck_of_array(path, width: int = 1280, height: int = 960, *, bands: int = 12,
                         band_px: int = 16, period_px: int = 32, tall_px: int = 64,
                         cells_px: int = 4):
    """A SPECK OF A REAL ARRAY in a frame that is otherwise blank: honest passages, far too few.

    At the producer's 6.28945 um/px the default here is 2.42 x 0.40 mm of a 170 mm plate, and it
    clears `review_session.EVIDENCE_INK_FLOOR` ten times over.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    pw = int(bands) * int(period_px)
    x0, y0 = (w - pw) // 2, (h - int(tall_px)) // 2
    for k in range(int(bands)):
        bx = x0 + k * int(period_px)
        _grid(d, [bx, y0, bx + int(band_px) - 1, y0 + int(tall_px) - 1], cells_px)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_wireframe_lattice(path, width: int = 1280, height: int = 960, *, cells_px: int = 8):
    """A LATTICE AND NOTHING ELSE: cell boundaries with no fill, so there is no enclosed interior
    anywhere in it and the only period in the picture is the lattice's own."""
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    for x in range(0, w, int(cells_px)):
        d.line([x, 0, x, h - 1], fill=EDGE, width=1)
    for y in range(0, h, int(cells_px)):
        d.line([0, y, w - 1, y], fill=EDGE, width=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_passages(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                   period_px: int = 32, duty: float = 0.5, patch_px=None):
    """THE HONEST SHAPE: passages of cells with BARE metal between, half of each period each.

    `duty` is the drawn share of one period - 0.5 is this plate, whose 100.631 um channels sit on
    a 201.262 um pitch. `patch_px` draws into a patch that size in the middle of the frame.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    pw, ph = (w, h) if patch_px is None else (int(patch_px[0]), int(patch_px[1]))
    x0, y0 = (w - pw) // 2, (h - ph) // 2
    run = max(1, int(round(int(period_px) * float(duty))))
    for bx in range(x0, x0 + pw, int(period_px)):
        _grid(d, [bx, y0, min(bx + run - 1, x0 + pw - 1), y0 + ph - 1], cells_px)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_anti_aliased_passages(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                                period_px: int = 32, duty: float = 0.5, offset_px: float = 0.375,
                                edge_px: float = 1.0, lit: int = 90, ss: int = 8):
    """THE HONEST SHAPE AS A REAL RENDERER WOULD LEAVE IT: supersampled, box-filtered, shaded,
    and offset by a fraction of a pixel so no edge lands on a pixel boundary.

    Every countable frame measured in rounds 1 to 4 was a hand-drawn lattice with exactly two
    grey levels and pixel-aligned edges. A box filter over a supersampled drawing IS what an
    anti-aliasing rasteriser applies, so this is the shape nothing had been tried on.
    """
    import numpy as np
    from PIL import Image

    w, h, s = int(width), int(height), int(ss)
    bw, bh = w * s, h * s
    x = np.arange(bw) / s - float(offset_px)
    y = np.arange(bh) / s
    inpass = (x % float(period_px)) < (float(period_px) * float(duty))
    a = np.where(inpass, float(FILL[0]), 255.0)[None, :] * np.ones((bh, 1))
    if lit:
        a = np.where(inpass[None, :],
                     np.clip(a - np.linspace(0, int(lit), bh)[:, None], 0, 255), a)
    a[:, ((x % float(cells_px)) < float(edge_px)) & inpass] = float(EDGE[0])
    a[np.ix_((y % float(cells_px)) < float(edge_px), np.flatnonzero(inpass))] = float(EDGE[0])
    small = a.reshape(h, s, w, s).mean(axis=(1, 3))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(np.rint(small), 0, 255).astype(np.uint8),
                    mode="L").convert("RGB").save(path, format="PNG")
    return path


def write_anisotropic_passages(path, width: int = 1280, height: int = 960, *, cell_w: int = 8,
                               cell_h: int = 4, passage_cells: int = 4, wall_cells: int = 4):
    """THE HONEST SHAPE WITH THE PLANNER'S OWN ANISOTROPY: passages of cells `cell_w` across by
    `cell_h` down, with bare metal between them.

    The planner pins the fine cell AT the thin wall's thickness and lets it run long in the other
    two directions, so a cell that is 8 px one way and 4 px the other in a window sized for a
    4 px cell is this part's ordinary shape. Drawn here because it is the frame that separates
    from the MAGNIFICATION LIE - `write_passages(cells_px=8, period_px=64)`, a cell 8 px SQUARE
    declared at a window that makes a cell 4 px. MEASURED, SYNTHETIC, VOLUME, 1280x960: both read
    8.0 px along their rows; this one reads 4.0 px down its columns and the lie reads 8.0.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    run = int(passage_cells) * int(cell_w)
    period = run + int(wall_cells) * int(cell_w)
    for bx in range(0, w, period):
        _grid(d, [bx, 0, min(bx + run - 1, w - 1), h - 1], int(cell_w), int(cell_h))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_row_coarse_col_fine(path, width: int = 1280, height: int = 960, *, cell_w: int = 2,
                              cell_h: int = 4, passage_cells: int = 8, wall_cells: int = 8):
    """A FRAME WHOSE CELL ON THE AXIS ITS STRUCTURE REPEATS ON DISAGREES WITH THE CLAIM AND WHOSE
    CELL ON THE OTHER AXIS AGREES: cells `cell_w` px across by `cell_h` px down, with honest bare
    metal between the passages, repeating HORIZONTALLY.

    THE FRAME THE ANY-AXIS FORM OF `cells_resolved` ADMITTED. At cell_w=2 against a claimed
    4.00 px cell the ROW reading - the axis the wall and the period are counted on - is a factor
    of two out, so the picture is drawn at half the magnification it declares, and the COLUMN
    reading is exactly 4.0. MEASURED, SYNTHETIC, VOLUME, 1280x960, through the production
    ArtifactResolver: passage 8c/wall 2c, 4c/2c and 12c/2c all discharged their region, and the
    8c/8c frame's 16.0 px of metal was published as "8.00 cell(s)" with "16.00 drawn cell(s) per
    period" - both exactly twice what the declared scale makes them.

    It is `write_anisotropic_passages` with the two cell sizes the other way round - and that is
    NOT, as this said, what tells the planner's shape from a scale lie. RE-MEASURED 2026-11-18,
    SYNTHETIC, VOLUME, 1280x960, a claimed 4.00 px cell, through the production ArtifactResolver:
    BOTH of them are refused. `write_anisotropic_passages(cell_w=8, cell_h=4)` reads 8.0 px on
    the axis it repeats on and is refused at passage 4c/wall 4c, 8c/2c and 12c/4c, exactly as
    this one is at 2.0 px. Swapping the two dimensions does not separate them, because neither
    reading agrees with the window.

    WHAT DOES SEPARATE FRAMES IS ORIENTATION RELATIVE TO THE REPEAT, not the cell's shape.
    MEASURED on the same set, one shape - 4 px ACROSS by 8 px DOWN - drawn both ways: repeating
    HORIZONTALLY it reads 4.0 px on the repeat axis and is ADMITTED; repeating VERTICALLY, as
    `write_horizontal_passages(cell_w=4, cell_h=8)` draws it, it reads 8.0 px on that axis and is
    REFUSED at every pitch from 32 to 128 px. So this picture's own defect is that the cell on
    the axis its wall and its period are counted in is HALF the one it declares, and the column
    agreeing is beside the point.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    run = int(passage_cells) * int(cell_w)
    period = run + int(wall_cells) * int(cell_w)
    for bx in range(0, w, period):
        _grid(d, [bx, 0, min(bx + run - 1, w - 1), h - 1], int(cell_w), int(cell_h))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_alternating_walls(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                            passage_px: int = 16, wall_a_px: int = 16, wall_b_px: int = 1):
    """PASSAGES WITH WALLS OF TWO WIDTHS, which is what fins that come in PAIRS draw as.

    This part's own z-normal cut holds exactly this shape (see `picture_structure.ACF_MIN`: "the
    fins come in pairs"). MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell, walls of 16 px
    and 1 px: 52 repeats every 49.0 px at 1.000 of a perfect array, a MEDIAN bare run of 8.5 px -
    2.125 drawn cells, over a floor of 2.00 - and a NARROWEST bare run of 1.0 px, a quarter of a
    cell. The median is a width neither wall in the picture has.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    x, wide = 0, True
    while x < w:
        _grid(d, [x, 0, min(x + int(passage_px) - 1, w - 1), h - 1], int(cells_px))
        x += int(passage_px) + (int(wall_a_px) if wide else int(wall_b_px))
        wide = not wide
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_plenum_either_side(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                             period_px: int = 32, array_share: float = 0.5):
    """AN HONEST ARRAY OVER `array_share` OF THE WIDTH, with FEATURELESS COOLANT either side of it.

    THE SHAPE THAT INFLATED THE FIGURE MEANT TO DISCLOSE IT. `repeat_span` ran from the first
    rising edge of the material mask to the LAST, and a non-periodic field supplies both ends.
    MEASURED, SYNTHETIC, VOLUME, 1280x960 at 4 px per cell:

        the array's share of the width     0.25      0.35      0.50
        repeat_span, first edge to last    0.6258    0.6758    0.7508
        repeat_span, the periodic chain    0.2258    0.3258    0.4758

    The old figure went UP as the real array SHRANK, and the sentence a reviewer reads said "over
    62.58% of the frame" about a frame three quarters of which is a plenum.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    aw = int(w * float(array_share))
    x0 = (w - aw) // 2
    if x0 > 0:
        _grid(d, [0, 0, x0 - 1, h - 1], int(cells_px))
        _grid(d, [x0 + aw, 0, w - 1, h - 1], int(cells_px))
    run = int(period_px) // 2
    for bx in range(x0, x0 + aw, int(period_px)):
        _grid(d, [bx, 0, min(bx + run - 1, x0 + aw - 1), h - 1], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_passages_with_a_transverse_join(path, width: int = 1280, height: int = 960, *,
                                          cells_px: int = 4, band_px: int = 16,
                                          period_px: int = 32, join_px: int = 33,
                                          join_at: str = "middle"):
    """THE REAL PLATE'S OWN SHAPE: honest passages crossed by a full-width TRANSVERSE MANIFOLD.

    This is `write_passages` with one band of coolant ruled all the way across it, which is what a
    cold plate whose channels are fed by a cross manifold draws as - and what job
    6dd6703c-c8d2-43cd-882c-a01c7375d014's through_the_plate zoom IS.

    MEASURED, 2026-11-19, REAL PLATE, VOLUME, on that job's own PNG, the first cut of a real mesh
    INTERIOR ever drawn: 1280x960, 51.7188% ink, 640 metal columns and 640 coolant columns, metal
    in 41 runs of 16.0 px (median 16.0, max 16.0; the two clipped at the frame edges read 8.0),
    coolant in 40 runs of 16.0 px, and EVERY metal column carrying exactly 33 inked pixels - all
    33 of them in ONE contiguous band at rows 462..494 where all 640 metal columns are inked at
    once. The defaults here draw that: a 33-row join across the middle of a 16/32 array.

    AND THE REAL FRAME IS NOT ANTI-ALIASED, which this does not have to reproduce because it is
    not what the join costs. Measured on the same PNG: 179 distinct grey levels and NOT ONE PIXEL
    between 193 and 254, so the counts under 193, 220, 240 and 254 are all exactly 635,520. The
    33 pixels in a metal column are dark, not faint.

    `join_at` puts the band in the MIDDLE, as the real plate has it, or against the TOP edge,
    which is where `write_truncated_plenum` puts its solid coolant and is the shape that must stay
    refused. The two differ in nothing but where the band sits.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    for bx in range(0, w, int(period_px)):
        _grid(d, [bx, 0, min(bx + int(band_px) - 1, w - 1), h - 1], int(cells_px))
    j = int(join_px)
    if j > 0:
        y0 = 0 if str(join_at) == "top" else (h - j) // 2
        _grid(d, [0, y0, w - 1, min(y0 + j - 1, h - 1)], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path


def write_passages_in_groups_of_three(path, width: int = 1280, height: int = 960, *,
                                      cells_px: int = 4, period_px: int = 62, duty: float = 0.5,
                                      short_share: float = 0.55):
    """PASSAGES AT ONE PITCH WITH A GROUP-OF-THREE SUPER-STRUCTURE OVER THEM, which is what a
    diagonal lattice draws as once the frame is magnified enough to resolve it.

    Every passage stands `period_px` from the next - so the ink's own runs are PERFECTLY REGULAR
    and their median IS the pitch - but every third one reaches only `short_share` of the frame's
    height, so the ink profile summed down the columns repeats every THREE pitches rather than
    every one. Autocorrelation then peaks at 3p outright, not in a tie with p.

    THIS IS THE SHAPE THAT COST JOB 606af55e ITS REVIEW. That run's through_the_plate zoom measured
    21 run spacings of 62.0 px with a spread of 0.016 and a correlation peak at lag 185 - 2.98
    times the spacing - and published 603.92 um as the period of a part whose passages were
    measured at 100.63 um, so a clean picture of 21 passages was refused for showing "a different
    feature of the same part".
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    run = max(1, int(round(int(period_px) * float(duty))))
    short = max(1, int(round(h * float(short_share))))
    for k, bx in enumerate(range(0, w, int(period_px))):
        tall = h if k % 3 else short
        _grid(d, [bx, 0, min(bx + run - 1, w - 1), tall - 1], int(cells_px))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path
