# Responsibility: INS-23, a perforated plate or a grille read from a CAD part's own faces - many short
# through-holes side by side in one flat face of a thin plate - and the staging shape that makes the
# plate's two sides (a free plate) or the grille's whole outline (a grille in a duct's face) the
# openings: a sleeve of wall round the plate's outline, open at its far ends.
# Boundaries: geometry only (OpenCASCADE). It reads and builds; it never decides for the user - the
# geometry check proposes what it reads, the user confirms it, and the staging builds the sleeve only
# where the declared ports sit at the sleeve's ends. The file is never changed.
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RULE = "INS-23"

#: At least this many through-holes in one flat plane make a perforation: a flange's 4 to 8 bolt
#: holes do not, nor a lid's 9 ports (lid_UNF, the most any ported part of the 222 checked has); a fan
#: grille's 12 slots (the fewest any perforation checked has) and a sieve's dozens do.
MIN_HOLES = 10
#: A hole of the perforation is within this factor of the holes' median area (a flange's bolt holes
#: beside a grille's slots are a fifth of their size and are not part of it).
SIZE_SPREAD = 4.0
#: The plate is thin against its holes: its thickness at most this many median hole widths (a sieve:
#: 1.4 mm holes through 5 mm). A manifold block's ports run deep and are not a perforation.
MAX_DEPTH_PER_WIDTH = 5.0
#: The holes are packed: together at least this share of the circle that encloses them (a fan grille
#: 0.5, a sieve 0.2 or more; a flange's bolt circle a few percent).
MIN_OPEN_SHARE = 0.1
#: At most this many holes of one plane are measured (the largest first): a filter mesh has thousands.
MAX_HOLES = 400
#: The sleeve runs this many hole pitches beyond the plate (at least twice its thickness).
SLEEVE_PITCHES = 3.0
#: A grille's lid circle clears its holes by this share of the median hole width.
LID_MARGIN = 0.25
#: The sleeve's wall, as a share of the outline's radius.
SLEEVE_WALL = 0.15


@dataclass
class Perforation:
    """What INS-23 read, in the shape's own length unit (metres in the check and the staging)."""
    kind: str                      # "plate": a free plate, both sides open; "grille": one face of a part
    normal: tuple                  # the perforated face's outward normal (out of the material)
    plane_point: tuple             # a point on the perforated face's plane
    holes: int                     # through-holes counted
    hole_width: float              # median equal-area diameter of a hole
    thickness: float               # the plate's thickness at the holes
    pitch: float                   # median centre-to-centre distance to the nearest hole
    open_share: float              # the holes' area over their enclosing circle's
    centre: tuple                  # the outline's centre, on the perforated face's plane
    radius: float                  # the outline's radius (the lid circle; for a plate its farthest point)
    area: float                    # the opening's area (the outline's)
    sleeve: float                  # how far the sleeve runs beyond the plate
    span: tuple = (0.0, 0.0)       # the part's extent along the normal, from the plane (plate only)
    outline_face: int = -1         # the face whose outer wire is a plate's outline (plate only)
    width: float = 0.0             # the outline's sides (a rectangle-like outline), else 0
    height: float = 0.0
    hole_centres: list = field(default_factory=list)
    hole_areas: list = field(default_factory=list)
    face_ids: list = field(default_factory=list)

    def ends(self) -> list[dict]:
        """The openings the sleeve makes: [{"side", "centroid", "normal", "area", "diameter"}] - a plate's
        upstream side (out along the perforated face's normal) and downstream side; a grille's one lid."""
        n, c = self.normal, self.centre
        d = 2.0 * math.sqrt(self.area / math.pi)
        if self.kind == "grille":
            return [{"side": "lid", "centroid": _add(c, n, self.sleeve), "normal": n, "area": self.area,
                     "diameter": d}]
        top, bottom = self.span
        return [{"side": "front", "centroid": _add(c, n, top + self.sleeve), "normal": n, "area": self.area,
                 "diameter": d},
                {"side": "back", "centroid": _add(c, n, bottom - self.sleeve), "normal": tuple(-v for v in n),
                 "area": self.area, "diameter": d}]

    def as_dict(self, to_mm: float = 1000.0) -> dict:
        return {"rule": RULE, "kind": self.kind, "holes": self.holes,
                "hole_mm": round(self.hole_width * to_mm, 3), "thickness_mm": round(self.thickness * to_mm, 3),
                "pitch_mm": round(self.pitch * to_mm, 3), "open_share": round(self.open_share, 3),
                "centre_mm": [round(v * to_mm, 4) for v in self.centre], "normal": [round(v, 5) for v in self.normal],
                "radius_mm": round(self.radius * to_mm, 4), "area_mm2": round(self.area * to_mm * to_mm, 4),
                "sleeve_mm": round(self.sleeve * to_mm, 4),
                "ends": [{"side": e["side"], "centroid_mm": [round(v * to_mm, 4) for v in e["centroid"]],
                          "normal": [round(v, 5) for v in e["normal"]],
                          "area_mm2": round(e["area"] * to_mm * to_mm, 4),
                          "diameter_mm": round(e["diameter"] * to_mm, 4)} for e in self.ends()],
                "hole_centres_mm": [[round(v * to_mm, 4) for v in h] for h in self.hole_centres]}


