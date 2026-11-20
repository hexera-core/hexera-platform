# Responsibility: Judge a CAD simplification by simulation before anything is kept: one driver solve of the simplified part, then per removed feature a local piece cut from the ORIGINAL, solved with and without the feature under the driver's displacements, giving each feature's estimated compliance change and the package's verdict against the error budget.
# Boundaries: measures and judges; it writes no STEP, promotes nothing and decides no run's outcome. gmsh and CalculiX run only in a freshly exec'd child interpreter (never a fork, never this process: gmsh installs signal handlers and the worker calls from a thread), and CalculiX at ONE thread. The parent does the OpenCASCADE geometry.
# Collaborates with: cad/simplify/simplify.py (calls verify_package), cad/simplify/recognise.py (face descriptions), cad/repair/mesh_probe.py (the child-interpreter pattern this copies), devtools/simplify/harness.py and local.py (the measured method this ports; the bench, not imported).
"""THE ONE-DRIVER T2 CHECK, AS MEASURED ON THE BENCH.

THE LOAD CASE is canonical because uploads carry none: the part is trimmed 3% off each end of its
longest axis (a copy used only for solving), the low cut face is clamped and the high one displaced
along the second-longest axis by 1e-3 of the length. Displacement control on exact planar faces is
the same continuum problem for every mesh of every variant (DEVLOG: a node-band clamp put a 1.07%
noise floor under compliance; trimmed faces 0.04%).

THE DRIVER is ONE global solve of the fully simplified part. Each removed feature then gets a local
region - its box padded by max(3 x radius, its own zone + 4 local cells) - cut from the trimmed
ORIGINAL; the feature is found again inside that piece and defeatured THERE (a region cut from the
defeatured part was a shape OCCT called invalid, and gmsh then meshed a twelfth of it). Both pieces
have every cut face, and any trimmed end face inside the region, driven by the driver's field, and
are solved with CalculiX. The estimate is

    est = -(W_s - W_o) / W_global

(displacement control: a local stiffening that raises the local energy raises the stiffness by
dW / W to first order, and the compliance moves the other way). The CUT CHECK compares the resultant
force on each cut plane with and without the feature: if removing it changed what the cut carries,
the region was too small to judge it, and the feature is rejected.

THE VERDICT (approved for slice 2): a feature is accepted when |est| <= margin x budget (0.5 x
0.1%: with one shared driver that margin made no false accept on nist_ftc_09); the package keeps
accepted features, least error first, while the sum of their |est| stays inside the budget. For FEA
the sum of member errors bounded the package's true error on every part measured.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: steel; the problem is linear and displacement-controlled, so material and magnitude cancel from
#: every relative figure - these only make the deck a physical one
E_MPA, NU = 210_000.0, 0.3
#: the load case trims this fraction off each end of the longest axis (harness.prepare)
TRIM_FRACTION = 0.03
#: the driver's background size is the diagonal over this, capped at half the thinnest wall
#: (PLAN-fea-3: at least two elements through every wall)
SIZE_DIVISOR = 30.0
#: a local piece is meshed this much finer than the driver
REFINE = 2.0
#: a local region pads the feature by at least this many radii
REGION_K = 3.0
#: the cut check: the largest change of any cut plane's resultant, relative to the largest
#: resultant, before a region counts as too small. NOT CALIBRATED: the bench records the mismatch
#: but never thresholded it (the two T2 misses it investigated read 0.008, so it is a weak guard);
#: on the synthetic plate of tests/native a hole read 0.043 with its estimate 3% off the global
#: answer, so the limit sits above that.
CUT_MISMATCH_LIMIT = 0.10
#: a mesh whose tets do not add up to the CAD volume within this is refused (harness.mesh_brep:
#: gmsh once meshed a twelfth of a region without raising, and the piece read 29% stiff)
MESH_VOLUME_TOLERANCE = 0.005
#: the volume of a regular tetrahedron of edge h is this times h cubed: the element estimate a
#: size is checked against before anything is meshed
_TET_VOLUME = 1.0 / (6.0 * math.sqrt(2.0))
#: local pieces solved at once, each its own child with CalculiX on one thread
PIECE_JOBS = 2
#: seconds of the deadline kept back from each child, so a child killed on time still answers
CHILD_MARGIN_S = 2.0

DRIVER = "driver"
PIECE = "piece"


class VerifyUnavailable(RuntimeError):
    """The check cannot run here (no CalculiX, no gmsh). Our limitation, never a verdict."""


# ------------------------------------------------------------------------------------------------
# the load case


@dataclass(frozen=True, slots=True)
class LoadCase:
    axis: int          # the longest bounding-box axis
    lo: float
    hi: float
    band: float        # trimmed this far in from each end; the cut faces carry the conditions
    bend_dir: int      # the axis the displaced end moves along (the second-longest)

    @property
    def lo_cut(self) -> float:
        return self.lo + self.band

    @property
    def hi_cut(self) -> float:
        return self.hi - self.band

    def to_dict(self) -> dict:
        return {"axis": self.axis, "lo": self.lo, "hi": self.hi, "band": self.band,
                "bend_dir": self.bend_dir}

    @classmethod
    def from_dict(cls, d: Mapping) -> LoadCase:
        return cls(int(d["axis"]), float(d["lo"]), float(d["hi"]), float(d["band"]),
                   int(d["bend_dir"]))


def load_case(bbox: Sequence[float]) -> LoadCase:
    x0, y0, z0, x1, y1, z1 = (float(v) for v in bbox)
    ext = np.array([x1 - x0, y1 - y0, z1 - z0])
    order = np.argsort(-ext, kind="stable")
    k = int(order[0])
    return LoadCase(k, (x0, y0, z0)[k], (x1, y1, z1)[k], TRIM_FRACTION * float(ext[k]), int(order[1]))


def zone_pad(radius: float, diag: float) -> float:
    """A feature's own zone: the padding inside which its effect is its own (harness: 2r + 2% diag)."""
    return 2.0 * float(radius) + 0.02 * float(diag)


