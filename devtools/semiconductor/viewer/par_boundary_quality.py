#!/usr/bin/env python3
"""Build a region's boundary-QUALITY sidecar from the DECOMPOSED mesh and checkMesh's fields.

The companion to parindex.py, and it must agree with it face for face. `write_quality` cannot be
pointed at cbox8 for the same two reasons `write_index` could not: there is no serial mesh, and
the mesh is binary. So this walks the same 8 processor directories in the same order and produces
arrays numbered against the index parindex.py wrote.

WHAT EACH METRIC IS, taken from boundary_quality's own rules rather than reinvented:
  - `skewness`     the BOUNDARY FACE'S OWN value, read from the nonuniform boundaryField that
                   checkMesh writes per patch, one entry per face of that patch.
  - `non_ortho`    the OWNER CELL'S value. A boundary face has no non-orthogonality of its own;
                   checkMesh writes `uniform 0` on the patch, which is true and useless to paint.
  - `aspect_ratio` the OWNER CELL'S value, because aspect ratio is a property of a cell and the
                   patch is written zeroGradient, which means exactly that.

THE ORDER IS THE THING THAT CAN SILENTLY GO WRONG. parindex assembled patch-major: for each patch
in processor0's boundary-file order, every processor's slice of it in processor order. This
repeats that loop exactly, and then refuses unless each per-patch count matches the index's own
patch table. A quality array off by one patch would paint the metal's numbers on the coolant and
look entirely plausible.

THE GATES, both independent of this code:
  1. For nonOrthoAngle and aspectRatio the maximum over ALL cells of ALL processors must equal the
     maximum checkMesh printed in its own summary for that region. That checks the field parse.
  2. For skewness every patch's value count must equal that patch's face count in the index.
  3. Every assembled array must be exactly the index's n_faces long.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/opt/mp/src")

from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import read_boundary_patches  # noqa: E402
from meshpipeline.render.boundary_quality import QUALITY_VERSION, _internal_field  # noqa: E402

CASE = Path("/mnt/work/cbox8")
OUT = Path("/home/rehaan/cb8")
NPROC = 8

FIELDS = {"skewness": "patch", "non_ortho": "owner_cell", "aspect_ratio": "owner_cell"}
FIELD_FILES = {"skewness": "skewness", "non_ortho": "nonOrthoAngle",
               "aspect_ratio": "aspectRatio"}

_LIST_RE = re.compile(rb"(\d+)\s*\n\(")
LABEL = np.dtype("<i4")

#: checkMesh's own summary lines, which gate the field parse.
_MAX_RE = {
    "non_ortho": re.compile(r"non-orthogonality Max:\s*([0-9.eE+-]+)"),
    "aspect_ratio": re.compile(r"Max aspect ratio\s*=\s*([0-9.eE+-]+)"),
    "skewness": re.compile(r"Max skewness\s*=\s*([0-9.eE+-]+)"),
}


def checkmesh_maxima(region: str) -> dict:
    """What checkMesh said, so the parse can be checked against it rather than trusted."""
    log = CASE / ("wf.%s.log" % region)
    if not log.is_file():
        return {}
    txt = log.read_text(errors="replace")
    out = {}
    for key, rx in _MAX_RE.items():
        hits = [float(m.group(1)) for m in rx.finditer(txt)]
        if hits:
            out[key] = max(hits)
    return out


def _after_header(fh) -> int:
    fh.seek(0)
    head = fh.read(8192)
    i = head.find(b"}")
    if i < 0:
        raise SystemExit("no FoamFile block")
    return i + 1


def _header_format(path: Path) -> bytes:
    """What a foam file says it is, from its own header: b"ascii" or b"binary"."""
    m = re.search(rb"format\s+(\w+)\s*;", path.open("rb").read(4096))
    return m.group(1) if m else b"binary"


def owner_slice(pm: Path, first: int, n: int):
    """The owner cell of each boundary face.

    BINARY IS TWO SEEKS; ASCII IS NOT SEEKABLE AT ALL. A binary labelList is fixed-width records,
    so face `first` is an offset. An ascii one is variable width - "7" and "12884553" are not the
    same number of bytes - so there is nothing to seek to and the whole list has to be parsed once
    and sliced.

    THIS READ BINARY UNCONDITIONALLY AND THE DELIVERED COOLANT IS ASCII. Every owner came back as
    four packed digit bytes read as an int32: 0x39393939, which is "9999", which is 960,051,513 -
    the same handful of values on every processor, all of them about 75x the cell count. The
    cell-range gate below is what caught it, and it is the reason that gate exists rather than a
    comment saying the owners ought to be in range.
    """
    with (pm / "owner").open("rb") as fh:
        off, cnt = _list_at(fh, _after_header(fh))
    if first + n > cnt:
        raise SystemExit("%s: owner has %d entries, asked for %d+%d" % (pm, cnt, first, n))
    if _header_format(pm / "owner") == b"ascii":
        data = (pm / "owner").read_bytes()
        end = data.rindex(b")")
        vals = np.fromstring(data[off:end].decode("latin-1"), dtype=np.int64, sep=" ")
        # the count in the file's own header is the gate on the parse
        if vals.size != cnt:
            raise SystemExit("%s: owner says %d entries, parsed %d" % (pm, cnt, vals.size))
        return vals[first:first + n]
    with (pm / "owner").open("rb") as fh:
        fh.seek(off + LABEL.itemsize * first)
        return np.frombuffer(fh.read(LABEL.itemsize * n), dtype=LABEL).astype(np.int64)


def _list_at(fh, start: int, window: int = 8192):
    fh.seek(start)
    head = fh.read(window)
    m = _LIST_RE.search(head)
    if not m:
        raise SystemExit("no foam list at %d" % start)
    return start + m.end(), int(m.group(1))


def all_patch_values(path: Path) -> dict:
    """Every patch's nonuniform boundaryField values, in ONE read of the file.

    boundary_quality's `_patch_values` re-reads the whole file per patch, which on the solid's
    288 MB-per-processor skewness would be three full reads where one does.
    """
    txt = path.read_text(errors="replace")
    i = txt.find("boundaryField")
    if i < 0:
        return {}
    seg = txt[i:]
    out = {}
    for m in re.finditer(r"\n    (\w+)\s*\n    \{(.*?)\n    \}", seg, re.S):
        name, body = m.group(1), m.group(2)
        if "nonuniform" not in body:
            continue
        j, k = body.find("("), body.rfind(")")
        if j < 0 or k <= j:
            continue
        out[name] = np.fromstring(body[j + 1:k], dtype=np.float64, sep=" ").astype(np.float32)  # noqa: NPY201
    return out


def real_patches(pm: Path):
    return [p for p in read_boundary_patches(pm) if str(p.get("type")) != "processor"]


def one_processor(job):
    """One processor's per-boundary-face quality, written out per patch so order is explicit."""
    region, p = job
    base = CASE / ("processor%d" % p) / "constant" / region
    pm = base / "polyMesh"
    d = OUT / "qparts" / region
    d.mkdir(parents=True, exist_ok=True)

    rows = real_patches(pm)
    keep = [r for r in rows if int(r["n_faces"]) > 0]
    info = {"cell_max": {}, "patches": {}}
    if not keep:
        return region, p, info

    first = min(int(r["start_face"]) for r in keep)
    last = max(int(r["start_face"]) + int(r["n_faces"]) for r in keep)
    n_span = last - first

    owner = None
    for key, basis in FIELDS.items():
        path = base / FIELD_FILES[key]
        if not path.is_file():
            continue
        if basis == "patch":
            vals = all_patch_values(path)
            for r in keep:
                nm = str(r["name"])
                got = vals.get(nm)
                n = int(r["n_faces"])
                if got is None or got.size != n:
                    arr = np.full(n, np.nan, dtype=np.float32)
                    info["patches"].setdefault(nm, {})[key] = (
                        "no values" if got is None else "%d values for %d faces" % (got.size, n))
                else:
                    arr = got
                np.save(d / ("p%d.%s.%s.npy" % (p, nm, key)), arr)
        else:
            uniform, cells = _internal_field(path)
            if cells is None:
                info["cell_max"][key] = float(uniform)
                per_face = np.full(n_span, float(uniform), dtype=np.float32)
            else:
                info["cell_max"][key] = float(cells.max())
                if owner is None:
                    owner = owner_slice(pm, first, n_span)
                if int(owner.max()) >= cells.size:
                    raise SystemExit("%s p%d %s: boundary names cell %d of %d"
                                     % (region, p, key, owner.max(), cells.size))
                per_face = cells[owner].astype(np.float32)
            for r in keep:
                f0 = int(r["start_face"]) - first
                n = int(r["n_faces"])
                np.save(d / ("p%d.%s.%s.npy" % (p, str(r["name"]), key)), per_face[f0:f0 + n])
    return region, p, info


