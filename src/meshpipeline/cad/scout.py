# Responsibility: Read a CAD part and say what it is before anyone asks the user - the openings it
# has, where they are and how big, what kind of body the file is, a point inside the flow, and its
# overall size - as a PROPOSAL the user confirms on a picture instead of typing it into a chat.
# Boundaries: geometry only. No model call, no rendering, no engine knowledge, no persistence. It
# proposes with a stated confidence; it never decides, and a part it cannot read says so.
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Openings smaller than this fraction of the largest candidate are bolt holes, vents and
#: drain taps, not ports. The corpus's smallest real branch is 12% of its manifold's feed.
MIN_OPENING_FRACTION = 0.02
#: A part with more candidate openings than this is a perforated plate or a heat-exchanger
#: face, not something with ports to name; the user is shown the largest and told the count.
MAX_OPENINGS = 16
#: A ring face (an annular end of a pipe wall) rims an opening when the hole is a real bore, not
#: a bolt hole: the inner area against the ring's own outer area.
MIN_RING_BORE_FRACTION = 0.2
#: Flat faces covering this much of the part's bounding box make it box-like - a body in a flow
#: (the Ahmed body's flats cover 84% of its box), not a fluid passage (a reducer's two mouths
#: cover 3%).
BOX_LIKE_FLAT_SHARE = 0.35


class UnreadableCad(RuntimeError):
    """The file could not be read as a CAD part; the message says why in the user's words."""


@dataclass
class Opening:
    """One proposed port, in metres. `kind` says which face it came from: a `disc` (a solid-model
    end face - a fluid body's mouth) or a `ring` (the annular end of a pipe wall, whose HOLE is
    the opening). `normal` points out of the part."""

    face_index: int
    kind: str
    centroid: tuple[float, float, float]
    normal: tuple[float, float, float]
    area: float
    wh: tuple[float, float]
    clear_ahead: bool
    on_extremity: bool
    name: str = ""
    role: str = ""
    confidence: float = 0.0
    sticker: int = 0          # 1..n in the order proposed; the number on the picture

    @property
    def shape(self) -> str:
        w, h = self.wh
        if w <= 0 or h <= 0:
            return "other"
        if abs(w - h) <= 0.08 * max(w, h) and abs(self.area - math.pi * w * h / 4.0) <= 0.12 * self.area:
            return "circle"
        if abs(self.area - w * h) <= 0.12 * self.area:
            return "rectangle"
        return "other"

    @property
    def equivalent_diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)

    def as_dict(self) -> dict:
        mm = 1000.0
        out = {
            "id": self.sticker, "face": self.face_index, "name": self.name, "role": self.role, "kind": self.kind,
            "shape": self.shape, "confidence": round(self.confidence, 2),
            "centroid_m": [round(v, 6) for v in self.centroid],
            "centroid_mm": [round(v * mm, 2) for v in self.centroid],
            "normal": [round(v, 5) for v in self.normal],
            "area_m2": round(self.area, 9),
            "area_mm2": round(self.area * mm * mm, 2),
            "on_extremity": self.on_extremity, "clear_ahead": self.clear_ahead,
        }
        if self.shape == "circle":
            out["diameter_mm"] = round(self.equivalent_diameter * mm, 2)
        else:
            out["width_mm"] = round(self.wh[0] * mm, 2)
            out["height_mm"] = round(self.wh[1] * mm, 2)
            out["diameter_mm"] = round(self.equivalent_diameter * mm, 2)   # equivalent, for sizing
        return out


@dataclass
class ScoutResult:
    body_kind: str                      # hollow_wall | pipe_wall | single_solid | surface
    input_kind: str                     # body-surface | fluid-domain | solid-body
    flow: str                           # internal | external
    solids: int
    planar_faces: int
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    openings: list[Opening]
    seed_point: tuple[float, float, float] | None
    confidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: every flat face the scout measured, proposed or not, so a user can add an opening on one
    #: and get its real size and position
    faces: list[dict] = field(default_factory=list)

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.bbox_max[0] - self.bbox_min[0], self.bbox_max[1] - self.bbox_min[1],
                self.bbox_max[2] - self.bbox_min[2])

    def as_dict(self) -> dict:
        mm = 1000.0
        return {
            "body_kind": self.body_kind, "input_kind": self.input_kind, "flow": self.flow,
            "solids": self.solids, "planar_faces": self.planar_faces,
            "bbox_min_m": [round(v, 6) for v in self.bbox_min],
            "bbox_max_m": [round(v, 6) for v in self.bbox_max],
            "size_mm": [round(v * mm, 2) for v in self.size],
            "openings": [o.as_dict() for o in self.openings],
            "seed_point_m": None if self.seed_point is None else [round(v, 6) for v in self.seed_point],
            "seed_point_mm": None if self.seed_point is None else [round(v * mm, 2) for v in self.seed_point],
            "confidence": {k: round(float(v), 2) for k, v in self.confidence.items()},
            "notes": list(self.notes),
            "faces": list(self.faces),
        }


