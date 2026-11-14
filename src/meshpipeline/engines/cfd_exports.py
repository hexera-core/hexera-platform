# Responsibility: Write a finished flow mesh for the other solvers - the OpenFOAM case as one zip, an Ansys Fluent mesh and an EnSight case - on the mesher, with OpenFOAM's own converters, so the user picks the file at download.
# Owns: which flow downloads exist (EXPORT_ARTIFACTS), their file names, how each is made and the record of what was made (EXPORTS_FILE).
# Boundaries: runs on the mesher after the engine and before the workspace is archived (engines/dispatch.run_engine_local); it reads the delivered polyMesh and never changes it. A format that cannot be written is left out and said so - the mesh never fails over an export.
# Collaborates with: engines/bucket_files (a big export stays in the exchange bucket, like an FEA deck), engines/return_scope (an export too big to send home is left out), application/artifact_uploader.cfd_export_plan (one download each), api/v1/simulation._artifact_label (their names).
"""THE FLOW MESH, FOR EVERY SOLVER WE CAN WRITE IT FOR.

Mesh once, export many - the FEA route's pattern (engines/fea_writers.py), for a flow mesh:

  * OpenFOAM case (.zip): the case's constant/ and system/ folders, the polyMesh as built.
  * Ansys Fluent mesh (.msh): OpenFOAM's foamMeshToFluent (ASCII, 11 significant digits). Fluent's
    mesh file is face-based like the polyMesh, so polyhedra, prism layers and every named boundary
    patch carry over as they are; a wall patch is a wall zone, a symmetry plane a symmetry zone, any
    other patch a pressure-outlet zone for the user to set. A patch name a Fluent reader cannot take
    is written with each odd character as _ (fluent_zone_name; the record lists them).
  * EnSight (.case): OpenFOAM's foamToEnsight, the cells and one part per boundary patch, in
    EnSight Gold C binary - whose coordinates are 32-bit floats (about 7 significant digits).
Everything (the bundle) carries the Fluent and EnSight files as well; the case itself is already in it.

The two converted formats are written only for a single-region mesh: foamMeshToFluent has no
region option, and a multi-region case (snapgrid, snappy_multiregion) is offered as its OpenFOAM
case only. Each file is checked after it is written (the Fluent header must declare exactly the
polyMesh's points, faces and cells) and dropped when it fails. Neither converted format has been
opened in its own program yet - only read back by independent readers - and the labels say so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

NOT_TESTED = "not yet tested in the real solver"
#: The downloads a flow mesh offers, in menu order: (logical key, file, label). The label's part
#: after " - " is the caveat the result screen shows as a small tag (ui/js/viewer/result_panel.fileLabel).
EXPORT_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("export_openfoam_case", "export_openfoam_case.zip", "OpenFOAM case (.zip)"),
    ("export_fluent", "export_fluent.zip", f"Ansys Fluent mesh (.msh) - {NOT_TESTED}"),
    ("export_ensight", "export_ensight.zip", "EnSight case (.case) - not yet tested in EnSight"),
)
OPENFOAM_ZIP, FLUENT_ZIP, ENSIGHT_ZIP = (f for _k, f, _l in EXPORT_ARTIFACTS)
FILES: tuple[str, ...] = tuple(f for _k, f, _l in EXPORT_ARTIFACTS)
#: The record of what was written, how big and how long it took, beside the mesh.
EXPORTS_FILE = "cfd_exports.json"
#: Where the converters run: a scratch case whose polyMesh is a link to the delivered one, so no
#: converter output ever lands in the delivered case. Named `mesh` so the files inside the zips are
#: mesh.msh and mesh.case.
SCRATCH = "_exports"
_SCRATCH_CASE = "mesh"
#: The engines whose finished mesh is an OpenFOAM polyMesh (VMTK's under openfoam_case/).
ENGINES = frozenset({"snappy", "cfmesh", "netgen", "vmtk", "snapgrid", "snappy_multiregion"})
#: The guard each converter runs under. A 3 M-cell mesh converts in well under a minute; a
#: converter still running at this is stuck, and its format is left out.
GUARD_S = 1800
#: Below this much of the run's wall left, no export is started.
MIN_LEFT_S = 120
#: Deflate level of the zips: 1 is about three times as fast as 6 and a few per cent larger.
ZIP_LEVEL = 1

_CONTROL_DICT = """FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      controlDict;
}
application     none;
startFrom       latestTime;
startTime       0;
stopAt          endTime;
endTime         0;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
writeFormat     binary;
writePrecision  12;
timeFormat      general;
timePrecision   6;
"""
#: The two dictionaries foamToEnsight's mesh insists on reading; no field is solved.
_FV_SCHEMES = """FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
"""
_FV_SOLUTION = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }
"""


