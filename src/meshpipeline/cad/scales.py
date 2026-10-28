# Responsibility: Say how many SIZES of passage a coolant has, and where the small ones are - so a
# mesher can spend its cells on the small ones instead of pricing the whole part at their size.
# Owns: the split between a broad scale and a fine one (read off the width distribution's own
# trough), the wetted area on each side of it, and the box that bounds the fine side.
# Boundaries: it measures and it reports. It chooses no cell size, no refinement level and no
# machine - engines/cfmesh_conjugate/budget.py prices what this returns, and the box is handed to
# cfMesh's objectRefinements by cfmesh_conjugate/conjugate.render_meshdict. It is given a radius
# field that engines/passage.passage_field_of_stls already read; it never reads geometry itself.
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

#: The widths are bucketed by OCTAVE, because what separates two scales is a FACTOR and not a
#: difference. A manifold channel is 2 mm and a microchannel slot is 0.098 mm: they are 1.9 mm
#: apart and a factor of 21 apart, and only the second number is the same for a part drawn in
#: inches. One bucket per doubling is coarse enough that a scale lands in one or two buckets and
#: fine enough to leave an empty bucket between two real scales.
OCTAVE = 2.0
#: A trough is a real gap when it holds no more than this share of the SMALLER of the two modes
#: beside it. Measured on a Corintis cold plate: the two modes hold 12,041 and 1,802 points and the
#: trough between them holds 54, a share of 0.030. A part with one scale and a long tail has no
#: bucket anywhere near this empty, which is what keeps a tail from being read as a second scale.
MAX_TROUGH_SHARE = 0.20
#: At least this many buckets must separate the two modes. Adjacent modes are one scale read
#: through a bucket edge, not two scales, and there is no room for a trough between them.
MIN_OCTAVES_APART = 2
#: The fine side's box may be at most this share of the part's own bounding volume. A "zone" that
#: is most of the part is not a local feature, and refining half a block at the fine size costs
#: nearly what refining all of it costs - at which point one cell size everywhere is the honest
#: answer and a simpler one. Measured on the customer plate: the real zone is 6.4% of the block,
#: and a deliberately bad split one octave too high gives 31.2%, so this sits well clear of both.
MAX_FINE_SHARE = 0.5
#: Fewer readable widths than this and the distribution is not a distribution. The field is read at
#: the surface's own vertices, so a part this coarsely tessellated has nothing to say about scale.
MIN_POINTS = 200


