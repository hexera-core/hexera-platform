#!/usr/bin/env python3
# Responsibility: Calibrate the in-loop mesh probe against the mesh-failure-real track: for each
#                 file report what gmsh says of the upload, of OCP's re-export of it, and of the
#                 in-loop (OCP read, BREP) path, then say where they agree.
# Owns: the manifest loader, resolve-by-sha256, the agreement rule and the calibration tables.
# Boundaries: runs gmsh only inside `probe_file`'s child; OCP only to read and re-export a STEP.
#             It repairs nothing and commits no CAD - files are found by hash under --roots.
"""Three probes per file, because OCP's STEP reader and gmsh's disagree on some files.

  file      probe_file on the original upload (what the track's "before" means)
  reexport  OCP reads it, STEPControl_Writer writes it, probe_file on that (what repair delivers
            unchanged)
  loop      OCP reads it, `mesh_defects` (BREP) - what the in-loop probe sees

`agreement` compares loop with reexport: the same shape, through two routes into gmsh. file vs
reexport is reported separately as the "reader effect".
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

from meshpipeline.cad.repair.mesh_probe import (
    MeshFailure,
    ProbeResult,
    mesh_defects,
    probe_file,
)

TRACK = "mesh-failure-real"
BUDGET_S = 120.0
WORKERS = 6
#: More disagreements than this and the in-loop probe is not measuring what the track scores.
MAX_DISAGREEMENTS = 8


def load_manifest(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("track") != TRACK:
        raise ValueError(f"{path} is not a {TRACK} manifest")
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_inputs(manifest: dict, roots: list[Path]) -> tuple[dict[str, Path], list[str]]:
    """Find each manifest file under `roots` by sha256, whatever it is called there.

    Returns ({name: path}, [names not found]); a missing file is listed, never skipped silently.
    """
    wanted = {f["sha256"]: f["name"] for f in manifest["files"]}
    found: dict[str, Path] = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or len(found) == len(wanted):
                continue
            try:
                name = wanted.get(_sha256(path))
            except OSError:
                continue
            if name is not None and name not in found:
                found[name] = path
    return found, [f["name"] for f in manifest["files"] if f["name"] not in found]


def agreement(file_result: ProbeResult, loop_result: ProbeResult) -> bool:
    """Both meshed, or both failed (any kinds): the binary verdict, which gates calibration.

    The track scores the file probe on the delivered STEP, so the in-loop probe only steers where
    repair looks; whether the part meshes is what it must get right. `same_kind` is the stricter,
    reported-only comparison.
    """
    return file_result.meshed == loop_result.meshed


def same_kind(file_result: ProbeResult, loop_result: ProbeResult) -> bool:
    """Secondary, non-gating: both meshed, or both failed with the same kind."""
    if file_result.meshed or loop_result.meshed:
        return file_result.meshed and loop_result.meshed
    assert file_result.failure is not None and loop_result.failure is not None
    return file_result.failure.kind == loop_result.failure.kind


def _verdict(result: ProbeResult) -> str:
    return "meshes" if result.meshed else (result.failure.kind if result.failure else "other")


def _reexport(shape, out: Path) -> bool:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    return writer.Write(str(out)) == IFSelect_RetDone


def _loop_result(defects) -> ProbeResult:
    if not defects:
        return ProbeResult(True, 1, None, {})
    first = defects[0]
    kind = str(getattr(first.code, "value", first.code)).removeprefix("mesh_")
    return ProbeResult(False, 0, MeshFailure(kind, (), None, ""), {})


def _diagonal(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def _one(task: tuple[str, str, float | None]) -> dict:
    name, path_str, diag = task
    path = Path(path_str)
    started = time.monotonic()
    row: dict = {"name": name}
    try:
        from meshpipeline.cad.repair.brep import _read_shape

        shape = _read_shape(path)
        if diag is None:
            # The corpus build's child died before it measured this part: take OCP's box.
            diag = _diagonal(shape)
            row["diag_from_ocp"] = round(diag, 1)
        original = probe_file(path, diagonal_mm=diag, budget_s=BUDGET_S)
        row["file"] = _verdict(original)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "reexport.step"
            if _reexport(shape, out):
                row["reexport"] = _verdict(probe_file(out, diagonal_mm=diag, budget_s=BUDGET_S))
            else:
                row["reexport"] = "reexport_failed"
        defects = mesh_defects(shape, diagonal_mm=diag, budget_s=BUDGET_S)
        row["loop"] = _verdict(_loop_result(defects))
        row["loop_located"] = [bool(d.fingerprint) for d in defects]
    except Exception as exc:  # noqa: BLE001 - one bad file is a row, not a dead run
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and a == b and not a.endswith("_failed")


def summarise(rows: list[dict], missing: list[str]) -> dict:
    complete = [r for r in rows if "error" not in r and "reexport" in r]
    loop_vs_reexport = [r for r in complete if (r["loop"] == "meshes") != (r["reexport"] == "meshes")]
    kind_swaps = [r for r in complete if r not in loop_vs_reexport
                  and not _same(r["loop"], r["reexport"])]
    reader_effect = [r for r in complete if not _same(r["file"], r["reexport"])]
    return {
        "files": len(rows) + len(missing),
        "verdicts": len(complete),
        "missing": missing,
        "errors": [r for r in rows if "error" in r],
        "loop_agrees_reexport": len(complete) - len(loop_vs_reexport),
        "loop_disagrees_reexport": loop_vs_reexport,
        "kind_swaps_both_fail": kind_swaps,
        "reader_effect": reader_effect,
        "stop_rule_tripped": len(loop_vs_reexport) > MAX_DISAGREEMENTS,
    }


def _table(rows: list[dict], cols: tuple[str, ...]) -> str:
    lines = ["| " + " | ".join(("name",) + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    lines += ["| " + " | ".join([r["name"]] + [str(r.get(c, "")) for c in cols]) + " |"
              for r in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--roots", type=Path, nargs="+", required=True)
    parser.add_argument("--json", type=Path, dest="json_out")
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    found, missing = resolve_inputs(manifest, args.roots)
    diag = {f["name"]: f["diag_mm"] for f in manifest["files"]}
    tasks = [(n, str(p), diag[n]) for n, p in found.items()]
    started = time.monotonic()
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = sorted(pool.map(_one, tasks), key=lambda r: r["name"])
    summary = summarise(rows, missing)
    summary["wall_seconds"] = round(time.monotonic() - started, 1)

    print(_table(rows, ("file", "reexport", "loop")))
    print(f"\nloop vs reexport (binary): agree {summary['loop_agrees_reexport']}/{summary['files']}")
    for r in summary["loop_disagrees_reexport"]:
        print(f"  DISAGREE {r['name']}: reexport={r['reexport']} loop={r['loop']}")
    for r in summary["kind_swaps_both_fail"]:
        print(f"  kind swap (not gating) {r['name']}: reexport={r['reexport']} loop={r['loop']}")
    print(f"reader effect (file vs reexport): {len(summary['reader_effect'])} files")
    for r in summary["reader_effect"]:
        print(f"  {r['name']}: file={r['file']} reexport={r['reexport']}")
    for name in missing:
        print(f"  MISSING {name}")
    if args.json_out:
        args.json_out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1) + "\n")
    return 1 if summary["stop_rule_tripped"] or missing or summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
