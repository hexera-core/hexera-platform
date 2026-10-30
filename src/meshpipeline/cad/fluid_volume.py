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
#: ...but never more than this across in absolute terms, however large the part (metres).
#:
#: A SHARE OF THE DIAGONAL ALONE DOES NOT SCALE TO A COLD PLATE. On the eleven test parts the
#: diagonal is 5 to 38 mm, so the bar is 10 to 76 um and every channel clears it. On a realistic
#: 100 x 100 x 5 mm server cold plate the diagonal is 141 mm and the bar is 283 um, which discards
#: a 100 x 500 um channel mouth (252 um across) - and because every mouth goes the same way, the
#: whole extraction fails rather than one channel. The part gets larger and the channels do not:
#: that is what a cold plate is.
#:
#: 10 um because the finest passage in the corpus is 10 um (embedded_silicon_D9_10um), so the bar
#: has to sit below that to support it at any part size. What the relative rule rejects is
#: tessellation slivers and chamfers rather than anything a flow could pass through, and a passage
#: too small to matter is dropped later anyway by its share of the largest passage's volume.
MAX_CAP_FLOOR_M = 10e-6
#: How far off a cap to step when asking what is on each side of it, as a fraction of the CAP'S OWN
#: radius. Tied to the cap and never to the part: 3% of the pin-fin plate's 15.87 mm diagonal is
#: 0.48 mm, which is wider than the 0.3 mm of metal above its ceiling, so no part-relative
#: tolerance can see this at all.
CAP_SIDE_STEP = 0.25
#: How far the surface actually written may enclose from what the B-rep said the coolant is, as a
#: fraction, before it is refused. This is the orientation check that works for MORE THAN ONE
#: passage, where the sign of the total does not: a reversed shell is internally consistent and
#: just subtracts. Tessellation on a curved channel costs a fraction of a percent, measured at
#: 0.0007% on the wavy array and exact on the straight ones, so 2% is loose by two orders of
#: magnitude against that and tight by a whole passage against an inversion.
VOLUME_AGREES = 0.02
#: ...and how far EACH PASSAGE's own shell may be from its own volume.
#:
#: THE TOTAL CANNOT CARRY THIS CHECK, which is arithmetic rather than opinion. A reversed passage
#: subtracts its own volume instead of adding it, so the total is short by twice that passage - and
#: on a part with many passages, twice one of them is nothing against the bar:
#:
#:     passages   one reversed, error in the TOTAL    against VOLUME_AGREES of 2%
#:          5                   40.0%                 caught
#:         25                    8.0%                 caught
#:        100                    2.0%                 EXACTLY the bar
#:        198                    1.01%                passes
#:
#: The embedded-silicon array extracts as 198 channels and the Corintis baseline as 100, so the
#: two parts in the set with the most passages are the two the global check cannot protect. Each
#: shell is closed on its own, so each has its own sign and its own volume, and comparing them one
#: at a time is exact however many there are. The honest tessellation error measured on these
#: parts is 0.17%, so this bar has room to spare.
PASSAGE_AGREES = 0.02


def cap_floor_m(diag: float) -> float:
    """The smallest hole that may be a cap, across, for a part of this diagonal (metres)."""
    return min(MIN_CAP * diag, MAX_CAP_FLOOR_M)
#: Two candidate caps are one when their centres lie this share of the diagonal apart and their
#: areas agree within CAP_SAME_AREA (two flanges bolted face to face rim the same bore).
CAP_SAME = 1e-5
CAP_SAME_AREA = 0.01
#: A passage is the fluid of the part when it holds at least this share of the largest passage's
#: volume; smaller closed spaces are the bore of a bolt hole capped at both ends, a pocket between
#: a nut and its washer: left out.
MIN_PASSAGE_SHARE = 0.02
#: ...AND A SHARE OF THE VOLUME IS THE WRONG AXIS FOR A BORE, which is why that rule lets one
#: through. A clamping bore's volume grows with the bracket it is drilled in, so on a big enough
#: bracket it clears any share bar: measured, the 9.0 mm bore through the clamp bar of the
#: buried_mouths test part is 381.703 mm3 against 6241.896 mm3 of real coolant - 6.1%, three times
#: the bar above - and the sixteen bolt holes of the Class 1500 spool fixture come to 32.7% of its
#: bore. Raising the share bar cannot separate them either way round: a real second passage in the
#: customer assembly of solid_classifiers' own docstring is 2.9% of its coolant, and the spool's
#: bolt holes are 2.045%.
#:
#: WHAT DOES NOT GROW IS THE SHAPE. A duct is longer than it is wide; a clamping bore is not.
#: Measured as the volume over (the sum of its mouth areas x its widest mouth's diameter), which
#: for a straight round hole is half its length-to-diameter ratio. Over 21 reads:
#:
#:     clamp bore 0.33   bolt hole 0.68  | 1.0 |   buried_mouths coolant 3.31
#:     manifold_two_level 3.95   buried_ports 3.98   cold_plate_2ch 5.12   slab_2ch 10.85
#:     lidded_4ch 11.33   hotspot_array 12.66   ported_plate 13.60   lee_garimella 27.18
#:     murray_ported 27.56   two_scale_plate 40.04   tuckerman_pease 50.48
#:     unit_cell / corintis 50.92   embedded_silicon 80.90
#:
#: The two kinds of hole are a factor of five apart at the nearest, which is why one bar fits.
#:
#: ONLY ON A PASSAGE THAT IS NOT THE LARGEST, because a flange IS a short fat bore and is
#: legitimately the fluid: flange_bored_8bolts' own 100 mm bore through 20 mm of metal measures
#: 0.10, blunter than anything this rejects. "The largest passage is the fluid" is already what
#: MIN_PASSAGE_SHARE assumes; this only asks whether a SECOND one is a duct as well.
MIN_PASSAGE_SLENDERNESS = 1.0
#: What the thin side of a TWO-SCALE part's wall is written as, beside the coolant surface itself.
#: The mesher refines around it as a second surface, which is how a part whose passages come in two
#: very different sizes gets its cells spent on the small ones - see cad/scales and
#: cfmesh_conjugate.plan.fine_surfaces.
FINE_SURFACE_NAME = "fluid_fine.stl"
#: An opening of a passage is proposed as one when it is at least this share of the passage's
#: widest opening (the scout's own MIN_OPENING_FRACTION): a drain tap or a gauge port stays a
#: sealed wall unless the user makes it an opening.
MIN_PORT_SHARE = 0.02
#: How many B-rep faces the split is tried on, at most: beyond it the check keeps its face-by-face
#: reading (a full car body, a city block).
#: Raised from 12000 once the corpus was measured: the 16-channel wavy plate has 12358 faces and
#: extracts in 12.2 s, well inside CHECK_BUDGET_S, so the old limit refused a part it could do.
MAX_FACES = 20000
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
    #: THE OUTER SKIN WITH ITS MOUTHS CAPPED, as faces pointing out of the part: the boundary of
    #: every region of the split that is not OUTSIDE, which is metal and coolant together and is
    #: exactly the volume a conjugate mesher has to fill. Carried because the split already knows
    #: it - `kind[i]` says outside, metal or space for every region - and the alternative was a
    #: boolean union of the metal with each passage, which is wrong on the first customer
    #: assembly whatever glue mode it is given.
    domain_boundary: list = field(default_factory=list)

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
                # NINE PLACES, not four. A cold plate's coolant is 2.6 mm3 = 0.0000026 L, and
                # four places rounded every chip-scale passage to 0.0 - the check then told the
                # user their part held no fluid while holding a correct extraction of it.
                "volume_l": round(p.volume * 1000.0, 9),
                "volume_mm3": round(p.volume * 1e9, 4),
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
    from scipy.spatial import cKDTree

    from meshpipeline.cad.occ_box import incremental_mesh

    if len(solids) < 2:
        return False
    lin = diag / 1000.0
    tol = 1e-4 * diag + lin
    meshes = []
    for s in solids:
        incremental_mesh(s, lin, 0.5)
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