# ------------------------------------------------------------------- reading the part ----
def write_view_stl(path, dest, *, prepared, angular_deflection: float = 0.3) -> Path:
    """The part's skin as a binary STL in metres, for the pictures the user and the vision model
    look at. Coarser than a meshing surface on purpose: it only has to look right."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer

    from meshpipeline.cad.normalise import occ_scale_transform

    shape = _read_shape(Path(path))
    shape = BRepBuilderAPI_Transform(shape, occ_scale_transform(prepared), True).Shape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = _norm((x1 - x0, y1 - y0, z1 - z0)) or 1.0
    BRepMesh_IncrementalMesh(shape, diag / 1500.0, False, angular_deflection, True)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(shape, str(dest)):
        raise UnreadableCad("the part's skin could not be written for viewing")
    return dest


def _read_shape(path: Path):
    from OCP.IFSelect import IFSelect_RetDone

    if path.suffix.lower() in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Reader as Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as Reader
    reader = Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise UnreadableCad(f"the file could not be read as CAD ({path.name})")
    reader.TransferRoots()
    return reader.OneShape()


Vec3 = tuple[float, float, float]


def _vec(p) -> Vec3:
    return (float(p.X()), float(p.Y()), float(p.Z()))


def _xyz(v) -> Vec3:
    return (float(v[0]), float(v[1]), float(v[2]))


def _wh(v) -> tuple[float, float]:
    return (float(v[0]), float(v[1]))


def _sub(a, b) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b, s: float = 1.0) -> Vec3:
    return (a[0] + s * b[0], a[1] + s * b[1], a[2] + s * b[2])


def _flip(v) -> Vec3:
    return (-v[0], -v[1], -v[2])


def _norm(v) -> float:
    return math.sqrt(sum(c * c for c in v))


def _unit(v) -> Vec3:
    n = _norm(v) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a, b) -> float:
    return sum(a[k] * b[k] for k in range(3))


def _rim_polyline(face, wire) -> list:
    """The wire's points on the face's own triangulation (the same construction the internal-flow
    tessellation uses), or [] when the triangulation does not carry it."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location

    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        return []
    trsf = loc.Transformation()
    pts: list = []
    wexp = BRepTools_WireExplorer(wire, face)
    while wexp.More():
        edge = wexp.Current()
        pol = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
        if pol is None:
            return []
        nodes = pol.Nodes()
        seq = [tri.Node(nodes.Value(k)).Transformed(trsf)
               for k in range(nodes.Lower(), nodes.Upper() + 1)]
        if edge.Orientation() == TopAbs_REVERSED:
            seq.reverse()
        for p in seq:
            q = _vec(p)
            if not pts or pts[-1] != q:
                pts.append(q)
        wexp.Next()
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _measure_wires(face) -> tuple[dict | None, dict | None]:
    """(outer, inner): what the face's outer wire encloses and what its largest inner wire
    encloses, each as {"area_m2", "centroid", "wh_m"} or None. A single-wire face has no inner."""
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.cad_tessellate import _rim_measure

    outer_w = BRepTools.OuterWire_s(face)
    outer = _rim_measure(_rim_polyline(face, outer_w))
    inner = None
    we = TopExp_Explorer(face, TopAbs_WIRE)
    while we.More():
        wire = TopoDS.Wire_s(we.Current())
        if not wire.IsSame(outer_w):
            m = _rim_measure(_rim_polyline(face, wire))
            if m is not None and (inner is None or m["area_m2"] > inner["area_m2"]):
                inner = m
        we.Next()
    return outer, inner