def scales_of_field(points, faces, radius, *, block_lo=None, block_hi=None) -> dict | None:
    """The coolant's passage widths split into a broad scale and a fine one, or None for one scale.

    `points`, `faces`, `radius` are what engines/passage.passage_field_of_stls returns: the wall's
    own vertices, its triangles, and the inscribed radius at each vertex. The width at a vertex is
    twice its radius.

    WHY THIS IS WORTH A MODULE. The mesher sizes every cell from ONE number, the narrowest passage,
    and prices the refinement band over the WHOLE wetted area at that size. On a part with two
    scales that is the wrong bill by orders of magnitude. A Corintis cold plate is a manifold of
    2-8 mm channels bonded to a layer of 0.098 mm slots: the slots are 35.9% of the wetted area and
    6.4% of the volume, and banding all 60,526 mm2 at a slot-sized cell estimates about three
    billion cells. Banding the manifold at a manifold-sized cell and only the slots at a slot-sized
    one reaches 3.26 cells across the slots inside 700 million.

    THE MEAN IS USELESS HERE and that is why this was not caught by eye: 4V/A over that whole
    coolant is 3.5 mm, because the thin layer is 2% of the volume spread over a third of the area.
    The distribution has to be looked at, not summarised.

    Returns, in METRES and SQUARE METRES:

        {"split_m":      where the two scales part company,
         "trough_share": how empty the gap is (0.0 is perfect separation),
         "fine":   {"width_m", "area_m2", "lo", "hi", "share_of_block"},
         "broad":  {"width_m", "area_m2"}}

    `width_m` is the 1st percentile on the fine side and the 5th on the broad side - each scale's
    OWN narrowest, not its middle, because a cell size has to suit the smallest thing it must hold
    together. The percentile rather than the minimum because a single tessellation spike is not a
    feature. `lo`/`hi` bound the fine side's triangles exactly; the caller pads the box, which it
    must, because a band needs room on both sides of the surface and only the caller knows the
    cell size that sets how much.

    None - with the reason logged at debug - when the part has one scale, which is the ordinary
    case and not a failure. A caller that gets None sizes the part the way it always has.
    """
    P = np.asarray(points, dtype=float)
    F = np.asarray(faces, dtype=int)
    R = np.asarray(radius, dtype=float)
    if P.ndim != 2 or P.shape[1] != 3 or F.ndim != 2 or F.shape[1] != 3 or len(R) != len(P):
        logger.debug("scales: the field is not (points, triangles, a radius per point)")
        return None
    width = np.where(R > 0.0, 2.0 * R, np.inf)
    w = width[np.isfinite(width)]
    if len(w) < MIN_POINTS or w.min() <= 0.0 or w.max() <= w.min():
        logger.debug("scales: %d readable widths, which is not a distribution", len(w))
        return None

    # ---- the two modes, and the emptiest bucket between them ----
    lo = float(np.floor(np.log2(w.min())))
    hi = float(np.ceil(np.log2(w.max())))
    edges = OCTAVE ** np.arange(lo, hi + 1.0)
    if len(edges) < MIN_OCTAVES_APART + 2:
        logger.debug("scales: the widths span %.1f octaves, too few to hold two scales",
                     hi - lo)
        return None
    count, _ = np.histogram(w, bins=edges)
    order = np.argsort(count)[::-1]
    a, b = int(min(order[0], order[1])), int(max(order[0], order[1]))
    if b - a < MIN_OCTAVES_APART:
        logger.debug("scales: the two fullest octaves are %d apart, so this is one scale", b - a)
        return None
    between = count[a + 1:b]
    t = a + 1 + int(np.argmin(between))
    share = float(between.min()) / max(float(min(count[a], count[b])), 1.0)
    if share > MAX_TROUGH_SHARE:
        logger.debug("scales: the gap between the two fullest octaves holds %.1f%% of the smaller "
                     "one, which is a slope and not a gap", 100.0 * share)
        return None
    split = float(edges[t + 1])

    # ---- AREA, not points, because area is what a refinement band is priced on ----
    tri = P[F]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    # A triangle belongs to the fine side when ALL THREE of its vertices do. Any-vertex would drag
    # every triangle that merely touches the thin layer into it, which on a bonded plate is the
    # whole seam; all-three keeps the boundary triangles with the broad side, where the coarser
    # cell is the safe one to get wrong.
    fine_tri = (width[F] <= split).all(axis=1)
    if not fine_tri.any() or fine_tri.all():
        logger.debug("scales: the split at %.6g m puts every triangle on one side", split)
        return None
    fine_area = float(area[fine_tri].sum())
    broad_area = float(area[~fine_tri].sum())
    fw = w[w <= split]
    bw = w[w > split]
    if len(fw) == 0 or len(bw) == 0:
        return None

    pts = tri[fine_tri].reshape(-1, 3)
    zlo, zhi = pts.min(axis=0), pts.max(axis=0)
    span = zhi - zlo
    if float(np.min(span)) <= 0.0:
        # a fine side that is flat in one axis has no volume to refine, and cfMesh's box would be
        # degenerate. It happens on a single-plane seam; the part is better sized the old way.
        logger.debug("scales: the fine side is flat in one axis, so it cannot be a refinement box")
        return None
    if block_lo is not None and block_hi is not None:
        blk = float(np.prod([max(0.0, float(block_hi[k]) - float(block_lo[k])) for k in range(3)]))
    else:
        blk = float(np.prod(P.max(axis=0) - P.min(axis=0)))
    fine_share = float(np.prod(span) / blk) if blk > 0.0 else 1.0
    if fine_share > MAX_FINE_SHARE:
        logger.debug("scales: the fine side's box is %.1f%% of the part, which is not a local "
                     "feature", 100.0 * fine_share)
        return None

    out = {
        "split_m": round(split, 12),
        "trough_share": round(share, 6),
        "fine": {"width_m": round(float(np.percentile(fw, 1)), 12),
                 "area_m2": round(fine_area, 15),
                 "lo": [round(float(v), 12) for v in zlo],
                 "hi": [round(float(v), 12) for v in zhi],
                 "share_of_block": round(fine_share, 9)},
        "broad": {"width_m": round(float(np.percentile(bw, 5)), 12),
                  "area_m2": round(broad_area, 15)},
    }
    logger.info("scales: two scales, split at %.4f mm (the gap holds %.1f%% of the smaller mode); "
                "fine %.4f mm over %.1f mm2 in %.1f%% of the part, broad %.4f mm over %.1f mm2",
                split * 1000.0, 100.0 * share, out["fine"]["width_m"] * 1000.0,
                fine_area * 1e6, 100.0 * fine_share, out["broad"]["width_m"] * 1000.0,
                broad_area * 1e6)
    return out


def write_fine_surface(points, faces, radius, *, split_m: float, out_path) -> dict | None:
    """Write the thin part of the coolant's wall as its own STL, for a finer refinement band.

    The same all-three-vertices rule `scales_of_field` uses, so the file holds exactly the
    triangles whose area it reported on the fine side. The surface is handed to cfMesh as a second
    `surfaceMeshRefinement` entry, which refines AROUND it - a band - rather than filling a box.

    WHY NOT A BOX. Measured on the first customer cold plate, 2026-10-28: the thin layer is 35.9%
    of the wetted area and about 2% of the volume, so a box bounding it is almost entirely metal.
    `objectRefinements type box` fills that box's volume at the fine size and priced out at
    456,206,329 cells, against 224,431,537 for a band round the same surface. The box run was not
    merely expensive, it was fatal: 478,648,313 octree boxes at 8 vertices each is 3,829,186,504,
    past the int32 label this OpenFOAM is built with, and cartesianMesh aborted with "Negative
    size requested" after 277 seconds on a machine that was using 50 GB of 251.

    Returns {"file", "triangles", "area_m2"} or None when there is nothing to write.
    """
    from pathlib import Path

    from meshpipeline.cad.stl_io import write_stl_binary

    P = np.asarray(points, dtype=float)
    F = np.asarray(faces, dtype=int)
    R = np.asarray(radius, dtype=float)
    width = np.where(R > 0.0, 2.0 * R, np.inf)
    keep = (width[F] <= float(split_m)).all(axis=1)
    if not keep.any():
        logger.debug("scales: no triangle is thinner than %.6g m", split_m)
        return None
    tri = P[F][keep]
    area = float(0.5 * np.linalg.norm(
        np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1).sum())
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(out, [tuple(tuple(float(v) for v in p) for p in t) for t in tri])
    logger.info("scales: the thin walls written as %d triangles over %.1f mm2 to %s",
                int(keep.sum()), area * 1e6, out.name)
    return {"file": str(out), "triangles": int(keep.sum()), "area_m2": round(area, 15)}


__all__ = ["scales_of_field", "write_fine_surface", "MAX_FINE_SHARE", "MAX_TROUGH_SHARE",
           "MIN_OCTAVES_APART"]