def solid_classifiers(solids: list) -> list:
    """One BRepClass3d_SolidClassifier per solid, built once and reused.

    BUILDING ONE IS NOT FREE and the cost is per SOLID, not per question: the classifier indexes
    the solid's faces when it is constructed. Measured on a customer's six-part cold plate, 9226
    faces: all six classifiers together take 0.21 s to build, and `metal_on_both_sides` used to
    build its own set per cap - 764 caps x 6 solids = 4584 constructions - which cost 291.0 s of a
    347.5 s extraction. Hoisted, the whole extraction is 141.5 s and the answer is the same on
    every number it reports: found, 6 parts, 58 caps kept of 764, 70 regions, 1 pocket left out,
    no leaks, two passages at 52144.5599 and 1517.227 mm3 with the same seed points.

    `_split` has always done it this way (`part_cls`, below); this is the same move at the other
    call site.
    """
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    return [BRepClass3d_SolidClassifier(s) for s in solids]


def metal_on_both_sides(solids: list, cap: _Cap, classifiers: list | None = None) -> bool:
    """Whether the cap has the part's own metal on BOTH sides of it, so it caps nothing.

    `classifiers`: the solids' classifiers when the caller already holds them (solid_classifiers).
    They are only ever read here, so sharing one set across every cap is sound and is what makes
    this affordable on an assembly; without it this is the single most expensive line in the whole
    extraction.

    A PIN'S TOP, exactly. The chamber's ceiling has an inner wire where each pin meets it, so a disc
    gets laid across it: above that disc is the lid's metal and below it is the pin's. Nothing can
    flow through it, and laying it there cuts the one chamber into pieces that then reach the
    outside past it.

    Both sides, and not either side, because either side would be wrong twice over. A real port has
    air on one side and fluid on the other. Two flanges bolted face to face rim the same bore and
    have fluid on both sides, and that cap has to stay (the split keeps the fluid on both sides of
    it as one passage). Only a cap with metal on both sides is a cap across solid material.

    MEASURED on the ten parts of the corpus that build as CAD, stepping CAP_SIDE_STEP of each cap's
    own radius along +normal and -normal and asking the solid classifier:

        part                               caps   on the box   metal both   free both
        pin_fin_300um_staggered             526           22          504          22
        embedded_silicon_D9_10um            396          396            0         396
        hotspot_array                        48           48            0          48
        manifold_two_level                   42           42            0          42
        murray_ported_4gen                   17           17            0          17
        unit_cell_100um                       2            2            0           2

    504 of 526 on the one part that fails, 0 of 505 on the five that do not, and the 504 are exactly
    the pin tops: the 22 that survive are the real ports, the ones on the bounding box.
    """
    import math

    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON

    radius = math.sqrt(max(float(cap.area), 0.0) / math.pi)
    step = CAP_SIDE_STEP * radius
    if step <= 0.0:
        return False
    classifiers = solid_classifiers(solids) if classifiers is None else classifiers
    for sign in (1.0, -1.0):
        point = gp_Pnt(*(cap.centroid[k] + sign * cap.normal[k] * step for k in range(3)))
        for cl in classifiers:
            cl.Perform(point, 1e-9)
            if cl.State() in (TopAbs_IN, TopAbs_ON):
                break
        else:
            return False                      # this side is free, so the cap caps something
    return True


#: Faces nearer than this to a shared plane are treated as lying on it, as a fraction of the part's
#: diagonal. The same figure the cap merge uses, for the same reason: a brazed joint's two sides are
#: drawn coincident and a tessellator's rounding is what separates them.
JOINT_PLANE = CAP_SAME
#: Planes carrying more faces than this are not fused. A fuse is the expensive operation in this
#: module and a plane with hundreds of faces on it is a lattice, not a joint; the per-face path
#: above already covers anything that is really a hole.
JOINT_MAX_FACES = 60


def _joint_caps(solids: list, diag: float, add) -> None:
    """Cap the holes enclosed by SEVERAL solids' coplanar faces together.

    Only where at least two different solids meet on the plane: one solid's own coplanar faces
    cannot enclose anything its own inner wires did not already enclose, so there would be nothing
    to find and the fuse would be paid for nothing.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    if len(solids) < 2:
        return
    tol = JOINT_PLANE * diag
    planes: dict = {}
    for k, solid in enumerate(solids):
        for f in _faces(solid):
            n = _plane_normal(f, diag)
            if n is None:
                continue
            _area, c = _surface(f)
            # the plane as (unit normal with a fixed sign, its distance from the origin), rounded
            # so two faces of the same joint land in the same bucket
            flip = -1.0 if (n[0], n[1], n[2]) < (0.0, 0.0, 0.0) else 1.0
            nn = tuple(round(flip * v, 6) for v in n)
            d = sum(nn[j] * c[j] for j in range(3))
            key = (*nn, round(d / tol) if tol > 0 else 0)
            planes.setdefault(key, []).append((k, f))
    for group in planes.values():
        hosts = {k for k, _f in group}
        if len(hosts) < 2 or len(group) > JOINT_MAX_FACES:
            continue
        fused = group[0][1]
        try:
            for _k, f in group[1:]:
                args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
                args.Append(fused)
                tools.Append(f)
                op = BRepAlgoAPI_Fuse()
                op.SetArguments(args)
                op.SetTools(tools)
                op.Build()
                if not op.IsDone():
                    raise RuntimeError("fuse failed")
                fused = op.Shape()
            # UNIFIED, OR THE HOLE NEVER APPEARS. A fuse of two coplanar faces keeps them as two
            # faces split along the edge they now share, so the groove mouth stays a notch on each
            # one's outer boundary and no inner wire exists to cap. Unifying merges them into one
            # face whose outer wire runs round the joint and whose INNER wires are the mouths. This
            # is the whole mechanism; without it the step above does nothing at all, which is how
            # it first behaved.
            from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
            uni = ShapeUpgrade_UnifySameDomain(fused, True, True, False)
            uni.Build()
            fused = uni.Shape()
        except Exception:  # noqa: BLE001 - a joint we cannot fuse is one we say nothing about
            continue
        for face in _faces_of(fused):
            wires = _explore(face, TopAbs_WIRE, TopoDS.Wire_s)
            if len(wires) < 2:
                continue
            pln = _face_plane(face, diag)
            if pln is None:
                continue
            outer = BRepTools.OuterWire_s(face)
            for w in wires:
                if w.IsSame(outer):
                    continue
                for cand in (w, TopoDS.Wire_s(w.Reversed())):
                    mk = BRepBuilderAPI_MakeFace(pln, cand, True)
                    if mk.IsDone() and _surface(mk.Face())[0] > 0:
                        # host -2: this cap belongs to the joint, not to any one solid, which is
                        # what the leak check reads to tell a joint from a bolted flange pair
                        add(mk.Face(), -2)
                        break


def _faces_of(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS
    return _explore(shape, TopAbs_FACE, TopoDS.Face_s)


def candidate_caps(solids: list, loose: list, diag: float) -> list[_Cap]:
    """Every place the space inside the parts may meet the outside, as a flat face across it: each
    hole a flat face of a part rims (its inner wire: a flange's bore, a pipe's open end, a bolt hole),
    and each closed free loop of an open sheet that lies flat. A cap that turns out to lie inside the
    fluid (two flanges bolted face to face rim the same bore) or between two pieces of metal is
    harmless: the split keeps the fluid on both sides of it as one passage.

    EXCEPT WHEN THERE ARE HUNDREDS OF THEM, which is why the pin-fin plate finds no coolant.
    Measured on the eleven-part corpus: it is the ONLY part with a cap that is not on the part's
    bounding box, and it has 504 of them.

        part                     caps   on the box   inside   coolant
        unit_cell_100um             2            2        0   found
        wavy_250um_16ch            32           32        0   found
        hotspot_array              48           48        0   found
        manifold_two_level         42           42        0   found
        murray_ported_4gen         17           17        0   found
        embedded_silicon_D9_10um  396          396        0   found
        pin_fin_300um_staggered   526           22      504   NOT FOUND

    Its coolant is one chamber with pins standing in it, and every pin's top is an inner wire of
    the chamber's ceiling face - so 504 discs 300.7 um across, facing z, get laid across the
    chamber, which cuts it into pieces that then reach the outside past them. The 22 real ports
    are the ones on the box, 586.3 um across and facing x.

    THE FIX IS NOT "DROP INTERIOR CAPS", quite: a recessed port is legitimately not on the box,
    and the flange-bolted-face-to-face case above needs interior caps to stay. The distinguishing
    fact is that the space just OUTSIDE a real opening is free, while stepping out of a pin's top
    along its own normal goes straight into the ceiling's metal. Measuring that per cap is the
    work, and a tolerance relative to the part's diagonal will not do it - 3% of this plate's
    15.87 mm diagonal is 0.48 mm, which is wider than the 0.3 mm of metal above its ceiling.

    SO IT IS MEASURED PER CAP, by `metal_on_both_sides`, whose own docstring carries the numbers:
    504 of the pin-fin plate's 526 caps go, 0 of the other five parts' 505 go, and what survives on
    the pin-fin plate is its 22 real ports.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    caps: list[_Cap] = []
    tol = CAP_SAME * diag
    min_area = math.pi * (0.5 * cap_floor_m(diag)) ** 2

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
    # A MOUTH THAT NO ONE FACE ENCLOSES. Above, a cap comes from an INNER wire of a single face -
    # a hole bored through one solid. A cold plate made the ordinary way has no such hole: the
    # channels are grooves milled into a base, so each mouth is a bite taken out of the base's end
    # face, and its fourth side is the LID's underside, which belongs to a different solid. Neither
    # face encloses anything, so nothing was capped and the part read as having no coolant at all.
    #
    # MEASURED, 2026-10-28, over six parts built for this: a base-and-lid plate is the only one of
    # them with ZERO faces carrying a hole (the others have 2, 6, 7, 7 and 9), and it is the only
    # one where the extraction found nothing - 0 mm3 against 2,100 declared. It is also the
    # commonest way a cold plate is actually made.
    #
    # THIS IS NOT THE COPLANAR RULE THE DEVLOG REFUTED. That one capped wherever two faces shared a
    # plane, which turned a finned heat sink into ten passages of room air. This fuses the coplanar
    # faces first and caps only a hole in the RESULT: on a heat sink the fin ends and the base fuse
    # into one region whose gaps are all on its outer boundary, so it yields nothing. The hole has
    # to be enclosed by the metal of the joint, which is what a real mouth is and a gap between
    # fins is not.
    _joint_caps(solids, diag, add)
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
    # LAST, and on the caps rather than on the wires, because a cap is only known to be across
    # solid metal once it has a plane, a centroid and a size: the step taken to ask is a quarter of
    # the cap's own radius.
    cls = solid_classifiers(solids) if solids else []
    kept = [c for c in caps if not (solids and metal_on_both_sides(solids, c, cls))]
    if len(kept) != len(caps):
        logger.info("fluid volume: %d of %d candidate caps have metal on both sides and cap "
                    "nothing (pin tops, ribs); %d kept", len(caps) - len(kept), len(caps),
                    len(kept))
    return kept


