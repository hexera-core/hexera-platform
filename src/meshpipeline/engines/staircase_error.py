# Responsibility: Say what volume error a cut-cell mesh carries at a given cells-across, from measurement.
# Boundaries: evidence ABOUT the sizing rule in engines/gap_sizing; it sizes nothing and decides nothing.
"""What the staircase actually costs, measured on real meshes.

WHAT THIS IS NOT. It is not a second sizing rule. `engines/gap_sizing` owns that decision and holds
every passage to PASSAGE_CELLS_ACROSS cells across, with the resolution_floor gate refusing a mesh
below PASSAGE_FLOOR_CELLS. Those constants are the product's rule and this module does not compete
with them - a second rule for one decision is two readers of one fact that can disagree.

WHAT IT IS. The rule's justification is from practice and literature: a laminar channel profile
needs about ten cells across to carry its pressure drop, RANS internal-flow practice is 20 to 40,
the meshers' own proximity defaults sit under ten. All sound, none of it measured on our own meshes.
This supplies that measurement, so a job can state the accuracy its resolution bought instead of
asserting that it is enough.

THE MEASUREMENTS, 2026-10-21. Three microchannel geometries meshed with cfMesh, each region's volume
compared against the CAD it was built from:

    cells across    volume error    geometry
         2             3.21%        10 um channels between 15 um silicon fins, 5 um cells
         5             1.04%        100 um channels, 20 um cells
        12             0.12%        119 to 300 um branching network, 10 um cells

A cut-cell mesher approximates an angled or curved wall with steps, so the volume it encloses is
systematically short until the steps are small against the passage. These are those errors.

AND EACH OF THOSE THREE NUMBERS IS ONE PART, which the first table did not say and which turns out
to be most of where the number comes from. Seven parts were meshed at 2 cells across on 2026-10-22
and five at 5 across, same mesher, same recipe:

    cells across    |error| range        median      parts
         2          1.75% to 27.54%      5.73%       7
         5          0.13% to 13.26%      1.53%       5

The 3.21% in the table above is the BEST of seven at that resolution, not the typical one, and
quoting it as what 2 cells across costs understates the worst by a factor of eight. The sign moves
too: at 2 across the errors ran from -27.5% to +5.9%.

WHAT SEPARATES THE GOOD ONES FROM THE BAD ONES is not the cell count, it is how many differently
placed walls the part averages over. A wall that falls between two rows of cell centres loses a row
of cells, one that falls just outside gains one, and a part presenting the same wall placement over
and over gets that error multiplied rather than cancelled:

    hotspot_array            24 passages, offset from each other        +2.2%
    lee_garimella            10 passages                                -1.7%
    manifold_two_level       16 channels plus slots                     +5.3%
    murray_branching         31 segments at many angles                 +5.9%
    unit_cell_100um          ONE prismatic channel, axis-aligned       -27.5%
    pin_fin_300um            round posts, one gap width throughout     -15.3%

The two worst are the two with a single wall placement each, and a plain straight-channel cold plate
is exactly that shape. The commonest geometry in this field is the one the average flatters most.

So the first table is the right guide to WHAT FINER CELLS BUY - the spread closes from 25 points
wide at 2 across to 13 at 5, and had closed to nothing by 12 - and it is not a prediction for any
one part. `single_passage_error_pct` is the figure to quote for a part with few, similar passages.

WHAT THEY SAY ABOUT THE EXISTING FLOOR. PASSAGE_FLOOR_CELLS is 12, chosen from practice. The 12
across measured here came in at 0.12% on volume, so the floor the gate already enforces is
independently corroborated by measurement rather than only by citation. That is the useful result:
not a new number, confidence in the one that was there.

WHY A TABLE AND NOT A CURVE FIT. Three points do not establish a power law - the local slope between
them runs from -1.2 to -2.4 - and fitting one would invent precision nobody measured. This
interpolates between what was seen and says plainly when it is asked outside that range.
"""
from __future__ import annotations

import math

#: (cells across the passage, measured volume error in per cent). Real meshes, named above.
#: Parts with MANY passages, where a misaligned wall in one is cancelled by another.
MEASURED: tuple[tuple[int, float], ...] = ((2, 3.21), (5, 1.044), (12, 0.123))

#: The WORST measured at each resolution, which was the same part both times: one prismatic
#: channel with axis-aligned walls, where nothing cancels. Magnitudes, because the sign moves with
#: the alignment and is not a property of the resolution - the 2-across mesh came out 27.5% short
#: of the CAD and the 5-across one 13.3% over it.
MEASURED_SINGLE_PASSAGE: tuple[tuple[int, float], ...] = ((2, 27.54), (5, 13.26))

#: How many parts each row of each table rests on, so a reader can weigh it.
MEASURED_PARTS: dict[int, int] = {2: 7, 5: 5, 12: 1}

#: The coarsest and finest resolutions anything was actually measured at. Outside this the answer
#: is an extrapolation and says so rather than quietly pretending.
COARSEST_MEASURED = MEASURED[0][0]
FINEST_MEASURED = MEASURED[-1][0]


