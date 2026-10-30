# Responsibility: Cut one small neighbourhood out of a DELIVERED mesh boundary at full resolution,
# for a reader who has zoomed in on something the drawn surface is too coarse to show.
# Boundaries: it reads a delivered workspace and packs faces. It judges no mesh, changes no file,
# and refuses rather than guess when the source is bigger than it will read.
# Collaborates with: render/polymesh_budget.py (whose merging is the reason this exists),
# engines/cfmesh_conjugate/polymesh_surface.py (the same reader the main view uses).
"""The full-resolution detail of one place in a mesh that is drawn merged.

WHY THIS EXISTS. `polymesh_budget.fit_to_budget` clusters cells so a delivered mesh fits what a
browser can carry: the first customer cold plate delivers 13,022,400 boundary faces on the coolant
alone and the budget is 800,000 triangles, so about sixteen delivered faces become one drawn one.
The payload says so, and the viewer's zoom still lets a reader go in to a few cell widths - where
what they see is a merged approximation of the cells, not the cells. Every face count, size and
quality figure they are given is the mesh's own; the SURFACE under their cursor is not.

So a reader who wants to look at a 98 um channel in a 177 mm part is given this instead: the same
reader, on the same delivered polyMesh, restricted to a ball around the point they asked about, at
the resolution the mesher wrote. Nothing is merged, and the count of what was in range comes back
with it so they know whether they are seeing all of it.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from meshpipeline.render.boundary_index import IndexUnavailable, open_index
from meshpipeline.render.boundary_quality import QualityUnavailable, open_quality

logger = logging.getLogger(__name__)

#: How many faces a region's boundary may hold before this will not PARSE it. The parse is the
#: whole boundary - points, the face stream and the patch table - because a face's position is not
#: known until its points are, so there is no way to seek to a neighbourhood. Measured on the
#: delivered coupon: 349,656 faces on the coolant and 375,594 on the metal. The customer plate's
#: coolant side is 13,022,400, which is the case this refuses and says so about rather than reading
#: 400 MB inside an API process.
#:
#: IT ONLY APPLIES WHEN THERE IS NO INDEX. `render.boundary_index` works out where every face is
#: once, in the worker, and writes it down; with that beside the mesh a ball becomes the buckets it
#: touches and nothing else is read, so the cost is the window's and the cap has nothing to guard.
MAX_SOURCE_FACES = 2_000_000
#: How many faces come back. A detail view is a few cell widths across, so this is a backstop for
#: a radius asked carelessly, not a budget anyone should meet.
MAX_RETURN_FACES = 150_000
#: How many boundaries stay parsed. A reader opens several details of ONE mesh, and the parse is
#: the whole cost: measured on the coupon, 8.0 s to answer the first ball and 0.05 s to answer the
#: next. Each entry is the compact form - the connectivity as one int64 array, the points as
#: float64, the centroids as float64 - which on the coupon's two regions is about 70 MB together,
#: so four of them is the most this will hold in an API process.
CACHED_BOUNDARIES = 4


class DetailError(RuntimeError):
    """The detail cannot be cut, with the reason a reader should be shown."""


def delivered_regions(workspace) -> list[tuple[str, Path]]:
    """Every DELIVERED region's polyMesh, named. One region, called "", on a single-region engine.

    THE ROOT MESH IS NOT ONE OF THEM when regions exist. `splitMeshRegions` leaves the pre-split
    block at constant/polyMesh carrying every patch with the two regions' face counts ADDED
    TOGETHER - zmax is 532 at the root, which is the fluid's 128 plus the solid's 404 - so a
    reader shown it is shown a mesh that was never delivered and no split at all. The viewer
    refuses to read it for exactly this reason, and the first version of this did not: measured on
    the delivered coupon, a 0.3 mm ball came back with `zmax` 732 faces beside `fluid/inlet` 732,
    the first being the pre-split surface standing in for the delivered one.
    """
    ws = Path(workspace)
    root = ws / "constant"
    named = [(d.name, d / "polyMesh") for d in sorted(root.glob("*"))
             if d.is_dir() and d.name != "polyMesh" and (d / "polyMesh" / "boundary").is_file()]
    if named:
        return named
    if (root / "polyMesh" / "boundary").is_file():
        return [("", root / "polyMesh")]
    return []


@lru_cache(maxsize=CACHED_BOUNDARIES)
def _parsed(polymesh: str, _stamp: tuple):
    """One region's whole boundary in the compact form, parsed once.

    `_stamp` is the owner file's size and mtime, so a workspace rebuilt under the same path is
    read again rather than answered from a mesh that is gone. It is in the key and unused in the
    body on purpose.

    Returns (points, sizes, flat, offs, centroids, first_face): `flat` is every face's point ids
    end to end, `offs` is where each face starts in it (len = faces + 1), and `centroids` is one
    `np.add.reduceat` over the lot - which is the whole reason this is not a loop per face.
    Measured on the coupon: 28.1 s a face at a time against 8.0 s here, and 0.05 s once cached.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (
        read_boundary_faces,
        read_boundary_patches,
        read_points,
    )

    pm = Path(polymesh)
    rows = read_boundary_patches(pm)
    if not rows:
        return None
    first = min(p["start_face"] for p in rows)
    last = max(p["start_face"] + p["n_faces"] for p in rows)
    points = read_points(pm)
    faces = read_boundary_faces(pm, first, last - first)
    sizes = np.fromiter((len(f) for f in faces), dtype=np.int64, count=len(faces))
    flat = np.fromiter((i for f in faces for i in f), dtype=np.int64, count=int(sizes.sum()))
    offs = np.concatenate(([0], np.cumsum(sizes)))
    # a degenerate face (fewer than three corners) would make reduceat divide by its own size;
    # they are dropped from the ANSWER below rather than from the index, so the face numbering
    # stays the boundary file's own
    safe = np.maximum(sizes, 1)
    centroids = np.add.reduceat(points[flat], offs[:-1], axis=0) / safe[:, None]
    return points, sizes, flat, offs, centroids, first