# --------------------------------------------------------------------------- the scout ----
def scout_cad(path, *, prepared, angular_deflection: float = 0.3) -> ScoutResult:
    """Everything the geometry can say about itself, in metres, as a proposal.

    `prepared` is the coordinate state the tessellation seam uses (contracts/coordinate_state),
    so this reads the same metres every downstream step reads."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.normalise import occ_scale_transform

    path = Path(path)
    shape = _read_shape(path)
    shape = BRepBuilderAPI_Transform(shape, occ_scale_transform(prepared), True).Shape()

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    bbox_min, bbox_max = (x0, y0, z0), (x1, y1, z1)
    diag = _norm(_sub(bbox_max, bbox_min))
    if not math.isfinite(diag) or diag <= 0:
        raise UnreadableCad("the part has no size (empty or degenerate geometry)")
    BRepMesh_IncrementalMesh(shape, diag / 2500.0, False, angular_deflection, True)

    solids: list = []
    se = TopExp_Explorer(shape, TopAbs_SOLID)
    while se.More():
        solids.append(TopoDS.Solid_s(se.Current()))
        se.Next()
    shells_per_solid = []
    for s in solids:
        n = 0
        he = TopExp_Explorer(s, TopAbs_SHELL)
        while he.More():
            n += 1
            he.Next()
        shells_per_solid.append(n)
    classifiers = [BRepClass3d_SolidClassifier(s) for s in solids]

    def inside_any(p) -> bool:
        for c in classifiers:
            c.Perform(gp_Pnt(*p), 1e-9)
            if c.State() == TopAbs_IN:
                return True
        return False

    def clear_ahead(origin, direction, skip: float) -> bool:
        """Nothing of the part lies along `direction` from `origin` beyond `skip`."""
        inter = BRepIntCurveSurface_Inter()
        inter.Init(shape, gp_Lin(gp_Pnt(*origin), gp_Dir(*direction)), 1e-9)
        while inter.More():
            if inter.W() > skip:
                return False
            inter.Next()
        return True

    # every planar face, measured: the candidate openings and the flat walls among them
    faces: list = []
    fe = TopExp_Explorer(shape, TopAbs_FACE)
    while fe.More():
        faces.append(TopoDS.Face_s(fe.Current()))
        fe.Next()

    candidates: list[Opening] = []
    planar = 0
    for i, f in enumerate(faces):
        surf = BRepAdaptor_Surface(f)
        if surf.GetType() != GeomAbs_Plane:
            continue
        planar += 1
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        area = g.Mass()
        centroid = _vec(g.CentreOfMass())
        normal = _unit(_vec(surf.Plane().Axis().Direction()))
        if f.Orientation() == TopAbs_REVERSED:
            normal = _flip(normal)
        # the normal must point OUT of the material; a face's own orientation says so for a
        # well-formed solid, and the classifier settles it for the rest
        probe = 0.002 * diag
        if solids and inside_any(_add(centroid, normal, probe)) and not inside_any(_add(centroid, normal, -probe)):
            normal = _flip(normal)
        outer, inner = _measure_wires(f)
        kind: str
        o_area: float
        o_centroid: tuple[float, float, float]
        o_wh: tuple[float, float]
        if inner is not None and outer is not None and inner["area_m2"] >= MIN_RING_BORE_FRACTION * outer["area_m2"]:
            # an annular end face: the HOLE is the opening
            kind, o_area = "ring", float(inner["area_m2"])
            o_centroid, o_wh = _xyz(inner["centroid"]), _wh(inner["wh_m"])
        else:
            kind, o_area, o_centroid = "disc", float(area), centroid
            o_wh = _wh(outer["wh_m"]) if outer else (0.0, 0.0)
        # on the part's extremity: the face sits on the bounding box in its own direction
        t_edge = min(((bbox_max[k] if normal[k] > 0 else bbox_min[k]) - o_centroid[k]) / normal[k]
                     for k in range(3) if abs(normal[k]) > 1e-6)
        on_extremity = t_edge <= 0.03 * diag
        clear = clear_ahead(_add(o_centroid, normal, 0.001 * diag), normal, 0.0)
        candidates.append(Opening(face_index=i, kind=kind, centroid=o_centroid, normal=normal,
                                  area=o_area, wh=o_wh, clear_ahead=clear, on_extremity=on_extremity))

    notes: list[str] = []
    candidates = drop_flange_twins(candidates, diag, tuple((bbox_min[k] + bbox_max[k]) / 2.0 for k in range(3)))
    measured = measured_faces(candidates)
    rings = [c for c in candidates if c.kind == "ring"]
    discs = [c for c in candidates if c.kind == "disc" and c.clear_ahead]
    hollow = any(n > 1 for n in shells_per_solid)

    if len(rings) >= 2 or (rings and hollow):
        body_kind, input_kind, flow = "pipe_wall", "body-surface", "internal"
        pool = rings
        confidence_kind = 0.9
    elif hollow:
        body_kind, input_kind, flow = "hollow_wall", "body-surface", "internal"
        pool = rings or discs
        confidence_kind = 0.75
    elif not solids:
        body_kind, input_kind, flow = "surface", "body-surface", "internal"
        pool = discs
        confidence_kind = 0.4
        notes.append("the file holds surfaces, not a closed solid; the openings are read from its flat faces")
    else:
        pool = discs
        # A fluid body is slender: its mouths are a few percent of its box's skin. A car body or
        # a hub is BOX-LIKE: its flat faces cover most of the box, and every one of them is
        # "clear ahead" too - so flat-face count alone would read the Ahmed body as a manifold.
        sx, sy, sz = (bbox_max[k] - bbox_min[k] for k in range(3))
        box_skin = 2.0 * (sx * sy + sy * sz + sz * sx) or 1.0
        flat_share = sum(o.area for o in candidates) / box_skin
        if len(pool) >= 2 and flat_share < BOX_LIKE_FLAT_SHARE:
            body_kind, input_kind, flow = "single_solid", "fluid-domain", "internal"
            confidence_kind = 0.7
        else:
            body_kind, input_kind, flow = "single_solid", "solid-body", "external"
            confidence_kind = 0.7 if flat_share >= BOX_LIKE_FLAT_SHARE else 0.6
            if len(pool) >= 2:
                notes.append(f"flat faces cover {100 * flat_share:.0f}% of the part's box, so it reads as a "
                             "solid body in a flow, not a fluid passage")

    pool = sorted(pool, key=lambda o: o.area, reverse=True)
    if pool:
        largest = pool[0].area
        pool = [o for o in pool if o.area >= MIN_OPENING_FRACTION * largest]
    if len(pool) > MAX_OPENINGS:
        notes.append(f"{len(pool)} candidate openings found; only the {MAX_OPENINGS} largest are proposed")
        pool = pool[:MAX_OPENINGS]

    openings = _name_openings(pool)
    for k, o in enumerate(openings, start=1):
        o.sticker = k                                     # the sticker number is what people see
        o.confidence = 0.85 if (o.on_extremity and o.clear_ahead) else 0.6 if o.clear_ahead else 0.4
    if flow == "external":
        openings = []

    seed = _seed_point(openings, inside_any, want_inside=(input_kind == "fluid-domain"),
                       bbox_min=bbox_min, bbox_max=bbox_max) if openings else None
    if openings and seed is None:
        notes.append("no point inside the flow could be confirmed; the mesher will look for one itself")

    return ScoutResult(
        body_kind=body_kind, input_kind=input_kind, flow=flow, solids=len(solids),
        planar_faces=planar, bbox_min=bbox_min, bbox_max=bbox_max, openings=openings,
        seed_point=seed,
        confidence={"input_kind": confidence_kind,
                    "openings": (sum(o.confidence for o in openings) / len(openings)) if openings else 0.0},
        notes=notes, faces=measured)


MAX_FACES = 200


def measured_faces(candidates: list[Opening]) -> list[dict]:
    """The flat faces as the console's "add an opening" reads them: position, normal, size and
    kind, the largest first, capped so a part with a thousand facets ships a useful few."""
    mm = 1000.0
    out = []
    for c in sorted(candidates, key=lambda o: o.area, reverse=True)[:MAX_FACES]:
        d = {"face": c.face_index, "kind": c.kind, "shape": c.shape,
             "centroid_m": [round(v, 6) for v in c.centroid],
             "centroid_mm": [round(v * mm, 2) for v in c.centroid],
             "normal": [round(v, 5) for v in c.normal], "area_mm2": round(c.area * mm * mm, 2),
             "diameter_mm": round(c.equivalent_diameter * mm, 2)}
        if c.shape != "circle":
            d["width_mm"], d["height_mm"] = round(c.wh[0] * mm, 2), round(c.wh[1] * mm, 2)
        out.append(d)
    return out


def drop_flange_twins(candidates: list[Opening], diag: float, centre=(0.0, 0.0, 0.0)) -> list[Opening]:
    """A flange plate at a duct's end has two flat faces with the same hole: the outside and the
    inside. Both read as openings a plate's thickness apart, facing opposite ways, over the same
    spot. Only the outer one is a mouth; the inner one is the same mouth seen from inside. Seen
    on bend_elbow_003, which came back with four openings for a duct that has two."""
    dropped: set[int] = set()
    for i, a in enumerate(candidates):
        if i in dropped:
            continue
        for j in range(i + 1, len(candidates)):
            if j in dropped:
                continue
            b = candidates[j]
            if _dot(a.normal, b.normal) > -0.95:
                continue                                   # not facing opposite ways
            between = _sub(b.centroid, a.centroid)
            gap = abs(_dot(between, a.normal))
            if gap > 0.1 * diag:
                continue                                   # a whole duct apart, not a plate apart
            across = _norm(_sub(between, tuple(_dot(between, a.normal) * c for c in a.normal)))
            if across > 0.5 * max(a.equivalent_diameter, b.equivalent_diameter, 1e-9):
                continue                                   # not over the same spot
            # the outer face is the one on the part's extremity; failing that, the one whose
            # normal points away from the part's centre (both faces of a plate point away from
            # each other, so that alone would not tell them apart)
            if a.on_extremity and not b.on_extremity:
                dropped.add(j)
            elif b.on_extremity and not a.on_extremity:
                dropped.add(i)
                break
            elif _dot(a.normal, _sub(a.centroid, centre)) >= _dot(b.normal, _sub(b.centroid, centre)):
                dropped.add(j)
            else:
                dropped.add(i)
                break
    return [c for k, c in enumerate(candidates) if k not in dropped]


def _name_openings(pool: list[Opening]) -> list[Opening]:
    """A first guess at roles: two openings are ordered along the axis that separates them most
    and the lower one is the inlet; with more, the widest is the feed. The vision step and the
    user both get to overrule this - it only makes sure every pin has a name."""
    if not pool:
        return []
    if len(pool) == 2:
        a, b = pool
        axis = max(range(3), key=lambda k: abs(a.centroid[k] - b.centroid[k]))
        first, second = (a, b) if a.centroid[axis] <= b.centroid[axis] else (b, a)
        first.name, first.role = "inlet", "inlet"
        second.name, second.role = "outlet", "outlet"
        return [first, second]
    pool[0].name, pool[0].role = "inlet", "inlet"
    for k, o in enumerate(pool[1:], start=1):
        o.name, o.role = (f"outlet_{k}" if len(pool) > 2 else "outlet"), "outlet"
    return pool


def _seed_point(openings: list[Opening], inside_any, *, want_inside: bool, bbox_min, bbox_max):
    """A point in the flow: stepped in from the inlet toward each outlet. For a fluid body the
    point must be IN the solid; for a wall part it must be in the void the wall encloses, which
    is NOT in the solid but inside the part's box."""
    def ok(p) -> bool:
        if not all(bbox_min[k] <= p[k] <= bbox_max[k] for k in range(3)):
            return False
        return inside_any(p) if want_inside else not inside_any(p)

    inlet = next((o for o in openings if o.role == "inlet"), openings[0])
    others = [o for o in openings if o is not inlet] or openings
    r_in = inlet.equivalent_diameter / 2.0
    # A ring duct's mouth has a centre body sitting on its axis, so the centroid itself is in
    # metal; the same probes are also tried off-axis, part-way out toward the rim.
    n = inlet.normal
    seed_dir = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    e1 = _unit(_sub(seed_dir, tuple(_dot(seed_dir, n) * c for c in n)))
    e2 = _unit((n[1] * e1[2] - n[2] * e1[1], n[2] * e1[0] - n[0] * e1[2], n[0] * e1[1] - n[1] * e1[0]))
    radial = [(0.0, 0.0)] + [(s * f * r_in, 0.0) for f in (0.6, 0.85) for s in (1, -1)] \
        + [(0.0, s * f * r_in) for f in (0.6, 0.85) for s in (1, -1)]

    def probes(base):
        for a, b in radial:
            yield _add(_add(base, e1, a), e2, b)

    for o in others:
        seg = _sub(o.centroid, inlet.centroid)
        length = _norm(seg)
        if length <= 0:
            continue
        u = _unit(seg)
        for frac in (0.1, 0.25, 0.5):
            for p in probes(_add(inlet.centroid, u, frac * length)):
                if ok(p):
                    return p
    # straight in along the inlet's own normal, which points out of the part
    for step in (0.5, 1.0, 2.0):
        for p in probes(_add(inlet.centroid, inlet.normal, -step * r_in)):
            if ok(p):
                return p
    return None
