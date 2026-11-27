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

        # C-LEVEL PASSES PER CHUNK, NOT A PYTHON LOOP PER FACE. The customer plate's fluid side
        # has 13,181,685 boundary faces; a regex match and a one-face `np.fromstring` each is
        # 13 M round trips through the interpreter and was the whole cost of this pass. Every
        # integer in the chunk now comes out of one `fromstring`, and the loop below says which
        # of them were corner counts without reading the text a second time - see its own note.
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

            # ONE PASS OVER THE TEXT, NOT TWO. `np.fromstring` below already yields every
            # digit run in the chunk, in order, because `table` turns the brackets into spaces
            # and a face id is never negative - so the corner counts are ALREADY IN `v` and the
            # regex pass that used to find them was reading the same bytes a second time.
            #
            # A CORNER COUNT IS THE DIGIT RUN A '(' FOLLOWS, which `searchsorted` resolves for
            # every bracket at once. MEASURED on a 29.0 MiB chunk of 900,000 quad records: the
            # regex body runs at 12.6 MB/s and this at 49.8 MB/s, 3.96x, with the sizes and
            # connectivity arrays EQUAL on quads, triangles, mixed widths, whitespace runs
            # inside a record, newlines inside a record, and one, two or a tab's worth of
            # whitespace before the bracket - which is the `\s*` the old regex allowed and
            # cfMesh never writes. At this plate's 71,851,833 boundary faces that is about
            # 144 s off a 380 s index.
            #
            # AND NOT A NEXT-NON-WHITESPACE TABLE, which is the other way to allow that `\s*`:
            # on a 64 MiB chunk it is a 256 MB temporary on its own, against a CHUNK_BYTES
            # chosen so that two arrays beside it do not run a worker out of memory.
            arr = np.frombuffer(body, dtype=np.uint8)
            pad = np.zeros(arr.size + 2, dtype=np.int8)
            pad[1:-1] = (arr >= 0x30) & (arr <= 0x39)
            run_end = np.flatnonzero(np.diff(pad) == -1)     # exclusive ends, in `arr` coords
            open_at = np.flatnonzero(arr == 0x28)            # '('
            if run_end.size == 0 or open_at.size == 0:
                continue
            k = np.searchsorted(run_end, open_at, side="right") - 1
            k = k[k >= 0]
            if k.size == 0:
                continue
            v = np.fromstring(body.translate(table), dtype=np.int64, sep=" ")  # noqa: NPY201
            # only as many records as are still wanted
            take = min(k.size, n_faces - got)
            if take <= 0:
                break
            if int(k[take - 1]) >= v.size:
                raise IndexUnavailable(
                    f"{path.name}: a face record's count is not among the integers read from it")
            sizes_c = v[k[:take]]
            if int(sizes_c.max()) > 255:
                raise IndexUnavailable(
                    f"{path.name}: a face has {int(sizes_c.max())} corners, over this index's 255")
            # everything up to the NEXT record's count, less the counts themselves, is ids
            end_run = int(k[take]) if take < k.size else v.size
            keep = np.ones(end_run, dtype=bool)
            keep[k[:take]] = False
            conn_c = v[:end_run][keep]
            if conn_c.size != int(sizes_c.sum()):
                raise IndexUnavailable(f"{path.name}: connectivity does not match its own counts")
            all_sizes.append(sizes_c.astype(np.uint8))
            all_conn.append(conn_c.astype(np.uint32))
            got += take
    if got != n_faces:
        raise IndexUnavailable(f"{path.name}: read {got} of {n_faces} boundary faces")
    sizes = np.concatenate(all_sizes) if all_sizes else np.empty(0, dtype=np.uint8)
    conn = np.concatenate(all_conn) if all_conn else np.empty(0, dtype=np.uint32)
    return sizes, conn