def near_ends(bbox: Sequence[float], lc: LoadCase, pad: float) -> bool:
    """Within the trimmed end bands plus `pad`: such a feature's faces carry or crowd the boundary
    condition, so removing it changes the problem rather than the discretisation."""
    return bbox[lc.axis] < lc.lo_cut + pad or bbox[lc.axis + 3] > lc.hi_cut - pad


def mesh_size(diag: float, wall: float | None) -> float:
    h = float(diag) / SIZE_DIVISOR
    return min(h, wall / 2.0) if wall else h


def estimated_tets(volume: float, h: float) -> int:
    return int(abs(volume) / (_TET_VOLUME * h ** 3)) if h > 0 else 0


# ------------------------------------------------------------------------------------------------
# geometry (parent process, OpenCASCADE)


def bbox_of(shape) -> tuple[float, float, float, float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    x0, y0, z0, x1, y1, z1 = (float(v) for v in b.Get())
    return (x0, y0, z0, x1, y1, z1)


def volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return float(g.Mass())


def is_valid(shape) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer
    return bool(BRepCheck_Analyzer(shape).IsValid())


def _box(lo: Sequence[float], hi: Sequence[float]):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()


def common(shape, tool):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    c = BRepAlgoAPI_Common(shape, tool)
    c.Build()
    return c.Shape() if c.IsDone() else None


def trim(shape, lc: LoadCase, diag: float):
    """The solving copy: the part cut by two planes across its longest axis (harness.prepare)."""
    bb = bbox_of(shape)
    lo = [v - 0.01 * diag for v in bb[:3]]
    hi = [v + 0.01 * diag for v in bb[3:]]
    lo[lc.axis], hi[lc.axis] = lc.lo_cut, lc.hi_cut
    out = common(shape, _box(lo, hi))
    if out is None:
        raise RuntimeError("the load case's end trim failed")
    return out


def write_brep(shape, path: Path) -> None:
    from OCP.BRepTools import BRepTools
    BRepTools.Write_s(shape, str(path))


@dataclass(frozen=True, slots=True)
class FaceSig:
    """A face named by what it IS, so it can be found again in another shape that contains it
    unchanged (a region cut from the part, or the part after another feature was removed)."""
    kind: str
    area: float
    centroid: tuple[float, float, float]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "area": self.area, "centroid": list(self.centroid)}


def face_sigs(shape) -> list[tuple[FaceSig, Any]]:
    from meshpipeline.cad.simplify.recognise import _describe, _faces
    faces, _ = _faces(shape)
    out = []
    for f in faces:
        d = _describe(f)
        cx, cy, cz = (float(v) for v in d.centroid)
        out.append((FaceSig(d.kind, float(d.area), (cx, cy, cz)), f))
    return out


def find_faces(shape, sigs: Sequence[FaceSig], tol: float, *, index=None) -> list | None:
    """The faces of `shape` matching every signature, or None when any one is missing."""
    index = face_sigs(shape) if index is None else index
    found = []
    for s in sigs:
        hit = None
        for t, f in index:
            if (t.kind == s.kind and abs(t.area - s.area) <= 1e-5 * max(abs(s.area), 1e-12)
                    and math.dist(t.centroid, s.centroid) <= tol):
                hit = f
                break
        if hit is None:
            return None
        found.append(hit)
    return found


def _face_count(shape) -> int:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return m.Extent()


