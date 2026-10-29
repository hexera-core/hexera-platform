# Responsibility: Extract a thin-walled part's MID-SURFACE from its B-rep: each even wall pair's mid-face (the
# overlap of the two faces each offset half the wall inward), each mid-face extended to meet the mid-face it
# runs into at a junction (T, L, cross), all of them sewn into one connected surface per body - and the
# checks that it is faithful (half the wall from both faces, every face accounted for, no open edge inside the
# material). With the record of which body is a shell, which is solid and which is mixed.
# Boundaries: geometry only, deterministic, no model call, no persistence beyond the files it is asked to
# write. It meshes nothing (the shell mesher reads its surface and record). OpenCASCADE throughout: offsets
# (BRepOffsetAPI_MakeOffsetShape, simple mode), Booleans and the general fuse (BOPAlgo_Builder), one thread.
# Collaborates with: cad/shell_pairs.py (the pairs and the face roles), cad/fea_measure.py (the labelled
# surface, Face_k order, face radii), cad/fea_assembly.py (Part_k_Face_j names), docs/design/shell/README.md.
"""THE SHELL RECORD (shell.json) AND THE MID-SURFACE (midsurface.brep).

    {"version": 1, "unit": "mm", "ratio": 0.1, "bodies": [{"body": "Part_1", "verdict": "shell" | "mixed" |
     "solid", "why", "area_mm2", "volume_mm3", "shell_area_share", "mid_volume_ratio", "solid_regions": [...]}],
     "pairs": [{"id": "P1", "faces": [..], "thickness_mm", "spread", "rays", "even", "why", "width_mm",
                "ratio", "shell", "mids": ["Mid_1"]}],
     "faces": [{"face", "kind", "role", "pairs", "body"}...],
     "mids": [{"id": "Mid_1", "pair": "P1", "body": "Part_1", "faces": [..], "thickness_mm", "area_mm2",
               "kind", "extended": [{"to": "Mid_2", "by_mm": ..}], "fidelity": {"max_error_mm", "points"}}],
     "sewn": {"faces", "edges", "shared_edges", "junction_edges", "boundary_edges", "open_edges",
              "open_at_mm": [...]},
     "checks": {"fidelity": bool, "connected": bool, "faces_accounted": bool, "volume": bool},
     "complete": bool, "notes": [...], "seconds"}

midsurface.brep holds one compound: every sewn mid-face, in the order of "surface_faces" in the record (each
entry: the mid-face it came from, so its thickness and its source faces).
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np

from meshpipeline.cad import shell_pairs as sp

logger = logging.getLogger(__name__)

VERSION = 1
RECORD_FILE = "shell.json"
SURFACE_FILE = "midsurface.brep"
#: EXTENSION (SHELL-3): a mid-face's straight edge is carried on to the mid-plane it runs into when every one
#: of EXTEND_SAMPLES points along it, moved outward in the face's plane, meets that plane within the reach -
#: half the target's wall plus EXTEND_WALLS times the thicker of the two walls (a fillet up to twice the wall
#: at the junction is bridged) - and the whole way there is inside the part's material. The edge then runs
#: along the line where the two mid-planes meet. Planes within NOT_PARALLEL_DEG of parallel are not met.
EXTEND_SAMPLES = (0.2, 0.5, 0.8)
EXTEND_WALLS = 2.0
NOT_PARALLEL_DEG = 5.0
#: A mid-face edge lying within JOINED_SHARE of a wall from another mid-face already meets it (a sheet's bend:
#: the flange's mid-plane and the bend's mid-cylinder share their edge) and is not extended.
JOINED_SHARE = 1e-3
#: THE SEW (SHELL-4): the mid-faces of one body are fused (general fuse) with this fuzzy tolerance, as a share
#: of the thinnest wall: edges that meet within it become one edge, a face another face's edge lies on is split
#: there - a conformal shell, shared nodes at every junction.
SEW_SHARE = 1e-3
#: ... but never below SEW_TOL_FACTOR times the largest tolerance the mid-faces carry themselves (offset B-spline
#: walls of a lofted cyclone funnel came out of the overlap with 0.0125 mm on 5 mm walls, and a fuzzy value
#: under that broke the general fuse), up to SEW_SHARE_MAX of the thinnest wall
SEW_TOL_FACTOR = 4.0
SEW_SHARE_MAX = 1e-2
#: AN OPEN EDGE: a free edge of the sewn surface (one face) whose outward side, OPEN_STEP of the wall past
#: it, is still inside the material - a junction the mid-faces did not reach. A real boundary (a plate's
#: rim, a hole) has the outside there - read also at each of SLIT_STEPS of the wall nearer the edge: outside
#: anywhere along the way is a real cut, however narrow (a lanced slit 0.3 mm wide in a 1.6 mm panel has the
#: next web's metal 0.48 mm past it).
OPEN_STEP = 0.3
SLIT_STEPS = (0.05, 0.15)
#: ... and no open edge is shorter than FLOOR_SHARE of its wall: the shell mesh's floor (SHMESH-2, no element
#: under half the wall) cannot see it, so no junction can be there (the 0.23 mm side of a sheet's rounded tongue
#: tip). Such an edge is counted apart, as below the floor.
FLOOR_SHARE = 0.5
#: FIDELITY (SHELL-5): every mid-face point read (FIDELITY_SAMPLES x FIDELITY_SAMPLES over its parameters)
#: lies half the wall from both of its faces within FIDELITY_SHARE of the wall.
FIDELITY_SAMPLES = 5
FIDELITY_SHARE = 0.02
#: THE VOLUME CHECK: a shell body's mid-faces times their walls hold its volume within VOLUME_BAND (rims,
#: blends and the corners of junctions are counted once, or not at all).
VOLUME_BAND = (0.85, 1.10)


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _pt(p) -> list[float]:
    return [_r(x) for x in np.asarray(p, dtype=float)]


# ----------------------------------------------------------------------------------- OCC helpers ----
def _map(shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    m = _map(shape, TopAbs_FACE)
    return [TopoDS.Face_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _area(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, g)
    return abs(float(g.Mass()))


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return abs(float(g.Mass()))


def _length(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.LinearProperties_s(shape, g)
    return abs(float(g.Mass()))


def _compound(shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _dist(a, b) -> float:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    d = BRepExtrema_DistShapeShape(a, b)
    return float(d.Value()) if d.IsDone() else float("inf")


def _vertex(p):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt

    return BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, p))).Vertex()


def width_of(shape) -> float:
    """A face's (or a set of faces') in-plane WIDTH, in its own units: the narrower of the middle side of its
    oriented bounding box and 4 x area / outer perimeter (a strip's width, a square's side; an L-shaped plate
    reads its arms, not its box)."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepTools import BRepTools

    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape, obb, True, True, False)
    sides = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))
    area = _area(shape)
    per = sum(_length(BRepTools.OuterWire_s(f)) for f in _faces(shape))
    w = sides[1]
    if per > 0:
        w = min(w, 4.0 * area / per)
    return float(w)


def _full_turn(face) -> bool:
    """Does a face on a cylinder or a cone go the whole way round its axis (its angle range a full turn)?"""
    from OCP.BRepTools import BRepTools

    u0, u1, _v0, _v1 = BRepTools.UVBounds_s(face)
    return (u1 - u0) >= 2.0 * math.pi * (1.0 - 1e-6)


def strip_width(face) -> float:
    """A face's STRIP width, its own units: twice its area over its whole perimeter (holes included) - a
    strip's width, a ring's radial width, a hole's bore depth; never more than its oriented box's middle
    side. A plate's rim reads the plate's thickness."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib

    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(face, obb, True, True, False)
    sides = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))
    per = _length(face)
    return float(min(sides[1], 2.0 * _area(face) / per)) if per > 0 else float(sides[1])


def _plane(face):
    """(origin, unit normal, x axis, y axis) of a planar face, numpy, the face's own coordinates; None for
    any other surface."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane

    a = BRepAdaptor_Surface(face, True)
    if a.GetType() != GeomAbs_Plane:
        return None
    pos = a.Plane().Position()

    def v(d):
        return np.array([d.X(), d.Y(), d.Z()], dtype=float)

    return v(pos.Location()), v(pos.Direction()), v(pos.XDirection()), v(pos.YDirection())


#: a point farther than this (the shape's own units) from the face's surface is not in the face: OCC's own face
#: classifier's gap check (BRepClass_FaceClassifier, 0.1), kept as it was
IN_FACE_GAP = 0.1
#: a piece of the general fuse is a piece of a source face when a point inside it lies within the ownership
#: tolerance of that face and inside it this share of that tolerance clear of its edges
IN_FACE_SHARE = 1.0


def _in_range(face, surf, uv):
    """The point's parameters moved by whole periods into the face's own range (a closed surface's projection
    comes back in its first period; a cone face may run from pi to 3 pi)."""
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d

    u0, _u1, v0, _v1 = BRepTools.UVBounds_s(face)
    u, v = uv.X(), uv.Y()
    if surf.IsUPeriodic():
        per = surf.UPeriod()
        u = u0 + (u - u0) % per
    if surf.IsVPeriodic():
        per = surf.VPeriod()
        v = v0 + (v - v0) % per
    return gp_Pnt2d(u, v)


def _free_form() -> tuple:
    from OCP.Geom import Geom_BezierSurface, Geom_BSplineSurface, Geom_OffsetSurface

    return (Geom_BSplineSurface, Geom_BezierSurface, Geom_OffsetSurface)


#: the surfaces whose parameters are no lengths or angles (_in_face)
FREE_FORM = _free_form()


#: one surface analyser per face, kept: building one reads the whole surface (a B-spline hull's: 5 ms), and the
#: same faces are asked about thousands of times (the extension and the edge census)
_ANALYSERS: dict[int, tuple] = {}
ANALYSERS_KEPT = 4096


def _analyser(face):
    """The face's surface (as BRep_Tool gives it, placed) and its ShapeAnalysis_Surface, built once per face."""
    from OCP.BRep import BRep_Tool
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    key = hash(face)
    got = _ANALYSERS.get(key)
    if got is not None and got[0].IsSame(face):
        return got[1], got[2]
    if len(_ANALYSERS) >= ANALYSERS_KEPT:
        _ANALYSERS.clear()
    surf = BRep_Tool.Surface_s(face)
    sa = ShapeAnalysis_Surface(surf)
    _ANALYSERS[key] = (face, surf, sa)
    return surf, sa


def _in_face(face, p, tol) -> bool:
    """Does point p (on or near the face's surface) lie inside the face, `tol` (its own units) clear of its edges?
    OCC's face classifier reads its tolerance in the surface's PARAMETERS. On the analytic surfaces those are
    lengths or angles; on a free-form one (B-spline, Bezier, an offset of either) they run 0..1 over the whole
    surface, so 0.05 read as 26 mm of a 500 mm wall and a point well inside a grown band came back ON its edge.
    So on a free-form surface the tolerance is turned into parameters first, by how fast the surface runs at the
    point."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_IN

    q = gp_Pnt(*map(float, p))
    surf = BRep_Tool.Surface_s(face)
    if isinstance(surf, FREE_FORM):
        try:
            surf, sa = _analyser(face)
            uv = _in_range(face, surf, sa.ValueOfUV(q, float(tol)))
            if sa.Gap() > max(tol, IN_FACE_GAP):
                return False                # off the surface: not in the face (as OCC's own classifier says)
            at, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
            surf.D1(uv.X(), uv.Y(), at, du, dv)
            speed = max(du.Magnitude(), dv.Magnitude())
            if speed > 0:
                c = BRepClass_FaceClassifier()
                c.Perform(face, uv, float(tol) / speed)
                return c.State() == TopAbs_IN
        except Exception:  # noqa: BLE001,S110 - the classifier on the point itself below
            pass
    return BRepClass_FaceClassifier(face, q, tol).State() == TopAbs_IN


#: THE QUICK INSIDE TEST: OpenCASCADE's solid classifier reads every face of the body for every point (47 ms a point,
#: 1,300 s of one 26-minute wave-energy float). So a point clear of the body's skin is told by its own closed
#: triangulation instead - a copy of the body meshed within QUICK_DEFLECTION of the thinnest wall, three rays
#: from the point counting the triangles they cross (odd: inside), all three agreeing. "Clear" is farther than
#: QUICK_CLEAR deflections plus twice the tolerance from every triangle: the true skin lies within the deflection of
#: its triangles, so no skin passes between the point and where the triangles say. A point nearer the skin, rays
#: that disagree, a body whose triangulation is not closed: the exact classifier, as before. The same answer, read
#: faster.
QUICK_DEFLECTION = 0.005
QUICK_CLEAR = 4.0
QUICK_ANGLE = 0.35
#: the three ray directions (fixed, off every axis and diagonal: a ray along a mesh edge or a face plane is rare)
QUICK_RAYS = ((0.5376, 0.6232, 0.5679), (-0.7081, 0.2214, 0.6705), (0.1523, -0.8734, 0.4625))


class _QuickInside:
    """A closed triangulation of a copy of the body and a cell locator on it; `state(p)` is True (inside), False
    (outside) or None (not clear of the skin, or the rays disagree: ask the exact classifier)."""

    def __init__(self, solid, deflection: float, tol: float):
        from OCP.BRep import BRep_Tool
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopLoc import TopLoc_Location
        from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData, vtkStaticCellLocator
        from vtkmodules.vtkFiltersFlowPaths import vtkModifiedBSPTree

        self.ok = False
        copy = BRepBuilderAPI_Copy(solid, True, False).Shape()     # the body's own faces keep their meshes
        BRepMesh_IncrementalMesh(copy, float(deflection), False, QUICK_ANGLE, False)
        xyz, tris, base = [], [], 0
        for f in _faces(copy):
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(f, loc)
            if tri is None or tri.NbTriangles() == 0:
                return                                              # a face with no mesh: no quick test
            tr = loc.Transformation()
            for k in range(1, tri.NbNodes() + 1):
                q = tri.Node(k).Transformed(tr)
                xyz.append((q.X(), q.Y(), q.Z()))
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                tris.append((base + a - 1, base + b - 1, base + c - 1))
            base += tri.NbNodes()
        pts_np = np.asarray(xyz, dtype=float)
        tri_np = np.asarray(tris, dtype=np.int64)
        # closed: every triangle edge is shared by an even number of triangles once the faces' copies of their shared
        # edge nodes are welded (they are the same points of the same edge)
        def closed(keys) -> bool:
            _, weld = np.unique(keys, axis=0, return_inverse=True)
            w = weld.ravel()[tri_np]
            edges = np.sort(np.concatenate([w[:, [0, 1]], w[:, [1, 2]], w[:, [2, 0]]]), axis=1)
            edges = edges[edges[:, 0] != edges[:, 1]]
            _, counts = np.unique(edges, axis=0, return_counts=True)
            return len(counts) > 0 and not np.any(counts % 2)

        if not (closed(pts_np) or closed(np.round(pts_np / max(tol, 1e-12)))):
            return
        pts = vtkPoints()
        pts.SetData(numpy_to_vtk(np.ascontiguousarray(pts_np), deep=True))
        cells = vtkCellArray()
        offsets = np.arange(0, 3 * len(tri_np) + 1, 3, dtype=np.int64)
        cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True),
                      numpy_to_vtkIdTypeArray(np.ascontiguousarray(tri_np.ravel()), deep=True))
        pd = vtkPolyData()
        pd.SetPoints(pts)
        pd.SetPolys(cells)
        self.loc = vtkStaticCellLocator()           # the nearest triangle
        self.loc.SetDataSet(pd)
        self.loc.BuildLocator()
        self.rays = vtkModifiedBSPTree()            # every triangle a ray crosses (the static locator missed some)
        self.rays.SetDataSet(pd)
        self.rays.BuildLocator()
        self.pd = pd
        lo, hi = pts_np.min(axis=0), pts_np.max(axis=0)
        self.reach = 2.0 * float(np.linalg.norm(hi - lo)) + 1.0
        self.clear = QUICK_CLEAR * float(deflection) + 2.0 * tol
        self.ok = True

    def state(self, p) -> bool | None:
        from vtkmodules.vtkCommonCore import mutable, vtkIdList, vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkGenericCell

        if not self.ok:
            return None
        x = [float(p[0]), float(p[1]), float(p[2])]
        closest = [0.0, 0.0, 0.0]
        cid, sub, d2 = mutable(0), mutable(0), mutable(0.0)
        self.loc.FindClosestPoint(x, closest, vtkGenericCell(), cid, sub, d2)  # type: ignore[call-overload]
        if float(d2) <= self.clear * self.clear:  # type: ignore[arg-type]
            return None
        votes = []
        for d in QUICK_RAYS:
            end = [x[i] + self.reach * d[i] for i in range(3)]
            hit_pts, hit_ids = vtkPoints(), vtkIdList()
            self.rays.IntersectWithLine(x, end, 0.0, hit_pts, hit_ids)
            votes.append(hit_ids.GetNumberOfIds() % 2 == 1)
        return votes[0] if votes[0] == votes[1] == votes[2] else None


class _Solid:
    """Is a point inside a body (or on its skin)? A point outside the body's box is outside without the classifier,
    a point clear of the skin is told by the body's triangulation (_QuickInside), and every point's state is kept:
    the joins and the edge census ask about the same points again and again."""

    def __init__(self, solid, tol: float, wall: float | None = None):
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier

        self.c = BRepClass3d_SolidClassifier(solid)
        self.tol = tol
        self.quick = None
        if wall is not None and wall > 0:
            try:
                self.quick = _QuickInside(solid, QUICK_DEFLECTION * wall, tol)
            except Exception as exc:  # noqa: BLE001 - the exact classifier alone
                logger.debug("shell: no quick inside test for a body (%s)", exc)
                self.quick = None
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        b = Bnd_Box()
        BRepBndLib.Add_s(solid, b, False)       # the exact geometry, not a mesh inside a curved face
        x0, y0, z0, x1, y1, z1 = b.Get()
        self.lo = np.array([x0, y0, z0]) - 2.0 * tol
        self.hi = np.array([x1, y1, z1]) + 2.0 * tol
        self.states: dict[tuple[float, float, float], int] = {}

    def _state(self, p):
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_OUT

        key = (float(p[0]), float(p[1]), float(p[2]))
        st = self.states.get(key)
        if st is None:
            from OCP.TopAbs import TopAbs_IN

            quick = None
            if any(key[i] < self.lo[i] or key[i] > self.hi[i] for i in range(3)):
                st = TopAbs_OUT
            elif self.quick is not None and (quick := self.quick.state(key)) is not None:
                st = TopAbs_IN if quick else TopAbs_OUT
            else:
                self.c.Perform(gp_Pnt(*key), self.tol)
                st = self.c.State()
            self.states[key] = st
        return st

    def inside(self, p) -> bool:
        from OCP.TopAbs import TopAbs_IN, TopAbs_ON

        return self._state(p) in (TopAbs_IN, TopAbs_ON)

    def strictly_inside(self, p) -> bool:
        from OCP.TopAbs import TopAbs_IN

        return self._state(p) == TopAbs_IN


# ------------------------------------------------------------------------------------ mid-faces ----
def half_offset(face, half: float):
    """The face offset `half` (its own units) INTO the material: OpenCASCADE's simple offset along the
    face's oriented normal (outward), so a negative distance."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape

    mk = BRepOffsetAPI_MakeOffsetShape()
    mk.PerformBySimple(face, -float(half))
    if not mk.IsDone():
        raise ValueError("the face could not be offset")
    return mk.Shape()


