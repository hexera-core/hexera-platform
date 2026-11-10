# Responsibility: Draw a delivered mesh boundary EXACTLY, by merging coplanar axis-aligned faces
# into the larger rectangles they already tile, so the whole part fits a browser without the drawn
# surface being an approximation of the mesh.
# Boundaries: it reads one region's boundaryIndex sidecar and nothing else. It judges no mesh,
# changes no file, and reports no figure it did not measure.
# Collaborates with: render/boundary_index.py (the arrays), render/index_surface.py (the same patch
# dicts, by the lossy route), render/boundary_quality.py (the per-face numbers a merged rectangle
# has to summarise honestly).
"""The drawable boundary of one region, merged without loss.

WHY. The delivered plate is drawn through `polymesh_budget.fit_to_budget`, which clusters faces
until the surface fits a triangle budget. On the first customer cold plate that lands the coolant
at 137.1 um, and the drawn surface is then an APPROXIMATION of the mesh rather than the mesh.

HOW WRONG IS IT, MEASURED - because the first version of this docstring guessed and the guess was
far too dramatic. It said the channel array "smears into a slab and no zoom recovers it", which
sounds right from the 137.1 um against a 100.631 um channel and is not what happens. Computing
both surfaces' area from their own buffers:

    patch                     budget route        exact route    budget / exact
    fluid/fluid_to_solid    0.062904137023    0.063763565471           98.65%
    solid/solid_to_air      0.045947075674     0.046764597353          98.25%
    chip/chip_to_air        0.015085800556     0.015117668556          99.79%
    TOTAL                   0.126763701813     0.128495269088          98.65%

So the budget route loses 1.35% of the coolant's area, not the channels. Screenshots of the two at
an IDENTICAL camera differ in 0.59% to 0.76% of canvas pixels. It is a good approximation that is
slightly wrong everywhere, not a picture with the features missing.

WHAT IS WORTH HAVING ANYWAY, all measured on the delivered plate:
  * the drawn surface IS the mesh - area error 1.0e-12 against the budget route's 1.35%
  * 2,635,540 triangles against 5,487,927, so 2.08x fewer
  * 76.2 MB against 152.6 MB
  * 5.0 s to load in the same browser against 18.7 s, and about 3x the frame rate while zooming

AND ONE HONESTY DEFECT IT EXPOSED IN THE OLD PAYLOAD. `area_m2` on a budget-reduced patch is the
DELIVERED area, not the area the patch draws - fluid/fluid_to_solid states 0.063763565415 and
draws 0.062904137023. A payload that states an area it does not have on screen is the kind of
figure this codebase keeps saying should be read out of the thing it describes.

WHAT THIS DOES INSTEAD. cfMesh builds this part on a ~25.1578 um lattice, so its walls are vast
fields of coplanar, axis-aligned quads. Coplanar quads that tile a rectangle ARE that rectangle:
replacing them changes the geometry by nothing at all. Measured on the delivered plate:

    region   delivered faces   exact polygons   collapse
    fluid         16,622,416          828,003     20.1 : 1
    solid         18,104,116        1,646,894     11.0 : 1
    chip           1,763,676              715   2466.7 : 1
    whole part    36,490,208        2,475,612     14.7 : 1

4,951,224 triangles for the whole part against the 5,487,927 the LOSSY payload already ships, and
1,656,006 for the coolant against its 4,445,857. Fewer triangles and no merge error, both at once,
which is why this is not a trade.

NO TOLERANCES, ANYWHERE, IN THE ADJACENCY. Two faces that share an edge carry bit-identical
coordinates for it, so exact equality is the CORRECT adjacency test and is cheaper than a
tolerance. Measured on the coolant: 15,401,694 adjacent pairs identical, and not one near-miss
anywhere within a micron.

Note what that argument does NOT rest on. An earlier version of this comment said two such faces
share a POINT ID, which is false here - this index is the union of eight processor directories and
holds up to 37,896 duplicate coordinates, so the same corner can carry several ids. Comparing
coordinates is what makes the test correct; comparing ids would have split real neighbours.

That is not a stylistic preference, it is the bug this was written around. The first version
grouped rows by a tolerance of 1e-10 m while sorting on the exact value. Two faces 44.9 mm apart in
u, whose v0 differed by 1e-11 m, landed next to each other in the sort, passed the tolerance row
test, and the gap test - `u0[i] - u1[i-1] > eps` - saw a NEGATIVE gap, so it merged them. The
rectangle it emitted covered 111x the area of the two faces it replaced. A tolerance grouping
combined with an exact sort is incoherent: the sort decides who gets compared, so it has to be
ordered by the same thing the comparison tests.

WHAT IT REFUSES TO MERGE, and why each refusal is the honest answer rather than a limitation:

  * a face whose normal is not along an axis (0.26% of the coolant). A real sloped face.
  * a face that is not a rectangle (0.25% of the coolant is off its bounding box by more than
    10%). A real notch, L or T.
  * a face that is ALMOST a rectangle. Measured on the coolant, loosening `RECT_TOL` from float
    noise to 1e-4 would merge 279,000 more faces and save 18% of the polygons - and would move
    geometry by up to 3.2 um, which is 13% of a cell. The whole point of this file is that the
    surface is the mesh, so that trade is not taken. `RECT_TOL` is a float-rounding guard and
    nothing else: an L-shaped face misses it by tens of percent.
  * two faces on planes 700 nm apart. This mesh really has those. Flattening them would put
    geometry on screen that the mesher never wrote.

WHAT A MERGED RECTANGLE CANNOT CARRY is one face's quality, because it stands for up to about 160
of them. It carries the WORST and the AREA-WEIGHTED MEAN over exactly the faces it replaced, plus
how many there were, and `basis` travels with them as it does everywhere else. A reader shown the
worst is shown an upper bound they can act on; a reader shown one face's value as if it were the
rectangle's is being told something no one measured. The faces kept whole carry their own value,
so about 8% of the part is exact per face and the rest is bounded.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The corner count under which a face is not drawn, matching `index_surface.MIN_CORNERS`: the
#: index keeps degenerate faces in its NUMBERING on purpose, so the filter has to happen here or
#: `face_count` means two different things on the two routes.
MIN_CORNERS = 3

#: How close a face's normal must be to an axis to be a merge candidate. At 0.9999 a face may be
#: 0.81 degrees off, which over a 201 um face is 2.8 nm of deviation - below the 7.5 nm that float32
#: can even express at this part's coordinates, so the threshold is loose only in appearance.
AXIAL_DOT = 0.9999

#: How far a face's own area may sit from its bounding box's and still count as a rectangle, as a
#: fraction of that box. THIS IS A FLOAT-ROUNDING GUARD, NOT A SHAPE TOLERANCE, and it is only HALF
#: the rectangle test - see FLATNESS below, without which it is not a shape test at all. float64
#: arithmetic over float32 coordinates lands within about 1e-16 relative; an L-shaped face misses
#: this by tens of percent.
RECT_TOL = 1e-9

#: How far a face's corners may stray from its plane, ALONG ITS OWN NORMAL, IN METRES. Zero: a face
#: that is not exactly flat is kept whole.
#:
#: THE AREA TEST ALONE IS BLIND TO TWIST, and this is the only reason this constant exists. Take a
#: quad whose four corners project onto the four corners of its bounding rectangle and lift two
#: DIAGONALLY OPPOSITE corners by h. Newell's two off-axis components are
#:     A_u = (W/2)(h0 - h1 - h2 + h3)      A_v = (L/2)(h0 + h1 - h2 - h3)
#: and both vanish whenever h0 == h2 and h1 == h3. So for a saddle |Newell| IS the bounding box and
#: |n[axis]| is exactly 1, for ANY h. Swept on a 201.2998 um square from h = 1 pm to 1 cm: the
#: relative area error is 0.000e+00 at every amplitude, while at h = 1 cm the triangles a renderer
#: actually draws cover 70x the rectangle. No tolerance on an area can bound a shape this way.
#:
#: This is not hypothetical on the delivered plate. 99,295 faces of 36,490,208 pass the area test
#: without lying in a plane (1,991 fluid, 97,177 solid, 127 chip), carrying 2.37% of the part's
#: area; drawn flat at `pc`, their high corners move by up to 1,247.97 nm, which is 4.96% of a
#: 25.1578 um lattice cell. The area book could not report it either: it sums |Newell| on both
#: sides, so the twist cancels and the residual reads as float noise.
#:
#: The guard costs 3.60% more polygons - 2,475,612 to 2,564,696 over the whole boundary - and buys
#: the claim this module is named for. With it, no drawn vertex moves at all.
FLATNESS_M = 0.0

#: How many faces' corners are gathered at once. The (faces, corners, 3) float64 gather is the only
#: array here that is a multiple of the connectivity: at 16.6 M quads it is 1.6 GB in one piece.
GEOMETRY_BLOCK_FACES = 2_000_000


def _ragged_arange(starts, lengths):
    """concatenate([arange(s, s + n) for s, n in zip(starts, lengths)]), without the loop.

    The same form `index_surface` uses and for the same reason: the per-face Python comprehension
    it replaces costs 2.4 us a face, which at 16.6 M faces is the whole runtime.
    """
    import numpy as np

    s = np.asarray(starts, dtype=np.int64)
    n = np.asarray(lengths, dtype=np.int64)
    total = int(n.sum())
    if total == 0:
        return np.empty(0, dtype=np.int64)
    group = np.concatenate(([0], np.cumsum(n)[:-1]))
    return np.repeat(s - group, n) + np.arange(total, dtype=np.int64)


def face_geometry(index, *, block: int = GEOMETRY_BLOCK_FACES):
    """Per face: the axis its normal lies along, that normal's sign, the plane it sits in, its
    extent in the plane, and its true area.

    Returns (axis, sign, plane, ext, area, is_rect, thick, fan) over the index's own face
    numbering, where `thick` is the spread of the face's corners along its own normal and `fan` is
    the area a renderer covers when it fans the polygon - the two numbers that make the rectangle
    test a shape test rather than a scalar coincidence.
    `axis` is -1 for a face whose normal is not along an axis and for a degenerate one. `plane`,
    `ext` are float32 - they are min/max over float32 corner coordinates, so float32 holds them
    exactly, and exactness is the entire basis of the adjacency test downstream. `area` is float64
    because it gets summed 16 million times.

    THE AREA IS NEWELL'S, over every corner, which is exact for a planar n-gon and is both the
    normal and twice the area in one pass - so there is no triangulation choice here to get wrong,
    and a 5-corner face with a hanging node is measured as the rectangle it is. A face shorter than
    its block's widest repeats its last corner, which adds a degenerate edge: that contributes a
    zero cross product, so it moves neither the normal nor the area.
    """
    import numpy as np

    pts = index.points
    sizes = index.sizes
    conn = index.conn
    offs = index.offs
    n = int(index.n_faces)

    axis = np.empty(n, dtype=np.int8)
    sign = np.empty(n, dtype=np.int8)
    plane = np.empty(n, dtype=np.float32)
    ext = np.empty((n, 4), dtype=np.float32)
    area = np.empty(n, dtype=np.float64)
    #: how far this face's corners spread along its own normal. The shape test, as opposed to the
    #: area test, and the only thing that sees a saddle - see FLATNESS_M.
    thick = np.empty(n, dtype=np.float64)
    #: the area a renderer actually covers, fanning the polygon from its first corner and taking
    #: each triangle's own magnitude. For a planar face this IS |Newell|; for a warped one it is
    #: larger, which is exactly the difference the area book cannot see.
    fan = np.empty(n, dtype=np.float64)

    for s in range(0, n, block):
        e = min(s + block, n)
        o = offs[s:e]
        sz = np.asarray(sizes[s:e], dtype=np.int64)
        rows = np.arange(e - s)
        widest = int(sz.max()) if sz.size else 0
        P = np.empty((e - s, max(widest, 1), 3), dtype=np.float64)
        for c in range(widest):
            P[:, c, :] = pts[conn[o + np.minimum(sz - 1, c)].astype(np.int64)].astype(np.float64)
        av = 0.5 * np.cross(P, np.roll(P, -1, axis=1)).sum(axis=1)
        mag = np.linalg.norm(av, axis=1)
        area[s:e] = mag
        # the fan a renderer draws: each triangle's OWN magnitude, summed. Taking the magnitude per
        # triangle rather than of the sum is the whole point - it is what stops a twist cancelling.
        tri = np.cross(P[:, 1:-1] - P[:, :1], P[:, 2:] - P[:, :1])
        fan[s:e] = 0.5 * np.linalg.norm(tri, axis=2).sum(axis=1)
        unit = av / np.where(mag > 0.0, mag, 1.0)[:, None]
        k = np.argmax(np.abs(unit), axis=1)
        axial = (np.abs(unit[rows, k]) >= AXIAL_DOT) & (mag > 0.0) & (sz >= MIN_CORNERS)
        axis[s:e] = np.where(axial, k, -1).astype(np.int8)
        sign[s:e] = np.where(unit[rows, k] >= 0.0, 1, -1).astype(np.int8)
        lo = P.min(axis=1)
        hi = P.max(axis=1)
        thick[s:e] = hi[rows, k] - lo[rows, k]
        # the plane is the face's OWN coordinate along its normal, not a bucket: for a face that
        # really lies in that plane lo == hi there, and the value is shared bit-for-bit with every
        # neighbour that shares a vertex with it
        plane[s:e] = lo[rows, k].astype(np.float32)
        ua = (k + 1) % 3
        va = (k + 2) % 3
        ext[s:e, 0] = lo[rows, ua].astype(np.float32)
        ext[s:e, 1] = hi[rows, ua].astype(np.float32)
        ext[s:e, 2] = lo[rows, va].astype(np.float32)
        ext[s:e, 3] = hi[rows, va].astype(np.float32)

    box = ((ext[:, 1].astype(np.float64) - ext[:, 0]) *
           (ext[:, 3].astype(np.float64) - ext[:, 2]))
    # TWO TESTS, NOT ONE, and the order matters for what each is allowed to conclude. `thick`
    # settles the SHAPE - whether the face lies in a plane at all - and only then does comparing
    # its area with its bounding box mean it IS that rectangle. The area test alone cannot see a
    # saddle, however tight the tolerance; see FLATNESS_M.
    is_rect = ((axis >= 0) & (thick <= FLATNESS_M)
               & (np.abs(area - box) <= RECT_TOL * np.where(box > 0.0, box, 1.0)))
    return axis, sign, plane, ext, area, is_rect, thick, fan


def merge_rectangles(u0, u1, v0, v1):
    """Merge one plane's worth of same-patch, same-plane, same-facing rectangles.

    Returns (rect, rect_of_face): `rect` is (m, 4) of u0, u1, v0, v1, and `rect_of_face` says which
    rectangle each INPUT face ended up in, in the input's own order, so a caller can gather what
    each rectangle stands for.

    Step 1 runs along u. The faces are sorted by (v0, v1, u0), so inside one row u is monotonic and
    the only adjacency question left is whether this face's u0 IS the previous face's u1. Sorting
    on exactly the values the break test compares is the correction described in the module
    docstring; the previous version sorted on v0 alone and tested v0 within a tolerance, so a row
    came out ordered by float noise and a gap could read as negative.

    Step 2 stacks those runs. Runs are sorted by (u0, u1, v0) and a run joins the one before it when
    their u span is identical and this run's v0 IS that run's v1.

    BOTH STEPS ARE CONSERVATIVE WHERE THEY ARE UNSURE. A break is taken on any inequality, so a
    shared edge that somehow failed to be bit-identical costs an extra rectangle and never a merge
    across a gap. That is the direction the error has to fall in: an extra rectangle is a slower
    draw, and a merge across a gap is geometry on screen that the mesh does not have.
    """
    import numpy as np

    n = len(u0)
    if n == 0:
        return np.empty((0, 4), dtype=np.float32), np.empty(0, dtype=np.int64)

    order = np.lexsort((u0, v1, v0))
    a0, a1, b0, b1 = u0[order], u1[order], v0[order], v1[order]
    new = np.empty(n, dtype=bool)
    new[0] = True
    if n > 1:
        new[1:] = (b0[1:] != b0[:-1]) | (b1[1:] != b1[:-1]) | (a0[1:] != a1[:-1])
    run_of_sorted = np.cumsum(new) - 1
    n_runs = int(run_of_sorted[-1]) + 1
    # sorted and contiguous, so a run's span is its first u0 and its last u1 - no min/max, and no
    # chance of a reduction reaching across a run boundary
    first = np.flatnonzero(new)
    last = np.append(first[1:] - 1, n - 1)
    ru0, ru1 = a0[first], a1[last]
    rv0, rv1 = b0[first], b1[first]

    order2 = np.lexsort((rv0, ru1, ru0))
    c0, c1, d0, d1 = ru0[order2], ru1[order2], rv0[order2], rv1[order2]
    new2 = np.empty(n_runs, dtype=bool)
    new2[0] = True
    if n_runs > 1:
        new2[1:] = (c0[1:] != c0[:-1]) | (c1[1:] != c1[:-1]) | (d0[1:] != d1[:-1])
    col_of_sorted_run = np.cumsum(new2) - 1
    n_cols = int(col_of_sorted_run[-1]) + 1
    f2 = np.flatnonzero(new2)
    l2 = np.append(f2[1:] - 1, n_runs - 1)
    rect = np.stack([c0[f2], c1[f2], d0[f2], d1[l2]], axis=1).astype(np.float32)

    col_of_run = np.empty(n_runs, dtype=np.int64)
    col_of_run[order2] = col_of_sorted_run
    rect_of_face = np.empty(n, dtype=np.int64)
    rect_of_face[order] = col_of_run[run_of_sorted]
    assert n_cols == len(rect)
    return rect, rect_of_face


def _summarise(values, rect_of_face, n_rects, weights):
    """The worst and the area-weighted mean of `values` over the faces behind each rectangle.

    `ufunc.at` is the obvious way and it is also the slow one - at 16 M faces it dominates the whole
    build - so the maximum goes through a sort and `reduceat`, which is one O(n log n) pass for all
    the metrics of one patch.
    """
    import numpy as np

    order = np.argsort(rect_of_face, kind="stable")
    counts = np.bincount(rect_of_face, minlength=n_rects)
    starts = np.concatenate(([0], np.cumsum(counts)))
    worst = np.full(n_rects, np.nan, dtype=np.float64)
    nonempty = np.flatnonzero(counts > 0)
    if nonempty.size:
        worst[nonempty] = np.maximum.reduceat(values[order], starts[nonempty])
    wsum = np.bincount(rect_of_face, weights=values * weights, minlength=n_rects)
    asum = np.bincount(rect_of_face, weights=weights, minlength=n_rects)
    mean = np.where(asum > 0, wsum / np.where(asum > 0, asum, 1.0), np.nan)
    return worst, mean, counts


def exact_surface(polymesh, *, region: str = "", skip_names: tuple[str, ...] = (),
                  with_quality: bool = True) -> tuple[list[dict], dict]:
    """One region's boundary, drawn exactly, as the patch dicts the viewer payload is built from.

    Returns (patches, provenance). Each patch carries `name`, `type`, `face_count` (THE MESH'S OWN
    delivered count, unchanged - the drawn count is `poly_count`), `points` and `polys` as raw bytes
    in `index_surface`'s format, `area_m2`, and `exact` with this patch's own accounting. When a
    quality sidecar is there, `quality` carries per drawn polygon `worst`, `mean`, `faces` and the
    `basis` each metric is measured on.

    `region` prefixes the patch names; a caller that prefixes them itself must leave it unset.

    Raises `boundary_index.IndexUnavailable` when there is no usable index here, which is the signal
    `detail_window` and `index_surface` already fall back on.
    """
    import numpy as np

    from meshpipeline.render.boundary_index import open_index
    from meshpipeline.render.boundary_quality import QualityUnavailable, open_quality

    index = open_index(polymesh)
    axis, sign, plane, ext, area, is_rect, thick, fan = face_geometry(index)
    patch_of = np.asarray(index.patch_of)
    sizes = index.sizes
    conn = index.conn
    pts = index.points
    offs = index.offs

    qual = None
    if with_quality:
        try:
            qual = open_quality(polymesh)
        except QualityUnavailable as exc:
            logger.debug("exact_surface: no per-face quality at %s (%s)", polymesh, exc)

    out: list[dict] = []
    totals = {"delivered_faces": 0, "drawn_polygons": 0, "merged": 0, "kept": 0,
              "area_delivered_m2": 0.0, "area_drawn_m2": 0.0, "patches": {}}
    #: the areas of the DELIVERED faces, kept so `cell_stats` describes the mesh's cells and not
    #: the merged polygons. A rectangle that stands for 544,272 faces has no cell size, and an
    #: odometer that took its size from one would tell a reader a cell is millimetres across.
    delivered_areas: list = []

    for pid, p in enumerate(index.patches):
        name = str(p["name"])
        if name in skip_names:
            continue
        f0, n = int(p["first_face"]), int(p["n_faces"])
        if n <= 0:
            continue
        ids = np.arange(f0, f0 + n, dtype=np.int64)
        # the patch table and `patch_of` must agree or a face is about to be drawn under another
        # patch's name, which is the one mistake this must not make
        if not np.all(patch_of[ids] == pid):
            raise ValueError(
                f"{polymesh}: patch {name!r} spans faces {f0}..{f0 + n} but patch_of disagrees")
        drawable = ids[np.asarray(sizes[ids], dtype=np.int64) >= MIN_CORNERS]
        if not drawable.size:
            continue

        rect_ids = drawable[is_rect[drawable]]
        kept_ids = drawable[~is_rect[drawable]]

        # ---- merge, one (axis, facing, plane) group at a time --------------------------------
        rects: list = []
        rect_axis: list = []
        rect_sign: list = []
        rect_plane: list = []
        rect_of_face = np.empty(rect_ids.size, dtype=np.int64)
        made = 0
        if rect_ids.size:
            go = np.lexsort((plane[rect_ids], sign[rect_ids].astype(np.int64),
                             axis[rect_ids].astype(np.int64)))
            g = rect_ids[go]
            ga, gs, gp = axis[g], sign[g], plane[g]
            brk = np.flatnonzero(np.concatenate((
                [True],
                (ga[1:] != ga[:-1]) | (gs[1:] != gs[:-1]) | (gp[1:] != gp[:-1]),
                [True])))
            for b in range(len(brk) - 1):
                lo, hi = int(brk[b]), int(brk[b + 1])
                sel = g[lo:hi]
                r, rof = merge_rectangles(ext[sel, 0], ext[sel, 1], ext[sel, 2], ext[sel, 3])
                rects.append(r)
                rect_axis.append(np.full(len(r), ga[lo], dtype=np.int8))
                rect_sign.append(np.full(len(r), gs[lo], dtype=np.int8))
                rect_plane.append(np.full(len(r), gp[lo], dtype=np.float32))
                rect_of_face[go[lo:hi]] = rof + made
                made += len(r)
        rect = np.concatenate(rects) if rects else np.empty((0, 4), dtype=np.float32)
        r_axis = np.concatenate(rect_axis) if rect_axis else np.empty(0, dtype=np.int8)
        r_sign = np.concatenate(rect_sign) if rect_sign else np.empty(0, dtype=np.int8)
        r_plane = np.concatenate(rect_plane) if rect_plane else np.empty(0, dtype=np.float32)

        # ---- the rectangles' corners, wound so they face the way their faces did -------------
        # for axis k the in-plane axes are u = k+1 and v = k+2 cyclically, and u x v = +k, so the
        # order below gives a +k normal. A group whose faces face -k is wound the other way round;
        # drawing it +k would light the inside of the part and leave the outside black.
        corner_u = np.stack([rect[:, 0], rect[:, 1], rect[:, 1], rect[:, 0]], axis=1)
        corner_v = np.stack([rect[:, 2], rect[:, 2], rect[:, 3], rect[:, 3]], axis=1)
        flip = r_sign < 0
        if flip.any():
            corner_u[flip] = corner_u[flip][:, ::-1]
            corner_v[flip] = corner_v[flip][:, ::-1]
        rc = np.empty((len(rect), 4, 3), dtype=np.float32)
        ka = r_axis.astype(np.int64)
        rows = np.arange(len(rect))
        rc[rows[:, None], np.arange(4)[None, :], ka[:, None]] = r_plane[:, None]
        rc[rows[:, None], np.arange(4)[None, :], ((ka + 1) % 3)[:, None]] = corner_u
        rc[rows[:, None], np.arange(4)[None, :], ((ka + 2) % 3)[:, None]] = corner_v

        # ---- the kept faces' corners, straight out of the index -----------------------------
        ksz = np.asarray(sizes[kept_ids], dtype=np.int64)
        ktake = _ragged_arange(offs[kept_ids], ksz)
        kc = np.asarray(pts[conn[ktake].astype(np.int64)], dtype=np.float32)

        # ---- one point array for both, deduplicated bit for bit -----------------------------
        # a void view is the fast exact dedup: a shared vertex is the SAME float32 triple in both
        # halves, because the rectangles' corners are min/max over float32 values and so are
        # float32-representable without rounding.
        allc = np.concatenate([rc.reshape(-1, 3), kc]) if len(rect) else kc
        allc = np.ascontiguousarray(allc)
        view = allc.view([("v", allc.dtype, 3)]).ravel()
        uniq, inv = np.unique(view, return_inverse=True)
        points = uniq.view(np.float32).reshape(-1, 3)
        inv = inv.astype(np.uint32, copy=False)

        n_poly = len(rect) + kept_ids.size
        poly_sizes = np.concatenate([np.full(len(rect), 4, dtype=np.int64), ksz])
        polys = np.empty(int(poly_sizes.sum()) + n_poly, dtype=np.uint32)
        starts = np.concatenate(([0], np.cumsum(poly_sizes + 1)[:-1]))
        polys[starts] = poly_sizes
        mask = np.ones(polys.size, dtype=bool)
        mask[starts] = False
        polys[mask] = inv

        # ---- the accounting, measured and not asserted ---------------------------------------
        # AGAINST THE FAN, NOT AGAINST |Newell|. The first version compared sum|Newell| on both
        # sides, and |Newell| of a twisted quad is its bounding box exactly - so the warp appeared
        # on both sides of the subtraction, cancelled, and the residual read as float noise. The
        # area a renderer covers is the fan, so that is the book. `max_vertex_shift_m` is the
        # figure no area book can hide: how far the merge actually moved a corner.
        area_in = float(fan[drawable].sum())
        rect_area = float(((rect[:, 1].astype(np.float64) - rect[:, 0]) *
                           (rect[:, 3].astype(np.float64) - rect[:, 2])).sum())
        area_out = rect_area + float(fan[kept_ids].sum())
        merged_in = float(fan[rect_ids].sum())
        err = abs(rect_area - merged_in) / merged_in if merged_in > 0 else 0.0
        shift = float(thick[rect_ids].max()) if rect_ids.size else 0.0

        row = {
            "name": f"{region}/{name}" if region else name,
            "type": str(p.get("type") or ""),
            # `face_count` IS THE MESH'S OWN. `poly_count` is what gets drawn, and conflating the
            # two is how a reader comes to believe a 36 M-face part is a 2 M-face part.
            "face_count": int(drawable.size),
            "poly_count": int(n_poly),
            "points": points.tobytes(),
            "polys": polys.tobytes(),
            "area_m2": area_out,
            "exact": {"delivered_faces": int(drawable.size), "drawn_polygons": int(n_poly),
                      "merged_faces": int(rect_ids.size), "merged_polygons": int(len(rect)),
                      "kept_faces": int(kept_ids.size),
                      "collapse": drawable.size / n_poly if n_poly else 0.0,
                      "area_delivered_m2": area_in, "area_drawn_m2": area_out,
                      "area_error": err, "max_vertex_shift_m": shift},
        }

        if qual is not None:
            # RAW BYTES, float32, one value per DRAWN POLYGON in `polys` order - not JSON numbers.
            # At 2.47 M polygons over three metrics a JSON list is about 300 MB of decimal text for
            # 30 MB of data, and the client already decodes the points and the polys this way.
            vals = {}
            faces_per = None
            for metric in qual.metrics:
                got_r = qual.values(metric, rect_ids) if rect_ids.size else None
                got_k = qual.values(metric, kept_ids) if kept_ids.size else None
                if got_r is None and got_k is None:
                    continue
                if got_r is not None and len(rect):
                    w = np.maximum(area[rect_ids], 0.0)
                    worst, mean, cnt = _summarise(np.asarray(got_r, dtype=np.float64),
                                                  rect_of_face, len(rect), w)
                else:
                    worst = mean = np.empty(0, dtype=np.float64)
                    cnt = np.zeros(len(rect), dtype=np.int64)
                kv = (np.asarray(got_k, dtype=np.float64) if got_k is not None
                      else np.full(kept_ids.size, np.nan))
                vals[metric] = {
                    "worst": np.concatenate([worst, kv]).astype(np.float32).tobytes(),
                    "mean": np.concatenate([mean, kv]).astype(np.float32).tobytes(),
                }
                if faces_per is None:
                    faces_per = np.concatenate(
                        [cnt, np.ones(kept_ids.size, dtype=np.int64)]).astype(np.uint32)
            if vals:
                # THE BASIS TRAVELS WITH THE VALUES, as it does in `detail_window`: a boundary
                # face's skewness is allowed 20 where an internal face gets 4, so anything that
                # draws all three against one bar paints faces red that checkMesh passed.
                row["faces_per_polygon"] = faces_per.tobytes()
                row["quality"] = {"basis": {m: qual.basis(m) for m in vals},
                                  "values": vals,
                                  "over": "a merged polygon carries the WORST and the "
                                          "area-weighted MEAN of the faces it replaced, and "
                                          "faces_per_polygon says how many that was; a face kept "
                                          "whole carries its own value"}
            del vals

        out.append(row)
        delivered_areas.append(area[drawable])
        totals["max_vertex_shift_m"] = max(totals.get("max_vertex_shift_m", 0.0), shift)
        totals["delivered_faces"] += int(drawable.size)
        totals["drawn_polygons"] += int(n_poly)
        totals["merged"] += int(len(rect))
        totals["kept"] += int(kept_ids.size)
        totals["area_delivered_m2"] += area_in
        totals["area_drawn_m2"] += area_out
        totals["patches"][row["name"]] = row["exact"]

    d, dr = totals["delivered_faces"], totals["drawn_polygons"]
    totals["collapse"] = d / dr if dr else 0.0
    ai, ao = totals["area_delivered_m2"], totals["area_drawn_m2"]
    totals["area_error"] = abs(ao - ai) / ai if ai > 0 else 0.0
    totals["rect_tol"] = RECT_TOL
    totals["axial_dot"] = AXIAL_DOT
    totals["flatness_m"] = FLATNESS_M
    totals.setdefault("max_vertex_shift_m", 0.0)
    if delivered_areas:
        from meshpipeline.render.index_surface import _cell_stats
        totals["cell_stats"] = _cell_stats(np.concatenate(delivered_areas))
    logger.info("exact_surface %s: %s faces -> %s polygons (%.1f:1), area error %.2e, "
                "no vertex moved more than %.3e m",
                polymesh, format(d, ","), format(dr, ","), totals["collapse"],
                totals["area_error"], totals["max_vertex_shift_m"])
    return out, totals


__all__ = ["AXIAL_DOT", "FLATNESS_M", "GEOMETRY_BLOCK_FACES", "MIN_CORNERS", "RECT_TOL",
           "exact_surface", "face_geometry", "merge_rectangles"]
