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

#: HOW DEEP THE REFINEMENT BAND RUNS, IN CELLS - and it is a count of GRADING SHELLS, so what sets
#: it is how many octree levels the band has to climb to reach the background, not how big its
#: cells are. cfMesh puts one shell per level between a refined surface and the background cell;
#: those shells ARE the band.
#:
#: THE OLD FIT WAS AN ARTIFACT AND IS GONE. It read
#:
#:     depth = 6.90 * (fine in mm) ** -0.341
#:
#: fitted on two readings of one part 2.6x apart in cell size - 8.56 at 0.5304 mm and 11.86 at
#: 0.2040 mm - and concluded the band deepens as cells shrink. Both of those runs left the
#: background at fine x 2**3, so a finer cell WAS a wider span and the fit could not separate the
#: two. Extrapolated to the 0.044007 mm cell this customer part actually needs, it predicted 20.0
#: cells of depth. The run measured 6.9. Being 2.9x high is not the direction that kills a run, but
#: it refuses meshes that would have built, which is what it did here.
#:
#: MEASURED WITH THE CELL SIZE HELD FIXED, 2026-10-28 - the first controlled reading this project
#: has had of the question. Same part, same thin surface, same 21,712.67 mm2 of fine wetted area,
#: same 0.044007 mm cell. Only the background moved:
#:
#:     background           span   octree boxes    depth the band implies
#:     1.40822 mm (lvl 8)     5     204,543,984    16.8    ABORTED on the 32-bit label
#:     0.35206 mm (lvl 10)    3     102,152,624     6.9    built
#:
#: Two extra levels of grading cost 2.4x the cells at IDENTICAL resolution. That is the largest
#: lever there is on the size of a conjugate mesh and nothing was using it: every run before this
#: left the background at level 8 and paid for five levels of grading it did not need.
#:
#: AT A SPAN OF 3 the three readings are 8.56, 11.86 and 6.9 over a 12x range of cell size, with no
#: trend in it - which is the evidence that the cell size was never the variable. The scatter is
#: the geometry, as the old comment already suspected ("the 4.3x spread across the other parts says
#: the geometry moves it as much as the resolution does").
#:
#: THE ANCHOR IS THE TOP OF THAT SCATTER, NOT ITS MIDDLE, and that is a deliberate asymmetry. The
#: mean, 9.1, under-predicted the 17,447,384-cell calibration run by 3.8% - and under-predicting
#: sizes a machine for less than the run needs and gets it killed hours in, where over-predicting
#: refuses a run that might have built. This module already records two fits that erred the unsafe
#: way (a 588 B/cell memory figure that under-priced by 27%, and the whole-block cell model). So
#: 11.86 is the anchor: the largest span-3 reading, which reproduces the run the test pins.
#:
#: A LINE THROUGH TWO SPANS, said plainly, and high on purpose. Checked against the two runs above
#: as whole meshes rather than as depths: it predicts 152.7M boxes where 102.2M were built (1.49x
#: high) and 259.1M where 204.5M were built (1.27x high). Both high, both inside the factor of two
#: this estimate is held to. It is used where being wrong by a factor beats being wrong by a
#: thousand.
DEPTH_AT_FITTED_SPAN = 11.86
#: 4.95 cells of depth per octree level, from the one controlled pair: 6.9 at span 3 and 16.8 at
#: span 5, same part, same 0.044007 mm cell, same 21,712.67 mm2 of fine wetted area.
DEPTH_PER_LEVEL = 4.95
#: The span every reading in the table above that is not the span-5 row was taken at.
DEPTH_FITTED_SPAN = 3
#: A band is at least one cell deep on each side of the surface, whatever the line says.
MIN_BAND_DEPTH = 2.0


def band_depth(span: int) -> float:
    """How many cells deep a refinement band runs, for a band `span` octree levels below the
    background. See DEPTH_AT_FITTED_SPAN for what this is measured on and what it replaced."""
    return max(MIN_BAND_DEPTH,
               DEPTH_AT_FITTED_SPAN + DEPTH_PER_LEVEL * (int(span) - DEPTH_FITTED_SPAN))

