# Responsibility: Choose how fine a conjugate run is, from the fidelity asked for and the cell limit.
# Boundaries: arithmetic on measured numbers. It meshes nothing and refuses nothing - plan.py carries what it chose.
# Collaborates with: cfmesh_conjugate/plan.py (which stores the choice) and cfmesh_conjugate/driver.py (which asks).
"""HOW FINE, and why this engine cannot hold the product's flow rule on these parts.

THE PRODUCT'S RULE IS 13 CELLS ACROSS the narrowest passage (engines/passage.PASSAGE_CELLS_ACROSS,
floor 12), and every flow engine's resolution_floor gate enforces it. This engine does not reach it
on a real cold plate, and that is measured rather than assumed:

    manifold_two_level   block 13.86 mm3, 100 um channels
        5 cells across      581,155 cells        (measured)
        13 cells across   ~10.2 M cells          (the same mesh scaled by 13/5 cubed)

against CELL_HARD_LIMIT of 8,000,000. On the laptop the same wall shows up as memory: at 13 across
only one of the eleven cold plates in the set fits 15.5 GB, at 909 bytes per cell.

SO THE BAR THIS ENGINE IS JUDGED BY IS NOT A CELL COUNT. The staircase measurements
(engines/staircase_error) say plainly that cells-across does not predict the error for any one
part - seven across came out fourteen times worse than six on the unit cell, and the error grew
steadily with refinement on the manifold - so a count would be a bar that neither proves nor
disproves anything about a given mesh. What is measured on every run is the thing the count was a
proxy for: the meshed coolant's disagreement with the CAD it was cut from. That is what the gate
reads, and the resolution used is reported beside it.

THE ESTIMATE BELOW IS A PRE-FLIGHT ONE and says so. It prices the whole block at the finest cell,
which is what the octree does when the refinement band covers the part - true for a thin plate,
badly over for a thick one. Measured against the two runs that exist:

    unit_cell_100um    4 across   estimated 584,192   meshed 661,776   (0.88x: estimate LOW)
    manifold_two_level 5 across   estimated 1,732,500 meshed 581,155   (2.98x: estimate HIGH)

A factor of three either way, so it is used only to keep a run from being started at a resolution
that obviously cannot finish. The delivered cell count is gated for real by manifest_valid against
the same limit.
"""
from __future__ import annotations

import math

#: What each mesh fidelity asks for, across the narrowest passage. These are the resolutions the
#: eleven-part lab sweep actually ran (engines/staircase_error), not a ladder invented here:
#: 2 across is in the table only to show what it costs (median 5.53% on volume, worst 27.54%), and
#: is not on offer.
CELLS_ACROSS_BY_FIDELITY: dict[str, int] = {
    "draft": 4,
    "standard": 5,
    "fine": 6,
    "max": 8,
}

#: The resolution asked for when none is named.
DEFAULT_FIDELITY = "standard"

#: Below this the engine will not mesh. At 2 and 3 cells across the measured volume error on the
#: eleven parts runs to 27.54% and 32.30%, so a mesh there cannot carry a claim about the coolant
#: it encloses - which is the one claim this engine's gate is built on.
FLOOR_CELLS_ACROSS = 4

#: How many octree levels below the background the coolant surface is refined to. Three on every
#: run in the set; the background is sized from it (plan.background_for).
LEVELS = 3

#: The pre-flight estimate's safety factor, from the one part where it came out LOW (0.88x).
ESTIMATE_MARGIN = 1.25


def estimate_cells(*, block_lo, block_hi, narrowest_m: float, cells_across: int,
                   levels: int = LEVELS) -> int:
    """A pre-flight cell count: the block filled with background cells, every one refined.

    Read the module docstring before using this for anything but "can this run be started at all".
    """
    if cells_across < 1 or narrowest_m <= 0.0:
        raise ValueError("a resolution needs a positive passage width and at least one cell")
    fine = float(narrowest_m) / int(cells_across)
    background = fine * (2 ** int(levels))
    n_bg = 1
    for k in range(3):
        span = float(block_hi[k]) - float(block_lo[k])
        n_bg *= max(1, int(math.ceil(span / background)))
    return int(n_bg * (2 ** int(levels)) ** 3 * ESTIMATE_MARGIN)


def cells_across_for(*, fidelity: str, block_lo, block_hi, narrowest_m: float,
                     limit: int, levels: int = LEVELS) -> tuple[int, int, str]:
    """(cells across, the estimate for it, what to say about it).

    Starts from what the fidelity asks and comes DOWN one step at a time until the estimate fits
    the cell limit, never below the floor. The third value is empty when the fidelity was met and
    otherwise says, in the user's words, what was given up and why - the run reports it rather
    than quietly meshing coarser than it was asked to.
    """
    asked = CELLS_ACROSS_BY_FIDELITY.get((fidelity or "").strip().lower(),
                                         CELLS_ACROSS_BY_FIDELITY[DEFAULT_FIDELITY])
    for n in range(asked, FLOOR_CELLS_ACROSS - 1, -1):
        est = estimate_cells(block_lo=block_lo, block_hi=block_hi, narrowest_m=narrowest_m,
                             cells_across=n, levels=levels)
        if est <= limit:
            said = "" if n == asked else (
                f"meshed with {n} cells across the narrowest passage rather than the {asked} this "
                f"fidelity asks for: {asked} across would be about "
                f"{estimate_cells(block_lo=block_lo, block_hi=block_hi, narrowest_m=narrowest_m, cells_across=asked, levels=levels):,} "
                f"cells against a limit of {limit:,}")
            return n, est, said
    est = estimate_cells(block_lo=block_lo, block_hi=block_hi, narrowest_m=narrowest_m,
                         cells_across=FLOOR_CELLS_ACROSS, levels=levels)
    # MESHED ANYWAY, AT THE FLOOR. The estimate came out high by three times on one of the two
    # parts it has been checked against, so refusing here would turn parts that mesh into parts
    # that were never tried. The delivered cell count is what is actually held to the limit
    # (the manifest_valid gate), and this says what to expect.
    return FLOOR_CELLS_ACROSS, est, (
        f"meshed with {FLOOR_CELLS_ACROSS} cells across the narrowest passage, the fewest this "
        f"engine will use. The estimate for it is about {est:,} cells against a limit of "
        f"{limit:,}, so the delivered mesh may not fit the budget - the estimate has come out "
        "high by as much as three times on the parts it has been checked against, so the run "
        "goes ahead and the delivered count decides")


def fits(estimate: int, limit: int) -> bool:
    return int(estimate) <= int(limit)


__all__ = ["CELLS_ACROSS_BY_FIDELITY", "DEFAULT_FIDELITY", "ESTIMATE_MARGIN",
           "FLOOR_CELLS_ACROSS", "LEVELS", "cells_across_for", "estimate_cells", "fits"]