def overlap(a, b, fuzzy: float) -> list:
    """The faces where two faces on one surface overlap (a Boolean common, one thread)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.TopTools import TopTools_ListOfShape

    c = BRepAlgoAPI_Common()
    la, lb = TopTools_ListOfShape(), TopTools_ListOfShape()
    la.Append(a)
    lb.Append(b)
    c.SetArguments(la)
    c.SetTools(lb)
    c.SetFuzzyValue(fuzzy)
    c.SetRunParallel(False)
    c.SetNonDestructive(True)
    c.Build()
    if not c.IsDone():
        return []
    return [f for f in _faces(c.Shape()) if _area(f) > 0]


def _centre_point(face):
    """The point at the middle of the face's parameter box when it lies inside the face, else None."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    u, v = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
    if not all(math.isfinite(x) for x in (u, v)):
        return None
    if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-9).State() != TopAbs_IN:
        return None
    q = BRepAdaptor_Surface(face, False).Value(u, v)
    return np.array([q.X(), q.Y(), q.Z()])


def _centre_gap(shape, other) -> float | None:
    """How far the middle of an offset face lies from `other` (a point to a face), or None when its middle is not
    inside it."""
    try:
        q = _centre_point(_faces(shape)[0])
    except Exception:  # noqa: BLE001 - no quick reading
        return None
    return None if q is None else _dist(_vertex(q), other)


def toward(face, other, half: float):
    """The face offset `half` toward `other` (its wall partner): the inward offset, checked by distance - the
    simple offset follows the surface's own normal, which is not the face's outward side on every surface (a
    revolved cone's), so the side that lands half the wall from `other` is taken. Read first from the offset's
    middle (a point to a face): when that lies half the wall from `other`, it is the side. Else face to face, the
    nearer of the two sides (the face to face distance ran 90 s on one offset B-spline pair of a plywood hull, and
    the middle alone misreads a small bend whose partner lies to one side)."""
    first = half_offset(face, half)
    quick = _centre_gap(first, other)
    if quick is not None and abs(quick - half) <= 0.05 * half:
        return first
    d1 = _dist(first, other)
    if abs(d1 - half) <= 0.05 * half:
        return first
    second = half_offset(face, -half)
    return second if abs(_dist(second, other) - half) < abs(d1 - half) else first


def mid_faces(fa, fb, t: float, fuzzy: float) -> list:
    """The mid-faces of an even wall pair: both faces offset half the wall toward each other, and where they
    overlap."""
    return overlap(toward(fa, fb, t / 2.0), toward(fb, fa, t / 2.0), fuzzy)


# ----------------------------------------------------------------------------- fitted mid-faces ----
#: SHELL-9, A MID-SURFACE FITTED THROUGH THE MIDPOINTS: for a TAPERED wall (shell_pairs.tapered_wall), and for an
#: even wall whose two half-offsets do not overlap (two free-form faces whose offsets are not the same surface: a
#: twisted strap). Over a FIT_SAMPLES x FIT_SAMPLES grid of face A's parameters, each point of A is paired with its
#: foot on B's surface; their midpoint is moved along the line between them until it lies as far from A's surface
#: as from B's (FIT_ITERATIONS steps). A cubic B-spline surface interpolates the grid of midpoints, its parameters
#: A's own scaled to 0..1 (iso-parametric), so A's outline maps onto it exactly; B's outline is projected onto it,
#: and the mid-face is where the two overlap. The wall's thickness at a point is its distance to A plus its
#: distance to B. A fitted mid-face is kept only when every point read on it lies as far from A as from B within
#: FIDELITY_SHARE of the local thickness.
FIT_SAMPLES = 17
#: a pair whose two faces are both narrower than four walls by this margin is not fitted (its mid-face would be
#: rejected as narrower than four walls; the margin covers a mid-face a little wider than either face)
FIT_WIDTH_SLACK = 1.5
FIT_ITERATIONS = 3
FIT_EDGE_SAMPLES = 24


def _np(p) -> np.ndarray:
    return np.array([p.X(), p.Y(), p.Z()])


def _surface_distance(proj, x: np.ndarray) -> float | None:
    from OCP.gp import gp_Pnt

    proj.Perform(gp_Pnt(*map(float, x)))
    return float(proj.LowerDistance()) if proj.NbPoints() > 0 else None


def _midpoint(pa_proj, pb_proj, p: np.ndarray) -> tuple[np.ndarray, float] | None:
    """The point midway between A and B on the line from A's point `p` to its foot on B, and the wall there."""
    from OCP.gp import gp_Pnt

    pb_proj.Perform(gp_Pnt(*map(float, p)))
    if pb_proj.NbPoints() < 1:
        return None
    q = _np(pb_proj.NearestPoint())
    d = q - p
    n = float(np.linalg.norm(d))
    if n <= 0:
        return None
    d /= n
    m = 0.5 * (p + q)
    for _ in range(FIT_ITERATIONS):
        ra, rb = _surface_distance(pa_proj, m), _surface_distance(pb_proj, m)
        if ra is None or rb is None:
            return None
        m = m + d * 0.5 * (rb - ra)
    da, db = _surface_distance(pa_proj, m), _surface_distance(pb_proj, m)
    if da is None or db is None:
        return None
    return m, da + db


def _mapped_curve(edge, face, box):
    """The edge's curve on `face`, as a 2D B-spline in the face's parameters scaled by `box` to 0..1 (exact: an
    affine map of a B-spline's poles)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d
    from OCP.Geom2d import Geom2d_TrimmedCurve
    from OCP.Geom2dConvert import Geom2dConvert
    from OCP.gp import gp_Pnt2d

    a = BRepAdaptor_Curve2d(edge, face)
    c = Geom2dConvert.CurveToBSplineCurve_s(Geom2d_TrimmedCurve(a.Curve(), a.FirstParameter(),
                                                                a.LastParameter()))
    u0, u1, v0, v1 = box
    for i in range(1, c.NbPoles() + 1):
        q = c.Pole(i)
        c.SetPole(i, gp_Pnt2d((q.X() - u0) / (u1 - u0), (q.Y() - v0) / (v1 - v0)))
    from OCP.TopAbs import TopAbs_REVERSED

    if edge.Orientation() == TopAbs_REVERSED:
        c.Reverse()                      # run the way the wire runs: each new edge is forward in its wire
    return c


def _wire_on(surf, curves: list):
    """A wire on `surf` through 2D curves given in order, end to end (each edge with its 3D curve), or None."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepLib import BRepLib

    mw = BRepBuilderAPI_MakeWire()
    for c in curves:
        me = BRepBuilderAPI_MakeEdge(c, surf)
        if not me.IsDone():
            return None
        e = me.Edge()
        if not BRepLib.BuildCurve3d_s(e, 1e-7):
            return None
        mw.Add(e)
        if not mw.IsDone():
            return None
    return mw.Wire()


def _wire_edges(wire, face) -> list:
    from OCP.BRepTools import BRepTools_WireExplorer

    out = []
    ex = BRepTools_WireExplorer(wire, face)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out


def _curves_of_a(fa, box) -> list[list]:
    """Face A's outline and holes as 2D curves in the fitted surface's parameters (outline first)."""
    return [[_mapped_curve(e, fa, box) for e in _wire_edges(w, fa)] for w in [_outer_wire(fa), *_inner_wires(fa)]]


def _region_on(surf, wires: list[list]):
    """The face on `surf` bounded by 2D-curve wires (outline first), or None."""
    built = [_wire_on(surf, w) for w in wires]
    if not built or any(w is None for w in built):
        return None
    return _face_with_holes(surf, built[0], built[1:])


def _overlap_2d(wires_a: list[list], wires_b: list[list], surf, fuzzy: float) -> list:
    """The overlap of two regions given as 2D-curve wires in the fitted surface's parameters, worked out FLAT (in
    the plane of the parameters, where a Boolean is plain: on a fitted surface closed round a cylinder's seam it
    found nothing) and laid back onto `surf`."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.Geom import Geom_Plane
    from OCP.Geom2d import Geom2d_TrimmedCurve
    from OCP.GeomAPI import GeomAPI
    from OCP.gp import gp_Pln
    from OCP.TopAbs import TopAbs_REVERSED

    pln = gp_Pln()
    flat = Geom_Plane(pln)

    def flat_region(wires):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire

        built = []
        for w in wires:
            mw = BRepBuilderAPI_MakeWire()
            for c in w:
                mw.Add(BRepBuilderAPI_MakeEdge(GeomAPI.To3d_s(c, pln)).Edge())
                if not mw.IsDone():
                    return None
            built.append(mw.Wire())
        return _face_with_holes(flat, built[0], built[1:])

    ra, rb = flat_region(wires_a), flat_region(wires_b)
    if ra is None or rb is None:
        return []
    out = []
    for f in overlap(ra, rb, fuzzy):
        wires = []
        for w in [_outer_wire(f), *_inner_wires(f)]:
            curves = []
            for e in _wire_edges(w, f):
                a = BRepAdaptor_Curve(e)
                c = Geom2d_TrimmedCurve(GeomAPI.To2d_s(a.Curve().Curve(), pln), a.FirstParameter(), a.LastParameter())
                curves.append(c.Reversed() if e.Orientation() == TopAbs_REVERSED else c)
            wires.append(curves)
        g = _region_on(surf, wires)
        if g is not None:
            out.append(g)
    return out


def _edge_points(edge, k: int) -> list[np.ndarray]:
    """k + 1 points along an edge, in the edge's own direction in its wire."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_REVERSED

    c = BRepAdaptor_Curve(edge)
    f, l = c.FirstParameter(), c.LastParameter()
    pts = [_np(c.Value(f + (l - f) * i / k)) for i in range(k + 1)]
    return pts[::-1] if edge.Orientation() == TopAbs_REVERSED else pts


def _covers(fb, fa) -> bool:
    """Every point read along face A's edges has its foot on face B (B lies across the whole of A)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt, gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    proj = sp._projector(fb)
    tol = max(BRep_Tool.Tolerance_s(fb), 1e-7)
    for w in [_outer_wire(fa), *_inner_wires(fa)]:
        for e in _wire_edges(w, fa):
            for p in _edge_points(e, FIT_EDGE_SAMPLES // 4):
                proj.Perform(gp_Pnt(*map(float, p)))
                if proj.NbPoints() < 1:
                    return False
                u, v = proj.LowerDistanceParameters()
                if BRepClass_FaceClassifier(fb, gp_Pnt2d(u, v), tol).State() not in (TopAbs_IN, TopAbs_ON):
                    return False
    return True


def _curves_of_b(fb, fa, box) -> list[list] | None:
    """Face B's outline and holes projected onto A's surface, as 2D curves in the fitted surface's parameters
    (outline first), or None. Points on a periodic surface are kept continuous with the one before, and the whole
    outline is moved by whole periods onto A's range."""
    from OCP.BRep import BRep_Tool
    from OCP.Geom2dAPI import Geom2dAPI_PointsToBSpline
    from OCP.gp import gp_Pnt, gp_Pnt2d
    from OCP.TColgp import TColgp_Array1OfPnt2d

    sa = BRep_Tool.Surface_s(fa)
    proj = sp._projector(fa)
    u0, u1, v0, v1 = box
    per_u = sa.UPeriod() if sa.IsUPeriodic() else None
    per_v = sa.VPeriod() if sa.IsVPeriodic() else None
    last = [0.5 * (u0 + u1), 0.5 * (v0 + v1)]

    def near(x: float, ref: float, per: float | None) -> float:
        return x if per is None else x + per * round((ref - x) / per)

    def points(w) -> list[np.ndarray] | None:
        """Each edge's points in A's parameters scaled to 0..1, end to end and closed."""
        out: list[np.ndarray] = []
        for e in _wire_edges(w, fb):
            uv = []
            for p in _edge_points(e, FIT_EDGE_SAMPLES):
                proj.Perform(gp_Pnt(*map(float, p)))
                if proj.NbPoints() < 1:
                    return None
                u, v = proj.LowerDistanceParameters()
                u, v = near(u, last[0], per_u), near(v, last[1], per_v)
                last[0], last[1] = u, v
                uv.append(((u - u0) / (u1 - u0), (v - v0) / (v1 - v0)))
            if out:
                uv[0] = tuple(out[-1][-1])
            out.append(np.array(uv))
        if not out or np.linalg.norm(out[-1][-1] - out[0][0]) > 1e-6:
            return None              # the outline did not close (it wound round a periodic surface)
        out[-1][-1] = out[0][0]
        return out

    def shifted(wires: list, per: float | None, axis: int, span: float) -> None:
        """Move the wires by whole periods onto A's range 0..1 (most overlap; a whole turn lands on it)."""
        if per is None:
            return
        step = per / span
        lo = min(float(c[:, axis].min()) for c in wires[0])
        hi = max(float(c[:, axis].max()) for c in wires[0])
        best = max(range(-3, 4), key=lambda k: (min(hi + k * step, 1.0) - max(lo + k * step, 0.0), -abs(k)))
        for w in wires:
            for c in w:
                c[:, axis] += best * step

    def wire(w):
        curves = []
        for uv in w:
            arr = TColgp_Array1OfPnt2d(1, len(uv))
            for i, (s, t) in enumerate(uv, 1):
                arr.SetValue(i, gp_Pnt2d(float(s), float(t)))
            fit = Geom2dAPI_PointsToBSpline(arr, 3, 3)
            if not fit.IsDone():
                return None
            curves.append(fit.Curve())
        return curves

    wires = [points(_outer_wire(fb))]
    if wires[0] is None:
        return None
    for w in _inner_wires(fb):
        wires.append(points(w))
        if wires[-1] is None:
            return None
    shifted(wires, per_u, 0, u1 - u0)
    shifted(wires, per_v, 1, v1 - v0)
    out = [wire(w) for w in wires]
    return None if any(c is None for c in out) else out