def span_of(background_m: float, cell_m: float) -> int:
    """How many octree levels separate a background cell from a finer one.

    Rounded, because cfMesh quantises every size to its own root box: a cell size that is not a
    power-of-two division of the background is silently moved to one.
    """
    import math

    if background_m <= 0.0 or cell_m <= 0.0 or cell_m >= background_m:
        return 0
    return max(0, int(round(math.log2(background_m / cell_m))))

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
#:     102,152,624 octree boxes   COMPLETED, 3060 s, peak 185 GB  <- the most, so far
#:     204,543,984 octree boxes   ABORTED after 302 s, peak 77.55 GB
#:     479,119,929 octree boxes   ABORTED after 277 s, peak 50.20 GB
#:
#: THE MULTIPLIER IS TWELVE, and it is not eight. Eight vertices a box would put the ceiling at
#: 2**31/8 = 268,435,455, which the 204,543,984-box run sits well inside, and that run aborted.
#: Twelve is three octree faces owned per box at four point labels each, and it is the figure that
#: fits EVERY row above and below:
#:
#:          boxes              x12       int32?   observed
#:     99,218,211    1,190,618,532       fits     completed
#:    102,152,624    1,225,831,488       fits     completed
#:    178,956,970    2,147,483,640       fits     <- the boundary, 2**31 // 12
#:    204,543,984    2,454,527,808       over     aborted
#:    204,593,041    2,455,116,492       over     aborted
#:    479,119,929    5,749,439,148       over     aborted
#:
#: Six readings, no exceptions, and the boundary falls inside the bracket the runs had already
#: fixed it to - (102,152,624, 204,543,984]. An independent reading of the same question found the
#: same multiplier.
#:
#: SO THE CEILING IS THE ARITHMETIC, NOT THE BRACKET. It was the largest count proven to build,
#: which is safe and crude: it refused the very configuration that had just built, because the cell
#: estimate for that configuration is deliberately 1.49x high (DEPTH_AT_FITTED_SPAN) and 1.49 x
#: 102M is over 102M. Stacking a conservative estimate on a conservative ceiling refuses work twice
#: for one reason. Above this boundary the overflow is certain; below it, this list cannot overflow,
#: and no measurement says another list overflows first.
#:
#: WHAT IT IS NOT. It is not a memory limit, and it is not per machine. Priced in memory the dead
#: runs looked affordable on any 100 GiB box, so mesh_machine would have sent them to a 961 GiB one
#: to fail there identically and dearer. It IS per PROCESS: cfMesh's MPI mode gives each rank its
#: own octree addressing, and the same case that aborts serially at 202,709,195 boxes built across
#: eight ranks in 1397 s. That is the way past this number, and refuse_for_octree says so.
OCTREE_LABELS_PER_BOX = 12
OCTREE_BOXES_PROVEN_OK = 102_152_624
OCTREE_BOXES_PROVEN_ABORT = 204_543_984
#: An octree box per delivered cell, near enough: the 99,218,211-box run delivered 99,172,391
#: cells, 0.05% apart. The outside and data boxes are a rounding error on this part (113,058 and
#: 131,216), so a cell estimate can be compared with a box ceiling directly.
BOXES_PER_CELL = 1.0


