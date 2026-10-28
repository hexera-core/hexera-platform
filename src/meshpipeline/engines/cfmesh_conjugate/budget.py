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

#: How many cells deep the finest refinement band runs at the coolant surface, measured by
#: subtracting the background count from the delivered count on every conjugate mesh that exists
#: and dividing by area/fine^2:
#:
#:     unit_cell_100um       4 across  0.0250 mm cells     661,776 cells    depth  7.04
#:     manifold_two_level    5 across  0.0200 mm cells     581,155 cells    depth  3.13
#:     corintis cold plate   5 across  0.5304 mm cells   1,852,814 cells    depth  8.56
#:     corintis cold plate  13 across  0.2040 mm cells  17,447,384 cells    depth 11.86
#:
#: IT IS NOT A CONSTANT, AND THE WAY IT MOVES IS THE UNSAFE WAY. The last two rows are the same
#: part at two resolutions, and the band gets DEEPER in cells as the cells get finer: 8.56 at
#: 0.53 mm, 11.86 at 0.204 mm. A single depth therefore UNDER-predicts at fine resolutions, which
#: is the direction that gets a run killed six hours in rather than refused up front. Fitted on
#: those two points, which are the only pair that holds the geometry fixed:
#:
#:     depth = DEPTH_AT_1MM * (fine in mm) ** DEPTH_EXPONENT
#:
#: Two points is a line through two points, not a law; the 4.3x spread across the other parts says
#: the geometry moves it as much as the resolution does. It is used only where being wrong by
#: three times beats being wrong by a thousand - the old whole-block model is 3.5x high at a
#: coarse resolution and ~26x high at a fine one, because the share of the block the band covers
#: falls as the cells shrink (37% of the customer plate at 0.53 mm, 4.8% at 0.069 mm). A model
#: whose error grows with the thing being sized cannot choose a resolution for a machine.
DEPTH_AT_1MM = 6.90
DEPTH_EXPONENT = -0.341


def band_depth(fine_m: float) -> float:
    """How many cells deep the finest band runs, at this cell size."""
    mm = max(float(fine_m) * 1000.0, 1e-9)
    return DEPTH_AT_1MM * (mm ** DEPTH_EXPONENT)

#: Octree depths the ladder may use. THE ONE KNOB THAT REACHES A LARGE MESH, and the one nothing
#: has ever varied - because `estimate_cells` cannot see it. That estimate divides the background
#: count by eight per level and multiplies its own refinement factor by eight, so its answer is
#: identical at every depth. Depth is what buys a coarse background under a fine coolant surface,
#: which is the whole design of the refinement (conjugate.py: "the background is sized from it").
LEVELS_LADDER: tuple[int, ...] = (3, 4, 5, 6)


#: What one cell costs at the mesher's HIGH-WATER MARK, and what OpenFOAM costs before it has
#: meshed anything. Fitted on three conjugate builds an order of magnitude apart, each one the
#: engine's own peak_rss_bytes and not a reading taken off `free` afterwards:
#:
#:      1,852,814 cells   1,802,297,344 bytes   972.7 per cell
#:     17,447,384 cells  13,377,228,800 bytes   766.7 per cell
#:     99,172,391 cells  74,610,000,000 bytes   752.3 per cell
#:
#: A line through the outer two predicts the middle one to 0.7%, which is why this is two numbers
#: rather than one: the per-cell figure alone over-prices a small mesh and under-prices a large
#: one. devtools/semiconductor carries 1010 bytes per cell and 128 MiB fixed, fitted on two builds
#: both under six million cells, which over-prices a hundred-million-cell run by about a third.
#:
#: MEASURED AT THE PEAK, NOT AFTER IT. An earlier fit here used 588 bytes per cell, taken from
#: `free` DURING the 99M run - which is a spot reading, and the moment it was taken was not the
#: high-water mark. It under-priced by 27%, in the direction that gets a run killed rather than
#: refused. peak_rss_bytes is in every conjugate report; nothing else should be used for this.
PEAK_BYTES_PER_CELL = 748.1
PEAK_FIXED_BYTES = 416 * 1024 ** 2


