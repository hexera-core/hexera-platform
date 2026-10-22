# Responsibility: Find the fluid an assembly of parts holds - the space inside them, closed off where
# it meets the outside - and hand it over as a closed fluid volume whose caps are its openings: the
# "volume extract" of the industry's CAD tools (cap the openings, keep the enclosed cavity).
# Owns: the candidate caps (every hole a flat face of a part rims, every free loop of an open sheet),
# the split of space by the parts and the caps, which regions are metal, outside or fluid, the
# openings of each fluid passage and a point inside it, and the closed surface of the fluid.
# Boundaries: OpenCASCADE B-rep in metres (the caller scales; a triangle file comes in as a B-rep
# built on its triangles, from_triangles). No engine, no storage, no model call, nothing about what
# the part is called: every rule is measured on the geometry. The geometry check folds the result
# into its proposal (cad/fluid_volume_check); the internal-flow staging (cad_tessellate,
# internal_surface) stages it for the meshers (stage_bounded). The split runs in a time-boxed child
# process (run_bounded): an assembly OpenCASCADE cannot split in time leaves the caller standing.
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: OpenCASCADE's fuzzy tolerance for the split, as a share of the part's diagonal: coincident faces
#: of parts that touch (a flange on a flange, a seat in its bore) are one face, not two a hair apart.
FUZZY = 1e-7
#: A hole is a cap candidate when it is at least this share of the part's diagonal across: smaller
#: ones (a thread's root, a tapped hole's chamfer) cannot carry a flow worth meshing.
MIN_CAP = 2e-3
#: Two candidate caps are one when their centres lie this share of the diagonal apart and their
#: areas agree within CAP_SAME_AREA (two flanges bolted face to face rim the same bore).
CAP_SAME = 1e-5
CAP_SAME_AREA = 0.01
#: A passage is the fluid of the part when it holds at least this share of the largest passage's
#: volume; smaller closed spaces are the bore of a bolt hole capped at both ends, a pocket between
#: a nut and its washer: left out.
MIN_PASSAGE_SHARE = 0.02
#: An opening of a passage is proposed as one when it is at least this share of the passage's
#: widest opening (the scout's own MIN_OPENING_FRACTION): a drain tap or a gauge port stays a
#: sealed wall unless the user makes it an opening.
MIN_PORT_SHARE = 0.02
#: How many B-rep faces the split is tried on, at most: beyond it the check keeps its face-by-face
#: reading (a full car body, a city block).
MAX_FACES = 12000
#: A cap the fluid meets over a ring thinner than this share of the hole's width is a GAP, not an
#: opening: a lid that does not quite reach the rim of the body it closes (a cyclone's roof 0.3 mm
#: short of its 150 mm barrel). Sealed into the wall, never proposed.
SLIT_SHARE = 0.02
#: A port's seed is stepped in from its cap by these shares of the cap's radius.
SEED_DEPTHS = (0.5, 0.3, 1.0, 0.15, 2.0)
#: A seed this share of its port's radius clear of every face is clear enough to stop looking.
SEED_CLEAR = 0.2




class ExtractionError(RuntimeError):
    """The split of space could not be made - said in words a user can act on."""


@dataclass
class Port:
    """One opening of a passage: where its fluid meets the outside, closed by a flat cap."""
    cap: int                                 # index of the candidate cap it came from
    faces: list                              # the cap's pieces bounding the fluid (TopoDS_Face)
    area: float                              # m2 the flow crosses
    centroid: tuple[float, float, float]
    normal: tuple[float, float, float]       # out of the fluid, the way the fluid leaves
    hole: float = 0.0                        # m2 the hole's rim encloses (area + what stands in it)
    wh: tuple[float, float] = (0.0, 0.0)     # the hole's sides, shortest first (m)
    on_extremity: bool = False
    #: nothing of the parts lies straight ahead of it: it opens to the open air, not into another
    #: part's bore (which then reaches the outside some other way)
    clear_ahead: bool = True

    @property
    def gap(self) -> bool:
        """A thin ring of a hole mostly closed by something else (SLIT_SHARE): a gap, not a port."""
        if self.hole <= 0 or self.area >= 0.5 * self.hole:
            return False
        width = self.area / max(math.pi * self.diameter, 1e-30)
        return width < SLIT_SHARE * self.diameter

    @property
    def filled(self) -> float:
        """What stands in the hole (a centre rod's end, a stem): the hole less the flow area."""
        left = self.hole - self.area
        return left if left > 0.01 * self.hole else 0.0

    @property
    def diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.hole or self.area, 0.0) / math.pi)


@dataclass
class Passage:
    """One connected fluid region: the space regions it is made of and the openings it has."""
    regions: list[int]
    volume: float
    ports: list[Port] = field(default_factory=list)
    seed: tuple[float, float, float] | None = None
    seed_clearance: float = 0.0
    bbox: tuple = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    #: the faces that bound it, each oriented out of the fluid (TopoDS_Face)
    boundary: list = field(default_factory=list)

    @property
    def main_ports(self) -> list[Port]:
        """Its openings worth proposing, the widest first (MIN_PORT_SHARE)."""
        ports = sorted((p for p in self.ports if not p.gap), key=lambda p: -p.area)
        if not ports:
            return []
        return [p for p in ports if p.area >= MIN_PORT_SHARE * ports[0].area]


@dataclass
class Extraction:
    """What the split found. `passages` are the fluid, largest first; empty when the space inside
    the parts reaches the outside other than through a cap (a leak) or there is no space inside."""
    passages: list[Passage]
    parts: int
    caps: int
    regions: int
    seconds: float
    #: closed spaces left out: (volume m3, openings) - bolt bores, pockets
    pockets: list = field(default_factory=list)
    reason: str = ""
    diag: float = 0.0
    #: joints between parts beyond which the inside reaches the outside (metres)
    leaks: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.passages)

    def as_dict(self) -> dict:
        mm = 1000.0
        return {
            "found": self.found, "reason": self.reason, "parts": self.parts, "caps": self.caps,
            "regions": self.regions, "seconds": round(self.seconds, 1),
            "pockets_left_out": len(self.pockets),
            "leaks_mm": [[round(v * mm, 1) for v in c] for c in self.leaks[:6]],
            "passages": [{
                "volume_l": round(p.volume * 1000.0, 4),
                "openings": [{"centroid_mm": [round(v * mm, 2) for v in o.centroid],
                              "normal": [round(v, 5) for v in o.normal],
                              "area_mm2": round(o.area * mm * mm, 2),
                              "hole_mm2": round(o.hole * mm * mm, 2),
                              "wh_mm": [round(v * mm, 2) for v in o.wh],
                              "on_extremity": bool(o.on_extremity), "clear_ahead": bool(o.clear_ahead),
                              "gap": o.gap, "proposed": any(o is q for q in p.main_ports)}
                             for o in p.ports],
                "seed_mm": None if p.seed is None else [round(v * mm, 2) for v in p.seed],
            } for p in self.passages],
        }


