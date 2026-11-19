# Responsibility: The fast path the product would run, measured against the truth: verify a whole simplification PACKAGE from ONE coarse global solve of the simplified part plus one small local solve per changed region, and compare the estimate with the package's true error (package.py).
# Boundaries: a developer tool run on the bench VM after harness.py and package.py; nothing in the product imports it. No solve of the original part as a whole is made - that is the point.
# Collaborates with: devtools/simplify/local.py (cutting, driving and solving a region), devtools/simplify/package.py (the true package error it is scored against), devtools/simplify/harness.py (prepare, recognise, remove, mesh_and_solve).
"""ONE COARSE SOLVE, N SMALL ONES.

    python devtools/simplify/package_local.py part.step --harness results/part [--package accepted]
           [--coarse 15] [--refine 4] [--jobs 16] [--threads 16]

1. Remove the package's features from the part in one defeaturing call.
2. Solve the SIMPLIFIED part once, coarse (size diagonal / COARSE), keeping its field: the only global
   solve, and of the cheaper of the two geometries.
3. Group the removed features into regions (padded boxes that overlap are merged), cut each region
   out of the original and the simplified part, drive both pieces' cut faces with the coarse field,
   solve them at h_global / REFINE, in parallel.
4. Estimate the package's compliance change as the sum over regions of -(W_s - W_o) / W_global.

Compared with the TRUE package change package.py measured (two global solves at the harness size), and
timed: the coarse solve, the regions, and what the truth cost.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402  (sibling scripts, not a package)
import local as L  # noqa: E402


def _regions(feats, pad_of) -> list[dict]:
    """Merge the padded boxes of the features until no two overlap."""
    boxes = [{"box": [f.bbox[k] - pad_of(f) for k in range(3)] + [f.bbox[k + 3] + pad_of(f) for k in range(3)],
              "members": [f.id]} for f in feats]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i]["box"], boxes[j]["box"]
                if all(a[k] <= b[k + 3] and b[k] <= a[k + 3] for k in range(3)):
                    boxes[i] = {"box": [min(a[k], b[k]) for k in range(3)] + [max(a[k + 3], b[k + 3]) for k in range(3)],
                                "members": boxes[i]["members"] + boxes[j]["members"]}
                    boxes.pop(j)
                    merged = True
                    break
            if merged:
                break
    return boxes


def _box_shape(b):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(*b[:3]), gp_Pnt(*b[3:])).Shape()


def _read_brep(path: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _region(spec: dict) -> dict:
    b = spec["box"]
    pb = spec["part_bb"]
    if all(b[k] <= pb[k] and b[k + 3] >= pb[k + 3] for k in range(3)):
        return {"ok": False, "why": "region covers the whole part"}
    box = _box_shape(b)
    # CUT THE ORIGINAL, THEN DEFEATURE THE PIECE. Measured on nist_ftc_09: cutting the defeatured
    # part with the box gave a shape OCCT itself calls invalid, which gmsh then meshed without raising
    # at a twelfth of the volume's tets. The original cuts cleanly; the package's features are then
    # found again INSIDE the piece (kind, radius, centroid) and removed there.
    lo = L._common(_read_brep(spec["orig"]), box)
    if lo is None:
        return {"ok": False, "why": "region cut failed"}
    diag = spec["diag"]
    found, faces, _ = H.recognise(lo, diag)
    want = spec["features"]
    take = [f for f in found if any(f.kind == w["kind"] and abs(f.size - w["size"]) <= 1e-6 * diag
                                    and sum((f.centroid[k] - w["centroid"][k]) ** 2 for k in range(3)) ** 0.5
                                    <= 1e-3 * diag for w in want)]
    if len(take) != len(want):
        return {"ok": False, "why": f"found {len(take)} of the region's {len(want)} features inside the piece"}
    ls, why = H.remove(lo, [faces[j] for f in take for j in f.faces])
    if ls is None:
        return {"ok": False, "why": f"defeaturing the piece: {why}"}
    d = Path(spec["dir"])
    d.mkdir(parents=True, exist_ok=True)
    H.write_brep(lo, d / "orig.brep")
    H.write_brep(ls, d / "simp.brep")
    drive = L._drive(Path(spec["field"]), b, spec["margin"])
    po = L._solve_piece(d / "orig.brep", d / "o", spec["h"], b, spec["tol"], drive, spec["lc"], spec["ho"])
    ps = L._solve_piece(d / "simp.brep", d / "s", spec["h"], b, spec["tol"], drive, spec["lc"], spec["ho"])
    if not (po["ok"] and ps["ok"]):
        return {"ok": False, "why": po.get("why") or ps.get("why")}
    return {"ok": True, "members": spec["members"], "W_o": po["energy"], "W_s": ps["energy"],
            "elements": po["elements"] + ps["elements"], "solve_s": po["solve_s"] + ps["solve_s"]}


def _isolated(item) -> tuple[int, dict]:
    """One region in its own process: gmsh keeps global state and is not thread-safe."""
    import subprocess
    i, spec = item
    res = spec.with_suffix(".out.json")
    res.unlink(missing_ok=True)
    try:
        p = subprocess.run([sys.executable, "-I", __file__, "--region", str(spec)], capture_output=True, text=True,
                           timeout=3600)
    except subprocess.TimeoutExpired:
        return i, {"ok": False, "why": "timed out"}
    if p.returncode != 0 or not res.exists():
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        return i, {"ok": False, "why": f"worker died: {tail[0][:200]}"}
    return i, json.loads(res.read_text())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--harness", required=True)
    ap.add_argument("--package", default="accepted", choices=("accepted", "all"))
    ap.add_argument("--budget", type=float, default=0.001)
    ap.add_argument("--coarse", type=float, default=15.0, help="the one global solve's size: diagonal / COARSE")
    ap.add_argument("--refine", type=float, default=2.0, help="region mesh size = harness h / REFINE")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--threads", type=int, default=16, help="solver threads for the one global solve")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--region"]:
        spec = Path(argv[1])
        spec.with_suffix(".out.json").write_text(json.dumps(_region(json.loads(spec.read_text())), default=float))
        return 0
    a = ap.parse_args(argv)
    hd = Path(a.harness)
    r = json.loads((hd / "result.json").read_text())
    truth = json.loads((hd / f"package_b{a.budget:g}.json").read_text()).get(a.package, {})
    if not truth.get("ok"):
        print(f"no true {a.package} package to compare with: {truth.get('why')}")
        return 1
    t0 = time.time()
    shape, diag, lc = H.prepare(a.step)
    feats, faces, _ = H.recognise(shape, diag)
    by_id = {f.id: f for f in feats}
    members = [by_id[i] for i in truth["members"]]
    simp, _ = H.remove(shape, [faces[j] for f in members for j in f.faces])
    if simp is None:
        print("the package did not remove in one call")
        return 1
    out = hd / "fast" / a.package
    (out / "geom").mkdir(parents=True, exist_ok=True)
    H.write_brep(simp, out / "geom" / "simplified.brep")
    t_geom = time.time() - t0

    # 2. the one global solve: the simplified part, coarse, on every core
    t1 = time.time()
    ho = r.get("ho_optimize", 1)
    g = H.mesh_and_solve(str(out / "geom" / "simplified.brep"), str(out / "global"), asdict(lc), diag / a.coarse,
                         {"none": []}, threads=a.threads, keep_field=True, ho_optimize=ho)
    t_global = time.time() - t1
    if not g.ok:
        print("coarse global solve failed:", g.why)
        return 1
    w_global = g.qoi["strain_energy"]

    # 3. regions, in parallel
    h = r["h"] / a.refine
    zpad = {f.id: 2.0 * f.size + 0.02 * diag for f in members}
    regions = _regions(members, lambda f: zpad[f.id] + 4 * h)
    tol = 1e-4 * diag
    lcd = asdict(lc)
    part_bb = H._bbox_of([shape])

    specs = []
    for i, reg in enumerate(regions):
        spec = out / f"r{i}.spec.json"
        spec.write_text(json.dumps({"orig": str(hd / "geom" / "original.brep"), "simp": str(out / "geom" / "simplified.brep"),
                                    "box": reg["box"], "members": reg["members"], "dir": str(out / f"r{i}"),
                                    "diag": diag, "features": [{"kind": by_id[m].kind, "size": by_id[m].size,
                                                                "centroid": list(by_id[m].centroid)}
                                                               for m in reg["members"]],
                                    "field": str(out / "global" / "field.npz"), "margin": 2 * diag / a.coarse,
                                    "h": h, "tol": tol, "lc": lcd, "ho": ho, "part_bb": list(part_bb)}))
        specs.append((i, spec))

    t2 = time.time()
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = dict(ex.map(_isolated, specs))
    t_regions = time.time() - t2
    ok = [v for v in res.values() if v["ok"]]
    est = sum(-(v["W_s"] - v["W_o"]) / w_global for v in ok)
    summary = {
        "package": a.package, "members": len(members), "regions": len(regions), "regions_ok": len(ok),
        "failed": {i: v["why"] for i, v in res.items() if not v["ok"]},
        "estimate": est, "truth": truth["compliance_change"], "ratio": est / truth["compliance_change"]
        if truth["compliance_change"] else None,
        "coarse_global_elements": g.elements, "region_elements": sum(v["elements"] for v in ok),
        "truth_elements_per_solve": r["original"]["elements"],
        "seconds": {"geometry": t_geom, "coarse_global": t_global, "regions": t_regions,
                    "total": time.time() - t0},
        "truth_seconds": {"original_solve": r["original"]["mesh_s"] + r["original"]["solve_s"]},
    }
    (out / "fast.json").write_text(json.dumps(summary, indent=1, default=float))
    ratio = "n/a" if summary["ratio"] is None else f"{summary['ratio']:.2f}"
    print(f"{a.package}: {len(members)} features in {len(regions)} regions ({len(ok)} ok): estimate {est:+.3%} "
          f"vs truth {truth['compliance_change']:+.3%} (ratio {ratio}); coarse global {g.elements} tets "
          f"{t_global:.1f}s, regions {t_regions:.1f}s, total {summary['seconds']['total']:.1f}s; truth's one "
          f"original solve alone {summary['truth_seconds']['original_solve']:.1f}s at {r['original']['elements']} tets"
          + ("" if math.isfinite(est) else " (estimate not finite)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
