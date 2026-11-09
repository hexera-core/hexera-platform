#!/usr/bin/env python3
"""Build a region's boundary-index sidecar straight from the DECOMPOSED, BINARY mesh.

WHY THIS EXISTS. The pitch-8 deliverable (`cbox8`) was split, checked and archived in its
decomposed form and the reconstructed copies were deleted, so the only pitch-8 mesh that exists is
8 processor directories. The viewer needs a boundary index, and `write_index` needs a serial
polyMesh - which would mean `reconstructParMesh -region X` three times, the serial tail that is
77% of this chain's wall clock. It does not need the volume at all: it reads `points`, `faces` and
`boundary` and keeps only the boundary. Those three exist per processor, and a region's real
boundary is exactly the union of its non-`processor` patches across the 8 of them - every boundary
face lives on exactly one processor, so the union neither drops nor double-counts.

AND THE MESH IS BINARY, which is why `write_index` could not be pointed at a processor directory
either: `_data_start` refuses anything but `format ascii`, by design (see
reference_foam_binary_breaks_viewer). Binary is not the harder case here, it is the easier one.
An ascii `faces` file has to be SCANNED to find where the boundary starts, because records are
variable length - `_stream_boundary_faces` walks 8.6 GB counting ")" characters. The binary form
is a `faceCompactList`: an int32 offsets list of nFaces+1 entries followed by the flat vertex
list, so face `n`'s vertices are found with two seeks and no scan at all. Measured layout on
processor0/chip, which is how the two lists below were confirmed rather than assumed:
    points  2,548,468 x 3 float64          = 61,163,232 B of a 61,164,161 B file
    faces   7,358,952 int32 offsets, then  = 29,435,808 B
            29,435,804 int32 vertices      = 117,743,216 B of a 147,179,968 B file
label=32 and scalar=64 come from the mesh's own Arch line, "LSB;label=32;scalar=64".

WHAT IT IS NOT. This does not reconstruct a mesh and must not be mistaken for one. It produces
the index and a `boundary` file that describes it, which is what travels to the viewer anyway (the
detail window answers with the mesh deleted). There is still no serial pitch-8 volume mesh.

THE CHECK THAT MAKES IT TRUSTWORTHY. Summing every patch across the 8 processors, real and
`processor` alike, must equal checkMesh's own global (faces - internal faces). Measured before any
of this was written, and it matches to the face on all three regions:
    fluid  16,622,416 + 793,862   = 17,416,278
    solid  18,104,116 + 1,797,772 = 19,901,888
    chip    1,763,676 + 247,450   =  2,011,126
so the real-patch sums above are the target this script has to reproduce exactly, and it refuses
to write an index that misses them.

POINTS ARE NOT WELDED ACROSS PROCESSORS. Each processor's boundary points are compacted on their
own and concatenated, so a point on a processor interface appears once per processor that touches
it. That costs a few per cent of vertices and changes no face, no centroid and no bucket; welding
would mean comparing 20M float triples for equality, which is a worse trade than carrying them.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.environ.get("MP_SRC") or "/opt/mp/src")

from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches  # noqa: E402
from meshpipeline.render.boundary_index import (  # noqa: E402
    INDEX_VERSION,
    NO_PATCH,
    _bucket_of,
    _grid,
)

#: WHERE IT READS AND WRITES. The defaults are the box this was written on; both read an
#: environment variable first so the same script can be pointed at a mesh pulled anywhere.
CASE = Path(os.environ.get("INDEX_CASE") or "/mnt/work/cbox8")
OUT = Path(os.environ.get("INDEX_OUT") or "/home/rehaan/cb8")
NPROC = int(os.environ.get("INDEX_NPROC") or 8)

#: What the real (non-`processor`) patches must come to, per region. From the processors' own
#: boundary files, cross-checked against checkMesh's global counts before this script was written.
EXPECT = {"fluid": 16_622_416, "solid": 18_104_116, "chip": 1_763_676}

#: A foam list's own header: the count on its own line, then the opening bracket.
_LIST_RE = re.compile(rb"(\d+)\s*\n\(")
LABEL = np.dtype("<i4")
SCALAR = np.dtype("<f8")


def _list_at(fh, start: int, window: int = 8192):
    """(byte offset of the first element, element count) of the foam list at or after `start`."""
    fh.seek(start)
    head = fh.read(window)
    m = _LIST_RE.search(head)
    if not m:
        raise SystemExit("no foam list found at offset %d" % start)
    return start + m.end(), int(m.group(1))


def _after_header(fh) -> int:
    """Byte offset just past the FoamFile block, so the header's own numbers are never matched."""
    fh.seek(0)
    head = fh.read(8192)
    i = head.find(b"}")
    if i < 0:
        raise SystemExit("no FoamFile block")
    return i + 1