# --------------------------------------------------------------------------- the split ----
def extract(shape, *, max_faces: int = MAX_FACES, extra_caps: list | None = None,
            fuzzy: float = FUZZY, want_domain: bool = False) -> Extraction:
    """The fluid the parts of `shape` (metres) hold. Never raises for geometry it cannot split:
    the reason is said in the result.

    `want_domain` also walks out the boundary of metal-plus-coolant (`Extraction.domain_boundary`).
    OFF BY DEFAULT because it is not free and almost nothing wants it: the geometry CHECK asks for
    passages, caps and volumes, and it is the one caller with a wall-clock budget
    (CHECK_BUDGET_S). Only a build that is going to write the domain asks for it."""
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
        found = _split(solids, loose, caps, diag, lo, hi, fuzzy, want_domain=want_domain)
    except ExtractionError as exc:
        return Extraction([], len(solids) or len(loose), len(caps), 0, time.time() - t0, diag=diag,
                          reason=str(exc))
    passages, pockets, n_regions, leaks, domain = found
    out = Extraction(passages, len(solids) or len(loose), len(caps), n_regions, time.time() - t0,
                     pockets=pockets, diag=diag)
    out.leaks = leaks
    out.domain_boundary = domain
    if not passages:
        out.reason = ("the space inside the parts reaches the outside other than through a flat "
                      "opening, or there is no space inside them")
        if out.leaks:
            out.reason += " - it leaks beyond the joint at " + ", ".join(
                "(" + ", ".join(f"{v * 1000:.0f}" for v in c) + ") mm" for c in out.leaks[:3])
    logger.info("fluid volume: %d part(s), %d cap(s), %d region(s), %d passage(s) in %.1f s",
                out.parts, out.caps, n_regions, len(passages), out.seconds)
    return out


def _split(solids, loose, caps, diag, lo, hi, fuzzy=FUZZY, *, want_domain: bool = False):
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
        if p.ports and p.volume >= MIN_PASSAGE_SHARE * biggest and (
                p.volume >= biggest or _is_duct(p)):
            keep.append(p)
        else:
            pockets.append((p.volume, len(p.ports)))
    for p in keep:
        p.boundary = _boundary(p, regions, owners, owner_ids)
        p.bbox = _box_of_faces(p.boundary)
        p.seed, p.seed_clearance = _seed(p, regions)
    # EVERY REGION THAT IS NOT OUTSIDE, bounded the same way a passage is: a face two members
    # share is interior to the domain and dropped, and what is left is the part's outer skin with
    # the port mouths closed by their own caps. "unknown" counts as inside - it is a region the
    # classifier could find no point in, and it lies within the outer skin by construction, since
    # the outside regions are the ones holding a point beyond every part.
    # ONLY WHEN SOMETHING WILL USE IT. This walks every face of every region that is not outside,
    # where a passage walks only its own - on the first customer assembly that is the difference
    # between a split that fits CHECK_BUDGET_S and one that does not, and the check does not want
    # a domain at all.
    inside = _Members([i for i, k in enumerate(kind) if k != "outside"]) if want_domain else None
    domain = _boundary(inside, regions, owners, owner_ids) if (inside and inside.regions) else []
    return keep, pockets, len(regions), leaks, domain


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


class _Members:
    """Anything with a `regions` list is what `_boundary` needs, and the domain is not a Passage."""

    __slots__ = ("regions",)

    def __init__(self, regions: list[int]) -> None:
        self.regions = regions


def _boundary(p, regions, owners, owner_ids) -> list:
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


def _is_duct(p: Passage) -> bool:
    """Whether a passage is longer than it is wide, measured on its own mouths.

    A duct rather than a hole drilled through dry metal - see MIN_PASSAGE_SLENDERNESS for the
    measurement and for why a share of the volume cannot make this distinction.

    True when nothing could be measured, because saying nothing is the safe answer here: this rule
    only ever DROPS a passage, so a bad reading would lose real coolant.
    """
    area = sum(o.hole for o in p.ports)
    widest = max((o.diameter for o in p.ports), default=0.0)
    if area <= 0.0 or widest <= 0.0:
        return True
    return p.volume >= MIN_PASSAGE_SLENDERNESS * area * widest


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


def surface_volume(T) -> float:
    """The signed volume a closed triangle surface encloses (m3), positive when its triangles are
    wound so their normals point OUT.

    This is the writer's own orientation check and it is not decoration. `write_stl_binary` zeroes
    every stored facet normal, so the winding is the only record of which way a face points, and
    the mesher reads this surface with `surfaceToCell ... useSurfaceOrientation true` - a surface
    written inside out would tag the metal as coolant and the coolant as metal, with nothing
    failing. Comparing this against the extraction's own volume catches that.
    """
    import numpy as np
    A = np.asarray(T, dtype=np.float64).reshape(-1, 3, 3)
    return float(np.einsum("ij,ij->i", A[:, 0], np.cross(A[:, 1], A[:, 2])).sum() / 6.0)