def _seamed(face, tol: float):
    """The face with its own coincident boundary edges sewn into one (a seam), when that leaves one face of the
    same area; else the face as it was."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    sw = BRepBuilderAPI_Sewing(tol)
    sw.Add(face)
    sw.Perform()
    out = _faces(sw.SewedShape())
    if len(out) == 1 and abs(_area(out[0]) - _area(face)) <= 1e-6 * _area(face):
        return out[0]
    return face


def _valid_faces(face) -> list:
    """The face when it is valid; else as OpenCASCADE's shape fixer leaves it, when that is valid; else none."""
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.ShapeFix import ShapeFix_Shape

    if BRepCheck_Analyzer(face).IsValid():
        return [face]
    fx = ShapeFix_Shape(face)
    fx.Perform()
    out = [f for f in _faces(fx.Shape()) if _area(f) > 0]
    return out if out and all(BRepCheck_Analyzer(f).IsValid() for f in out) else []


def fitted_fidelity(mid, fa, fb) -> tuple[float, float, int, float, float]:
    """A fitted mid-face read on a FIDELITY_SAMPLES grid: (the largest |dA - dB| / 2, its own units; the largest
    |dA - dB| / (dA + dB); points read; the thinnest and thickest dA + dB)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    a = BRepAdaptor_Surface(mid, True)
    u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
    worst, rel, n, lo, hi = 0.0, 0.0, 0, math.inf, 0.0
    k = FIDELITY_SAMPLES
    for i in range(k):
        for j in range(k):
            u, v = u0 + (u1 - u0) * (i + 0.5) / k, v0 + (v1 - v0) * (j + 0.5) / k
            if BRepClass_FaceClassifier(mid, gp_Pnt2d(u, v), 1e-9).State() != TopAbs_IN:
                continue
            vx = _vertex(_np(a.Value(u, v)))
            da, db = _dist(vx, fa), _dist(vx, fb)
            worst = max(worst, 0.5 * abs(da - db))
            rel = max(rel, abs(da - db) / (da + db) if da + db > 0 else math.inf)
            lo, hi = min(lo, da + db), max(hi, da + db)
            n += 1
    return worst, rel, n, lo, hi


def fitted_mid(fa, fb, t_hint: float, fuzzy: float) -> tuple[list, float, float, float, str]:
    """SHELL-9: the fitted mid-faces of faces fa, fb ([] when none can be made, with why), and the wall's thinnest,
    thickest and mean thickness over them (own units). `t_hint`: the wall as read by the pair test."""
    from OCP.Approx import Approx_IsoParametric
    from OCP.BRep import BRep_Tool
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
    from OCP.gp import gp_Pnt, gp_Pnt2d
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    box = BRepTools.UVBounds_s(fa)
    u0, u1, v0, v1 = box
    if not all(math.isfinite(x) for x in box) or u1 <= u0 or v1 <= v0:
        return [], 0.0, 0.0, 0.0, "a face without bounds"
    sa = BRep_Tool.Surface_s(fa)
    pa_proj, pb_proj = sp._projector(fa), sp._projector(fb)
    n = FIT_SAMPLES
    grid = TColgp_Array2OfPnt(1, n, 1, n)
    walls = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            p = sa.Value(u0 + (u1 - u0) * i / (n - 1), v0 + (v1 - v0) * j / (n - 1))
            got = _midpoint(pa_proj, pb_proj, _np(p))
            if got is None:
                return [], 0.0, 0.0, 0.0, "a point of the wall has no midpoint"
            m, w = got
            grid.SetValue(i + 1, j + 1, gp_Pnt(*map(float, m)))
            walls[i, j] = w
    # a cubic interpolation through the midpoints (an approximation of higher degree waved between them: a twisted
    # strap's mid-surface came out 14 times its area)
    fit = GeomAPI_PointsToBSplineSurface()
    fit.Interpolate(grid, Approx_IsoParametric)
    if not fit.IsDone():
        return [], 0.0, 0.0, 0.0, "no surface could be fitted through the midpoints"
    surf = fit.Surface()
    wires_a = _curves_of_a(fa, box)
    if _covers(fb, fa):
        region = _region_on(surf, wires_a)
        if region is None:
            return [], 0.0, 0.0, 0.0, "the face's outline could not be laid on the fitted surface"
        faces = [region]
    else:
        wires_b = _curves_of_b(fb, fa, box)
        faces = _overlap_2d(wires_a, wires_b, surf, 1e-6) if wires_b is not None else []
        if not faces:
            return [], 0.0, 0.0, 0.0, "the two faces do not overlap"
    from OCP.BRepLib import BRepLib

    for f in faces:
        BRepLib.BuildCurves3d_s(f)
    if surf.IsUClosed() or surf.IsVClosed():
        # a wall closed round (a drafted tube): the fitted face's two sides along the seam are one edge
        faces = [_seamed(f, 1e-4 * t_hint) for f in faces]
    faces = [g for f in faces for g in _valid_faces(f)]
    if not faces:
        return [], 0.0, 0.0, 0.0, "the fitted mid-face is not a valid face"
    read = [walls[i, j] for i in range(n) for j in range(n)
            if any(BRepClass_FaceClassifier(f, gp_Pnt2d(i / (n - 1), j / (n - 1)), 1e-9).State()
                   in (TopAbs_IN, TopAbs_ON) for f in faces)]
    worst_rel, lo, hi = 0.0, math.inf, 0.0
    for f in faces:
        _w, rel, _n, flo, fhi = fitted_fidelity(f, fa, fb)
        worst_rel = max(worst_rel, rel)
        lo, hi = min(lo, flo), max(hi, fhi)
    if worst_rel > FIDELITY_SHARE:
        return [], 0.0, 0.0, 0.0, (f"the fitted mid-surface is off the middle by {100 * worst_rel:.1f}% of the wall")
    if not read and not math.isfinite(lo):
        return [], 0.0, 0.0, 0.0, "no point of the fitted mid-surface could be read"
    read = read or [lo, hi]
    lo, hi = min(lo, *read), max(hi, *read)
    # a fitted mid-face is itself a few walls wide (as a shell sheet is, SHELL-2): a narrower one is a local feature
    # of the wall beside it (a flat dimple 1.4 walls wide in a printed cylinder's side), which that wall's own
    # mid-face runs across when it is joined
    if width_of(_compound(faces)) < hi / sp.SHELL_WIDTH_RATIO:
        return [], 0.0, 0.0, 0.0, "it is narrower than four walls (a local feature of the wall beside it)"
    return faces, float(lo), float(hi), float(np.mean(read)), ""


# ------------------------------------------------------------------------------------ extension ----
class _Mid:
    """One mid-face while it is built."""

    def __init__(self, k: int, face, pair: int, body: int, t: float):
        self.k, self.face, self.pair, self.body, self.t = k, face, pair, body, t
        self.fitted = False              # fitted through the midpoints (SHELL-9)
        self.t_min = self.t_max = t      # the wall's thinnest and thickest over it (a tapered wall's differ)
        self.plane = _plane(face)
        self.extended: list[dict] = []
        self.original = face
        self.true_face = face            # where the mid-face really lies (the checks read this one)
        self.offset = 0.0                # how far it was moved onto a thicker neighbour's mid-plane (SHELL-7)
        self.remade = False              # an offset surface re-made as a B-spline before the join (remade_offset)
        self.box = _box(face)            # the original face's box, its own units
        self.face_box = self.box         # the (extended) face's


#: AN OFFSET MID-FACE IS RE-MADE AS A B-SPLINE before it is joined (OpenCASCADE's ShapeCustom, offset surfaces
#: only): the same face, every point read on it within OFFSET_REMAKE_SHARE of the wall of where it was, else it
#: stays an offset. The general fuse of the offset of an extruded B-spline with a plane ran for over 40 minutes (a
#: fan housing), the same B-spline in 0.01 s. The checks still read the offset itself (true_face).
OFFSET_REMAKE_SHARE = 1e-4
OFFSET_REMAKE_SAMPLES = 3


def remade_offset(face, tol: float):
    """The face, its offset surface re-made as a B-spline, when every point read on it lies within `tol` (its own
    units) of the face; None for any other surface, or when it cannot be re-made that close."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.Geom import Geom_BSplineSurface, Geom_OffsetSurface
    from OCP.gp import gp_Pnt2d
    from OCP.ShapeCustom import ShapeCustom
    from OCP.TopAbs import TopAbs_IN

    if not isinstance(BRep_Tool.Surface_s(face), Geom_OffsetSurface):
        return None
    try:
        out = _faces(ShapeCustom.ConvertToBSpline_s(face, False, False, True, False))
        if len(out) != 1 or not isinstance(BRep_Tool.Surface_s(out[0]), Geom_BSplineSurface):
            return None
        g = out[0]
        a = BRepAdaptor_Surface(g, True)
        u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
        k = OFFSET_REMAKE_SAMPLES
        read = 0
        for i in range(k):
            for j in range(k):
                u, v = u0 + (u1 - u0) * (i + 0.5) / k, v0 + (v1 - v0) * (j + 0.5) / k
                if BRepClass_FaceClassifier(g, gp_Pnt2d(u, v), 1e-9).State() != TopAbs_IN:
                    continue
                q = a.Value(u, v)
                if _dist(_vertex((q.X(), q.Y(), q.Z())), face) > tol:
                    return None
                read += 1
        if read == 0 or abs(_area(g) - _area(face)) > 1e-6 * max(_area(face), 1e-300):
            return None
        return g
    except Exception as exc:  # noqa: BLE001 - the offset stays
        logger.debug("shell: an offset mid-face could not be re-made as a B-spline (%s)", exc)
        return None


def _box(shape) -> tuple[np.ndarray, np.ndarray]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _near_box(p, box, reach: float) -> bool:
    """Is point p within `reach` of the box (a quick test before an exact distance)?"""
    lo, hi = box
    return bool(np.all(p >= lo - reach) and np.all(p <= hi + reach))


def _ordered_edges(wire, face) -> list:
    from OCP.BRepTools import BRepTools_WireExplorer

    out = []
    ex = BRepTools_WireExplorer(wire, face)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out


def _ends(edge):
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp

    a, b = TopExp.FirstVertex_s(edge, True), TopExp.LastVertex_s(edge, True)

    def xyz(v):
        p = BRep_Tool.Pnt_s(v)
        return np.array([p.X(), p.Y(), p.Z()])

    return xyz(a), xyz(b)


def _is_line(edge) -> bool:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line

    return BRepAdaptor_Curve(edge).GetType() == GeomAbs_Line


def _outward(face, p0, p1, n, step) -> np.ndarray | None:
    t = p1 - p0
    L = float(np.linalg.norm(t))
    if L <= 0:
        return None
    d = np.cross(t / L, n)
    d /= max(float(np.linalg.norm(d)), 1e-300)
    mid = 0.5 * (p0 + p1)
    tol = 1e-3 * step
    if _in_face(face, mid + d * step, tol):
        d = -d
    if _in_face(face, mid + d * step, tol) or not _in_face(face, mid - d * step, tol):
        return None
    return d


def _target(m: _Mid, p0, p1, d, mids: list[_Mid], solid: _Solid) -> tuple[_Mid, float] | None:
    """The planar mid-face whose plane every sample of the edge p0-p1 meets, moving along d, inside the
    material and within the reach; and the farthest such distance. None when the samples do not agree."""
    found = []
    for f in EXTEND_SAMPLES:
        p = p0 + (p1 - p0) * f
        best = None
        for o in mids:
            if o is m or o.plane is None or o.body != m.body:
                continue
            on, nn = o.plane[0], o.plane[1]
            cos = float(np.dot(d, nn))
            if abs(float(np.dot(m.plane[1], nn))) > math.cos(math.radians(NOT_PARALLEL_DEG)) or abs(cos) < 1e-6:
                continue
            s = float(np.dot(on - p, nn)) / cos
            reach = 0.5 * o.t + EXTEND_WALLS * max(m.t, o.t)
            if not (1e-9 * reach < s <= reach):
                continue
            if best is not None and s >= best[1] - 1e-9 * reach:
                continue
            # the cheap tests first: the solid classifier on a large body is the slowest call of the extension (an
            # offshore assembly spent over 30 minutes in it), and every test must pass anyway
            if not _near_box(p + d * s, o.box, reach) or _dist(_vertex(p + d * s), o.original) > reach:
                continue
            if not all(solid.inside(p + d * s * g) for g in (0.25, 0.5, 0.75)):
                continue
            best = (o, s)
        if best is None:
            return None
        found.append(best)
    planes = {(tuple(np.round(o.plane[1] * np.sign(o.plane[1][np.argmax(np.abs(o.plane[1]))]), 9)),
               round(float(np.dot(o.plane[0], o.plane[1] * np.sign(o.plane[1][np.argmax(np.abs(o.plane[1]))]))), 9))
              for o, _s in found}
    if len(planes) != 1:
        return None
    return min(found, key=lambda x: x[0].k)[0], max(s for _o, s in found)


def _meet_line(m: _Mid, o: _Mid):
    """The line where two planes meet, in m's plane frame: (point, unit direction), 2D."""
    om, nm, ex, ey = m.plane
    oo, no = o.plane[0], o.plane[1]
    u = np.cross(nm, no)
    u /= np.linalg.norm(u)
    # a point on both planes: the one nearest om
    A = np.array([nm, no, u])
    q = np.linalg.solve(A, np.array([np.dot(nm, om), np.dot(no, oo), np.dot(u, om)]))
    to2 = lambda v: np.array([np.dot(v - om, ex), np.dot(v - om, ey)])  # noqa: E731
    return to2(q), np.array([np.dot(u, ex), np.dot(u, ey)])


