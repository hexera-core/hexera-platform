# Responsibility: Phase-1 ground truth for CAD simplification: for one part, remove each candidate feature on its own, mesh and solve the original and every simplified part, and record cost saved and error spent per feature.
# Boundaries: a developer tool run on the bench VM (gmsh, OCP and CalculiX `ccx` on PATH). Nothing in the product imports it. It invents a load case (fixed at one end of the longest axis, bent at the other) because the corpus parts come without one.
# Collaborates with: docs/superpowers/specs/2026-11-19-cad-simplification-design.md (tier T3 and phase P1), engines/gmsh/abaqus_deck.py (the tet10 node order).
"""ONE FEATURE AT A TIME, MEASURED.

    python devtools/simplify/harness.py part.step --out results/part [--jobs 16] [--size 30]

For the part and for each candidate feature removed alone (OpenCASCADE BRepAlgoAPI_Defeaturing):
mesh with gmsh (10-node tets, curvature-sized, so small features cost cells), solve linear static
in CalculiX, and write one JSON row: cells, mesh and solve seconds, and the change of every quantity
of interest against the original. A second mesh of the original at a slightly different size gives
the NOISE FLOOR: two meshes of the same part disagree by that much already, so an error below it is
not a measurement of the feature.

Units are the file's (mm for the NIST corpus), N and MPa; steel E = 210 GPa, nu = 0.3. Linear, so
the load's magnitude cancels from every relative error.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

#: gmsh's 10-node tet differs from CalculiX's C3D10 only by its last two nodes
#: (engines/gmsh/abaqus_deck.GMSH_TET10_TO_SOLVER, repeated so the bench needs no package import).
GMSH_TET10_TO_SOLVER = (0, 1, 2, 3, 4, 5, 6, 7, 9, 8)
E_MPA, NU = 210_000.0, 0.3
TWO_PI = 2 * math.pi


# ------------------------------------------------------------------------------------------------
# geometry: read, recognise, remove

def read_step(path: str):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader
    r = STEPControl_Reader()
    if r.ReadFile(path) != IFSelect_RetDone:
        raise SystemExit(f"cannot read {path}")
    r.TransferRoots()
    return r.OneShape()


def _faces(shape):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    from OCP.TopoDS import TopoDS
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)], m


def _adjacency(shape, fmap) -> list[set[int]]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    em = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, em)
    adj: list[set[int]] = [set() for _ in range(fmap.Extent())]
    for i in range(1, em.Extent() + 1):
        ids = [fmap.FindIndex(f) - 1 for f in em.FindFromIndex(i)]
        for a in ids:
            for b in ids:
                if a != b:
                    adj[a].add(b)
    return adj


@dataclass
class FaceInfo:
    kind: str            # plane / cyl / cone / torus / sph / bspl / other
    area: float
    centroid: tuple
    radius: float = 0.0  # cylinder radius, torus minor radius
    span: float = 0.0    # angular span in u (cylinder/cone/torus), radians
    concave: bool = False
    axis: tuple = ()     # (point, direction) for cylinder/cone


def describe(face) -> FaceInfo:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp, BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import (
        GeomAbs_BSplineSurface,
        GeomAbs_Cone,
        GeomAbs_Cylinder,
        GeomAbs_Plane,
        GeomAbs_Sphere,
        GeomAbs_Torus,
    )
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    kind = {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cyl", GeomAbs_Cone: "cone", GeomAbs_Torus: "torus",
            GeomAbs_Sphere: "sph", GeomAbs_BSplineSurface: "bspl"}.get(t, "other")
    info = FaceInfo(kind, g.Mass(), (c.X(), c.Y(), c.Z()))
    if kind not in ("cyl", "cone", "torus", "sph"):
        return info
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    info.span = u1 - u0
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, n)   # orientation-aware
    if kind in ("cyl", "cone"):
        ax = s.Cylinder().Axis() if kind == "cyl" else s.Cone().Axis()
        info.radius = s.Cylinder().Radius() if kind == "cyl" else s.Cone().RefRadius()
        o, d = ax.Location(), ax.Direction()
        info.axis = ((o.X(), o.Y(), o.Z()), (d.X(), d.Y(), d.Z()))
        w = np.array([p.X() - o.X(), p.Y() - o.Y(), p.Z() - o.Z()])
        dv = np.array([d.X(), d.Y(), d.Z()])
        radial = w - w.dot(dv) * dv
        info.concave = float(np.dot([n.X(), n.Y(), n.Z()], radial)) < 0.0   # normal toward the axis
    elif kind == "torus":
        tor = s.Torus()
        info.radius = tor.MinorRadius()
        ax = tor.Axis()
        o, d = ax.Location(), ax.Direction()
        # the tube centre nearest p: concave when the normal points toward it (material outside the tube)
        dv = np.array([d.X(), d.Y(), d.Z()])
        w = np.array([p.X() - o.X(), p.Y() - o.Y(), p.Z() - o.Z()])
        radial = w - w.dot(dv) * dv
        rn = np.linalg.norm(radial)
        centre = np.array([o.X(), o.Y(), o.Z()]) + (radial / rn * tor.MajorRadius() if rn > 0 else 0)
        info.concave = float(np.dot([n.X(), n.Y(), n.Z()], centre - np.array([p.X(), p.Y(), p.Z()]))) > 0.0
    elif kind == "sph":
        info.radius = s.Sphere().Radius()
    return info


@dataclass
class Feature:
    id: str
    kind: str                 # hole / blend
    faces: list[int]
    size: float               # radius
    concave: bool
    centroid: tuple
    bbox: tuple = ()
    area: float = 0.0


def _coaxial(a: FaceInfo, b: FaceInfo, tol: float) -> bool:
    if not a.axis or not b.axis:
        return False
    (pa, da), (pb, db) = a.axis, b.axis
    da, db = np.array(da), np.array(db)
    if abs(abs(da.dot(db)) - 1.0) > 1e-6:
        return False
    w = np.array(pb) - np.array(pa)
    return float(np.linalg.norm(w - w.dot(da) * da)) < tol


def recognise(shape, diag: float, *, blend_ratio: float = 0.03) -> tuple[list[Feature], list, list[FaceInfo]]:
    """Holes (concave cylinders, with the coaxial cones and blends at their mouths) and blends
    (tori and part-cylinders of radius under blend_ratio x diagonal), each a connected face set."""
    faces, fmap = _faces(shape)
    info = [describe(f) for f in faces]
    adj = _adjacency(shape, fmap)
    tol = 1e-4 * diag
    used: set[int] = set()
    feats: list[Feature] = []

    # holes: grow from each concave cylinder over coaxial cylinders/cones
    for i, fi in enumerate(info):
        if i in used or fi.kind != "cyl" or not fi.concave:
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                fk = info[k]
                if k in group or k in used:
                    continue
                if fk.kind in ("cyl", "cone") and _coaxial(info[i], fk, tol) and (fk.concave or fk.kind == "cone"):
                    group.add(k)
                    stack.append(k)
        span = sum(info[j].span for j in group if info[j].kind == "cyl" and abs(info[j].radius - fi.radius) < tol)
        if span < TWO_PI - 1e-3:           # a part-cylinder concave face is a fillet, not a hole
            continue
        used |= group
        feats.append(Feature(f"hole{len(feats)}", "hole", sorted(group), fi.radius, True, fi.centroid))

    # blends: tori and small part-cylinders, grown over each other
    def is_blend(fi: FaceInfo) -> bool:
        return (fi.kind == "torus" or (fi.kind == "cyl" and fi.span < TWO_PI - 1e-3)) and fi.radius < blend_ratio * diag

    for i, fi in enumerate(info):
        if i in used or not is_blend(fi):
            continue
        group, stack = {i}, [i]
        while stack:
            j = stack.pop()
            for k in adj[j]:
                if k not in group and k not in used and is_blend(info[k]) and info[k].concave == fi.concave:
                    group.add(k)
                    stack.append(k)
        used |= group
        feats.append(Feature(f"blend{len(feats)}", "blend", sorted(group), max(info[j].radius for j in group),
                             fi.concave, fi.centroid))

    for f in feats:
        pts = np.array([info[j].centroid for j in f.faces])
        f.area = float(sum(info[j].area for j in f.faces))
        f.centroid = tuple(np.average(pts, axis=0, weights=[info[j].area for j in f.faces]).tolist())
        b = _bbox_of([faces[j] for j in f.faces])
        f.bbox = b
    return feats, faces, info


def _bbox_of(shapes) -> tuple:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    for s in shapes:
        BRepBndLib.Add_s(s, b)
    return tuple(b.Get())


def remove(shape, faces) -> tuple[object | None, str]:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
    from OCP.BRepCheck import BRepCheck_Analyzer
    d = BRepAlgoAPI_Defeaturing()
    d.SetShape(shape)
    for f in faces:
        d.AddFaceToRemove(f)
    d.SetRunParallel(False)
    d.SetToFillHistory(False)
    d.Build()
    if not d.IsDone():
        return None, "defeaturing failed"
    out = d.Shape()
    if not BRepCheck_Analyzer(out).IsValid():
        return None, "result is not a valid shape"
    # IsDone() is true when OCCT could not remove the faces and handed the input back (it records a
    # warning instead). Measured on nist_ftc_09: 12 "removed" blends with dV ~ 1e-15. A face set that
    # is still all there is a no-op, not a free simplification.
    before = len(_faces(shape)[0])
    if len(_faces(out)[0]) == before and abs(volume(out) - volume(shape)) <= 1e-9 * abs(volume(shape)):
        return None, "defeaturing left the shape unchanged"
    return out, ""


def volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return g.Mass()


def write_brep(shape, path: Path) -> None:
    from OCP.BRepTools import BRepTools
    BRepTools.Write_s(shape, str(path))


# ------------------------------------------------------------------------------------------------
# load case

@dataclass
class LoadCase:
    axis: int          # longest bbox axis
    lo: float
    hi: float
    band: float        # a face lies in an end band when its whole bbox is within this of the end
    bend_dir: int      # the axis the load pushes along (second-longest)


def load_case(bbox) -> LoadCase:
    x0, y0, z0, x1, y1, z1 = bbox
    ext = np.array([x1 - x0, y1 - y0, z1 - z0])
    order = np.argsort(-ext)
    k = int(order[0])
    lo, hi = (x0, y0, z0)[k], (x1, y1, z1)[k]
    return LoadCase(k, lo, hi, 0.01 * ext[k], int(order[1]))


def near_bc(f: Feature, lc: LoadCase, margin: float) -> bool:
    lo_k, hi_k = f.bbox[lc.axis], f.bbox[lc.axis + 3]
    return lo_k < lc.lo + lc.band + margin or hi_k > lc.hi - lc.band - margin


# ------------------------------------------------------------------------------------------------
# mesh and solve (run in a worker process; each gets its own gmsh)

@dataclass
class Solved:
    ok: bool
    why: str = ""
    nodes: int = 0
    elements: int = 0
    mesh_s: float = 0.0
    solve_s: float = 0.0
    qoi: dict = field(default_factory=dict)


def mesh_and_solve(brep: str, work: str, lc: dict, h_max: float, exclude: dict, threads: int = 1) -> Solved:
    import gmsh
    lc = LoadCase(**lc)
    wd = Path(work)
    wd.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    gmsh.initialize(readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", threads)
        gmsh.model.occ.importShapes(brep)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", h_max)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h_max / 200)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        gmsh.option.setNumber("Mesh.HighOrderOptimize", 1)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)
        surfs = [t for _, t in gmsh.model.getEntities(2)]
        fixed, loaded = [], []
        for t in surfs:
            b = gmsh.model.getBoundingBox(2, t)
            if b[lc.axis + 3] <= lc.lo + lc.band:
                fixed.append(t)
            elif b[lc.axis] >= lc.hi - lc.band:
                loaded.append(t)
        if not fixed or not loaded:
            return Solved(False, f"no end faces (fixed {len(fixed)}, loaded {len(loaded)})")
        gmsh.model.mesh.generate(3)
        mesh_s = time.time() - t0
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        xyz = xyz.reshape(-1, 3)
        idx = {int(t): i for i, t in enumerate(tags)}
        et, en = gmsh.model.mesh.getElementsByType(11)
        conn = np.asarray(en, dtype=np.int64).reshape(-1, 10)[:, list(GMSH_TET10_TO_SOLVER)]

        def nodes_on(ts):
            s = set()
            for t in ts:
                nt, _, _ = gmsh.model.mesh.getNodes(2, t, includeBoundary=True)
                s.update(int(n) for n in nt)
            return sorted(s)

        nfix, nload = nodes_on(fixed), nodes_on(loaded)
    finally:
        gmsh.finalize()
    if len(conn) == 0:
        return Solved(False, "no tetrahedra")

    # deck: total force 1000 N along bend_dir spread over the loaded nodes (linear: magnitude cancels)
    f_node = 1000.0 / len(nload)
    inp = wd / "job.inp"
    with inp.open("w") as fh:
        fh.write("*NODE, NSET=NALL\n")
        for t, (x, y, z) in zip(tags, xyz):
            fh.write(f"{int(t)},{x:.9g},{y:.9g},{z:.9g}\n")
        fh.write("*ELEMENT, TYPE=C3D10, ELSET=EALL\n")
        for e, c in zip(et, conn):
            fh.write(f"{int(e)}," + ",".join(str(int(v)) for v in c) + "\n")
        fh.write("*NSET, NSET=NFIX\n" + "\n".join(map(str, nfix)) + "\n")
        fh.write("*NSET, NSET=NLOAD\n" + "\n".join(map(str, nload)) + "\n")
        fh.write(f"*MATERIAL, NAME=STEEL\n*ELASTIC\n{E_MPA},{NU}\n*SOLID SECTION, ELSET=EALL, MATERIAL=STEEL\n")
        fh.write("*BOUNDARY\nNFIX,1,3,0\n*STEP\n*STATIC\n*CLOAD\n")
        fh.write(f"NLOAD,{lc.bend_dir + 1},{f_node:.9g}\n")
        fh.write("*NODE PRINT, NSET=NLOAD\nU\n*NODE PRINT, NSET=NFIX\nRF\n*NODE FILE\nU\n*EL FILE\nS\n*END STEP\n")
    t1 = time.time()
    env = dict(os.environ, OMP_NUM_THREADS=str(threads), CCX_NPROC_EQUATION_SOLVER=str(threads))
    p = subprocess.run(["ccx", "-i", "job"], cwd=wd, env=env, capture_output=True, text=True, timeout=7200)
    solve_s = time.time() - t1
    if p.returncode != 0 or not (wd / "job.frd").exists():
        (wd / "ccx.log").write_text(p.stdout[-20000:] + p.stderr[-5000:])
        return Solved(False, "ccx failed", len(tags), len(conn), mesh_s, solve_s)
    u, s_vm = read_frd(wd / "job.frd")
    q = qois(u, s_vm, xyz, idx, nload, lc, f_node, exclude)
    for big in ("job.frd",):
        (wd / big).unlink(missing_ok=True)
    return Solved(True, "", len(tags), len(conn), mesh_s, solve_s, q)


def read_frd(path: Path) -> tuple[dict[int, np.ndarray], dict[int, float]]:
    """Nodal displacement and nodal von Mises (CalculiX extrapolates stresses to nodes in .frd)."""
    u: dict[int, np.ndarray] = {}
    svm: dict[int, float] = {}
    block = None
    with path.open() as fh:
        for ln in fh:
            if ln.startswith("    1PSTEP"):
                continue
            if ln.startswith(" -4"):
                name = ln[5:13].strip()
                block = name if name in ("DISP", "STRESS") else None
                continue
            if ln.startswith(" -3"):
                block = None
                continue
            if block and ln.startswith(" -1"):
                n = int(ln[3:13])
                vals = [float(ln[13 + 12 * k:25 + 12 * k]) for k in range((len(ln.rstrip()) - 13) // 12)]
                if block == "DISP":
                    u[n] = np.array(vals[:3])
                else:
                    sxx, syy, szz, sxy, syz, szx = vals[:6]
                    svm[n] = math.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                                       + 3 * (sxy ** 2 + syz ** 2 + szx ** 2))
    return u, svm


def _in_box(p, b, pad) -> bool:
    return all(b[k] - pad <= p[k] <= b[k + 3] + pad for k in range(3))


def qois(u, svm, xyz, idx, nload, lc: LoadCase, f_node, exclude: dict) -> dict:
    """Compliance (F.u), max |u|, mean tip deflection, and max von Mises away from the fixed/loaded
    ends, once per named exclusion set (a removed feature's own zone), so a removed stress riser is
    not counted as an error at its own location and the original is read under the same exclusion."""
    comp = sum(f_node * u[n][lc.bend_dir] for n in nload if n in u)
    tip = float(np.mean([u[n][lc.bend_dir] for n in nload if n in u]))
    umax = max(float(np.linalg.norm(v)) for v in u.values())
    length = lc.hi - lc.lo
    mid = [(xyz[idx[n]], s) for n, s in svm.items()
           if lc.lo + 0.1 * length <= xyz[idx[n]][lc.axis] <= lc.hi - 0.1 * length]
    far = {name: max((s for p, s in mid if not any(_in_box(p, b, pad) for b, pad in boxes)), default=0.0)
           for name, boxes in exclude.items()}
    return {"compliance": comp, "tip_deflection": tip, "max_disp": umax, "max_vm_far": far}


# ------------------------------------------------------------------------------------------------
# the run

def _rel(q: dict, ref: dict, far_key: str) -> dict:
    out = {k: (q[k] - ref[k]) / ref[k] for k in ("compliance", "tip_deflection", "max_disp") if ref[k]}
    a, b = q["max_vm_far"].get(far_key), ref["max_vm_far"].get(far_key)
    if a is not None and b:
        out["max_vm_far"] = (a - b) / b
    return out


def _job(args):
    name, brep, work, lc, h, excl = args
    try:
        return name, asdict(mesh_and_solve(brep, work, lc, h, excl))
    except Exception as exc:   # one bad feature must not end the run
        return name, asdict(Solved(False, f"{type(exc).__name__}: {exc}"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--size", type=float, default=30.0, help="background element size = diagonal / SIZE")
    ap.add_argument("--max-features", type=int, default=60)
    a = ap.parse_args(argv)
    if not shutil.which("ccx"):
        raise SystemExit("ccx (CalculiX) not on PATH")
    out = Path(a.out)
    (out / "geom").mkdir(parents=True, exist_ok=True)

    shape = read_step(a.step)
    bb = _bbox_of([shape])
    diag = math.dist(bb[:3], bb[3:])
    lc = load_case(bb)
    h = diag / a.size
    feats, faces, _ = recognise(shape, diag)
    v0 = volume(shape)
    write_brep(shape, out / "geom" / "original.brep")

    rows: dict[str, dict] = {}
    jobs = []
    excl_all: dict[str, list] = {}
    for f in feats[: a.max_features]:
        pad = 2.0 * f.size + 0.02 * diag
        row = asdict(f) | {"pad": pad}
        if near_bc(f, lc, pad):
            row["skipped"] = "touches the fixed or loaded end"
            rows[f.id] = row
            continue
        simp, why = remove(shape, [faces[j] for j in f.faces])
        if simp is None:
            row["skipped"] = why
            rows[f.id] = row
            continue
        row["volume_change"] = (volume(simp) - v0) / v0
        path = out / "geom" / f"{f.id}.brep"
        write_brep(simp, path)
        rows[f.id] = row
        excl_all[f.id] = [(f.bbox, pad)]
        jobs.append((f.id, str(path), str(out / "work" / f.id), asdict(lc), h, {f.id: [(f.bbox, pad)]}))

    # the original reads its far stress under every feature's exclusion, so each row compares like
    # with like; "none" (nothing excluded) is what the noise run is compared on
    excl_all["none"] = []
    base = [("original", str(out / "geom" / "original.brep"), str(out / "work" / "original"), asdict(lc), h, excl_all),
            ("noise", str(out / "geom" / "original.brep"), str(out / "work" / "noise"), asdict(lc), h * 0.93, excl_all)]
    results: dict[str, dict] = {}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(_job, j) for j in base + jobs]
        for fut in as_completed(futs):
            name, res = fut.result()
            results[name] = res
            print(f"[{time.time() - t0:7.1f}s] {name}: {'ok' if res['ok'] else res['why']} "
                  f"elements={res['elements']}", flush=True)

    ref = results.get("original", {})
    if not ref.get("ok"):
        print("original did not solve:", ref.get("why"), file=sys.stderr)
    # recompute the far stress of the original against each feature's own exclusion for a fair row
    for fid, row in rows.items():
        r = results.get(fid)
        if not r:
            continue
        row["result"] = r
        if r["ok"] and ref.get("ok"):
            row["cells_saved"] = 1 - r["elements"] / ref["elements"]
            row["mesh_s_saved"] = 1 - r["mesh_s"] / max(ref["mesh_s"], 1e-9)
            row["solve_s_saved"] = 1 - r["solve_s"] / max(ref["solve_s"], 1e-9)
            row["error"] = _rel(r["qoi"], ref["qoi"], fid)
    noise = results.get("noise", {})
    if noise.get("ok") and ref.get("ok"):
        noise["error"] = _rel(noise["qoi"], ref["qoi"], "none")
    summary = {"part": a.step, "diag": diag, "h": h, "load_case": asdict(lc), "volume": v0,
               "n_features": len(feats), "original": ref, "noise": noise, "features": rows,
               "wall_s": time.time() - t0}
    (out / "result.json").write_text(json.dumps(summary, indent=1, default=float))
    print(f"wrote {out / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