def narrowest_passage_m(x: Extraction, stl_path, _field_out: list | None = None) -> float:
    """The narrowest coolant passage in the part, in metres (0.0 when it cannot be read).

    THE CORPUS DECLARES THIS AND A CUSTOMER WILL NOT. devtools/semiconductor/parts.py says of it
    "DECLARED, not measured: it cannot be read off a triangle soup, and it is what the cell size
    is derived from", and the cell size is what the whole mesh follows from. It is measurable now,
    because the coolant is extracted as real faces and written as a closed surface.

    TWO READINGS, AND THE SMALLER WINS, because each is blind where the other sees and both err
    LARGE - and large is the unsafe direction, since it makes every cell coarser than intended.

      the port mouths     exact for a prismatic channel, straight off the B-rep. Blind to a
                          channel that narrows INSIDE the part (the hotspot array's mouths are
                          120 um and its channels pinch to 60) and optimistic where a channel
                          meets the end face at a slant (the wavy array's mouths read 294.71 um
                          for a 250 um channel, which is 250/cos 32 degrees).
      the radius field    engines/passage reads the staged wall's inscribed radius, which sees a
                          pinch anywhere along the channel. Blind below its own sampling spacing,
                          a fraction of the part's diagonal: on the wavy plate that works out at
                          250 um - the channel width itself - so every statistic read 354 um,
                          which is the diagonal of the sampling cell rather than the channel.

    Measured against what the corpus declares, taking the smaller: unit_cell, the two-level
    manifold, the hotspot array and the embedded-silicon array all exact; murray_ported -0.9%;
    the wavy array +17.9%, which is the slant and which both readings share. So the declaration
    becomes the scorecard rather than an input, with one curved part read 18% coarse.
    """
    from pathlib import Path

    mouths = [min(p.wh) for passage in x.passages for p in passage.ports if min(p.wh) > 0.0]
    best = min(mouths) if mouths else 0.0
    try:
        from meshpipeline.engines.passage import passage_field_of_stls, radius_stats
        seed = x.passages[0].seed if x.passages else None
        ports = [tuple(p.centroid) for passage in x.passages for p in passage.main_ports]
        # THE SHARP EDGES ARE READ, and without them a third of a real plate is invisible.
        #
        # The field is read at the wall's own vertices, and a SHORT neck carries vertices on its
        # two rims alone - each on a 90-degree edge, where the chord reads the neighbour it opens
        # into rather than the neck. engines/passage already carries the answer for exactly this
        # ("an orifice plate's bore ... venturi_orifice_007's 26 mm orifice read as its 53 mm pipe
        # at every point and was sized for the pipe"), and this call was not asking for it.
        #
        # MEASURED on a Corintis cold plate, 2026-10-27. Without the sharp edges the narrowest
        # passage read 2.6525 mm; with them, 0.0980 mm. The mesh built on the first number came
        # out with its main passage cut in two and 223 one-cell specks beside it, because 33.0%
        # of that coolant's wall area is thinner than the 0.53 mm cell the first number chose.
        # Confirmed independently by ray-casting every one of its 52,334 triangles into the fluid
        # and taking the far wall: area-weighted p1 0.0979 mm against the sharp-edge reading's
        # 0.0983. Two methods that share no code, agreeing to three decimals.
        #
        # THE MEAN HIDES IT COMPLETELY, which is why this was not caught by eye. The part is a
        # manifold of millimetre channels bonded to a microfluidic layer, so 4V/A over the whole
        # coolant is 3.5 mm: the thin layer is 2% of the volume spread over a third of the area.
        # A mean is the wrong statistic for a cell size, which has to suit the smallest thing the
        # mesh must hold together.
        field = passage_field_of_stls([Path(stl_path)], interior_point=seed,
                                      port_centroids=ports, read_sharp_edges=True)
        # HANDED BACK, because the scale split needs the same field and this is the expensive read
        # in the module. `_field_out` is a list the caller passes in to receive it; callers that do
        # not care pass nothing and nothing changes for them.
        if _field_out is not None and field is not None:
            _field_out.append(field)
        stats = radius_stats(field[2]) if field is not None else {}
        across = 2.0 * float(stats.get("min") or 0.0)
    except Exception as exc:                 # noqa: BLE001 - the mouths still stand on their own
        # AT WARNING, NOT INFO. The field reads through pyvista and scipy and can genuinely fail
        # on odd geometry, which is why this is caught at all - but the first thing it caught was
        # a NameError in this very function, and at info level that sat silently behind a reading
        # that was 100% too coarse on the hotspot array. A fallback that hides a bug is worse than
        # no fallback.
        logger.warning("fluid volume: the radius field did not read (%s: %s); the narrowest "
                       "passage is the mouths' own, which cannot see a channel that pinches "
                       "inside the part", type(exc).__name__, str(exc)[:160])
        across = 0.0
    if across > 0.0:
        best = min(best, across) if best > 0.0 else across
    return float(best)


def first_passage_wound_in(triangles, spans, volumes) -> tuple[int, float, float] | None:
    """The first passage whose own shell does not enclose its own volume, or None when all do.

    `spans` is (first triangle, how many) per passage, in the order the triangles were written, and
    `volumes` what the B-rep says each passage holds. Returns (which, what its shell encloses, what
    it should) for the first one that disagrees.

    SIGNED, so a reversed shell is caught by its sign alone whatever its size: a passage written
    inside out encloses MINUS its volume. The tolerance is only for a shell the tessellator left
    slightly short.
    """
    import numpy as np

    T = np.asarray(triangles, dtype=np.float64)
    for k, (start, count) in enumerate(spans):
        if count <= 0:
            continue
        got = surface_volume(T[start:start + count])
        want = float(volumes[k]) if k < len(volumes) else 0.0
        if got <= 0.0:
            return k, got, want
        if want > 0.0 and abs(got - want) > PASSAGE_AGREES * want:
            return k, got, want
    return None


def _scales_and_fine_surface(out_path, field) -> tuple[dict | None, dict | None]:
    """The part's scale split, and its thin side written beside the coolant surface.

    (None, None) when the part has ONE scale, which is the ordinary case and not a failure: of ten
    parts in this project's corpus read on 2026-10-28, every one came back single-scaled and only
    the first real customer cold plate did not. A caller that gets None sizes the part the way it
    always has - from its narrowest passage, everywhere.

    Never raises. The split reads through pyvista and numpy and can genuinely fail on odd geometry,
    and one scale is the safe answer: it over-resolves the broad passages, which is expensive,
    where a wrong split would under-resolve the narrow ones, which is wrong.
    """
    if field is None:
        return None, None
    try:
        # pathlib is imported per function in this module, and this one needs it: without the
        # import the line below raises NameError, which no test caught because every test part
        # reads as ONE scale and returns above this point.
        from pathlib import Path

        from meshpipeline.cad import scales as SC

        got = SC.scales_of_field(field[0], field[1], field[2])
        if got is None:
            return None, None
        fine = SC.write_fine_surface(field[0], field[1], field[2], split_m=got["split_m"],
                                     out_path=Path(out_path).with_name(FINE_SURFACE_NAME))
        return got, (fine or None)
    except Exception as exc:                 # noqa: BLE001 - one scale is the safe answer
        logger.warning("fluid volume: the scale split did not read (%s: %s); the part will be "
                       "sized from its narrowest passage everywhere, which is right for a part "
                       "with one scale and expensive for one with two",
                       type(exc).__name__, str(exc)[:160])
        return None, None