#: WHAT A BINARY FOAM FILE SAYS ABOUT ITS OWN WIDTHS. Read rather than assumed - the hand-run
#: script this replaces hardcoded label=32 and scalar=64 off one mesh's line. They are per-file
#: facts, and a mesh written by a label64 build packs 8-byte labels: read as int32 those come back
#: as every other number interleaved with zeros, which is a face table that parses cleanly and
#: describes nothing. So the line is read and a width this cannot size is refused by name.
_ARCH_RE = re.compile(rb'arch\s+"([^"]*)"')
_FOAM_FORMAT_RE = re.compile(rb"format\s+(\w+)\s*;")
#: A foam list's own header in a binary file: the count on its own line, then the bracket. The
#: data follows the bracket packed, with no separators and no record headers.
_BINARY_LIST_RE = re.compile(rb"(\d+)\s*\n\(")
#: The (label bits, scalar bits) pairs this can read, and what numpy calls each.
_BINARY_WIDTHS = {(32, 64): ("<i4", "<f8"), (32, 32): ("<i4", "<f4"),
                  (64, 64): ("<i8", "<f8"), (64, 32): ("<i8", "<f4")}


def _foam_format(path: Path) -> str:
    """"ascii" or "binary", from the file's own FoamFile block and nothing else.

    NEITHER IS REFUSED HERE, which is the difference between this and `_data_start`. The mesh the
    CHAIN writes is ascii at every stage that could write one (`layers.py`, `case_scaffold.py`),
    because a binary polyMesh is unreadable to the viewer, to this index and to the quality
    sidecar. A decomposed mesh is the one the product may not have written: measured on the real
    four-rank case, its reconstructed copy is ascii and its processor directories are binary, from
    the same run - `decomposePar` and the parallel utilities obey `system/controlDict`, and that
    case's says binary. So the form is a question to ask the file, not a property of the product.
    """
    with path.open("rb") as fh:
        head = fh.read(4096)
    end = head.find(b"}")
    if end < 0:
        raise IndexUnavailable(f"{path.name}: no FoamFile block")
    m = _FOAM_FORMAT_RE.search(head[:end])
    if not m:
        raise IndexUnavailable(f"{path.name}: its FoamFile block does not say which format it is")
    said = m.group(1).decode("latin-1")
    if said not in ("ascii", "binary"):
        raise IndexUnavailable(f"{path.name}: format {said!r}, which this does not read")
    return said


def _binary_widths(path: Path) -> tuple[str, str]:
    """(label dtype, scalar dtype) for this binary file, from its own `arch` line."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    m = _ARCH_RE.search(head[:max(head.find(b"}"), 0) or len(head)])
    if not m:
        # NOT A DEFAULT DRESSED AS A MEASUREMENT. Every polyMesh OpenFOAM writes carries this
        # line; a file without one is refused rather than guessed at, because the guess that is
        # wrong produces a face table rather than an error.
        raise IndexUnavailable(
            f"{path.name}: a binary mesh with no `arch` line, so its integer and float widths "
            "are not stated and this will not assume them")
    arch = m.group(1).decode("latin-1")
    try:
        label = int(re.search(r"label\s*=\s*(\d+)", arch).group(1))
        scalar = int(re.search(r"scalar\s*=\s*(\d+)", arch).group(1))
    except (AttributeError, ValueError) as exc:
        raise IndexUnavailable(f"{path.name}: arch {arch!r} does not state its widths") from exc
    if "LSB" not in arch.upper():
        raise IndexUnavailable(
            f"{path.name}: arch {arch!r} is not little-endian, which this does not read")
    got = _BINARY_WIDTHS.get((label, scalar))
    if got is None:
        raise IndexUnavailable(f"{path.name}: arch {arch!r} is a width pair this cannot size")
    return got


def _binary_list_at(fh, start: int, window: int = 8192) -> tuple[int, int]:
    """(byte offset of the first packed element, element count) of the list at or after `start`."""
    fh.seek(start)
    m = _BINARY_LIST_RE.search(fh.read(window))
    if not m:
        raise IndexUnavailable(f"no foam list found at offset {start}")
    return start + m.end(), int(m.group(1))


def _binary_after_header(fh) -> int:
    """Just past the FoamFile block, so the header's own numbers are never taken for a count."""
    fh.seek(0)
    head = fh.read(8192)
    i = head.find(b"}")
    if i < 0:
        raise IndexUnavailable("no FoamFile block")
    return i + 1