def _cross2(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _intersect(l1, l2):
    (p, u), (q, v) = l1, l2
    den = _cross2(u, v)
    if abs(den) < 1e-9:
        return None
    s = _cross2(q - p, v) / den
    return p + u * s


def _foot(x, line):
    p, u = line
    return p + u * float(np.dot(x - p, u))


def extend(m: _Mid, mids: list[_Mid], solid: _Solid, fuzzy: float) -> None:
    """SHELL-3: carry m's straight edges on to the mid-planes they run into (see EXTEND_*). m.face is
    replaced by the extended face; m.extended says which edges went where. An edge already meeting another
    mid-face (JOINED_SHARE) stays. A face that cannot be rebuilt keeps its own shape."""
    if m.plane is None:
        return
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    om, nm, ex, ey = m.plane
    to2 = lambda v: np.array([np.dot(v - om, ex), np.dot(v - om, ey)])  # noqa: E731
    to3 = lambda w: om + ex * w[0] + ey * w[1]  # noqa: E731
    face = m.face
    outer = BRepTools.OuterWire_s(face)
    wires = [outer] + [TopoDS.Wire_s(w) for w in (_map(face, TopAbs_WIRE).FindKey(i)
                                                    for i in range(1, _map(face, TopAbs_WIRE).Extent() + 1))
                       if not w.IsSame(outer)]
    others = [o for o in mids if o is not m and o.body == m.body]
    changed = False
    new_wires = []
    for wire in wires:
        edges = _ordered_edges(wire, face)
        items = []
        for e in edges:
            p0, p1 = _ends(e)
            item = {"edge": e, "line": _is_line(e), "p0": to2(p0), "p1": to2(p1), "new": None}
            if item["line"]:
                mid3 = 0.5 * (p0 + p1)
                joined = any(_near_box(mid3, o.face_box, JOINED_SHARE * max(m.t, o.t) + fuzzy)
                             and _dist(_vertex(mid3), o.face) <= JOINED_SHARE * max(m.t, o.t) + fuzzy for o in others)
                d = None if joined else _outward(face, p0, p1, nm, 0.01 * m.t)
                hit = _target(m, p0, p1, d, mids, solid) if d is not None else None
                if hit is not None:
                    o, s = hit
                    item["new"] = _meet_line(m, o)
                    item["to"] = o
                    item["by"] = s
                    changed = True
            items.append(item)
        new_wires.append(items)
    if not changed:
        return

    def line_of(it):
        if it["new"] is not None:
            return it["new"]
        u = it["p1"] - it["p0"]
        return it["p0"], u / max(float(np.linalg.norm(u)), 1e-300)

    built = []
    for items in new_wires:
        n = len(items)
        starts: list[np.ndarray] = [np.zeros(2)] * n
        ends: list[np.ndarray] = [np.zeros(2)] * n
        for i in range(n):
            a, b = items[i], items[(i + 1) % n]
            v = a["p1"]                       # the shared vertex (a's end, b's start)
            if a["new"] is None and b["new"] is None:
                ends[i], starts[(i + 1) % n] = v, v
                continue
            if a["line"] and b["line"]:
                x = _intersect(line_of(a), line_of(b))
                if x is not None:
                    ends[i], starts[(i + 1) % n] = x, x
                    continue
            ends[i] = _foot(v, line_of(a)) if a["line"] else v
            starts[(i + 1) % n] = _foot(v, line_of(b)) if b["line"] else v
        mk = BRepBuilderAPI_MakeWire()
        tiny = 1e-7 * max(m.t, 1e-9)
        for i, it in enumerate(items):
            s0, e0 = starts[i], ends[i]
            if it["line"]:
                if it["new"] is not None or np.linalg.norm(s0 - it["p0"]) > tiny or np.linalg.norm(e0 - it["p1"]) > tiny:
                    u0 = it["p1"] - it["p0"]
                    if float(np.dot(e0 - s0, u0)) <= 0:
                        return            # the edge turned round: an extension this simple rule cannot draw
                    mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*to3(s0)), gp_Pnt(*to3(e0))).Edge())
                else:
                    mk.Add(it["edge"])
            else:
                mk.Add(it["edge"])
            nxt = starts[(i + 1) % n]
            if np.linalg.norm(nxt - e0) > tiny:
                mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*to3(e0)), gp_Pnt(*to3(nxt))).Edge())
        if not mk.IsDone():
            return
        built.append(mk.Wire())
    pln = gp_Pln(gp_Ax3(gp_Pnt(*om), gp_Dir(*nm), gp_Dir(*ex)))
    mf = BRepBuilderAPI_MakeFace(pln, built[0], True)
    for w in built[1:]:
        mf.Add(w)
    if not mf.IsDone():
        return
    from OCP.ShapeFix import ShapeFix_Face

    fix = ShapeFix_Face(mf.Face())
    fix.Perform()
    new = fix.Face()
    if not BRepCheck_Analyzer(new).IsValid() or _area(new) < _area(face) * (1 - 1e-9):
        return
    m.face = new
    m.face_box = _box(new)
    for items in new_wires:
        for it in items:
            if it["new"] is not None:
                m.extended.append({"to": it["to"].k, "by": float(it["by"])})


# ------------------------------------------------------------------------------------------ sew ----
def touching_groups(boxes: list, reach: float) -> list[list[int]]:
    """The faces in groups that can touch: two faces whose boxes, each grown by `reach`, overlap are in one
    group (and so on, transitively). Faces in different groups are apart by more than `reach` - no fuse can
    join them. Each group in index order, the groups by their first index: deterministic."""
    n = len(boxes)
    if n == 0:
        return []
    lo = np.array([b[0] for b in boxes]) - reach
    hi = np.array([b[1] for b in boxes]) + reach
    parent = list(range(n))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        touch = np.nonzero(np.all(lo[i + 1:] <= hi[i], axis=1) & np.all(hi[i + 1:] >= lo[i], axis=1))[0] + i + 1
        for j in touch.tolist():
            a, b = root(i), root(j)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def _fuse(faces: list, fuzzy: float):
    from OCP.BOPAlgo import BOPAlgo_Builder

    b = BOPAlgo_Builder()
    for f in faces:
        b.AddArgument(f)
    b.SetFuzzyValue(fuzzy)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    b.Perform()
    if b.HasErrors():
        raise ValueError("the mid-faces could not be sewn")
    images = []
    for f in faces:
        mod = list(b.Modified(f))
        images.append(mod if mod else ([] if b.IsDeleted(f) else [f]))
    return b.Shape(), images


def sew(faces: list, fuzzy: float):
    """The general fuse of the faces (one thread): (the fused shape, for each input the faces it became). Faces
    that cannot touch (touching_groups) are fused apart, group by group, and the results put in one compound: the
    same surface, without one fuse over every face of a large part at once (a fan housing's 400 mid-faces stalled
    a single fuse for over 40 minutes)."""
    groups = touching_groups([_box(f) for f in faces], 2.0 * fuzzy)
    if len(groups) <= 1:
        return _fuse(faces, fuzzy)
    images: list = [None] * len(faces)
    parts = []
    for g in groups:
        if len(g) == 1:
            parts.append(faces[g[0]])
            images[g[0]] = [faces[g[0]]]
            continue
        out, imgs = _fuse([faces[i] for i in g], fuzzy)
        parts.append(out)
        for i, im in zip(g, imgs, strict=True):
            images[i] = im
    return _compound(parts), images


def _edge_mid(edge):
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    c = BRepAdaptor_Curve(edge)
    u = 0.5 * (c.FirstParameter() + c.LastParameter())
    p = c.Value(u)
    return np.array([p.X(), p.Y(), p.Z()])


def _face_normal_at(face, p):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt

    uv = _analyser(face)[1].ValueOfUV(gp_Pnt(*map(float, p)), 1e-9)
    pr = BRepLProp_SLProps(BRepAdaptor_Surface(face), uv.X(), uv.Y(), 1, 1e-9)
    if not pr.IsNormalDefined():
        return None
    n = pr.Normal()
    return np.array([n.X(), n.Y(), n.Z()])


#: an edge ENDS IN ANOTHER WALL when both it and the point past it lie within this share over half of that
#: wall's thickness from that wall's mid-face
IN_WALL_SLACK = 1.05
#: ... and when the step past it runs along that wall: within 60 degrees of parallel to it
IN_WALL_COS = 0.5


def _sewn_reach(start: int, adjacency: dict[int, set[int]]) -> set[int]:
    """Every sewn face reachable from `start` through shared edges: the connected surface it belongs to."""
    seen, todo = {start}, [start]
    while todo:
        k = todo.pop()
        for j in adjacency.get(k, ()):
            if j not in seen:
                seen.add(j)
                todo.append(j)
    return seen


def _ends_in_wall(p, q, own: int, walls: list, tol: float, sewn_to: set | None = None, ends: tuple | None = None) -> bool:
    """Do the edge point p and the point q just past it both lie inside the wall of a sewn face that the edge's own
    sheet is sewn to (`sewn_to`: the face indices joined to it through the surface)? Two sheets that never meet -
    a butt step left unsnapped - are a gap, not one wall holding the other. The step past the edge must run along
    that wall - or the edge must END IN THE JUNCTION: both its `ends` inside sewn walls and at least one of them on
    a sewn mid-face. Such an edge is the side of a band that runs into a wall and stops on its mid-face (a
    gusset's slanted edge carried into the plate it stands on, or across the corner of the two plates it joins),
    wherever its step points; a junction stopped short runs alongside the wall, its ends on no mid-face."""
    live = [(k, f, w, box) for k, f, w, box in walls if k != own and (sewn_to is None or k in sewn_to)]
    vp = vq = None
    held = False
    for _k, f, w, box in live:
        reach = 0.5 * w * IN_WALL_SLACK + tol
        if not (_near_box(p, box, reach) and _near_box(q, box, reach)):
            continue
        vp = vp if vp is not None else _vertex(p)
        vq = vq if vq is not None else _vertex(q)
        if _dist(vp, f) <= reach and _dist(vq, f) <= reach:
            n = _face_normal_at(f, p)
            step = q - p
            # it runs ALONG that wall (a plate side inside a thicker plate), not INTO it (a junction stopped short)
            if n is not None and abs(float(np.dot(step / max(float(np.linalg.norm(step)), 1e-300), n))) <= IN_WALL_COS:
                return True
            held = True
    if not held or ends is None:
        return False
    on_mid = False
    for e in ends:
        inside = False
        ve = _vertex(e)
        for _k, f, w, box in live:
            reach = 0.5 * w * IN_WALL_SLACK + tol
            if not _near_box(e, box, reach):
                continue
            d = _dist(ve, f)
            inside = inside or d <= reach
            on_mid = on_mid or d <= tol
            if inside and on_mid:
                break
        if not inside:
            return False
    return on_mid


def _near_any(p, faces_boxes, reach: float) -> bool:
    """Is point p within `reach` of any of the faces (each with its box: boxes first, exact distance after)?"""
    v = None
    for f, box in faces_boxes:
        if _near_box(p, box, reach):
            v = v if v is not None else _vertex(p)
            if _dist(v, f) <= reach:
                return True
    return False


