# Responsibility: Reduce OpenFOAM's own mesh-quality fields to one value per delivered boundary
# face, once, where the mesh is; and read them back beside a boundary index.
# Boundaries: it reads checkMesh's output and a polyMesh and writes a sidecar. It measures nothing
# itself, judges nothing, and carries no bar.
# Collaborates with: render/boundary_index.py (the face numbering these align to),
# render/detail_window.py (which serves them), engines/cfmesh_conjugate (which runs checkMesh).
"""The quality of each delivered boundary face, as checkMesh measured it.

WHY NOT RE-DERIVE IT. `render.face_quality` computes these metrics in numpy and refuses any mesh
over MAX_FACES_FOR_FIELDS, 40 M faces, because the pass needs the whole mesh: owner, neighbour,
every face's area vector and every cell's centre. Priced on the customer plate that is about
25.6 GB resident for the coolant and 36.3 GB for the metal, against a 16 GB worker. The cap is
not a limitation to route around, it is that arithmetic written down.

checkMesh has already done the work. It runs per region in the mesher, on the machine that holds
the mesh, and `-writeFields` makes it write what it measured. Measured on the delivered coupon
(ws_13, fluid, 5,686,631 cells) 2026-10-30, the fields' own maxima reproduce checkMesh's summary
line for line:

    field           max          checkMesh said
    nonOrthoAngle   16.88060     Mesh non-orthogonality Max: 16.8806
    skewness        0.69653      Max skewness = 0.696529
    aspectRatio     1.20951      Max aspect ratio = 1.20951

So a face coloured from these is coloured with the number the panel already shows, by the same
tool that produced it, rather than by a second implementation drifting from the first.

WHAT EACH NUMBER IS, which differs per metric and is recorded in the sidecar rather than implied:
  - `skewness` is the BOUNDARY FACE'S OWN value. checkMesh writes it as a nonuniform boundaryField
    with exactly one entry per face of the patch (1,183,731 on the coupon's `fluid_to_solid`,
    which is that patch's face count). Its bar is not the internal one: checkMesh allows a
    boundary face 20 where an internal face gets 4, because a boundary skewness is a half-cell
    measurement. Anything judging these must use the boundary bar or it paints faces red that
    checkMesh called fine.
  - `non_ortho` is the OWNER CELL'S value. A boundary face has no non-orthogonality of its own -
    the metric lives between two cells - and checkMesh writes `value uniform 0` on the patch,
    which is a true statement about the face and a useless one to colour. The cell behind it is
    the thing a reader is asking about.
  - `aspect_ratio` is the OWNER CELL'S value, because aspect ratio is a property of a cell and
    checkMesh writes the patch as zeroGradient, which means exactly that.

WHAT THIS DOES NOT DO is decide whether any of it is bad. No limit is stored and none is applied.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: This sidecar's own version, separate from the boundary index's. They are written together and
#: they can fail apart: a region whose fields checkMesh did not write still gets an index, and the
#: detail window still works there without colours. One stamp must not govern two claims.
QUALITY_VERSION = 1

#: Where it goes, beside the index rather than inside it. `boundary_index` says of itself that it
#: knows where faces are and nothing about whether they are any good; that stays true.
QUALITY_DIRNAME = "boundaryQuality"

#: The three fields read, and what each one's value is attached to. `patch` means checkMesh wrote
#: one value per face of the patch; `owner_cell` means the value belongs to the cell behind the
#: face. This mapping is the contract and it is written into the sidecar's meta.
FIELDS = {
    "skewness": "patch",
    "non_ortho": "owner_cell",
    "aspect_ratio": "owner_cell",
}

#: The file checkMesh writes each of them to.
FIELD_FILES = {
    "skewness": "skewness",
    "non_ortho": "nonOrthoAngle",
    "aspect_ratio": "aspectRatio",
}

#: checkMesh's own argument for writing exactly these three and not the other twelve. `cellVolume`
#: and `minPyrVolume` alone are 68 MB each on a 5.7 M-cell region, and nothing reads them.
CHECKMESH_WRITE_FIELDS = "(nonOrthoAngle skewness aspectRatio)"

CHUNK_BYTES = 64 * 1024 * 1024

#: The punctuation that shares a stream with the numbers in an ascii foam list.
_LIST_PUNCT = bytes.maketrans(b"();", b"   ")


class QualityUnavailable(RuntimeError):
    """There is no usable per-face quality here, with the reason. Callers fall back to no colour."""


def quality_dir(polymesh) -> Path:
    """Where this region's per-face quality lives."""
    return Path(polymesh) / QUALITY_DIRNAME


def _field_path(polymesh: Path, key: str) -> Path:
    """checkMesh writes its fields beside the polyMesh, in the region's own directory."""
    return Path(polymesh).parent / FIELD_FILES[key]


