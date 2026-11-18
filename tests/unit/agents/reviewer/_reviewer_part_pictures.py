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
#
# The duplicate of this file under tests/unit/sandbox is deliberate, for the reason
# `_reviewer_passage_pictures.py` already gives: tests/unit/_render_fakes.py is shared with work
# in flight and is not this change's to edit.
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


def write_alternating_walls(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                            passage_px: int = 16, wall_a_px: int = 16, wall_b_px: int = 1):
    """PASSAGES WITH WALLS OF TWO WIDTHS, which is what fins that come in PAIRS draw as.

    This part's own z-normal cut holds exactly this shape (see : "the
    fins come in pairs"), so it is its shape and not a contrived one. It is drawn here for the
    one sentence that only a bimodal frame reaches: the clause that says the walls in this frame
    are NOT all one width. With  8 against a window that makes a planned cell 4.00 px,
    that clause has to be given in PIXELS, because the frame resolved no cell of the declared
    size to count either wall in.
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