# ------------------------------------------------------------------------------- helpers ----
def _explore(shape, kind, cast) -> list:
    from OCP.TopExp import TopExp_Explorer
    out = []
    e = TopExp_Explorer(shape, kind)
    while e.More():
        out.append(cast(e.Current()))
        e.Next()
    return out


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    return _explore(shape, TopAbs_FACE, TopoDS.Face_s)


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS
    return _explore(shape, TopAbs_SOLID, TopoDS.Solid_s)


def _surface(f) -> tuple[float, tuple[float, float, float]]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, g)
    c = g.CentreOfMass()
    return float(g.Mass()), (float(c.X()), float(c.Y()), float(c.Z()))


def _volume(s) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(s, g)
    return float(g.Mass())


def _box(shape) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b, False)
    if b.IsVoid():
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return (x0, y0, z0), (x1, y1, z1)


def _real_box(shape):
    """The box of the shape's own surface (cad/occ_box): OpenCASCADE's envelope can be many times
    the part, and a stray entity at 1e100 (a broken STEP) would make every tolerance meaningless."""
    from meshpipeline.cad.occ_box import loose_box, mesh_to_size
    box, _lin = mesh_to_size(shape, 1.0 / 400.0, 0.5)
    if not all(math.isfinite(v) and abs(v) < 1e6 for v in box):
        box = loose_box(shape)
    return (box[0], box[1], box[2]), (box[3], box[4], box[5])


def _face_plane(face, diag: float):
    """The plane a face lies in (gp_Pln), or None: a true plane, or a B-spline or other surface that
    is flat within a millionth of the part (an IGES file trims its flats as B-splines)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLib import BRepLib_FindSurface
    from OCP.GeomAbs import GeomAbs_Plane

    ad = BRepAdaptor_Surface(face)
    if ad.GetType() == GeomAbs_Plane:
        return ad.Plane()
    if ad.GetType() not in _FLAT_CAPABLE:
        return None
    fs = BRepLib_FindSurface(face, max(1e-6 * diag, 1e-9), True, False)
    if not fs.Found():
        return None
    from OCP.GeomAdaptor import GeomAdaptor_Surface
    ga = GeomAdaptor_Surface(fs.Surface())
    return ga.Plane() if ga.GetType() == GeomAbs_Plane else None


def _surface_types():
    from OCP.GeomAbs import (
        GeomAbs_BezierSurface,
        GeomAbs_BSplineSurface,
        GeomAbs_OtherSurface,
        GeomAbs_SurfaceOfExtrusion,
    )
    return (GeomAbs_BSplineSurface, GeomAbs_BezierSurface, GeomAbs_SurfaceOfExtrusion, GeomAbs_OtherSurface)


_FLAT_CAPABLE = _surface_types()


def _plane_normal(face, diag: float = 1.0) -> tuple[float, float, float] | None:
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    face = TopoDS.Face_s(face)
    pln = _face_plane(face, diag)
    if pln is None:
        return None
    d = pln.Axis().Direction()
    if face.Orientation() == TopAbs_REVERSED:
        return (-d.X(), -d.Y(), -d.Z())
    return (d.X(), d.Y(), d.Z())


# ------------------------------------------------------------------------------ the parts ----
def parts_of(shape, diag: float) -> tuple[list, list]:
    """(solids, loose faces): every solid of the file, and - for a file of surfaces - its sheets
    sewn into shells, the closed ones made solids (an IGES cyclone of 51 trimmed surfaces is two
    closed bodies once sewn) and the open ones kept as faces whose free loops are capped."""
    solids = _solids(shape)
    if solids:
        return solids, []
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.BRepLib import BRepLib
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopoDS import TopoDS

    sew = BRepBuilderAPI_Sewing(max(1e-6 * diag, 1e-9))
    for f in _faces(shape):
        sew.Add(f)
    sew.Perform()
    sewn = sew.SewedShape()
    loose: list = []
    for sh in _explore(sewn, TopAbs_SHELL, TopoDS.Shell_s):
        if sh.Closed() or _free_edges(sh) == 0:
            mk = BRepBuilderAPI_MakeSolid(sh)
            if mk.IsDone():
                s = mk.Solid()
                BRepLib.OrientClosedSolid_s(s)
                solids.append(s)
                continue
        loose.extend(_faces(sh))
    if not solids and not loose:
        loose = _faces(sewn)
    return solids, loose


def _free_edges(shell) -> int:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shell, TopAbs_EDGE, TopAbs_FACE, m)
    return sum(1 for i in range(1, m.Extent() + 1) if m.FindFromIndex(i).Extent() == 1)


def touching_parts(solids: list, diag: float) -> bool:
    """Whether any two solids touch or overlap - an assembly whose parts' skins meet, so the skins
    together are no closed boundary of anything and only a split of space says where the fluid is.
    Read on the parts' own triangles (a point of one within a ten-thousandth of the part of a
    triangle of the other): exact distances between B-reps with threads take minutes."""
    import numpy as np
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from scipy.spatial import cKDTree

    if len(solids) < 2:
        return False
    lin = diag / 1000.0
    tol = 1e-4 * diag + lin
    meshes = []
    for s in solids:
        BRepMesh_IncrementalMesh(s, lin, False, 0.5, True)
        t = np.asarray([x for f in _faces(s) for x in _face_triangles(f)], dtype=float).reshape(-1, 3, 3)
        meshes.append(t)
    boxes = [(t.reshape(-1, 3).min(axis=0) - tol, t.reshape(-1, 3).max(axis=0) + tol) if len(t) else None
             for t in meshes]
    for i in range(len(solids)):
        for j in range(len(solids)):
            box_j = boxes[j]
            if i == j or boxes[i] is None or box_j is None:
                continue
            lo, hi = box_j
            pts = np.unique(meshes[i].reshape(-1, 3), axis=0)
            pts = pts[np.all((pts >= lo) & (pts <= hi), axis=1)]
            if not len(pts):
                continue
            T = meshes[j]
            tree = cKDTree(T.mean(axis=1))
            _d, idx = tree.query(pts, k=min(8, len(T)))
            idx = np.asarray(idx).reshape(len(pts), -1)
            for c in range(idx.shape[1]):
                if float(np.min(_point_triangle_distance(pts, T[idx[:, c]]))) <= tol:
                    return True
    return False


def _point_triangle_distance(p, T):
    """Distance from each point to its triangle (n x 3 and n x 3 x 3), exact."""
    import numpy as np
    a, b, c = T[:, 0], T[:, 1], T[:, 2]
    n = np.cross(b - a, c - a)
    nn = np.linalg.norm(n, axis=1).clip(1e-300)
    n = n / nn[:, None]
    d = np.einsum("ij,ij->i", p - a, n)
    q = p - d[:, None] * n
    inside = np.ones(len(p), dtype=bool)
    for u, v in ((a, b), (b, c), (c, a)):
        inside &= np.einsum("ij,ij->i", np.cross(v - u, q - u), n) >= 0
    best = np.where(inside, np.abs(d), np.inf)
    for u, v in ((a, b), (b, c), (c, a)):
        e = v - u
        t = (np.einsum("ij,ij->i", p - u, e) / np.einsum("ij,ij->i", e, e).clip(1e-300)).clip(0.0, 1.0)
        best = np.minimum(best, np.linalg.norm(p - (u + t[:, None] * e), axis=1))
    return best


# ---------------------------------------------------------------------------- the caps ----
@dataclass
class _Cap:
    face: object
    area: float
    centroid: tuple[float, float, float]
    normal: tuple[float, float, float]
    #: the parts whose flat faces rim this hole: two when it is where two parts meet face to face
    #: (a flange bolted to a flange) - a JOINT, never where the fluid meets the open air
    hosts: set = field(default_factory=set)


def candidate_caps(solids: list, loose: list, diag: float) -> list[_Cap]:
    """Every place the space inside the parts may meet the outside, as a flat face across it: each
    hole a flat face of a part rims (its inner wire: a flange's bore, a pipe's open end, a bolt hole),
    and each closed free loop of an open sheet that lies flat. A cap that turns out to lie inside the
    fluid (two flanges bolted face to face rim the same bore) or between two pieces of metal is
    harmless: the split keeps the fluid on both sides of it as one passage."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    caps: list[_Cap] = []
    tol = CAP_SAME * diag
    min_area = math.pi * (0.5 * MIN_CAP * diag) ** 2

    def add(face, host: int) -> None:
        area, c = _surface(face)
        if area < min_area:
            return
        n = _plane_normal(face, diag) or (0.0, 0.0, 1.0)
        for q in caps:
            if (math.dist(q.centroid, c) <= tol and abs(q.area - area) <= CAP_SAME_AREA * area
                    and abs(sum(a * b for a, b in zip(q.normal, n))) > 0.999):
                q.hosts.add(host)
                return
        caps.append(_Cap(face, area, c, n, {host}))

    def from_wire(pln, wire, host: int) -> None:
        for w in (wire, TopoDS.Wire_s(wire.Reversed())):
            mk = BRepBuilderAPI_MakeFace(pln, w, True)
            if mk.IsDone() and _surface(mk.Face())[0] > 0:
                add(mk.Face(), host)
                return

    hosts = [(k, f) for k, s in enumerate(solids) for f in _faces(s)] + [(-1, f) for f in loose]
    for k, host in hosts:
        wires = _explore(host, TopAbs_WIRE, TopoDS.Wire_s)
        if len(wires) < 2:
            continue
        pln = _face_plane(host, diag)
        if pln is None:
            continue
        outer = BRepTools.OuterWire_s(host)
        for w in wires:
            if not w.IsSame(outer):
                from_wire(pln, w, k)
    if loose:
        from OCP.BRep import BRep_Builder
        from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
        from OCP.TopoDS import TopoDS_Compound
        comp = TopoDS_Compound()
        bld = BRep_Builder()
        bld.MakeCompound(comp)
        for f in loose:
            bld.Add(comp, f)
        free = ShapeAnalysis_FreeBounds(comp, max(1e-6 * diag, 1e-9), False, False)
        for w in _explore(free.GetClosedWires(), TopAbs_WIRE, TopoDS.Wire_s):
            mk = BRepBuilderAPI_MakeFace(w, True)
            if mk.IsDone():
                add(mk.Face(), -1)
    return caps


