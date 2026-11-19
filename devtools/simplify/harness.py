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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def wall_thickness(shape, diag: float, *, percentile: float = 10.0, max_rays: int = 3000) -> float | None:
    """The part's thin-wall thickness: rays cast INWARD from points on its surface (triangle centroids
    of an OCCT tessellation, along the face's inward normal) to where they first leave the solid; the
    given percentile of those distances. None when no ray hit (an open shape)."""
    import numpy as _np
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    BRepMesh_IncrementalMesh(shape, 2e-3 * diag, False, 0.3, True)
    pts, nrm, area = [], [], []
    for f in _faces(shape)[0]:
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        p = _np.array([[q.X(), q.Y(), q.Z()] for q in (tri.Node(i).Transformed(tr) for i in range(1, tri.NbNodes() + 1))])
        flip = f.Orientation() == TopAbs_REVERSED
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = (v - 1 for v in tri.Triangle(k).Get())
            n = _np.cross(p[b] - p[a], p[c] - p[a])
            ar = 0.5 * float(_np.linalg.norm(n))
            if ar <= 0:
                continue
            n = n / (2 * ar)
            pts.append((p[a] + p[b] + p[c]) / 3)
            nrm.append(n if flip else -n)        # inward: OCCT triangles wind outward on a FORWARD face
            area.append(ar)
    if not pts:
        return None
    rng = _np.random.default_rng(0)
    w = _np.asarray(area) / sum(area)
    pick = rng.choice(len(pts), size=min(max_rays, len(pts)), replace=len(pts) < max_rays, p=w)
    ints = IntCurvesFace_ShapeIntersector()
    ints.Load(shape, 1e-6 * diag)
    eps = 1e-5 * diag
    d_hit = []
    for i in pick:
        o = pts[i] + eps * nrm[i]
        ints.Perform(gp_Lin(gp_Pnt(*o), gp_Dir(*nrm[i])), 0.0, diag)
        ws = [ints.WParameter(j) for j in range(1, ints.NbPnt() + 1)]
        # a ray launched beside an edge can clip the neighbouring face within micrometres; measured on
        # nist_ftc_09 those put the 10th percentile at 0.009 mm on a 3 mm plate. Hits under 0.1% of
        # the diagonal are discarded - so a wall thinner than that is NOT seen (stated limitation).
        ws = [x for x in ws if x > 1e-3 * diag]
        if ws:
            d_hit.append(min(ws) + eps)
    return float(_np.percentile(d_hit, percentile)) if d_hit else None


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
    band: float        # the part is trimmed this far in from each end; the cut faces carry the BCs
    bend_dir: int      # the axis the load pushes along (second-longest)

    @property
    def lo_cut(self) -> float:
        return self.lo + self.band

    @property
    def hi_cut(self) -> float:
        return self.hi - self.band


def load_case(bbox) -> LoadCase:
    x0, y0, z0, x1, y1, z1 = bbox
    ext = np.array([x1 - x0, y1 - y0, z1 - z0])
    order = np.argsort(-ext)
    k = int(order[0])
    lo, hi = (x0, y0, z0)[k], (x1, y1, z1)[k]
    return LoadCase(k, lo, hi, 0.03 * ext[k], int(order[1]))