def _add(a, n, t: float) -> tuple:
    return tuple(a[k] + t * n[k] for k in range(3))


def _dot(a, b) -> float:
    return sum(a[k] * b[k] for k in range(3))


def _sub(a, b) -> tuple:
    return tuple(a[k] - b[k] for k in range(3))


def _norm(a) -> float:
    return math.sqrt(_dot(a, a))


def _median(v: list) -> float:
    s = sorted(v)
    if not s:
        return 0.0
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def _wire_points(wire, per_edge: int = 12) -> list:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    pts = []
    ee = TopExp_Explorer(wire, TopAbs_EDGE)
    while ee.More():
        c = BRepAdaptor_Curve(TopoDS.Edge_s(ee.Current()))
        ee.Next()
        a, b = c.FirstParameter(), c.LastParameter()
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        for k in range(per_edge):
            p = c.Value(a + (b - a) * k / per_edge)
            pts.append((p.X(), p.Y(), p.Z()))
    return pts


def _wire_area(wire) -> tuple[float, tuple] | None:
    """The area a closed planar wire encloses and its centroid."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    mk = BRepBuilderAPI_MakeFace(wire, True)
    if not mk.IsDone():
        return None
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(mk.Face(), g)
    a = abs(float(g.Mass()))
    c = g.CentreOfMass()
    return (a, (c.X(), c.Y(), c.Z())) if a > 0 else None


class _Probe:
    """Rays and inside tests on the part's solids: the scout's own probe (cad/scout_probe), on the part's mesh
    wherever the mesh cannot be wrong and exact elsewhere - an exact classifier on an unmeshed B-spline part
    takes minutes a point."""

    def __init__(self, shape):
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        from meshpipeline.cad.occ_box import mesh_to_size
        from meshpipeline.cad.scout_probe import make_probe

        solids = []
        se = TopExp_Explorer(shape, TopAbs_SOLID)
        while se.More():
            solids.append(TopoDS.Solid_s(se.Current()))
            se.Next()
        _box, lin = mesh_to_size(shape, 1.0 / 2500.0, 0.3)
        self.probe = make_probe(shape, solids, deflection=lin)

    def inside(self, p) -> bool:
        return bool(self.probe.inside_any(tuple(float(v) for v in p)))

    def first_hit(self, origin, direction, skip: float = 0.0) -> float:
        """Distance along the ray to the first surface past `skip`; inf when it leaves the part."""
        if not self.probe.meets_beyond(origin, direction, skip):
            return math.inf
        return float(self.probe.first_beyond(origin, direction, skip))


def _planar_faces(shape) -> list[dict]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    fe = TopExp_Explorer(shape, TopAbs_FACE)
    i = -1
    while fe.More():
        f = TopoDS.Face_s(fe.Current())
        fe.Next()
        i += 1
        a = BRepAdaptor_Surface(f)
        if a.GetType() != GeomAbs_Plane:
            continue
        pl = a.Plane()
        d = pl.Axis().Direction()
        n = (d.X(), d.Y(), d.Z())
        if f.Orientation() == TopAbs_REVERSED:
            n = tuple(-v for v in n)
        o = pl.Location()
        outer = BRepTools.OuterWire_s(f)
        inner = []
        we = TopExp_Explorer(f, TopAbs_WIRE)
        while we.More():
            w = TopoDS.Wire_s(we.Current())
            we.Next()
            if not w.IsSame(outer):
                inner.append(w)
        out.append({"i": i, "face": f, "n": n, "p": (o.X(), o.Y(), o.Z()), "outer": outer, "inner": inner})
    return out


def _thickness(probe: _Probe, holes: list[dict], n, eps: float) -> float:
    """The plate's thickness between its holes: from a point of material just behind the face midway
    between neighbouring holes, the distance along -n to the far side."""
    depths = []
    for h in holes[:40]:
        q = min((o for o in holes if o is not h), key=lambda o: _norm(_sub(o["c"], h["c"])), default=None)
        if q is None:
            continue
        m = tuple(0.5 * (h["c"][k] + q["c"][k]) for k in range(3))
        start = _add(m, n, -eps)
        if not probe.inside(start):
            continue                            # the midpoint is not on a bar (two holes of one slot ring)
        t = probe.first_hit(start, tuple(-v for v in n), skip=0.0)
        if math.isfinite(t):
            depths.append(t + eps)
    return _median(depths) if len(depths) >= 3 else 0.0


def _square_to(n) -> tuple[tuple, tuple]:
    a = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _sub(a, tuple(_dot(a, n) * v for v in n))
    u = tuple(v / _norm(u) for v in u)
    w = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
    return u, w


def _opens_wide(probe: _Probe, c, n, depth: float, width: float) -> bool:
    """Past the plate's far side the hole opens into a space wider than itself (the open air, a duct's
    inside): at least 5 of 8 points a hole's width round its axis there are free of material. A blind
    bolt hole or a bore that runs on into a boss has its own wall there (measured: 0 of 8)."""
    u, w = _square_to(n)
    q = _add(c, n, -depth)
    free = 0
    for k in range(8):
        ang = 2.0 * math.pi * k / 8
        p = tuple(q[i] + width * (math.cos(ang) * u[i] + math.sin(ang) * w[i]) for i in range(3))
        free += not probe.inside(p)
    return free >= 5


def read_perforation(shape) -> Perforation | None:
    """INS-23: THE PART'S PERFORATED PLATE OR GRILLE, if it has one. A plane of the part's flat faces holding
    at least MIN_HOLES through-holes of about one size (within SIZE_SPREAD of their median area), packed
    (MIN_OPEN_SHARE of the circle round them), through a plate no thicker than MAX_DEPTH_PER_WIDTH of their
    width. Through: from each hole, along into the part, nothing is met for more than 1.5 times the plate's
    thickness, and past the plate the hole opens into a space wider than itself (a blind bolt hole or a bore
    running on into a boss does not). Both sides of the plate open to the air: a free plate (a sieve, a tube
    sheet), whose openings are its two sides. One side walled in by the part (a duct's or a cup's inside): a
    grille, whose opening is one lid over its holes on the open side. Both walled in: a baffle inside a
    passage, not read here. The perforation with the most holes wins; None when there is none."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = math.dist((x0, y0, z0), (x1, y1, z1))
    if not (math.isfinite(diag) and diag > 0):
        return None
    faces = _planar_faces(shape)
    # planes holding many holes, faces of one plane together (a plate drawn in several faces)
    groups: list[dict] = []
    tol = 1e-4 * diag
    for f in faces:
        if not f["inner"]:
            continue
        off = _dot(f["n"], f["p"])
        g = next((g for g in groups if _dot(g["n"], f["n"]) > math.cos(math.radians(2.0))
                  and abs(g["off"] - off) <= tol), None)
        if g is None:
            g = {"n": f["n"], "off": off, "faces": []}
            groups.append(g)
        g["faces"].append(f)
    probe = None
    best: Perforation | None = None
    for g in groups:
        wires = [(f, w) for f in g["faces"] for w in f["inner"]]
        if len(wires) < MIN_HOLES:
            continue
        holes = []
        for f, w in wires:
            m = _wire_area(w)
            if m is None:
                continue
            holes.append({"a": m[0], "c": m[1], "wire": w, "face": f["i"]})
        holes = sorted(holes, key=lambda h: -h["a"])[:MAX_HOLES]
        if len(holes) < MIN_HOLES:
            continue
        med = _median([h["a"] for h in holes])
        holes = [h for h in holes if med / SIZE_SPREAD <= h["a"] <= med * SIZE_SPREAD]
        if len(holes) < MIN_HOLES:
            continue
        if probe is None:
            probe = _Probe(shape)
        n = g["n"]
        width = 2.0 * math.sqrt(_median([h["a"] for h in holes]) / math.pi)
        eps = 0.02 * width
        t = _thickness(probe, holes, n, eps)
        logger.debug("%s: plane n=%s: %d holes of %.4g, plate %.4g thick", RULE, tuple(round(v, 3) for v in n),
                     len(holes), width, t)
        if not 0.0 < t <= MAX_DEPTH_PER_WIDTH * width:
            continue
        through = []
        for h in holes:
            start = _add(h["c"], n, eps)
            if probe.inside(_add(h["c"], n, -eps)):
                continue                        # the centroid is on material (a bent slot): not probed
            hit = probe.first_hit(start, tuple(-v for v in n), skip=0.0)
            own = 2.0 * math.sqrt(h["a"] / math.pi)
            if hit > 1.5 * t + eps and _opens_wide(probe, h["c"], n, t + 0.5 * own, own):
                through.append(h)
        logger.debug("%s: plane n=%s: %d holes of %.4g, plate %.4g thick, %d through", RULE,
                     tuple(round(v, 3) for v in n), len(holes), width, t, len(through))
        if len(through) < MIN_HOLES:
            continue
        perf = _perforation(shape, probe, through, n, t, width)
        if perf is not None and (best is None or perf.holes > best.holes):
            best = perf
    return best


