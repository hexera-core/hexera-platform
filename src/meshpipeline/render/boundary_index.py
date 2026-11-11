# Responsibility: Write, once at delivery, an index of a delivered mesh boundary that lets a later
# reader seek to one neighbourhood instead of parsing the whole surface; and read it back.
# Boundaries: it reads a polyMesh and writes a sidecar directory. It judges no mesh, changes no
# mesh file, and never reports a figure it did not measure.
# Collaborates with: render/detail_window.py (the reader this exists for),
# engines/cfmesh_conjugate/polymesh_surface.py (whose parse this replaces at scale).
"""An index of a delivered boundary, so detail at any mesh size costs the same.

WHY THIS EXISTS. `detail_window` says it plainly: "The read is the whole boundary - points, the
face stream and the patch table - because a face's position is not known until its points are, so
there is no way to seek to a neighbourhood." That is true of a polyMesh, and it is the reason the
detail window refuses a boundary over MAX_SOURCE_FACES. It stops being true the moment somebody
has already worked out where every face is: this writes that down.

Measured on the customer plate's 182,269,914-cell mesh (ws_par247, 2026-10-30), the figures that
make the old path refuse are the fluid side's 13,181,685 boundary faces behind a 9.1 GB `faces`
file and a 2.7 GB `points` file. `polymesh_surface.read_points` reads that whole file into one
bytes object and `translate`s a second copy of it; `read_boundary_faces` scans the 8.6 GB of
internal-face records to find where the boundary starts and then builds one Python list per face.
Neither is a bug at coupon size and neither survives this one, so the pass here streams.

WHAT IS WRITTEN is the boundary and nothing else: its own points, its own connectivity, one
centroid per face, which patch each face came from, and a uniform grid of buckets over the
centroids. A query turns a ball into the handful of buckets it touches, gathers those faces' ids,
and tests their centroids exactly - so the cost follows the size of the WINDOW, not of the mesh.

WHAT IS NOT WRITTEN is any figure about mesh quality. A colour belongs to the measurement that
produced it; this file knows where faces are and nothing about whether they are any good.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: The sidecar's own version. A reader that finds a number it does not know ignores the index and
#: lets its caller fall back, rather than reading arrays whose meaning may have moved.
#: 2: the source stamp is size and a hash of the boundary, not mtime, so an index survives being
#: shipped. See `_source_stamp`.
INDEX_VERSION = 2

#: The sidecar's directory name, inside the region's polyMesh beside the mesh it describes. It goes
#: under the polyMesh on purpose: a mesh and its index travel together or not at all, and a stale
#: index beside a rebuilt mesh is the one failure this must not have.
INDEX_DIRNAME = "boundaryIndex"

#: About how many faces a bucket should hold. A query's cost is (buckets touched) x (this), so a
#: small number makes the grid huge and a large one makes every query read faces it throws away.
TARGET_FACES_PER_BUCKET = 32

#: The most buckets the grid may have, whatever the face count. The bucket table is the one part of
#: the sidecar that is not proportional to the mesh, so it is the one that needs a ceiling.
MAX_BUCKETS = 4_000_000

#: How much of a file is parsed at a time. Big enough that per-chunk overhead disappears, small
#: enough that two of them beside the arrays is not what runs a worker out of memory.
CHUNK_BYTES = 64 * 1024 * 1024

#: The patch id for a face no patch names. uint16's top value, so it cannot collide with a real
#: patch index; `write_index` refuses a mesh with that many patches rather than wrap round.
NO_PATCH = 0xFFFF

_HEADER_ASCII_RE = re.compile(rb"format\s+ascii\s*;")
#: A face record's corner COUNT - the integer immediately before its bracket. Matching only this,
#: rather than the record and its contents, is what lets the ids be parsed in one call for the
#: whole chunk instead of one call per face.
_SIZE_RE = re.compile(rb"(\d+)\s*\(")


class IndexUnavailable(RuntimeError):
    """There is no usable index here, with the reason. Callers that can fall back should."""


def index_dir(polymesh) -> Path:
    """Where this region's index lives."""
    return Path(polymesh) / INDEX_DIRNAME