def defeature(shape, faces) -> tuple[Any | None, str]:
    """One BRepAlgoAPI_Defeaturing call over `faces`; the result, or None and why.

    IsDone() is TRUE when OCCT could not remove the faces and handed the input back (it records a
    warning instead) - measured on nist_ftc_09, twelve "removed" blends with dV ~ 1e-15. So an
    unchanged face count and volume is a no-op, refused here, not a free simplification."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
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
    if not is_valid(out):
        return None, "the defeatured shape is not valid"
    v0 = volume(shape)
    if _face_count(out) == _face_count(shape) and abs(volume(out) - v0) <= 1e-9 * abs(v0):
        return None, "defeaturing left the shape unchanged"
    return out, ""


def wall_thickness(shape, diag: float, *, percentile: float = 10.0, max_rays: int = 3000) -> float | None:
    """The thin-wall thickness: rays cast INWARD from surface points to where they leave the solid,
    the given percentile of those distances (harness.wall_thickness). None when no ray hit. Hits
    under 0.1% of the diagonal are a ray clipping the neighbouring face beside an edge and are
    dropped, so a wall thinner than that is not seen."""
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    from meshpipeline.cad.occ_box import incremental_mesh
    from meshpipeline.cad.simplify.recognise import _faces
    incremental_mesh(shape, 2e-3 * diag, 0.3)
    pts, nrm, area = [], [], []
    for f in _faces(shape)[0]:
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        p = np.array([[q.X(), q.Y(), q.Z()] for q in
                      (tri.Node(i).Transformed(tr) for i in range(1, tri.NbNodes() + 1))])
        flip = f.Orientation() == TopAbs_REVERSED
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = (v - 1 for v in tri.Triangle(k).Get())
            n = np.cross(p[b] - p[a], p[c] - p[a])
            ar = 0.5 * float(np.linalg.norm(n))
            if ar <= 0:
                continue
            n = n / (2 * ar)
            pts.append((p[a] + p[b] + p[c]) / 3)
            nrm.append(n if flip else -n)      # inward: OCCT triangles wind outward on a FORWARD face
            area.append(ar)
    if not pts:
        return None
    rng = np.random.default_rng(0)
    w = np.asarray(area) / sum(area)
    pick = rng.choice(len(pts), size=min(max_rays, len(pts)), replace=len(pts) < max_rays, p=w)
    ints = IntCurvesFace_ShapeIntersector()
    ints.Load(shape, 1e-6 * diag)
    eps = 1e-5 * diag
    hits = []
    for i in pick:
        o = pts[i] + eps * nrm[i]
        ints.Perform(gp_Lin(gp_Pnt(*o), gp_Dir(*nrm[i])), 0.0, diag)
        ws = [ints.WParameter(j) for j in range(1, ints.NbPnt() + 1)]
        ws = [x for x in ws if x > 1e-3 * diag]
        if ws:
            hits.append(min(ws) + eps)
    return float(np.percentile(hits, percentile)) if hits else None


# ------------------------------------------------------------------------------------------------
# one job: mesh and solve, in the child (gmsh and CalculiX)


@dataclass
class Meshed:
    tags: np.ndarray        # node tags
    xyz: np.ndarray         # (n, 3)
    etags: np.ndarray
    conn: np.ndarray        # (m, 10), CalculiX C3D10 order
    sets: dict              # name -> sorted node tags on the surfaces picked for it
    seconds: float = 0.0


def _row_index(tags: np.ndarray) -> np.ndarray:
    idx = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
    idx[tags] = np.arange(len(tags))
    return idx


def mesh_volume(m: Meshed) -> float:
    """The tets' volume from their corner nodes (straight-edged; curved mid-nodes move it a little)."""
    p = m.xyz[_row_index(m.tags)[m.conn[:, :4]]]
    return float(np.abs(np.einsum("ij,ij->i", np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]),
                                  p[:, 3] - p[:, 0])).sum() / 6.0)


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