# --------------------------------------------------------------------------- the split ----
def extract(shape, *, max_faces: int = MAX_FACES, extra_caps: list | None = None,
            fuzzy: float = FUZZY) -> Extraction:
    """The fluid the parts of `shape` (metres) hold. Never raises for geometry it cannot split:
    the reason is said in the result."""
    t0 = time.time()
    lo, hi = _real_box(shape)
    diag = math.dist(lo, hi)
    if not (diag > 0 and math.isfinite(diag)):
        return Extraction([], 0, 0, 0, time.time() - t0, reason="the parts have no size")
    solids, loose = parts_of(shape, diag)
    n_faces = sum(len(_faces(s)) for s in solids) + len(loose)
    if n_faces > max_faces:
        return Extraction([], len(solids), 0, 0, time.time() - t0, diag=diag,
                          reason=f"{n_faces} faces: too many to split the space quickly")
    if not solids and not loose:
        return Extraction([], 0, 0, 0, time.time() - t0, diag=diag, reason="no faces to bound a fluid")
    caps = candidate_caps(solids, loose, diag) + list(extra_caps or [])
    try:
        found = _split(solids, loose, caps, diag, lo, hi, fuzzy)
    except ExtractionError as exc:
        return Extraction([], len(solids) or len(loose), len(caps), 0, time.time() - t0, diag=diag,
                          reason=str(exc))
    passages, pockets, n_regions, leaks = found
    out = Extraction(passages, len(solids) or len(loose), len(caps), n_regions, time.time() - t0,
                     pockets=pockets, diag=diag)
    out.leaks = leaks
    if not passages:
        out.reason = ("the space inside the parts reaches the outside other than through a flat "
                      "opening, or there is no space inside them")
        if out.leaks:
            out.reason += " - it leaks beyond the joint at " + ", ".join(
                "(" + ", ".join(f"{v * 1000:.0f}" for v in c) + ") mm" for c in out.leaks[:3])
    logger.info("fluid volume: %d part(s), %d cap(s), %d region(s), %d passage(s) in %.1f s",
                out.parts, out.caps, n_regions, len(passages), out.seconds)
    return out