def case_root(workspace) -> Path | None:
    """The OpenFOAM case of a finished flow mesh: the workspace itself, or VMTK's openfoam_case/.
    None when there is none."""
    ws = Path(workspace)
    for root in (ws, ws / "openfoam_case"):
        if (root / "constant" / "polyMesh" / "owner").is_file() or (root / "constant" / "regionProperties").is_file():
            return root
    return None


def is_multi_region(root: Path) -> bool:
    return (Path(root) / "constant" / "regionProperties").is_file()


_NOTE = re.compile(rb"note\s+\"([^\"]*)\"")


def polymesh_counts(poly: Path) -> dict:
    """{points, faces, cells, internal_faces} of a polyMesh. From the owner file's header note when it
    has one ("nPoints:... nCells:... nFaces:... nInternalFaces:...", what OpenFOAM's own writers put
    there); cfMesh writes no note, and then the lists are counted (_counted). {} when unreadable."""
    try:
        with (Path(poly) / "owner").open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return {}
    m = _NOTE.search(head)
    if not m:
        return _counted(Path(poly))
    vals = dict(re.findall(r"n(\w+):\s*(\d+)", m.group(1).decode("ascii", "replace")))
    out = {"points": vals.get("Points"), "cells": vals.get("Cells"), "faces": vals.get("Faces"),
           "internal_faces": vals.get("InternalFaces")}
    return {k: int(v) for k, v in out.items() if v is not None}


_LIST_START = re.compile(rb"\}\s*(?://[^\n]*\n\s*)*(\d+)\s*\(")


def _list(path: Path):
    """An OpenFOAM label list file (owner, neighbour), ASCII or binary, as an int array."""
    import numpy as np
    raw = path.read_bytes()
    m = _LIST_START.search(raw)
    if not m:
        raise ValueError(f"{path.name}: no list")
    n, body = int(m.group(1)), m.end()
    binary = re.search(rb"format\s+binary", raw[:m.start()]) is not None
    if binary:
        wide = re.search(rb"label=64", raw[:m.start()]) is not None
        return np.frombuffer(raw, dtype=np.int64 if wide else np.int32, count=n, offset=body)
    end = raw.index(b")", body)
    vals = np.array(raw[body:end].split(), dtype=np.int64)
    if len(vals) != n:
        raise ValueError(f"{path.name}: {len(vals)} entries, header says {n}")
    return vals


def _counted(poly: Path) -> dict:
    """The counts read from the lists themselves: faces = the owner list's length, internal faces =
    the neighbour's entries that name a cell, cells = the highest cell either names plus one, points = the points list's
    declared length. {} when any cannot be read."""
    try:
        owner, neighbour = _list(poly / "owner"), _list(poly / "neighbour")
        with (poly / "points").open("rb") as fh:
            head = fh.read(8192)
        m = _LIST_START.search(head)
        if not m:
            return {}
        cells = int(max(owner.max(initial=-1), neighbour.max(initial=-1))) + 1
        # cfMesh's neighbour list runs over every face, -1 on the boundary
        return {"points": int(m.group(1)), "faces": int(len(owner)), "cells": cells,
                "internal_faces": int((neighbour >= 0).sum())}
    except (OSError, ValueError):
        return {}


_FLUENT_DECL = re.compile(rb"\((10|12|13)\s+\(0\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)")


def fluent_counts(msh: Path) -> dict:
    """{points, faces, cells} as a Fluent mesh's header declares them - the zone-0 declarations
    "(10 (0 1 N ...))", "(13 (0 1 N ...))", "(12 (0 1 N ...))", hexadecimal - read from its first
    64 KiB."""
    with Path(msh).open("rb") as fh:
        head = fh.read(65536)
    names = {b"10": "points", b"13": "faces", b"12": "cells"}
    out: dict = {}
    for kind, first, last in _FLUENT_DECL.findall(head):
        out.setdefault(names[kind], int(last, 16) - int(first, 16) + 1)
    return out


def _run(cmd: str, case: Path, *, bashrc: str, timeout: int) -> tuple[int, str]:
    from meshpipeline.sandbox.safe_exec import run_guarded, scrubbed_subprocess_env
    proc = run_guarded(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && {cmd}"], cwd=str(case),
                       env=scrubbed_subprocess_env(), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-1500:]