#: The bar each metric is judged against in a CUT, and where each one comes from. These travel in
#: the response beside the values, not in the sidecar: a file that stored a bar would be a second
#: opinion drifting from whoever is judging. They are not an engine's judgement either - the values
#: came from checkMesh, so the bar that belongs with them is OpenFOAM's own.
#:
#: SKEWNESS IS 20 HERE AND 4 ON THE MAIN SURFACE, and that is not an inconsistency. checkMesh
#: allows a BOUNDARY face 20 where an internal face gets 4, because a boundary skewness is a
#: half-cell measurement; every value in a cut is a boundary face's own. Judging these against 4
#: paints faces red that checkMesh passed, which is the failure this constant exists to prevent.
QUALITY_BARS = {
    # engines/openfoam_criteria MAX_NON_ORTHO: "65 deg is the standard meshQualityControls bar
    # (maxNonOrtho 65): above it, gradient/laplacian discretisation degrades"
    "non_ortho": 65.0,
    # checkMesh's own boundary bar, the one face_quality records as `boundary_limit`
    "skewness": 20.0,
    # render.face_quality ASPECT_RATIO_LIMIT, which is checkMesh's
    "aspect_ratio": 1000.0,
}


def _quality_getter(qual):
    """A callable that gathers one region's per-face quality for the faces finally sent, or None.

    It is a callable rather than the arrays themselves because the budget below cuts `idx` down
    after the faces are chosen, and these have to describe what is actually sent.
    """
    if qual is None:
        return None

    def get(idx):
        vals = {}
        for metric in qual.metrics:
            got = qual.values(metric, idx)
            if got is not None:
                vals[metric] = got
        if not vals:
            return None
        # THE BASIS TRAVELS WITH THE VALUES. It is not the same for all three - a boundary face's
        # skewness is its own and checkMesh allows it 20 where an internal face gets 4, while
        # non-orthogonality and aspect ratio are the owner cell's - so anything that draws these
        # against one bar paints faces red that checkMesh called fine.
        return {"basis": {m: qual.basis(m) for m in vals}, "values": vals}

    return get