def _enclosed(probe: _Probe, q, n, reach: float) -> bool:
    """A space walled in round q, square to n: at least 5 of 8 rays in that plane meet the part within `reach`
    (a duct's or a cup's inside), or q is in the part's material. The open air beside a free plate is not."""
    if probe.inside(q):
        return True
    u, w = _square_to(n)
    hits = 0
    for k in range(8):
        ang = 2.0 * math.pi * k / 8
        d = tuple(math.cos(ang) * u[i] + math.sin(ang) * w[i] for i in range(3))
        hits += probe.first_hit(q, d) <= reach
    return hits >= 5


def _perforation(shape, probe: _Probe, holes: list[dict], n, t: float, width: float) -> Perforation | None:
    # the holes' circle: centred on their area-weighted centre, out to the farthest point of any hole
    total = sum(h["a"] for h in holes)
    c = tuple(sum(h["a"] * h["c"][k] for h in holes) / total for k in range(3))
    far = 0.0
    for h in holes:
        h["pts"] = _wire_points(h["wire"])
        for p in h["pts"]:
            rel = _sub(p, c)
            far = max(far, _norm(_sub(rel, tuple(_dot(rel, n) * v for v in n))))
    if far <= 0:
        return None
    open_share = total / (math.pi * far * far)
    if open_share < MIN_OPEN_SHARE:
        return None
    pitch = _median([min(_norm(_sub(h["c"], o["c"])) for o in holes if o is not h) for h in holes])
    sleeve = max(SLEEVE_PITCHES * pitch, 2.0 * t)
    width = 2.0 * math.sqrt(_median([h["a"] for h in holes]) / math.pi)
    # WHAT LIES ON EACH SIDE, half the holes' circle out from the plate: the open air (a free plate) or a
    # space the part walls in (a duct's or a cup's inside, behind a grille). Walled in on both sides, it is a
    # baffle inside a passage - the passage's own ends are its openings, not this reading's.
    reach = 3.0 * far
    front = _enclosed(probe, _add(c, n, 0.5 * far), n, reach)
    back = _enclosed(probe, _add(c, n, -(t + 0.5 * far)), n, reach)
    logger.debug("%s: %d holes, walled in in front %s, behind %s", RULE, len(holes), front, back)
    if front and back:
        return None
    common = {"holes": len(holes), "hole_width": width, "thickness": t, "pitch": pitch, "open_share": open_share,
              "sleeve": sleeve, "hole_centres": [tuple(h["c"]) for h in holes],
              "hole_areas": [h["a"] for h in holes], "face_ids": sorted({h["face"] for h in holes})}
    if front or back:
        # the lid lies on the plate's face toward the open air
        if front:
            n, c = tuple(-v for v in n), _add(c, n, -t)
        r = _lid_radius(probe, holes, c, n, far, width)
        if r is None:
            logger.info("%s: %d holes read as a grille, but no lid circle clears its face", RULE, len(holes))
            return None
        return Perforation(kind="grille", normal=tuple(n), plane_point=tuple(c), centre=c, radius=r,
                           area=math.pi * r * r, **common)
    return _plate(shape, c, n, {"normal": tuple(n), "plane_point": tuple(c), **common})