def is_binary(path: Path) -> bool:
    """Which format this file is in, from its own FoamFile block. Neither is refused: the ascii
    path exists because the delivered mesh is written ascii on purpose (binary is unreadable to
    the viewer, the index and the quality sidecar)."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    i = head.find(b"}")
    if i < 0:
        raise SystemExit("%s: no FoamFile block" % path)
    return bool(re.search(rb"format\s+binary\s*;", head[:i]))


def read_faces_slice(path: Path, first: int, n: int):
    """Faces [first, first+n) as (sizes uint8, conn). Two seeks on a binary mesh; on an ascii one
    the internal faces before `first` have to be walked, which is a sequential read."""
    if not is_binary(path):
        from meshpipeline.render.boundary_index import _stream_boundary_faces
        return _stream_boundary_faces(path, first, n)
    with path.open("rb") as fh:
        off1, n1 = _list_at(fh, _after_header(fh))
        if first + n + 1 > n1:
            raise SystemExit("%s: face range %d+%d exceeds %d offsets" % (path, first, n, n1))
        fh.seek(off1 + LABEL.itemsize * first)
        offs = np.frombuffer(fh.read(LABEL.itemsize * (n + 1)), dtype=LABEL).astype(np.int64)
        # The vertex list begins after the offsets list's closing bracket.
        off2, _n2 = _list_at(fh, off1 + LABEL.itemsize * n1, window=256)
        fh.seek(off2 + LABEL.itemsize * int(offs[0]))
        conn = np.frombuffer(fh.read(LABEL.itemsize * int(offs[-1] - offs[0])), dtype=LABEL)
    sizes = np.diff(offs)
    if sizes.max(initial=0) > 255:
        raise SystemExit("%s: a face with %d corners" % (path, sizes.max()))
    return sizes.astype(np.uint8), conn


def map_points(path: Path):
    """The points, as something that can be indexed by point id. Binary is memmapped so only the
    points a face touches are ever read; ascii has to be parsed, so it comes back as a real array
    (float32 from the platform streamer, which is enough for a surface drawn at micron scale)."""
    if not is_binary(path):
        from meshpipeline.render.boundary_index import _stream_points
        return _stream_points(path)
    with path.open("rb") as fh:
        off, cnt = _list_at(fh, _after_header(fh))
    return np.memmap(path, dtype=SCALAR, mode="r", offset=off, shape=(cnt, 3))


def real_patches(pm: Path):
    """This processor's real boundary patches, in file order, `processor` ones dropped."""
    return [p for p in read_boundary_patches(pm) if str(p.get("type")) != "processor"]


