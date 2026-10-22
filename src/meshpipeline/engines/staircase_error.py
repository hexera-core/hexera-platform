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
MEASURED: tuple[tuple[int, float], ...] = ((2, 3.21), (5, 1.044), (12, 0.123))

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
    if cells_across <= 0 or not math.isfinite(cells_across):
        raise StaircaseEvidenceError(f"cells across must be a positive number, got {cells_across}")
    if cells_across <= COARSEST_MEASURED:
        return MEASURED[0][1]
    if cells_across >= FINEST_MEASURED:
        return MEASURED[-1][1]
    for (n0, e0), (n1, e1) in zip(MEASURED, MEASURED[1:]):
        if n0 <= cells_across <= n1:
            t = (math.log(cells_across) - math.log(n0)) / (math.log(n1) - math.log(n0))
            return math.exp(math.log(e0) + t * (math.log(e1) - math.log(e0)))
    return MEASURED[-1][1]


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
        "clamped_to_measured_range": clamped,
        "measured_range": [COARSEST_MEASURED, FINEST_MEASURED],
        "basis": (
            "measured on three microchannel geometries, 2026-10-21: 2 cells across a passage cost "
            "3.21% of fluid volume, 5 cost 1.04%, 12 cost 0.12%"),
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


__all__ = ["MEASURED", "COARSEST_MEASURED", "FINEST_MEASURED", "StaircaseEvidenceError",
           "volume_error_pct", "is_extrapolated", "annotate", "corroborates_floor"]
