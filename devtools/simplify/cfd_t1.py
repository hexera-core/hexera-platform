# Responsibility: A first CFD tier-T1 screen for HOLES, scored against cfd_harness's own ground truth: from the ORIGINAL part's one converged flow solution, read the pressure difference across each through hole and rank holes by |dp| x hole area (the force a filled hole would add), next to what removing each hole measured in T3.
# Boundaries: a developer tool run on the CFD bench VM after cfd_harness.py has solved the original (results/<part>/work/original). It copies that case and adds a probes pass; it changes no geometry and no harness result. Holes only; blends need a surface-sensitivity screen (adjoint), not this.
# Collaborates with: devtools/simplify/cfd_harness.py (recognise, scaled, the run's meta.json and result.json), devtools/simplify/harness.py (read_step, describe), docs/simplification/CFD-NOTES.md (what it measured).
"""DOES ONE SOLVE OF THE ORIGINAL RANK THE HOLES?

    python devtools/simplify/cfd_t1.py results/part

For each hole the harness solved: probe p on the hole axis just outside both mouths in the original's
solution (OpenFOAM `probes`, run in the mesh image), and form eta = |p_a - p_b| x pi r^2, the
normal force a cap over the hole would carry, reported relative to the original drag. A hole that
carries no pressure difference passes no flow and its filling should change nothing; the measured
T3 change of lift and drag per unit drag is printed beside it, with the rank correlation.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfd_harness as C  # noqa: E402  (a sibling script, not a package)
import harness as H  # noqa: E402


def _place(points, scale, rot):
    """Points in file units -> the case frame (cfd_harness.scaled on a vertex compound)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for p in points:
        b.Add(comp, BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex())
    out = []
    ex = TopExp_Explorer(C.scaled(comp, scale, rot), TopAbs_VERTEX)
    while ex.More():
        q = BRep_Tool.Pnt_s(TopoDS.Vertex_s(ex.Current()))
        out.append((q.X(), q.Y(), q.Z()))
        ex.Next()
    return out


def _spearman(a, b) -> float:
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    run = Path(argv[0]).resolve()
    meta = json.loads((run / "meta.json").read_text())
    res = json.loads((run / "result.json").read_text())
    args = meta["args"]
    shape = H.read_step(meta["part"] if Path(meta["part"]).is_absolute() else str(run.parent.parent / meta["part"]))
    bb = H._bbox_of([shape])
    diag = math.dist(bb[:3], bb[3:])
    feats, faces, info = C.recognise(shape, diag)
    order = [int(i) for i in np.argsort(-(np.array(bb[3:]) - np.array(bb[:3])))]
    centre = tuple(0.5 * (np.array(bb[:3]) + np.array(bb[3:])))
    rot = (order[1], args["aoa"], centre) if meta["mode"] == "external" else None
    holes, pts = [], []
    for f in feats:
        row = res["features"].get(f.id, {})
        if f.kind != "hole" or not row.get("error"):
            continue
        cyl = next(info[j] for j in f.faces if info[j].kind == "cyl")
        d = np.array(cyl.axis[1])
        c = np.array(f.centroid)
        ext = np.array(f.bbox[3:]) - np.array(f.bbox[:3])
        half = 0.5 * float(np.abs(ext).dot(np.abs(d))) + max(1.0, 0.5 * f.size)   # past both mouths
        pts += [tuple(c + half * d), tuple(c - half * d)]
        holes.append(f)
    placed = _place(pts, args["scale"], rot)
    case = run / "work" / "t1_probes"
    if case.exists():
        shutil.rmtree(case)
    shutil.copytree(run / "work" / "original", case, ignore=shutil.ignore_patterns("postProcessing", "log.*", "time.*"))
    # postProcess runs the controlDict's function objects on the latest time: add the probes there
    # (measured: `postProcess -dict <file>` with the same entries created no function object)
    cdict = case / "system" / "controlDict"
    probes = ("probes { type probes; libs (sampling); fields (p); fixedLocations true;\nprobeLocations (\n"
              + "\n".join(C._vec(p) for p in placed) + "\n); }\n")
    # ...and only them: the forces object needs U and the transport model, which postProcess does not load
    text = cdict.read_text()
    cdict.write_text(text[: text.index("functions {")] + "functions {\n" + probes + "}\n")
    cmd = ["docker", "run", "--rm", "--name", "cfd-t1-probes", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{case}:{case}", C.IMAGE, "bash", "-c",
           f"source {C.OF_BASHRC} >/dev/null 2>&1; cd {case} && postProcess -latestTime -fields '(p)' > log.probes 2>&1"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    pfile = sorted(case.glob("postProcessing/*/*/p"))
    if not pfile:
        raise SystemExit(f"no probe output in {case} (see log.probes*)")
    vals = None
    for ln in pfile[-1].read_text().splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        vals = [float(v) for v in ln.split()[1:]]
    if vals is None or len(vals) != len(placed):
        raise SystemExit(f"expected {len(placed)} probe values, got {vals}")
    drag = res["original"]["named"]["drag"]
    rows = []
    for i, f in enumerate(holes):
        dp = vals[2 * i] - vals[2 * i + 1]
        area = math.pi * (f.size * args["scale"]) ** 2
        err = res["features"][f.id]["error"]
        rows.append((f.id, f.size, dp, abs(dp) * area / abs(drag), err.get("lift/drag"), err.get("drag")))
    print("| hole | r mm | dp across | eta / drag | T3 lift/drag | T3 drag |\n|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: -r[3]):
        print(f"| {r[0]} | {r[1]:.2f} | {r[2]:+.4g} | {r[3]:.3%} | {r[4]:+.3%} | {r[5]:+.3%} |")
    eta = [r[3] for r in rows]
    print(f"\nSpearman(eta, |T3 lift/drag|) = {_spearman(eta, [abs(r[4]) for r in rows]):.2f}, "
          f"Spearman(eta, |T3 drag|) = {_spearman(eta, [abs(r[5]) for r in rows]):.2f}, n = {len(rows)}")
    floor = res.get("noise_floor", {})
    above = [r[0] for r in rows if floor.get("lift/drag") and abs(r[4]) > floor["lift/drag"]]
    print(f"T3 above the lift/drag floor: {above}; T1 top 3: {[r[0] for r in sorted(rows, key=lambda r: -r[3])[:3]]}")
    (run / "t1_holes.json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