def prepare(step: str):
    """Read the part and TRIM 3% off each end of its longest axis, so the boundary conditions sit on
    two exact planar cut faces that every mesh of every variant shares.

    Measured on nist_ftc_09, two load cases that did not: faces wholly inside an end band (none exist
    on a part with curved ends - nist_ctc_01/03 had no load case at all), then every NODE inside the
    band (the band's edge cuts through elements, so how much material is clamped depends on the mesh:
    five meshes at one size spread compliance by 1.07%, and coarser came out softer, which FE cannot
    do on its own)."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    shape = read_step(step)
    bb = _bbox_of([shape])
    diag = math.dist(bb[:3], bb[3:])
    lc = load_case(bb)
    lo, hi = list(bb[:3]), list(bb[3:])
    lo = [v - 0.01 * diag for v in lo]
    hi = [v + 0.01 * diag for v in hi]
    lo[lc.axis], hi[lc.axis] = lc.lo_cut, lc.hi_cut
    box = BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    c = BRepAlgoAPI_Common(shape, box)
    c.Build()
    if not c.IsDone():
        raise SystemExit(f"cannot trim the ends of {step}")
    return c.Shape(), diag, lc


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


@dataclass
class Meshed:
    tags: np.ndarray          # node tags
    xyz: np.ndarray           # (n, 3)
    etags: np.ndarray
    conn: np.ndarray          # (m, 10), CalculiX C3D10 order
    sets: dict                # name -> sorted node tags on the surfaces picked for it
    seconds: float


def mesh_brep(brep: str, h_max: float, pick, threads: int = 1, ho_optimize: int = 1) -> Meshed | str:
    """Mesh a BREP with 10-node tets; pick(bbox) -> set name or None names each surface's node set.
    Returns the reason as a string when a set comes out empty or the mesh has no tetrahedra."""
    import gmsh
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
        gmsh.option.setNumber("Mesh.HighOrderOptimize", ho_optimize)
        # without the optimiser, curved 10-node tets fold (CalculiX: "nonpositive jacobian", measured on
        # KFL08); straight-sided quadratic elements cannot, at the price of a faceted boundary
        gmsh.option.setNumber("Mesh.SecondOrderLinear", 0 if ho_optimize else 1)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)
        picked: dict[str, list[int]] = {}
        for _, t in gmsh.model.getEntities(2):
            name = pick(gmsh.model.getBoundingBox(2, t))
            if name:
                picked.setdefault(name, []).append(t)
        gmsh.model.mesh.generate(3)
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        et, en = gmsh.model.mesh.getElementsByType(11)
        sets = {}
        for name, ts in picked.items():
            nodes: set[int] = set()
            for t in ts:
                nt, _, _ = gmsh.model.mesh.getNodes(2, t, includeBoundary=True)
                nodes.update(int(n) for n in nt)
            sets[name] = sorted(nodes)
    finally:
        gmsh.finalize()
    if len(et) == 0:
        return "no tetrahedra"
    conn = np.asarray(en, dtype=np.int64).reshape(-1, 10)[:, list(GMSH_TET10_TO_SOLVER)]
    m = Meshed(np.asarray(tags, dtype=np.int64), np.asarray(xyz).reshape(-1, 3), np.asarray(et, dtype=np.int64),
               conn, sets, time.time() - t0)
    # THE MESH MUST FILL THE PART. Measured on a region cut from nist_ftc_09's simplified package:
    # gmsh meshed it without raising, at a twelfth of the tets the same region of the original took,
    # and the "simplified" piece read 29% stiff. A mesh whose tets do not add up to the CAD volume is
    # refused here, before anything is solved or scored.
    v_cad = _brep_volume(brep)
    v_mesh = mesh_volume(m)
    if v_cad and abs(v_mesh - v_cad) > MESH_VOLUME_TOLERANCE * abs(v_cad):
        return f"mesh volume {v_mesh:.6g} differs from the CAD volume {v_cad:.6g} by {(v_mesh - v_cad) / v_cad:+.2%}"
    return m


#: a curved boundary meshed with straight-sided or 2nd-order elements is a little off its CAD volume;
#: a mesh that misses a piece of the part is far more than this
MESH_VOLUME_TOLERANCE = 0.005


def mesh_volume(m: Meshed) -> float:
    """The tets' volume from their corner nodes (straight-edged; the curved mid-nodes move it a little)."""
    row = {int(t): i for i, t in enumerate(m.tags)}
    idx = np.vectorize(row.__getitem__)(m.conn[:, :4])
    p = m.xyz[idx]
    return float(np.abs(np.einsum("ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), p[:, 3] - p[:, 0])).sum() / 6)


def _brep_volume(path: str) -> float | None:
    try:
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape
        shape = TopoDS_Shape()
        BRepTools.Read_s(shape, path, BRep_Builder())
        return volume(shape)
    except Exception:  # noqa: BLE001 - no CAD volume means no check, not a failed mesh
        return None


def run_ccx(m: Meshed, wd: Path, step_cards: str, threads: int = 1) -> tuple[bool, float]:
    """Write the deck (mesh, every node set, steel) plus step_cards (everything from *BOUNDARY on),
    run CalculiX, and say whether it produced job.frd."""
    with (wd / "job.inp").open("w") as fh:
        fh.write("*NODE, NSET=NALL\n")
        for t, (x, y, z) in zip(m.tags, m.xyz):
            fh.write(f"{int(t)},{x:.9g},{y:.9g},{z:.9g}\n")
        fh.write("*ELEMENT, TYPE=C3D10, ELSET=EALL\n")
        for e, c in zip(m.etags, m.conn):
            fh.write(f"{int(e)}," + ",".join(str(int(v)) for v in c) + "\n")
        for name, nodes in m.sets.items():
            fh.write(f"*NSET, NSET={name}\n" + "\n".join(map(str, nodes)) + "\n")
        fh.write(f"*MATERIAL, NAME=STEEL\n*ELASTIC\n{E_MPA},{NU}\n*SOLID SECTION, ELSET=EALL, MATERIAL=STEEL\n")
        fh.write(step_cards)
    t1 = time.time()
    env = dict(os.environ, OMP_NUM_THREADS=str(threads), CCX_NPROC_EQUATION_SOLVER=str(threads))
    p = subprocess.run(["ccx", "-i", "job"], cwd=wd, env=env, capture_output=True, text=True, timeout=7200)
    ok = p.returncode == 0 and (wd / "job.frd").exists()
    if not ok:
        (wd / "ccx.log").write_text(p.stdout[-20000:] + p.stderr[-5000:])
    return ok, time.time() - t1


def mesh_and_solve(brep: str, work: str, lc: dict, h_max: float, exclude: dict, threads: int = 1,
                   keep_field: bool = False, ho_optimize: int = 1) -> Solved:
    lc = LoadCase(**lc)
    wd = Path(work)
    wd.mkdir(parents=True, exist_ok=True)

    tol = 1e-5 * (lc.hi - lc.lo)

    def pick(b):
        if b[lc.axis + 3] - b[lc.axis] > tol:
            return None
        if abs(b[lc.axis] - lc.lo_cut) <= tol:
            return "NFIX"
        if abs(b[lc.axis] - lc.hi_cut) <= tol:
            return "NLOAD"
        return None

    m = mesh_brep(brep, h_max, pick, threads, ho_optimize)
    if isinstance(m, str):
        return Solved(False, m)
    if not m.sets.get("NFIX") or not m.sets.get("NLOAD"):
        return Solved(False, f"no end faces (fixed {len(m.sets.get('NFIX', []))}, loaded {len(m.sets.get('NLOAD', []))})")
    # DISPLACEMENT CONTROL on the two trimmed end faces (prepare()): the high one moves by delta
    # along bend_dir, the low one not at all - the same continuum problem for every mesh. A force
    # spread equally over nodes instead depends on how the mesher filled the end.
    delta = 1e-3 * (lc.hi - lc.lo)
    d = lc.bend_dir + 1
    ok, solve_s = run_ccx(m, wd, "*BOUNDARY\nNFIX,1,3,0\n*STEP\n*STATIC\n*BOUNDARY\n"
                          f"NLOAD,{d},{d},{delta:.9g}\n"
                          "*NODE PRINT, NSET=NLOAD, TOTALS=ONLY\nRF\n"
                          "*NODE FILE\nU\n*EL FILE\nS\n*EL PRINT, ELSET=EALL, TOTALS=ONLY\nELSE\n*END STEP\n", threads)
    if not ok:
        return Solved(False, "ccx failed", len(m.tags), len(m.conn), m.seconds, solve_s)
    fld = read_field(wd / "job.frd", m)
    if keep_field:
        np.savez_compressed(wd / "field.npz", tags=m.tags, xyz=m.xyz, u=fld.u, s=fld.s)
    rf = read_total_force(wd / "job.dat")
    if rf is None or rf[lc.bend_dir] == 0:
        return Solved(False, "no reaction force in job.dat", len(m.tags), len(m.conn), m.seconds, solve_s)
    q = qois(fld, m, lc, delta, rf[lc.bend_dir], exclude)
    q["strain_energy"] = read_energy(wd / "job.dat")
    (wd / "job.frd").unlink(missing_ok=True)
    return Solved(True, "", len(m.tags), len(m.conn), m.seconds, solve_s, q)


@dataclass
class Field:
    u: np.ndarray    # (n, 3), row i is node m.tags[i]
    s: np.ndarray    # (n, 6) sxx syy szz sxy syz szx, nodal (CalculiX extrapolates to nodes)

    @property
    def vm(self) -> np.ndarray:
        sxx, syy, szz, sxy, syz, szx = self.s.T
        return np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                       + 3 * (sxy ** 2 + syz ** 2 + szx ** 2))


def read_field(path: Path, m: Meshed) -> Field:
    row = {int(t): i for i, t in enumerate(m.tags)}
    u = np.zeros((len(m.tags), 3))
    s = np.zeros((len(m.tags), 6))
    block = None
    with path.open() as fh:
        for ln in fh:
            if ln.startswith(" -4"):
                name = ln[5:13].strip()
                block = name if name in ("DISP", "STRESS") else None
                continue
            if ln.startswith(" -3"):
                block = None
                continue
            if block and ln.startswith(" -1"):
                i = row.get(int(ln[3:13]))
                if i is None:
                    continue
                vals = [float(ln[13 + 12 * k:25 + 12 * k]) for k in range((len(ln.rstrip()) - 13) // 12)]
                if block == "DISP":
                    u[i] = vals[:3]
                else:
                    s[i] = vals[:6]
    return Field(u, s)


def read_total_force(dat: Path) -> list[float] | None:
    """The 'total force (fx,fy,fz)' CalculiX prints for *NODE PRINT ... TOTALS=ONLY / RF."""
    lines = dat.read_text().splitlines() if dat.exists() else []
    for i, ln in enumerate(lines):
        if "total force" in ln:
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    return [float(v) for v in nxt.split()[-3:]]
    return None


def read_energy(dat: Path) -> float | None:
    """The 'total internal energy' CalculiX prints for *EL PRINT ... TOTALS=ONLY / ELSE."""
    lines = dat.read_text().splitlines() if dat.exists() else []
    for i, ln in enumerate(lines):
        if "total internal energy" in ln:
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    return float(nxt.split()[-1])
    return None


def qois(fld: Field, m: Meshed, lc: LoadCase, delta: float, reaction: float, exclude: dict) -> dict:
    """Compliance (delta / reaction: the flexibility, deflection per unit load), the reaction itself,
    max |u|, and max von Mises away from the fixed/loaded
    ends, once per named exclusion set (a removed feature's own zone), so a removed stress riser is
    not counted as an error at its own location and the original is read under the same exclusion."""
    comp = delta / reaction
    umax = float(np.linalg.norm(fld.u, axis=1).max())
    length = lc.hi - lc.lo
    a = m.xyz[:, lc.axis]
    mid = (a >= lc.lo + 0.1 * length) & (a <= lc.hi - 0.1 * length)
    vm = fld.vm
    far = {}
    for name, boxes in exclude.items():
        keep = mid.copy()
        for b, pad in boxes:
            inside = np.all((m.xyz >= np.array(b[:3]) - pad) & (m.xyz <= np.array(b[3:]) + pad), axis=1)
            keep &= ~inside
        far[name] = float(vm[keep].max()) if keep.any() else 0.0
    return {"compliance": comp, "reaction": reaction, "max_disp": umax, "max_vm_far": far}


# ------------------------------------------------------------------------------------------------
# the run

def _rel(q: dict, ref: dict, far_key: str) -> dict:
    out = {k: (q[k] - ref[k]) / ref[k] for k in ("compliance", "reaction", "max_disp") if ref.get(k)}
    a, b = q["max_vm_far"].get(far_key), ref["max_vm_far"].get(far_key)
    if a is not None and b:
        out["max_vm_far"] = (a - b) / b
    return out


def _job(args):
    """Run one mesh-and-solve in its OWN process. Measured on nist_ctc_02: a native abort inside
    gmsh/OCCT took a whole ProcessPoolExecutor down (BrokenProcessPool) and lost the part; as a
    subprocess, a crash costs one row.

    `modes` are the high-order settings to try, in order. Only the ORIGINAL may fall back from 1
    (curved, optimised) to 0 (straight-sided); every variant then runs in the original's mode alone,
    because an original meshed one way compared with a variant meshed the other measures the
    element type, not the feature."""
    name, brep, work, lc, h, excl, keep, *rest = args
    modes = tuple(rest[0]) if rest else (1,)
    wd = Path(work)
    wd.mkdir(parents=True, exist_ok=True)
    spec = wd / "spec.json"
    res = wd / "solved.json"
    # Measured on KFL08 (FreeCAD-library housing): gmsh's high-order optimiser ABORTS the process
    # ("Failed to reach critical value in pass 1 for measure(s): ScaledJac") on some curved parts.
    # One retry without it; the run records which it took, because the two meshes differ.
    for ho in modes:
        spec.write_text(json.dumps({"brep": brep, "work": work, "lc": lc, "h": h, "exclude": excl, "keep": keep,
                                    "ho_optimize": ho}))
        res.unlink(missing_ok=True)
        try:
            p = subprocess.run([sys.executable, "-I", __file__, "--one", str(spec)], capture_output=True, text=True,
                               timeout=7200)
        except subprocess.TimeoutExpired:
            return name, asdict(Solved(False, "timed out"))
        if p.returncode == 0 and res.exists():
            out = json.loads(res.read_text())
            out["ho_optimize"] = ho
            return name, out
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        if "critical value" not in tail[0]:
            break
    return name, asdict(Solved(False, f"worker died: {tail[0][:200]}"))


def _one(spec_path: str) -> int:
    sp = json.loads(Path(spec_path).read_text())
    try:
        r = mesh_and_solve(sp["brep"], sp["work"], sp["lc"], sp["h"], sp["exclude"], keep_field=sp["keep"],
                           ho_optimize=sp.get("ho_optimize", 1))
    except Exception as exc:   # one bad feature must not end the run
        r = Solved(False, f"{type(exc).__name__}: {exc}")
    (Path(sp["work"]) / "solved.json").write_text(json.dumps(asdict(r), default=float))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--one"]:
        return _one(argv[1])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--size", type=float, default=30.0, help="background element size = diagonal / SIZE")
    ap.add_argument("--max-features", type=int, default=60)
    ap.add_argument("--no-wall-rule", action="store_true",
                    help="size by the diagonal only (the bench's behaviour before PLAN-fea-3 was applied)")
    ap.add_argument("--keep-fields", action="store_true",
                    help="save each run's nodal U and S (work/<name>/field.npz) for local.py")
    a = ap.parse_args(argv)
    if not shutil.which("ccx"):
        raise SystemExit("ccx (CalculiX) not on PATH")
    out = Path(a.out)
    (out / "geom").mkdir(parents=True, exist_ok=True)

    shape, diag, lc = prepare(a.step)
    # PLAN-fea-3's rule, which the product's FEA plan applies and this bench did not: at least two
    # elements through every wall. Measured on nist_ftc_09 (a 3 mm plate, h 11 mm): without it,
    # removing features let the mesher coarsen the plate to one element through its thickness, and
    # the stiffening that caused was read as the features' effect (a local check on its accepted
    # package came out 29% stiff against a true 0.28%).
    t_wall = wall_thickness(shape, diag)
    h = diag / a.size
    if t_wall and not a.no_wall_rule:
        h = min(h, t_wall / 2)
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
        jobs.append((f.id, str(path), str(out / "work" / f.id), asdict(lc), h, {f.id: [(f.bbox, pad)]},
                     a.keep_fields))

    # the original reads its far stress under every feature's exclusion, so each row compares like
    # with like; "none" (nothing excluded) is what the noise run is compared on
    excl_all["none"] = []
    base = [("original", str(out / "geom" / "original.brep"), str(out / "work" / "original"), asdict(lc), h, excl_all,
             a.keep_fields),
            ("noise", str(out / "geom" / "original.brep"), str(out / "work" / "noise"), asdict(lc), h * 0.93, excl_all,
             False)]
    results: dict[str, dict] = {}
    t0 = time.time()
    # the original first, alone: its high-order mode is the part's mode (see _job)
    name, res = _job(base[0] + ((1, 0),))
    results[name] = res
    mode = (res.get("ho_optimize", 1),)
    print(f"[{time.time() - t0:7.1f}s] original: {'ok' if res['ok'] else res['why']} elements={res['elements']} "
          f"high-order mode {mode[0]}", flush=True)
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = [ex.submit(_job, j + (mode,)) for j in base[1:] + jobs]
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
    summary = {"part": a.step, "diag": diag, "h": h, "t_wall": t_wall, "wall_rule": not a.no_wall_rule,
               "load_case": asdict(lc), "volume": v0, "ho_optimize": mode[0],
               "n_features": len(feats), "original": ref, "noise": noise, "features": rows,
               "wall_s": time.time() - t0}
    (out / "result.json").write_text(json.dumps(summary, indent=1, default=float))
    print(f"wrote {out / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