def _inside_edge(edge, face) -> bool:
    """Is the edge inside its one face rather than on its border: a closed surface's seam, or a SLIT (an edge the
    face's boundary runs along on both sides - where the general fuse cut a face along a line that ends inside
    it)? Either way the face is on both sides of it: no free edge."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    if BRep_Tool.IsClosed_s(edge, face):
        return True
    from OCP.TopAbs import TopAbs_EXTERNAL, TopAbs_INTERNAL

    n = 0
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        if ex.Current().IsSame(edge):
            if ex.Current().Orientation() in (TopAbs_INTERNAL, TopAbs_EXTERNAL):
                return True             # the fuse writes a slit once, as an internal edge
            n += 1
        ex.Next()
    return n >= 2


#: where along a free edge its outward side is read: the middle first, then off it (a point on a closed
#: surface's seam, or at a vertex, cannot tell the face's side from the other)
SIDE_FRACTIONS = (0.5, 0.37, 0.63, 0.21, 0.79)


def _outward_at(edge, face, step: float):
    """(a point on the edge, the unit direction in the face's surface away from the face there) at the first
    place along the edge where exactly one side lies in the face; None when no place tells."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.gp import gp_Pnt, gp_Vec

    c = BRepAdaptor_Curve(edge)
    a, b = c.FirstParameter(), c.LastParameter()
    for frac in SIDE_FRACTIONS:
        q, v = gp_Pnt(), gp_Vec()
        c.D1(a + (b - a) * frac, q, v)
        p = np.array([q.X(), q.Y(), q.Z()])
        tan = np.array([v.X(), v.Y(), v.Z()])
        n = _face_normal_at(face, p)
        if n is None or np.linalg.norm(tan) <= 0:
            continue
        d = np.cross(tan / np.linalg.norm(tan), n)
        if np.linalg.norm(d) <= 0:
            continue
        d /= float(np.linalg.norm(d))
        plus, minus = _in_face(face, p + d * step, 1e-3 * step), _in_face(face, p - d * step, 1e-3 * step)
        if plus != minus:
            return p, (-d if plus else d)
    return None


def edge_census(fused, owner_t: dict, solid: _Solid | None, *, scale: float, thick_faces: list | None = None) -> dict:
    """The sewn surface's edges: shared by two faces (a closed face's seam counts so), a junction (three or
    more), a real boundary (one face, the outside just past it), a SOLID LINK (one face, the material past it
    belonging to a thick region of the same body - where a mixed part's shell meets its solid) or OPEN (one
    face, still material past it, no thick region there: a junction the mid-faces did not reach). `owner_t`:
    each fused face's wall (its own units) by its index in the fused shape's face map; `scale`: mm per unit;
    `thick_faces`: the body's faces of thick regions (cad/shell_pairs SOLID and THICK roles)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(fused, TopAbs_EDGE, TopAbs_FACE, e2f)
    fmap = _map(fused, TopAbs_FACE)
    shared = junction = boundary = 0
    open_at: list[list[float]] = []
    links: list[list[float]] = []
    thick = [(f, _box(f)) for f in thick_faces] if thick_faces else []
    # every sewn face with its wall: an edge that ends INSIDE ANOTHER SHELL WALL is no open edge
    sewn = [(k, TopoDS.Face_s(fmap.FindKey(k)), owner_t.get(k, 0.0)) for k in range(1, fmap.Extent() + 1)]
    walls = [(k, f, w, _box(f)) for k, f, w in sewn if w > 0]
    in_wall = below_floor = 0
    adjacency: dict[int, set[int]] = {}
    for i in range(1, e2f.Extent() + 1):
        ks = {fmap.FindIndex(f) for f in e2f.FindFromIndex(i)} - {0}
        for k in ks:
            adjacency.setdefault(k, set()).update(ks - {k})
    # each face's connected surface, read once (one set shared by every face in it)
    component: dict[int, set[int]] = {}

    def reach_of(k: int) -> set[int]:
        if k not in component:
            comp = _sewn_reach(k, adjacency)
            for j in comp:
                component[j] = comp
        return component[k]

    for i in range(1, e2f.Extent() + 1):
        e = TopoDS.Edge_s(e2f.FindKey(i))
        if BRep_Tool.Degenerated_s(e):
            continue
        faces = []
        for f in e2f.FindFromIndex(i):
            k = fmap.FindIndex(f)
            if k not in faces:
                faces.append(k)
        if len(faces) == 2 or (len(faces) == 1 and _inside_edge(e, TopoDS.Face_s(fmap.FindKey(faces[0])))):
            shared += 1
            continue
        if len(faces) > 2:
            junction += 1
            continue
        face = TopoDS.Face_s(fmap.FindKey(faces[0]))
        t = owner_t.get(faces[0], 0.0)
        if solid is None or t <= 0:
            boundary += 1
            continue
        found = _outward_at(e, face, 0.01 * t)
        if found is None:
            boundary += 1
            continue
        p, d = found
        q = p + d * OPEN_STEP * t
        if not solid.strictly_inside(q) or not all(solid.strictly_inside(p + d * f * t) for f in SLIT_STEPS):
            boundary += 1
        elif _ends_in_wall(p, q, faces[0], walls, solid.tol, sewn_to=reach_of(faces[0]), ends=_ends(e)):
            # SHELL-3: the edge of a thinner plate's band that runs into a thicker plate's wall and ends there (the
            # thinner plate's own side, inside the junction): the material past it is the other wall, meshed
            in_wall += 1
        elif _near_any(p, thick, (0.5 + OPEN_STEP) * t + solid.tol):
            links.append(_pt(p * scale))
        elif _length(e) < FLOOR_SHARE * t:
            below_floor += 1
        else:
            open_at.append(_pt(p * scale))
    return {"faces": int(fmap.Extent()), "edges": int(e2f.Extent()), "shared_edges": shared,
            "junction_edges": junction, "boundary_edges": boundary, "ends_in_wall_edges": in_wall, "below_floor_edges": below_floor,
            "solid_link_edges": len(links),
            "open_edges": len(open_at), "open_at_mm": open_at[:50], "solid_link_at_mm": links[:50]}


# ------------------------------------------------------------------------------------- fidelity ----
def fidelity(mid, fa, fb, t: float) -> tuple[float, int]:
    """The largest distance error of the mid-face from half the wall to each of its faces (its own units),
    over the points read, and how many points."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    a = BRepAdaptor_Surface(mid, True)
    u0, u1, v0, v1 = a.FirstUParameter(), a.LastUParameter(), a.FirstVParameter(), a.LastVParameter()
    worst, n = 0.0, 0
    k = FIDELITY_SAMPLES
    for i in range(k):
        for j in range(k):
            u = u0 + (u1 - u0) * (i + 0.5) / k
            v = v0 + (v1 - v0) * (j + 0.5) / k
            if BRepClass_FaceClassifier(mid, gp_Pnt2d(u, v), 1e-9).State() != TopAbs_IN:
                continue
            p = a.Value(u, v)
            vx = _vertex((p.X(), p.Y(), p.Z()))
            worst = max(worst, abs(_dist(vx, fa) - t / 2), abs(_dist(vx, fb) - t / 2))
            n += 1
    return worst, n


# -------------------------------------------------------------------------------------- extract ----
# ------------------------------------------------------------------------- join: grow, cut, keep ----
#: SHELL-3 (v2, 29 Oct): every shell mid-face of a body is GROWN on its own surface by GROW_REACH (half the body's
#: thickest wall plus GROW_WALLS times its own wall) past every edge; the grown faces, the mid-faces themselves and
#: the body's own CAD faces are fused (general fuse, one thread); and each mid-face keeps, besides itself, the
#: grown pieces that (a) lie inside the part's material, (b) are reached from it without crossing another
#: mid-face, and (c) reach another mid-face. So a mid-face runs on until it meets the mid-face it runs into -
#: whatever the two surfaces are: plane to plane (T, L, cross, box), cylinder to plane (a tube on a plate),
#: cylinder to cone, a flange that faces only part of an edge (a relief cut) - and stops there; past a plate's
#: rim or into a hole it is outside the material and is dropped; into a thick region with no mid-face to meet
#: (a mixed part's solid) it is dropped too, and the edge there is a solid link.
GROW_WALLS = 2.0


def joined_edges(m: _Mid, others: list[_Mid], fuzzy: float, *, curved_only: bool = False) -> list:
    """The edges of mid-face m that already meet another mid-face of its body (a flange running into its bend,
    two pieces of one plate side by side): within JOINED_SHARE of a wall of it."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    out = []
    em = _map(m.original, TopAbs_EDGE)
    for i in range(1, em.Extent() + 1):
        e = TopoDS.Edge_s(em.FindKey(i))
        p = _edge_mid(e)
        for o in others:
            if o is m or (curved_only and o.plane is not None):
                continue
            reach = JOINED_SHARE * max(m.t, o.t) + fuzzy
            if _near_box(p, o.box, reach) and _dist(_vertex(p), o.original) <= reach:
                out.append(e)
                break
    return out


def grow(face, reach: float, joined: list | None = None, *, remake: bool = True, extend: bool = True):
    """The face grown `reach` (its own units) past its edges on its own surface; None when it cannot be grown.
    A plane grows by offsetting its outline outward and its holes inward (sharp corners kept); any other
    surface by widening its parameter range by `reach` measured at the face's middle (a periodic direction that
    already closes stays closed). It never grows past a JOINED edge (one that already meets another mid-face):
    a band laid tangent along a bend it already meets would only spoil that shared edge. `extend`: False keeps the
    surface within its own ends (a re-made offset in the join that does not re-make offsets, as the offset was)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt, gp_Vec

    surf = BRep_Tool.Surface_s(face)
    own_surf = surf
    joined = joined or []
    if _plane(face) is not None:
        grown = _grow_plane(face, reach, joined)
        if grown is not None:
            return grown
        # the outline offset failed (a disc bounded by one circle did): grow its parameter range as tiles instead
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    hold = _held_sides(face, joined, (u0, u1, v0, v1))
    a = BRepAdaptor_Surface(face, True)
    p, du_vec, dv_vec = gp_Pnt(), gp_Vec(), gp_Vec()
    a.D1(0.5 * (u0 + u1), 0.5 * (v0 + v1), p, du_vec, dv_vec)
    su, sv = du_vec.Magnitude(), dv_vec.Magnitude()
    if su <= 0 or sv <= 0:
        return None
    du, dv = reach / su, reach / sv
    for name, per in (("u", surf.UPeriod() if surf.IsUPeriodic() else None),
                      ("v", surf.VPeriod() if surf.IsVPeriodic() else None)):
        if per:
            span = (u1 - u0) if name == "u" else (v1 - v0)
            cap = max(0.0, min(0.25 * per, 0.45 * (per - span)))
            if name == "u":
                du = min(du, cap)
            else:
                dv = min(dv, cap)
    lo_u = u0 if "u0" in hold else u0 - du
    hi_u = u1 if "u1" in hold else u1 + du
    lo_v = v0 if "v0" in hold else v0 - dv
    hi_v = v1 if "v1" in hold else v1 + dv
    if surf.IsUPeriodic() and hi_u - lo_u >= surf.UPeriod():
        lo_u, hi_u = u0, u0 + surf.UPeriod()
    if surf.IsVPeriodic() and hi_v - lo_v >= surf.VPeriod():
        lo_v, hi_v = v0, v0 + surf.VPeriod()
    b = surf.Bounds()
    short = {name for name, want, have, up in (("u0", lo_u, b[0], False), ("u1", hi_u, b[1], True),
                                               ("v0", lo_v, b[2], False), ("v1", hi_v, b[3], True))
             if not (surf.IsUPeriodic() if name[0] == "u" else surf.IsVPeriodic())
             and (want > have if up else want < have)}
    if short and extend:
        surf = _extended_surface(face, surf, GROW_EXTEND * reach, short, (u0, u1, v0, v1), remake=remake)
        b = surf.Bounds()
    if not surf.IsUPeriodic():
        lo_u, hi_u = max(lo_u, b[0]), min(hi_u, b[1])
    if not surf.IsVPeriodic():
        lo_v, hi_v = max(lo_v, b[2]), min(hi_v, b[3])
    # as TILES - the face's own range and a band on each grown side - so no one face runs across a closed
    # surface's seam (the general fuse could not split a grown torus that did, and lost the whole mid-face)
    us = [lo_u, u0, u1, hi_u]
    vs = [lo_v, v0, v1, hi_v]
    tiles = []
    for a0, a1 in zip(us[:-1], us[1:], strict=True):
        for b0, b1 in zip(vs[:-1], vs[1:], strict=True):
            if a1 - a0 <= 1e-12 or b1 - b0 <= 1e-12:
                continue
            # the face's own range on its own surface (an extended copy of it, re-made as a B-spline, would lie
            # over the mid-face itself a hair off and spoil the fuse); the grown bands on the extended surface
            own = (a0, a1) == (u0, u1) and (b0, b1) == (v0, v1)
            mk = BRepBuilderAPI_MakeFace(own_surf if own else surf, a0, a1, b0, b1, 1e-7)
            if mk.IsDone():
                tiles.append(mk.Face())
    return _compound(tiles) if tiles else None


#: a surface that ends where its face does (a lofted B-spline wall, its offset) is carried on past its own ends by
#: this share of the reach before the face is grown on it, so it can reach the wall it runs into
GROW_EXTEND = 1.5


def _extended_surface(face, surf, length: float, sides: set[str], bounds, *, remake: bool = True):
    """The face's surface carried on `length` (its own units) past each of its own parameter ends in `sides`
    (OCC's BRepLib::ExtendFace: a B-spline or Bezier continued smoothly), or the surface itself when it cannot
    be: the extension must keep the face's own points where they are. An offset surface (the mid-face of a
    drafted wall whose one side is a B-spline) is not carried on by ExtendFace, so it is first re-made as a
    B-spline within EXTEND_APPROX of the length, on the same parameters, and that is carried on. (Its basis
    carried on and offset again is exact, but the general fuse took over 10 minutes on such offsets.)"""
    ext = _extend_face_surface(face, surf, length, sides, bounds)
    if ext is None and remake:
        bs = _as_bspline_face(surf, EXTEND_APPROX * length)
        if bs is not None:
            ext = _extend_face_surface(bs, surf, length, sides, bounds, slack=EXTEND_APPROX * length)
    return surf if ext is None else ext


#: an offset surface is re-made as a B-spline within this share of the extension length (far under the sew's
#: fuzzy value) before it is extended, of at most this degree and this many spans each way (a heavier B-spline
#: only slows the general fuse: degree 14 and 50 spans made one offshore part's fuse 70 s)
EXTEND_APPROX = 1e-5
EXTEND_APPROX_DEGREE = 6
EXTEND_APPROX_SPANS = 16


def _as_bspline_face(surf, tol: float):
    """A face over the whole of an offset surface re-made as a B-spline (the same parameters), or None."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.Geom import Geom_OffsetSurface
    from OCP.GeomAbs import GeomAbs_C2
    from OCP.GeomConvert import GeomConvert_ApproxSurface

    if not isinstance(surf, Geom_OffsetSurface):
        return None
    try:
        ap = GeomConvert_ApproxSurface(surf, float(tol), GeomAbs_C2, GeomAbs_C2, EXTEND_APPROX_DEGREE,
                                       EXTEND_APPROX_DEGREE, EXTEND_APPROX_SPANS, 0)
        if not ap.HasResult() or ap.MaxError() > tol:
            return None
        u0, u1, v0, v1 = surf.Bounds()
        mk = BRepBuilderAPI_MakeFace(ap.Surface(), u0, u1, v0, v1, 1e-7)
        return mk.Face() if mk.IsDone() else None
    except Exception as exc:  # noqa: BLE001 - the offset surface is grown as far as it goes
        logger.debug("shell: an offset surface could not be re-made as a B-spline (%s)", exc)
        return None


def _extend_face_surface(face, surf, length: float, sides: set[str], bounds, slack: float = 0.0):
    """ExtendFace on `face`; its surface when it reaches past `surf`'s own ends on every side asked and keeps the
    original face's points (those of `surf` over `bounds`) where they are, else None."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepLib import BRepLib
    from OCP.TopoDS import TopoDS_Face

    try:
        out = TopoDS_Face()
        BRepLib.ExtendFace_s(face, float(length), "u0" in sides, "u1" in sides, "v0" in sides, "v1" in sides, out)
        if out.IsNull():
            return None
        ext = BRep_Tool.Surface_s(out)
        have, got = surf.Bounds(), ext.Bounds()
        if not any((got[0] < have[0]) if n == "u0" else (got[1] > have[1]) if n == "u1" else
                   (got[2] < have[2]) if n == "v0" else (got[3] > have[3]) for n in sides):
            return None
        u0, u1, v0, v1 = bounds
        scale = max(length, 1e-12)
        for fu, fv in ((0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)):
            u, v = u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)
            if surf.Value(u, v).Distance(ext.Value(u, v)) > 1e-6 * scale + 2.0 * slack:
                return None
        return ext
    except Exception as exc:  # noqa: BLE001 - the surface is grown as far as it goes
        logger.debug("shell: a surface could not be extended (%s)", exc)
        return None


def _held_sides(face, joined: list, bounds) -> set[str]:
    """Which sides of a face's parameter rectangle (u0, u1, v0, v1) a joined edge lies on: those are not grown."""
    from OCP.BRep import BRep_Tool
    from OCP.gp import gp_Pnt
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    u0, u1, v0, v1 = bounds
    surf = BRep_Tool.Surface_s(face)
    sa = ShapeAnalysis_Surface(surf)
    eu, ev = 1e-6 * max(abs(u1 - u0), 1e-12), 1e-6 * max(abs(v1 - v0), 1e-12)
    pu = surf.UPeriod() if surf.IsUPeriodic() else None
    pv = surf.VPeriod() if surf.IsVPeriodic() else None

    def near(val: float, ref: float, eps: float, period: float | None) -> bool:
        d = val - ref
        if period:
            d = (d + 0.5 * period) % period - 0.5 * period
        return abs(d) <= max(eps, 1e-9)

    out: set[str] = set()
    for e in joined:
        uv = sa.ValueOfUV(gp_Pnt(*map(float, _edge_mid(e))), 1e-7)
        for name, val, ref, eps, per in (("u0", uv.X(), u0, eu, pu), ("u1", uv.X(), u1, eu, pu),
                                         ("v0", uv.Y(), v0, ev, pv), ("v1", uv.Y(), v1, ev, pv)):
            if near(val, ref, eps, per):
                out.add(name)
    return out


def _plane_union(a, b):
    """The union of two faces on one plane, as one face (the larger piece when it comes apart)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    f = BRepAlgoAPI_Fuse(a, b)
    if not f.IsDone():
        return a
    u = ShapeUpgrade_UnifySameDomain(f.Shape(), True, True, True)
    u.Build()
    fs = _faces(u.Shape())
    return _compound(fs) if fs else a


def _cut_beyond(grown, face, joined: list, reach: float):
    """The grown plane with the strip beyond each joined straight edge taken off (a 2D cut in the plane)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    pl = _plane(face)
    if pl is None:
        return grown
    out = grown
    for e in joined:
        if not _is_line(e):
            continue
        p0, p1 = _ends(e)
        d = _outward(face, p0, p1, pl[1], 1e-3 * reach)
        if d is None:
            continue
        poly = BRepBuilderAPI_MakePolygon()
        for q in (p0, p1, p1 + d * 1.01 * reach, p0 + d * 1.01 * reach):
            poly.Add(gp_Pnt(*map(float, q)))
        poly.Close()
        strip = BRepBuilderAPI_MakeFace(BRep_Tool.Surface_s(face), poly.Wire(), True)
        if not strip.IsDone():
            continue
        cut = BRepAlgoAPI_Cut(out, strip.Face())
        if cut.IsDone():
            fs = _faces(cut.Shape())
            if fs:
                out = _compound(fs)          # every piece: a strip can part the band in two
    return out


#: an offset outline whose tolerance is above this share of the reach is rebuilt from its edges' 3D curves
GROW_TOL_SHARE = 1e-4


def _max_tol(shape) -> float:
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
    from OCP.TopAbs import TopAbs_SHAPE

    return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1, TopAbs_SHAPE))


