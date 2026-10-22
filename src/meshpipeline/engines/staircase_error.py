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
to be most of where the number comes from. Eight parts were meshed at 2 cells across on 2026-10-22
and five at 5 across, same mesher, same recipe:

    cells across    |error| range        median      parts
         2          0.05% to 27.54%      5.53%       8
         5          0.13% to 13.26%      1.06%       6

The range at ONE resolution covers nearly three orders of magnitude, so a single number for what
2 cells across costs cannot be right whichever number is chosen. The sign moves too: at 2 across
the errors ran from -27.5% to +5.9%.

WHAT SORTS THEM is how many DIFFERENTLY PLACED WALLS the part averages over. A wall that falls
between two rows of cell centres loses a row of cells, one that falls just outside gains one, and a
part presenting the same wall placement over and over gets that error multiplied rather than
cancelled.

Two things supply different placements, and the two resolutions each show one of them.

At 2 cells across it is the PASSAGE COUNT, almost perfectly:

    corintis_straight_100um  100 passages                              -0.05%
    lee_garimella             10 passages                               -1.75%
    hotspot_array             24 passages                               +2.20%
    manifold_two_level        1 network, 16 channels                    +5.33%
    murray_ported             1 network, 31 segments                    +5.73%
    murray_branching          1 network, 31 segments                    +5.92%
    pin_fin_300um             1 chamber around posts                   -15.26%
    unit_cell_100um           ONE prismatic channel                    -27.54%

Monotone but for the inversion between 10 and 24, which is 0.45 of a point.

At 5 cells across it is not, and the exception is the informative one:

    murray_ported             1 passage, 31 segments at many angles     +0.13%
    murray_branching          the same tree, sealed                     +0.52%
    lee_garimella            10 passages                                +0.58%
    manifold_two_level        1 network, 16 axis-aligned channels       +1.53%
    pin_fin_300um             1 chamber around posts                    +3.97%
    unit_cell_100um           ONE prismatic channel                    +13.26%

The branching tree has ONE passage and beats the ten-passage part. Its walls run at every angle, so
a single passage supplies as many distinct placements as ten parallel ones do, and the error
averages away inside it. Axis-aligned walls are what cannot average: the three worst at both
resolutions are the three whose walls are all parallel to the octree.

So a plain straight-channel cold plate with a handful of wide passages is the bad end, and it is
the commonest geometry in this field.

A PREDICTION THAT WAS WRONG, recorded because it is the one that would otherwise be made again.
Before the 100-channel part was run it was expected to come out BADLY, near the unit cell, on the
reasoning that its channels are identical and at identical pitch, so they would share one alignment
against the octree and have nothing to average against each other. It came in at -0.05%, the best
of the eight. A hundred identical channels do average.

Alignment is real and second-order. The unit cell at 2 across, same resolution, with the block grown
on one face so the octree falls differently: -27.54%, -31.82%, -27.01%, -27.43% for block heights of
1.300, 1.350, 1.400 and 1.500 mm. Four points of movement on a part already at twenty-seven, and it
jumps rather than trending, which is what alignment looks like.

So the first table is the right guide to WHAT FINER CELLS BUY ON AVERAGE - the spread closes from
27 points wide at 2 across to 13 at 5, and had closed to nothing by 12 - and it is not a prediction
for any one part. `single_passage_error_pct` is the figure to quote for a part with one passage or a
few similar ones.

AND REFINING DOES NOT RELIABLY REDUCE IT. This is the finding that matters most for anyone reading
a single number off a single mesh, and it was measured on purpose: every cells-across from 2 to 8
on one part, and 4 to 7 on a second.

    unit_cell_100um, one 100 um channel
        2 across    h 44.91 um      -27.54%
        3 across    h 33.97 um      -32.30%
        4 across    h 24.52 um       -5.97%
        5 across    h 19.72 um      +13.26%
        6 across    h 16.60 um       -1.11%
        7 across    h 14.32 um      -15.20%
        8 across    h 12.50 um       -0.15%

    manifold_two_level, two levels joined at slots
        4 across    h 24.96 um       +0.05%
        5 across    h 19.97 um       +1.53%
        6 across    h 16.72 um       +2.18%

Seven cells across is FOURTEEN TIMES WORSE than six on the first part. On the second the error grows
steadily as the mesh is refined, from 0.05% to 2.18%. Neither sequence is monotone in the direction
anyone would assume, and the two are not even the same shape, so this is a property of a part and a
resolution together rather than of either alone.

An even-versus-odd rule looked obvious on the first part - the even values fall monotonically,
-27.54, -5.97, -1.11, -0.15, and the odd ones do not - and it is not there on the second. Recorded
because it is the pattern anyone would reach for next.

WHAT TO DO ABOUT IT. Not trust one mesh. `devtools/semiconductor/convergence.py` and `semi converge`
exist for exactly this: three meshes of the same part give an observed order of convergence, an
extrapolated value and a band, and they refuse to give a band when the sequence does not converge
monotonically. A two-mesh comparison that happens to agree is the trap, because on these sequences
two meshes can agree closely and both be far out.

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
MEASURED_PARTS: dict[int, int] = {2: 8, 5: 6, 12: 1}

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
            "measured on real meshes, not cited from practice: 2 cells across a passage cost 0.05% "
            "to 27.54% of fluid volume over eight parts (median 5.53%), 5 across cost 0.13% to "
            "13.26% over six parts (median 1.06%), and 12 across cost 0.12%. What sorts them is "
            "how many differently placed walls the part averages over, which comes from many "
            "passages or from one passage with walls at many angles: a 100-channel array came in "
            "at 0.05% and a branching tree with one passage at 0.13%, where a single prismatic "
            "channel with axis-aligned walls came in at 27.5%. The worst_measured figure is that "
            "channel, and a straight-channel cold plate with a few wide passages is nearest it"),
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