def write_surface(x: Extraction, out_path, *, angular_deflection: float = 0.2,
                  linear_deflection: float | None = None) -> dict | None:
    """The coolant's whole boundary as ONE closed binary STL, in metres, with no patch names.

    WHAT A CONJUGATE CASE NEEDS OF THE COOLANT, and no more. It takes its inlet and outlet from the
    BLOCK's six named faces (engines/cfmesh/conjugate.block_surface_stl) and uses this surface only
    to refine towards the coolant and then to tag the coolant cells (surfaceMeshRefinement, then
    surfaceToCell with useSurfaceOrientation). So nothing here is declared, matched or labelled -
    which is the whole reason a single uploaded solid can be read at all, since `stage` refuses
    without declared openings and a cold plate arrives with none.

    EVERY passage is written, not the largest. An embedded-silicon array extracts as 198 separate
    channels and all 198 are coolant; there is nothing in the geometry that would make one of them
    something else.

    None, with the reason logged, when the surface does not come out closed or comes out inside
    out. An open edge would let surfaceToCell flood the whole block instead of the channels.
    """
    from pathlib import Path

    import numpy as np
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad import internal_surface as isf
    from meshpipeline.cad.cad_tessellate import seal_open_rims
    from meshpipeline.cad.occ_box import mesh_to_size
    from meshpipeline.cad.stl_io import write_stl_binary

    if not x.passages:
        return None
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    faces = 0
    for p in x.passages:
        for f in p.boundary:
            bld.Add(comp, f)
            faces += 1
    if not faces:
        return None
    mesh_to_size(comp, 1.0 / 2500.0, angular_deflection, linear_deflection=linear_deflection)
    tris: list = []
    spans: list[tuple[int, int]] = []          # (first triangle, how many) per passage
    for p in x.passages:
        start = len(tris)
        for f in p.boundary:
            tris.extend(_face_triangles(f))
        spans.append((start, len(tris) - start))
    if not tris:
        logger.warning("fluid volume: the coolant's %d face(s) tessellated to nothing", faces)
        return None
    # the coordinates as the mesher reads them (binary STL is float32), points a hair apart joined
    T = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3).astype(np.float32).astype(np.float64)
    names = ["wall"] * len(T)
    V, F, L = _weld_labelled(T, names)
    # the rim audit every staged surface makes: a face the tessellator left bare leaves a gap
    membranes, open_rims = seal_open_rims([tuple(map(tuple, t)) for t in V[F]])
    if membranes:
        T = np.concatenate([V[F], np.asarray(membranes, dtype=np.float64).reshape(-1, 3, 3)])
        V, F, L = _weld_labelled(T, [*L.tolist(), *(["wall"] * len(membranes))])
    checks = isf._verify(V, F, L, [], "wall", is_lid=np.zeros(len(F), dtype=bool))
    # THREE WAYS A SURFACE CAN BE UNUSABLE HERE, and each is refused rather than written. An open
    # edge lets surfaceToCell flood the whole block instead of the channels. A non-manifold edge
    # (three or more faces meeting) leaves "inside" undefined along it. And an edge crossed the
    # same way by both its faces is a FLIPPED face: the signed-volume check below is a single
    # global number, so a handful of flipped facets on a large surface can hide inside it, which
    # is exactly what the winding count catches.
    for key, what in (("open_edges", "open edge(s)"),
                      ("nonmanifold_edges", "non-manifold edge(s)"),
                      ("inconsistent_edges", "inconsistently wound edge(s)")):
        if checks[key]:
            logger.warning("fluid volume: the coolant surface has %d %s - not written",
                           checks[key], what)
            return None
    # EACH PASSAGE AGAINST ITS OWN VOLUME, before the total, because the total cannot see one
    # reversed passage among a hundred (see PASSAGE_AGREES). Measured on the raw triangles rather
    # than the welded surface: a closed shell's signed volume is a sum over its own faces and does
    # not need its vertices joined to its neighbours'.
    # ...AND ONLY WHEN THE SHELLS CAME OUT CLOSED AS TESSELLATED. seal_open_rims adds membranes
    # AFTER the triangles were gathered per passage, and a membrane belongs to no passage any more:
    # the passage it closes is short by exactly the volume the membrane put back, so checking it
    # against its own volume would refuse a surface that is correct. Where a rim had to be sealed
    # the attribution is gone, so the total is all there is, and this says so rather than
    # pretending the stronger check ran.
    if membranes:
        logger.info("fluid volume: %d rim membrane(s) were needed, so the per-passage volume "
                    "check does not apply; the total stands", len(membranes))
    bad = (None if membranes else
           first_passage_wound_in(tris, spans, [p.volume for p in x.passages]))
    if bad is not None:
        which, got, want = bad
        logger.warning("fluid volume: passage %d of %d encloses %.6g m3 where the B-rep says "
                       "%.6g m3 - it is inside out or incomplete - not written",
                       which + 1, len(x.passages), got, want)
        return None
    signed = surface_volume(V[F])
    found = sum(p.volume for p in x.passages)
    if signed <= 0.0:
        logger.warning("fluid volume: the coolant surface came out inside out "
                       "(signed volume %.6g m3) - not written", signed)
        return None
    # ...AND IT HAS TO ENCLOSE WHAT THE B-REP SAID, which the sign alone does not establish once
    # there is more than one passage. Each shell is internally consistent on its own, so a
    # reversed one raises no open, non-manifold or inconsistent edge, and it merely SUBTRACTS from
    # the total: measured on a four-channel plate with one channel reversed, the surface was
    # written with every edge count zero while the extraction said 2.0 mm3 and the surface
    # enclosed 1.0. The two numbers sat side by side in this function's own return value and
    # nothing compared them. Step 7's part has 5 and 16 channels, so this is its case exactly.
    if found > 0.0 and abs(signed - found) > VOLUME_AGREES * found:
        logger.warning("fluid volume: the coolant surface encloses %.6g m3 but the extraction "
                       "found %.6g m3 - a passage is inside out - not written", signed, found)
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(out_path, V[F])
    used = V[F].reshape(-1, 3)
    # ONE READ OF THE RADIUS FIELD, two answers from it: how narrow the narrowest passage is, and
    # whether the part has one scale of passage or two. Reading it twice would double the most
    # expensive thing this function does.
    _field: list = []
    narrowest = narrowest_passage_m(x, out_path, _field)
    scales, fine = _scales_and_fine_surface(out_path, _field[0] if _field else None)
    return {
        "path": str(out_path), "triangles": int(len(F)), "faces": faces,
        "passages": len(x.passages),
        # WHERE THE COOLANT REACHES, which decides which faces of the block are open. The caller
        # cannot work it out from anything else here - centroids and areas describe the ports, not
        # the extent - and reading it back off the STL that was just written is a second answer to
        # a question this function already holds the vertices for.
        "bbox_min_m": [round(float(used[:, k].min()), 12) for k in range(3)],
        "bbox_max_m": [round(float(used[:, k].max()), 12) for k in range(3)],
        # THE WETTED AREA, which two separate things needed and neither could have.
        #
        # SIZING. A conjugate mesh is almost all refinement band: of 1,852,814 cells on a customer
        # cold plate, 9,817 were background and the rest lay within a few cells of this surface. So
        # what a resolution COSTS follows the area, not the block - and the pre-flight estimate,
        # which prices the whole block at the finest cell, is blind to the one knob that helps.
        # Raising the octree depth divides the background count by eight per level and multiplies
        # its own refinement factor by eight, so that estimate cannot see a change in depth AT ALL,
        # which is why nothing has ever varied it.
        #
        # ACCURACY. The delivered coolant VOLUME is checked against this surface, and volume is
        # exactly the quantity a staircase cancels in: a mesh can lose a 98 um channel and gain
        # the same volume along a wall and read correct. Area does not cancel that way, so the
        # delivered interface area against this is a second reading of the same coolant that the
        # first one cannot agree with by construction.
        #
        # Measured on the triangles that were just written, which is the same surface every number
        # beside it describes.
        "wetted_area_m2": round(float(np.linalg.norm(
            np.cross(V[F][:, 1] - V[F][:, 0], V[F][:, 2] - V[F][:, 0]), axis=1).sum() * 0.5), 15),
        # WHERE THE MOUTHS ARE, which is a different question from where the coolant reaches.
        #
        # A conjugate case took its inlet and outlet from the BLOCK's six faces, which works only
        # while the coolant's mouths lie ON those faces. Eight of the nine parts in the corpus do,
        # because a channel milled across a plate ends flush with the side it was cut on. The first
        # real customer assembly does not: a Corintis cold plate's mouths are fitting bores, and
        # the plate's bounding box is set by the fittings' own bodies, so the coolant stopped 2 mm
        # inside the block on every side and the mesher refused with "it is sealed inside the
        # metal". The mouths were measured all along; nothing carried them out of here.
        #
        # Each row is one mouth of one passage, in metres: where it is, which way the fluid leaves
        # through it, how much area the flow crosses, and how wide the hole is. That is everything
        # needed to name a patch on it after the region split, whether or not it sits on a block
        # face.
        "ports": [{"passage": k,
                   "centroid_m": [round(float(v), 12) for v in p.centroid],
                   "normal": [round(float(v), 9) for v in p.normal],
                   "area_m2": round(float(p.area), 15),
                   "diameter_m": round(float(p.diameter), 12),
                   "wh_m": [round(float(v), 12) for v in p.wh],
                   "clear_ahead": bool(p.clear_ahead)}
                  for k, passage in enumerate(x.passages) for p in passage.main_ports],
        "open_edges": int(checks["open_edges"]),
        "nonmanifold_edges": int(checks["nonmanifold_edges"]),
        # read by its real name. This said checks.get("inconsistent"), which _verify does not
        # return, so it was always 0 and the winding was never actually gated.
        "inconsistent_edges": int(checks["inconsistent_edges"]),
        # the two volumes a caller compares: what the B-rep says the coolant is, and what the
        # surface actually written encloses. They agree to the tessellation's error, and the
        # SIGN is the orientation proof.
        "volume_m3": round(found, 15), "surface_volume_m3": round(signed, 15),
        "volume_mm3": round(found * 1e9, 6), "surface_volume_mm3": round(signed * 1e9, 6),
        "seed_m": None if x.passages[0].seed is None else [round(v, 9) for v in x.passages[0].seed],
        # THE THREE NUMBERS A REAL UPLOAD WILL NOT DECLARE, all measured: the volume and the
        # passage count above, and the narrowest passage here. conjugate.build takes exactly these
        # as expect_fluid_volume_m3, expect_fluid_pieces and narrowest_m, and the cell size follows
        # from the last of them.
        "narrowest_m": round(narrowest, 12),
        # ONE SCALE OF PASSAGE, OR TWO - and where the small one is. None for the ordinary part,
        # which has one.
        #
        # WHY IT IS WORTH RECORDING. The cell size follows from the narrowest passage, and the
        # refinement band is priced over the WHOLE wetted area at that size. On a part with two
        # scales that is the wrong bill by orders of magnitude: the first real customer cold plate
        # is a manifold of 2-8 mm channels bonded to a layer of 0.098 mm slots, and banding all
        # 60,526 mm2 of it at a slot-sized cell estimates about three billion cells. The slots are
        # 35.9% of the area inside 6.0% of the volume, so banding the manifold at a manifold-sized
        # cell and only the slots at a slot-sized one is what makes the part affordable at all.
        "scales": scales,
        # The thin side's own triangles, written beside this surface for the mesher to refine
        # AROUND - a band, not a box containing them. Absent when the part has one scale. Measured:
        # a box bounding that thin layer prices at 456,206,329 cells against 224,431,537 for a band
        # round the same triangles, because the box is almost all metal.
        "fine_surface": fine,
        "open_rims": open_rims,
    }