def _internal_field(path: Path):
    """A volScalarField's internalField, as float32, parsed a chunk at a time.

    Streamed for the same reason `boundary_index` streams: on the customer plate's fluid side
    `skewness` is about 1.0 GB of ascii, and a `read_text()` of it is that again as a str before
    anything is parsed.
    """
    import numpy as np

    with path.open("rb") as fh:
        head = fh.read(4096)
        m = re.search(rb"internalField\s+nonuniform\s+List<scalar>\s*\n(\d+)\s*\n\(", head)
        if not m:
            # a uniform internalField is legal and carries one value for every cell
            u = re.search(rb"internalField\s+uniform\s+([-\d.eE+]+)\s*;", head)
            if u:
                return float(u.group(1)), None
            raise QualityUnavailable(f"{path.name}: no internalField this knows how to read")
        count = int(m.group(1))
        start = m.end()

        out = np.empty(count, dtype=np.float32)
        got = 0
        carry = b""
        fh.seek(start)
        while got < count:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            buf = carry + chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                carry = buf
                continue
            carry = buf[cut + 1:]
            seg = buf[:cut]
            # STOP AT THE LIST'S OWN TERMINATOR. One chunk is 64 MB and a small field is smaller
            # than that, so the read runs straight on into `boundaryField` and its patch names -
            # and `fromstring` does not refuse text it cannot parse, it stops at it and warns.
            # Parsing past the list is how a count would come out short on a file that was whole.
            end = seg.find(b"\n)")
            if end >= 0:
                seg = seg[:end]
            vals = np.fromstring(seg.translate(_LIST_PUNCT),  # noqa: NPY201
                                 dtype=np.float64, sep=" ")
            n = min(vals.size, count - got)
            if n:
                out[got:got + n] = vals[:n]
                got += n
    if got != count:
        raise QualityUnavailable(f"{path.name}: read {got} of {count} internal values")
    return None, out


def _patch_values(path: Path, patch: str):
    """One patch's boundaryField values, or None when checkMesh wrote none for it.

    `calculated` with a nonuniform list is the case worth having - that is checkMesh handing over
    a real per-face measurement. `uniform 0` and `zeroGradient` are not values, and returning them
    as if they were is how a boundary ends up painted with a number nobody measured.
    """
    import numpy as np

    txt = path.read_text(errors="replace")
    i = txt.find("boundaryField")
    if i < 0:
        return None
    seg = txt[i:]
    m = re.search(re.escape(patch) + r"\s*\{(.*?)\n    \}", seg, re.S)
    if not m:
        return None
    body = m.group(1)
    if "nonuniform" not in body:
        return None
    j = body.find("(")
    k = body.rfind(")")
    if j < 0 or k <= j:
        return None
    return np.fromstring(body[j + 1:k], dtype=np.float64, sep=" ").astype(np.float32)  # noqa: NPY201


def _owner_tail(polymesh: Path, first_face: int, n_faces: int):
    """The owner cell of each boundary face.

    `owner` is one cell id per face in face order, and the boundary faces are the last ones, so
    this streams the whole file and keeps the tail. On the plate's fluid side that is a 2.1 GB
    sequential read for 13.2 M numbers, which is the cheapest way to them that does not require
    the file to be seekable by record.
    """
    import numpy as np

    from meshpipeline.render.boundary_index import _data_start

    offset, count = _data_start(polymesh / "owner")
    if first_face + n_faces > count:
        raise QualityUnavailable("the boundary face range is outside the owner file's own count")

    want_from = first_face
    out = np.empty(n_faces, dtype=np.int64)
    seen = 0
    got = 0
    carry = b""
    with (polymesh / "owner").open("rb") as fh:
        fh.seek(offset)
        while got < n_faces:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                break
            buf = carry + chunk
            cut = buf.rfind(b"\n")
            if cut < 0:
                carry = buf
                continue
            carry = buf[cut + 1:]
            vals = np.fromstring(buf[:cut].translate(_LIST_PUNCT), dtype=np.int64, sep=" ")  # noqa: NPY201
            if vals.size:
                lo = max(0, want_from - seen)
                if lo < vals.size:
                    take = vals[lo:lo + (n_faces - got)]
                    out[got:got + take.size] = take
                    got += int(take.size)
                seen += int(vals.size)
    if got != n_faces:
        raise QualityUnavailable(f"owner: read {got} of {n_faces} boundary owners")
    return out


