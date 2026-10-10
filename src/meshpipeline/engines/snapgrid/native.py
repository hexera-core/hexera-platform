# Responsibility: Cut a snap-grid mesh into one polyMesh per region with OpenFOAM's own splitMeshRegions, check every region with checkMesh, and prove the interfaces match the grid's.
# Owns: the native stage order (splitMeshRegions -cellZonesOnly, then checkMesh per region), the per-region quality numbers and the interface reconciliation.
# Boundaries: runs OpenFOAM in a scrubbed, time-bounded environment; it judges only what the native tools report against what the grid wrote.
# Collaborates with: engines/snapgrid/mesher.py (writes the case), engines/snapgrid/cli.py, the mesh image (OpenFOAM v2412).
"""splitMeshRegions + checkMesh on a snap-grid case.

The grid wrote one polyMesh whose every cell is in a cellZone. `splitMeshRegions -cellZonesOnly
-overwrite` cuts it into constant/<region>/polyMesh (one per zone - no walking, so a part that an
overlap left in two pieces stays one region) and turns every face between two zones into a pair of
mappedWall patches `<a>_to_<b>` / `<b>_to_<a>`, which is what a conjugate solver couples across.
Because both sides of each interface are the same grid faces, their face counts must be equal and
equal to the count the grid wrote; this is checked. checkMesh then reads every region.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: (command, log name). ORDER IS CONTRACT.
SPLIT = ("splitMeshRegions -cellZonesOnly -overwrite", "splitMeshRegions")

_CM = {
    "cells": re.compile(r"cells:\s+(\d+)"),
    "faces": re.compile(r"faces:\s+(\d+)"),
    "hexahedra": re.compile(r"hexahedra:\s+(\d+)"),
    "polyhedra": re.compile(r"polyhedra:\s+(\d+)"),
    "max_non_ortho": re.compile(r"non-orthogonality Max:\s+([\d.eE+-]+)"),
    "max_skewness": re.compile(r"[Mm]ax skewness\s*=\s*([\d.eE+-]+)"),
    "max_aspect_ratio": re.compile(r"Max aspect ratio\s*=\s*([\d.eE+-]+)"),
    "skew_faces": re.compile(r"(\d+)\s+highly skew faces"),
}
_FATAL = (("negative volume", "negative-volume cells"), ("open cell", "open cells"),
          ("incorrectly oriented", "incorrectly oriented faces"))


def default_bashrc() -> str:
    """The OpenFOAM environment to source: the setting (the mesh image's v2412), else a local
    install."""
    import meshpipeline.settings.runtime as rtcfg

    for cand in (rtcfg.OPENFOAM_BASHRC, "/usr/lib/openfoam/openfoam2412/etc/bashrc",
                 "/opt/openfoam11/etc/bashrc"):
        if cand and Path(cand).is_file():
            return cand
    return rtcfg.OPENFOAM_BASHRC


def dialect_of(bashrc: str | None = None) -> str:
    """Which OpenFOAM the bashrc sets up: ESI's releases are named by year and month
    (openfoam2412), the Foundation's by number (openfoam11)."""
    path = bashrc or default_bashrc()
    return "esi" if re.search(r"openfoam(v?\d{4})(/|$)", path) else "foundation"


def _run(case: Path, command: str, log: str, *, bashrc: str, timeout: int) -> tuple[int, str]:
    from meshpipeline.sandbox.safe_exec import run_guarded, scrubbed_subprocess_env

    full = f"source {bashrc} >/dev/null 2>&1 && {command}"
    try:
        p = run_guarded(["bash", "-lc", full], cwd=str(case), env=scrubbed_subprocess_env(),
                        capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        (case / f"log.{log}").write_text(f"TIMEOUT after {timeout} s\n")
        return -1, "timeout"
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    (case / f"log.{log}").write_text(out)
    return p.returncode, out


def parse_check_mesh(out: str) -> dict:
    q: dict = {"mesh_ok": "Mesh OK" in out}
    for k, pat in _CM.items():
        m = pat.search(out)
        if m:
            v = m.group(1)
            q[k] = float(v) if any(c in v for c in ".eE") else int(v)
    q["fatal"] = [label for mark, label in _FATAL if mark in out.lower()]
    q.setdefault("skew_faces", 0)
    return q


def _boundary_counts(path: Path) -> dict[str, tuple[str, int]]:
    if not path.exists():
        return {}
    txt = path.read_text(errors="replace")
    out: dict[str, tuple[str, int]] = {}
    for m in re.finditer(r"([A-Za-z0-9_]+)\s*\{(.*?)\}", txt, re.S):
        nf = re.search(r"nFaces\s+(\d+)\s*;", m.group(2))
        ty = re.search(r"\btype\s+([A-Za-z]+)\s*;", m.group(2))
        if nf:
            out[m.group(1)] = (ty.group(1) if ty else "", int(nf.group(1)))
    return out


#: Regions checked one by one (in parallel) when the case has at most this many; above it the
#: whole mesh is checked once and the regions by their counts (every region is cut from it).
CHECK_EACH_REGION_UP_TO = 64


def check(case, *, bashrc: str | None = None, timeout: int = 3600, jobs: int = 4,
          each_region: bool | None = None, keep_merged: bool = False) -> dict:
    """Check a case mesher.mesh_ecxml wrote (its regions already written, no split needed):
    checkMesh on the whole mesh (every cell and face the regions are cut from), then every region
    (in parallel, `jobs` at a time) when there are few enough, and every interface's two sides
    against each other and the grid. `ok` when nothing is fatal, every declared region has a mesh
    and no other, and every interface matches."""
    from concurrent.futures import ThreadPoolExecutor

    case = Path(case)
    bashrc = bashrc or default_bashrc()
    report = json.loads((case / "snapgrid_report.json").read_text())
    regions = report["fluid_regions"] + report["solid_regions"]
    result: dict = {"ok": False, "regions": {}, "interfaces": [], "problems": [],
                    "method": "regions written directly; checkMesh on the whole mesh"}
    rc, out = _run(case, "checkMesh -constant", "checkMesh", bashrc=bashrc, timeout=timeout)
    whole = parse_check_mesh(out)
    whole["rc"] = rc
    result["whole"] = whole
    if rc != 0 or whole["fatal"]:
        result["problems"].append(f"checkMesh on the whole mesh: rc={rc}, fatal={whole['fatal']}")
    present = sorted(d.name for d in (case / "constant").iterdir()
                     if d.is_dir() and (d / "polyMesh" / "owner").exists() and d.name != "polyMesh")
    missing = [r for r in regions if r not in present]
    undeclared = [r for r in present if r not in regions]
    if missing:
        result["problems"].append(f"regions without a mesh: {missing}")
    if undeclared:
        result["problems"].append(f"regions nobody declared: {undeclared}")
    meshes = report.get("region_meshes") or {}
    if each_region is None:
        each_region = len(present) <= CHECK_EACH_REGION_UP_TO
    if each_region:
        result["method"] += f"; checkMesh on each of the {len(present)} regions"

        def one(r: str):
            rc_, out_ = _run(case, f"checkMesh -constant -region {r}", f"checkMesh.{r}",
                             bashrc=bashrc, timeout=timeout)
            q_ = parse_check_mesh(out_)
            q_["rc"] = rc_
            return r, q_

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for r, q in pool.map(one, present):
                result["regions"][r] = q
                if q["rc"] != 0 or q["fatal"]:
                    result["problems"].append(f"checkMesh on {r}: rc={q['rc']}, fatal={q['fatal']}")
    else:
        for r in present:
            result["regions"][r] = {"cells": (meshes.get(r) or {}).get("cells")}
    total = sum(int((meshes.get(r) or {}).get("cells") or 0) for r in present)
    result["cells"] = total
    if total != report["polymesh"]["cells"] or (whole.get("cells") not in (None, total)):
        result["problems"].append(f"the regions hold {total:,} cells, the grid wrote "
                                  f"{report['polymesh']['cells']:,}, checkMesh read "
                                  f"{whole.get('cells')}")
    for row in report["interfaces"]:
        a, b, want = row["a"], row["b"], row["faces"]
        ab = (meshes.get(a) or {}).get("interfaces", {}).get(f"{a}_to_{b}")
        ba = (meshes.get(b) or {}).get("interfaces", {}).get(f"{b}_to_{a}")
        files = (_boundary_counts(case / "constant" / a / "polyMesh" / "boundary").get(f"{a}_to_{b}"),
                 _boundary_counts(case / "constant" / b / "polyMesh" / "boundary").get(f"{b}_to_{a}"))
        rec = {"a": a, "b": b, "kind": row["kind"], "grid_faces": want, "faces_a": ab,
               "faces_b": ba, "type_a": (files[0] or ("", 0))[0], "type_b": (files[1] or ("", 0))[0]}
        rec["ok"] = ab == want and ba == want and all(f is not None and f[1] == want for f in files)
        result["interfaces"].append(rec)
        if not rec["ok"]:
            result["problems"].append(f"interface {a}/{b}: the grid has {want} faces, the regions "
                                      f"{ab} and {ba}")
    q_all = [whole] + [q for q in result["regions"].values() if "max_non_ortho" in q]
    worst = [q.get("max_non_ortho") for q in q_all if q.get("max_non_ortho") is not None]
    result["max_non_ortho"] = max(worst) if worst else None
    skew = [q.get("max_skewness") for q in q_all if q.get("max_skewness") is not None]
    result["max_skewness"] = max(skew) if skew else None
    faces = whole.get("faces") or 0
    result["skew_faces"] = whole.get("skew_faces", 0)
    result["skew_fraction"] = (whole.get("skew_faces", 0) / faces) if faces else 0.0
    result["ok"] = not result["problems"]
    if result["ok"] and not keep_merged:
        # the whole mesh is every region again: the delivered case is the regions
        shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
    report["native"] = result
    (case / "snapgrid_report.json").write_text(json.dumps(report, indent=1, default=float))
    return result


def split_and_check(case, *, bashrc: str | None = None, timeout: int = 3600,
                    keep_merged: bool = False) -> dict:
    """Run the native stages on a case mesher.mesh_ecxml wrote; return what they found:
    `ok` only when the split wrote every region, every region passes checkMesh with no fatal
    defect, and every interface carries the face count the grid wrote on both sides."""
    case = Path(case)
    bashrc = bashrc or default_bashrc()
    report = json.loads((case / "snapgrid_report.json").read_text())
    regions = report["fluid_regions"] + report["solid_regions"]
    result: dict = {"ok": False, "regions": {}, "interfaces": [], "problems": []}
    rc, _out = _run(case, SPLIT[0], SPLIT[1], bashrc=bashrc, timeout=timeout)
    result["split_rc"] = rc
    if rc != 0:
        result["problems"].append(f"splitMeshRegions failed (rc={rc}); see log.splitMeshRegions")
        return result
    missing = [r for r in regions if not (case / "constant" / r / "polyMesh" / "owner").exists()]
    present = sorted(d.name for d in (case / "constant").iterdir()
                     if d.is_dir() and (d / "polyMesh" / "owner").exists())
    undeclared = [r for r in present if r not in regions]
    if missing:
        result["problems"].append(f"regions without a mesh after the split: {missing}")
    if undeclared:
        result["problems"].append(f"regions the split made that the grid never declared: "
                                  f"{undeclared}")
    bounds = {r: _boundary_counts(case / "constant" / r / "polyMesh" / "boundary")
              for r in present}
    total = 0
    for r in present:
        rc, out = _run(case, f"checkMesh -constant -region {r}", f"checkMesh.{r}",
                       bashrc=bashrc, timeout=timeout)
        q = parse_check_mesh(out)
        q["rc"] = rc
        result["regions"][r] = q
        total += int(q.get("cells") or 0)
        if rc != 0 or q["fatal"]:
            result["problems"].append(f"checkMesh on {r}: rc={rc}, fatal={q['fatal']}")
    result["cells"] = total
    if total != report["polymesh"]["cells"]:
        result["problems"].append(f"the regions hold {total:,} cells, the grid wrote "
                                  f"{report['polymesh']['cells']:,}")
    for row in report["interfaces"]:
        a, b, want = row["a"], row["b"], row["faces"]
        ab = bounds.get(a, {}).get(f"{a}_to_{b}", ("", None))
        ba = bounds.get(b, {}).get(f"{b}_to_{a}", ("", None))
        rec = {"a": a, "b": b, "kind": row["kind"], "grid_faces": want, "faces_a": ab[1],
               "faces_b": ba[1], "type_a": ab[0], "type_b": ba[0]}
        rec["ok"] = ab[1] == want and ba[1] == want
        result["interfaces"].append(rec)
        if not rec["ok"]:
            result["problems"].append(f"interface {a}/{b}: the grid wrote {want} faces, the split "
                                      f"has {ab[1]} and {ba[1]}")
    worst = [q.get("max_non_ortho") for q in result["regions"].values()
             if q.get("max_non_ortho") is not None]
    result["max_non_ortho"] = max(worst) if worst else None
    skew = [q.get("max_skewness") for q in result["regions"].values()
            if q.get("max_skewness") is not None]
    result["max_skewness"] = max(skew) if skew else None
    result["ok"] = not result["problems"]
    if result["ok"] and not keep_merged:
        # the merged mesh is every region again: the delivered case is the split one
        shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
    report["native"] = result
    (case / "snapgrid_report.json").write_text(json.dumps(report, indent=1, default=float))
    return result


__all__ = ["SPLIT", "default_bashrc", "parse_check_mesh", "split_and_check"]
