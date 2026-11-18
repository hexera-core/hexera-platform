# Responsibility: Draw the PNG shapes a prepared cut really comes home as, so a test about one can
# tell a picture of passages from a picture of solid metal.
# Boundaries: pixels only. Nothing here asserts; nothing here imports the product.
#
# WHY IT IS NOT IN tests/unit/_render_fakes.py. That module's `write_cut_png` draws ONE rectangle
# filled with a grid of cells, which is a picture with NO PASSAGE IN IT. Measured through the
# production reader, 2026-11-17: a 4 px grid filling a 1280x960 frame is 100.0000% ink, 319
# enclosed interiors on its best row at a 4.0 px pitch, 720 rows carrying a countable run - and
# ZERO repeats, because every column of it is inked. That is the plenum shape, and it is precisely
# the frame that has to be refused. A fixture that draws it cannot stand for a good frame, and a
# test using it for one cannot tell the two cases apart.
#
# A REAL CUT ACROSS A COOLED PLATE ALTERNATES PASSAGE AND METAL, so that is what this draws.
# Measured through the same reader on the shape below (passage 16 px, period 32 px, 4 px cells):
# 53.1250% ink, 160 interiors, 720 rows, 4.0 px pitch, 39 repeats every 32.0 px at 0.975 of a
# perfect array - and the real plate's own along_the_flow zoom, which is a cut of the fluid/solid
# INTERFACE and not of a volume, measures 79 repeats every 16.0 px at 0.987.
#
# IT IS A DUPLICATE of tests/unit/sandbox/_passage_pictures.py, under a different module name
# because tests/unit has no packages and both directories land on sys.path. _render_fakes.py,
# where one copy would belong, is shared with work in flight and is not this change's to edit.
from __future__ import annotations


def write_cut_picture(path, width: int = 1280, height: int = 960, *, cells_px: int = 4,
                      period_px: int = 32, patch_px=None):
    """A REAL PNG of a FLUID-ONLY CUT ACROSS THE PASSAGES: bands of cells with bare metal between.

    `cells_px` is how many pixels across each drawn cell is - 4 is the floor a cell is countable
    at, 1 cannot be counted, 32 is a picture of something eight times bigger than the cell the
    window claims. `period_px` is the drawn passage pitch, with the passage half of it;
    `period_px=0` draws one solid block of cells instead, which is the plenum shape. `patch_px`
    is (w, h) to draw into a patch that size in the middle of the frame rather than over all of
    it, for the frames that are nearly blank or only a few cells deep.
    """
    from PIL import Image, ImageDraw

    w, h = int(width), int(height)
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    pw, ph = (w, h) if patch_px is None else (int(patch_px[0]), int(patch_px[1]))
    x0, y0 = (w - pw) // 2, (h - ph) // 2

    def block(bx0: int, bx1: int) -> None:
        box = [bx0, y0, bx1, y0 + ph - 1]
        # A light fill with dark edges ruled across it, which is what show_edges=True over a
        # coloured mesh produces: the reader counts the lighter interiors that have a darker pixel
        # on BOTH sides, so the fill and the lines both have to be there.
        d.rectangle(box, fill=(176, 196, 222))
        if cells_px > 0:
            for x in range(box[0], box[2] + 1, cells_px):
                d.line([x, box[1], x, box[3]], fill=(0, 0, 0), width=1)
            for y in range(box[1], box[3] + 1, cells_px):
                d.line([box[0], y, box[2], y], fill=(0, 0, 0), width=1)

    if period_px <= 0:
        block(x0, x0 + pw - 1)
    else:
        passage = max(1, int(period_px) // 2)
        for bx in range(x0, x0 + pw, int(period_px)):
            block(bx, min(bx + passage, x0 + pw - 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="PNG")
    return path
