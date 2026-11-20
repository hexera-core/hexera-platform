# Responsibility: Hold settings/viewer_limits.DRAWN_MAX_TRIANGLES to the arithmetic that chose it -
# that it admits the lossless surface of the mesh it was raised for, and that the whole page at
# that budget still fits the one browser configuration this repo has actually measured.
# Boundaries: arithmetic over recorded measurements. It opens no mesh and starts no browser.
"""THE CEILING IS A MEASUREMENT, SO IT IS TESTED AS ONE.

`DRAWN_MAX_TRIANGLES` was 800,000 for twelve days after the measurement that should have moved it
was written down. The comment beside it said "a build that has been measured sets
HEXERA_DRAWN_MAX_TRIANGLES" and nobody did, so the delivered cold plate - 283,429,764 cells,
coolant passages 98.2422 um wide - was served the LOSSY route: 17,070,364 faces clustered to
524,274 triangles at 567.2 um, which is 5.8x coarser than the channel. Every drawn triangle
spanned about six passages.

A number that is only defended in a comment moves back the first time somebody wants a smaller
payload. These are the two things that must stay true of it, and neither is an opinion.
"""
from __future__ import annotations

from meshpipeline.settings import viewer_limits as VL

#: THE LOSSLESS SURFACE OF THE DELIVERED PLATE, measured 2026-11-19 by running
#: render.exact_surface over the live workspace of job 6dd6703c-c8d2-43cd-882c-a01c7375d014
#: (fluid 16,584,838 faces -> 854,403 polygons, solid 485,526 -> 205,265, area error 2.17e-16,
#: no vertex moved) and counting the fan triangulation off the poly stream.
PLATE_EXACT_POLYGONS = 1_059_668
PLATE_EXACT_TRIANGLES = 2_164_434

#: ...and the polygon-size distribution that makes the ratio 2.0426 and not 2. Counted over the
#: same surface. "Two triangles per polygon" is right for a quad and wrong for this mesh by
#: 45,098 triangles, which is why the figure above is counted and not derived.
PLATE_POLYGON_SIZES = {3: 953, 4: 1_016_303, 5: 38_966, 6: 3_256, 7: 187, 8: 3}

#: What the poor-cell overlays add. They are drawn OVER the mesh and `cfmesh_conjugate.viewer`
#: adds them AFTER `fit_to_budget` on purpose - so they are never merged, and so they are not
#: counted against this ceiling. Any sum that forgets them is a sum of less than what is on
#: screen. Measured on the same payload: 13 sets.
PLATE_POOR_POLYGONS = 423_102
PLATE_POOR_TRIANGLES = 859_079

#: THE ONE CONFIGURATION THIS REPO HAS MEASURED DRAWING WELL. 2026-11-07, one actor at 1:1 on
#: Intel UHD via ANGLE/D3D11: 1,904,636 faces / 3,810,902 triangles at 55.2 fps and 0.30 ms of CPU
#: per frame, 174.4 MB of VBO with edges off.
MEASURED_GOOD_POLYGONS = 1_904_636
MEASURED_GOOD_TRIANGLES = 3_810_902
#: ...in DECIMAL megabytes, which is the unit that record is written in and is not a detail: read
#: as MiB the same two measurements give 96.0 B and 91.8 B a polygon, which do not agree and would
#: make "about 92 B" look like a coincidence of one payload rather than a property of the
#: renderer. 174.4e6 / 1,904,636 = 91.6 and 1,396e6 / 15,200,000 = 91.8, which do.
MEASURED_GOOD_VBO_BYTES = 174.4e6

#: VRAM per DRAWN POLYGON, which is what binds. The same figure falls out of both of that day's
#: measurements - 174.4 MB / 1,904,636 = 91.6 B and 1,396 MB / 15.2 M = 91.8 B - which is why it
#: is treated as a constant of the renderer and not of one payload.
VRAM_BYTES_PER_POLYGON = 92


def test_the_fan_triangulation_is_counted_and_not_assumed():
    """n - 2 per polygon over the REAL distribution, which is not 2 per polygon.

    This is here because the obvious shortcut is wrong by an amount that matters at the margin:
    the ceiling is set from a triangle count, and "two per polygon" understates this surface by
    45,098 triangles. A mesh with hanging nodes has more n-gons, not fewer.
    """
    assert sum(PLATE_POLYGON_SIZES.values()) == PLATE_EXACT_POLYGONS
    counted = sum(n * (k - 2) for k, n in PLATE_POLYGON_SIZES.items())
    assert counted == PLATE_EXACT_TRIANGLES
    assert 2 * PLATE_EXACT_POLYGONS == PLATE_EXACT_TRIANGLES - 45_098, (
        "the quad shortcut should be wrong by exactly the n-gons this surface has")