def _packer(points, sizes, flat, offs):
    """`_pack` bound to one region's arrays.

    Both paths below hand the assembly loop the SAME callable - this, or a BoundaryIndex's own
    `pack` - so the loop that shares out the budget and names the patches does not know or care
    which of the two found the faces. Keeping that one loop is the point: it is where the
    "every patch keeps at least one face" rule lives, and a second copy of it for the indexed
    path is a second place for a conjugate detail to lose one of its two sides.
    """
    def pack(idx):
        return _pack(points, sizes, flat, offs, idx)
    return pack


def _pack(points, sizes, flat, offs, idx):
    """The two buffers polymesh_surface.boundary_surface writes, for the faces `idx` names.

    int64 throughout and the cast once at the end: a uint32 `sizes` makes np.concatenate promote
    the offsets to float64 - int64 beside uint64 has no integer type that holds both - and
    indexing with them raises "arrays used as indices must be of integer type".
    """
    import numpy as np

    take = np.concatenate([np.arange(offs[i], offs[i + 1]) for i in idx]) if idx.size else (
        np.empty(0, dtype=np.int64))
    mine = flat[take]
    sz = sizes[idx]
    uniq, inv = np.unique(mine, return_inverse=True)
    polys = np.empty(len(mine) + len(sz), dtype=np.uint32)
    starts = np.concatenate(([0], np.cumsum(sz + 1)[:-1]))
    polys[starts] = sz
    mask = np.ones(len(polys), dtype=bool)
    mask[starts] = False
    polys[mask] = inv
    return points[uniq].astype(np.float32).tobytes(), polys.tobytes()