def _split(solids, loose, caps, diag, lo, hi, fuzzy=FUZZY):
    from OCP.BOPAlgo import BOPAlgo_MakerVolume
    from OCP.BRep import BRep_Builder
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.TopTools import (
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
        TopTools_ListOfShape,
    )
    parts = TopoDS_Compound()
    bp = BRep_Builder()
    bp.MakeCompound(parts)
    for x in [*solids, *loose]:
        bp.Add(parts, x)
    mv = BOPAlgo_MakerVolume()
    args = TopTools_ListOfShape()
    for s in [*solids, *loose, *(c.face for c in caps)]:
        args.Append(s)
    mv.SetArguments(args)
    mv.SetRunParallel(True)
    mv.SetIntersect(True)
    mv.SetFuzzyValue(fuzzy * diag)
    mv.Perform()
    if mv.HasErrors():
        raise ExtractionError("the parts could not be split into regions of space (OpenCASCADE's "
                              "volume maker failed on them)")
    result = mv.Shape()
    regions = _solids(result)
    if not regions:
        raise ExtractionError("the parts close no region of space")
    vols = [_volume(r) for r in regions]

    # THE OUTSIDE. OpenCASCADE drops the regions touching its own box round the arguments; one it
    # could not (an inverted shell holding everything) contains a point beyond every part.
    far = gp_Pnt(*(h + 0.05 * diag for h in hi))
    outside = set()
    for i, r in enumerate(regions):
        if vols[i] < 0 or len(_explore(r, TopAbs_SHELL, TopoDS.Shell_s)) > 1:
            cl = BRepClass3d_SolidClassifier(r)
            cl.Perform(far, 1e-9)
            if vols[i] < 0 or cl.State() == TopAbs_IN:
                outside.add(i)
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(result, TopAbs_FACE, TopAbs_SOLID, owners)
    # A SHELL NO REGION FILLS. A region of several shells is its outer shell less the spaces its
    # other shells close - and each of those spaces is a region of its own. Now and then the
    # volume maker hands a closed space to the wrong solid as one of its shells (a static mixer's
    # Y-piece bore came back as a shell of the outside): such a shell is made a region here, and
    # its faces are owned by it instead.
    moved: dict[tuple[int, int], int] = {}
    for i in range(len(regions)):
        shells = _explore(regions[i], TopAbs_SHELL, TopoDS.Shell_s)
        if len(shells) < 2:
            continue
        closed = [(sh, _shell_solid(sh)) for sh in shells]
        closed = [(sh, so, abs(_volume(so))) for sh, so in closed if so is not None]
        if len(closed) < 2:
            continue
        closed.sort(key=lambda t: -t[2])
        for sh, so, _v in closed[1:]:
            pts = _inside_points(so, diag, 1)
            if not pts or _filled_by(pts[0], regions, skip=i):
                continue
            j = len(regions)
            regions.append(so)
            vols.append(abs(_volume(so)))
            for f in _faces(sh):
                fi = owners.FindIndex(f)
                if fi:
                    moved[(fi, i)] = j
    rmap = TopTools_IndexedMapOfShape()
    for r in regions:
        rmap.Add(r)

    # METAL OR FLUID: a point inside each region, inside one of the parts or not. An open sheet
    # bounds no metal: every region of a surface model is space.
    part_cls = [(BRepClass3d_SolidClassifier(s), _box(s)) for s in solids]

    def in_metal(p) -> bool:
        for cl, (a, b) in part_cls:
            if any(p[k] < a[k] - 1e-9 or p[k] > b[k] + 1e-9 for k in range(3)):
                continue
            cl.Perform(gp_Pnt(*p), 1e-9)
            if cl.State() == TopAbs_IN:
                return True
        return False

    kind = []
    for i, r in enumerate(regions):
        if i in outside:
            kind.append("outside")
            continue
        votes = [in_metal(p) for p in _inside_points(r, diag, 3)]
        if not votes:
            kind.append("unknown")
        else:
            kind.append("metal" if sum(votes) * 2 > len(votes) else "space")

    # WHICH CAP EACH FACE OF THE SPLIT CAME FROM (its images), and who owns each face
    cap_of: dict[int, int] = {}
    for k, c in enumerate(caps):
        images = mv.Modified(c.face)
        pieces = list(images) if images.Size() else ([] if mv.IsDeleted(c.face) else [c.face])
        for f in pieces:
            fi = owners.FindIndex(f)
            if fi:
                cap_of[fi] = k

    def owner_ids(fi: int) -> list[int]:
        ids = [rmap.FindIndex(s) - 1 for s in owners.FindFromIndex(fi)]
        return [moved.get((fi, o), o) for o in ids]

    parent = list(range(len(regions)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    port_faces: dict[int, list[tuple[int, int]]] = {}   # space region -> [(face index, cap)]
    for fi in range(1, owners.Extent() + 1):
        own = owner_ids(fi)
        space = [o for o in own if kind[o] == "space"]
        if len(space) == 2:
            parent[find(space[0])] = find(space[1])          # a cap inside the fluid: one passage
        elif len(space) == 1 and all(kind[o] == "outside" for o in own if o != space[0]):
            if fi in cap_of:
                port_faces.setdefault(space[0], []).append((fi, cap_of[fi]))
            else:
                # space meeting the outside through no cap: a sheet's edge or a gap - the passage leaks
                port_faces.setdefault(space[0], []).append((fi, -1))
    groups: dict[int, list[int]] = {}
    for i, k in enumerate(kind):
        if k == "space":
            groups.setdefault(find(i), []).append(i)

    passages: list[Passage] = []
    for members in groups.values():
        vol = sum(abs(vols[m]) for m in members)
        faces = [pf for m in members for pf in port_faces.get(m, [])]
        if any(c < 0 for _fi, c in faces):
            continue                                        # it leaks: not a closed passage
        p = Passage(regions=members, volume=vol)
        by_cap: dict[int, list] = {}
        for fi, c in faces:
            by_cap.setdefault(c, []).append(fi)
        for c, fis in by_cap.items():
            holder = next(o for o in owner_ids(fis[0]) if o in members)
            p.ports.append(_port(c, caps[c], [TopoDS.Face_s(owners.FindKey(fi)) for fi in fis],
                                 regions[holder], lo, hi, diag, parts))
        passages.append(p)
    passages.sort(key=lambda p: -p.volume)
    keep, pockets = [], []
    # A PORT AT A JOINT (a hole two parts rim face to face) has the outside beyond it only because
    # the part on its other side reaches the outside some other way - a gap, a slit, a hole in a
    # curved wall: the passage is not closed, and where it leaks is said
    leaks: list = []
    for p in list(passages):
        joint = [o for o in p.ports if len({h for h in caps[o.cap].hosts if h >= 0}) >= 2]
        if joint:
            passages.remove(p)
            leaks.extend(o.centroid for o in joint)
    biggest = passages[0].volume if passages else 0.0
    for p in passages:
        if p.ports and p.volume >= MIN_PASSAGE_SHARE * biggest:
            keep.append(p)
        else:
            pockets.append((p.volume, len(p.ports)))
    for p in keep:
        p.boundary = _boundary(p, regions, owners, owner_ids)
        p.bbox = _box_of_faces(p.boundary)
        p.seed, p.seed_clearance = _seed(p, regions)
    return keep, pockets, len(regions), leaks


def _shell_solid(shell):
    """The closed space a shell bounds, as a solid facing out (None when it bounds none)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepLib import BRepLib
    try:
        mk = BRepBuilderAPI_MakeSolid(shell)
        if not mk.IsDone():
            return None
        so = mk.Solid()
        BRepLib.OrientClosedSolid_s(so)
        return so
    except Exception:  # noqa: BLE001 - an open or broken shell bounds nothing
        return None


def _filled_by(p, regions: list, skip: int) -> bool:
    """Whether some region (other than `skip`) holds the point."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN
    for j, r in enumerate(regions):
        if j == skip:
            continue
        lo, hi = _box(r)
        if any(p[k] < lo[k] or p[k] > hi[k] for k in range(3)):
            continue
        cl = BRepClass3d_SolidClassifier(r)
        cl.Perform(gp_Pnt(*p), 1e-12)
        if cl.State() == TopAbs_IN:
            return True
    return False


def _inside_points(region, diag: float, want: int) -> list[tuple[float, float, float]]:
    """Points strictly inside a region of space: a point on one of its faces, stepped in against
    the face's outward normal and checked by the classifier - from several faces, so one sliver of
    a face does not decide what the region is."""
    from OCP.BOPTools import BOPTools_AlgoTools3D
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec
    from OCP.IntTools import IntTools_Context
    from OCP.TopAbs import TopAbs_IN, TopAbs_REVERSED

    cl = BRepClass3d_SolidClassifier(region)
    ctx = IntTools_Context()
    out: list = []
    faces = sorted(_faces(region), key=lambda f: -_surface(f)[0])
    for f in faces[:12]:
        p, uv = gp_Pnt(), gp_Pnt2d()
        if BOPTools_AlgoTools3D.PointInFace_s(f, p, uv, ctx) != 0:
            continue
        ad = BRepAdaptor_Surface(f)
        q, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        ad.D1(uv.X(), uv.Y(), q, du, dv)
        n = du.Crossed(dv)
        if n.Magnitude() < 1e-30:
            continue
        n.Normalize()
        if f.Orientation() == TopAbs_REVERSED:
            n.Reverse()
        for eps in (1e-5, 1e-4, 1e-6, 1e-3):
            s = eps * diag
            x = (p.X() - n.X() * s, p.Y() - n.Y() * s, p.Z() - n.Z() * s)
            cl.Perform(gp_Pnt(*x), 1e-12)
            if cl.State() == TopAbs_IN:
                out.append(x)
                break
        if len(out) >= want:
            break
    return out


def _port(k: int, cap: _Cap, faces: list, region, lo, hi, diag, parts) -> Port:
    """One opening: the cap's pieces bounding the fluid, measured, facing out of the fluid."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer

    area, cx = 0.0, [0.0, 0.0, 0.0]
    for f in faces:
        a, c = _surface(f)
        area += a
        for i in range(3):
            cx[i] += a * c[i]
    centroid = tuple(v / area for v in cx) if area > 0 else cap.centroid
    # OUT OF THE FLUID: the plane's normal turned the way the fluid region holds the face (a
    # solid's faces point out of it)
    normal = cap.normal
    pln = _face_plane(faces[0], diag)
    e = TopExp_Explorer(region, TopAbs_FACE)
    while e.More() and pln is not None:
        f = e.Current()
        e.Next()
        if f.IsSame(faces[0]):
            d = pln.Axis().Direction()
            n = (d.X(), d.Y(), d.Z())
            normal = tuple(-v for v in n) if f.Orientation() == TopAbs_REVERSED else n
            break
    hole, wh = _hole(faces, area)
    steps = [((hi[i] if normal[i] > 0 else lo[i]) - centroid[i]) / normal[i]
             for i in range(3) if abs(normal[i]) > 1e-6]
    t_edge = min(steps) if steps else 0.0
    return Port(cap=k, faces=faces, area=area, centroid=centroid, normal=normal,  # type: ignore[arg-type]
                hole=max(hole, area), wh=wh, on_extremity=t_edge <= 0.03 * diag,
                clear_ahead=not _meets(parts, centroid, normal, 1e-4 * diag))


def _meets(parts, origin, direction, skip: float) -> bool:
    """Whether a ray from `origin` along `direction` meets any of the parts beyond `skip`."""
    from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

    if parts is None:
        return False
    ray = BRepIntCurveSurface_Inter()
    ray.Init(parts, gp_Lin(gp_Pnt(*origin), gp_Dir(*direction)), 1e-9)
    while ray.More():
        if ray.W() > skip:
            return True
        ray.Next()
    return False


def _hole(faces: list, area: float) -> tuple[float, tuple[float, float]]:
    """The area the opening's rim encloses - its faces plus whatever stands in them (a stem's end
    inside a ring of fluid: the face's inner wires) - and the sides of its outer rim (the minimum
    rectangle round it, shortest first)."""
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.cad_tessellate import _rim_measure
    hole = area
    wh: tuple[float, float] = (0.0, 0.0)
    best = -1.0
    for f in faces:
        outer = BRepTools.OuterWire_s(f)
        for w in _explore(f, TopAbs_WIRE, TopoDS.Wire_s):
            pts = _wire_points(w)
            m = _rim_measure(pts) if len(pts) >= 3 else None
            if m is None:
                continue
            if w.IsSame(outer):
                if m["area_m2"] > best:
                    best = float(m["area_m2"])
                    wh = (float(m["wh_m"][0]), float(m["wh_m"][1]))
            else:
                hole += float(m["area_m2"])
    return hole, wh


def _wire_points(wire, per_edge: int = 24) -> list[tuple[float, float, float]]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopAbs import TopAbs_REVERSED

    pts: list = []
    we = BRepTools_WireExplorer(wire)
    while we.More():
        edge = we.Current()
        c = BRepAdaptor_Curve(edge)
        a, b = c.FirstParameter(), c.LastParameter()
        seq = []
        for k in range(per_edge):
            p = c.Value(a + (b - a) * k / per_edge)
            seq.append((p.X(), p.Y(), p.Z()))
        if edge.Orientation() == TopAbs_REVERSED:
            p = c.Value(b)
            seq = [(p.X(), p.Y(), p.Z())] + seq[1:][::-1]
        pts.extend(seq)
        we.Next()
    return pts


def _boundary(p: Passage, regions, owners, owner_ids) -> list:
    """The faces that bound the passage, each as its region holds it (pointing out of the fluid):
    every face of its regions but those two of its own regions share."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
    members = set(p.regions)
    out = []
    for m in p.regions:
        e = TopExp_Explorer(regions[m], TopAbs_FACE)
        while e.More():
            f = e.Current()
            e.Next()
            fi = owners.FindIndex(f)
            inside = owner_ids(fi) if fi else []
            if sum(1 for o in inside if o in members) >= 2:
                continue
            out.append(TopoDS.Face_s(f))
    return out


def _box_of_faces(faces: list):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for f in faces:
        b.Add(comp, f)
    return _box(comp)


def _seed(p: Passage, regions) -> tuple[tuple[float, float, float] | None, float]:
    """A point inside the passage, well clear of its faces: stepped in from each opening along its
    axis (and off the axis, for a mouth with a body standing in it), the clearest one kept."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopoDS import TopoDS_Compound

    cls = [BRepClass3d_SolidClassifier(regions[m]) for m in p.regions]
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for f in p.boundary:
        bld.Add(comp, f)

    def inside(x) -> bool:
        for cl in cls:
            cl.Perform(gp_Pnt(*x), 1e-12)
            if cl.State() == TopAbs_IN:
                return True
        return False

    best: tuple | None = None
    for port in sorted(p.ports, key=lambda q: -q.area):
        r = math.sqrt(max(port.hole, port.area) / math.pi)
        n = port.normal
        a = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = _unit(_cross(n, a))
        v = _cross(n, u)
        offsets = [(0.0, 0.0)] + [(f * s, 0.0) for f in (0.5, 0.8) for s in (1, -1)] \
            + [(0.0, f * s) for f in (0.5, 0.8) for s in (1, -1)]
        for depth in SEED_DEPTHS:
            for a_, b_ in offsets:
                x = tuple(port.centroid[k] - depth * r * n[k] + r * (a_ * u[k] + b_ * v[k]) for k in range(3))
                if not inside(x):
                    continue
                d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*x)).Vertex(), comp)
                clear = float(d.Value()) if d.IsDone() else 0.0
                if best is None or clear > best[1]:
                    best = (x, clear)
                if clear >= SEED_CLEAR * r:
                    return best[0], best[1]
    return (best[0], best[1]) if best else (None, 0.0)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(sum(v * v for v in a)) or 1.0
    return tuple(v / n for v in a)


# ------------------------------------------------------------------------ triangle files ----
#: A triangle file is split on a B-rep built on its triangles: past this many it is not tried.
MAX_TRIANGLES = 60000


def from_triangles(tris) -> tuple[object | None, str]:
    """A triangle surface (n x 3 x 3, metres) as OpenCASCADE solids - one per closed piece, its
    flat stretches merged into single planar faces so a flange's face is one face with its bore as
    an inner wire, as in a CAD file - or (None, why) when it cannot be: too many triangles, or no
    piece is closed."""
    import numpy as np
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeShapeOnMesh,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
    )
    from OCP.BRepLib import BRepLib
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Triangle, Poly_Triangulation
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    T = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if len(T) > MAX_TRIANGLES:
        return None, f"{len(T)} triangles: too many to split the space quickly"
    if len(T) < 4:
        return None, "too few triangles to bound anything"
    verts, faces = _weld(T)
    piece, closed = part_pieces(verts, faces)
    diag = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0)))
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    n_closed = 0
    for k in range(len(closed)):
        F = faces[piece == k]
        used, inv = np.unique(F, return_inverse=True)
        Fk = np.asarray(inv).reshape(-1, 3)
        tri = Poly_Triangulation(len(used), len(Fk), False)
        for i, v in enumerate(verts[used], start=1):
            tri.SetNode(i, gp_Pnt(float(v[0]), float(v[1]), float(v[2])))
        for i, (a, b, c) in enumerate(Fk, start=1):
            tri.SetTriangle(i, Poly_Triangle(int(a) + 1, int(b) + 1, int(c) + 1))
        mk = BRepBuilderAPI_MakeShapeOnMesh(tri)
        mk.Build()
        if not mk.IsDone():
            continue
        # the triangles of one piece share their edges already; a sewing pass joins any a file
        # wrote twice
        sew = BRepBuilderAPI_Sewing(max(1e-7 * diag, 1e-10))
        sew.Add(mk.Shape())
        sew.Perform()
        for sh in _explore(sew.SewedShape(), TopAbs_SHELL, TopoDS.Shell_s):
            if _free_edges(sh) == 0:
                ms = BRepBuilderAPI_MakeSolid(sh)
                if ms.IsDone():
                    so = ms.Solid()
                    BRepLib.OrientClosedSolid_s(so)
                    unify = ShapeUpgrade_UnifySameDomain(so, True, True, False)
                    unify.SetLinearTolerance(max(1e-6 * diag, 1e-10))
                    unify.SetAngularTolerance(1e-4)
                    unify.Build()
                    bld.Add(comp, unify.Shape())
                    n_closed += 1
                    continue
            bld.Add(comp, sh)
    if not n_closed:
        return None, "no piece of the surface is closed, so no part bounds a space"
    return comp, ""


# ------------------------------------------------------------------------------ staging ----
#: A declared opening is one of the extracted openings when its stated location lies within this
#: share of the opening's width of the opening's centre, and its stated size agrees within
#: MATCH_RATIO (the internal staging's own band, cad/internal_surface).
MATCH_REACH = 0.5
MATCH_RATIO = 2.0


def match_declared(x: Extraction, declared: list) -> dict | None:
    """{port name: (passage index, Port)} for every declared opening (declaration_targets'
    shape: name, area_m2, near_m), each its own extracted opening - or None when one is not: the
    declaration was made on another reading of the part, and the staging that reading uses stands."""
    ports = [d for d in (declared or []) if isinstance(d, dict) and d.get("name")]
    if not ports or not x.found:
        return None
    out: dict = {}
    taken: set = set()
    for d in sorted(ports, key=lambda q: str(q["name"])):
        near = d.get("near_m")
        if not near:
            return None
        best = None
        for pi, p in enumerate(x.passages):
            for o in p.ports:
                if (pi, o.cap) in taken:
                    continue
                dist = math.dist(o.centroid, near)
                if dist > MATCH_REACH * max(o.diameter, 1e-9):
                    continue
                a = d.get("area_m2")
                if a and not any(1.0 / MATCH_RATIO <= m / a <= MATCH_RATIO for m in (o.area, o.hole) if m > 0):
                    continue
                if best is None or dist < best[0]:
                    best = (dist, pi, o)
        if best is None:
            return None
        taken.add((best[1], best[2].cap))
        out[str(d["name"])] = (best[1], best[2])
    return out


def _face_triangles(face) -> list:
    """The face's triangles in world coordinates, wound the way the face faces."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        return []
    trsf = loc.Transformation()
    flip = face.Orientation() == TopAbs_REVERSED
    nodes = []
    for k in range(1, tri.NbNodes() + 1):
        q = tri.Node(k).Transformed(trsf)
        nodes.append((float(q.X()), float(q.Y()), float(q.Z())))
    out = []
    for k in range(1, tri.NbTriangles() + 1):
        a, b, c = tri.Triangle(k).Get()
        if flip:
            out.append((nodes[a - 1], nodes[c - 1], nodes[b - 1]))
        else:
            out.append((nodes[a - 1], nodes[b - 1], nodes[c - 1]))
    return out


def stage(x: Extraction, declared: list, out_dir, *, angular_deflection: float = 0.2,
          linear_deflection: float | None = None) -> dict | None:
    """The extracted fluid as the internal-flow staging every engine reads (the record the surface
    staging writes, cad/internal_surface.write_staged): the passages the declared openings lie on,
    the wall and one patch per declared opening under its own name, every other opening sealed into
    the wall, the checks that it is closed and named, and the point inside the fluid. None when the
    declaration is not this extraction's, or the surface does not come out closed."""
    from pathlib import Path

    import numpy as np
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import internal_surface as isf
    from meshpipeline.cad.cad_tessellate import seal_open_rims
    from meshpipeline.cad.occ_box import mesh_to_size

    matched = match_declared(x, declared)
    if matched is None:
        return None
    used = sorted({pi for pi, _o in matched.values()})
    main = x.passages[used[0]]
    if main.seed is None:
        return None
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for pi in used:
        for f in x.passages[pi].boundary:
            bld.Add(comp, f)
    mesh_to_size(comp, 1.0 / 2500.0, angular_deflection, linear_deflection=linear_deflection)
    label: dict = {}
    for name, (_pi, o) in matched.items():
        m = TopTools_IndexedMapOfShape()
        for f in o.faces:
            m.Add(f)
        label[name] = m
    tris: list = []
    names: list[str] = []
    sealed = []
    declared_caps = {(pi, o.cap) for pi, o in matched.values()}
    for pi in used:
        for o in x.passages[pi].ports:
            if (pi, o.cap) not in declared_caps:
                sealed.append({"area": round(o.area, 10), "centroid": [round(v, 6) for v in o.centroid],
                               "kind": "gap" if o.gap else "opening"})
        for f in x.passages[pi].boundary:
            name = next((n for n, m in label.items() if m.Contains(f)), "wall")
            t = _face_triangles(f)
            tris.extend(t)
            names.extend([name] * len(t))
    # the coordinates as the mesher reads them (binary STL is float32), points a hair apart joined
    T = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3).astype(np.float32).astype(np.float64)
    V, F, L = _weld_labelled(T, names)
    # THE RIM AUDIT every internal staging makes: a face the tessellator left without triangles
    # leaves a gap, sealed into the wall
    membranes, open_rims = seal_open_rims([tuple(map(tuple, t)) for t in V[F]])
    if membranes:
        T = np.concatenate([V[F], np.asarray(membranes, dtype=np.float64).reshape(-1, 3, 3)])
        V, F, L = _weld_labelled(T, [*L.tolist(), *(["wall"] * len(membranes))])
    if any(not np.any(L == n) for n in matched):
        return None                     # a declared opening with no triangles: not this fluid
    ports = {}
    for name, (_pi, o) in matched.items():
        op = isf._Opening(kind="cap", centroid=np.asarray(o.centroid, dtype=float),
                          normal=np.asarray(o.normal, dtype=float), area=float(o.area),
                          faces=np.flatnonzero(L == name), name=name, role="inlet")
        if o.filled > 0.0:
            op.inner = isf._Opening(kind="cap", centroid=np.asarray(o.centroid, dtype=float),
                                    normal=np.asarray(o.normal, dtype=float), area=float(o.filled))
        ports[name] = op
    facts = isf._verify(V, F, L, list(matched), "wall", is_lid=np.isin(L, list(matched)))
    if facts["open_edges"]:
        logger.warning("fluid volume: the extracted surface has %d open edge(s) - not staged",
                       facts["open_edges"])
        return None
    facts.update({"sealed": sealed, "open_rims": open_rims,
                  "fluid_volume": {**x.as_dict(), "staged_passages": len(used)}})
    # THE SEED, PROVEN ON WHAT THE MESHER READS: inside the welded float32 surface by its winding
    # number. Several passages carry one seed (the largest's): a seeded mesher meets the others in
    # the passage reading (cad/fluid_passages) and says so; cfMesh meshes each one.
    seed = np.asarray(main.seed, dtype=float)
    if float(isf._winding(V[F], seed[None, :])[0]) < 0.5:
        logger.warning("fluid volume: the seed is not inside the staged surface - not staged")
        return None
    staged = isf.StagedInternal(verts=V, faces=F, labels=L.tolist(), ports=ports, wall="wall",
                                seed=seed, facts=facts)
    rec = isf.write_staged(staged, Path(out_dir))
    rec["source"] = "fluid_volume"
    rec["sealed"]["open_rims"] = open_rims
    return rec