def _data_start(path: Path) -> tuple[int, int]:
    """(byte offset of the first record, number of records) for an ascii foam list."""
    with path.open("rb") as fh:
        head = fh.read(65536)
    if not _HEADER_ASCII_RE.search(head):
        raise IndexUnavailable(f"{path.name}: not an ascii foam file")
    offset = 0
    count = None
    for line in head.splitlines(keepends=True):
        stripped = line.strip()
        if count is None and stripped.isdigit():
            count = int(stripped)
        elif count is not None and stripped == b"(":
            return offset + len(line), count
        offset += len(line)
    raise IndexUnavailable(f"{path.name}: could not locate list start in header")


def _stream_points(path: Path):
    """Every point, as one (P, 3) float32 array, parsed a chunk at a time.

    The whole-file read this replaces costs three copies of the file at once - the bytes, the
    `translate` of them, and the float64 array - which on the customer plate's 2.7 GB points file
    is about 11 GB to produce a 1.0 GB answer. Here the file is never all in memory: each chunk is
    cut back to its last complete record, the remainder carried to the next one.
    """
    import numpy as np

    offset, count = _data_start(path)
    out = np.empty((count, 3), dtype=np.float32)
    table = bytes.maketrans(b"()", b"  ")
    got = 0
    carry = b""
    with path.open("rb") as fh:
        fh.seek(offset)
        while got < count:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            buf = carry + chunk
            # cut at the last ')' so no record is split across chunks; what follows is carried
            cut = buf.rfind(b")")
            if cut < 0:
                carry = buf
                continue
            carry = buf[cut + 1:]
            body = buf[:cut + 1].translate(table)
            vals = np.fromstring(body, dtype=np.float64, sep=" ")  # noqa: NPY201
            n = min(vals.size // 3, count - got)
            if n:
                out[got:got + n] = vals[:n * 3].reshape(-1, 3)
                got += n
    if got != count:
        raise IndexUnavailable(f"{path.name}: read {got} of {count} points")
    return out


def _stream_boundary_faces(path: Path, first_face: int, n_faces: int):
    """The boundary's connectivity as (sizes uint8, conn uint32), parsed a chunk at a time.

    `sizes` is uint8 because a polyMesh face with more than 255 corners is not a thing cfMesh or
    snappyHexMesh writes; a file that held one would be refused here rather than silently wrapped.
    """
    import numpy as np

    offset, count = _data_start(path)
    if first_face + n_faces > count:
        raise IndexUnavailable("boundary face range exceeds the faces file's own count")

    # SKIP THE INTERNAL FACES BY COUNTING TERMINATORS, not by parsing them. On the customer plate
    # that is 221,872,725 records and 8.6 GB to walk past; counting b")" in 64 MB chunks is one
    # sequential read and no allocation per record.
    pos = offset
    remaining = first_face
    with path.open("rb") as fh:
        fh.seek(offset)
        while remaining > 0:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                raise IndexUnavailable(f"{path.name}: EOF before face {first_face}")
            n = chunk.count(b")")
            if n < remaining:
                remaining -= n
                pos += len(chunk)
                continue
            idx = -1
            for _ in range(remaining):
                idx = chunk.index(b")", idx + 1)
            pos += idx + 1
            break

        # THREE C-LEVEL PASSES PER CHUNK, NOT A PYTHON LOOP PER FACE. The customer plate's fluid
        # side has 13,181,685 boundary faces; a regex match and a one-face `np.fromstring` each is
        # 13 M round trips through the interpreter and was the whole cost of this pass. Instead:
        # every integer in the chunk comes out in one `fromstring`, the corner COUNTS come out in
        # one `findall`, and the counts then say exactly which of those integers were counts - so
        # the connectivity is the rest of them, removed by index and never looked at one at a time.
        all_sizes: list = []
        all_conn: list = []
        got = 0
        carry = b""
        table = bytes.maketrans(b"()", b"  ")
        fh.seek(pos)
        while got < n_faces:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            buf = carry + chunk
            cut = buf.rfind(b")")
            if cut < 0:
                carry = buf
                continue
            carry = buf[cut + 1:]
            body = buf[:cut + 1]

            ks = _SIZE_RE.findall(body)
            if not ks:
                continue
            sizes_c = np.fromiter((int(k) for k in ks), dtype=np.int64, count=len(ks))
            if sizes_c.size and int(sizes_c.max()) > 255:
                raise IndexUnavailable(
                    f"{path.name}: a face has {int(sizes_c.max())} corners, over this index's 255")
            # only as many records as are still wanted
            take = min(sizes_c.size, n_faces - got)
            sizes_c = sizes_c[:take]
            v = np.fromstring(body.translate(table), dtype=np.int64, sep=" ")  # noqa: NPY201
            # record i's count sits at index cumsum(sizes)[i-1] + i, because the counts are
            # interleaved with the ids they introduce
            csum = np.cumsum(sizes_c)
            prefix_at = np.concatenate(([0], csum[:-1] + np.arange(1, take))) if take else \
                np.empty(0, dtype=np.int64)
            need = int(csum[-1]) + take if take else 0
            if v.size < need:
                raise IndexUnavailable(
                    f"{path.name}: {v.size} integers where {need} were declared by the counts")
            conn_c = np.delete(v[:need], prefix_at)
            if conn_c.size != (int(csum[-1]) if take else 0):
                raise IndexUnavailable(f"{path.name}: connectivity does not match its own counts")
            all_sizes.append(sizes_c.astype(np.uint8))
            all_conn.append(conn_c.astype(np.uint32))
            got += take
    if got != n_faces:
        raise IndexUnavailable(f"{path.name}: read {got} of {n_faces} boundary faces")
    sizes = np.concatenate(all_sizes) if all_sizes else np.empty(0, dtype=np.uint8)
    conn = np.concatenate(all_conn) if all_conn else np.empty(0, dtype=np.uint32)
    return sizes, conn


def _grid(centroids, n_faces: int):
    """A uniform bucket grid over the centroids: (origin, cell edge, dims)."""
    import numpy as np

    lo = centroids.min(axis=0).astype(np.float64)
    hi = centroids.max(axis=0).astype(np.float64)
    span = np.maximum(hi - lo, 1e-12)
    want = max(1, min(MAX_BUCKETS, int(n_faces // max(1, TARGET_FACES_PER_BUCKET)) or 1))
    # One edge for all three axes, so a ball of radius r touches the same number of buckets
    # whichever way it is pointing. Derived from the centroids' own bounding volume, not the
    # part's: a boundary is a surface, and its bbox is the only span that is certainly populated.
    edge = float((span.prod() / want) ** (1.0 / 3.0))
    if not (edge > 0.0):
        edge = float(span.max())
    dims = np.maximum(np.ceil(span / edge).astype(np.int64), 1)
    # The edge above is a target; clamp the dims so a degenerate span (a flat boundary, where one
    # axis is ~0) cannot blow the bucket count out on the other two.
    while int(dims.prod()) > MAX_BUCKETS:
        edge *= 1.26  # ~cube root of 2, so each step halves the bucket count
        dims = np.maximum(np.ceil(span / edge).astype(np.int64), 1)
    return lo, edge, dims


def _bucket_of(centroids, origin, edge: float, dims):
    """Each face's flat bucket id."""
    import numpy as np

    ijk = np.floor((centroids.astype(np.float64) - origin) / edge).astype(np.int64)
    np.clip(ijk, 0, dims - 1, out=ijk)
    return (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]


def write_index(polymesh, *, region: str = "", skip_names: tuple[str, ...] = ()) -> dict:
    """Build this region's boundary index and write it beside the mesh. Returns its meta.

    One pass over the mesh, in the worker, where the mesh and the memory already are. Every later
    question about where faces are is answered from the arrays this leaves behind.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches

    pm = Path(polymesh)
    rows = [p for p in read_boundary_patches(pm) if p["name"] not in skip_names]
    if not rows:
        raise IndexUnavailable(f"{pm}: no boundary patches to index")
    rows.sort(key=lambda p: int(p["start_face"]))
    first = int(rows[0]["start_face"])
    last = max(int(p["start_face"]) + int(p["n_faces"]) for p in rows)
    n_faces = last - first

    # THE PATCHES MUST BE CONTIGUOUS for a single streamed read to cover them, and in a polyMesh
    # they are: every boundary patch's faces sit after all the internal ones, in start_face order.
    # If a `skip_names` cut a hole in the middle, the hole is still READ (it is cheaper than two
    # passes) and simply never named by any patch, so no face of it can be returned.
    covered = sum(int(p["n_faces"]) for p in rows)
    if covered > n_faces:
        raise IndexUnavailable(f"{pm}: patches overlap; {covered} faces declared across {n_faces}")

    points = _stream_points(pm / "points")
    sizes, conn = _stream_boundary_faces(pm / "faces", first, n_faces)

    offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
    # A degenerate face (under three corners) would make reduceat divide by its own size. They are
    # kept in the NUMBERING, so a face id here is the boundary file's own, and dropped from every
    # answer by the reader.
    safe = np.maximum(sizes.astype(np.int64), 1)
    centroids = (np.add.reduceat(points[conn.astype(np.int64)], offs[:-1], axis=0)
                 / safe[:, None]).astype(np.float32)

    # COMPACT TO THE BOUNDARY'S OWN POINTS. The region's points file holds every point in the
    # volume - 84.8 M on the customer plate's fluid side - and a surface needs the ~13 M its own
    # faces touch. Renumbering here is what keeps the sidecar a fraction of the mesh.
    used, local = np.unique(conn, return_inverse=True)
    bpoints = points[used.astype(np.int64)].astype(np.float32)
    del points
    lconn = local.astype(np.uint32)

    # A SENTINEL, NOT ZERO. The span read above runs from the first boundary face to the last,
    # and a `skip_names` can leave a hole in the middle of it - those faces are read because one
    # pass is cheaper than two, and no patch names them. Initialised to zero they would come back
    # under the FIRST patch's name, which is the quiet version of the failure this is meant to
    # prevent: a reader who asked for the coolant being handed the metal. Latent until a caller
    # passes skip_names, which the conjugate viewer's own `_SKIP = ("solid_to_fluid",)` does.
    if len(rows) >= NO_PATCH:
        raise IndexUnavailable(f"{pm}: {len(rows)} patches, more than this index can number")
    patch_of = np.full(n_faces, NO_PATCH, dtype=np.uint16)
    patches = []
    for pid, p in enumerate(rows):
        f0 = int(p["start_face"]) - first
        n = int(p["n_faces"])
        patch_of[f0:f0 + n] = pid
        patches.append({"name": str(p["name"]), "type": str(p.get("type") or ""),
                        "first_face": f0, "n_faces": n})

    origin, edge, dims = _grid(centroids, n_faces)
    bid = _bucket_of(centroids, origin, edge, dims)
    nb = int(dims.prod())
    order = np.argsort(bid, kind="stable").astype(np.uint32)
    counts = np.bincount(bid, minlength=nb).astype(np.int64)
    starts = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)

    out = index_dir(pm)
    out.mkdir(parents=True, exist_ok=True)
    for name, arr in (("points.f32", bpoints), ("sizes.u8", sizes), ("conn.u32", lconn),
                      ("centroids.f32", centroids), ("patch_of.u16", patch_of),
                      ("bucket_face.u32", order), ("bucket_start.i64", starts)):
        (out / name).write_bytes(arr.tobytes())

    meta = {
        "version": INDEX_VERSION,
        "region": region,
        "n_faces": int(n_faces),
        "n_points": int(bpoints.shape[0]),
        "first_face": first,
        "patches": patches,
        "grid": {"origin": [float(v) for v in origin], "edge": float(edge),
                 "dims": [int(v) for v in dims], "n_buckets": nb},
        "bbox": {"min": [float(v) for v in centroids.min(axis=0)],
                 "max": [float(v) for v in centroids.max(axis=0)]},
        "bytes": {name: int((out / name).stat().st_size) for name in
                  ("points.f32", "sizes.u8", "conn.u32", "centroids.f32", "patch_of.u16",
                   "bucket_face.u32", "bucket_start.i64")},
        # The source's own identity, so a reader can tell a rebuilt mesh from the one indexed.
        "source": _source_stamp(pm),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    logger.info("boundary index written for %s: %d faces, %d buckets, %.1f MB",
                region or pm.parent.name, n_faces, nb,
                sum(meta["bytes"].values()) / 1e6)
    return meta


def _source_stamp(polymesh: Path) -> dict:
    """What the indexed mesh looked like, so a rebuilt one is not answered from a stale index.

    NOT MTIME, and that is the whole point of this function's shape. `detail_window._parsed` keys
    its cache on (size, mtime_ns) and is right to: the file it reads is local and was never moved.
    An index is meant to TRAVEL - it answers without the mesh, so it gets shipped to hosts the
    workspace was never copied to - and mtime does not survive the trip. Measured 2026-10-30: the
    boundary file came out of `tar -cpzf` / `tar -xpzf` with its seconds intact and its nanoseconds
    zeroed, so a stamp on mtime_ns refused an index that had just been shipped correctly. A guard
    that fires on the legitimate case is worse than none, because the next person turns it off.

    So: SIZE for the big files, because hashing a 9.1 GB `faces` is not something to do on every
    open, and SIZE PLUS CONTENT for `boundary`, which is about 1 KB and is the thing that actually
    says which surface this is.
    """
    st: dict = {}
    for name in ("faces", "points"):
        p = Path(polymesh) / name
        if p.is_file():
            st[name] = {"size": int(p.stat().st_size), "ends": _ends_digest(p)}
    b = Path(polymesh) / "boundary"
    if b.is_file():
        data = b.read_bytes()
        st["boundary"] = {"size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()}
    return st


#: How much of each end of a big file goes into its digest. Two seeks and 2 MB, whatever the file.
STAMP_SAMPLE_BYTES = 1024 * 1024


def _ends_digest(path: Path) -> str:
    """A digest of a big file's first and last megabyte, with its size already beside it.

    SIZE ALONE IS NOT ENOUGH and that is not hypothetical. Rewriting the test mesh with one cell
    stretched moves the points and leaves `points` at exactly the same byte count, because
    "(3 0 0)" and "(4 0 0)" are the same length - so a size-only stamp answered a changed mesh
    from the old index, and the index's centroids described geometry that was gone.

    A FULL HASH IS NOT AFFORDABLE on the files this guards: `faces` is 9.1 GB on the customer
    plate's fluid side and `points` is 2.7 GB, and `open_index` runs per request. So this is a
    CHANGE DETECTOR, not a proof: a rebuild moves the coordinates, and the coordinates live at
    both ends of the file as much as the middle. It would miss a mesh altered only in its middle
    at identical length, which is not a thing a mesher does, and the boundary's own full hash
    beside it still has to match.
    """
    h = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as fh:
        h.update(fh.read(STAMP_SAMPLE_BYTES))
        if size > 2 * STAMP_SAMPLE_BYTES:
            fh.seek(-STAMP_SAMPLE_BYTES, 2)
            h.update(fh.read(STAMP_SAMPLE_BYTES))
    return h.hexdigest()


class BoundaryIndex:
    """One region's index, mapped. Every query is a slice of these arrays."""

    def __init__(self, directory, meta: dict, arrays: dict):
        self.dir = Path(directory)
        self.meta = meta
        self._a = arrays

    @property
    def n_faces(self) -> int:
        return int(self.meta["n_faces"])

    @property
    def offs(self):
        """Where each face's corners start in `conn`, derived on first use and kept.

        It is the one thing about an index that cannot be memory-mapped: a running total has to
        be walked. Deriving it when something actually needs it keeps `open_index` free, which
        matters because a request opens the index several times and only one of those ever packs.
        """
        import numpy as np

        got = self._a.get("offs")
        if got is None:
            got = np.concatenate(([0], np.cumsum(np.asarray(self._a["sizes"], dtype=np.int64))))
            self._a["offs"] = got
        return got

    @property
    def patches(self) -> list[dict]:
        return list(self.meta.get("patches") or ())

    @property
    def points(self):
        """The BOUNDARY's own points, float32 (n_points, 3), already compacted and renumbered -
        not the volume's. `render.index_surface` draws from these."""
        return self._a["points"]

    @property
    def sizes(self):
        """Corner count per face, uint8 (n_faces,). A face under three corners is kept in the
        NUMBERING on purpose, so a face id here is the boundary file's own; whoever draws decides
        what to do with it."""
        return self._a["sizes"]

    @property
    def conn(self):
        """Every face's corners end to end, uint32, as local ids into `points`. A patch's faces are
        contiguous in this numbering, so a patch's connectivity is one SLICE of this rather than a
        gather - which is the whole reason drawing from the index is affordable."""
        return self._a["conn"]

    @property
    def centroids(self):
        """One centroid per face, float32 (n_faces, 3), in this index's own face numbering.

        Exposed because a caller that has to say WHERE a bad face is cannot get there through
        `faces_near`, which needs the place as its input. `render.boundary_quality` builds the
        viewer's hotspots from these.
        """
        return self._a["centroids"]

    @property
    def patch_of(self):
        """Which patch each face belongs to, uint16 (n_faces,), as an index into `patches` -
        or NO_PATCH for a face no patch names. Exposed for the same reason as `centroids`: a
        hotspot has to name its patch, and the mapping is already written down here."""
        return self._a["patch_of"]

    def faces_near(self, centre, radius: float):
        """The ids of every face whose centroid is within `radius` of `centre`.

        Only the buckets the ball touches are gathered, and their faces are then tested exactly -
        so the answer is the same set the whole-boundary scan returns, at the cost of the window.
        """
        import numpy as np

        g = self.meta["grid"]
        origin = np.asarray(g["origin"], dtype=np.float64)
        edge = float(g["edge"])
        dims = np.asarray(g["dims"], dtype=np.int64)
        c = np.asarray([float(v) for v in centre], dtype=np.float64)
        r = float(radius)

        lo = np.clip(np.floor((c - r - origin) / edge).astype(np.int64), 0, dims - 1)
        hi = np.clip(np.floor((c + r - origin) / edge).astype(np.int64), 0, dims - 1)
        bucket_face = self._a["bucket_face"]
        starts = self._a["bucket_start"]

        picked: list = []
        for i in range(int(lo[0]), int(hi[0]) + 1):
            for j in range(int(lo[1]), int(hi[1]) + 1):
                base = (i * int(dims[1]) + j) * int(dims[2])
                k0, k1 = int(lo[2]), int(hi[2])
                # one slice per (i, j) row: the k axis is contiguous in the flat bucket id
                s = int(starts[base + k0])
                e = int(starts[base + k1 + 1])
                if e > s:
                    picked.append(bucket_face[s:e])
        if not picked:
            return np.empty(0, dtype=np.int64)
        cand = np.concatenate(picked).astype(np.int64)
        cen = self._a["centroids"][cand]
        keep = ((cen.astype(np.float64) - c) ** 2).sum(axis=1) <= r * r
        keep &= self._a["sizes"][cand] >= 3
        return cand[keep]

    def faces_in_box(self, lo, hi):
        """The ids of every face whose centroid is inside the axis-aligned box `lo`..`hi`.

        THE SAME BUCKET WALK AS `faces_near`, with the box's own test instead of the sphere's. It
        exists because a VIEW is a box and not a ball: covering a screen with a ball either
        over-reaches at the corners - by a factor of about 2.2 on a 16:9 frustum, which is the
        difference between fitting a browser's budget and not - or under-reaches in depth and
        leaves the far half of what the reader can see unread.

        A face whose centroid is in the box is kept WHOLE, the same rule `faces_near` uses, so
        nothing is ever clipped into a shape the mesher did not write.
        """
        import numpy as np

        g = self.meta["grid"]
        origin = np.asarray(g["origin"], dtype=np.float64)
        edge = float(g["edge"])
        dims = np.asarray(g["dims"], dtype=np.int64)
        a = np.asarray([float(v) for v in lo], dtype=np.float64)
        b = np.asarray([float(v) for v in hi], dtype=np.float64)
        a, b = np.minimum(a, b), np.maximum(a, b)

        i0 = np.clip(np.floor((a - origin) / edge).astype(np.int64), 0, dims - 1)
        i1 = np.clip(np.floor((b - origin) / edge).astype(np.int64), 0, dims - 1)
        bucket_face = self._a["bucket_face"]
        starts = self._a["bucket_start"]

        picked: list = []
        for i in range(int(i0[0]), int(i1[0]) + 1):
            for j in range(int(i0[1]), int(i1[1]) + 1):
                base = (i * int(dims[1]) + j) * int(dims[2])
                s = int(starts[base + int(i0[2])])
                e = int(starts[base + int(i1[2]) + 1])
                if e > s:
                    picked.append(bucket_face[s:e])
        if not picked:
            return np.empty(0, dtype=np.int64)
        cand = np.concatenate(picked).astype(np.int64)
        cen = self._a["centroids"][cand].astype(np.float64)
        keep = np.all((cen >= a) & (cen <= b), axis=1)
        keep &= self._a["sizes"][cand] >= 3
        return cand[keep]

    def count_in_box(self, lo, hi) -> int:
        """How many faces are in that box, without packing any of them.

        A deep-zoom viewer has to decide whether the view it is about to draw FITS before it asks
        for it, and the decision costs a bucket walk while the answer costs tens of megabytes.
        """
        return int(self.faces_in_box(lo, hi).size)

    def patch_slices(self, face_ids):
        """`face_ids` split by the patch each came from, as [(patch meta, ids), ...].

        A FACE NO PATCH NAMES IS LEFT OUT, not assigned to the nearest one. Those are the faces
        inside a `skip_names` hole, read by the one pass that covers the whole boundary span and
        carrying NO_PATCH; no patch index can equal it, so they fall out of the loop below. That
        is the point: this is the step that turns a face id into a name a reader is shown, and
        the one thing it must never do is give a face the wrong patch's name.
        """
        import numpy as np

        ids = np.asarray(face_ids, dtype=np.int64)
        if not ids.size:
            return []
        pid = self._a["patch_of"][ids]
        out = []
        for i, p in enumerate(self.patches):
            mine = ids[pid == i]
            if mine.size:
                out.append((p, mine))
        return out

    def pack(self, face_ids):
        """The (points, polys) buffer pair `polymesh_surface.boundary_surface` writes.

        Same two buffers the main view is drawn from, so a client renders a detail with the code it
        already has: float32 xyz, then uint32 [corner count, local ids...] per face.
        """
        import numpy as np

        ids = np.asarray(face_ids, dtype=np.int64)
        sizes = self._a["sizes"]
        conn = self._a["conn"]
        pts = self._a["points"]
        if not ids.size:
            return b"", b""
        offs = self.offs
        # NO PER-FACE PYTHON. This was `concatenate([arange(offs[i], offs[i+1]) for i in ids])`,
        # which is one interpreter round trip and one live array per face - measured at 2.361 us a
        # face, which is why `index_surface` says in its own docstring that it cannot use this
        # method and carries a copy of the fast form instead. A detail cut never noticed, because
        # a cut is a few thousand faces; a deep-zoom WINDOW is 1,372,166 of them at the zoom where
        # real cells first become legible, and 3.2 s of the 5.3 s that took was this line.
        start = offs[ids]
        length = (offs[ids + 1] - start)
        total = int(length.sum())
        group = np.concatenate(([0], np.cumsum(length)[:-1]))
        take = np.repeat(start - group, length) + np.arange(total, dtype=np.int64)
        mine = conn[take].astype(np.int64)
        sz = sizes[ids].astype(np.int64)
        uniq, inv = np.unique(mine, return_inverse=True)
        polys = np.empty(int(mine.size + sz.size), dtype=np.uint32)
        starts = np.concatenate(([0], np.cumsum(sz + 1)[:-1]))
        polys[starts] = sz
        mask = np.ones(polys.size, dtype=bool)
        mask[starts] = False
        polys[mask] = inv
        return pts[uniq].astype(np.float32).tobytes(), polys.tobytes()


def open_index(polymesh) -> BoundaryIndex:
    """Map this region's index, or raise IndexUnavailable so the caller can fall back."""
    import numpy as np

    d = index_dir(polymesh)
    meta_path = d / "meta.json"
    if not meta_path.is_file():
        raise IndexUnavailable(f"{d}: no boundary index here")
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError as exc:
        raise IndexUnavailable(f"{meta_path}: unreadable ({exc})") from exc
    if int(meta.get("version") or 0) != INDEX_VERSION:
        raise IndexUnavailable(
            f"{d}: index version {meta.get('version')!r}, this reads {INDEX_VERSION}")
    # A REBUILT MESH IS NOT THE INDEXED ONE. The stamp is the same guard `detail_window._parsed`
    # puts in its cache key, for the same reason: a workspace rebuilt under one path would
    # otherwise be answered from a surface that no longer exists.
    #
    # A MESH THAT IS NOT THERE AT ALL IS ALLOWED, and is named here rather than falling out of a
    # loop that happened to compare nothing. The index carries the boundary's own points and
    # connectivity, so it answers without the mesh it was built from - which is how a detail
    # window works on a host the 137 GB workspace was never copied to. What that costs is the
    # stamp: with the files gone there is nothing to check the index against, so the patch table
    # below is the one cross-check left and it is not optional.
    now = _source_stamp(Path(polymesh))
    was = meta.get("source") or {}
    for name, seen in was.items():
        if name not in now:
            continue
        if now[name] != seen:
            raise IndexUnavailable(f"{d}: the mesh's {name} changed since this index was written")

    # THE BOUNDARY FILE AND THE INDEX MUST DESCRIBE ONE SURFACE. `detail_at` reads the patch table
    # from the mesh and keeps only the index's faces whose patch it names, so a boundary file that
    # disagrees does not error - it silently drops patches, or returns a patch's faces under
    # another patch's name. Cheap to check (the file is about 1 KB) and the only thing standing
    # between a self-contained index and a surface it was not built from.
    bnd = Path(polymesh) / "boundary"
    if bnd.is_file():
        from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches
        try:
            live = {str(p["name"]): int(p["n_faces"]) for p in read_boundary_patches(Path(polymesh))}
        except Exception as exc:  # noqa: BLE001 - an unreadable boundary is not a usable index
            raise IndexUnavailable(f"{bnd}: unreadable ({exc})") from exc
        for p in meta.get("patches") or ():
            name, n = str(p["name"]), int(p["n_faces"])
            if live.get(name) != n:
                raise IndexUnavailable(
                    f"{d}: the index has {name} at {n:,} faces and the mesh's boundary "
                    f"has it at {live.get(name, 'no such patch')!r}")

    n_f = int(meta["n_faces"])
    n_p = int(meta["n_points"])
    nb = int(meta["grid"]["n_buckets"])

    def mm(name, dtype, shape):
        return np.memmap(d / name, dtype=dtype, mode="r", shape=shape)

    # NOTHING IS READ HERE, and that is the point. Every array below is a memmap, so opening an
    # index costs a handful of stat calls whatever the mesh's size. The one thing that was not
    # was the offsets: `cumsum` materialised the whole uint8 sizes array and walked it. Measured
    # on the customer plate's 13,181,685-face coolant side, 2026-10-30: `open_index` cost
    # 0.136 s, of which 57% was that one cumsum - and a single detail request opens the index
    # FOUR times, once per region in `detail_at` and once more inside each `open_quality`. That
    # was 0.55 s of a 0.78 s request spent recomputing the same thing four times.
    # `BoundaryIndex.offs` derives it on first use instead, and only `pack` ever asks for it.
    #
    # The connectivity's length comes off the file rather than from the sizes' sum, for the same
    # reason: it is a uint32 each, so the file's own size says how many there are.
    arrays = {
        "points": mm("points.f32", np.float32, (n_p, 3)),
        "sizes": mm("sizes.u8", np.uint8, (n_f,)),
        "conn": mm("conn.u32", np.uint32, ((d / "conn.u32").stat().st_size // 4,)),
        "centroids": mm("centroids.f32", np.float32, (n_f, 3)),
        "patch_of": mm("patch_of.u16", np.uint16, (n_f,)),
        "bucket_face": mm("bucket_face.u32", np.uint32, (n_f,)),
        "bucket_start": mm("bucket_start.i64", np.int64, (nb + 1,)),
    }
    return BoundaryIndex(d, meta, arrays)


def has_index(polymesh) -> bool:
    """Whether a usable index sits beside this mesh. Never raises."""
    try:
        open_index(polymesh)
    except Exception:  # noqa: BLE001 - the answer to "is there one" is no
        return False
    return True


__all__ = ["BoundaryIndex", "INDEX_DIRNAME", "INDEX_VERSION", "IndexUnavailable",
           "has_index", "index_dir", "open_index", "write_index"]