def _clean_wire(wire, reach: float):
    """The offset outline, or the same outline rebuilt edge by edge from its 3D curves when OCC's 2D offset left
    it a wild tolerance (a hole's circle came back with a tolerance of 138 mm on a plate whose plane is placed at
    its corner, and that one wire broke the general fuse of the whole body); None when it cannot be mended."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopoDS import TopoDS

    limit = GROW_TOL_SHARE * reach
    if _max_tol(wire) <= limit:
        return wire
    mw = BRepBuilderAPI_MakeWire()
    ex = BRepTools_WireExplorer(wire)
    while ex.More():
        e = ex.Current()
        if not BRep_Tool.Degenerated_s(e):
            first, last = BRep_Tool.Range_s(e)
            curve = BRep_Tool.Curve_s(e, 0.0, 1.0)
            if curve is None:
                return None
            mk = BRepBuilderAPI_MakeEdge(curve, first, last)
            if not mk.IsDone():
                return None
            # the wire runs the same way round (an outline one way, a hole the other)
            mw.Add(TopoDS.Edge_s(mk.Edge().Oriented(e.Orientation())))
            if not mw.IsDone():
                return None
        ex.Next()
    if not mw.IsDone():
        return None
    out = TopoDS.Wire_s(mw.Wire().Oriented(wire.Orientation()))
    return out if _max_tol(out) <= limit else None


def _outer_wire(face):
    from OCP.BRepTools import BRepTools

    return BRepTools.OuterWire_s(face)


def _inner_wires(face) -> list:
    """The face's wires other than its outline (its holes), in the face's own order."""
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    outer = _outer_wire(face)
    wm = _map(face, TopAbs_WIRE)
    return [TopoDS.Wire_s(wm.FindKey(i)) for i in range(1, wm.Extent() + 1) if not wm.FindKey(i).IsSame(outer)]


def _face_with_holes(surf, outline, holes: list):
    """A face on the plane `surf` bounded by `outline` with `holes` cut out, each hole run the other way round
    from the outline (one that would ADD area is turned round), or None."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.TopoDS import TopoDS

    if outline is None or outline.IsNull():
        return None
    mf = BRepBuilderAPI_MakeFace(surf, outline, True)
    if not mf.IsDone():
        return None
    face = mf.Face()
    if _signed_area(face) < 0:
        face = BRepBuilderAPI_MakeFace(surf, TopoDS.Wire_s(outline.Reversed()), True).Face()
    whole = _signed_area(face)
    for w in holes:
        for cand in (w, TopoDS.Wire_s(w.Reversed())):
            mk = BRepBuilderAPI_MakeFace(face)
            mk.Add(cand)
            if mk.IsDone() and 0 <= _signed_area(mk.Face()) < whole:
                face = mk.Face()
                whole = _signed_area(face)
                break
        else:
            return None
    return face