def _file_safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name)) or "patch"


# ------------------------------------------------------------------- a bounded child run ----
#: How long the geometry check waits for the split before it keeps its face-by-face reading, and
#: how long the staging waits before it stages the parts' skins as before (seconds).
CHECK_BUDGET_S = 120.0
STAGE_BUDGET_S = 600.0


def run_bounded(task: dict, work, timeout_s: float) -> dict:
    """Run one task (`propose` or `stage`, see _child) in a child process: a split that takes too
    long (thread on thread) or brings OpenCASCADE down leaves the caller standing, with the reason.
    Returns the child's JSON answer, or {"error": why}."""
    import json
    import subprocess  # noqa: S404 - our own module, run with our own interpreter
    import sys
    from pathlib import Path

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    spec, out = work / "fluid_volume_task.json", work / "fluid_volume_answer.json"
    out.unlink(missing_ok=True)
    spec.write_text(json.dumps(task))
    try:
        subprocess.run([sys.executable, "-m", "meshpipeline.cad.fluid_volume", str(spec), str(out)],  # noqa: S603
                       timeout=timeout_s, capture_output=True, check=False)
    except subprocess.TimeoutExpired:
        return {"error": f"finding the fluid inside the parts took over {timeout_s:.0f} s"}
    if not out.exists():
        return {"error": "finding the fluid inside the parts stopped without an answer"}
    try:
        return json.loads(out.read_text())
    except ValueError:
        return {"error": "finding the fluid inside the parts gave no readable answer"}