def _binary_points(path: Path):
    """Every point, MAPPED rather than read: a flat list of 3N packed scalars after one header.

    A memmap and not an array on purpose. Only the points a boundary face touches are ever
    touched, and on a rank of a big region that is a small fraction of the volume's points - which
    is the whole reason the binary form is the cheaper one to index.
    """
    import numpy as np

    _label, scalar = _binary_widths(path)
    with path.open("rb") as fh:
        off, cnt = _binary_list_at(fh, _binary_after_header(fh))
    want = off + cnt * 3 * np.dtype(scalar).itemsize
    if want > path.stat().st_size:
        raise IndexUnavailable(
            f"{path.name}: declares {cnt:,} points, which needs {want:,} bytes of a "
            f"{path.stat().st_size:,}-byte file")
    return np.memmap(path, dtype=scalar, mode="r", offset=off, shape=(cnt, 3))


def _binary_boundary_faces(path: Path, first_face: int, n_faces: int):
    """Faces [first_face, first_face + n_faces) as (sizes uint8, conn), in two seeks.

    A binary `faces` is a faceCompactList: an offsets list of nFaces+1 labels, then a second list
    holding every face's vertex ids end to end - so face n's corners are found by arithmetic and
    nothing before them is read. The ascii form has to be SCANNED, because its records are
    variable length and there is nothing to seek to: `_stream_boundary_faces` walks every internal
    face counting brackets. Binary is not the harder case here, it is the easier one.
    """
    import numpy as np

    label, _scalar = _binary_widths(path)
    w = np.dtype(label).itemsize
    with path.open("rb") as fh:
        off1, n1 = _binary_list_at(fh, _binary_after_header(fh))
        if first_face + n_faces + 1 > n1:
            raise IndexUnavailable(
                f"{path.name}: faces {first_face:,}+{n_faces:,} is outside the {n1 - 1:,} this "
                "file holds")
        fh.seek(off1 + w * first_face)
        offs = np.frombuffer(fh.read(w * (n_faces + 1)), dtype=label).astype(np.int64)
        # The vertex list begins after the offsets list's own closing bracket.
        off2, n2 = _binary_list_at(fh, off1 + w * n1, window=256)
        if int(offs[-1]) > n2:
            raise IndexUnavailable(
                f"{path.name}: a face ends at vertex {int(offs[-1]):,} of {n2:,}")
        fh.seek(off2 + w * int(offs[0]))
        conn = np.frombuffer(fh.read(w * int(offs[-1] - offs[0])), dtype=label)
    sizes = np.diff(offs)
    if sizes.size and int(sizes.min()) < 0:
        raise IndexUnavailable(f"{path.name}: its face offsets do not increase")
    if sizes.size and int(sizes.max()) > 255:
        raise IndexUnavailable(
            f"{path.name}: a face with {int(sizes.max())} corners, which the sidecar's uint8 "
            "sizes cannot hold")
    return sizes.astype(np.uint8), conn.astype(np.uint32)


def _mesh_points(path: Path):
    """Every point of this mesh, whichever form it is written in."""
    return _binary_points(path) if _foam_format(path) == "binary" else _stream_points(path)


def _boundary_faces(path: Path, first_face: int, n_faces: int):
    """A span of this mesh's faces as (sizes uint8, conn), whichever form it is written in."""
    if _foam_format(path) == "binary":
        return _binary_boundary_faces(path, first_face, n_faces)
    return _stream_boundary_faces(path, first_face, n_faces)


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


def index_is_decomposed(case, polymesh) -> bool:
    """Whether this region's cells live in per-rank meshes rather than beside its boundary file.

    ONE PRODUCER OF THIS DECISION, read by the index and by the quality sidecar, because they are
    numbered against each other: an index built from the ranks beside a quality array reduced from
    a serial mesh - or the other way round - would paint one mesh's numbers on another's faces and
    look entirely plausible.

    IT ASKS ABOUT `faces`, not about `owner` or about the rank count. A decomposed-only delivery
    has a `boundary` file written from the ranks (`conjugate._publish_decomposed_boundaries`) and
    no cells at all beside it, and a MERGED delivery keeps its `processor*` directories until the
    reconstruct has finished - so "are there ranks" is true in both and says nothing. The file the
    index actually reads is the one worth asking about.
    """
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import processor_dirs

    if (Path(polymesh) / "faces").is_file():
        return False
    return bool(processor_dirs(Path(case)))