#: THE CEILING IS AN INTEGER, NOT A MACHINE, and it is MEASURED rather than derived.
#:
#: cartesianMesh builds an octree and then allocates a label per octree vertex in a LongList of
#: `label`. This OpenFOAM is built linux64GccDPInt32Opt (WM_LABEL_SIZE=32, and it is the only
#: platform directory in the image), so `label` is a signed 32-bit int and a large enough octree
#: overflows that allocation. The abort is always the same, in meshOctreeAddressing:
#:
#:     --> FOAM FATAL ERROR: Negative size requested.
#:         From void Foam::Module::LongList<T, Offset>::allocateSize(Foam::label)
#:                                     [with T = int; int Offset = 19; Foam::label = int]
#:
#: Measured on one customer cold plate, 2026-10-28, same part and same machine each time:
#:
#:      99,218,211 octree boxes   COMPLETED, 99,172,391 cells, 2530 s, peak 74.61 GB
#:     204,543,984 octree boxes   ABORTED after 302 s, peak 77.55 GB
#:     479,119,929 octree boxes   ABORTED after 277 s, peak 50.20 GB
#:
#: THE MULTIPLIER IS NOT EIGHT. Eight vertices a box would put the ceiling at 2**31/8 =
#: 268,435,455, which the 204,543,984-box run is well inside, and it aborted. 2**31 divided by the
#: plausible multipliers brackets it: /12 gives 178,956,970 and /21 gives 102,261,126, both of
#: which sit between the run that worked and the run that did not, while /8 is excluded by the
#: abort and /24 is excluded by the run that completed. Narrowing it needs bisection runs, and
#: each one is cheap - the abort lands about five minutes in, at "Creating octree vertices".
#:
#: SO THE CEILING IS THE LARGEST COUNT PROVEN TO WORK, not the smallest proven to fail. The
#: failure is an abort partway through, after the whole octree is built, and its words say nothing
#: about a size limit; priced in memory the dead run looked affordable on any 100 GiB machine, so
#: mesh_machine would have sent it to a 961 GiB box to fail there identically and dearer. Refusing
#: a mesh that might have built is the cheap mistake here. Raise this when a run proves a higher
#: number, and lower it if one fails below.
OCTREE_BOXES_PROVEN_OK = 99_218_211
OCTREE_BOXES_PROVEN_ABORT = 204_543_984
#: An octree box per delivered cell, near enough: the 99,218,211-box run delivered 99,172,391
#: cells, 0.05% apart. The outside and data boxes are a rounding error on this part (113,058 and
#: 131,216), so a cell estimate can be compared with a box ceiling directly.
BOXES_PER_CELL = 1.0


def octree_ceiling_cells() -> int:
    """The most cells this mesher is known to be able to build, before any machine is considered."""
    return int(OCTREE_BOXES_PROVEN_OK / BOXES_PER_CELL)


def refuse_for_octree(cells: int) -> str:
    """Why a mesh of `cells` cannot be built at all, or "" when it can.

    Checked BEFORE the machine is chosen, because no machine changes the answer.
    """
    want = int(cells)
    if want <= octree_ceiling_cells():
        return ""
    proven = "" if want < OCTREE_BOXES_PROVEN_ABORT else (
        f" A run of {OCTREE_BOXES_PROVEN_ABORT:,} boxes on this mesher aborted partway through "
        "with 'Negative size requested', having used a third of the machine it was on.")
    return (f"this mesh cannot be built at any size of machine: about {want:,} cells is past the "
            f"largest octree this mesher has been shown to build, {octree_ceiling_cells():,}. "
            "The limit is the 32-bit label this OpenFOAM is compiled with, so a larger machine "
            f"does not help; an Int64 build of OpenFOAM would.{proven}")


def peak_gib(cells: int) -> float:
    """What the mesher will need at its peak, in GiB, for a mesh of `cells`."""
    return (PEAK_FIXED_BYTES + max(0, int(cells)) * PEAK_BYTES_PER_CELL) / (1024.0 ** 3)


def written_gib(cells: int) -> float:
    """What the finished mesh occupies on disk, which Cloud Run holds in memory as well.

    909 bytes a cell, measured on a finished mesh (devtools/semiconductor), and doubled because a
    conjugate build writes the whole block AND the two regions it is cut into.
    """
    return 2.0 * max(0, int(cells)) * 909.0 / (1024.0 ** 3)


def _block_m3(block_lo, block_hi) -> float:
    v = 1.0
    for k in range(3):
        v *= max(0.0, float(block_hi[k]) - float(block_lo[k]))
    return v