def detail_at(workspace, *, centre, radius: float, skip_names: tuple[str, ...] = ()) -> dict:
    """The delivered boundary within `radius` of `centre`, unmerged.

    Raises DetailError with a reason a reader can act on: too big to read, nothing delivered, a
    radius that is not a length. Returns {"centre", "radius", "patches": [...], "faces_in_range",
    "faces_returned", "truncated"}; each patch carries the points/polys pair
    `viewer_pack.polymesh_response` packs for the main view, so the client draws this with the
    code it already has.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    ws = Path(workspace)
    c = np.asarray([float(v) for v in centre], dtype=np.float64)
    if c.shape != (3,):
        raise DetailError("a detail needs a point in three dimensions")
    r = float(radius)
    if not (r > 0.0):
        raise DetailError(f"a detail needs a positive radius, not {radius!r}")

    regions = delivered_regions(ws)
    if not regions:
        raise DetailError("this job delivered no mesh boundary to look at")

    # (region, patch, the face ids kept, something that packs them, something that gathers their
    # quality or None). Written out rather than annotated generically because the two branches
    # below fill it from different readers and the assembly loop must not be able to tell.
    hits: list[tuple] = []
    in_range = 0
    for region, polymesh in regions:
        rows = [p for p in read_boundary_patches(polymesh) if p["name"] not in skip_names]
        if not rows:
            continue

        # AN INDEX MEANS NOTHING IS PARSED, so the cap below has nothing to guard. It was written
        # once in the worker on this mesh; `open_index` refuses it if the mesh has changed since,
        # and this then falls through to the parse exactly as it did before there was an index.
        index = None
        try:
            index = open_index(polymesh)
        except IndexUnavailable as exc:
            logger.debug("detail: no boundary index at %s (%s)", polymesh, exc)
        if index is not None:
            # A CUT IS THE ONE PLACE A PER-FACE NUMBER IS ALSO A PER-DRAWN-POLYGON NUMBER. Nothing
            # here is merged: one delivered face is one polygon, packed in the ids' own order, so
            # `values(metric, ids)` gathered with those same ids lines up exactly. The main view
            # is reduced at this size and cannot take these arrays at all - there one drawn face
            # stands for several delivered ones, and a colour on it was measured on none of them.
            qual = None
            try:
                qual = open_quality(polymesh)
            except QualityUnavailable as exc:
                logger.debug("detail: no per-face quality at %s (%s)", polymesh, exc)
            # THE INDEX HOLDS EVERY PATCH, including any `skip_names` asked this call to leave
            # out - it is written once and read by callers that want different things - so the
            # skipping happens here rather than being baked into the file.
            want = {str(p["name"]) for p in rows}
            for p, mine in index.patch_slices(index.faces_near(c, r)):
                if str(p["name"]) not in want:
                    continue
                in_range += int(mine.size)
                hits.append((region, p, mine, index.pack, _quality_getter(qual)))
            continue

        total = sum(int(p["n_faces"]) for p in rows)
        if total > MAX_SOURCE_FACES:
            # NAMED, not skipped in silence. A reader who asked to see the coolant and was given
            # the metal would read the metal as the coolant.
            raise DetailError(
                f"the {region or 'mesh'} boundary is {total:,} faces and this reads at most "
                f"{MAX_SOURCE_FACES:,} at once, so its detail is not available")
        owner = polymesh / "owner"
        stat = owner.stat() if owner.is_file() else (polymesh / "boundary").stat()
        parsed = _parsed(str(polymesh), (stat.st_size, int(stat.st_mtime_ns)))
        if parsed is None:
            continue
        points, sizes, flat, offs, centroids, first = parsed
        # A FACE IS IN RANGE WHEN ITS CENTROID IS, which is the test the viewer's own areas use,
        # and it keeps each face whole rather than clipping it into something the mesher never
        # wrote.
        near = ((centroids - c) ** 2).sum(axis=1) <= r * r
        for p in rows:
            f0 = int(p["start_face"]) - first
            n = int(p["n_faces"])
            idx = np.flatnonzero(near[f0:f0 + n] & (sizes[f0:f0 + n] >= 3)) + f0
            if not idx.size:
                continue
            in_range += int(idx.size)
            # NO COLOURS ON THIS PATH, and not because they would be wrong: the per-face quality
            # is numbered against a boundary index, and this branch is the one taken when there
            # is no index. A mesh delivered before any of this behaves exactly as it did.
            hits.append((region, p, idx, _packer(points, sizes, flat, offs), None))

    # THE BUDGET IS SHARED OUT, NOT EATEN FIRST-COME. Measured on the delivered coupon, 2026-10-29,
    # before this: a 4 mm window held 684,660 faces in range across both regions, the cap of
    # 150,000 was spent walking the patches in order, and the answer came back as 150,000 faces of
    # `fluid/fluid_to_solid` with the metal side missing altogether. The payload said
    # `truncated: true` and said nothing about WHAT was dropped, so a reader looking at a
    # conjugate interface was shown one side of it and had no way to know. Same mistake as
    # publishing half a conjugate review surface, in a different file.
    #
    # Every patch with a face in range keeps at least one, the rest of the budget goes out in
    # proportion by largest remainder, and the faces kept inside a patch are spread evenly across
    # it - a contiguous prefix is whatever order the mesher happened to write, which puts the whole
    # sample in one corner of the window.
    counts = [int(h[2].size) for h in hits]
    shares = _share_out(MAX_RETURN_FACES, counts)
    out: list[dict] = []
    returned = 0
    for (region, p, idx, pack, qget), keep, here in zip(hits, shares, counts):
        if keep <= 0:
            continue
        if keep < idx.size:
            idx = idx[np.unique(np.linspace(0, idx.size - 1, keep).round().astype(np.int64))]
        returned += int(idx.size)
        pts_b, polys_b = pack(idx)
        # AFTER the budget has cut `idx` down, never before. The arrays have to describe the faces
        # that are actually being sent, and a window that was truncated sends a sample of them.
        qvals = qget(idx) if qget else None
        out.append({
            "name": f"{region}/{p['name']}" if region else str(p["name"]),
            "type": str(p["type"]),
            "face_count": int(idx.size),
            # one entry per face in `polys`, in the same order, or absent
            "quality": qvals,
            # WHAT THIS PATCH ACTUALLY HAS THERE, so a client drawing a truncated window can say
            # "1 face in 5 of the coolant" rather than leaving a reader to assume it is all of it.
            "faces_in_range": int(here),
            "points": pts_b,
            "polys": polys_b,
        })
    return {
        "centre": [float(v) for v in c],
        "radius": r,
        "patches": out,
        "faces_in_range": int(in_range),
        "faces_returned": int(returned),
        "truncated": bool(in_range > returned),
    }


def detail_response(job_id: str, cut: dict) -> dict:
    """One cut as the ROUTE serves it: the two buffers base64'd, every number as measured.

    This is a named shape rather than a dict literal in the handler because the browser has to
    agree with it and the browser is tested somewhere else. `tests/ui/test_detail_window.py`
    drives the shipped viewer against the bytes this builds from a real polyMesh, so a key
    renamed here fails in the browser gate instead of in front of a reader. The window shipped
    with a client that threw before it ever issued the request, and a client and a route that
    were only ever read side by side is how that happens.
    """
    import base64

    def one(q: dict) -> dict:
        out = {"name": q["name"], "type": q["type"], "face_count": q["face_count"],
               # how much of this patch is in the window, so a truncated answer can say
               # what fraction of each side it is showing rather than only that it cut
               "faces_in_range": q["faces_in_range"],
               "points_b64": base64.b64encode(q["points"]).decode("ascii"),
               "polys_b64": base64.b64encode(q["polys"]).decode("ascii")}
        qual = q.get("quality")
        if qual:
            # float32 per face, in the polys' own order, exactly as the viewer already decodes the
            # main surface's arrays. ABSENT rather than zero-filled when a metric was not measured:
            # a quality number that was not taken is not a good one, and a client handed zeros
            # paints a clean mesh.
            out["quality_b64"] = {m: base64.b64encode(v.astype("<f4").tobytes()).decode("ascii")
                                  for m, v in qual["values"].items()}
            out["quality_basis"] = dict(qual["basis"])
            # THE BAR TRAVELS WITH THE VALUES, so a colour on screen has a stated line behind it.
            # Without one a client can only invent a ramp, and then 18 deg and 68 deg are the
            # same hue with nothing saying which side of anything they are on.
            out["quality_limit"] = {m: QUALITY_BARS[m] for m in qual["values"]
                                    if m in QUALITY_BARS}
        return out

    return {"job_id": job_id, "centre": cut["centre"], "radius": cut["radius"],
            "faces_in_range": cut["faces_in_range"], "faces_returned": cut["faces_returned"],
            "truncated": cut["truncated"],
            "patches": [one(q) for q in cut["patches"]]}


def _share_out(budget: int, counts: list[int]) -> list[int]:
    """How many of each patch's in-range faces to return, so none of them disappears.

    Largest remainder, with a floor of one for every patch that has anything at all. A patch that
    is 0.3% of the window is worth one face rather than none: the alternative is a region silently
    missing from a conjugate detail, which is what this replaced.
    """
    total = sum(int(n) for n in counts)
    if total <= 0:
        return [0 for _ in counts]
    if total <= int(budget):
        return [int(n) for n in counts]
    b = int(budget)
    if len(counts) >= b:
        # More patches than the budget has room for. Nothing sensible is left to share, so give
        # them a face each in the order they came and stop - still every patch rather than one.
        return [1 if i < b else 0 for i, _ in enumerate(counts)]
    exact = [b * int(n) / total for n in counts]
    share = [max(1, int(e)) for e in exact]
    while sum(share) > b:                                 # the floors can overshoot
        i = max(range(len(share)), key=lambda j: (share[j], exact[j]))
        if share[i] <= 1:
            break
        share[i] -= 1
    left = b - sum(share)
    if left > 0:
        order = sorted(range(len(counts)), key=lambda j: exact[j] - int(exact[j]), reverse=True)
        for j in order:
            if left <= 0:
                break
            room = int(counts[j]) - share[j]
            if room <= 0:
                continue
            add = min(room, left)
            share[j] += add
            left -= add
    return [min(int(share[i]), int(counts[i])) for i in range(len(counts))]


__all__ = ["CACHED_BOUNDARIES", "MAX_RETURN_FACES", "MAX_SOURCE_FACES", "DetailError",
           "delivered_regions", "detail_at"]