def surface_bounded(source, out_path, *, scale: float | None = None, kind: str = "cad",
                    angular_deflection: float = 0.2, linear_deflection: float | None = None,
                    budget_s: float | None = None, domain_path=None,
                    domain_patch: str = "") -> dict | None:
    """The coolant of the parts of `source`, written to `out_path` as one closed STL, in a
    time-boxed child process. None - with the reason logged - when there is no coolant to find or
    the surface does not come out closed.

    Unlike `stage_bounded` this needs NOTHING declared: the caller has a file and wants the fluid
    inside it, which is the case a cold plate arrives as.

    `scale` IS REQUIRED, AND IT USED TO DEFAULT TO 1.0. OpenCASCADE hands back millimetres whatever
    the file declared, so 1.0 means "write the STL in millimetres" while this module's own
    docstrings, and every `_m` key in the record it returns, say metres. A caller that simply
    forgot it got a surface a thousand times too big with no complaint anywhere, and then a mesher
    asked for micron cells across a 170 mm plate: 877,659,515 octree boxes and 250 of 251 GiB
    before it was killed, twice. `engines/cfmesh_conjugate/driver.cad_scale` is the factor; pass it.
    `cad/cad_tessellate._occ_to_metres` already refuses the same way for the same reason.
    """
    from pathlib import Path

    if scale is None:
        raise ExtractionError(
            "surface_bounded needs the scale that makes this CAD's coordinates metres - "
            "engines/cfmesh_conjugate/driver.cad_scale(source) - because OpenCASCADE returns "
            "millimetres whatever the file declares, and every _m key of the record this returns "
            "is labelled metres")
    out_path = Path(out_path)
    answer = run_bounded({"task": "surface", "source": str(source), "scale": float(scale),
                          "kind": kind, "out_path": str(out_path),
                          **({"domain_path": str(domain_path)} if domain_path else {}),
                          **({"domain_patch": str(domain_patch)} if domain_patch else {}),
                          "angular_deflection": angular_deflection,
                          "linear_deflection": linear_deflection},
                         out_path.parent / f"{out_path.stem}_fluid_volume",
                         budget_s or STAGE_BUDGET_S)
    rec = answer.get("surface")
    dom = answer.get("domain")
    if isinstance(dom, dict) and isinstance(rec, dict):
        if dom.get("ok"):
            rec["domain"] = dom
            logger.info("fluid volume: wrote the domain the mesher fills - %s mm3 of a %s mm3 box "
                        "(%.1f%%), %d triangles", format(dom["volume_mm3"], ",.0f"),
                        format(dom["box_mm3"], ",.0f"), dom["share_of_box"] * 100.0,
                        dom["triangles"])
        else:
            # SAID OUT LOUD, because the box standing is a 3x bigger mesh and a silent fallback
            # looks exactly like a part that was never worth shelling.
            logger.info("fluid volume: no domain surface (%s) - the box stands",
                        dom.get("reason") or "no reason given")
    if not rec:
        why = answer.get("error") or (answer.get("extraction") or {}).get("reason") \
            or "the parts hold no fluid that reaches the outside"
        logger.info("fluid volume: no coolant surface written (%s)", why)
        return None
    logger.info("fluid volume: wrote the coolant surface - %d triangles, %s mm3 over %d passage(s)",
                rec["triangles"], rec["volume_mm3"], rec["passages"])
    return rec


#: How much of its own bounding box a part has to fill before the box is a reasonable domain. A
#: conjugate mesher fills ONE manifold volume and cuts the regions out of it afterwards, and the
#: easy manifold is the cuboid round the part - every cell of air between the part and that cuboid
#: is paid for at the background size. Measured on the first customer cold plate, 2026-10-29:
#:
#:     bounding box      846,543 mm3     177.51 x 47.21 x 101.01 mm
#:     the six solids    222,662 mm3     26.3% of it
#:     the coolant        52,145 mm3     one circuit, two ports
#:     metal + coolant   274,807 mm3     32.5% of it
#:
#: Two 33 mm fittings standing off a 7-9 mm plate stack put the other 67.5% there, and none of it
#: is part. Above this share the cuboid costs little and is the simpler surface; below it, the
#: part own skin is worth building.
#:
#: AND ON THAT ASSEMBLY IT IS NOW BUILT. This used to say the boolean in `domain_solid` did not
#: come off on it, and that no part the engine had met had reached this threshold. Both were true
#: of the fuse; `_domain_from_split` builds the domain out of the split that already separated
#: metal from coolant instead, and the split does come off.
#:
#: MEASURED IN A CUSTOMER RUN, 2026-10-30, job 7439e942 on ColdPlateAssembly_CLEAN.step through
#: the page: `share_of_box` 0.3685 against BOX_WORTH_KEEPING 0.6, and the driver filled the part's
#: own skin - 276,372 mm3 of a 750,015 mm3 box, 27,696 triangles, one solid `wall_outer` - in place
#: of the twelve-triangle cuboid. The mechanism is still tested on the 2-channel fixture (7.04 mm3
#: of metal and 0.96 of coolant fuse to 8.00 as one closed piece) and `write_domain` still refuses
#: anything that is not one closed piece, so a part the split cannot close keeps the cuboid.
BOX_WORTH_KEEPING = 0.6

#: The tolerance the coolant own boundary faces are sewn back into a shell with. The faces come
#: from one split of one shape, so they already share their edges; this is for the last digit.
SEW_TOL = 1e-7


def _domain_from_split(x: Extraction):
    """Metal and coolant together, out of the split that already separated them - or None.

    NO BOOLEAN. The split classifies every region of space as outside, metal or space, so the
    domain is "every region that is not outside" and its boundary is every face of those regions
    that two of them do not share: the part's outer skin, with each port mouth closed by its own
    cap. `Extraction.domain_boundary` carries it because the split knew it all along.

    MEASURED ON THE FIRST CUSTOMER ASSEMBLY (ColdPlateAssembly_CLEAN.step, 6 solids, 2 passages,
    70 regions), which is the part the boolean cannot do:

        domain faces   348
        sewn           2 shells: one degenerate (1 free edge, 0 mm3), one CLOSED
        closed shell   276,372.4 mm3
        the cuboid     750,014.9 mm3        what the engine meshes today
        ratio          2.71x less volume to fill

    276,372.4 against the 276,324 the boolean produces on the fixture it does work on is the same
    answer to four figures, so this is the same domain by a route that does not need the fuse.

    THE DEGENERATE SHELL IS EXPECTED and is why this takes the largest CLOSED one rather than the
    first: sewing 348 faces off a split leaves the odd sliver that closes nothing, exactly as a
    passage boundary does, and `domain_solid` has always skipped those.
    """
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopoDS import TopoDS

    faces = getattr(x, "domain_boundary", None) or []
    if not faces:
        return None
    try:
        sew = BRepBuilderAPI_Sewing(SEW_TOL)
        for f in faces:
            sew.Add(f)
        sew.Perform()
        sewn = sew.SewedShape()
        # EVERY CLOSED SHELL, not the biggest one. Sewing a split's faces leaves the odd sliver
        # that closes nothing - the customer assembly gave two shells, one of them 0 mm3 with a
        # free edge - and those are skipped exactly as an open passage boundary always was. But a
        # shell that DOES close is a piece of the domain, and taking only the largest would drop
        # the rest without saying so. `write_domain` has always summed over several pieces, and
        # the fuse it replaces produced them.
        pieces, total = [], 0.0
        for sh in (_explore(sewn, TopAbs_SHELL, TopoDS.Shell_s) or [sewn]):
            if _free_edges(sh):
                continue
            made = _shell_solid(sh)
            if made is None:
                continue
            v = abs(_volume(made))
            if v <= 0.0:
                continue
            pieces.append(made)
            total += v
        if not pieces:
            logger.info("fluid volume: the split's own domain did not close (%d face(s)) - "
                        "falling back to the fuse", len(faces))
            return None
        logger.info("fluid volume: the domain came out of the split, %d face(s), %d piece(s), "
                    "%.1f mm3", len(faces), len(pieces), total * 1e9)
        if len(pieces) == 1:
            return pieces[0]
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound

        comp = TopoDS_Compound()
        bld = BRep_Builder()
        bld.MakeCompound(comp)
        for one in pieces:
            bld.Add(comp, one)
        return comp
    except Exception:  # noqa: BLE001 - the fuse below is the fallback, not a second failure
        logger.warning("fluid volume: the split's own domain could not be sewn", exc_info=True)
        return None