class StaircaseEvidenceError(ValueError):
    """Asked for the error at a resolution that is not a resolution."""


def volume_error_pct(cells_across: float) -> float:
    """The volume error measured at this resolution, interpolated between the real points.

    Interpolation is log-log, because the error falls roughly geometrically with resolution and a
    straight line in linear space would badly overstate the middle of the range.

    Outside the measured range this CLAMPS to the nearest measured value rather than extrapolating.
    Past 12 across the true error keeps falling, so the clamp is conservative - it reports the worst
    case still supported by evidence. Use `is_extrapolated` to tell the two apart.
    """
    return _interpolate(MEASURED, cells_across)


def _interpolate(table: tuple[tuple[int, float], ...], cells_across: float) -> float:
    """Log-log interpolation inside a table, clamping to its ends outside it.

    One implementation for both tables. Two copies of this was the obvious way to add the second
    one and would have been two things that could disagree about how the middle of the range is
    read.
    """
    if cells_across <= 0 or not math.isfinite(cells_across):
        raise StaircaseEvidenceError(f"cells across must be a positive number, got {cells_across}")
    if cells_across <= table[0][0]:
        return table[0][1]
    if cells_across >= table[-1][0]:
        return table[-1][1]
    for (n0, e0), (n1, e1) in zip(table, table[1:]):
        if n0 <= cells_across <= n1:
            t = (math.log(cells_across) - math.log(n0)) / (math.log(n1) - math.log(n0))
            return math.exp(math.log(e0) + t * (math.log(e1) - math.log(e0)))
    return table[-1][1]


#: The single-passage curve, with the 12-across point BORROWED from the many-passage table. By 12
#: the two had closed to inside a tenth of a per cent of each other, which is the same thing as
#: saying the alignment stops mattering once the cells are small enough; nothing was measured on a
#: single passage at 12, and pretending the spread stays wide out there would be its own invention.
_SINGLE_CURVE: tuple[tuple[int, float], ...] = MEASURED_SINGLE_PASSAGE + (MEASURED[-1],)


def single_passage_error_pct(cells_across: float) -> float:
    """The volume error measured on a part with ONE passage, where nothing cancels.

    The honest upper end of what a resolution can cost, and the figure to quote for a part with few
    large passages. Clamps outside the measured range, same as the average curve.
    """
    return _interpolate(_SINGLE_CURVE, cells_across)


def is_extrapolated(cells_across: float) -> bool:
    """True when the answer is a clamp to the edge of the evidence, not a reading from inside it."""
    return not (COARSEST_MEASURED <= cells_across <= FINEST_MEASURED)


def annotate(cells_across: float) -> dict:
    """What a mesh at this resolution is worth, for the manifest and the brief.

    Phrased as evidence the reader can check, never as a verdict: the verdict belongs to the
    resolution_floor gate, which has its own floor and its own authority.
    """
    err = volume_error_pct(cells_across)
    clamped = is_extrapolated(cells_across)
    return {
        "cells_across": cells_across,
        "expected_volume_error_pct": round(err, 3),
        "worst_measured_volume_error_pct": round(single_passage_error_pct(cells_across), 3),
        "clamped_to_measured_range": clamped,
        "measured_range": [COARSEST_MEASURED, FINEST_MEASURED],
        "basis": (
            "measured on real meshes, not cited from practice: 2 cells across a passage cost 1.75% "
            "to 27.54% of fluid volume over seven parts (median 5.73%), 5 across cost 0.13% to "
            "13.26% over five parts (median 1.53%), and 12 across cost 0.12%. What separates them "
            "is how many differently placed walls the part averages over: a block of 24 offset "
            "passages came in at 2.2% where a single prismatic channel with axis-aligned walls "
            "came in at 27.5%. The worst_measured figure is that single channel, and a plain "
            "straight-channel cold plate is nearer it than the median"),
        "note": (
            "finer than anything measured; the figure is the 12-across result and the true error "
            "is smaller" if clamped and cells_across > FINEST_MEASURED else
            "coarser than anything measured; the true error is larger than the figure"
            if clamped else "read from inside the measured range"),
    }


def corroborates_floor(floor_cells: float) -> dict:
    """Whether the gate's floor is supported by what was measured, and by how much.

    The gate holds PASSAGE_FLOOR_CELLS from practice. This says what that floor is worth on our own
    meshes, so the number carries a measurement and not only a citation.
    """
    return {
        "floor_cells": floor_cells,
        "expected_volume_error_pct": round(volume_error_pct(floor_cells), 3),
        "inside_measured_range": not is_extrapolated(floor_cells),
    }


__all__ = ["MEASURED", "MEASURED_SINGLE_PASSAGE", "MEASURED_PARTS", "COARSEST_MEASURED", "FINEST_MEASURED",
           "StaircaseEvidenceError", "volume_error_pct", "single_passage_error_pct",
           "is_extrapolated", "annotate", "corroborates_floor"]