def octree_ceiling_cells(ranks: int = 1) -> int:
    """The most cells this mesher can build, before any machine is considered.

    `ranks` is how many processes the mesh is built across. The overflowing list is per process, so
    ranks multiply the ceiling - measured, not assumed: the case that aborts serially at
    202,709,195 boxes completed across eight ranks.
    """
    per_rank = (2 ** 31 - 1) // OCTREE_LABELS_PER_BOX
    return int(max(1, int(ranks)) * per_rank / BOXES_PER_CELL)


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
    return (f"this mesh cannot be built by this mesher on one process: about {want:,} cells is "
            f"past the largest octree it has been shown to build, {octree_ceiling_cells():,}. The "
            "limit is the 32-bit label this OpenFOAM is compiled with, so a larger machine does "
            "not help. What does: meshing across processes, where each one counts only its own "
            "share - every tool in this chain takes -parallel except reconstructParMesh, which is "
            "the step that assembles the mesh back onto one process and therefore the step to "
            f"skip. An Int64 build of OpenFOAM would lift it outright.{proven}")


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

    Against the meshes that have been built (see DEPTH_AT_FITTED_SPAN) it is within a factor of two where
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
    # THE SPAN IS `levels`, by construction: the background is the fine cell shifted up that many
    # octree levels, two lines above. Passing it is what makes this estimate able to tell two
    # depths apart at all - before 2026-10-28 the depth came from the cell size, so the estimate's
    # answer moved only through the background term and the one knob that buys a large mesh was
    # invisible to it in the direction that matters.
    band = float(wetted_area_m2) * band_depth(int(levels)) / (fine * fine)
    bg = _block_m3(block_lo, block_hi) / (background ** 3)
    return int((band + bg) * ESTIMATE_MARGIN)


