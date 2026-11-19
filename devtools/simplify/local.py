# Responsibility: Verification tiers T1 and T2 of CAD simplification, scored against the harness's own ground truth: for each feature the harness removed, (T2) cut a box around it out of the original and the simplified part, drive the cut faces with the simplified part's global displacements, solve both pieces and compare; (T1, holes) read the traction the simplified solution carries across where the hole's wall was.
# Boundaries: a developer tool run on the bench VM after `harness.py --keep-fields`. Nothing in the product imports it. It changes no geometry it did not make itself.
# Collaborates with: devtools/simplify/harness.py (recognition, removal, meshing, CalculiX, the T3 rows it compares against), docs/superpowers/specs/2026-11-19-cad-simplification-design.md (tiers T1 and T2).
"""IS A LOCAL SOLVE ENOUGH TO JUDGE A SIMPLIFICATION?

    python devtools/simplify/local.py part.step --harness results/part [--k 3] [--jobs 16]

T2, per feature: region R = the feature's box padded by max(k x size, 2h). Cut R out of the original
and out of the simplified part (BRepAlgoAPI_Common), mesh both at the global size h, prescribe on
every cut face the displacement of the simplified part's GLOBAL solution (harness field.npz), solve
both. Reported:

- the strain energies W_o, W_s, and from them a first-order estimate of the global compliance change:
  the global run is displacement-controlled, so its energy W = delta^2 K / 2 and a local stiffening
  that raises the local energy by dW (same cut displacements) raises K by dW / W to first order; the
  flexibility (compliance) moves the other way: (C_s - C_o)/C_o ~ -(W_s - W_o) / W_global;
- the peak von Mises in R away from the feature's own zone and from the cut, original vs simplified;
- the CUT CHECK: the resultant force on each cut plane, original vs simplified. The piece carries no
  other load, so the total over all cut faces is zero by equilibrium and says nothing; per plane it
  does. If removing the feature changed what the cut carries, the region was too small to judge it.

T1, holes only: from the same global field, sample the stress on the removed hole's wall (it lies
inside the simplified solid) and form eta = |wall|^(1/2) * integral(|sigma n|^2 / E) over the wall,
an energy (N mm), reported relative to C_s. For a hole the original wall was traction-free, so this is
the analysis-aware defeaturing estimator's structure (Buffa et al., arXiv 2007.11525) carried to
elasticity. Its constant is uncalibrated; here it only has to rank as T3 ranks. eta is reported relative to
2 W_global.

The pieces are meshed at h_local = h / REFINE (default 2): a local solve is cheap to refine, and the
region is at least 4 local cells deeper than the feature on every side so a peak can be read away
from both the cut and the feature's own zone.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402  (a sibling script, not a package)


def _box(b, pad):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    lo = gp_Pnt(b[0] - pad, b[1] - pad, b[2] - pad)
    hi = gp_Pnt(b[3] + pad, b[4] + pad, b[5] + pad)
    return BRepPrimAPI_MakeBox(lo, hi).Shape(), (b[0] - pad, b[1] - pad, b[2] - pad, b[3] + pad, b[4] + pad, b[5] + pad)


def _common(shape, box):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    c = BRepAlgoAPI_Common(shape, box)
    c.Build()
    return c.Shape() if c.IsDone() else None


def _plane_of(b, region, tol):
    """Which cut plane a surface lies on ('x0'...'z1'), or None for a surface of the part itself."""
    for k, ax in enumerate("xyz"):
        if b[k + 3] - b[k] <= tol:
            for side, val in (("0", region[k]), ("1", region[k + 3])):
                if abs(b[k] - val) <= tol:
                    return f"C{ax.upper()}{side}"
    return None


def _drive(global_npz: Path, region, margin: float):
    """An interpolator of the global displacement over the global nodes near the region."""
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
    g = np.load(global_npz)
    lo, hi = np.array(region[:3]) - margin, np.array(region[3:]) + margin
    sel = np.all((g["xyz"] >= lo) & (g["xyz"] <= hi), axis=1)
    pts, u = g["xyz"][sel], g["u"][sel]
    lin, near = LinearNDInterpolator(pts, u), NearestNDInterpolator(pts, u)

    def at(q):
        v = lin(q)
        bad = np.isnan(v).any(axis=1)
        if bad.any():
            v[bad] = near(q[bad])
        return v
    return at


def _read_rf(dat: Path) -> dict[int, np.ndarray]:
    rf: dict[int, np.ndarray] = {}
    on = False
    for ln in dat.read_text().splitlines():
        if "forces (fx,fy,fz)" in ln:
            on = True
            continue
        if on:
            parts = ln.split()
            if len(parts) == 4:
                try:
                    rf[int(parts[0])] = np.array([float(v) for v in parts[1:]])
                    continue
                except ValueError:
                    pass
            if parts:
                on = False
    return rf


def _solve_piece(brep: Path, work: Path, h: float, region, tol: float, drive) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    m = H.mesh_brep(str(brep), h, lambda b: _plane_of(b, region, tol))
    if isinstance(m, str):
        return {"ok": False, "why": m}
    cut_sets = [k for k in m.sets if k.startswith("C")]
    if not cut_sets:
        return {"ok": False, "why": "no cut faces"}
    row = {int(t): i for i, t in enumerate(m.tags)}
    cut_nodes = sorted({n for k in cut_sets for n in m.sets[k]})
    u = drive(m.xyz[[row[n] for n in cut_nodes]])
    cards = ["*STEP", "*STATIC", "*BOUNDARY"]
    for n, (ux, uy, uz) in zip(cut_nodes, u):
        cards += [f"{n},1,1,{ux:.9g}", f"{n},2,2,{uy:.9g}", f"{n},3,3,{uz:.9g}"]
    m.sets["NCUT"] = cut_nodes
    cards += ["*NODE PRINT, NSET=NCUT", "RF", "*EL PRINT, ELSET=EALL, TOTALS=ONLY", "ELSE",
              "*NODE FILE", "U", "*EL FILE", "S", "*END STEP", ""]
    ok, solve_s = H.run_ccx(m, work, "\n".join(cards))
    if not ok:
        return {"ok": False, "why": "ccx failed"}
    fld = H.read_field(work / "job.frd", m)
    rf = _read_rf(work / "job.dat")
    per_plane = {k: np.sum([rf[n] for n in m.sets[k] if n in rf], axis=0).tolist() for k in cut_sets}
    (work / "job.frd").unlink(missing_ok=True)
    return {"ok": True, "elements": len(m.conn), "solve_s": solve_s, "energy": H.read_energy(work / "job.dat"),
            "xyz": m.xyz, "vm": fld.vm, "planes": per_plane}


def _peak(piece, region, h, zone, pad):
    """Peak von Mises in the region, away from the cut (2h) and from the feature's own zone."""
    x = piece["xyz"]
    lo, hi = np.array(region[:3]) + 2 * h, np.array(region[3:]) - 2 * h
    keep = np.all((x >= lo) & (x <= hi), axis=1)
    keep &= ~np.all((x >= np.array(zone[:3]) - pad) & (x <= np.array(zone[3:]) + pad), axis=1)
    return float(piece["vm"][keep].max()) if keep.any() else None


