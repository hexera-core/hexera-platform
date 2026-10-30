# Responsibility: Hand `render.polymesh_budget.fit_to_budget` the same patch dicts
# `cfmesh_conjugate.polymesh_surface.boundary_surface` hands it, built from the boundaryIndex
# sidecar instead of from a polyMesh parse.
# Boundaries: it reads one region's sidecar and nothing else. It judges no mesh, changes no file,
# and reports no figure it did not measure.
# Collaborates with: render/boundary_index.py (the arrays), render/polymesh_budget.py (the
# consumer), engines/cfmesh_conjugate/viewer.py (the caller).
"""The drawable boundary of one region, read out of its index.

WHY. `boundary_surface` is the only thing that currently produces the (points, polys) pair the
budget eats, and it gets there by reading the whole polyMesh. `read_points` reads the entire
`points` file into one bytes object and `translate`s a second copy of it before numpy sees a byte;
`read_boundary_faces` streams past every internal face record to find where the boundary starts and
then builds one Python `list[int]` per face. `native.py` prices that parse at about 303 s for
13,181,685 faces behind a 2.7 GB points file. The customer plate's coolant region has 31,184,373
boundary faces behind 246,049,422 internal ones, and its metal region 40,667,460 behind
711,609,849.

`boundary_index.write_index` has already done exactly that parse, once, in the worker, and written
the answer down: the boundary's own compacted points, its connectivity, its per-face corner counts
and its patch table. It cost 355 s and 349 s on those two regions. Nothing was reading it except
the detail window.

WHAT IT IS NOT. It is not a second opinion about the surface. Every value here is either a slice of
the sidecar or arithmetic over the sidecar's own arrays, and the output is byte-identical to
`boundary_surface`'s for the same patch - which is the only claim worth making about it, and the
one `tests/unit/render/test_the_index_draws_the_same_surface.py` holds to.

THE ONE THING THAT MAKES IT AFFORDABLE is that a patch's faces are CONTIGUOUS in the index's own
numbering, because `write_index` fills `patch_of[f0:f0 + n] = pid` in one go. So a patch's
connectivity is a single slice of `conn`. `BoundaryIndex.pack` cannot take that path - it accepts
arbitrary face ids, so it builds its gather with one `np.arange` per face inside a Python
comprehension, which at 31 M faces is 31 M interpreter round trips with 31 M live arrays before the
concatenate. There is no per-face Python in this file, on any path.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The corner count under which a face is not drawn. `boundary_surface` filters `len(f) >= 3` and
#: counts what is left, while the index keeps degenerate faces in its NUMBERING on purpose - so a
#: face id here is the boundary file's own. The filter therefore has to happen here, or
#: `face_count` means two different things on the two paths.
MIN_CORNERS = 3

#: How many faces' corners are gathered at once in the area pass. The (faces, k, 3) gather is the
#: only array here that is a multiple of the connectivity: at 31 M quads it is 3.0 GB of float32 in
#: one piece, so it is the only step that is blocked.
AREA_BLOCK_FACES = 2_000_000


def _ragged_arange(starts, lengths):
    """concatenate([arange(s, s + n) for s, n in zip(starts, lengths)]), without the loop.

    THE LOOP IS THE WHOLE POINT. It is the per-face comprehension this module exists to avoid, and
    it had crept back into two branches here: measured at 2.397 us a face, against
    `BoundaryIndex.pack`'s own 2.361 us - statistically the same thing this file disqualifies
    `pack` for. One degenerate face anywhere in a 31 M-face patch sent the whole patch down it.
    This form is 0.041 us a face and `np.array_equal` to it.
    """
    import numpy as np

    s = np.asarray(starts, dtype=np.int64)
    n = np.asarray(lengths, dtype=np.int64)
    total = int(n.sum())
    if total == 0:
        return np.empty(0, dtype=np.int64)
    # where each group begins in the OUTPUT, so the offset to add back is start - group_start
    group = np.concatenate(([0], np.cumsum(n)[:-1]))
    return np.repeat(s - group, n) + np.arange(total, dtype=np.int64)


def surface_from_index(polymesh, *, region: str = "", skip_names: tuple[str, ...] = (),
                       with_area: bool = True) -> tuple[list[dict], dict | None]:
    """`boundary_surface`'s own two return values, out of the index beside `polymesh`.

    Returns (patches, cell_stats). Each patch is the dict `fit_to_budget` consumes: `name`, `type`,
    `face_count`, `points` (float32 xyz, native byte order, RAW BYTES and not base64 - the base64
    happens later in `viewer_pack`), `polys` (uint32 `[n, i0 .. i(n-1)]` repeated, raw bytes), and
    `area_m2` when `with_area`.

    `region` prefixes the patch names, and a caller that prefixes them itself must leave it unset:
    `conjugate_viewer` does its own, so passing both gives "fluid/fluid/coolant_in".

    Raises `boundary_index.IndexUnavailable` when there is no usable index here, which is the same
    signal `detail_window` already falls back on.
    """
    import numpy as np

    from meshpipeline.render.boundary_index import open_index

    # NO POLYMESH ARRAY IS PARSED HERE. `open_index` reads meta.json, the ~1 KB `boundary` file and
    # 1 MB from each end of `faces` and `points` for the change stamp; every array below is a
    # memmap of the sidecar. A mesh that is absent altogether is allowed, so this answers on a host
    # the workspace never reached.
    idx = open_index(polymesh)
    sizes_all = idx.sizes
    conn_all = idx.conn
    pts_all = idx.points
    offs = idx.offs
    n_points = int(pts_all.shape[0])

    # ALLOCATED ONCE, not per patch. Both are sized by the REGION's point count - 31.5 M on the
    # coolant, so 31 MB and 126 MB - and rebuilding them inside the loop is that much per patch for
    # nothing. `seen` is cleared through the entries that were set, never reallocated.
    seen = np.zeros(n_points, dtype=bool)
    remap = np.empty(n_points, dtype=np.uint32)

    out: list[dict] = []
    all_areas: list = []
    for p in idx.patches:
        name = str(p["name"])
        if name in skip_names:
            continue
        # `first_face` ON A PATCH IS LOCAL AND `first_face` ON THE META IS GLOBAL. Same key, two
        # meanings: a patch's is `start_face - first` and the meta's is `first` itself. This wants
        # the local one; the global one reads the wrong faces and says nothing about it.
        f0, n = int(p["first_face"]), int(p["n_faces"])
        if n <= 0:
            continue
        sz_u8 = np.asarray(sizes_all[f0:f0 + n])
        good = sz_u8 >= MIN_CORNERS

        if bool(good.all()):
            conn = np.asarray(conn_all[int(offs[f0]):int(offs[f0 + n])])
            sz = sz_u8.astype(np.int64)
        else:
            keep = np.flatnonzero(good).astype(np.int64) + f0
            sz = sz_u8[good].astype(np.int64)
            conn = np.asarray(conn_all[_ragged_arange(offs[keep], sz)])
        if not sz.size:
            continue

        # COMPACT THE POINTS WITHOUT A SORT. `conn` is already local ids in [0, n_points), so the
        # set it touches comes out of a mask. The renumbering is the same one `polymesh_surface`
        # and `write_index` produce with `np.unique(..., return_inverse=True)`, because uniques
        # come back ascending and `flatnonzero` of a mask is ascending - at O(n_points + len(conn))
        # instead of a sort of len(conn), which at 125 M uint32 is the difference that matters.
        seen[conn] = True
        uniq = np.flatnonzero(seen)
        remap[uniq] = np.arange(uniq.size, dtype=np.uint32)
        inv = remap[conn]
        seen[conn] = False

        # THE POLYGON STREAM, `[n, i0 .. i(n-1)]` repeated, uint32, native byte order. That layout
        # is what `polymesh_budget._triangles` walks and what the browser counts polygons with.
        polys = np.empty(int(conn.size + sz.size), dtype=np.uint32)
        starts = np.concatenate(([0], np.cumsum(sz + 1)[:-1]))
        polys[starts] = sz
        mask = np.ones(polys.size, dtype=bool)
        mask[starts] = False
        polys[mask] = inv

        row = {
            "name": f"{region}/{name}" if region else name,
            # `face_count` IS THE MESH'S OWN AND MUST STAY SO. `fit_to_budget` sums exactly this
            # into `delivered_faces`, and that figure is what tells a reader the mesh is not as
            # coarse as the drawing. NOTHING HERE MAY THIN THE GEOMETRY AND LEAVE THIS ALONE:
            # `reduce_patch` returns a patch untouched when it already fits and `fit_to_budget`
            # then records nothing, so a pre-thinned patch comes back claiming nothing was merged.
            "face_count": int(sz.size),
            "type": str(p.get("type") or ""),
            "points": np.asarray(pts_all[uniq], dtype=np.float32).tobytes(),
            "polys": polys.tobytes(),
        }
        if with_area:
            areas = _areas(pts_all, conn, sz)
            all_areas.append(areas)
            # the wetted area this patch delivers. NOT IN THE SIDECAR - `boundary_index` stores
            # where faces are and nothing derived - so it is the fan sum `_face_areas` uses, over
            # the index's own points. `viewer_pack` makes it optional, but on a conjugate mesh the
            # interface's area IS the heat-transfer area, and staircasing takes it away without
            # changing a single face count.
            row["area_m2"] = float(areas.sum())
        out.append(row)

    if not out:
        return [], None
    stats = _cell_stats(np.concatenate(all_areas)) if (with_area and all_areas) else None
    logger.info("boundary drawn from the index: %d patch(es), %s faces%s",
                len(out), format(sum(r["face_count"] for r in out), ","),
                "" if with_area else ", areas not measured")
    return out, stats


def _areas(pts_all, conn, sz):
    """One area per face: the fan sum `polymesh_surface._face_areas` uses, blocked by size."""
    import numpy as np

    n = int(sz.size)
    offs = np.concatenate(([0], np.cumsum(sz)))
    areas = np.empty(n, dtype=np.float64)
    # One vectorised block per corner count present, exactly as `_face_areas` does: a cartesian
    # mesh's boundary is almost all quads with a handful of n-gons where the octree graded. Every
    # face in a block has the SAME corner count k, which is what lets the gather be built by
    # broadcasting rather than by a comprehension.
    for k in np.unique(sz):
        sel = np.flatnonzero(sz == k)
        for lo in range(0, sel.size, AREA_BLOCK_FACES):
            part = sel[lo:lo + AREA_BLOCK_FACES]
            ids = (offs[part][:, None] + np.arange(int(k), dtype=np.int64)).reshape(-1)
            v = np.asarray(pts_all)[conn[ids].astype(np.int64)].reshape(-1, int(k), 3)
            cr = np.cross(v[:, 1:-1] - v[:, :1], v[:, 2:] - v[:, :1])
            areas[part] = 0.5 * np.linalg.norm(cr.sum(axis=1), axis=1)
    return areas


def _cell_stats(areas) -> dict:
    """`boundary_surface`'s own cell_stats, unchanged: the 10th percentile of the equivalent cell
    edge, and the area-weighted median of it."""
    import numpy as np

    cell_sizes = np.sqrt(np.maximum(areas, 0.0))
    order = np.argsort(cell_sizes)
    cum = np.cumsum(areas[order])
    typ = float(cell_sizes[order][np.searchsorted(cum, cum[-1] * 0.5)]) if cum[-1] > 0 else 0.0
    return {"p10": float(np.percentile(cell_sizes, 10)), "typ": typ}


__all__ = ["AREA_BLOCK_FACES", "MIN_CORNERS", "surface_from_index"]