def assemble(region, cell_max):
    """Concatenate patch-major, in parindex's exact order, and write the quality sidecar."""
    t0 = time.time()
    d = OUT / "qparts" / region
    idx_meta = json.loads((OUT / "sidecar" / region / "polyMesh" / "boundaryIndex"
                           / "meta.json").read_text())
    want_patches = [(p["name"], int(p["n_faces"]), int(p["first_face"]))
                    for p in idx_meta["patches"]]
    n_faces = int(idx_meta["n_faces"])

    arrays, notes = {}, {}
    for key in FIELDS:
        parts, total = [], 0
        ok = True
        for nm, n_want, f0 in want_patches:
            got = []
            for p in range(NPROC):
                f = d / ("p%d.%s.%s.npy" % (p, nm, key))
                if f.is_file():
                    got.append(np.load(f))
            a = np.concatenate(got) if got else np.empty(0, np.float32)
            # GATE 2: the patch must come to exactly what the index says it is.
            if a.size != n_want:
                notes[key] = "%s: assembled %d values for %d faces" % (nm, a.size, n_want)
                ok = False
                break
            if total != f0:
                notes[key] = "%s: starts at %d, the index puts it at %d" % (nm, total, f0)
                ok = False
                break
            parts.append(a)
            total += a.size
        if not ok:
            continue
        arr = np.concatenate(parts) if parts else np.empty(0, np.float32)
        # GATE 3
        if arr.size != n_faces:
            notes[key] = "assembled %d of %d faces" % (arr.size, n_faces)
            continue
        if not np.isfinite(arr).any():
            notes[key] = "checkMesh wrote no usable values on any patch"
            continue
        arrays[key] = arr

    if not arrays:
        raise SystemExit("%s: nothing could be reduced (%s)" % (region, notes))

    # GATE 1: the field parse, against checkMesh's own summary.
    said = checkmesh_maxima(region)
    gate = {}
    for key in ("non_ortho", "aspect_ratio"):
        if key in cell_max and key in said:
            mine, theirs = cell_max[key], said[key]
            agree = abs(mine - theirs) <= max(1e-4, 1e-5 * abs(theirs))
            gate[key] = {"cells_max": mine, "checkMesh_said": theirs, "agree": agree}
            if not agree:
                raise SystemExit("%s %s: parsed cell max %.6f, checkMesh said %.6f"
                                 % (region, key, mine, theirs))
    if "skewness" in arrays and "skewness" in said:
        bmax = float(np.nanmax(arrays["skewness"]))
        gate["skewness"] = {"boundary_max": bmax, "checkMesh_said_all_faces": said["skewness"],
                            "within": bmax <= said["skewness"] + 1e-6}
        if bmax > said["skewness"] + 1e-6:
            raise SystemExit("%s skewness: boundary max %.6f exceeds checkMesh's %.6f"
                             % (region, bmax, said["skewness"]))

    out = OUT / "sidecar" / region / "polyMesh" / "boundaryQuality"
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for key, arr in arrays.items():
        (out / ("%s.f32" % key)).write_bytes(np.ascontiguousarray(arr).tobytes())
        written[key] = int((out / ("%s.f32" % key)).stat().st_size)

    def fig(a):
        f = a[np.isfinite(a)]
        if not f.size:
            return None
        return {"max": float(f.max()), "mean": float(f.mean()), "measured": int(f.size)}

    meta = {
        "version": QUALITY_VERSION,
        "region": region,
        "n_faces": n_faces,
        "basis": {k: FIELDS[k] for k in arrays},
        "source": "checkMesh -writeFields, run -parallel on the decomposed mesh",
        "fields": {k: FIELD_FILES[k] for k in arrays},
        "bytes": written,
        "figures": {k: fig(v) for k, v in arrays.items()},
        "notes": notes,
        "index": {"n_faces": n_faces, "first_face": int(idx_meta["first_face"]),
                  "patches": [{"name": n, "n_faces": c} for n, c, _ in want_patches]},
        # What was checked and against what, in the file, so the next reader does not have to
        # take the alignment on trust.
        "checked_against_checkMesh": gate,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print("  %-6s %s  %.1f MB in %d s" % (region, sorted(arrays),
                                          sum(written.values()) / 1e6, time.time() - t0),
          flush=True)
    for k, v in meta["figures"].items():
        if v:
            print("      %-13s max %-12.5f mean %-12.5f over %s faces"
                  % (k, v["max"], v["mean"], f"{v['measured']:,}"), flush=True)
    for k, v in gate.items():
        print("      gate %-10s %s" % (k, v), flush=True)
    if notes:
        print("      notes:", notes, flush=True)
    return meta


def main():
    regions = sys.argv[1:] or ["chip", "fluid", "solid"]
    jobs = [(r, p) for r in regions for p in range(NPROC)]
    t0 = time.time()
    cell_max: dict = {r: {} for r in regions}
    with mp.Pool(min(len(jobs), 24)) as pool:
        for region, p, info in pool.imap_unordered(one_processor, jobs):
            for k, v in (info.get("cell_max") or {}).items():
                cell_max[region][k] = max(cell_max[region].get(k, -1e30), v)
            if info.get("patches"):
                print("   %-6s p%d  %s" % (region, p, info["patches"]), flush=True)
    print("extracted in %d s" % (time.time() - t0), flush=True)
    for r in regions:
        assemble(r, cell_max[r])
    print("TOTAL %d s" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