def stage_bounded(source, out_dir, *, declared: list, scale: float = 1.0, kind: str = "cad",
                  angular_deflection: float = 0.2, linear_deflection: float | None = None,
                  budget_s: float | None = None) -> dict | None:
    """The internal-flow staging of the fluid the parts of `source` hold (stage), made in a
    time-boxed child process; None - with the reason logged - when the declared openings are not
    this fluid's, the split fails or runs out of time: the caller stages the parts as before."""
    from pathlib import Path

    if not [d for d in (declared or []) if isinstance(d, dict) and d.get("near_m")]:
        return None
    out_dir = Path(out_dir)
    answer = run_bounded({"task": "stage", "source": str(source), "scale": float(scale), "kind": kind,
                          "declared": [{"name": d.get("name"), "area_m2": d.get("area_m2"),
                                        "near_m": list(d["near_m"]) if d.get("near_m") else None}
                                       for d in declared],
                          "out_dir": str(out_dir), "angular_deflection": angular_deflection,
                          "linear_deflection": linear_deflection},
                         out_dir.parent / f"{out_dir.name}_fluid_volume", budget_s or STAGE_BUDGET_S)
    rec = answer.get("staged")
    if not rec:
        why = answer.get("error") or (answer.get("extraction") or {}).get("reason")             or "the declared openings are not the openings of the fluid the parts hold"
        logger.info("fluid volume: not staged (%s) - the parts are staged as before", why)
        return None
    logger.info("fluid volume: staged the fluid the parts hold - %s",
                "; ".join(f"{n} {v['area'] * 1e6:.0f} mm2" for n, v in rec["openings"].items()))
    return rec