def domain_solid(solids: list, x: Extraction, _skipped: list | None = None):
    """Metal UNION coolant as one solid - the volume a conjugate mesher has to fill - or None.

    THE DOMAIN IS NOT THE PART AND NOT THE BOX. A cold plate boundary is one closed surface that
    runs over the outside, dives in at the inlet, winds through the channels and comes back out at
    the outlet, so a mesher given it fills the METAL and leaves the coolant outside the domain:
    measured on a part skin handed to cfMesh, the fluid region came back at 256 cells against
    495,506. Fusing the coolant back in caps the mouths with the coolant own end faces and leaves
    exactly the part outer skin, enclosing metal and coolant together - which is the volume the
    split is going to cut in two.

    Every passage goes in whose boundary CLOSES, including a dead end with one port: a bore that
    opens to the outside is inside the outer skin whether anything flows through it or not. A
    closed pocket is not a passage (Extraction.pockets) and is already inside the metal.

    AND A SHELL THAT DOES NOT CLOSE IS NOT A SOLID, which is the whole reason this checks.
    `BRepBuilderAPI_MakeSolid` does not require closure, so an open shell comes back as a "solid"
    that measures whatever its open surface happens to enclose. Measured on the first customer
    assembly:

        passage 0   9331 faces   1 shell   free_edges 0   solid 52,144.6 mm3   boundary said 52,144.6
        passage 1     14 faces   1 shell   free_edges 2   solid    933.4 mm3   boundary said  1,517.2

    The second is a dead-end bore whose boundary leaves two edges free. Fusing it took 346.8 s,
    reported IsDone, and produced a shape whose bounding box measured zero - so the domain was
    silently abandoned and the box kept. A passage that will not close is skipped and named; what
    it holds stays a void inside the metal, which is where the engine has always left a pocket.

    `_skipped` collects the passages left out, for the caller to say out loud.
    """
    from OCP.BOPAlgo import BOPAlgo_GlueShift
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    if not solids:
        return None
    from_split = _domain_from_split(x)
    if from_split is not None:
        return from_split
    fluids = []
    for k, passage in enumerate(x.passages):
        if not passage.boundary:
            if _skipped is not None:
                _skipped.append(k)
            continue
        sew = BRepBuilderAPI_Sewing(SEW_TOL)
        for f in passage.boundary:
            sew.Add(f)
        sew.Perform()
        sewn = sew.SewedShape()
        # TopoDS.Shell_s, not TopoDS_Shell.DownCast_s: this OCP has only the former, and the
        # cast that does not exist raised inside the child, where _child logged it as "no domain
        # surface" and the engine quietly kept the box.
        made = None
        for sh in (_explore(sewn, TopAbs_SHELL, TopoDS.Shell_s) or [sewn]):
            if _free_edges(sh):
                continue
            made = _shell_solid(sh)
            if made is not None:
                fluids.append(made)
                break
        if made is None and _skipped is not None:
            _skipped.append(k)
    if not fluids:
        return None
    args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    for one in solids:
        args.Append(one)
    for one in fluids:
        tools.Append(one)
    fu = BRepAlgoAPI_Fuse()
    fu.SetArguments(args)
    fu.SetTools(tools)
    fu.SetFuzzyValue(FUZZY)
    # GLUE, BECAUSE THE TWO BOUNDARIES ARE THE SAME SURFACE. The coolant's boundary faces come out
    # of the split of THIS shape, so every passage wall the tool carries is coincident with the
    # cavity wall the argument carries - the case BOPAlgo's glue mode exists for.
    #
    # IT IS A SPEED WIN AND NOT A CORRECTNESS FIX, and the difference matters because I wrote it
    # down as the fix first. Measured on the first customer assembly (6 solids, 222,662 mm3, plus
    # one coolant circuit of 52,144.6 mm3):
    #
    #     no glue      816.6 s    -11.4752 mm3 in a 1,118 mm3 box
    #     GlueShift    142.2 s    -11.4752 mm3 in a 1,118 mm3 box    identical, 5.7x faster
    #     GlueFull      81.9 s    2 solids, 0.0 mm3, 24,310 mm3 box  wrong a second way
    #
    # GlueShift gives the same answer 5.7x sooner, so it stays. GlueFull asserts that nothing
    # really intersects, which is too strong here: it returns the same shell twice with opposite
    # orientations, which is where the exact 0.0 comes from.
    #
    # THE BOOLEAN IS WRONG ON THAT ASSEMBLY AND NO GLUE MODE FIXES IT. A 1,118 mm3 bounding box
    # out of a part whose own box is 846,543 is a fragment. `write_domain` refuses anything that is
    # not one closed piece, so the engine keeps the cuboid - correct, and 3x the cells. The route
    # that avoids a boolean is to take the metal's own closed tessellation and flood-fill its outer
    # skin with the mouth rims as barriers, then add the caps: the rims are chains of mesh edges
    # because a CAD tessellation respects face boundaries, so it needs no tolerance against this
    # part's 98 um walls - which is what defeats matching the two tessellations against each other,
    # since the deflection at 1/2500 of a 210 mm diagonal is 0.084 mm, itself thicker than a wall.
    # Not attempted.
    fu.SetGlue(BOPAlgo_GlueShift)
    fu.SetRunParallel(True)
    fu.Build()
    if not fu.IsDone():
        return None
    return fu.Shape()