def _lid_radius(probe: _Probe, holes: list[dict], c, n, far: float, width: float) -> float | None:
    """The grille's lid circle: round the holes, LID_MARGIN of a hole's width clear of them, on the face's
    material all the way round (no other hole cut, not over the face's edge) - else None."""
    u, w = _square_to(n)
    eps = 0.05 * width

    def on_material(r: float) -> bool:
        # just behind the face all the way round: material (no hole cut, not past the face's edge), and
        # just in front of it, open air (the face, not a step in it)
        for k in range(72):
            ang = 2.0 * math.pi * k / 72
            for rr in (r, r + 0.02 * width):
                p = tuple(c[i] + rr * (math.cos(ang) * u[i] + math.sin(ang) * w[i]) for i in range(3))
                if not probe.inside(_add(p, n, -eps)) or probe.inside(_add(p, n, eps)):
                    return False
        return True
    # the widest margin that keeps the circle on the face (a grille flush with a flange's edge has little room)
    for share in (LID_MARGIN, LID_MARGIN / 2.0, LID_MARGIN / 5.0, LID_MARGIN / 10.0):
        if on_material(far + share * width):
            return far + share * width
    return None


def _plate(shape, c, n, common: dict) -> Perforation | None:
    """A free plate: its outline is the outer wire of its largest flat face square to the holes, its span
    along n the part's own."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    best = None
    for f in _planar_faces(shape):
        if abs(_dot(f["n"], n)) < math.cos(math.radians(2.0)):
            continue
        m = _wire_area(f["outer"])
        if m is not None and (best is None or m[0] > best[1][0]):
            best = (f, m)
    if best is None:
        return None
    f, (area, oc) = best
    # the outline's centre, on the perforated face's plane
    centre = _add(oc, n, _dot(_sub(c, oc), n))
    pts = _wire_points(f["outer"])
    rad = max(_norm(_sub(_sub(p, centre), tuple(_dot(_sub(p, centre), n) * v for v in n))) for p in pts)
    # the part's extent along n, measured from the perforated face's plane
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    s = [_dot(_sub((x, y, z), centre), n) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    span = (max(s), min(s))
    return Perforation(kind="plate", centre=centre, radius=rad, area=area, span=span, outline_face=f["i"],
                       **common)


# ----------------------------------------------------------------------------- triangle parts ----
#: A triangle part (STL, OBJ) is read for a perforation only up to this many triangles: it is sewn into a solid
#: first, which takes seconds for a sieve's few thousand and minutes past this.
MAX_SEWN_TRIANGLES = 50_000


def solid_from_triangles(tris):
    """A closed triangle surface (n, 3, 3) sewn into an OpenCASCADE solid, its coplanar triangles unified into
    faces - so a plate's face is one face again, its holes inner wires. None when it is too big or does not sew
    into one closed shell."""
    import numpy as np
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
    )
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_IN, TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if not 4 <= len(tris) <= MAX_SEWN_TRIANGLES:
        return None
    size = float(np.ptp(tris.reshape(-1, 3), axis=0).max())
    sew = BRepBuilderAPI_Sewing(1e-6 * size)
    for t in tris:
        poly = BRepBuilderAPI_MakePolygon(gp_Pnt(*t[0]), gp_Pnt(*t[1]), gp_Pnt(*t[2]), True)
        if poly.IsDone():
            f = BRepBuilderAPI_MakeFace(poly.Wire(), True)
            if f.IsDone():
                sew.Add(f.Face())
    sew.Perform()
    shells = []
    se = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    while se.More():
        shells.append(TopoDS.Shell_s(se.Current()))
        se.Next()
    if len(shells) != 1 or not shells[0].Closed():
        return None
    mk = BRepBuilderAPI_MakeSolid(shells[0])
    if not mk.IsDone():
        return None
    solid = mk.Solid()
    cl = BRepClass3d_SolidClassifier(solid)
    cl.PerformInfinitePoint(1e-9)
    if cl.State() == TopAbs_IN:                 # wound inside out: turn it round
        solid.Reverse()
    unify = ShapeUpgrade_UnifySameDomain(solid, True, True, False)
    unify.Build()
    return unify.Shape()


def _oriented_triangles(shape):
    """Every triangle of a meshed shape, wound out of its material (a reversed face's triangles turned round)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    out = []
    fe = TopExp_Explorer(shape, TopAbs_FACE)
    while fe.More():
        face = TopoDS.Face_s(fe.Current())
        fe.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        flip = face.Orientation() == TopAbs_REVERSED
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(k).Get()
            if flip:
                b, c = c, b
            out.append([tuple(tri.Node(i).Transformed(tr).Coord()) for i in (a, b, c)])
    return out


def sleeved_triangles(tris, declared: list | None):
    """INS-23 for a triangle part's staging: when it is a perforated plate or a grille and the declared ports sit at
    its sleeve's ends, its triangles with the sleeve fused on (the same sleeve as a CAD part's); else None."""
    import numpy as np

    if not [p for p in declared or [] if p.get("near_m")]:
        return None
    shape = solid_from_triangles(tris)
    if shape is None:
        return None
    perf = read_perforation(shape)
    if perf is None or not sleeve_ends_declared(perf, declared):
        return None
    sleeved = with_sleeve(shape, perf)
    if sleeved is None:
        return None
    from meshpipeline.cad.occ_box import mesh_to_size

    mesh_to_size(sleeved, 1.0 / 2500.0, 0.2)
    logger.info("%s: the triangle part's %s of %d holes staged in a sleeve %.4g past it", RULE, perf.kind, perf.holes,
                perf.sleeve)
    return np.asarray(_oriented_triangles(sleeved), dtype=float).reshape(-1, 3, 3)


# ------------------------------------------------------------------------------------- staging ----
def sleeve_ends_declared(perf: Perforation, declared: list | None) -> bool:
    """Whether the declaration puts its ports at the sleeve's ends (the user said yes to the plate): every
    end has a declared port within a quarter of the outline's radius of its centre."""
    located = [tuple(p["near_m"]) for p in declared or [] if p.get("near_m")]
    if not located:
        return False
    return all(any(math.dist(q, e["centroid"]) <= 0.25 * perf.radius for q in located) for e in perf.ends())


def with_sleeve(shape, perf: Perforation):
    """The part with the sleeve fused to it: a tube of wall round the outline, open at its far ends, which
    are the openings. A plate's sleeve runs past both its sides, a grille's out from its face. The bars and
    the plate stay wall inside the fluid. Returns the fused shape, or None when the fuse fails."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    n, c = perf.normal, perf.centre
    outer_r = perf.radius * (1.0 + SLEEVE_WALL)
    if perf.kind == "grille":
        lo, hi = 0.0, perf.sleeve
        inner = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*_add(c, n, lo)), gp_Dir(*n)), perf.radius, hi - lo).Shape()
    else:
        top, bottom = perf.span
        lo, hi = bottom - perf.sleeve, top + perf.sleeve
        face = _outline_face(shape, perf.outline_face)
        if face is None:
            return None
        # the outline's own face, moved to the sleeve's far end and swept the sleeve's length
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(*[v * (lo - _dot(_sub(_face_point(face), c), n)) for v in n]))
        start = BRepBuilderAPI_Transform(face, tr, True).Shape()
        inner = BRepPrimAPI_MakePrism(start, gp_Vec(*[v * (hi - lo) for v in n])).Shape()
    outer = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*_add(c, n, lo)), gp_Dir(*n)), outer_r, hi - lo).Shape()
    cut = BRepAlgoAPI_Cut(outer, inner)
    if not cut.IsDone():
        return None
    fuse = BRepAlgoAPI_Fuse(shape, cut.Shape())
    if not fuse.IsDone():
        return None
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    unify = ShapeUpgrade_UnifySameDomain(fuse.Shape(), True, True, False)
    unify.Build()
    return unify.Shape()


def _outline_face(shape, index: int):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    fe = TopExp_Explorer(shape, TopAbs_FACE)
    i = -1
    while fe.More():
        i += 1
        if i == index:
            mk = BRepBuilderAPI_MakeFace(BRepTools.OuterWire_s(TopoDS.Face_s(fe.Current())), True)
            return mk.Face() if mk.IsDone() else None
        fe.Next()
    return None


def _face_point(face) -> tuple:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    p = g.CentreOfMass()
    return (p.X(), p.Y(), p.Z())