def _signed_area(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    return float(g.Mass())


def _left_over(face, grown) -> float:
    """The area of `face` not covered by `grown` (both on one plane)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    cut = BRepAlgoAPI_Cut(face, grown)
    if not cut.IsDone():
        return float("inf")
    return sum(_area(f) for f in _faces(cut.Shape()))


def _offset_region(surf, wire, dist: float, join) -> list:
    """The region inside `wire` (on the plane `surf`) offset by `dist` (outward when positive, inward when
    negative), as faces - the largest first; [] when nothing is left (a hole shrunk away) or it cannot be done.
    OCC's 2D offset takes its sign from how the wire runs against the plane's normal, so the sign is checked
    on the result: an offset that went the wrong way (a 20 mm hole of a base plate grew to 30 mm when asked
    to shrink to 10) is done again the other way."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    region = _face_with_holes(surf, wire, [])
    if region is None:
        return []
    whole = _area(region)
    for signed in (dist, -dist):
        mk = BRepOffsetAPI_MakeOffset(region, join)
        mk.Perform(signed)
        if not mk.IsDone():
            if signed == dist:
                continue
            return []
        wm = _map(mk.Shape(), TopAbs_WIRE)
        out = []
        for i in range(1, wm.Extent() + 1):
            w = _clean_wire(TopoDS.Wire_s(wm.FindKey(i)), abs(dist))
            f = _face_with_holes(surf, w, []) if w is not None else None
            if f is not None and _area(f) > 0:
                out.append(f)
        total = sum(_area(f) for f in out)
        if (total > whole) if dist > 0 else (total < whole):
            out.sort(key=lambda f: -_area(f))
            return out
    return []


def _grow_plane(face, reach: float, joined: list | None = None):
    """A plane face grown `reach` past its edges in its own plane: its outline offset outward and each hole
    offset inward on its own (a hole that shrinks away is gone), the shrunk holes cut from the grown outline.
    Each wire is offset alone: OCC's 2D offset of a whole face with holes grew a 20 mm hole of a bolted base
    plate to 30 mm instead of 10 mm, and left another hole's circle a 138 mm tolerance."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection
    from OCP.ShapeFix import ShapeFix_Face

    surf = BRep_Tool.Surface_s(face)
    holes = _inner_wires(face)
    for join in (GeomAbs_Intersection, GeomAbs_Arc):
        try:
            grown = _offset_region(surf, _outer_wire(face), reach, join)
            if not grown:
                continue
            out = grown[0]
            for w in holes:
                for left in _offset_region(surf, w, -reach, join):
                    cut = BRepAlgoAPI_Cut(out, left)
                    if not cut.IsDone():
                        raise ValueError("a shrunk hole could not be cut out")
                    fs = _faces(cut.Shape())
                    if not fs:
                        raise ValueError("a shrunk hole took the whole face")
                    out = _compound(fs) if len(fs) > 1 else fs[0]
            if len(_faces(out)) == 1:
                fix = ShapeFix_Face(_faces(out)[0])
                fix.Perform()
                out = fix.Face()
            if joined:
                out = _cut_beyond(out, face, joined, reach)
                out = _plane_union(out, face)
            # the grown face must hold the whole face it was grown from
            if _area(out) >= _area(face) * (1 - 1e-9) and _left_over(face, out) <= 1e-6 * _area(face):
                return out
        except Exception as exc:  # noqa: BLE001 - try the other join, then give up growing this face
            logger.debug("shell: a plane could not be grown (%s)", exc)
    return None


#: the deflections (shares of the face's box diagonal) _inner_point meshes a face at, coarse first
INNER_MESH_SHARES = (0.05, 0.005, 0.0005)
#: ... and how many of each mesh's largest triangles it tries for a middle inside the face
INNER_TRIES = 64


def _inner_point(face):
    """A point inside the face (its own units), or None: the middle of the largest triangle of the face's own
    triangulation (UV coordinates, so on the surface exactly) - inside any face, however thin (a 1.5 mm band
    along a 200 mm edge has no point of a coarse parameter grid in it). A curved sliver the coarse mesher
    cannot triangulate (a 1 mm band along a 550 mm curved edge) is meshed again finer. The point must lie IN the
    face: a coarse mesh cuts across a curved border (a 2 mm ring round a 40 mm hole, meshed at a 9 mm sag), so a
    triangle's middle can fall outside it; the largest triangle whose middle is in the face is taken, finer
    meshes after."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopLoc import TopLoc_Location

    lo, hi = _box(face)
    size = float(np.linalg.norm(hi - lo)) or 1.0
    fallback = None
    for share in INNER_MESH_SHARES:
        BRepMesh_IncrementalMesh(face, share * size, False, 0.5, False)
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        if tri is None or tri.NbTriangles() == 0 or not tri.HasUVNodes():
            continue
        sized = []
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(k).Get()
            pa, pb, pc = tri.UVNode(a), tri.UVNode(b), tri.UVNode(c)
            A = abs((pb.X() - pa.X()) * (pc.Y() - pa.Y()) - (pc.X() - pa.X()) * (pb.Y() - pa.Y()))
            sized.append((-A, k, ((pa.X() + pb.X() + pc.X()) / 3.0, (pa.Y() + pb.Y() + pc.Y()) / 3.0)))
        sized.sort()
        if fallback is None:
            fallback = sized[0][2]
        for _a, _k, uv in sized[:INNER_TRIES]:
            if BRepClass_FaceClassifier(face, gp_Pnt2d(*uv), 1e-9).State() == TopAbs_IN:
                q = BRepAdaptor_Surface(face, False).Value(*uv)
                return np.array([q.X(), q.Y(), q.Z()])
    if fallback is None:
        return None
    q = BRepAdaptor_Surface(face, False).Value(*fallback)
    return np.array([q.X(), q.Y(), q.Z()])


def join_body(mine: list[_Mid], body_faces: list, solid: _Solid, *, t_max: float, fuzzy: float,
              remake: bool = True, patch_alone: bool = True):
    """SHELL-3 v2 for one body: (the fused shape, the kept faces, each kept face's mid-face index into `mine`,
    for each mid-face the extensions it kept as [(other mid index or None, area)])."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    grown = []
    for m in mine:
        g = grow(m.original, 0.5 * t_max + GROW_WALLS * m.t,
                 joined_edges(m, mine, fuzzy, curved_only=m.plane is not None), remake=remake,
                 extend=remake or not m.remade)
        grown.append(g)
    b = BOPAlgo_Builder()
    from OCP.BRep import BRep_Tool as _BT

    for m, g in zip(mine, grown, strict=True):
        # a free-form mid-face's grown patch lies over the mid-face itself (its parameter box widened), on a surface
        # the fuse does not merge with the mid-face's own: both in the fuse were the same wall twice (a twisted
        # strap held 29% more metal than it has). The patch alone goes in; its pieces inside the mid-face are the
        # mid-face's own (read by geometry below).
        if patch_alone and g is not None and isinstance(_BT.Surface_s(m.original), FREE_FORM):
            continue
        b.AddArgument(m.original)
    for g in grown:
        for tile in (_faces(g) if g is not None else []):
            b.AddArgument(tile)
    # the faces of the part itself as ONE argument: they already meet edge to edge, and the general fuse does not
    # intersect the faces of one argument with each other (a fan housing's skin against itself was most of its join)
    if body_faces:
        b.AddArgument(_compound(body_faces))
    b.SetFuzzyValue(fuzzy)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    b.Perform()
    if b.HasErrors():
        raise ValueError("the grown mid-faces could not be fused")
    fused = b.Shape()
    fmap = _map(fused, TopAbs_FACE)
    n = fmap.Extent()
    owners: list[set[int]] = [set() for _ in range(n + 1)]
    core: list[set[int]] = [set() for _ in range(n + 1)]
    skin = [False] * (n + 1)          # a piece of the part's own CAD faces

    # WHICH FACE EACH PIECE IS A PIECE OF, by geometry (a point inside the piece, lying in that face): the fuse's
    # own history loses a face it merges with a coincident one (a torus mid-face and its grown copy that wraps
    # round the seam came back "deleted")
    tol = 10.0 * fuzzy + 1e-9
    sources = [(i, "core", m.original, m.box) for i, m in enumerate(mine)]
    sources += [(i, "grown", tile, _box(tile)) for i, g in enumerate(grown) if g is not None for tile in _faces(g)]
    sources += [(-1, "skin", f, _box(f)) for f in body_faces]
    from OCP.TopoDS import TopoDS as _T

    # a piece the fuse's history gives only to the part's own faces is skin, read without a point (meshing every
    # skin piece of a fan housing's extruded B-spline walls for a point took minutes); a piece any mid-face or grown
    # tile may share is read by geometry, as below
    def pieces_of(f) -> list[int]:
        mod = list(b.Modified(f))
        got = mod if mod else ([] if b.IsDeleted(f) else [f])
        return [k for k in (fmap.FindIndex(x) for x in got) if k > 0]

    of_skin = {k for f in body_faces for k in pieces_of(f)}
    of_mids = {k for m in mine for k in pieces_of(m.original)}
    of_mids |= {k for g in grown if g is not None for tile in _faces(g) for k in pieces_of(tile)}
    for k in range(1, n + 1):
        if k in of_skin and k not in of_mids:
            skin[k] = True
            continue
        q = _inner_point(_T.Face_s(fmap.FindKey(k)))
        if q is None:
            continue
        for i, what, f, box in sources:
            if not _near_box(q, box, tol) or not _in_face(f, q, IN_FACE_SHARE * tol) or _dist(_vertex(q), f) > tol:
                continue
            if what == "skin":
                skin[k] = True
            else:
                owners[k].add(i)
                if what == "core":
                    core[k].add(i)
    # ... and by the fuse's own history too, for the mid-faces themselves: a piece of a mid-face whose inner point
    # cannot be read (an offset B-spline piece the mesher will not triangulate) is still that mid-face, never lost
    for i, m in enumerate(mine):
        pieces = list(b.Modified(m.original))
        if not pieces and not b.IsDeleted(m.original):
            pieces = [m.original]
        for piece in pieces:
            k = fmap.FindIndex(piece)
            if k > 0 and not core[k] and not owners[k]:
                owners[k].add(i)
                core[k].add(i)
    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(fused, TopAbs_EDGE, TopAbs_FACE, e2f)
    face_edges: list[list[int]] = [[] for _ in range(n + 1)]
    edge_faces: list[list[int]] = [[]]
    for ei in range(1, e2f.Extent() + 1):
        fs = sorted({fmap.FindIndex(f) for f in e2f.FindFromIndex(ei)} - {0})
        edge_faces.append(fs)
        for k in fs:
            face_edges[k].append(ei)
    inside: dict[int, bool] = {}

    def in_material(k: int) -> bool:
        if k not in inside:
            from OCP.TopoDS import TopoDS

            p = _inner_point(TopoDS.Face_s(fmap.FindKey(k)))
            inside[k] = p is not None and solid.strictly_inside(p)
        return inside[k]

    def blocked(ei: int, i: int) -> bool:
        return any(owners[h] and i not in owners[h] for h in edge_faces[ei])

    kept_ext: dict[int, set[int]] = {}
    for i in range(len(mine)):
        start = [k for k in range(1, n + 1) if i in core[k]]
        seen = set(start)
        todo = list(start)
        ext: set[int] = set()
        while todo:
            k = todo.pop()
            for ei in face_edges[k]:
                if blocked(ei, i):
                    continue
                for g in edge_faces[ei]:
                    if g in seen or i not in owners[g] or core[g]:
                        continue
                    seen.add(g)
                    if in_material(g):
                        ext.add(g)
                        todo.append(g)
        # (c): keep the pieces of an extension that reach another mid-face, or run to the part's own skin (a wall
        # whose mid-face stopped short of its end); a stub ending inside the material (a thick region) is dropped
        comps, left = [], set(ext)
        while left:
            s0 = min(left)
            comp, stack = {s0}, [s0]
            left.discard(s0)
            while stack:
                k = stack.pop()
                for ei in face_edges[k]:
                    for g in edge_faces[ei]:
                        if g in left:
                            left.discard(g)
                            comp.add(g)
                            stack.append(g)
            comps.append(comp)
        keep: set[int] = set()
        for comp in comps:
            meets = any(blocked(ei, i) and any(owners[h] and i not in owners[h] and (core[h] or in_material(h))
                                               for h in edge_faces[ei])
                        for k in comp for ei in face_edges[k])
            meets = meets or any(any(skin[h] for h in edge_faces[ei]) and not any(i in core[h] for h in edge_faces[ei])
                                 for k in comp for ei in face_edges[k])
            if meets:
                keep |= comp
        kept_ext[i] = keep
    chosen: dict[int, int] = {}
    for k in range(1, n + 1):
        if core[k]:
            chosen[k] = min(core[k])
    for i in sorted(kept_ext):
        for k in sorted(kept_ext[i]):
            chosen.setdefault(k, i)
    from OCP.TopoDS import TopoDS

    kept =[TopoDS.Face_s(fmap.FindKey(k)) for k in sorted(chosen)]
    owner = [chosen[k] for k in sorted(chosen)]
    reached: list[list[tuple[int | None, float]]] = [[] for _ in mine]
    for i, ks in kept_ext.items():
        for k in sorted(ks):
            others = sorted({o for ei in face_edges[k] for h in edge_faces[ei] for o in owners[h]} - {i})
            reached[i].append((others[0] if others else None, _area(TopoDS.Face_s(fmap.FindKey(k)))))
    return _compound(kept), kept, owner, reached


def _face_bodies(shape, faces_map) -> tuple[list[int], list]:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    smap = _map(shape, TopAbs_SOLID)
    solids = [TopoDS.Solid_s(smap.FindKey(i)) for i in range(1, smap.Extent() + 1)]
    body = [-1] * faces_map.Extent()
    for b, s in enumerate(solids):
        ex = TopExp_Explorer(s, TopAbs_FACE)
        while ex.More():
            k = faces_map.FindIndex(ex.Current())
            if k > 0 and body[k - 1] < 0:
                body[k - 1] = b
            ex.Next()
    return body, solids


def _neighbours(shape, faces_map) -> list[set[int]]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
    out: list[set[int]] = [set() for _ in range(faces_map.Extent())]
    for i in range(1, e2f.Extent() + 1):
        ks = {faces_map.FindIndex(f) - 1 for f in e2f.FindFromIndex(i)} - {-1}
        for a in ks:
            out[a] |= ks - {a}
    return out


def _regions(faces: list[int], neighbours: list[set[int]]) -> list[list[int]]:
    left, out = set(faces), []
    for f in sorted(faces):
        if f not in left:
            continue
        group, todo = [], [f]
        left.discard(f)
        while todo:
            x = todo.pop()
            group.append(x)
            for y in sorted(neighbours[x] & left):
                left.discard(y)
                todo.append(y)
        out.append(sorted(group))
    return out


#: SHELL-6, NEVER FAIL: a region the mid-faces do not close - the part's faces within FALLBACK_WALLS times the
#: thicker wall there of an open edge - is meshed SOLID and tied to the shell round it, and said so. A body the
#: extraction cannot read at all is meshed solid whole. The record never stops a run.
FALLBACK_WALLS = 3.0
#: a join's sewn faces must hold the body's mid-faces' own area to within this share (SHELL-3)
JOIN_KEEP_SHARE = 0.01


#: SHELL-7, PLATES OF TWO THICKNESSES MEETING IN LINE (a butt step, flush on one side): their mid-planes are
#: parallel and apart by half the difference, so nothing meets. The thinner plate's mid-face is moved onto the
#: thicker one's mid-plane - when the two are parallel within STEP_PARALLEL_DEG, apart by no more than half the
#: thicker wall, and touch edge to edge (the faces come within the offset plus STEP_TOUCH of the thicker wall:
#: a plate crossing right at the step parts them by its own wall) - and the offset is kept for the solver (Nastran ZOFFS, Abaqus / CalculiX OFFSET): the same stiffness and mass,
#: one flat shell. The checks still read where the mid-face really lies.
STEP_PARALLEL_DEG = 1.0
STEP_TOUCH = 0.5



#: SHELL-10, A FITTED MID-FACE BRIDGED TO THE MID-FACE IT CONTINUES: a bend whose outside is not concentric with its
#: inside (a CAD model that rounds both corners of a sheet alike) is a tapered wall, and its fitted mid-face ends
#: where its two faces stop facing each other - short of its flanges' mid-faces, which run on almost tangent to
#: it, so neither extension nor the general fuse meets them. A free edge of a fitted mid-face that runs alongside
#: a free edge of another mid-face of the body - every one of BRIDGE_SAMPLES + 1 points of each edge within
#: BRIDGE_WALLS of the thicker wall of the other edge, the two edges apart by more than the sewing tolerance, and
#: the strip between them inside the material - is joined to it by the ruled strip between the two edges, a part
#: of the fitted mid-face.
BRIDGE_WALLS = 1.0
BRIDGE_SAMPLES = 4


def _free_edges(face, others: list, tol: float) -> list:
    """The edges of a face that no other face (in `others`) lies along (its middle point further than tol)."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    emap = _map(face, TopAbs_EDGE)
    out = []
    for i in range(1, emap.Extent() + 1):
        e = TopoDS.Edge_s(emap.FindKey(i))
        if _length(e) <= tol:
            continue
        mid = _vertex(_edge_points(e, 2)[1])
        if not any(_dist(mid, o) <= tol for o in others):
            out.append(e)
    return out


def bridges(mine: list[_Mid], solid: _Solid, fuzzy: float) -> list[tuple[int, int, object]]:
    """SHELL-10: the ruled strips joining fitted mid-faces to the mid-faces they continue: [(fitted mid index,
    other mid index, strip face)]."""
    from OCP.BRepFill import BRepFill
    from OCP.TopoDS import TopoDS

    out: list[tuple[int, int, object]] = []
    if not any(m.fitted for m in mine):
        return out
    faces = [m.face for m in mine]
    tol = 10.0 * fuzzy
    free = [_free_edges(f, faces[:i] + faces[i + 1:], tol) for i, f in enumerate(faces)]
    used: set[tuple[int, int, int, int]] = set()
    for i, m in enumerate(mine):
        if not m.fitted:
            continue
        for a, ea in enumerate(free[i]):
            pa = _edge_points(ea, BRIDGE_SAMPLES)
            for j, o in enumerate(mine):
                if j == i:
                    continue
                reach = BRIDGE_WALLS * max(m.t_max, o.t_max)
                for b, eb in enumerate(free[j]):
                    key = (min(i, j), a if i < j else b, max(i, j), b if i < j else a)
                    if key in used:
                        continue
                    pb = _edge_points(eb, BRIDGE_SAMPLES)
                    gaps = [_dist(_vertex(x), eb) for x in pa] + [_dist(_vertex(x), ea) for x in pb]
                    if max(gaps) > reach or max(gaps) <= tol:
                        continue
                    # the strip between them, its middle in the material
                    flip = (np.linalg.norm(pa[0] - pb[0]) + np.linalg.norm(pa[-1] - pb[-1])
                            > np.linalg.norm(pa[0] - pb[-1]) + np.linalg.norm(pa[-1] - pb[0]))
                    mids = [0.5 * (x + y) for x, y in zip(pa, pb[::-1] if flip else pb, strict=True)]
                    if not all(solid.inside(x) for x in mids[1:-1]):
                        continue
                    try:
                        strip = BRepFill.Face_s(ea, TopoDS.Edge_s(eb.Reversed()) if flip else eb)
                    except Exception as exc:  # noqa: BLE001 - no strip, the edge stays open
                        logger.debug("shell: no strip between Mid_%d and Mid_%d (%s)", m.k + 1, o.k + 1, exc)
                        continue
                    if strip.IsNull() or _area(strip) <= tol * _length(ea):
                        continue
                    used.add(key)
                    out.append((i, j, strip))
    return out


def _join_straight(mine: list[_Mid], solid: _Solid, fuzzy: float, notes: list[str], body: str):
    """The straight-edge extension (extend) and a plain sew: (fused shape, each mid-face's faces in it)."""
    for m in mine:
        m.face = m.original
        try:
            extend(m, mine, solid, fuzzy)
        except Exception as exc:  # noqa: BLE001 - a mid-face that cannot be extended keeps its shape
            logger.debug("shell: Mid_%d could not be extended (%s)", m.k + 1, exc)
    if len(mine) == 1:
        return _compound([mine[0].face]), [[mine[0].face]]
    try:
        strips = bridges(mine, solid, fuzzy)
    except Exception as exc:  # noqa: BLE001 - no strips, the edges stay as they are
        logger.debug("shell: the fitted mid-faces could not be bridged on %s (%s)", body, exc)
        strips = []
    for i, j, strip in strips:
        mine[i].extended.append({"to": mine[j].k, "area": _area(strip)})
    try:
        fused, images = sew([m.face for m in mine] + [f for _i, _j, f in strips], fuzzy)
    except ValueError as exc:
        notes.append(f"{body}: {exc}.")
        return _compound([m.face for m in mine]), [[m.face] for m in mine]
    out = [list(images[i]) for i in range(len(mine))]
    for n, (i, _j, _f) in enumerate(strips):
        out[i] += images[len(mine) + n]
    return fused, out


def _join_general(mine: list[_Mid], body_faces: list, solid: _Solid, *, t_max: float, fuzzy: float,
                  remake: bool = True, patch_alone: bool = True):
    """The general join (join_body) and one more sew of what it kept: (fused shape, each mid-face's faces)."""
    fused, kept, owner_i, reached = join_body(mine, body_faces, solid, t_max=t_max, fuzzy=fuzzy, remake=remake,
                                              patch_alone=patch_alone)
    for i, m in enumerate(mine):
        for other, area in reached[i]:
            m.extended.append({"to": None if other is None else mine[other].k, "area": area})
    images = [[f for f, o in zip(kept, owner_i, strict=True) if o == i] for i in range(len(mine))]
    if len(kept) > 1:
        fused, again = sew(kept, fuzzy)
        images = [[g for f, o in zip(kept, owner_i, strict=True) if o == i for g in again[kept.index(f)]]
                  for i in range(len(mine))]
    return fused, images


def snap_steps(mine: list[_Mid], solid: _Solid) -> None:
    """SHELL-7 on one body's mid-faces, thickest first (a plate snaps to a neighbour already settled)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    cos_lim = math.cos(math.radians(STEP_PARALLEL_DEG))
    order = sorted(mine, key=lambda m: (-m.t, m.k))
    for m in order:
        if m.plane is None:
            continue
        best = None
        for o in order:
            if o is m or o.plane is None or o.t <= m.t * (1 + 1e-6):
                continue
            no = o.plane[1]
            if abs(float(np.dot(m.plane[1], no))) < cos_lim:
                continue
            d = float(np.dot(o.plane[0] - m.plane[0], no))      # m's plane to o's plane, along o's normal
            if abs(d) <= 1e-9 * o.t or abs(d) > 0.5 * o.t * (1 + 1e-6):
                continue
            if not _near_box(m.box[0], (o.box[0], o.box[1]), abs(d) + o.t + max(m.box[1] - m.box[0])):
                continue
            if _dist(m.original, o.original) > abs(d) + STEP_TOUCH * o.t:
                continue
            if best is None or o.t > best[0].t:
                best = (o, d)
        if best is None:
            continue
        o, d = best
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(*map(float, o.plane[1] * d)))
        moved = BRepBuilderAPI_Transform(m.original, tr, True).Shape()
        moved_face = _faces(moved)[0]
        q = _inner_point(moved_face)
        if q is None or not solid.inside(q):
            continue
        m.original, m.face = moved_face, moved_face
        m.plane = _plane(moved_face)
        m.box = m.face_box = _box(moved_face)
        m.offset = -d                         # the true mid-face lies this far from the shell's reference plane


def fallback_regions(sewn_rows: list[dict], faces: list, names: list[str], body_of: list[int],
                     body_names: list[str], pairs: list, mm: float) -> list[dict]:
    """SHELL-6: one region per group of open edges of a body (open points within FALLBACK_WALLS walls of each
    other share a region): the CAD faces round them, where they are, and why."""
    out: list[dict] = []
    for row in sewn_rows:
        pts = [np.asarray(q, dtype=float) / mm for q in row.get("open_at_mm", [])]
        if not pts:
            continue
        b = body_names.index(row["body"])
        walls = [pr.thickness for pr in pairs if pr.shell and body_of[pr.a] == b]
        reach = FALLBACK_WALLS * (max(walls) if walls else 0.0) / mm
        mine = [(k, _box(faces[k])) for k in range(len(faces)) if body_of[k] == b]
        groups: list[list[np.ndarray]] = []
        for q in pts:
            for g in groups:
                if any(np.linalg.norm(q - r) <= reach for r in g):
                    g.append(q)
                    break
            else:
                groups.append([q])
        for g in groups:
            near = sorted({k for q in g for k, box in mine
                           if _near_box(q, box, reach) and _dist(_vertex(q), faces[k]) <= reach})
            out.append({"body": row["body"], "faces": [names[k] for k in near], "at_mm": [_pt(q * mm) for q in g],
                        "why": "the mid-faces do not meet here; this region is meshed solid and tied to the shell"})
    return out


def extract(shape, *, mm_per_unit: float, names: list[str] | None = None) -> tuple[dict, object]:
    """The shell record and the sewn mid-surface (a compound, the shape's own units) of an OpenCASCADE shape
    whose coordinates are `mm_per_unit` millimetres each. Never raises for a part it cannot shell: the record
    says what was not shelled and why."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad import fea_measure as fm

    started = time.time()
    mm = float(mm_per_unit)
    fmap = _map(shape, TopAbs_FACE)
    faces = _faces(shape)
    body_of, solids = _face_bodies(shape, fmap)
    if names is None:
        if len(solids) > 1:
            from meshpipeline.cad.fea_assembly import global_face_names

            names = global_face_names(shape)
        else:
            names = [f"Face_{k}" for k in range(1, len(faces) + 1)]
    body_names = [f"Part_{b + 1}" for b in range(len(solids))] or ["Part_1"]
    notes: list[str] = []
    surface, _f, kinds = fm._brep_surface(shape, mm)
    partners = sp.ray_partners(surface)
    diag_mm = float(surface.diag)

    # SHELL-1: the even wall pairs, confirmed on the exact faces (read from the smaller face)
    pairs: list[sp.Pair] = []
    for (a, b), rays in partners.items():
        if body_of[a] != body_of[b] or body_of[a] < 0:
            continue
        small, big = (a, b) if _area(faces[a]) <= _area(faces[b]) else (b, a)
        try:
            ok, t, spread, why = sp.even_wall(faces[small], faces[big], mm)
        except Exception as exc:  # noqa: BLE001 - a pair that cannot be read is no pair
            logger.debug("shell: pair %s/%s could not be read (%s)", names[a], names[b], exc)
            ok, t, spread, why = False, float("nan"), float("nan"), "the faces could not be read"
        pair = sp.Pair(a=a, b=b, thickness=t, spread=spread, rays=rays, even=ok, why=why)
        # SHELL-9: a wall that is not even but turns little is a TAPERED wall, judged at its thickest point
        if not ok and ("not parallel" in why or "not even" in why):
            try:
                tap, lo, hi, _turn, why_t = sp.tapered_wall(faces[small], faces[big], mm)
            except Exception as exc:  # noqa: BLE001 - no taper read: the pair stays as it was
                logger.debug("shell: taper of %s/%s could not be read (%s)", names[a], names[b], exc)
                tap = False
            if tap:
                pair.tapered, pair.t_min, pair.t_max, pair.thickness = True, lo, hi, hi
                pair.why = f"a tapered wall, {lo:.3g} to {hi:.3g} mm"
        pairs.append(pair)

    # the mid-faces of every even or tapered pair, and each pair's width and verdict (SHELL-2)
    mids: list[_Mid] = []
    for i, p in enumerate(pairs):
        if not p.even and not p.tapered:
            continue
        t_u = p.thickness / mm
        got = []
        if p.even:
            try:
                got = mid_faces(faces[p.a], faces[p.b], t_u, 1e-4 * t_u)
            except Exception as exc:  # noqa: BLE001
                logger.debug("shell: the mid-face of %s/%s failed (%s)", names[p.a], names[p.b], exc)
        fitted, lo_u, hi_u, mean_u = False, t_u, t_u, t_u
        if not got:
            # SHELL-9: a tapered wall, or an even one whose half-offsets do not overlap (a twisted strap): fitted
            small, big = (p.a, p.b) if _area(faces[p.a]) <= _area(faces[p.b]) else (p.b, p.a)
            try:
                # a fitted mid-face lies between its two faces: when both are far narrower than four of its
                # walls, so is it - not fitted at all (17 x 17 midpoints each; a fan housing has hundreds)
                wide = FIT_WIDTH_SLACK * max(width_of(faces[p.a]), width_of(faces[p.b]))
                if wide < t_u / sp.SHELL_WIDTH_RATIO:
                    got, why_f = [], "it is narrower than four walls (a local feature of the wall beside it)"
                else:
                    got, lo_u, hi_u, mean_u, why_f = fitted_mid(faces[small], faces[big], t_u, 1e-4 * t_u)
            except Exception as exc:  # noqa: BLE001
                logger.debug("shell: the fitted mid-face of %s/%s failed (%s)", names[p.a], names[p.b], exc)
                got, why_f = [], "the faces could not be read"
            fitted = bool(got)
            if not got:
                p.why = "the two faces do not overlap" if p.even else f"{p.why}; no mid-surface: {why_f}"
                p.tapered = False
                continue
            if p.tapered:
                p.t_min, p.t_max, p.thickness = lo_u * mm, hi_u * mm, hi_u * mm
                p.why = f"a tapered wall, {p.t_min:.3g} to {p.t_max:.3g} mm"
        for f in got:
            p.mids.append(len(mids))
            m = _Mid(len(mids), f, i, body_of[p.a], mean_u if fitted else t_u)
            if fitted:
                m.fitted, m.t_min, m.t_max = True, lo_u, hi_u
            mids.append(m)
    neighbours = _neighbours(shape, fmap)
    # SHELL-2: shell or solid, sheet by sheet
    for s_i, group in enumerate(sp.sheets(pairs, neighbours, body_of)):
        sheet_faces = [mids[k].face for i in group for k in pairs[i].mids]
        size = math.sqrt(sum(_area(f) for f in sheet_faces)) * mm
        width = width_of(_compound(sheet_faces)) * mm
        t = max(pairs[i].thickness for i in group)
        shell = t <= sp.SHELL_RATIO * size and t <= sp.SHELL_WIDTH_RATIO * width
        for i in group:
            pairs[i].sheet, pairs[i].size, pairs[i].width, pairs[i].shell = s_i, size, width, shell
    # SHELL-8: a lap (two shell plates stacked and fused) is shelled with the plates it is made of
    for i in sp.laps(pairs):
        pairs[i].shell = True
        pairs[i].why = "a lap: two shell plates stacked, shelled with them"

    # SHELL-2: every face's role
    widths, radii, arcs = [], [], []
    for k, f in enumerate(faces):
        try:
            widths.append(strip_width(f) * mm)
        except Exception:  # noqa: BLE001
            widths.append(float("inf"))
        per = _length(f)
        # a cylinder or cone that closes round its axis is a boss, a pin or a bore - never a fillet, whose arc runs
        # across it: a 6 mm stand-off of radius 3 on a 3 mm wall read as a blend by its developed width
        arcs.append(float("inf") if kinds[k] in ("cylinder", "cone") and _full_turn(f)
                    else 2.0 * _area(f) / per * mm if per > 0 else float("inf"))
        radii.append(None if kinds[k] == "plane" else fm.face_radius(f, mm))
    roles = sp.face_roles(names, pairs, kinds=kinds, widths=widths, radii=radii, neighbours=neighbours,
                          arcs=arcs)
    for k, row in enumerate(roles):
        row["body"] = body_names[body_of[k]] if body_of[k] >= 0 else None

    # the body verdicts
    bodies = []
    shell_bodies: set[int] = set()
    for b, sol in enumerate(solids):
        ks = [k for k in range(len(faces)) if body_of[k] == b]
        area = sum(_area(faces[k]) for k in ks) * mm * mm
        shellish = [k for k in ks if roles[k]["role"] in (sp.PAIRED, sp.RIM, sp.BLEND)]
        solid_faces = [k for k in ks if roles[k]["role"] in (sp.SOLID, sp.THICK)]
        has_shell = any(roles[k]["role"] == sp.PAIRED for k in ks)
        share = sum(_area(faces[k]) for k in shellish) * mm * mm / area if area > 0 else 0.0
        if has_shell and not solid_faces:
            verdict, why = "shell", "every wall is thin against its size"
            shell_bodies.add(b)
        elif has_shell:
            verdict, why = "mixed", "thin walls and thick regions"
        else:
            verdict, why = "solid", "no wall is thin against its size"
        row = {"body": body_names[b], "verdict": verdict, "why": why, "area_mm2": _r(area),
               "volume_mm3": _r(_volume(sol) * mm ** 3), "shell_area_share": _r(share)}
        if verdict == "mixed":
            row["solid_regions"] = [{"faces": [names[k] for k in reg],
                                     "area_mm2": _r(sum(_area(faces[k]) for k in reg) * mm * mm)}
                                    for reg in _regions(solid_faces, neighbours)]
        bodies.append(row)

    # SHELL-3 / SHELL-4: extend and sew the shell mid-faces of every shell or mixed body
    from OCP.TopTools import TopTools_IndexedMapOfShape

    surface_faces: list = []
    surface_owner: list[int] = []
    seen = TopTools_IndexedMapOfShape()
    sewn_rows = []
    for b, sol in enumerate(solids):
        mine = [m for m in mids if m.body == b and pairs[m.pair].shell]
        if not mine:
            continue
        t_min = min(m.t for m in mine)
        fuzzy = min(max(SEW_SHARE * t_min, SEW_TOL_FACTOR * max(_max_tol(m.original) for m in mine)),
                    SEW_SHARE_MAX * t_min)
        solid = _Solid(sol, 1e-3 * t_min, wall=t_min)
        body_faces = [faces[k] for k in range(len(faces)) if body_of[k] == b]
        t_max = max(pairs[i].thickness for i in range(len(pairs))
                    if (pairs[i].even or pairs[i].tapered) and body_of[pairs[i].a] == b and pairs[i].shell) / mm
        snap_steps(mine, solid)
        for m in mine:
            g = remade_offset(m.original, OFFSET_REMAKE_SHARE * m.t)
            if g is not None:
                m.original = m.face = g
                m.box = m.face_box = _box(g)
                m.remade = True
        thick_faces = [faces[k] for k in range(len(faces))
                       if body_of[k] == b and roles[k]["role"] in (sp.SOLID, sp.THICK)]

        def census_of(fused, images, mine=mine, solid=solid, thick_faces=thick_faces):
            fmap_f = _map(fused, TopAbs_FACE)
            owner_t = {}
            for m, imgs in zip(mine, images, strict=True):
                for f in imgs:
                    k = fmap_f.FindIndex(f)
                    if k > 0:
                        owner_t[k] = m.t
            return edge_census(fused, owner_t, solid, scale=mm, thick_faces=thick_faces)

        # SHELL-3, BOTH JOINS, THE BETTER KEPT: the straight-edge extension (plane to plane: T, L, box, the bent and
        # slotted sheets of the corpus) and the general join (grow, cut, keep: any surfaces - a tube on a plate,
        # a cylinder into a cone, a relief-cut flange). The one leaving fewer open edges is kept; on a tie, the
        # straight one. Deterministic: each is.
        straight = _join_straight(mine, solid, fuzzy, notes, body_names[b])
        census = census_of(*straight)
        chosen, method, ext_straight = straight, "straight", [list(m.extended) for m in mine]
        if census["open_edges"]:
            for m in mine:
                m.extended = []
            # the general join, and - when a mid-face is an offset surface, re-made as a B-spline to be grown past its
            # own ends - the same without that re-making: the better of the two is kept, as between the joins
            from OCP.BRep import BRep_Tool
            from OCP.Geom import Geom_OffsetSurface

            offsets = any(m.remade or isinstance(BRep_Tool.Surface_s(m.original), Geom_OffsetSurface) for m in mine)
            # a free-form mid-face's grown patch goes in alone (a twisted strap's wall was there twice with both),
            # and also with the mid-face beside it, as alone can leave a re-made offset wall's junctions open that
            # both close (a cyclone): the one leaving fewer open edges is kept, on a tie the alone one
            free = any(isinstance(BRep_Tool.Surface_s(m.original), FREE_FORM) for m in mine)
            core = sum(_area(m.true_face) for m in mine)
            ext_general: list = []
            for remake, alone in [(r, a) for r in ((True, False) if offsets else (True,))
                                  for a in ((True, False) if free else (True,))]:
                if method == "general" and census["open_edges"] == 0:
                    break
                # a variant joins differently only near the walls it treats differently: with the mid-faces in
                # beside their patches, the free-form walls; without the re-making, the offset walls. It is tried
                # only when an open edge left is within a grown patch's reach of such a wall - measured to the
                # wall itself, its box first (a fan housing's free-form wall spans the whole body, so its box
                # held every open edge: 3 joins of 1 to 2 minutes each, none closing an edge)
                if not (remake and alone):
                    reach = 0.5 * t_max + GROW_WALLS * max(m.t for m in mine)
                    listed = census["open_edges"] <= len(census.get("open_at_mm", []))
                    walls = [m for m in mine
                             if (alone or isinstance(BRep_Tool.Surface_s(m.original), FREE_FORM))
                             and (remake or m.remade or isinstance(BRep_Tool.Surface_s(m.original), Geom_OffsetSurface))]
                    if listed and not any(_near_box(q, m.box, reach) and _dist(_vertex(q), m.original) <= reach
                                          for q in (np.asarray(x, float) / mm for x in census.get("open_at_mm", []))
                                          for m in walls):
                        continue
                for m in mine:
                    m.extended = []
                try:
                    general = _join_general(mine, body_faces, solid, t_max=t_max, fuzzy=fuzzy, remake=remake,
                                            patch_alone=alone)
                    census_g = census_of(*general)
                except Exception as exc:  # noqa: BLE001 - the straight join stands
                    logger.debug("shell: the general join failed on %s (%s)", body_names[b], exc)
                    continue
                # a join must keep every mid-face whole: one that dropped mid-faces (a cyclone funnel's whole
                # surface once came back empty, and so with no open edge) is no join
                kept = sum(_area(f) for imgs in general[1] for f in imgs)
                if kept < (1.0 - JOIN_KEEP_SHARE) * core:
                    notes.append(f"{body_names[b]}: a general join lost {100 * (1 - kept / core):.0f}% of the "
                                 "mid-faces' area and was not used.")
                elif census_g["open_edges"] < census["open_edges"]:
                    chosen, method, census = general, "general", census_g
                    ext_general = [list(m.extended) for m in mine]
            if method == "general":
                for m, ext in zip(mine, ext_general, strict=True):
                    m.extended = ext
            if method == "straight":
                for m, ext in zip(mine, ext_straight, strict=True):
                    m.extended = ext
        fused, images = chosen
        census["body"] = body_names[b]
        census["join"] = method
        sewn_rows.append(census)
        for m, imgs in zip(mine, images, strict=True):
            for f in imgs:
                # a face once only: a join can hand back one face twice, turned both ways (sh0011's cyclone),
                # and the surface written would hold fewer faces than the record names
                if seen.Contains(f):
                    continue
                seen.Add(f)
                surface_faces.append(f)
                surface_owner.append(m.k)

    # SHELL-5: the checks
    mid_rows = []
    worst_rel = 0.0
    for m in mids:
        p = pairs[m.pair]
        if m.fitted:
            # SHELL-9: as far from one face as from the other, within FIDELITY_SHARE of the wall there
            err, rel, npts, _lo, _hi = fitted_fidelity(m.true_face, faces[p.a], faces[p.b])
            worst_rel = max(worst_rel, rel)
        else:
            err, npts = fidelity(m.true_face, faces[p.a], faces[p.b], m.t)
            worst_rel = max(worst_rel, err / m.t if m.t > 0 else 0.0)
        mid_rows.append({"id": f"Mid_{m.k + 1}", "pair": f"P{m.pair + 1}", "body": body_names[m.body],
                         "faces": [names[p.a], names[p.b]], "thickness_mm": _r(p.thickness),
                         **({"fitted": True, "thickness_range_mm": [_r(m.t_min * mm), _r(m.t_max * mm)],
                             "thickness_mean_mm": _r(m.t * mm)} if m.fitted else {}),
                         "shell": bool(p.shell), "area_mm2": _r(_area(m.true_face) * mm * mm),
                         **({"offset_mm": _r(m.offset * mm)} if m.offset else {}),
                         "kind": fm._kind_word(BRepAdaptor_Surface(m.true_face if m.remade else m.original,
                                                                   True).GetType()),
                         "extended": [{"to": None if e["to"] is None else f"Mid_{e['to'] + 1}",
                                       **({"by_mm": _r(e["by"] * mm)} if "by" in e else {}),
                                       **({"area_mm2": _r(e["area"] * mm * mm)} if "area" in e else {})}
                                      for e in m.extended],
                         "fidelity": {"max_error_mm": _r(err * mm), "points": npts}})
    # the volume the sewn surface holds: each sewn face (a mid-face as extended to its junctions) times its wall -
    # a short-legged angle's corner is a quarter of its metal, and the mid-faces alone left it out
    held = [0.0] * len(solids)
    for f, k in zip(surface_faces, surface_owner, strict=True):
        held[mids[k].body] += _area(f) * mids[k].t
    for row, b in zip(bodies, range(len(solids)), strict=True):
        if held[b] > 0 and row["volume_mm3"]:
            row["mid_volume_ratio"] = _r(held[b] * mm ** 3 / row["volume_mm3"])
    open_edges = sum(c["open_edges"] for c in sewn_rows)
    accounted = all(roles[k]["role"] in (sp.PAIRED, sp.RIM, sp.BLEND)
                    for k in range(len(faces)) if body_of[k] in shell_bodies)
    vol_ok = all(VOLUME_BAND[0] <= r.get("mid_volume_ratio", 1.0) <= VOLUME_BAND[1]
                 for r in bodies if r["verdict"] == "shell")
    checks = {"fidelity": worst_rel <= FIDELITY_SHARE, "connected": open_edges == 0,
              "faces_accounted": accounted, "volume": vol_ok}
    fallback = fallback_regions(sewn_rows, faces, names, body_of, body_names, pairs, mm)
    if fallback:
        notes.append(f"{len(fallback)} region(s) where the mid-faces do not meet are meshed solid and tied to the "
                     "shell there (SHELL-6).")
    loose = sum(1 for k in range(len(faces)) if body_of[k] < 0)
    if loose:
        notes.append(f"{loose} face(s) belong to no solid body (a sheet beside the bodies); they are not shelled.")
    record = {"version": VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO, "diag_mm": _r(diag_mm),
              "bodies": bodies,
              "pairs": [{"id": f"P{i + 1}", "faces": [names[p.a], names[p.b]], "thickness_mm": _r(p.thickness),
                         "spread": _r(p.spread), "rays": p.rays, "even": p.even, "why": p.why,
                         **({"tapered": True, "thickness_range_mm": [_r(p.span()[0]), _r(p.span()[1])]}
                            if p.tapered else {}),
                         "sheet": None if p.sheet is None else f"S{p.sheet + 1}",
                         "size_mm": None if p.size is None else _r(p.size),
                         "width_mm": None if p.width is None else _r(p.width),
                         "ratio": None if not p.size else _r(p.thickness / p.size), "shell": p.shell,
                         "mids": [f"Mid_{k + 1}" for k in p.mids]} for i, p in enumerate(pairs)],
              "faces": roles, "mids": mid_rows, "sewn": sewn_rows,
              "surface_faces": [f"Mid_{k + 1}" for k in surface_owner],
              "checks": checks, "fallback_regions": fallback, "complete": True, "notes": notes,
              "seconds": round(time.time() - started, 2)}
    return record, _compound(surface_faces)


def solid_record(shape, why: str) -> dict:
    """SHELL-6: the record of a part that is meshed solid whole - every body solid, with the reason."""
    from OCP.TopAbs import TopAbs_SOLID

    n = _map(shape, TopAbs_SOLID).Extent()
    return {"version": VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO,
            "bodies": [{"body": f"Part_{b + 1}", "verdict": "solid", "why": why} for b in range(max(n, 1))],
            "pairs": [], "faces": [], "mids": [], "sewn": [], "surface_faces": [],
            "checks": {"fidelity": True, "connected": True, "faces_accounted": True, "volume": True},
            "fallback_regions": [], "complete": False, "notes": [f"The whole part is meshed solid: {why}."],
            "seconds": 0.0}


def extract_file(path: Path | str, out_dir: Path | str | None = None, *, mm_per_unit: float) -> dict:
    """Read a STEP, IGES or BREP file, extract its mid-surface, and (when `out_dir` is given) write
    shell.json and midsurface.brep there."""
    from meshpipeline.cad.scout import read_cad

    shape = read_cad(Path(path))
    try:
        record, surface = extract(shape, mm_per_unit=mm_per_unit)
    except Exception as exc:  # noqa: BLE001 - SHELL-6: never fail; the whole part is meshed solid and told so
        logger.warning("shell: the extraction failed (%s); the part is meshed solid", exc)
        record, surface = solid_record(shape, f"the mid-surface could not be extracted ({type(exc).__name__})"), \
            _compound([])
    if out_dir is not None:
        from OCP.BRepTools import BRepTools

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        BRepTools.Write_s(surface, str(out / SURFACE_FILE))
        (out / RECORD_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    return record


__all__ = ["RECORD_FILE", "SURFACE_FILE", "VERSION", "extract", "extract_file", "mid_faces", "width_of"]