def t1_hole(shape, faces, fids: list[int], global_npz: Path, c_s: float, diag: float) -> dict:
    """eta = |wall|^(1/2) * sum(|sigma n|^2 / E * dA) over the removed hole's wall, relative to C_s."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from scipy.spatial import cKDTree
    g = np.load(global_npz)
    tree = cKDTree(g["xyz"])
    BRepMesh_IncrementalMesh(shape, 1e-3 * diag, False, 0.2, True)
    total_area, integral = 0.0, 0.0
    for j in fids:
        f = faces[j]
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        p = np.array([[tri.Node(i).Transformed(tr).X(), tri.Node(i).Transformed(tr).Y(), tri.Node(i).Transformed(tr).Z()]
                      for i in range(1, tri.NbNodes() + 1)])
        flip = f.Orientation() == TopAbs_REVERSED
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = (v - 1 for v in tri.Triangle(k).Get())
            n = np.cross(p[b] - p[a], p[c] - p[a])
            area = 0.5 * np.linalg.norm(n)
            if area <= 0:
                continue
            n = (-n if flip else n) / (2 * area)
            ctr = (p[a] + p[b] + p[c]) / 3
            _, i = tree.query(ctr)
            sxx, syy, szz, sxy, syz, szx = g["s"][i]
            sig = np.array([[sxx, sxy, szx], [sxy, syy, syz], [szx, syz, szz]])
            t = sig @ n
            integral += float(t @ t) / H.E_MPA * area
            total_area += area
    eta = math.sqrt(total_area) * integral
    return {"eta": eta, "eta_rel": eta / c_s if c_s else None, "wall_area": total_area}


def _one(args) -> tuple[str, dict]:
    step, fid, feat, harness_dir, h_global, k, diag, w_global, refine = args
    h = h_global / refine
    out = Path(harness_dir) / "local" / fid
    try:
        shape = H.prepare(step)[0]
        feats, faces, _ = H.recognise(shape, diag)
        f = next((x for x in feats if x.id == fid), None)
        if f is None or f.faces != feat["faces"]:
            return fid, {"ok": False, "why": "recognition differs from the harness run"}
        simp, why = H.remove(shape, [faces[j] for j in f.faces])
        if simp is None:
            return fid, {"ok": False, "why": why}
        zpad = 2.0 * f.size + 0.02 * diag
        pad = max(k * f.size, zpad + 4 * h)
        box, region = _box(f.bbox, pad)
        part_bb = H._bbox_of([shape])
        if all(region[i] <= part_bb[i] and region[i + 3] >= part_bb[i + 3] for i in range(3)):
            return fid, {"ok": False, "why": "region covers the whole part"}
        lo, ls = _common(shape, box), _common(simp, box)
        if lo is None or ls is None:
            return fid, {"ok": False, "why": "region cut failed"}
        out.mkdir(parents=True, exist_ok=True)
        H.write_brep(lo, out / "orig.brep")
        H.write_brep(ls, out / "simp.brep")
        npz = Path(harness_dir) / "work" / fid / "field.npz"
        drive = _drive(npz, region, 2 * h_global)
        tol = 1e-4 * diag
        po = _solve_piece(out / "orig.brep", out / "o", h, region, tol, drive)
        ps = _solve_piece(out / "simp.brep", out / "s", h, region, tol, drive)
        res: dict = {"ok": po["ok"] and ps["ok"], "pad": pad, "region": region}
        if not res["ok"]:
            res["why"] = po.get("why") or ps.get("why")
            return fid, res
        wo, ws = po["energy"], ps["energy"]
        res["W_o"], res["W_s"] = wo, ws
        res["est_compliance"] = -(ws - wo) / w_global if (wo is not None and ws is not None and w_global) else None
        so, ss = _peak(po, region, h, f.bbox, zpad), _peak(ps, region, h, f.bbox, zpad)
        res["peak_o"], res["peak_s"] = so, ss
        res["est_local_stress"] = (ss - so) / so if so and ss is not None else None
        planes = sorted(set(po["planes"]) & set(ps["planes"]))
        fo = np.array([po["planes"][p] for p in planes])
        fs = np.array([ps["planes"][p] for p in planes])
        scale = max(float(np.linalg.norm(fo, axis=1).max()), 1e-30) if len(planes) else 1.0
        res["cut_mismatch"] = float(np.linalg.norm(fo - fs, axis=1).max() / scale) if len(planes) else None
        res["elements"] = (po["elements"], ps["elements"])
        if f.kind == "hole":
            res["t1"] = t1_hole(shape, faces, f.faces, npz, 2 * w_global, diag)
        return fid, res
    except Exception as exc:
        return fid, {"ok": False, "why": f"{type(exc).__name__}: {exc}"}


def _isolated(t) -> tuple[str, dict]:
    """One feature in its own process (a native abort costs one row, as in harness._job)."""
    fid, hd = t[1], Path(t[3])
    spec = hd / "local" / f"{fid}.spec.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps(list(t)))
    try:
        p = subprocess.run([sys.executable, "-I", __file__, "--one", str(spec)], capture_output=True, text=True,
                           timeout=7200)
    except subprocess.TimeoutExpired:
        return fid, {"ok": False, "why": "timed out"}
    res = spec.with_suffix(".out.json")
    if p.returncode != 0 or not res.exists():
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        return fid, {"ok": False, "why": f"worker died: {tail[0][:200]}"}
    return fid, json.loads(res.read_text())


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--one"]:
        spec = Path(argv[1])
        fid, res = _one(tuple(json.loads(spec.read_text())))
        spec.with_suffix(".out.json").write_text(json.dumps(res, default=float))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--harness", required=True, help="the harness --out directory (run with --keep-fields)")
    ap.add_argument("--k", type=float, default=3.0, help="region padding = max(k x feature size, 2h)")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--refine", type=float, default=2.0, help="local mesh size = global h / REFINE")
    a = ap.parse_args(argv)
    hd = Path(a.harness)
    r = json.loads((hd / "result.json").read_text())
    todo = []
    for fid, row in r["features"].items():
        if "error" not in row or not (hd / "work" / fid / "field.npz").exists():
            continue
        w_global = row["result"]["qoi"]["strain_energy"]
        todo.append((a.step, fid, row, str(hd), r["h"], a.k, r["diag"], w_global, a.refine))
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for fut in as_completed([ex.submit(_isolated, t) for t in todo]):
            fid, res = fut.result()
            out[fid] = res
            print(f"{fid}: {'ok' if res['ok'] else res['why']}", flush=True)
    name = f"local_k{a.k:g}_r{a.refine:g}.json"
    (hd / name).write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {hd / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