def one_processor(job):
    """Pull one processor's real boundary out to .npy, and say what it found."""
    region, p = job
    pm = CASE / ("processor%d" % p) / "constant" / region / "polyMesh"
    rows = real_patches(pm)
    keep = [r for r in rows if int(r["n_faces"]) > 0]
    d = OUT / "parts" / region
    d.mkdir(parents=True, exist_ok=True)
    if not keep:
        np.save(d / ("p%d.points.npy" % p), np.empty((0, 3), np.float32))
        return region, p, {}, 0

    first = min(int(r["start_face"]) for r in keep)
    last = max(int(r["start_face"]) + int(r["n_faces"]) for r in keep)

    # THE REAL PATCHES MUST BE CONTIGUOUS for one read to cover them and nothing else. In a
    # decomposed mesh they are: the `processor` patches are appended after the real ones. Checked
    # rather than assumed, because a hole here would pull processor-interface faces into the
    # delivered surface and they would render as a wall that is not there.
    if sum(int(r["n_faces"]) for r in keep) != last - first:
        raise SystemExit("processor%d/%s: real patches are not contiguous" % (p, region))

    sizes, conn = read_faces_slice(pm / "faces", first, last - first)
    used, local = np.unique(conn, return_inverse=True)
    pts = map_points(pm / "points")
    bpts = np.asarray(pts[used.astype(np.int64)], dtype=np.float32)
    del pts
    lconn = local.astype(np.uint32)

    offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
    found = {}
    for r in keep:
        f0 = int(r["start_face"]) - first
        n = int(r["n_faces"])
        nm = str(r["name"])
        np.save(d / ("p%d.%s.sizes.npy" % (p, nm)), sizes[f0:f0 + n])
        np.save(d / ("p%d.%s.conn.npy" % (p, nm)), lconn[offs[f0]:offs[f0 + n]])
        found[nm] = n
    np.save(d / ("p%d.points.npy" % p), bpts)
    return region, p, found, int(bpts.shape[0])


