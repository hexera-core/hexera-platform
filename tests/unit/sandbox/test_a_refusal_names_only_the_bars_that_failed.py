# Responsibility: Verify that the line logged when a frame cannot be shown to resolve cells names
# the tests that ACTUALLY FAILED on it, and asserts nothing about the ones it cleared.
# Boundaries: the reading side's log only. Nothing here renders, nothing here changes a verdict,
# and every frame below is refused in both arms.
#
# THE DEFECT THIS IS WRITTEN FROM. `ArtifactResolver`'s "sized correctly but the picture is not of
# cells" branch printed all four bars unconditionally, so an operator was told three comparisons
# had failed that had not. MEASURED 2026-11-18 against the pre-change tree, SYNTHETIC, VOLUME,
# 1280x960 at a claimed 4.00 px cell, through the production ArtifactResolver:
#
#   `write_passages(cells_px=8, period_px=64)` resolves 60 interiors on its best row over 840
#   rows at 50.0000% ink, and the log read "only 60 cell(s) are resolved on its best row, under
#   the 4 a passage needs, on 840 row(s) against the 12 an array needs, at a drawn spacing of
#   8.0 px against the 4.00 px a cell should be, and 50.0000% of the frame has anything drawn in
#   it at all against the 0.1000% a cut needs". 60 is not under 4, 840 is not under 12, and
#   50.0000% is five hundred times the floor it is printed as missing. One of the four was true.
#
# AND REAL PLATE, SURFACE, job 0ff6f9e5, along_the_flow zoom, the same line: "3.2585% of the frame
# has anything drawn in it at all against the 0.1000% a cut needs" - thirty-two times the floor.
#
# NOTHING BELOW IMPORTS A NAME THIS ROUND ADDED, and no assertion here is about a verdict: every
# frame in this file is refused before and after, which is asserted alongside the words.
from __future__ import annotations

import json
import logging
from pathlib import Path

from _part_pictures import (  # noqa: E402  (this dir is on sys.path under pytest)
    write_passages,
    write_wireframe_lattice,
)

from meshpipeline.sandbox.review_session import FRAME_ZOOM, ArtifactResolver

#: Job 0ff6f9e5's own numbers, off the delivered manifest and the plan it ran with.
CELL_M = 2.51578e-05
FRAME_PX = (1280, 960)
MPP = CELL_M / 4.0

REGION = {"name": "across_the_flow", "kind": "slice", "normal": [1.0, 0.0, 0.0],
          "plane_axes": [1, 2], "label": "Square across the coolant's path",
          "why": "the cells across each passage, and the metal between them"}


def _refusal(ws: Path, caplog, draw, **kw) -> tuple[object, str]:
    """Draw one frame, resolve it as production does, and hand back the cut and the line logged."""
    rel = "review/cuts/across_the_flow_zoom.png"
    draw(ws / rel, *FRAME_PX, **kw)
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    man = {"mesh_units": "m", "mesh_paths": {"surface": "mesh.msh"},
           "quality": {"near_wall_cell_m": CELL_M, "min_cell_m": 7.787400214457237e-06,
                       "max_cell_m": 0.0006788786304584814},
           "inspection_regions": [dict(REGION, frames=[
               {"scope": FRAME_ZOOM, "image": rel,
                "window_m": [MPP * FRAME_PX[0], MPP * FRAME_PX[1]],
                "metres_per_pixel": MPP,
                "centre_m": [0.0309152940, 0.0242716710],
                "centre_source": "the densest window-sized patch of this cut"}])]}
    (ws / "manifest.json").write_text(json.dumps(man))
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        got = ArtifactResolver(ws, man).resolve_cuts().get("across_the_flow", ())
    assert got, "the frame was not offered at all, so nothing below is being tested"
    # MATCHED ON THE OPENING ALONE, so that the assertions in each test are what distinguishes
    # the two trees. Neither frame here reaches any other branch that opens this way: both hold a
    # measured periodic structure, so the "its own pixels hold no passage array" line - the only
    # other one that says "is sized correctly" - cannot fire on them.
    lines = [r.getMessage() for r in caplog.records if "is sized correctly" in r.getMessage()]
    assert len(lines) == 1, (
        "exactly one refusal of this shape is expected: " + repr(
            [r.getMessage() for r in caplog.records]))
    return got[0], lines[0]


def test_a_healthy_interior_count_is_not_reported_as_a_failure(tmp_path, caplog):
    """THE FRAME THE FOUR-BAR SENTENCE WAS MEASURED ON: 60 interiors, 840 rows, 50% ink.

    Its one real failure is the drawn cell: 8.0 px on the axis its structure repeats on against
    the 4.00 px its own window declares, a factor of two. The other three figures are healthy and
    the line must not say otherwise.
    """
    cut, line = _refusal(tmp_path, caplog, write_passages,
                         cells_px=8, period_px=64, duty=0.5)
    assert cut.ink is not None
    assert (cut.ink.cells_on_best_row, cut.ink.cell_rows) == (60, 840), (
        cut.ink.cells_on_best_row, cut.ink.cell_rows)
    assert abs(cut.ink.ink_fraction - 0.5) < 1e-6, cut.ink.ink_fraction
    assert not cut.counts_cells, "this frame is refused in both arms and that is not what moved"
    # the one that failed, named, on the axis it was read
    assert "is drawn 8.0 px apart against the 4.00 px" in line, line
    assert "horizontal axis the structure repeats on" in line, line
    # and not one word about the three it cleared
    assert "60 cell(s)" not in line, (
        "60 interiors on the best row clears the floor of 4 fifteen times over: " + line)
    assert "840 row(s)" not in line and "scan row(s)" not in line, line
    assert "50.0000%" not in line and "has anything drawn in it at all" not in line, (
        "50% ink is five hundred times the floor this line said it missed: " + line)


def test_the_bars_that_did_fail_are_all_named(tmp_path, caplog):
    """A LATTICE WITH NO ENCLOSED INTERIOR ANYWHERE: two bars fail and the ink bar does not.

    23.4375% of this frame is drawn, which clears EVIDENCE_INK_FLOOR two hundred times over, and
    it resolves no cell interior at all - so the interior count and the row count are the real
    failures and the ink is not. Naming only the first of them would be the same defect from the
    other side, so both have to be there and the ink must not be.
    """
    cut, line = _refusal(tmp_path, caplog, write_wireframe_lattice, cells_px=8)
    assert cut.ink is not None
    assert (cut.ink.cells_on_best_row, cut.ink.cell_rows) == (0, 0)
    assert cut.ink.ink_fraction > 0.2, cut.ink.ink_fraction
    assert not cut.counts_cells
    assert "only 0 cell(s) are resolved on its best row, under the 4 a passage needs" in line, line
    assert "resolved on only 0 scan row(s), under the 12" in line, line
    assert "no cell interior was resolved along the horizontal axis" in line, line
    assert "has anything drawn in it at all" not in line, (
        "23.4375% ink clears the 0.1000% floor and must not be reported as missing it: " + line)