def _load(task: dict):
    """The parts the task names, in metres: a CAD file scaled by `scale`, or a metre STL."""
    from pathlib import Path

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    from OCP.IFSelect import IFSelect_RetDone

    src = Path(task["source"])
    if task.get("kind") == "triangles":
        from meshpipeline.cad.stl_io import read_stl_triangles
        shape, why = from_triangles(read_stl_triangles(src))
        if shape is None:
            raise ExtractionError(why)
        return shape
    from meshpipeline.cad.cad_tessellate import is_iges
    if is_iges(src):
        from OCP.IGESControl import IGESControl_Reader as Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as Reader
    reader = Reader()
    if reader.ReadFile(str(src)) != IFSelect_RetDone:
        raise ExtractionError(f"the file could not be read as CAD ({src.name})")
    reader.TransferRoots()
    shape = reader.OneShape()
    scale = float(task.get("scale") or 1.0)
    if scale != 1.0:
        tr = gp_Trsf()
        tr.SetScaleFactor(scale)
        shape = BRepBuilderAPI_Transform(shape, tr, True).Shape()
    return shape


def closed_pieces(tris) -> int:
    """How many parts of a triangle surface are closed (part_pieces): the parts of a triangle
    file, when it holds an assembly."""
    import numpy as np

    T = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if len(T) < 4:
        return 0
    verts, faces = _weld(T)
    _piece, closed = part_pieces(verts, faces)
    return int(closed.sum())