def write_index_decomposed(case, *, region: str = "") -> dict:
    """Build this region's boundary index from the PER-RANK meshes. Same sidecar, same reader.

    WHY IT EXISTS. With `CONJUGATE_MERGE_REGIONS=0` the chain never reconstructs, so
    `constant/<region>/polyMesh` holds the patch table `_publish_decomposed_boundaries` wrote from
    the ranks and nothing else - no `faces`, no `points`, no cells - and `write_index` needs all
    three. The cells are in `processor*/constant/<region>/polyMesh`, and a region's real boundary
    is exactly the union of its non-`processor` patches across the ranks: every boundary face of
    the whole mesh lives on exactly one rank, so the union neither drops a face nor counts one
    twice. The faces on a cut BETWEEN ranks are a different thing and are excluded - each was an
    internal face of the whole mesh and is a boundary face on both ranks that share it, so taking
    them in would draw walls through the middle of the part.

    IT RECONSTRUCTS NOTHING and must not be read as doing so. It reads each rank's `points`,
    `faces` and `boundary`, keeps the boundary, and writes the sidecar. There is still no serial
    volume mesh, and `meta["source"]` says that where a later reader will look for it.

    THE PATCH TABLE IS NOT ITS OWN, and that is load-bearing. It comes from
    `decomposed_region_boundary`, which is also what the `boundary` file beside the sidecar was
    written from - and `open_index` cross-checks the index's patch table against that file on
    every open. Two independent readings of the same ranks that disagreed about one patch would
    make every detail request refuse, with nothing wrong with either the mesh or the index. One
    producer, read twice.

    WHAT THE HAND-RUN SCRIPT THIS REPLACES DID INSTEAD
    (`devtools/semiconductor/viewer/par_boundary_index.py`), because both are corrected here:

      - IT GATED ON THREE HARDCODED FACE COUNTS - the pitch-8 deliverable's own 16,622,416 /
        18,104,116 / 1,763,676 - and skipped the gate entirely for any region not among those
        three names. So the one check that made it trustworthy did not run at all on a new case,
        which is the only case it would ever be pointed at again. Here the gate is the ranks' own
        declaration, per patch, and every region has one.
      - IT TOOK THE PATCH ORDER FROM processor0 ALONE, so a patch that rank 0 did not list was
        dropped with no error. A rank is supposed to list every global patch at `nFaces 0`, and
        does on the real 4-rank case this was measured against, but "supposed to" is what the
        union in `decomposed_region_boundary` costs nothing to stop depending on.

    EITHER FORMAT, and that is not generosity. The chain writes `writeFormat ascii` at every
    stage that could write a mesh, because a binary polyMesh is unreadable to the viewer - but the
    decomposed mesh is the one the product may not have written. Measured on the real four-rank
    case: its reconstructed copy is ascii and its processor directories are binary, FROM THE SAME
    RUN, because `decomposePar` and the parallel utilities obey `system/controlDict` and that
    case's says binary. An ascii-only reader here would have refused the only real decomposed mesh
    there is to check this against. See `_foam_format` and `_binary_boundary_faces`.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (
        DecomposedBoundaryError,
        decomposed_rank_spans,
        decomposed_region_boundary,
        processor_dirs,
        rank_of,
    )

    case = Path(case)
    pm_out = (case / "constant" / region / "polyMesh") if region else (case / "constant" / "polyMesh")
    ranks = processor_dirs(case)
    if not ranks:
        raise IndexUnavailable(f"{case}: no processor* directories to build an index from")
    try:
        rows = [r for r in decomposed_region_boundary(case, region) if int(r["n_faces"]) > 0]
        # THE SAME SPANS THE QUALITY SIDECAR WILL READ, from the one function that produces them,
        # so the two cannot walk the ranks in different orders or cut a patch in two places. See
        # `decomposed_rank_spans` for why that is not a guard but the removal of the possibility.
        spans = decomposed_rank_spans(case, region)
    except DecomposedBoundaryError as exc:
        raise IndexUnavailable(f"{case}: {exc}") from exc
    if not rows:
        raise IndexUnavailable(
            f"{case}: the {len(ranks)} ranks declare no real boundary faces for region {region!r}")
    if len(rows) >= NO_PATCH:
        raise IndexUnavailable(f"{case}: {len(rows)} patches, more than this index can number")
    want = {str(r["name"]): int(r["n_faces"]) for r in rows}

    # PASS ONE, RANK BY RANK: each rank's own real boundary, compacted to the points its faces
    # touch. Serial and in memory, where the hand-run script used a 24-process pool writing .npy
    # part files under a scratch directory. The arrays here are the sidecar's own size - points
    # and connectivity together came to under 4% of the mesh on disk on the customer plate - the
    # read is I/O bound either way, and a mesh job's workspace is archived and returned whole, so
    # part files left in it are part files that get shipped.
    rank_points: list = []
    parts: dict[str, list] = {str(r["name"]): [] for r in rows}
    for rank_i, (pm, local, first, n_span) in enumerate(spans):
        unknown = sorted({str(p["name"]) for p in local} - set(want))
        if unknown:
            raise IndexUnavailable(
                f"{rank_of(pm)}: patches {unknown} are on this rank and not in the region's own "
                f"patch table, so these per-rank files are not one mesh")
        sizes, conn = _boundary_faces(pm / "faces", first, n_span)
        used, local_ids = np.unique(conn, return_inverse=True)
        points = _mesh_points(pm / "points")
        if used.size and int(used.max()) >= int(points.shape[0]):
            raise IndexUnavailable(
                f"{rank_of(pm)}: a boundary face names point {int(used.max()):,} of "
                f"{int(points.shape[0]):,}")
        rank_points.append(np.asarray(points[used.astype(np.int64)], dtype=np.float32))
        del points
        lconn = np.asarray(local_ids).reshape(-1).astype(np.int64)
        offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
        for p in local:
            f0 = int(p["start_face"]) - first
            n = int(p["n_faces"])
            parts[str(p["name"])].append(
                (rank_i, sizes[f0:f0 + n], lconn[int(offs[f0]):int(offs[f0 + n])]))

    # PASS TWO: the ranks' point blocks end to end, in RANK order rather than patch order, so a
    # face from rank 3 can be renumbered against rank 3's block. Points are NOT welded across the
    # ranks: a point on a cut appears once per rank that touches it, which costs a few per cent of
    # vertices and changes no face, no centroid and no bucket. Welding would mean comparing tens of
    # millions of float triples for equality, which is a worse trade than carrying them.
    counts = [int(b.shape[0]) for b in rank_points]
    offsets = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
    bpoints = np.concatenate(rank_points) if rank_points else np.empty((0, 3), np.float32)
    del rank_points

    sizes_parts, conn_parts, patches, cursor = [], [], [], 0
    for r in rows:
        nm = str(r["name"])
        n_here = 0
        for rank_i, s, c in parts[nm]:
            sizes_parts.append(s)
            conn_parts.append(c + int(offsets[rank_i]))
            n_here += int(s.shape[0])
        # THE GATE, and it is the ranks' own declaration rather than three numbers from one case.
        # Anything else means faces were dropped or doubled between the boundary files and this
        # read of them, and an index like that is worse than none, because it answers.
        if n_here != want[nm]:
            raise IndexUnavailable(
                f"{case}: assembled {n_here:,} faces for patch {nm!r} and the ranks' boundary "
                f"files declare {want[nm]:,}")
        patches.append({"name": nm, "type": str(r.get("type") or ""), "first_face": cursor,
                        "n_faces": n_here})
        cursor += n_here
    del parts

    sizes = np.concatenate(sizes_parts) if sizes_parts else np.empty(0, np.uint8)
    conn64 = np.concatenate(conn_parts) if conn_parts else np.empty(0, np.int64)
    del sizes_parts, conn_parts
    n_faces = int(sizes.shape[0])
    offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
    if int(offs[-1]) != int(conn64.shape[0]):
        raise IndexUnavailable(
            f"{case}: the connectivity is {int(conn64.shape[0]):,} long and the face sizes need "
            f"{int(offs[-1]):,}")
    if conn64.size and int(conn64.max()) >= int(bpoints.shape[0]):
        raise IndexUnavailable(
            f"{case}: a face names point {int(conn64.max()):,} of {int(bpoints.shape[0]):,}")
    conn = conn64.astype(np.uint32)
    del conn64

    # A degenerate face (under three corners) would make reduceat divide by its own size. They are
    # kept in the NUMBERING, so a face id here is the assembled boundary's own, and dropped from
    # every answer by the reader - exactly as in `write_index`.
    safe = np.maximum(sizes.astype(np.int64), 1)
    centroids = (np.add.reduceat(bpoints[conn.astype(np.int64)], offs[:-1], axis=0)
                 / safe[:, None]).astype(np.float32)

    patch_of = np.full(n_faces, NO_PATCH, dtype=np.uint16)
    for pid, p in enumerate(patches):
        patch_of[p["first_face"]:p["first_face"] + p["n_faces"]] = pid

    origin, edge, dims = _grid(centroids, n_faces)
    bid = _bucket_of(centroids, origin, edge, dims)
    nb = int(dims.prod())
    order = np.argsort(bid, kind="stable").astype(np.uint32)
    starts = np.concatenate(
        ([0], np.cumsum(np.bincount(bid, minlength=nb).astype(np.int64)))).astype(np.int64)

    out = index_dir(pm_out)
    out.mkdir(parents=True, exist_ok=True)
    for name, arr in (("points.f32", bpoints), ("sizes.u8", sizes), ("conn.u32", conn),
                      ("centroids.f32", centroids), ("patch_of.u16", patch_of),
                      ("bucket_face.u32", order), ("bucket_start.i64", starts)):
        (out / name).write_bytes(np.ascontiguousarray(arr).tobytes())

    # THE STAMP IS THE PUBLISHED `boundary` FILE'S OWN, which is the only file of the mesh that
    # sits beside this sidecar - there is no serial `faces` or `points` to size. `_source_stamp`
    # returns exactly that one entry here, so `open_index`'s check still runs and still runs on a
    # real number: the patch table is what a reader is answered from, and a boundary file rewritten
    # under a standing index is the one failure this must not have. The provenance keys beside it
    # are not files and `open_index` skips them, which is what lets them be said here at all.
    source = dict(_source_stamp(pm_out))
    source["built_from"] = "decomposed"
    source["n_processors"] = len(ranks)
    source["ranks_with_boundary"] = len(spans)
    source["ranks"] = [d.name for d in ranks]
    source["note"] = ("the union of the non-processor patches across the processor directories; "
                      "there is no serial volume mesh and this index is not a reconstruct")

    meta = {
        "version": INDEX_VERSION,
        "region": region,
        "n_faces": n_faces,
        "n_points": int(bpoints.shape[0]),
        # ZERO, AND NOT A GUESS. On the meta this key is the first boundary face's id in the
        # mesh's own global face numbering, which a decomposed mesh does not have: each rank
        # numbers from zero. The assembled boundary's first face is its own face 0, and every
        # reader of the sidecar uses the patch-local `first_face` instead (see
        # `index_surface`, which says so where it reads them).
        "first_face": 0,
        "patches": patches,
        "grid": {"origin": [float(v) for v in origin], "edge": float(edge),
                 "dims": [int(v) for v in dims], "n_buckets": nb},
        "bbox": {"min": [float(v) for v in centroids.min(axis=0)],
                 "max": [float(v) for v in centroids.max(axis=0)]},
        "bytes": {name: int((out / name).stat().st_size) for name in
                  ("points.f32", "sizes.u8", "conn.u32", "centroids.f32", "patch_of.u16",
                   "bucket_face.u32", "bucket_start.i64")},
        "source": source,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    logger.info("boundary index written for %s from %d of %d ranks: %d faces, %d points, "
                "%d buckets, %.1f MB", region or "the mesh", len(spans), len(ranks), n_faces,
                int(bpoints.shape[0]), nb, sum(meta["bytes"].values()) / 1e6)
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
           "index_is_decomposed", "write_index_decomposed",
           "has_index", "index_dir", "open_index", "write_index"]