def band_estimate_cells(*, block_lo, block_hi, narrowest_m: float, cells_across: int,
                        wetted_area_m2: float, levels: int = LEVELS) -> int:
    """A pre-flight cell count that prices the refinement BAND, not the whole block.

    The band at the coolant surface, plus background cells everywhere else. Needs the coolant's
    wetted area, which cad/fluid_volume.write_surface measures off the triangles it writes; use
    `estimate_cells` where that is not known.

    Against the meshes that have been built (see DEPTH_AT_1MM) it is within a factor of two where
    the whole-block model is within a factor of 3.5, and the two part company at fine resolutions,
    which is exactly where a sizing decision is made: the whole-block model is ~26x high at
    0.069 mm cells on the customer plate.
    """
    if cells_across < 1 or narrowest_m <= 0.0:
        raise ValueError("a resolution needs a positive passage width and at least one cell")
    if wetted_area_m2 <= 0.0:
        raise ValueError("the band is sized from the coolant's wetted area")
    fine = float(narrowest_m) / int(cells_across)
    background = fine * (2 ** int(levels))
    band = float(wetted_area_m2) * band_depth(fine) / (fine * fine)
    bg = _block_m3(block_lo, block_hi) / (background ** 3)
    return int((band + bg) * ESTIMATE_MARGIN)


def plan_for_budget(*, block_lo, block_hi, narrowest_m: float, wetted_area_m2: float,
                    limit: int, asked_across: int) -> tuple[int, int, int, str]:
    """(cells across, octree depth, the estimate, what to say) - the FINEST pair that fits `limit`.

    THE DEPTH IS CHOSEN, not assumed. Depth costs nothing at the coolant surface and divides the
    background count by eight each time, so the pair to want is the asked-for resolution at
    whatever depth makes its background affordable - and only when no depth does that is the
    resolution itself stepped down. The old ladder stepped the resolution down while holding the
    one free knob fixed, which on a customer cold plate collapsed draft, standard, fine and max to
    the same four cells across.
    """
    best = ""
    for across in range(int(asked_across), FLOOR_CELLS_ACROSS - 1, -1):
        for levels in LEVELS_LADDER:
            est = band_estimate_cells(block_lo=block_lo, block_hi=block_hi,
                                      narrowest_m=narrowest_m, cells_across=across,
                                      wetted_area_m2=wetted_area_m2, levels=levels)
            if est <= limit:
                said = "" if (across == asked_across and levels == LEVELS) else (
                    f"meshed with {across} cells across the narrowest passage at {levels} octree "
                    f"levels, about {est:,} cells against a limit of {limit:,}")
                return across, levels, est, said
    deepest = LEVELS_LADDER[-1]
    est = band_estimate_cells(block_lo=block_lo, block_hi=block_hi, narrowest_m=narrowest_m,
                              cells_across=FLOOR_CELLS_ACROSS, wetted_area_m2=wetted_area_m2,
                              levels=deepest)
    # NOTHING FITS, and that is said rather than meshed anyway. The floor branch of
    # `cells_across_for` goes ahead because its estimate has come out high by three times and
    # refusing would turn parts that mesh into parts never tried. This one is different: the band
    # model's error is bounded by the same factor of three, and an estimate of 64,028,252,160
    # against a limit of 8,000,000 is not a three-times question. Starting that run spends hours
    # and ends in a kernel kill.
    return FLOOR_CELLS_ACROSS, deepest, est, (
        f"this part does not fit: {FLOOR_CELLS_ACROSS} cells across its narrowest passage at "
        f"{deepest} octree levels is about {est:,} cells against a limit of {limit:,}. Its "
        f"narrowest feature needs a mesh {est / max(limit, 1):.0f} times larger than this "
        "deployment allows")


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


__all__ = ["PEAK_BYTES_PER_CELL", "PEAK_FIXED_BYTES", "peak_gib", "written_gib", "DEPTH_AT_1MM", "DEPTH_EXPONENT", "band_depth", "CELLS_ACROSS_BY_FIDELITY", "DEFAULT_FIDELITY", "ESTIMATE_MARGIN",
           "LEVELS_LADDER", "band_estimate_cells", "plan_for_budget",
           "FLOOR_CELLS_ACROSS", "LEVELS", "cells_across_for", "estimate_cells", "fits"]