def _weld_labelled(T, names: list):
    """(points, faces, patch per face) of labelled triangles, welded as _weld does."""
    import numpy as np
    P = np.asarray(T, dtype=np.float64).reshape(-1, 3)
    V, F_all = _weld_keep(P)
    lab = np.asarray(names, dtype=object)
    ok = (F_all[:, 0] != F_all[:, 1]) & (F_all[:, 1] != F_all[:, 2]) & (F_all[:, 0] != F_all[:, 2])
    return V, F_all[ok], lab[ok]


def _weld_keep(P):
    """_weld, but every triangle kept (degenerate ones too), in order: the caller filters."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    U, inv = np.unique(P, axis=0, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    diag = float(np.linalg.norm(U.max(axis=0) - U.min(axis=0))) if len(U) else 0.0
    pairs = cKDTree(U).query_pairs(max(1e-9 * diag, 1e-15), output_type="ndarray")
    if len(pairs):
        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(U), len(U)))
        _n, lab = connected_components(g, directed=False)
        first = np.full(int(lab.max()) + 1, -1, dtype=np.int64)
        for k in range(len(U) - 1, -1, -1):
            first[lab[k]] = k
        keep = np.unique(first)
        remap = np.searchsorted(keep, first[lab])
        U, inv = U[keep], remap[inv]
    return U, inv.reshape(-1, 3)


def _weld(T):
    """(points, faces) of a triangle soup: points closer than a billionth of the part's size are one
    point (a file writes 2e-18 where it means 0), and nothing else is joined - two parts pressed face
    to face keep both their faces (open_ends.skin_faces drops them)."""
    import numpy as np
    U, F = _weld_keep(np.asarray(T, dtype=np.float64).reshape(-1, 3))
    ok = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    return U, F[ok]


def part_pieces(verts, faces):
    """(piece per face, closed per piece) of a triangle surface holding several parts, which may
    touch: two parts that meet along an edge (a flange on a flange, a lid on its can) share it with
    four triangles, and a part's own triangles are told apart there by walking round the edge - each
    triangle pairs with its neighbour across the wedge of metal they close (the radial-edge rule; of
    two coincident faces, the one closing the wedge before it comes first). A piece is closed when
    every edge of every triangle of it found its pair."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    F = np.asarray(faces, dtype=np.int64)
    V = np.asarray(verts, dtype=np.float64)
    nf = len(F)
    a = F.reshape(-1)
    b = F[:, [1, 2, 0]].reshape(-1)
    third = F[:, [2, 0, 1]].reshape(-1)
    key = np.c_[np.minimum(a, b), np.maximum(a, b)]
    _uniq, eid, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    eid = np.asarray(eid).reshape(-1)
    face = np.repeat(np.arange(nf), 3)
    order = np.argsort(eid, kind="stable")
    es = eid[order]
    starts = np.r_[0, np.flatnonzero(np.diff(es)) + 1, len(es)]
    rows: list[int] = []
    cols: list[int] = []
    paired = np.zeros(len(a), dtype=bool)          # per corner (face edge): found its pair
    normals = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    for s, e in zip(starts[:-1].tolist(), starts[1:].tolist()):
        h = order[s:e]
        k = len(h)
        if k == 2:
            rows.append(int(face[h[0]]))
            cols.append(int(face[h[1]]))
            paired[h] = True
            continue
        if k < 2 or k % 2:
            continue
        u, v = V[a[h[0]]], V[b[h[0]]]
        if a[h[0]] > b[h[0]]:
            u, v = v, u
        axis = v - u
        ln = float(np.linalg.norm(axis))
        if ln <= 0:
            continue
        axis = axis / ln
        d = V[third[h]] - u
        d = d - np.outer(d @ axis, axis)
        d /= np.linalg.norm(d, axis=1, keepdims=True).clip(1e-300)
        ref = d[0]
        side = np.cross(axis, ref)
        theta = np.mod(np.arctan2(d @ side, d @ ref), 2.0 * np.pi)
        n = normals[face[h]]
        t = np.sign(np.einsum("ij,ij->i", n, np.cross(axis, d)))
        ring = sorted(range(k), key=lambda i: (round(float(theta[i]), 9), -t[i]))
        # a wedge of metal lies between a triangle facing back (t < 0) and the next facing on (t > 0)
        start = next((j for j in range(k) if t[ring[j]] < 0 and t[ring[(j + 1) % k]] > 0), None)
        if start is None:
            continue
        for j in range(start, start + k, 2):
            i0, i1 = ring[j % k], ring[(j + 1) % k]
            if t[i0] < 0 < t[i1]:
                rows.append(int(face[h[i0]]))
                cols.append(int(face[h[i1]]))
                paired[h[i0]] = paired[h[i1]] = True
    g = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(nf, nf))
    _n, piece = connected_components(g, directed=False)
    n_pieces = int(piece.max()) + 1 if nf else 0
    open_face = ~paired.reshape(-1, 3).all(axis=1)
    closed = np.ones(n_pieces, dtype=bool)
    closed[np.unique(piece[open_face])] = False
    return piece, closed


def _child(task: dict) -> dict:
    try:
        shape = _load(task)
    except ExtractionError as exc:
        return {"found": False, "reason": str(exc)}
    # SEVERAL SOLIDS THAT DO NOT TOUCH (a pipe and a centre rod standing free in it) bound the
    # fluid with their skins as they are: the scout reads them and the staging stages them that way.
    # Parts that touch or overlap (or a file of surfaces) are split.
    solids = _solids(shape)
    lo, hi = _real_box(shape)
    if len(solids) >= 2 and not touching_parts(solids, math.dist(lo, hi)):
        skipped = {"found": False, "reason": "the parts do not touch: their own skins bound the fluid"}
        return {"staged": None, "extraction": skipped} if task.get("task") == "stage" else skipped
    x = extract(shape)
    if task.get("task") == "stage":
        rec = stage(x, task.get("declared") or [], task["out_dir"],
                    angular_deflection=float(task.get("angular_deflection") or 0.2),
                    linear_deflection=task.get("linear_deflection"))
        return {"staged": rec, "extraction": x.as_dict()}
    return x.as_dict()


def main(argv: list[str]) -> int:
    import json
    from pathlib import Path

    task = json.loads(Path(argv[0]).read_text())
    threads = int(task.get("threads") or 0)
    if threads > 0:
        from OCP.OSD import OSD_ThreadPool
        OSD_ThreadPool.DefaultPool_s(threads)
    answer = _child(task)
    Path(argv[1]).write_text(json.dumps(answer, default=str))
    return 0


__all__ = ["CHECK_BUDGET_S", "MAX_FACES", "MAX_TRIANGLES", "STAGE_BUDGET_S", "Extraction", "ExtractionError",
           "Passage", "Port", "candidate_caps", "closed_pieces", "part_pieces", "extract", "from_triangles", "match_declared", "parts_of",
           "run_bounded", "stage", "stage_bounded", "touching_parts"]


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