def test_the_ceiling_admits_the_plate_drawn_losslessly():
    """The whole reason it moved. Below this the product throws away a surface it has already
    built, and serves a clustered approximation of a 98 um channel at 567 um instead."""
    assert VL.DRAWN_MAX_TRIANGLES >= PLATE_EXACT_TRIANGLES, (
        f"the ceiling is {VL.DRAWN_MAX_TRIANGLES:,}, so the delivered plate's lossless surface of "
        f"{PLATE_EXACT_TRIANGLES:,} triangles does not fit and the budget route runs instead - "
        "which is the defect this constant was raised to fix")


def test_the_whole_page_at_this_ceiling_still_fits_what_was_measured():
    """THE OVERLAYS ARE PART OF THE PAGE even though they are not part of the budget.

    A ceiling chosen against the mesh alone is a ceiling chosen against part of the screen. The
    sum that has to hold is in POLYGONS, because VRAM is: a payload spending the whole budget,
    plus the overlays this plate carries, must stay at or under the polygon count that was
    measured drawing at 55.2 fps.
    """
    budget_polygons = VL.DRAWN_MAX_TRIANGLES * PLATE_EXACT_POLYGONS / PLATE_EXACT_TRIANGLES
    worst_case = budget_polygons + PLATE_POOR_POLYGONS
    assert worst_case <= MEASURED_GOOD_POLYGONS, (
        f"at a ceiling of {VL.DRAWN_MAX_TRIANGLES:,} a full payload is {budget_polygons:,.0f} "
        f"polygons, and with this plate's {PLATE_POOR_POLYGONS:,} polygons of quality overlay "
        f"that is {worst_case:,.0f} - past the {MEASURED_GOOD_POLYGONS:,} that were measured "
        "drawing at 55.2 fps. Raising the ceiling further needs a new measurement, not a bigger "
        "number")
    assert worst_case * VRAM_BYTES_PER_POLYGON <= MEASURED_GOOD_VBO_BYTES, (
        "and the same sum in bytes must stay under the 174.4 MB of VBO that was measured")


def test_the_ceiling_is_not_quietly_enormous():
    """The other direction. 92 B a polygon means a ceiling can be set to a number that no machine
    can hold, and nothing would fail until a reader's tab did - WebGL exposes no way to ask how
    much video memory is left, which is why this file exists rather than a runtime check."""
    polygons = VL.DRAWN_MAX_TRIANGLES * PLATE_EXACT_POLYGONS / PLATE_EXACT_TRIANGLES
    assert polygons * VRAM_BYTES_PER_POLYGON < 512e6, (
        "a drawn surface alone should not be budgeted past half a gigabyte of video memory")


def test_the_measured_point_is_quoted_consistently():
    """The two figures that travel together everywhere in this record have to agree with each
    other, and the way they agree is the point of this file.

    THE FIRST VERSION OF THIS TEST ASSERTED 3,810,902 == 2 x 1,904,636 AND FAILED BY 1,630.
    It was written on the assumption that the measured actor was all quads, which is the same
    assumption that understates this plate's own exact surface by 45,098 triangles. It is wrong
    both times and for the same reason: a cartesian boundary is mostly quads with n-gons wherever
    the octree graded, so the ratio is a little over 2 and never exactly 2. The numbers are
    right; the shortcut was not.
    """
    ratio = MEASURED_GOOD_TRIANGLES / MEASURED_GOOD_POLYGONS
    assert 2.0 < ratio < 2.1, ratio
    assert MEASURED_GOOD_TRIANGLES > 2 * MEASURED_GOOD_POLYGONS, (
        "a fan triangulation of a quad-and-n-gon boundary never comes out BELOW 2 per polygon")
    assert abs(MEASURED_GOOD_VBO_BYTES / MEASURED_GOOD_POLYGONS - VRAM_BYTES_PER_POLYGON) < 1.0


def test_the_overlays_are_counted_in_triangles_and_polygons_consistently():
    """A sanity bound on the overlay figures themselves: they are mostly quads, like the rest of
    a cartesian boundary, so their triangle count sits just over twice their polygon count."""
    ratio = PLATE_POOR_TRIANGLES / PLATE_POOR_POLYGONS
    assert 2.0 <= ratio < 2.1, ratio