def plan_for_budget(*, block_lo, block_hi, narrowest_m: float, wetted_area_m2: float,
                    limit: int, asked_across: int) -> tuple[int, int, int, str]:
    """(cells across, octree depth, the estimate, what to say) - the FINEST pair that fits `limit`.

    THE DEPTH IS CHOSEN, not assumed, and the resolution is held while it is chosen: the resolution
    is what a customer asked for and the depth is ours. The old ladder stepped the resolution down
    while holding depth fixed, which on a customer cold plate collapsed draft, standard, fine and
    max to the same four cells across.

    DEPTH IS NOT FREE, and this docstring used to say it was - "depth costs nothing at the coolant
    surface and divides the background count by eight each time". The first half is wrong. The
    levels between a refined surface and the background are grading SHELLS of real cells, and on a
    part whose band dominates its background they are nearly the whole mesh: on this customer plate
    the band is 1.96M cells against 9,818 of background. Measured 2026-10-28 with the cell size
    held fixed, going from a span of 3 to a span of 5 cost 2.4x the cells at identical resolution
    (204,543,984 octree boxes against 102,152,624) and the deeper one died.

    So the depths are walked shallowest-first and the first fit is taken, which is now also the
    CHEAPEST fit rather than the dearest. Nothing about the loop changed; what changed is that it
    is no longer doing the opposite of what it intended.
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
    # NOTHING FITS, and the refusal is priced at the CHEAPEST configuration there is - the floor
    # resolution at the shallowest octree. A refusal has to state the best case, because the number
    # in it is the user's answer to "could this ever be meshed here": pricing the worst case would
    # overstate it and refuse parts by a margin that is the planner's own choice rather than the
    # part's. This line read LEVELS_LADDER[-1] until 2026-10-28, which was the cheapest option
    # while depth was believed free and is the dearest now that it is measured.
    cheapest = LEVELS_LADDER[0]
    est = band_estimate_cells(block_lo=block_lo, block_hi=block_hi, narrowest_m=narrowest_m,
                              cells_across=FLOOR_CELLS_ACROSS, wetted_area_m2=wetted_area_m2,
                              levels=cheapest)
    # ...and it is said rather than meshed anyway. The floor branch of `cells_across_for` goes
    # ahead because its estimate has come out high by three times and refusing would turn parts
    # that mesh into parts never tried. This one is different: the band model's error is bounded by
    # the same factor of three, and an estimate of 64,028,252,160 against a limit of 8,000,000 is
    # not a three-times question. Starting that run spends hours and ends in a kernel kill.
    return FLOOR_CELLS_ACROSS, cheapest, est, (
        f"this part does not fit: {FLOOR_CELLS_ACROSS} cells across its narrowest passage at "
        f"{cheapest} octree levels is about {est:,} cells against a limit of {limit:,}. Its "
        f"narrowest feature needs a mesh {est / max(limit, 1):.0f} times larger than this "
        "deployment allows")


#: How many cells across the THIN passages is worth paying for. Four is the floor at which a
#: passage holds together as a passage: measured on the first customer cold plate, 0.098 mm slots
#: under a 0.0947 mm cell got 1.03 cells across and the coolant came out in 367 disconnected
#: pieces, which checkMesh called OK and no solver could have used. Eight is where the return stops
#: being worth the fourfold cost of the next halving.
FINE_ACROSS_FLOOR = 2
FINE_ACROSS_WANTED = 4
FINE_ACROSS_CAP = 8
#: The most extra octree levels a thin surface may have below the coolant surface. Each level is
#: another set of grading shells (see DEPTH_AT_FITTED_SPAN), so this is a cost bound, not a
#: geometric one.
FINE_EXTRA_LEVELS = (1, 2, 3, 4)
#: The spans the BROAD surface may sit at, which is a different ladder from LEVELS_LADDER on
#: purpose. A two-scale part wants its background close to its broad surface, because every level
#: between them is a set of grading shells paid for over the broad surface's whole area - 38,813
#: mm2 on the customer plate. The configuration that built that part had the background at octree
#: level 10 with the coolant surface at 11 and the thin surface at 13: a broad span of ONE. That is
#: measured, where LEVELS_LADDER's shallowest is 3 and could not express it.
#:
#: LEVELS_LADDER is left alone. The single-scale path's own measurements are all at spans of 3 and
#: above, and letting it prefer a span of 1 would be choosing a regime on the strength of an
#: extrapolation to band_depth's floor.
TWO_SCALE_BROAD_LEVELS = (1, 2, 3)


def two_scale_estimate(*, block_lo, block_hi, broad_cell_m: float, broad_area_m2: float,
                       fine_cell_m: float, fine_area_m2: float, background_m: float,
                       broad_levels: int, fine_levels: int, margin: bool = True) -> int:
    """A pre-flight cell count for a part meshed at TWO cell sizes.

    The same band model as `band_estimate_cells`, applied once per surface - each at its own cell
    size and, which is the part that matters, its own octree SPAN. A thin surface four levels below
    the background carries four levels of grading shells, and those shells are most of the mesh.
    """
    if min(broad_cell_m, fine_cell_m, background_m) <= 0.0:
        raise ValueError("every cell size in a two-scale plan has to be positive")
    if fine_cell_m >= broad_cell_m:
        raise ValueError("the fine cell has to be finer than the broad one")
    band_broad = float(broad_area_m2) * band_depth(int(broad_levels)) / broad_cell_m ** 2
    band_fine = float(fine_area_m2) * band_depth(int(fine_levels)) / fine_cell_m ** 2
    bg = _block_m3(block_lo, block_hi) / background_m ** 3
    # `margin` OFF for the octree ceiling, ON for everything else. ESTIMATE_MARGIN is headroom for
    # sizing a machine, and DEPTH_AT_FITTED_SPAN is already the top of its scatter, so the two
    # together put this 1.27x to 1.49x above the meshes that have been built - which is what we
    # want when the question is "how much memory". The octree ceiling is not a thing to leave
    # headroom in: it is an exact integer boundary, and multiplying a deliberately high estimate
    # before comparing against it refuses configurations that build.
    return int((band_broad + band_fine + bg) * (ESTIMATE_MARGIN if margin else 1.0))


def plan_two_scales(*, block_lo, block_hi, scales: dict, limit: int,
                    asked_across: int) -> tuple[dict | None, str]:
    """The best two-scale plan that fits, or (None, why) when none does.

    `scales` is what cad/scales.scales_of_field measured: the split, and each side's own narrowest
    width and wetted area.

    WHAT IS BEING TRADED. The broad passages need a cell they can be resolved with and no finer -
    on the customer plate 12 cells across a 2.1 mm channel is plenty, and every cell spent beyond
    that is spent over 38,813 mm2 of area. The thin passages need as many cells across as the
    budget will buy, because below about four they stop holding together at all. So the broad side
    is held at what it needs and the remaining budget goes to the thin side, which is the opposite
    of sizing the whole part from its narrowest feature.

    THE OCTREE CEILING IS CHECKED HERE TOO, not only the cell limit, because it is the binding one
    on a part like this: 2.23 cells across those slots is about 102 million cells, and the mesher
    aborts somewhere above that whatever machine it is on. See OCTREE_BOXES_PROVEN_OK.
    """
    broad_w = float(scales["broad"]["width_m"])
    broad_a = float(scales["broad"]["area_m2"])
    fine_w = float(scales["fine"]["width_m"])
    fine_a = float(scales["fine"]["area_m2"])
    if min(broad_w, fine_w, broad_a, fine_a) <= 0.0:
        return None, "the scale split carried a width or an area that is not positive"
    ceiling = octree_ceiling_cells()
    best = None
    # the thin side first, because it is the reason to do this at all: walk its resolution down
    # from what is wanted, and under each one take the shallowest octree that fits
    for broad_across in range(int(asked_across), FLOOR_CELLS_ACROSS - 1, -1):
        for broad_levels in TWO_SCALE_BROAD_LEVELS:
            broad_cell = broad_w / broad_across
            background = broad_cell * (2 ** broad_levels)
            for extra in sorted(FINE_EXTRA_LEVELS, reverse=True):
                fine_levels = broad_levels + extra
                fine_cell = background / (2 ** fine_levels)
                across = fine_w / fine_cell
                if across < FINE_ACROSS_FLOOR or across > FINE_ACROSS_CAP:
                    continue
                shape = dict(block_lo=block_lo, block_hi=block_hi, broad_cell_m=broad_cell,
                             broad_area_m2=broad_a, fine_cell_m=fine_cell, fine_area_m2=fine_a,
                             background_m=background, broad_levels=broad_levels,
                             fine_levels=fine_levels)
                est = two_scale_estimate(**shape)
                if est > limit:
                    continue
                # the ceiling is an exact boundary, so it is compared against the unmargined count
                if two_scale_estimate(**shape, margin=False) > ceiling:
                    continue
                plan = {"background_m": background, "broad_levels": broad_levels,
                        "fine_levels": fine_levels, "broad_cell_m": broad_cell,
                        "fine_cell_m": fine_cell, "broad_across": broad_across,
                        "fine_across": across, "estimate": est}
                # the best plan is the one that resolves the THIN side furthest; ties go to the
                # cheaper estimate
                if best is None or (plan["fine_across"], -plan["estimate"]) > (
                        best["fine_across"], -best["estimate"]):
                    best = plan
            if best is not None and best["fine_across"] >= FINE_ACROSS_WANTED:
                break
        if best is not None and best["fine_across"] >= FINE_ACROSS_WANTED:
            break
    if best is None:
        return None, (
            f"this part has two scales of passage - about {broad_w * 1000:.2f} mm and "
            f"{fine_w * 1000:.4f} mm - and no pair of cell sizes resolves the thin ones at "
            f"{FINE_ACROSS_FLOOR} cells across inside a limit of {limit:,} cells and this "
            f"mesher's own ceiling of {ceiling:,}")
    said = (f"meshed at two cell sizes: {best['broad_cell_m'] * 1000:.4f} mm over the "
            f"{broad_w * 1000:.2f} mm passages ({best['broad_across']} across) and "
            f"{best['fine_cell_m'] * 1000:.5f} mm over the {fine_w * 1000:.4f} mm ones "
            f"({best['fine_across']:.2f} across), about {best['estimate']:,} cells")
    return best, said


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


__all__ = ["PEAK_BYTES_PER_CELL", "PEAK_FIXED_BYTES", "peak_gib", "written_gib", "DEPTH_AT_FITTED_SPAN", "DEPTH_PER_LEVEL", "band_depth", "span_of", "plan_two_scales", "two_scale_estimate", "FINE_ACROSS_FLOOR", "FINE_ACROSS_WANTED", "CELLS_ACROSS_BY_FIDELITY", "DEFAULT_FIDELITY", "ESTIMATE_MARGIN",
           "LEVELS_LADDER", "band_estimate_cells", "plan_for_budget",
           "FLOOR_CELLS_ACROSS", "LEVELS", "cells_across_for", "estimate_cells", "fits"]