def mesh_brep(brep: str, h: float, pick: Callable[[Sequence[float]], str | None],
              ho: int = 1) -> Meshed | str:
    """Mesh a BREP with 10-node tets; `pick(bbox)` names each surface's node set. The reason as a
    string when there is no mesh, or one that does not fill the part (harness.mesh_brep)."""
    import gmsh

    from meshpipeline.engines.gmsh.abaqus_deck import GMSH_TET10_TO_SOLVER
    t0 = time.monotonic()
    gmsh.initialize(readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.model.occ.importShapes(brep)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", h / 200)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        gmsh.option.setNumber("Mesh.HighOrderOptimize", ho)
        # without the optimiser curved 10-node tets fold (CalculiX "nonpositive jacobian");
        # straight-sided quadratic elements cannot, at the price of a faceted boundary
        gmsh.option.setNumber("Mesh.SecondOrderLinear", 0 if ho else 1)
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
    m = Meshed(np.asarray(tags, dtype=np.int64), np.asarray(xyz, dtype=float).reshape(-1, 3),
               np.asarray(et, dtype=np.int64), conn, sets, time.monotonic() - t0)
    v_cad, v_mesh = _brep_volume(brep), mesh_volume(m)
    if v_cad and abs(v_mesh - v_cad) > MESH_VOLUME_TOLERANCE * abs(v_cad):
        return (f"mesh volume {v_mesh:.6g} differs from the CAD volume {v_cad:.6g} by "
                f"{(v_mesh - v_cad) / v_cad:+.2%}")
    return m


#: (node tags, dof, values): a prescribed displacement on one degree of freedom of those nodes
Boundary = tuple[np.ndarray, int, np.ndarray]


def read_dat(path: Path) -> tuple[dict[tuple[str, str], dict[int, np.ndarray]], float | None]:
    """CalculiX's .dat: every per-node block ("forces (fx,fy,fz) for set X", "displacements
    (vx,vy,vz) for set X") as {(kind, SET): {node: vector}}, and the total internal energy."""
    blocks: dict[tuple[str, str], dict[int, np.ndarray]] = {}
    energy = None
    lines = path.read_text().splitlines() if path.exists() else []
    cur: dict[int, np.ndarray] | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        low = ln.strip().lower()
        if "total internal energy" in low:
            cur = None
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    energy = float(nxt.split()[-1])
                    break
        elif low.startswith(("forces (fx,fy,fz)", "displacements (vx,vy,vz)")):
            kind = "forces" if low.startswith("forces") else "displacements"
            words = low.split()
            name = words[words.index("set") + 1].upper() if "set" in words else ""
            cur = blocks.setdefault((kind, name), {})
        elif cur is not None:
            parts = ln.split()
            if len(parts) == 4:
                try:
                    cur[int(parts[0])] = np.array([float(v) for v in parts[1:]])
                except ValueError:
                    cur = None
            elif parts:
                cur = None
        i += 1
    return blocks, energy


def solve_ccx(m: Meshed, work: Path, bcs: Sequence[Boundary], *, reaction_sets: Sequence[str],
              want_field: bool, budget_s: float) -> dict:
    """Write the deck, run CalculiX on ONE thread, read energy, per-set reactions and (optionally)
    the displacement field. ONE THREAD: ccx 2.21 at 16 threads gave a wrong answer in 2 of 11 runs
    of the same deck (DEVLOG, fast_solve)."""
    ccx = shutil.which("ccx")
    if ccx is None:
        return {"ok": False, "why": "ccx (CalculiX) is not on PATH"}
    work.mkdir(parents=True, exist_ok=True)
    with (work / "job.inp").open("w") as fh:
        fh.write("*NODE, NSET=NALL\n")
        for t, (x, y, z) in zip(m.tags, m.xyz, strict=True):
            fh.write(f"{int(t)},{x:.9g},{y:.9g},{z:.9g}\n")
        fh.write("*ELEMENT, TYPE=C3D10, ELSET=EALL\n")
        for e, c in zip(m.etags, m.conn, strict=True):
            fh.write(f"{int(e)}," + ",".join(str(int(v)) for v in c) + "\n")
        for name in reaction_sets:
            fh.write(f"*NSET, NSET={name}\n" + "\n".join(map(str, m.sets[name])) + "\n")
        fh.write(f"*MATERIAL, NAME=STEEL\n*ELASTIC\n{E_MPA},{NU}\n"
                 "*SOLID SECTION, ELSET=EALL, MATERIAL=STEEL\n*STEP\n*STATIC\n*BOUNDARY\n")
        for nodes, dof, vals in bcs:
            for n, v in zip(nodes, np.broadcast_to(vals, np.shape(nodes)), strict=True):
                fh.write(f"{int(n)},{dof},{dof},{float(v):.9g}\n")
        for name in reaction_sets:
            fh.write(f"*NODE PRINT, NSET={name}\nRF\n")
        if want_field:
            fh.write("*NODE PRINT, NSET=NALL\nU\n")
        fh.write("*EL PRINT, ELSET=EALL, TOTALS=ONLY\nELSE\n*END STEP\n")
    # the thread pins go on the command line through env(1): the child inherits everything else,
    # and this module reads no environment of its own
    pins = ["OMP_NUM_THREADS=1", "CCX_NPROC_EQUATION_SOLVER=1", "CCX_NPROC_STIFFNESS=1",
            "CCX_NPROC_RESULTS=1"]
    t1 = time.monotonic()
    try:
        p = subprocess.run(["env", *pins, ccx, "-i", "job"], cwd=work, capture_output=True, text=True,
                           timeout=max(1.0, budget_s), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "the solve ran past its time"}
    solve_s = time.monotonic() - t1
    blocks, energy = read_dat(work / "job.dat")
    if p.returncode != 0 or energy is None:
        tail = (p.stdout or "").strip().splitlines()[-1:] or [f"exit {p.returncode}"]
        return {"ok": False, "why": f"ccx failed: {tail[0][:200]}", "solve_s": solve_s}
    reactions = {}
    for name in reaction_sets:
        rf = blocks.get(("forces", name.upper()), {})
        reactions[name] = (np.sum(list(rf.values()), axis=0).tolist() if rf else [0.0, 0.0, 0.0])
    out: dict = {"ok": True, "energy": energy, "reactions": reactions, "solve_s": solve_s}
    if want_field:
        disp = blocks.get(("displacements", "NALL"), {})
        u = np.zeros((len(m.tags), 3))
        idx = _row_index(m.tags)
        for n, v in disp.items():
            if 0 <= n < len(idx) and idx[n] >= 0:
                u[idx[n]] = v
        out["u"] = u
    return out


def _driver_pick(lc: LoadCase):
    tol = 1e-5 * (lc.hi - lc.lo)

    def pick(b):
        if b[lc.axis + 3] - b[lc.axis] > tol:
            return None
        if abs(b[lc.axis] - lc.lo_cut) <= tol:
            return "NFIX"
        if abs(b[lc.axis] - lc.hi_cut) <= tol:
            return "NLOAD"
        return None
    return pick


def plane_of(b: Sequence[float], region: Sequence[float], tol: float, lc: LoadCase | None) -> str | None:
    """Which cut plane a piece's surface lies on ('CX0'...'CZ1'), 'TRIM' for one of the load case's
    trimmed end faces, or None for a surface of the part itself (local._plane_of). A trimmed face
    inside a region is DRIVEN like a cut face: left free, holes 23 mm from the clamp read 6x low."""
    if lc is not None and b[lc.axis + 3] - b[lc.axis] <= tol:
        for val in (lc.lo_cut, lc.hi_cut):
            if abs(b[lc.axis] - val) <= tol:
                return "TRIM"
    for k, ax in enumerate("XYZ"):
        if b[k + 3] - b[k] <= tol:
            for side, val in (("0", region[k]), ("1", region[k + 3])):
                if abs(b[k] - val) <= tol:
                    return f"C{ax}{side}"
    return None


def _interpolator(npz: str):
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
    g = np.load(npz)
    pts, u = g["xyz"], g["u"]
    lin, near = LinearNDInterpolator(pts, u), NearestNDInterpolator(pts, u)

    def at(q):
        v = lin(q)
        bad = np.isnan(v).any(axis=1)
        if bad.any():
            v[bad] = near(q[bad])
        return v
    return at


def run_job(spec: Mapping, *, mesher=mesh_brep, solver=solve_ccx, deadline: float | None = None) -> dict:
    """Mesh and solve one job in THIS process. The child's body; callable in-process with a fake
    `mesher` and `solver` so the boundary conditions and the reading can be tested without either.

    spec: kind (driver|piece), brep, h, ho, lc, max_tets, work; a piece adds region, tol and
    drive (an npz of the driver's xyz and u near the region); a driver adds field (where to save
    its own xyz and u)."""
    budget = (deadline - time.monotonic()) if deadline is not None else float(spec.get("budget_s", 3600))
    lc = LoadCase.from_dict(spec["lc"])
    kind = spec["kind"]
    if kind == DRIVER:
        pick = _driver_pick(lc)
    else:
        region, tol = spec["region"], float(spec["tol"])

        def pick(b):
            return plane_of(b, region, tol, lc)
    m = mesher(str(spec["brep"]), float(spec["h"]), pick, int(spec.get("ho", 1)))
    if isinstance(m, str):
        return {"ok": False, "why": m}
    out: dict = {"nodes": len(m.tags), "elements": len(m.conn), "mesh_s": m.seconds}
    if len(m.conn) > int(spec.get("max_tets", 10 ** 9)):
        return {**out, "ok": False, "why": f"{len(m.conn)} tets is more than this check solves"}
    remaining = budget - m.seconds if deadline is None else deadline - time.monotonic()
    work = Path(spec["work"])
    work.mkdir(parents=True, exist_ok=True)
    if kind == DRIVER:
        if not m.sets.get("NFIX") or not m.sets.get("NLOAD"):
            return {**out, "ok": False, "why": "no trimmed end faces to load"}
        delta = 1e-3 * (lc.hi - lc.lo)
        fix = np.asarray(m.sets["NFIX"])
        bcs = [(fix, 1, np.zeros(len(fix))), (fix, 2, np.zeros(len(fix))), (fix, 3, np.zeros(len(fix))),
               (np.asarray(m.sets["NLOAD"]), lc.bend_dir + 1, np.full(len(m.sets["NLOAD"]), delta))]
        r = solver(m, work, bcs, reaction_sets=["NLOAD"], want_field=True, budget_s=remaining)
        if not r.get("ok"):
            return {**out, **r, "ok": False}
        if not r["reactions"]["NLOAD"][lc.bend_dir]:
            return {**out, "ok": False, "why": "no reaction at the displaced end"}
        np.savez(spec["field"], xyz=m.xyz, u=r["u"])
        return {**out, "ok": True, "energy": r["energy"], "solve_s": r.get("solve_s", 0.0),
                "reaction": r["reactions"]["NLOAD"], "field": str(spec["field"])}
    cut_sets = sorted(k for k in m.sets if k.startswith("C"))
    if not cut_sets:
        return {**out, "ok": False, "why": "no cut faces"}
    # driven: every cut face and any trimmed end face inside the region; the cut check reads only
    # the cut planes
    driven = np.asarray(sorted({n for k in cut_sets + (["TRIM"] if "TRIM" in m.sets else [])
                                for n in m.sets[k]}), dtype=np.int64)
    u = _interpolator(str(spec["drive"]))(m.xyz[_row_index(m.tags)[driven]])
    bcs = [(driven, k + 1, u[:, k]) for k in range(3)]
    r = solver(m, work, bcs, reaction_sets=cut_sets, want_field=False, budget_s=remaining)
    if not r.get("ok"):
        return {**out, **r, "ok": False}
    return {**out, "ok": True, "energy": r["energy"], "solve_s": r.get("solve_s", 0.0),
            "planes": r["reactions"]}


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def run_job_in_child(spec: Mapping, budget_s: float) -> dict:
    """Run one job in a fresh interpreter, bounded by `budget_s` (cad/repair/mesh_probe._run_child).
    NEVER raises: a timeout, a crash or an unreadable answer is a failed job."""
    work = Path(spec["work"])
    work.mkdir(parents=True, exist_ok=True)
    spec_path, answer = work / "spec.json", work / "answer.json"
    spec_path.write_text(json.dumps({**spec, "budget_s": budget_s, "answer": str(answer)}))
    cmd = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.simplify.verify", str(spec_path)]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, start_new_session=True, text=True)
    except OSError as exc:
        return {"ok": False, "why": f"the solver process did not start: {type(exc).__name__}"}
    try:
        _, err = proc.communicate(timeout=max(budget_s, 0.0))
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "why": "timed out", "timed_out": True}
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    try:
        return dict(json.loads(answer.read_text()))
    except (OSError, ValueError):
        tail = (err or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return {"ok": False, "why": f"the solver process died: {tail[0][:200]}"}


def _child_main(argv: Sequence[str]) -> int:
    """`python -m meshpipeline.cad.simplify.verify SPEC`: one job, its answer written beside it."""
    spec = json.loads(Path(argv[0]).read_text())
    deadline = time.monotonic() + float(spec.get("budget_s", 3600)) - 1.0
    try:
        res = run_job(spec, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - one bad job is a failed row, not a dead parent
        res = {"ok": False, "why": f"{type(exc).__name__}: {exc}"[:300]}
    Path(spec["answer"]).write_text(json.dumps(res, default=float))
    return 0


# ------------------------------------------------------------------------------------------------
# the package


@dataclass(frozen=True, slots=True)
class FeatureToVerify:
    id: str
    kind: str
    radius: float
    bbox: tuple[float, float, float, float, float, float]
    faces: tuple[FaceSig, ...]


@dataclass(frozen=True, slots=True)
class FeatureEstimate:
    id: str
    ok: bool
    estimate: float | None = None      # -(W_s - W_o) / W_global: the compliance change it costs
    cut_mismatch: float | None = None
    w_original: float | None = None
    w_simplified: float | None = None
    elements: tuple[int, int] = (0, 0)
    why: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "estimate": self.estimate,
                "cut_mismatch": self.cut_mismatch, "w_original": self.w_original,
                "w_simplified": self.w_simplified, "elements": list(self.elements), "why": self.why}


@dataclass(frozen=True, slots=True)
class FeatureVerdict:
    id: str
    accepted: bool
    reason: str               # accepted | over_screen | over_budget | cut_mismatch | not_verified
    estimate: float | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "accepted": self.accepted, "reason": self.reason,
                "estimate": self.estimate, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PackageVerdict:
    verdicts: tuple[FeatureVerdict, ...]
    estimates: tuple[FeatureEstimate, ...]
    budget: float
    margin: float
    #: the sum of the accepted members' |estimate|: the error the package spends from the budget
    spent: float = 0.0
    #: the signed sum, for the reader who wants the direction
    estimated_change: float = 0.0
    driver: dict = field(default_factory=dict)
    #: why no feature could be judged at all, or "" when the check ran
    abstained: str = ""

    @property
    def accepted(self) -> tuple[str, ...]:
        return tuple(v.id for v in self.verdicts if v.accepted)

    def to_dict(self) -> dict:
        return {"accepted": list(self.accepted), "budget": self.budget, "margin": self.margin,
                "spent": self.spent, "estimated_change": self.estimated_change,
                "abstained": self.abstained, "driver": dict(self.driver),
                "verdicts": [v.to_dict() for v in self.verdicts],
                "estimates": [e.to_dict() for e in self.estimates]}


def judge(estimates: Sequence[FeatureEstimate], *, budget: float, margin: float,
          cut_limit: float = CUT_MISMATCH_LIMIT) -> tuple[tuple[FeatureVerdict, ...], float, float]:
    """Each feature's verdict, then the ledger: accepted features enter least error first while the
    summed |estimate| stays within the budget. Returns (verdicts, spent, signed change)."""
    verdicts: dict[str, FeatureVerdict] = {}
    screened: list[FeatureEstimate] = []
    for e in estimates:
        if not e.ok or e.estimate is None:
            verdicts[e.id] = FeatureVerdict(e.id, False, "not_verified", None, e.why)
        elif e.cut_mismatch is not None and e.cut_mismatch > cut_limit:
            verdicts[e.id] = FeatureVerdict(e.id, False, "cut_mismatch", e.estimate,
                                            f"cut resultant moved {e.cut_mismatch:.3g}; the region "
                                            "was too small to judge it")
        elif abs(e.estimate) > margin * budget:
            verdicts[e.id] = FeatureVerdict(e.id, False, "over_screen", e.estimate)
        else:
            screened.append(e)
    spent, change = 0.0, 0.0
    for e in sorted(screened, key=lambda x: (abs(x.estimate or 0.0), x.id)):
        cost = abs(e.estimate or 0.0)
        if spent + cost <= budget:
            spent += cost
            change += float(e.estimate or 0.0)
            verdicts[e.id] = FeatureVerdict(e.id, True, "accepted", e.estimate)
        else:
            verdicts[e.id] = FeatureVerdict(e.id, False, "over_budget", e.estimate)
    return tuple(verdicts[e.id] for e in estimates), spent, change


def tools_available() -> str:
    """'' when this host can run the check, else what is missing."""
    import importlib.util
    if shutil.which("ccx") is None:
        return "CalculiX (ccx) is not installed"
    if importlib.util.find_spec("gmsh") is None:
        return "gmsh is not installed"
    if importlib.util.find_spec("scipy") is None:
        return "scipy is not installed"
    return ""


def _abstain(features, why: str, *, budget: float, margin: float, driver=None) -> PackageVerdict:
    est = tuple(FeatureEstimate(f.id, False, why=why) for f in features)
    return PackageVerdict(judge(est, budget=budget, margin=margin)[0], est, budget, margin,
                          driver=dict(driver or {}), abstained=why)


def verify_package(original, simplified, features: Sequence[FeatureToVerify], *, budget: float,
                   margin: float, deadline: float, max_tets: int, runner=None,
                   jobs: int = PIECE_JOBS) -> PackageVerdict:
    """Every removed feature's estimate from ONE driver solve of `simplified`, and the verdicts.

    `original` and `simplified` are whole, untrimmed shapes; the trim is made here. `runner(spec,
    budget_s) -> dict` runs one job (default: a fresh child interpreter). Raises VerifyUnavailable
    only when the default runner cannot run on this host; everything else that goes wrong is a
    feature, or the whole package, NOT VERIFIED - never accepted."""
    if runner is None:
        missing = tools_available()
        if missing:
            raise VerifyUnavailable(missing)
        runner = run_job_in_child
    features = list(features)
    bb = bbox_of(original)
    diag = math.dist(bb[:3], bb[3:])
    lc = load_case(bb)
    t_orig, t_simp = trim(original, lc, diag), trim(simplified, lc, diag)
    wall = wall_thickness(t_orig, diag)
    h = mesh_size(diag, wall)
    driver_info: dict = {"h": h, "wall": wall, "diag": diag, "load_case": lc.to_dict()}
    est = estimated_tets(volume(t_simp), h)
    driver_info["estimated_tets"] = est
    if est > max_tets:
        return _abstain(features, f"the part needs about {est} tets at {h:.3g} (half its thinnest "
                        f"wall), more than this check solves ({max_tets})", budget=budget,
                        margin=margin, driver=driver_info)

    def left() -> float:
        return deadline - time.monotonic() - CHILD_MARGIN_S

    with tempfile.TemporaryDirectory(prefix="simplify-verify-") as td:
        root = Path(td)
        write_brep(t_simp, root / "driver.brep")
        drv: dict = {}
        ho = 1
        # gmsh's high-order optimiser ABORTS the process on some curved parts (measured on KFL08):
        # one retry straight-sided; every piece then uses the driver's mode, because a piece meshed
        # the other way would measure the element type, not the feature
        for ho in (1, 0):
            if left() <= 0:
                drv = {"ok": False, "why": "out of time before the driver solve"}
                break
            drv = runner({"kind": DRIVER, "brep": str(root / "driver.brep"), "h": h, "ho": ho,
                          "lc": lc.to_dict(), "max_tets": max_tets, "work": str(root / f"driver{ho}"),
                          "field": str(root / f"driver{ho}" / "field.npz")}, left())
            # a mesh too large to solve is as large straight-sided: no retry for that
            if drv.get("ok") or drv.get("timed_out") or "tets is more" in str(drv.get("why", "")):
                break
        driver_info.update({k: drv.get(k) for k in ("elements", "nodes", "mesh_s", "solve_s", "energy")},
                           ho=ho)
        w_global = drv.get("energy")
        if not drv.get("ok") or not w_global:
            return _abstain(features, f"the driver solve failed: {drv.get('why', 'no energy')}",
                            budget=budget, margin=margin, driver=driver_info)
        field_npz = np.load(drv["field"])
        gxyz, gu = field_npz["xyz"], field_npz["u"]

        h_loc = h / REFINE
        tol = 1e-4 * diag
        pending: list[tuple[FeatureToVerify, dict, dict]] = []
        results: dict[str, FeatureEstimate] = {}
        # THE GEOMETRY IS PREPARED HERE, ONE FEATURE AT A TIME: OpenCASCADE Booleans on one shared
        # shape are not something to run from several threads. Only the solves run in parallel.
        for f in features:
            zpad = zone_pad(f.radius, diag)
            pad = max(REGION_K * f.radius, zpad + 4 * h_loc)
            region = (f.bbox[0] - pad, f.bbox[1] - pad, f.bbox[2] - pad,
                      f.bbox[3] + pad, f.bbox[4] + pad, f.bbox[5] + pad)
            if all(region[i] <= bb[i] and region[i + 3] >= bb[i + 3] for i in range(3)):
                results[f.id] = FeatureEstimate(f.id, False, why="its region covers the whole part")
                continue
            piece = common(t_orig, _box(region[:3], region[3:]))
            if piece is None:
                results[f.id] = FeatureEstimate(f.id, False, why="the region cut failed")
                continue
            faces = find_faces(piece, f.faces, tol)
            if faces is None:
                results[f.id] = FeatureEstimate(f.id, False, why="the feature is not whole inside its region")
                continue
            piece_s, why = defeature(piece, faces)
            if piece_s is None:
                results[f.id] = FeatureEstimate(f.id, False, why=f"in its region: {why}")
                continue
            est_p = estimated_tets(volume(piece), h_loc)
            if est_p > max_tets:
                results[f.id] = FeatureEstimate(f.id, False, why=f"its region needs about {est_p} tets")
                continue
            wd = root / f.id
            wd.mkdir()
            write_brep(piece, wd / "orig.brep")
            write_brep(piece_s, wd / "simp.brep")
            lo, hi = np.array(region[:3]) - 2 * h, np.array(region[3:]) + 2 * h
            near = np.all((gxyz >= lo) & (gxyz <= hi), axis=1)
            if int(near.sum()) < 4:
                results[f.id] = FeatureEstimate(f.id, False, why="the driver has no nodes in its region")
                continue
            np.savez(wd / "drive.npz", xyz=gxyz[near], u=gu[near])
            base = {"kind": PIECE, "h": h_loc, "ho": ho, "lc": lc.to_dict(), "max_tets": max_tets,
                    "region": list(region), "tol": tol, "drive": str(wd / "drive.npz")}
            pending.append((f, {**base, "brep": str(wd / "orig.brep"), "work": str(wd / "o")},
                            {**base, "brep": str(wd / "simp.brep"), "work": str(wd / "s")}))

        def _run(spec: dict) -> dict:
            budget_s = left()
            if budget_s <= 0:
                return {"ok": False, "why": "out of time"}
            try:
                return runner(spec, budget_s)
            except Exception as exc:  # noqa: BLE001 - a runner that breaks fails its job only
                return {"ok": False, "why": f"{type(exc).__name__}"}

        specs = [s for _, so, ss in pending for s in (so, ss)]
        if jobs <= 1:
            answers = [_run(s) for s in specs]
        else:
            with ThreadPoolExecutor(max_workers=jobs) as ex:
                answers = list(ex.map(_run, specs))
        for n, (f, _, _) in enumerate(pending):
            po, ps = answers[2 * n], answers[2 * n + 1]
            results[f.id] = _estimate(f.id, po, ps, float(w_global))

    estimates = tuple(results.get(f.id, FeatureEstimate(f.id, False, why="not reached")) for f in features)
    verdicts, spent, change = judge(estimates, budget=budget, margin=margin)
    return PackageVerdict(verdicts, estimates, budget, margin, spent=spent, estimated_change=change,
                          driver=driver_info)


def _estimate(fid: str, po: Mapping, ps: Mapping, w_global: float) -> FeatureEstimate:
    if not po.get("ok") or not ps.get("ok"):
        return FeatureEstimate(fid, False, why=str(po.get("why") or ps.get("why") or "a piece failed"))
    wo, ws = po.get("energy"), ps.get("energy")
    if wo is None or ws is None:
        return FeatureEstimate(fid, False, why="a piece reported no energy")
    planes = sorted(set(po.get("planes") or {}) & set(ps.get("planes") or {}))
    mismatch = None
    if planes:
        fo = np.array([po["planes"][p] for p in planes], dtype=float)
        fs = np.array([ps["planes"][p] for p in planes], dtype=float)
        scale = max(float(np.linalg.norm(fo, axis=1).max()), 1e-30)
        mismatch = float(np.linalg.norm(fo - fs, axis=1).max() / scale)
    return FeatureEstimate(fid, True, estimate=-(float(ws) - float(wo)) / w_global,
                           cut_mismatch=mismatch, w_original=float(wo), w_simplified=float(ws),
                           elements=(int(po.get("elements", 0)), int(ps.get("elements", 0))))


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