def write_domain(solids: list, x: Extraction, out_path, *, angular_deflection: float = 0.2,
                 linear_deflection: float | None = None,
                 outer_name: str = "") -> dict:
    """domain_solid as one closed STL, with what it is worth said in the record.

    ALWAYS A DICT, and `ok` says whether there is a domain. A bare None lost the reason: this runs
    in a time-boxed child process whose stdout is read and thrown away unless it times out (see
    run_bounded), so the one run anybody wanted explained was the one that left no trace. On the
    first customer assembly it came back `"domain": null` after 1,001 s with nothing anywhere
    saying why. The reason travels in the answer now, because that is the only thing that does.

    `outer_name` writes it as ASCII with that ONE named solid instead of binary. Binary STL
    carries no names and a nameless surface cannot have an inlet, so a mesher handed this needs
    the name; a caller only measuring it does not, and binary is a fifth of the bytes.

    None when the fuse does not come off or the surface does not come out closed - and that is not
    a failure, it is the engine keeping the cuboid it has always used. The record is what lets the
    caller decide: share_of_box is how much of the bounding box the domain actually is, and
    BOX_WORTH_KEEPING is where the cuboid stops being worth its air.
    """
    from pathlib import Path

    from meshpipeline.cad.cad_tessellate import seal_open_rims
    from meshpipeline.cad.occ_box import mesh_to_size
    from meshpipeline.cad.stl_io import write_stl_binary, write_stl_solids

    skipped: list = []
    domain = domain_solid(solids, x, skipped)
    aside = (f" (passage {', '.join(str(k) for k in skipped)} would not close)" if skipped else "")
    if domain is None:
        return {"ok": False, "skipped_passages": skipped,
                "reason": f"the part and its coolant would not fuse into one solid{aside}"}
    pieces = _solids(domain)
    if not pieces:
        return {"ok": False, "reason": "the fuse produced no solid"}
    lo, hi = _real_box(domain)
    box = max((hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]), 0.0)
    vol = sum(_volume(one) for one in pieces)
    if vol <= 0.0 or box <= 0.0:
        return {"ok": False, "skipped_passages": skipped,
                "reason": f"the fused domain measures {vol * 1e9:.4f} mm3 "
                          f"in a {box * 1e9:.4f} mm3 box{aside}"}
    mesh_to_size(domain, 1.0 / 2500.0, angular_deflection, linear_deflection=linear_deflection)
    tris: list = []
    for f in _faces(domain):
        tris.extend(_face_triangles(f))
    if not tris:
        return {"ok": False, "reason": "the fused domain tessellated to no triangles"}
    _membranes, rims = seal_open_rims(tris)
    closed = closed_pieces(tris)
    # A DOMAIN THAT IS NOT CLOSED IS NOT A DOMAIN. cfMesh fills what the surface encloses, and an
    # open surface encloses the universe; the caller keeps the cuboid rather than find out.
    if rims or closed != 1 or len(pieces) != 1:
        return {"ok": False, "triangles": len(tris), "solids": len(pieces),
                "volume_mm3": round(vol * 1e9, 4), "box_mm3": round(box * 1e9, 4),
                "skipped_passages": skipped,
                "reason": (f"the domain surface is {len(pieces)} solid(s) and {closed} closed "
                           f"piece(s) with {len(rims)} open rim(s), not one closed piece{aside}")}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if outer_name:
        write_stl_solids(out_path, {outer_name: tris})
    else:
        write_stl_binary(out_path, tris)
    return {
        "ok": True,
        "path": str(out_path),
        "solid_name": outer_name,
        "triangles": len(tris),
        "solids": len(pieces),
        "volume_mm3": round(vol * 1e9, 4),
        "box_mm3": round(box * 1e9, 4),
        "share_of_box": round(vol / box, 4),
        "metal_mm3": round(sum(_volume(one) for one in solids) * 1e9, 4),
        "fluid_mm3": round(sum(q.volume for k, q in enumerate(x.passages)
                               if k not in set(skipped)) * 1e9, 4),
        "skipped_passages": skipped,
        "lo_m": [round(v, 9) for v in lo],
        "hi_m": [round(v, 9) for v in hi],
    }


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
#:
#: 120 s WAS NOT A LIMIT ANYTHING IMPOSED. It was added in one commit with no reasoning beside it,
#: when the slowest part anyone had run was 62.6 s, and the first real customer assembly - six
#: solids, 9226 faces - took 360.3 s and so was simply discarded. Nothing above this is anywhere
#: near as tight: the scout's own Celery soft limit is sized from the file (900 s + 60 s per MB,
#: which is 1853 s for that 15.9 MB upload), and a step that beats its progress is never stopped
#: for taking long (application/geometry_progress: "Liveness, not a budget").
#:
#: 300 s WAS PRICED OFF A READING THAT DOES NOT REPRODUCE. It was set from "that customer assembly
#: at 9226 faces now measures 141.5 s in `extract` and 162.9 s of child wall". Measured again on
#: 2026-10-29, same file, same checkout, on an idle box (load 0.31, every container under 1%),
#: twice:
#:
#:     extract   359.2 s      of which candidate_caps 116.6 s
#:     extract   359.0 s      and the boundary walks 16.1 s, 4.5%
#:
#: 359 s against a 300 s budget, reproducibly, on the one part this engine was built for. What it
#: cost: the geometry check timed out on every run of Corintis's plate, fell back to the
#: inside-space map, and read a liquid-cooled cold plate as a solid body in a flow - the wrong job,
#: from the flagship file, every time.
#:
#: 900 s, and the reasoning is that NOTHING ELSE AROUND THIS IS A FLAT NUMBER. The scout's own
#: Celery soft limit is sized from the file - 900 s + 60 s per MB, which is 1,853 s for that
#: 15.9 MB upload - and `application/geometry_progress` states the principle outright: "Liveness,
#: not a budget". 900 s leaves that enclosing limit in charge of a file this size, is 2.5x the
#: measurement rather than 0.84x it, and is still a bound: `extract` refuses anything over
#: MAX_FACES = 20000 faces outright, and the slowest part in the corpus is 62.6 s.
#:
#: WHAT WOULD BE BETTER than either number is not spending it. 116.6 s of the 359 is
#: `candidate_caps`, which is the next thing to measure into.
#:
#: A SPLIT THAT RUNS OUT OF TIME IS NOT "NO COOLANT". The two used to arrive downstream as the same
#: thing (`found` false either way), which withheld the conjugate mesher from a part whose coolant
#: the split would have found; cad/fluid_volume_check.fold now says which of the two it was.
CHECK_BUDGET_S = 900.0
#: The BUILDER's own extraction, which splits the same space and then writes a surface out of it -
#: and now a second one when the domain is worth building. It cannot be the tighter of the two, and
#: `test_the_check_budget_leaves_room_for_an_assembly_at_the_face_limit` pins that relationship
#: rather than either number. 1,200 s keeps the same 1.33x over the check that 600 held over 300.
STAGE_BUDGET_S = 1200.0


def _log_child(out, err, why: str) -> None:
    """What the split child said, when the split did not come back. Only on the failing paths: a
    run that answers has its answer, and this is the only account of one that does not."""
    def _tail(b) -> str:
        if not b:
            return ""
        text = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
        return text.strip()[-1500:]

    logger.warning("fluid volume: the split %s%s%s", why,
                   f"; stderr: {_tail(err)}" if _tail(err) else "",
                   f"; stdout: {_tail(out)}" if _tail(out) else "")


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
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "meshpipeline.cad.fluid_volume", str(spec), str(out)],
            timeout=timeout_s, capture_output=True, check=False)
    except subprocess.TimeoutExpired as exc:
        # THE CHILD'S OWN ACCOUNT, OR THERE IS NONE. Everything it logs - the candidate-cap count,
        # the part/cap/region line, the stage timings - goes to a pipe that was being read and
        # thrown away, so the one run anybody wanted to explain was also the one run that left no
        # trace. A kill still has whatever it managed to write before it.
        _log_child(exc.stdout, exc.stderr, f"ran past {timeout_s:.0f} s")
        return {"error": f"finding the fluid inside the parts took over {timeout_s:.0f} s"}
    if not out.exists():
        _log_child(done.stdout, done.stderr, f"exited {done.returncode} with no answer")
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
    x = extract(shape, want_domain=bool(task.get("domain_path")))
    if task.get("task") == "stage":
        rec = stage(x, task.get("declared") or [], task["out_dir"],
                    angular_deflection=float(task.get("angular_deflection") or 0.2),
                    linear_deflection=task.get("linear_deflection"))
        return {"staged": rec, "extraction": x.as_dict()}
    if task.get("task") == "surface":
        # THE GEOMETRY CROSSES THE PROCESS BOUNDARY AS A FILE, not in the answer: as_dict carries
        # centroids and areas and no faces, so a caller that wants the coolant itself had no way
        # to get it. The child writes the STL and the answer says where it is.
        rec = write_surface(x, task["out_path"],
                            angular_deflection=float(task.get("angular_deflection") or 0.2),
                            linear_deflection=task.get("linear_deflection"))
        out = {"surface": rec, "extraction": x.as_dict()}
        # AND THE DOMAIN, on the extraction this has already paid for. The split is the expensive
        # step - 382 s on the first customer assembly - so a caller that wants the volume a
        # conjugate mesher fills must not pay for a second one. Never fatal: without it the engine
        # keeps the cuboid it has always used.
        if task.get("domain_path"):
            try:
                out["domain"] = write_domain(
                    solids, x, task["domain_path"],
                    angular_deflection=float(task.get("angular_deflection") or 0.2),
                    linear_deflection=task.get("linear_deflection"),
                    outer_name=str(task.get("domain_patch") or ""))
            except Exception as exc:  # noqa: BLE001 - the box is the answer when this is not
                out["domain"] = {"ok": False,
                                 "reason": f"building it raised {type(exc).__name__}: {exc}"}
        return out
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


__all__ = ["CHECK_BUDGET_S", "MAX_CAP_FLOOR_M", "MAX_FACES", "MAX_TRIANGLES", "STAGE_BUDGET_S", "Extraction", "ExtractionError",
           "Passage", "Port", "candidate_caps", "closed_pieces", "part_pieces", "extract", "from_triangles", "match_declared", "parts_of", "first_passage_wound_in", "narrowest_passage_m", "surface_bounded", "surface_volume", "write_surface",
           "run_bounded", "stage", "stage_bounded", "touching_parts"]


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