def assemble(region):
    """Concatenate the 8 processors into one surface, patch by patch, and write the sidecar."""
    t0 = time.time()
    d = OUT / "parts" / region
    pm0 = CASE / "processor0" / "constant" / region / "polyMesh"
    # CANONICAL PATCH ORDER from processor0, so the delivered order is the mesh's own and does not
    # depend on which processor happened to hold the first face.
    rows0 = real_patches(pm0)
    order = [str(r["name"]) for r in rows0]
    types = {str(r["name"]): str(r.get("type") or "") for r in rows0}

    pts_blocks, pt_off, offsets = [], 0, {}
    for p in range(NPROC):
        b = np.load(d / ("p%d.points.npy" % p))
        offsets[p] = pt_off
        pt_off += int(b.shape[0])
        pts_blocks.append(b)
    bpoints = np.concatenate(pts_blocks) if pts_blocks else np.empty((0, 3), np.float32)
    del pts_blocks

    sizes_parts, conn_parts, patches, cursor = [], [], [], 0
    for nm in order:
        n_here = 0
        for p in range(NPROC):
            fs = d / ("p%d.%s.sizes.npy" % (p, nm))
            if not fs.is_file():
                continue
            s = np.load(fs)
            c = np.load(d / ("p%d.%s.conn.npy" % (p, nm))).astype(np.uint32) + offsets[p]
            sizes_parts.append(s)
            conn_parts.append(c)
            n_here += int(s.shape[0])
        patches.append({"name": nm, "type": types.get(nm, ""),
                        "first_face": cursor, "n_faces": n_here})
        cursor += n_here

    sizes = np.concatenate(sizes_parts) if sizes_parts else np.empty(0, np.uint8)
    conn = np.concatenate(conn_parts) if conn_parts else np.empty(0, np.uint32)
    del sizes_parts, conn_parts
    n_faces = int(sizes.shape[0])

    # THE GATE. Anything other than the counts the processors' own boundary files declare means
    # faces were dropped or doubled, and an index like that is worse than none.
    want = EXPECT.get(region)
    if want is not None and n_faces != want:
        raise SystemExit("%s: assembled %d faces, the boundary files declare %d" %
                         (region, n_faces, want))
    if sum(p["n_faces"] for p in patches) != n_faces:
        raise SystemExit("%s: patch table does not sum to %d" % (region, n_faces))

    offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
    if int(offs[-1]) != int(conn.shape[0]):
        raise SystemExit("%s: connectivity is %d long, the sizes need %d" %
                         (region, conn.shape[0], offs[-1]))
    if n_faces and int(conn.max()) >= int(bpoints.shape[0]):
        raise SystemExit("%s: a face references point %d of %d" %
                         (region, conn.max(), bpoints.shape[0]))

    safe = np.maximum(sizes.astype(np.int64), 1)
    centroids = (np.add.reduceat(bpoints[conn.astype(np.int64)], offs[:-1], axis=0)
                 / safe[:, None]).astype(np.float32)

    if len(patches) >= NO_PATCH:
        raise SystemExit("%s: too many patches to number" % region)
    patch_of = np.full(n_faces, NO_PATCH, dtype=np.uint16)
    for pid, p in enumerate(patches):
        patch_of[p["first_face"]:p["first_face"] + p["n_faces"]] = pid

    origin, edge, dims = _grid(centroids, n_faces)
    bid = _bucket_of(centroids, origin, edge, dims)
    nb = int(dims.prod())
    order_ix = np.argsort(bid, kind="stable").astype(np.uint32)
    starts = np.concatenate(([0], np.cumsum(np.bincount(bid, minlength=nb).astype(np.int64))))

    out = OUT / "sidecar" / region / "polyMesh"
    idx = out / "boundaryIndex"
    idx.mkdir(parents=True, exist_ok=True)
    for name, arr in (("points.f32", bpoints), ("sizes.u8", sizes), ("conn.u32", conn),
                      ("centroids.f32", centroids), ("patch_of.u16", patch_of),
                      ("bucket_face.u32", order_ix), ("bucket_start.i64", starts.astype(np.int64))):
        (idx / name).write_bytes(np.ascontiguousarray(arr).tobytes())

    # A `boundary` FILE BESIDE IT, because `open_index` cross-checks the index's patch table
    # against the mesh's whenever one is present, and that check is the only one left once the
    # volume mesh is not shipped. Writing it makes the check run, and run on real numbers.
    lines = ["FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
             "    class       polyBoundaryMesh;\n    location    \"constant/%s/polyMesh\";\n"
             "    object      boundary;\n}\n\n" % region,
             "%d\n(\n" % len(patches)]
    for p in patches:
        lines.append("    %s\n    {\n        type            %s;\n        nFaces          %d;\n"
                     "        startFace       %d;\n    }\n"
                     % (p["name"], p["type"] or "patch", p["n_faces"], p["first_face"]))
    lines.append(")\n")
    (out / "boundary").write_text("".join(lines))

    meta = {
        "version": INDEX_VERSION,
        "region": region,
        "n_faces": n_faces,
        "n_points": int(bpoints.shape[0]),
        "first_face": 0,
        "patches": patches,
        "grid": {"origin": [float(v) for v in origin], "edge": float(edge),
                 "dims": [int(v) for v in dims], "n_buckets": nb},
        "bbox": {"min": [float(v) for v in centroids.min(axis=0)],
                 "max": [float(v) for v in centroids.max(axis=0)]},
        "bytes": {n: int((idx / n).stat().st_size) for n in
                  ("points.f32", "sizes.u8", "conn.u32", "centroids.f32", "patch_of.u16",
                   "bucket_face.u32", "bucket_start.i64")},
        # NOT a `_source_stamp` of a serial mesh, because there is no serial mesh. What this index
        # was built from, said plainly, so nobody later reads it as a reconstruct.
        "source": {"built_from": "decomposed-binary", "case": str(CASE), "n_processors": NPROC,
                   "note": "union of the non-processor patches across the processor directories"},
    }
    (idx / "meta.json").write_text(json.dumps(meta, indent=2))
    mb = sum(meta["bytes"].values()) / 1e6
    print("  %-6s %s faces  %s points  %s buckets  %.1f MB  assembled in %d s"
          % (region, f"{n_faces:,}", f"{bpoints.shape[0]:,}", f"{nb:,}", mb, time.time() - t0),
          flush=True)
    for p in patches:
        print("      %-18s %12s" % (p["name"], f"{p['n_faces']:,}"), flush=True)
    return meta


def main():
    regions = sys.argv[1:] or ["chip", "fluid", "solid"]
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [(r, p) for r in regions for p in range(NPROC)]
    print("extracting %d parts on %d cores" % (len(jobs), os.cpu_count()), flush=True)
    t0 = time.time()
    with mp.Pool(min(len(jobs), 24)) as pool:
        for region, p, found, npts in pool.imap_unordered(one_processor, jobs):
            print("   %-6s p%d  %-44s %s pts" % (region, p, found, f"{npts:,}"), flush=True)
    print("extraction done in %d s" % (time.time() - t0), flush=True)
    for r in regions:
        assemble(r)
    print("TOTAL %d s" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