def write_quality(polymesh, *, region: str = "") -> dict:
    """Reduce checkMesh's fields to one value per boundary face and write them beside the index.

    Aligned to the BOUNDARY INDEX's face numbering, which is the boundary file's own: face i here
    is face i there, degenerate faces included, so a detail cut can gather `values[ids]` with the
    very ids it drew.
    """
    import numpy as np

    from meshpipeline.render.boundary_index import open_index

    pm = Path(polymesh)
    idx = open_index(pm)                 # raises IndexUnavailable, which the caller treats as fatal
    n_faces = idx.n_faces
    first = int(idx.meta["first_face"])

    present = {k: _field_path(pm, k) for k in FIELDS}
    missing = sorted(k for k, p in present.items() if not p.is_file())
    if len(missing) == len(FIELDS):
        raise QualityUnavailable(
            f"{pm.parent}: checkMesh wrote none of {sorted(FIELD_FILES.values())}")

    owner = None
    arrays: dict = {}
    notes: dict = {}
    for key, basis in FIELDS.items():
        path = present[key]
        if not path.is_file():
            notes[key] = f"checkMesh did not write {FIELD_FILES[key]}"
            continue
        if basis == "patch":
            # one patch at a time, because checkMesh writes them per patch and the index's face
            # numbering is the concatenation of those patches in start_face order
            vals = np.full(n_faces, np.nan, dtype=np.float32)
            wrote = 0
            for p in idx.patches:
                got = _patch_values(path, str(p["name"]))
                if got is None:
                    continue
                f0, n = int(p["first_face"]), int(p["n_faces"])
                if got.size != n:
                    notes[key] = (f"{p['name']}: checkMesh wrote {got.size:,} values for "
                                  f"{n:,} faces, so they are not used")
                    continue
                vals[f0:f0 + n] = got
                wrote += n
            if not wrote:
                # NOT OVER THE TOP OF A MORE SPECIFIC ONE. A patch whose value count disagreed has
                # already said so, and "carries no per-face values" on top of that loses the only
                # part of the message that says what to go and look at.
                notes.setdefault(
                    key, f"{FIELD_FILES[key]} carries no per-face values on any patch")
                continue
            arrays[key] = vals
        else:
            uniform, cells = _internal_field(path)
            if owner is None:
                owner = _owner_tail(pm, first, n_faces)
            if cells is None:
                arrays[key] = np.full(n_faces, float(uniform), dtype=np.float32)
            else:
                if int(owner.max()) >= cells.size:
                    notes[key] = (f"{FIELD_FILES[key]} has {cells.size:,} cells and the boundary "
                                  f"names cell {int(owner.max()):,}, so it is a different mesh")
                    continue
                arrays[key] = cells[owner].astype(np.float32)

    if not arrays:
        raise QualityUnavailable(
            f"{pm.parent}: none of checkMesh's fields could be reduced ({notes})")

    out = quality_dir(pm)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for key, arr in arrays.items():
        (out / f"{key}.f32").write_bytes(arr.tobytes())
        written[key] = int((out / f"{key}.f32").stat().st_size)

    def _fig(a):
        finite = a[np.isfinite(a)]
        if not finite.size:
            return None
        return {"max": float(finite.max()), "mean": float(finite.mean()),
                "measured": int(finite.size)}

    meta = {
        "version": QUALITY_VERSION,
        "region": region,
        "n_faces": int(n_faces),
        # THE BASIS PER METRIC, in the file, because it is not the same for all three and a reader
        # that assumes one of them is wrong about the other two.
        "basis": {k: FIELDS[k] for k in arrays},
        "source": "checkMesh -writeFields",
        "fields": {k: FIELD_FILES[k] for k in arrays},
        "bytes": written,
        "figures": {k: _fig(v) for k, v in arrays.items()},
        # WHAT IS NOT HERE AND WHY, rather than a key that is simply absent.
        "notes": notes,
        # The index this is numbered against. A quality sidecar beside a different index is
        # meaningless, and this is what lets a reader say so.
        "index": {"n_faces": int(n_faces), "first_face": first,
                  "patches": [{"name": str(p["name"]), "n_faces": int(p["n_faces"])}
                              for p in idx.patches]},
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    logger.info("boundary quality written for %s: %s over %d faces, %.1f MB",
                region or pm.parent.name, sorted(arrays), n_faces,
                sum(written.values()) / 1e6)
    return meta


#: checkMesh's own summary lines, which are what the field parse is checked against. Its printed
#: maxima are produced by a walk this code has no part in, so they are the one independent number
#: available - and the reduction is believed because it reproduces them, not because it looks right.
_SAID_MAX = {
    "non_ortho": re.compile(r"non-orthogonality Max:\s*([0-9.eE+-]+)"),
    "aspect_ratio": re.compile(r"Max aspect ratio\s*=\s*([0-9.eE+-]+)"),
    "skewness": re.compile(r"Max skewness\s*=\s*([0-9.eE+-]+)"),
}

#: How far a parsed cell maximum may sit from checkMesh's printed one. It prints six significant
#: figures, so this is the print's own resolution and not a tolerance for disagreement.
_MAX_REL_TOL = 1e-5
_MAX_ABS_TOL = 1e-4

#: A patch name as `read_boundary_patches` spells it. NARROWER WOULD BE WRONG: `\w+` drops a patch
#: whose name carries a dot, a colon or a hyphen, and a dropped patch is a patch painted NaN.
_PATCH_BLOCK_RE = re.compile(r"\n    ([A-Za-z_][\w.:-]*)\s*\n    \{(.*?)\n    \}", re.S)


def _all_patch_values(path: Path) -> dict:
    """Every patch's nonuniform boundaryField values, in ONE read of the file.

    `_patch_values` re-reads the whole file per patch, which is right for the serial writer - it
    asks for the patches the index names and there are a handful. Here the question is asked once
    per rank per field, and on the customer plate's metal side `skewness` is 288 MB PER RANK, so
    three patches would be three full reads of it where one does.

    THE SAME RULE ABOUT WHAT COUNTS AS A VALUE. `calculated` with a nonuniform list is checkMesh
    handing over a real per-face measurement. `uniform 0` and `zeroGradient` are not values, and
    returning them as if they were is how a boundary ends up painted with a number nobody measured.
    """
    import numpy as np

    txt = path.read_text(errors="replace")
    i = txt.find("boundaryField")
    if i < 0:
        return {}
    out: dict = {}
    for m in _PATCH_BLOCK_RE.finditer(txt[i:]):
        name, body = m.group(1), m.group(2)
        if "nonuniform" not in body:
            continue
        j, k = body.find("("), body.rfind(")")
        if j < 0 or k <= j:
            continue
        out[name] = np.fromstring(body[j + 1:k], dtype=np.float64, sep=" ").astype(np.float32)  # noqa: NPY201
    return out


def _rank_owner(polymesh: Path, first_face: int, n_faces: int):
    """The owner cell of each face in [first_face, first_face + n_faces), either format.

    BINARY IS TWO SEEKS; ASCII IS NOT SEEKABLE AT ALL. A binary labelList is fixed-width records,
    so face `first_face` is an offset. An ascii one is variable width - "7" and "12884553" are not
    the same number of bytes - so there is nothing to seek to and the list has to be walked.

    THE TRAP THIS CARRIES A GATE FOR, measured rather than imagined. A reader that assumed binary
    and met an ascii owner file read four packed digit bytes as one int32: 0x39393939, which is
    "9999", which is 960,051,513 - the same handful of values on every rank, all of them about
    seventy-five times the cell count. Nothing about the shape of that answer is wrong. What
    caught it is the caller's cell-range check, and that is why the check is not optional.
    """
    import numpy as np

    from meshpipeline.render.boundary_index import (
        IndexUnavailable,
        _binary_after_header,
        _binary_list_at,
        _binary_widths,
        _foam_format,
    )

    path = Path(polymesh) / "owner"
    try:
        if _foam_format(path) != "binary":
            return _owner_tail(Path(polymesh), first_face, n_faces)
        label, _scalar = _binary_widths(path)
    except IndexUnavailable as exc:
        raise QualityUnavailable(f"{path}: {exc}") from exc
    w = np.dtype(label).itemsize
    with path.open("rb") as fh:
        off, cnt = _binary_list_at(fh, _binary_after_header(fh))
        if first_face + n_faces > cnt:
            raise QualityUnavailable(
                f"{path.name}: it holds {cnt:,} owners and faces {first_face:,}+{n_faces:,} were "
                "asked for")
        fh.seek(off + w * first_face)
        return np.frombuffer(fh.read(w * n_faces), dtype=label).astype(np.int64)


def _checkmesh_said(case: Path, region: str) -> dict:
    """The maxima checkMesh printed for this region, from the log it wrote beside the mesh.

    THE FILENAME IS NOT SPELLED HERE. `foam_exec.CHECKMESH_LOG` is its one producer and two
    things already read it (`mesh_runner._output_mark` for progress and `mesher_stage.read_stage`
    for the customer's live line); a third spelling in this file would be a third thing to drift.
    Empty when there is no log, which is a measurement that was not taken rather than one that
    disagreed, and the caller records the difference.
    """
    try:
        from meshpipeline.engines.cfmesh_conjugate.foam_exec import CHECKMESH_LOG
    except ImportError:               # pragma: no cover - the engine is always importable here
        return {}
    log = Path(case) / CHECKMESH_LOG.format(suffix=f"_{region}" if region else "")
    if not log.is_file():
        return {}
    txt = log.read_text(errors="replace")
    out = {}
    for key, rx in _SAID_MAX.items():
        hits = [float(m.group(1)) for m in rx.finditer(txt)]
        if hits:
            out[key] = max(hits)
    return out


def write_quality_decomposed(case, *, region: str = "") -> dict:
    """Reduce checkMesh's per-rank fields to one value per boundary face of the assembled index.

    THE COMPANION TO `write_index_decomposed`, and it must agree with it face for face. With
    `CONJUGATE_MERGE_REGIONS=0` there is no serial mesh to reduce: checkMesh ran across the ranks
    and wrote its fields into `processor*/constant/<region>/`, and the index was assembled from
    the same ranks. This walks them in the same order, through the same
    `decomposed_rank_spans`, and refuses unless every patch comes to exactly what the index's own
    patch table says it is.

    WHY ONE PRODUCER OF THE SPANS AND NOT TWO LOOPS. A quality array that walked the ranks in a
    different order would be the right length, would sum to the right patch totals, and would pass
    every gate there is to write - and it would paint the metal's numbers on the coolant. That is
    not a hypothetical reading of the risk; it is the hand-run scripts' own docstring. There is no
    cheap check for it, so the loop is written once (`polymesh_surface.decomposed_rank_spans`) and
    read twice, and the failure is gone rather than guarded.

    WHAT EACH METRIC IS, taken from this module's own rules rather than reinvented per path:
      - `skewness`     the BOUNDARY FACE'S OWN value, from the nonuniform boundaryField checkMesh
                       writes per patch, one entry per face of that patch.
      - `non_ortho`    the OWNER CELL'S value. A boundary face has no non-orthogonality of its
                       own, and checkMesh writes `uniform 0` on the patch, which is true and
                       useless to paint.
      - `aspect_ratio` the OWNER CELL'S value, because aspect ratio is a property of a cell and
                       the patch is written zeroGradient, which means exactly that.

    THE GATE THAT MAKES IT TRUSTWORTHY is checkMesh's own summary. For the two owner-cell metrics
    the maximum over ALL cells of ALL ranks must be the maximum checkMesh printed - the same cells,
    so the same number - and for skewness the boundary's maximum must not exceed the whole mesh's,
    because the boundary is a subset of the faces. A disagreement RAISES: the colours are lost and
    the caller reports why, which is a far better outcome than a plausible wrong number on a
    picture a customer is looking at.
    """
    import numpy as np

    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (
        DecomposedBoundaryError,
        decomposed_rank_spans,
        rank_of,
    )
    from meshpipeline.render.boundary_index import open_index

    case = Path(case)
    pm_out = (case / "constant" / region / "polyMesh") if region else (case / "constant" / "polyMesh")
    # RAISES IndexUnavailable, which the caller treats as fatal for this region: a per-face array
    # is numbered against an index, so without one there is nothing for it to be numbered against.
    idx = open_index(pm_out)
    n_faces = idx.n_faces
    try:
        spans = decomposed_rank_spans(case, region)
    except DecomposedBoundaryError as exc:
        raise QualityUnavailable(f"{case}: {exc}") from exc
    if not spans:
        raise QualityUnavailable(
            f"{case}: no processor directory holds a real boundary for region {region!r}")

    per_rank: list[dict] = []
    cell_max: dict = {}
    notes: dict = {}
    reduced: set = set()
    for pm, rows, first, n_span in spans:
        rank = rank_of(pm)
        base = pm.parent
        got: dict = {}
        owner = None
        for key, basis in FIELDS.items():
            path = base / FIELD_FILES[key]
            if not path.is_file():
                notes.setdefault(key, f"checkMesh did not write {FIELD_FILES[key]}")
                continue
            if basis == "patch":
                vals = _all_patch_values(path)
                for p in rows:
                    nm, n = str(p["name"]), int(p["n_faces"])
                    seen = vals.get(nm)
                    if seen is None or seen.size != n:
                        # NAMED AND CARRIED AS NaN, not dropped. The array has to stay the index's
                        # own length or the gates below cannot tell a short patch from a short
                        # file, and the reader draws a NaN as no colour rather than as a number.
                        notes[key] = (f"{rank}/{nm}: checkMesh wrote "
                                      f"{'no values' if seen is None else format(seen.size, ',')}"
                                      f" for {n:,} faces")
                        got.setdefault(nm, {})[key] = np.full(n, np.nan, dtype=np.float32)
                    else:
                        got.setdefault(nm, {})[key] = seen
                        reduced.add(key)
            else:
                uniform, cells = _internal_field(path)
                if cells is None:
                    cell_max[key] = max(cell_max.get(key, float("-inf")), float(uniform))
                    per_face = np.full(n_span, float(uniform), dtype=np.float32)
                else:
                    cell_max[key] = max(cell_max.get(key, float("-inf")), float(cells.max()))
                    if owner is None:
                        owner = _rank_owner(pm, first, n_span)
                    # THE CELL-RANGE CHECK IS NOT OPTIONAL - see `_rank_owner` for the misread it
                    # is the only thing that caught.
                    if owner.size and int(owner.max()) >= cells.size:
                        raise QualityUnavailable(
                            f"{rank}: {FIELD_FILES[key]} has {cells.size:,} cells and the "
                            f"boundary names cell {int(owner.max()):,}, so they are not one mesh")
                    per_face = cells[owner].astype(np.float32)
                for p in rows:
                    f0, n = int(p["start_face"]) - first, int(p["n_faces"])
                    got.setdefault(str(p["name"]), {})[key] = per_face[f0:f0 + n]
                reduced.add(key)
        per_rank.append(got)

    arrays: dict = {}
    for key in FIELDS:
        if key not in reduced:
            notes.setdefault(key, f"{FIELD_FILES[key]} carries no per-face values on any rank")
            continue
        parts: list = []
        total = 0
        ok = True
        for p in idx.patches:
            nm, n_want, f0 = str(p["name"]), int(p["n_faces"]), int(p["first_face"])
            # THE INDEX'S OWN LAYOUT, checked as it is walked rather than at the end: a patch that
            # starts in the wrong place is a patch whose colours belong to another surface.
            if total != f0:
                notes[key] = f"{nm} assembles at {total:,} and the index puts it at {f0:,}"
                ok = False
                break
            chunks = [r[nm][key] for r in per_rank if key in r.get(nm, {})]
            a = np.concatenate(chunks) if chunks else np.empty(0, np.float32)
            if a.size != n_want:
                notes[key] = f"{nm}: assembled {a.size:,} values for {n_want:,} faces"
                ok = False
                break
            parts.append(a)
            total += int(a.size)
        if not ok:
            continue
        arr = np.concatenate(parts) if parts else np.empty(0, np.float32)
        if arr.size != n_faces:
            notes[key] = f"assembled {arr.size:,} of the index's {n_faces:,} faces"
            continue
        if not np.isfinite(arr).any():
            notes[key] = "checkMesh wrote no usable value on any patch of any rank"
            continue
        arrays[key] = arr

    if not arrays:
        raise QualityUnavailable(
            f"{pm_out.parent}: none of checkMesh's fields could be reduced ({notes})")

    said = _checkmesh_said(case, region)
    gate: dict = {}
    for key in ("non_ortho", "aspect_ratio"):
        if key in cell_max and key in said:
            mine, theirs = cell_max[key], said[key]
            agree = abs(mine - theirs) <= max(_MAX_ABS_TOL, _MAX_REL_TOL * abs(theirs))
            gate[key] = {"cells_max": mine, "checkMesh_said": theirs, "agree": bool(agree)}
            if not agree:
                raise QualityUnavailable(
                    f"{region or 'the mesh'} {key}: the ranks' cells come to {mine:.6f} and "
                    f"checkMesh printed {theirs:.6f}, so this parse is not its measurement")
    if "skewness" in arrays and "skewness" in said:
        bmax = float(np.nanmax(arrays["skewness"]))
        gate["skewness"] = {"boundary_max": bmax, "checkMesh_said_all_faces": said["skewness"],
                            "within": bool(bmax <= said["skewness"] + 1e-6)}
        if bmax > said["skewness"] + 1e-6:
            raise QualityUnavailable(
                f"{region or 'the mesh'} skewness: the boundary's maximum {bmax:.6f} is above "
                f"checkMesh's whole-mesh {said['skewness']:.6f}, which cannot be")
    if not said:
        # SAID RATHER THAN LEFT ABSENT. An empty gate dict reads the same as a gate that passed,
        # and the difference between "checked and agreed" and "there was no log to check against"
        # is the difference between a measurement and a hope.
        gate["not_checked"] = "no checkMesh log beside the mesh, so nothing independent to compare"

    out = quality_dir(pm_out)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for key, arr in arrays.items():
        (out / f"{key}.f32").write_bytes(np.ascontiguousarray(arr).tobytes())
        written[key] = int((out / f"{key}.f32").stat().st_size)

    def _fig(a):
        finite = a[np.isfinite(a)]
        if not finite.size:
            return None
        return {"max": float(finite.max()), "mean": float(finite.mean()),
                "measured": int(finite.size)}

    meta = {
        "version": QUALITY_VERSION,
        "region": region,
        "n_faces": int(n_faces),
        "basis": {k: FIELDS[k] for k in arrays},
        "source": "checkMesh -writeFields, reduced from the per-rank meshes with no reconstruct",
        "fields": {k: FIELD_FILES[k] for k in arrays},
        "bytes": written,
        "figures": {k: _fig(v) for k, v in arrays.items()},
        "notes": notes,
        "index": {"n_faces": int(n_faces), "first_face": int(idx.meta["first_face"]),
                  "patches": [{"name": str(p["name"]), "n_faces": int(p["n_faces"])}
                              for p in idx.patches]},
        # WHAT WAS CHECKED AND AGAINST WHAT, in the file, so the next reader does not have to take
        # the alignment on trust - and so "not checked" is visible rather than indistinguishable
        # from "checked and fine".
        "checked_against_checkMesh": gate,
        "ranks": len(spans),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    logger.info("boundary quality written for %s from %d ranks: %s over %d faces, %.1f MB",
                region or pm_out.parent.name, len(spans), sorted(arrays), n_faces,
                sum(written.values()) / 1e6)
    return meta


class BoundaryQuality:
    """One region's per-face quality, mapped."""

    def __init__(self, directory, meta: dict, arrays: dict):
        self.dir = Path(directory)
        self.meta = meta
        self._a = arrays

    @property
    def metrics(self) -> list[str]:
        return sorted(self._a)

    def basis(self, metric: str) -> str:
        return str((self.meta.get("basis") or {}).get(metric, ""))

    def values(self, metric: str, face_ids):
        """One metric's values for the faces named, in the order named."""
        import numpy as np

        arr = self._a.get(metric)
        if arr is None:
            return None
        return np.asarray(arr)[np.asarray(face_ids, dtype=np.int64)]


def open_quality(polymesh) -> BoundaryQuality:
    """Map this region's per-face quality, or raise so the caller can serve the cut without it."""
    import numpy as np

    from meshpipeline.render.boundary_index import open_index

    d = quality_dir(polymesh)
    meta_path = d / "meta.json"
    if not meta_path.is_file():
        raise QualityUnavailable(f"{d}: no per-face quality here")
    try:
        meta = json.loads(meta_path.read_text())
    except ValueError as exc:
        raise QualityUnavailable(f"{meta_path}: unreadable ({exc})") from exc
    if int(meta.get("version") or 0) != QUALITY_VERSION:
        raise QualityUnavailable(
            f"{d}: quality version {meta.get('version')!r}, this reads {QUALITY_VERSION}")

    # IT MUST BE NUMBERED AGAINST THE INDEX BESIDE IT. These are two files written together and
    # able to be replaced apart; an array whose face i is not the index's face i colours every
    # face with some other face's number, and nothing downstream could tell.
    idx = open_index(polymesh)
    want = meta.get("index") or {}
    live = {"n_faces": idx.n_faces, "first_face": int(idx.meta["first_face"]),
            "patches": [{"name": str(p["name"]), "n_faces": int(p["n_faces"])}
                        for p in idx.patches]}
    if want != live:
        raise QualityUnavailable(
            f"{d}: this was numbered against a different boundary index than the one beside it")

    n = int(meta["n_faces"])
    arrays = {}
    for key in (meta.get("fields") or {}):
        p = d / f"{key}.f32"
        if p.is_file():
            arrays[key] = np.memmap(p, dtype=np.float32, mode="r", shape=(n,))
    if not arrays:
        raise QualityUnavailable(f"{d}: the meta names fields and none of their files are here")
    return BoundaryQuality(d, meta, arrays)


def has_quality(polymesh) -> bool:
    """Whether usable per-face quality sits beside this mesh. Never raises."""
    try:
        open_quality(polymesh)
    except Exception:  # noqa: BLE001 - the answer to "is there any" is no
        return False
    return True


#: How many of the worst faces per metric are reported as hotspots. The same number
#: `render.face_quality` ships, so a reader cannot tell the two paths apart by the length of a
#: list.
MAX_HOTSPOTS_PER_METRIC = 60


def _sidecar_hotspots(metric: str, values, limit: float, centroids, patch_of, names: list[str],
                      offset: int = 0) -> list[dict]:
    """The worst faces over the bar, as places. Same shape as face_quality's, minus cell_faces."""
    import numpy as np

    v = np.asarray(values)
    over = np.nonzero(v > float(limit))[0]
    if not len(over):
        return []
    order = over[np.argsort(-(v[over]))][:MAX_HOTSPOTS_PER_METRIC]
    out = []
    for f in order:
        f = int(f)
        pid = int(patch_of[f])
        c = centroids[f]
        out.append({"metric": metric, "value": float(v[f]),
                    "x": float(c[0]), "y": float(c[1]), "z": float(c[2]),
                    "patch": names[pid] if 0 <= pid < len(names) else None})
    return [dict(h, face=int(h.pop("_f", 0)) + offset) if "_f" in h else h for h in out]


def quality_fields_from_sidecar(polymesh, *, non_ortho_limit: float, skew_limit: float = 4.0,
                                aspect_limit: float = 1000.0, patch_names=None) -> dict | None:
    """The viewer's quality fields, read from checkMesh's own numbers instead of re-derived.

    The same payload shape `render.face_quality.quality_fields` returns, so one viewer reads both,
    with three honest differences recorded in it: the basis is per metric and is checkMesh's,
    skewness is judged against the BOUNDARY bar of 20 rather than the internal 4 because every
    value here is a boundary face's own, and every count is over boundary faces rather than over
    the mesh. See `attach_sidecar_quality` for why this exists.

    None when there is no usable sidecar, or when no patch asked for has any face here - the same
    two answers face_quality gives, so the caller needs no new branch.
    """
    import base64

    import numpy as np

    from meshpipeline.render.face_quality import BOUNDARY_SKEW_LIMIT

    try:
        q = open_quality(polymesh)
    except Exception as exc:                # noqa: BLE001 - absent, stale or mismatched: no fields
        logger.info("boundary quality unavailable for the viewer: %s", exc)
        return None
    from meshpipeline.render.boundary_index import open_index

    idx = open_index(polymesh)
    names = [str(p["name"]) for p in idx.patches]
    wanted = set(patch_names) if patch_names else None

    # THE BAR EACH METRIC IS ACTUALLY JUDGED BY, given what the sidecar says each value IS.
    bars = {"non_ortho": float(non_ortho_limit),
            "skewness": float(BOUNDARY_SKEW_LIMIT),
            "aspect_ratio": float(aspect_limit)}
    labels = {"non_ortho": ("non-orthogonality", "\u00b0"),
              "skewness": ("skewness", ""), "aspect_ratio": ("aspect ratio", "")}

    per_patch: dict[str, dict] = {}
    for p in idx.patches:
        name = str(p["name"])
        if wanted is not None and name not in wanted:
            continue
        lo = int(p["first_face"])
        n = int(p["n_faces"])
        if n <= 0:
            continue
        row: dict = {"count": n}
        for key in ("non_ortho", "skewness", "aspect_ratio"):
            if key not in q.metrics:
                continue
            arr = np.asarray(q.values(key, np.arange(lo, lo + n, dtype=np.int64)),
                             dtype=np.float32)
            row[f"{key}_b64"] = base64.b64encode(arr.tobytes()).decode("ascii")
        if len(row) > 1:
            per_patch[name] = row
    if not per_patch:
        return None

    metrics: dict = {}
    hotspots: list[dict] = []
    for key in ("non_ortho", "skewness", "aspect_ratio"):
        if key not in q.metrics:
            continue
        whole = np.asarray(q.values(key, np.arange(idx.n_faces, dtype=np.int64)),
                           dtype=np.float32)
        finite = whole[np.isfinite(whole)]
        label, unit = labels[key]
        m = {"label": label, "unit": unit, "limit": bars[key],
             "max": float(finite.max()) if finite.size else 0.0,
             "n_over": int(np.count_nonzero(whole > bars[key])),
             "n_faces": int(idx.n_faces),
             # WHAT THESE NUMBERS ARE OF, because they are not of the same thing face_quality's
             # are: boundary faces only, and one value per face as checkMesh wrote it.
             "scope": "boundary",
             "basis": q.basis(key),
             "source": str(q.meta.get("source") or "checkMesh"),
             }
        if key == "skewness":
            # The internal bar is carried but NOT applied: nothing here is an internal face.
            m["internal_limit"] = float(skew_limit)
        if key == "aspect_ratio":
            m["floor"] = 1.0
            m["scale_to"] = float(max(m["max"], 2.0))
        metrics[key] = m
        hotspots += _sidecar_hotspots(key, whole, bars[key], idx.centroids, idx.patch_of, names)

    logger.info("viewer quality fields from checkMesh's own fields: %d boundary faces over %d "
                "patch(es), %d hotspot(s)", idx.n_faces, len(per_patch), len(hotspots))
    return {"basis": "checkMesh_per_metric", "metrics": metrics, "patches": per_patch,
            "hotspots": hotspots}


def attach_sidecar_quality(resp: dict, polymesh, *, non_ortho_limit: float,
                           skew_limit: float = 4.0, aspect_limit: float = 1000.0,
                           patch_of: dict[str, str] | None = None) -> None:
    """Add `quality_fields` to a viewer response from the sidecar, or leave it untouched.

    WHY THIS EXISTS. `render.face_quality` is the first choice and it refuses any mesh over its
    40 M-face cap, because its measurement needs the whole polyMesh resident - about 25.6 GB for
    this plate's coolant and 36.3 GB for its metal. On a mesh past the cap it returns None,
    `cfmesh_conjugate.viewer._paint` finds nothing to merge and returns, and the payload carries
    no `quality_fields` at all. Two of the six readings the customer asked for go missing on the
    meshes big enough to need them, silently, because a heatmap is swallowed by design.

    checkMesh measured all three metrics on the way past and `-writeFields` wrote them down;
    `write_quality` has already reduced them to one value per boundary face. This reads that.

    Same contract as `face_quality.attach_quality_fields`: `patch_of` maps a response patch name
    to the one the polyMesh uses, every failure is logged and swallowed, and a response that
    already has fields is left exactly as it is.
    """
    if resp.get("quality_fields"):
        return
    try:
        alias = dict(patch_of or {})
        names = [str(p.get("name") or "") for p in (resp.get("patches") or [])
                 if isinstance(p, dict)]
        fields = quality_fields_from_sidecar(
            polymesh, non_ortho_limit=non_ortho_limit, skew_limit=skew_limit,
            aspect_limit=aspect_limit, patch_names=[alias.get(n, n) for n in names])
        if not fields:
            return
        if alias:
            back = {mesh_name: shown for shown, mesh_name in alias.items()}
            fields["patches"] = {back.get(k, k): v
                                 for k, v in (fields.get("patches") or {}).items()}
            for h in fields.get("hotspots") or []:
                if h.get("patch") in back:
                    h["patch"] = back[h["patch"]]
        resp["quality_fields"] = fields
    except Exception:                       # noqa: BLE001 - a heatmap is never worth a delivery
        logger.exception("viewer quality fields from the sidecar unavailable - no heatmap")


__all__ = ["BoundaryQuality", "CHECKMESH_WRITE_FIELDS", "FIELDS", "FIELD_FILES",
           "MAX_HOTSPOTS_PER_METRIC", "attach_sidecar_quality",
           "quality_fields_from_sidecar",
           "QUALITY_DIRNAME", "QUALITY_VERSION", "QualityUnavailable", "has_quality",
           "open_quality", "quality_dir", "write_quality", "write_quality_decomposed"]