def _zip(dest: Path, entries: list[tuple[Path, str]]) -> int:
    """`entries` (file, name in the zip) written to `dest` through a temporary name, so a zip that is
    there is a whole one. Returns its bytes."""
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=ZIP_LEVEL,
                         allowZip64=True) as zf:
        for src, name in entries:
            zf.write(src, arcname=name)
    os.replace(tmp, dest)
    return dest.stat().st_size


_ZONE_UNSAFE = re.compile(r"[^A-Za-z0-9_.:\-]")
_ZONE_LINE = re.compile(rb"^\((39|45) \((\d+) (\S+) (\S+)\)\(\)\)$")
#: How far from its end a Fluent mesh's zone names start: foamMeshToFluent writes them last, one
#: short line a zone.
_TAIL_BYTES = 4 * 1024 * 1024


def fluent_zone_name(name: str) -> str:
    """A patch name as a Fluent zone may carry it: letters, digits and _ . : - (any other character
    becomes _). foamMeshToFluent writes the patch name as it is, and a reader stops at the first
    character it does not take - a "%" in a snappy region name stopped fluent3DMeshToFoam (lab,
    2026-11-14)."""
    return _ZONE_UNSAFE.sub("_", str(name)) or "zone"


def _zip_fluent(msh: Path, dest: Path, arcname: str) -> tuple[int, dict]:
    """The Fluent mesh into its zip, streamed, with every zone name made safe (fluent_zone_name) and
    kept unique. Returns (zip bytes, {old name: new name} for each one changed)."""
    size = msh.stat().st_size
    start = max(0, size - _TAIL_BYTES)
    with msh.open("rb") as fh:
        fh.seek(start)
        tail = fh.read()
    lines = tail.split(b"\n")
    # the zone-name lines are the file's last lines: find where they begin
    k = len(lines)
    while k > 0 and (not lines[k - 1].strip() or _ZONE_LINE.match(lines[k - 1].strip())):
        k -= 1
    renamed: dict = {}
    zone_lines = [_ZONE_LINE.match(ln.strip()) for ln in lines[k:]]
    # a name that is already safe never changes; an unsafe one takes the next free safe spelling
    taken = {m.group(4).decode("utf-8", "replace") for m in zone_lines if m} & {
        fluent_zone_name(m.group(4).decode("utf-8", "replace")) for m in zone_lines if m}
    fixed = []
    for ln, m in zip(lines[k:], zone_lines, strict=True):
        if not m:
            fixed.append(ln)
            continue
        old = m.group(4).decode("utf-8", "replace")
        new = fluent_zone_name(old)
        if new != old:
            base, n = new, 1
            while new in taken:
                n += 1
                new = f"{base}_{n}"
            taken.add(new)
            renamed[old] = new
        fixed.append(b"(%s (%s %s %s)())" % (m.group(1), m.group(2), m.group(3), new.encode()))
    keep = start + len(b"\n".join(lines[:k])) + (1 if k else 0)
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=ZIP_LEVEL,
                         allowZip64=True) as zf, msh.open("rb") as src, \
            zf.open(arcname, "w", force_zip64=True) as out:
        left = keep
        while left > 0:
            chunk = src.read(min(8 * 1024 * 1024, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
        out.write(b"\n".join(fixed))
    os.replace(tmp, dest)
    return dest.stat().st_size, renamed


def _tree(root: Path, top: str, prefix: str) -> list[tuple[Path, str]]:
    base = root / top
    if not base.is_dir():
        return []
    return [(p, f"{prefix}/{p.relative_to(root).as_posix()}")
            for p in sorted(base.rglob("*")) if p.is_file() and not p.name.endswith(".part")]


_MESH_FILES = ("points", "faces", "owner", "neighbour")


def mesh_fingerprint(workspace) -> str:
    """What identifies the delivered mesh: every polyMesh's file sizes and its boundary file's bytes
    (every region's, for a multi-region case). "" when there is no case. Cheap - no mesh file is read
    but the small boundary."""
    root = case_root(workspace)
    if root is None:
        return ""
    h = hashlib.sha256()
    for poly in sorted(p for p in root.glob("constant/**/polyMesh") if p.is_dir()):
        rel = poly.relative_to(root).as_posix()
        for name in _MESH_FILES:
            f = poly / name
            h.update(f"{rel}/{name}:{f.stat().st_size if f.is_file() else -1};".encode())
        b = poly / "boundary"
        if b.is_file():
            h.update(b.read_bytes())
    return h.hexdigest()[:24]


def offered(workspace, held: dict | None = None) -> list[tuple[str, str, str]]:
    """The downloads (logical key, file, label) that belong to the mesh in `workspace` NOW: the
    record must name this mesh (a later pass's files never ride with an earlier pass's mesh put back,
    engines/snappy/drivers._restore_sound_mesh) and each file must be the very one recorded - on disk,
    or kept in the exchange bucket (`held`, engines/bucket_files.kept) - by its size."""
    ws = Path(workspace)
    try:
        record = json.loads((ws / EXPORTS_FILE).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(record, dict) or not record.get("mesh") or record.get("mesh") != mesh_fingerprint(ws):
        return []
    files = record.get("files")
    if not isinstance(files, dict):
        files = {}
    out = []
    for key, name, label in EXPORT_ARTIFACTS:
        want = (files.get(name) or {}).get("bytes")
        if not isinstance(want, int) or want <= 0:
            continue
        p = ws / name
        if p.is_file() and p.stat().st_size == want:
            out.append((key, name, label))
        elif held and name in held and int(getattr(held[name], "bytes", -1)) == want:
            out.append((key, name, label))
    return out


def clear(workspace) -> None:
    """Remove every export and the scratch case: a previous pass's file never stands for this one."""
    ws = Path(workspace)
    for name in (*FILES, EXPORTS_FILE):
        (ws / name).unlink(missing_ok=True)
        (ws / (name + ".part")).unlink(missing_ok=True)
    shutil.rmtree(ws / SCRATCH, ignore_errors=True)


def write_exports(workspace, *, bashrc: str, guard_s: int = GUARD_S) -> dict:
    """Write the flow downloads of the finished mesh in `workspace` (the module text). Returns the
    record also written to EXPORTS_FILE: {files: {file: {bytes, seconds}}, left_out: {file: why},
    seconds}. Never raises for a format: what cannot be written is left out."""
    ws = Path(workspace)
    t_all = time.monotonic()
    deadline = t_all + float(guard_s)        # all of it, both converters together

    def left() -> int:
        return max(1, int(deadline - time.monotonic()))

    clear(ws)
    record: dict = {"files": {}, "left_out": {}, "mesh": mesh_fingerprint(ws)}
    root = case_root(ws)
    if root is None:
        record["left_out"] = dict.fromkeys(FILES, "no OpenFOAM case beside the mesh")
        return _finish(ws, record, t_all)

    # THE OPENFOAM CASE: constant/ and system/ as built, under one folder
    t = time.monotonic()
    try:
        entries = _tree(root, "constant", "openfoam_case") + _tree(root, "system", "openfoam_case")
        n = _zip(ws / OPENFOAM_ZIP, entries)
        record["files"][OPENFOAM_ZIP] = {"bytes": n, "seconds": round(time.monotonic() - t, 1)}
    except Exception as exc:  # noqa: BLE001 - an export is never a reason to lose the mesh
        logger.warning("cfd exports: the OpenFOAM case zip was not written", exc_info=True)
        (ws / (OPENFOAM_ZIP + ".part")).unlink(missing_ok=True)
        record["left_out"][OPENFOAM_ZIP] = f"{type(exc).__name__}: {exc}"[:300]

    if is_multi_region(root):
        for f in (FLUENT_ZIP, ENSIGHT_ZIP):
            record["left_out"][f] = "a multi-region mesh is delivered as its OpenFOAM case only"
        return _finish(ws, record, t_all)

    poly = root / "constant" / "polyMesh"
    counts = polymesh_counts(poly)
    case = ws / SCRATCH / _SCRATCH_CASE
    try:
        (case / "system").mkdir(parents=True, exist_ok=True)
        (case / "constant").mkdir(parents=True, exist_ok=True)
        (case / "system" / "controlDict").write_text(_CONTROL_DICT)
        (case / "system" / "fvSchemes").write_text(_FV_SCHEMES)
        (case / "system" / "fvSolution").write_text(_FV_SOLUTION)
        (case / "constant" / "polyMesh").symlink_to(poly.resolve(), target_is_directory=True)
    except OSError as exc:
        for f in (FLUENT_ZIP, ENSIGHT_ZIP):
            record["left_out"][f] = f"scratch case: {exc}"[:300]
        shutil.rmtree(ws / SCRATCH, ignore_errors=True)
        return _finish(ws, record, t_all)

    # ANSYS FLUENT: foamMeshToFluent writes fluentInterface/<case>.msh
    t = time.monotonic()
    try:
        rc, tail = _run("foamMeshToFluent", case, bashrc=bashrc, timeout=left())
        msh = case / "fluentInterface" / f"{_SCRATCH_CASE}.msh"
        if rc != 0 or not msh.is_file() or msh.stat().st_size == 0:
            raise RuntimeError(f"foamMeshToFluent rc {rc}: {tail[-300:]}")
        got = fluent_counts(msh)
        want = {k: counts[k] for k in ("points", "faces", "cells") if k in counts}
        if not want or any(got.get(k) != v for k, v in want.items()):
            raise RuntimeError(f"the Fluent header declares {got}, the polyMesh has {want}")
        n, renamed = _zip_fluent(msh, ws / FLUENT_ZIP, f"{_SCRATCH_CASE}.msh")
        record["files"][FLUENT_ZIP] = {"bytes": n, "msh_bytes": msh.stat().st_size,
                                       "seconds": round(time.monotonic() - t, 1)}
        if renamed:
            record["files"][FLUENT_ZIP]["renamed_zones"] = renamed
        msh.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cfd exports: the Fluent mesh was not written: %s", exc)
        (ws / (FLUENT_ZIP + ".part")).unlink(missing_ok=True)
        record["left_out"][FLUENT_ZIP] = f"{type(exc).__name__}: {exc}"[:300]

    # ENSIGHT: foamToEnsight writes EnSight/<case>.case and its geometry, one part per patch
    t = time.monotonic()
    try:
        rc, tail = _run("foamToEnsight -constant -no-fields -no-lagrangian", case, bashrc=bashrc,
                        timeout=left())
        out = case / "EnSight"
        cases = sorted(out.glob("*.case")) if out.is_dir() else []
        if rc != 0 or not cases:
            raise RuntimeError(f"foamToEnsight rc {rc}: {tail[-300:]}")
        entries = [(p, f"ensight/{p.relative_to(out).as_posix()}")
                   for p in sorted(out.rglob("*")) if p.is_file()]
        n = _zip(ws / ENSIGHT_ZIP, entries)
        record["files"][ENSIGHT_ZIP] = {"bytes": n, "seconds": round(time.monotonic() - t, 1)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("cfd exports: the EnSight case was not written: %s", exc)
        (ws / (ENSIGHT_ZIP + ".part")).unlink(missing_ok=True)
        record["left_out"][ENSIGHT_ZIP] = f"{type(exc).__name__}: {exc}"[:300]

    shutil.rmtree(ws / SCRATCH, ignore_errors=True)
    return _finish(ws, record, t_all)


def _finish(ws: Path, record: dict, t0: float) -> dict:
    record["seconds"] = round(time.monotonic() - t0, 1)
    try:
        (ws / EXPORTS_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    except OSError:
        pass
    if record["files"]:
        logger.info("cfd exports: %s in %.1f s", ", ".join(sorted(record["files"])), record["seconds"])
    return record


def after_run(workspace, *, engine: str, result, bashrc: str, left_s: float | None = None) -> dict | None:
    """The mesher's hook (engines/dispatch.run_engine_local): the exports of a flow mesh that was
    built, inside what is left of the run's wall (`left_s`) - the converters never cost a mesh its
    way home. A failed run's workspace only loses a previous pass's exports. Never raises."""
    try:
        if engine not in ENGINES:
            return None
        ok = isinstance(result, dict) and result.get("rc") == 0 and not result.get("timed_out")
        guard = int(min(GUARD_S, GUARD_S if left_s is None else left_s))
        if not ok or guard < MIN_LEFT_S:
            clear(workspace)
            if ok:
                logger.warning("cfd exports: %s s left of the run's wall - not written", guard)
            return None
        return write_exports(workspace, bashrc=bashrc, guard_s=guard)
    except Exception:  # noqa: BLE001 - an export must never lose a finished mesh
        logger.warning("cfd exports: not written", exc_info=True)
        return None


__all__ = ["ENGINES", "ENSIGHT_ZIP", "EXPORTS_FILE", "EXPORT_ARTIFACTS", "FILES", "FLUENT_ZIP", "GUARD_S",
           "MIN_LEFT_S", "NOT_TESTED", "OPENFOAM_ZIP", "SCRATCH", "ZIP_LEVEL", "after_run", "case_root", "clear",
           "fluent_counts", "fluent_zone_name", "is_multi_region", "mesh_fingerprint", "offered", "polymesh_counts",
           "write_exports"]
