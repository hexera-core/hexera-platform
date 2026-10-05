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
MIN_RING_BORE_FRACTION = 0.1
#: A closed solid's flat face is a MOUTH (the end of a swept fluid body) only when it is a round or
#: rectangular section at least this fraction of the part's thinnest box side. A nacelle's 8 mm
#: tail flat on a 200 mm body, a blade tip, a wing's airfoil-shaped tip are not mouths.
MIN_MOUTH_OF_THICKNESS = 0.1
#: A mouth is a duct section, not a plate's edge or a pin's end: a flat with sides beyond this
#: ratio is a mouth only when its narrow side is a real size against the part (THIN_FLAT of the
#: diagonal); a wide flat HVAC duct's 6:1 mouth stays, a bracket's 3 mm edge goes.
MOUTH_MAX_ASPECT = 6.0
THIN_FLAT = 0.05
#: A closed solid that fills this much of its bounding box is a body (a car fills ~80% of its box),
#: not a passage (a bent or branched fluid body fills far less). Judged with the flat share.
BOX_LIKE_FILL = 0.5
#: Flat faces covering this much of the box make a body whatever it fills: a finned heat sink
#: (90%) is machined, not a manifold with twelve mouths.
FINNED_FLAT_SHARE = 0.75
#: A swept passage has a few odd flats at most (a volute's side, a mitred elbow's top); a switch,
#: a connector or a bracket has many. Past this many, or past MAX_MOUTHS mouth-like flats, the part
#: is machined.
MAX_ODD_FLATS = 2
MAX_MOUTHS = 12
#: Same-size flats fanning over three or more directions are blade tips only by the handful: a
#: Y junction's three arms fan too.
MIN_BLADES = 4
#: A duct's side wall has a partner of the same size facing it across the duct, about one wall
#: width away; a mouth's partner (the other mouth) is far away, or faces another way, or is absent.
#: Two such flats closer than this, in units of their own size, are walls, not mouths.
WALL_PAIR_GAP = 0.9
#: How many of the largest flat faces get the ray and rim probes.
MAX_PROBED = 32
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
    #: the face's outer extent (for a ring, around the hole): what tells a flange band, which
    #: sits exactly inside the next band's hole, from a coaxial fitting's port, which leaves a gap
    outer_wh: tuple[float, float] = (0.0, 0.0)
    #: nothing of the part sits in the face's own plane just outside its rim (an orifice plate or a
    #: bore shoulder is ringed by wall; a real end face is ringed by air)
    rim_free: bool = True
    #: how far the body runs behind the face along -normal (m), and over the face's own size
    depth: float = 0.0
    depth_ratio: float = 0.0
    #: the face looks back at the part's centre (a flange's back face), not away from it
    inward: bool = False

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
def write_view_stl(path, dest, *, prepared, angular_deflection: float = 0.3, shape=None) -> Path:
    """The part's skin as a binary STL in metres, for the pictures the user and the vision model
    look at. Coarser than a meshing surface on purpose: it only has to look right. `shape` is the
    part as read_cad read it, when the caller has it already: a big STEP takes 10-25 s to read."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer

    from meshpipeline.cad.normalise import occ_scale_transform

    shape = _read_shape(Path(path)) if shape is None else shape
    # a scaled COPY: the shape as read is left untouched (unmeshed) for anyone else reading it
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


def read_cad(path):
    """The part in a STEP or IGES file, as read - unscaled and unmeshed. scout_cad and
    write_view_stl both take it (shape=), so the geometry check reads a file once, not twice."""
    return _read_shape(Path(path))


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
def scout_cad(path, *, prepared, angular_deflection: float = 0.3, shape=None) -> ScoutResult:
    """Everything the geometry can say about itself, in metres, as a proposal.

    `prepared` is the coordinate state the tessellation seam uses (contracts/coordinate_state),
    so this reads the same metres every downstream step reads. `shape` is the part as read_cad
    read it, when the caller has it already."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.normalise import occ_scale_transform
    from meshpipeline.cad.scout_probe import LazyProbe

    path = Path(path)
    shape = _read_shape(path) if shape is None else shape
    # a scaled COPY, meshed below: the shape as read stays untouched for the view skin
    shape = BRepBuilderAPI_Transform(shape, occ_scale_transform(prepared), True).Shape()

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    bbox_min, bbox_max = (x0, y0, z0), (x1, y1, z1)
    diag = _norm(_sub(bbox_max, bbox_min))
    if not math.isfinite(diag) or diag <= 0:
        raise UnreadableCad("the part has no size (empty or degenerate geometry)")
    lin = diag / 2500.0
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)

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
    vg = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vg)
    volume = abs(float(vg.Mass()))
    # THE PROBES' ANSWERS come from the part's own mesh wherever the mesh cannot be wrong, and from
    # the exact B-rep everywhere else (cad/scout_probe): the same answers, minutes faster on a
    # part with hundreds of curved faces.
    part_probe = LazyProbe(shape, solids, deflection=lin)
    inside_any = part_probe.inside_any

    def clear_ahead(origin, direction, skip: float) -> bool:
        """Nothing of the part lies along `direction` from `origin` beyond `skip`."""
        return not part_probe.meets_beyond(origin, direction, skip)

    def first_hit(origin, direction, skip: float) -> float:
        """Distance to the first surface along `direction` beyond `skip`, or 0.0 when none."""
        return part_probe.first_beyond(origin, direction, skip)

    def rim_is_free(face, centroid, normal) -> bool:
        """The face's plane just outside its outer rim holds no material: an end face, not a plate
        set into a wall or a shoulder inside a bore."""
        pts = _rim_polyline(face, BRepTools.OuterWire_s(face))
        if len(pts) < 3:
            return True
        step = max(1, len(pts) // 8)
        for q in pts[::step]:
            out = _add(centroid, _sub(q, centroid), 1.12)          # 12% beyond the rim, in-plane
            for dz in (0.004 * diag, -0.004 * diag):
                if inside_any(_add(out, normal, dz)):
                    return False
        return True

    centre = tuple((bbox_min[k] + bbox_max[k]) / 2.0 for k in range(3))

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
        outer_wh = _wh(outer["wh_m"]) if outer else o_wh
        # on the part's extremity: the face sits on the bounding box in its own direction
        t_edge = min(((bbox_max[k] if normal[k] > 0 else bbox_min[k]) - o_centroid[k]) / normal[k]
                     for k in range(3) if abs(normal[k]) > 1e-6)
        on_extremity = t_edge <= 0.03 * diag
        inward = (not on_extremity) and _dot(normal, _sub(o_centroid, centre)) < 0.0
        candidates.append(Opening(face_index=i, kind=kind, centroid=o_centroid, normal=normal,
                                  area=o_area, wh=o_wh, clear_ahead=False, on_extremity=on_extremity,
                                  outer_wh=outer_wh, inward=inward))

    # THE PROBES (clear ahead, rim, depth) cost a ray or a dozen classifier calls each; a boat hull
    # or an assembly has thousands of small flats, so only the candidates that can matter get them -
    # the largest few - and the rest are dropped as facets, vents and bolt seats.
    # Rings and discs are ranked apart: a wide flat duct's end rings are tiny against its side
    # faces, and they are the whole point.
    every_flat = list(candidates)                          # what the stage's "add an opening" may snap to
    probed: list[Opening] = []
    for kind in ("ring", "disc"):
        same = sorted((o for o in candidates if o.kind == kind), key=lambda o: o.area, reverse=True)
        largest = same[0].area if same else 0.0
        for o in same[:MAX_PROBED]:
            if o.area < MIN_OPENING_FRACTION * largest:
                break
            o.clear_ahead = clear_ahead(_add(o.centroid, o.normal, 0.001 * diag), o.normal, 0.0)
            o.rim_free = rim_is_free(faces[o.face_index], o.centroid, o.normal)
            depth = first_hit(o.centroid, _flip(o.normal), 0.001 * diag)
            o.depth = depth
            o.depth_ratio = depth / max(math.sqrt(max(o.area, 0.0)), 1e-9)
            probed.append(o)
    candidates = probed

    notes: list[str] = []
    decision: dict = {}
    candidates = drop_flange_twins(candidates, tuple((bbox_min[k] + bbox_max[k]) / 2.0 for k in range(3)))
    candidates = drop_stacked_rings(candidates)
    measured = measured_faces(every_flat)
    # A RING is a port when its hole opens to the outside (clear ahead), nothing rings the face in
    # its own plane (an orifice plate and a bore shoulder are set into wall) and it faces away from
    # the part (a flange's back face looks at the body it is bolted to).
    rings = [c for c in candidates if c.kind == "ring" and c.clear_ahead and c.rim_free and not c.inward]
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
        # A closed single solid is EITHER a fluid body (its mouths are the flat ends of a swept
        # passage) or a solid body in a flow (a car, a hub, a wing). Geometry alone cannot always
        # tell a solid cylinder from the water inside a pipe - that is the user's word - but it can
        # rule out the usual false mouths and the box-like bodies:
        #   - a mouth is a round or rectangular flat at least a fifth of the part's thinnest side
        #     (a nacelle's tail flat, a blade tip and a wing's airfoil-shaped tip are not);
        #   - a body whose flats cover much of its box AND which fills it is a body (the Ahmed body:
        #     84% and 95%); a short fat elbow covers a third but fills less than half;
        #   - a part with more than a couple of odd flats, or a dozen mouth-like ones, is machined
        #     (a switch, a connector, a bracket), and so is one whose same-size flats fan around an axis;
        #   - a body with more odd flats than mouths (blade tips, lugs, keyways) is machined, not swept.
        sx, sy, sz = (bbox_max[k] - bbox_min[k] for k in range(3))
        thinnest = max(min(sx, sy, sz), 1e-9)
        fill = volume / max(sx * sy * sz, 1e-18)
        walls = _wall_pairs(discs)
        mouth_like = [o for o in discs if o.shape in ("circle", "rectangle")
                      and o.equivalent_diameter >= MIN_MOUTH_OF_THICKNESS * thinnest
                      and (max(o.wh) <= MOUTH_MAX_ASPECT * max(min(o.wh), 1e-9) or min(o.wh) >= THIN_FLAT * diag)]
        if mouth_like and all(id(o) in walls for o in mouth_like) and fill >= BOX_LIKE_FILL:
            # A short fat passage: its two ends face each other a diameter apart, and they are all it
            # has. It fills its box like the cylinder it is; a rotor hub whose two end discs sit as
            # close is nearly all blades and fills a fifth of its box, so its discs stay walls.
            walls = set()
        mouths = [o for o in mouth_like if id(o) not in walls]
        # odd in shape, size or aspect; a duct's side walls are long and thin but they are not odd
        odd_flats = len([o for o in discs if o not in mouth_like and id(o) not in walls])
        box_skin = 2.0 * (sx * sy + sy * sz + sz * sx) or 1.0
        flat_share = sum(o.area for o in candidates) / box_skin
        box_like = (flat_share >= BOX_LIKE_FLAT_SHARE and fill >= BOX_LIKE_FILL) or flat_share >= FINNED_FLAT_SHARE
        bladed = _bladed([o for o in discs if id(o) not in walls])
        machined = odd_flats > MAX_ODD_FLATS or len(mouths) > MAX_MOUTHS or bladed
        decision = {"flat_share": flat_share, "fill": fill, "n_mouths": len(mouths), "n_odd": odd_flats,
                    "bladed": float(bladed)}
        pool = mouths
        if len(mouths) >= 2 and not box_like and not machined:
            body_kind, input_kind, flow = "single_solid", "fluid-domain", "internal"
            confidence_kind = 0.7
        else:
            body_kind, input_kind, flow = "single_solid", "solid-body", "external"
            confidence_kind = 0.7 if (box_like or machined or not mouths) else 0.6
            if len(mouths) >= 2 and box_like:
                notes.append(f"flat faces cover {100 * flat_share:.0f}% of the part's box and the part fills "
                             f"{100 * fill:.0f}% of it, so it reads as a solid body in a flow, not a fluid passage")
            elif len(mouths) >= 2 and machined:
                notes.append(f"{odd_flats} odd flat faces against {len(mouths)} mouth-like ones, so it reads "
                             "as a machined body in a flow, not a fluid passage")

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
                    "openings": (sum(o.confidence for o in openings) / len(openings)) if openings else 0.0,
                    **decision},
        notes=notes, faces=measured)


MAX_FACES = 200


def _wall_pairs(discs: list[Opening]) -> set[int]:
    """The flats that are a duct's side walls: each has a partner of about its size facing it
    (anti-parallel) across a gap no wider than WALL_PAIR_GAP of its own size. Returns their ids."""
    walls: set[int] = set()
    for i, a in enumerate(discs):
        for b in discs[i + 1:]:
            if _dot(a.normal, b.normal) > -0.8:
                continue                                   # not facing each other (a taper's walls converge a little)
            if not (0.5 <= a.area / max(b.area, 1e-18) <= 2.0):
                continue                                   # not the same size
            gap = abs(_dot(_sub(b.centroid, a.centroid), a.normal))
            size = math.sqrt(max(min(a.area, b.area), 0.0))
            if gap <= WALL_PAIR_GAP * size:
                walls.add(id(a)); walls.add(id(b))
    return walls


def _bladed(discs: list[Opening]) -> bool:
    """MIN_BLADES or more flats of one size whose normals fan out in three or more directions are
    blade tips or lugs around an axis (a rotor, a fan), not the ports of a passage - a manifold's
    identical outlets all face one way, a cross fitting's four ports lie on two axes, and a Y
    junction's three arms are too few."""
    groups: dict[tuple, list[Opening]] = {}
    for o in discs:
        key = (o.shape, round(math.log(max(o.area, 1e-12)) / math.log(1.15)))   # ~15% size bins
        groups.setdefault(key, []).append(o)
    for same in groups.values():
        if len(same) < MIN_BLADES:
            continue
        axes: list = []
        for o in same:
            if all(abs(_dot(o.normal, a)) < 0.9 for a in axes):
                axes.append(o.normal)
        if len(axes) >= 3:
            return True
    return False


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
             "diameter_mm": round(c.equivalent_diameter * mm, 2),
             # plain bools: the mesh scout computes these with numpy, and a numpy bool is not JSON
             "clear_ahead": bool(c.clear_ahead), "on_extremity": bool(c.on_extremity), "rim_free": bool(c.rim_free),
             "inward": bool(c.inward), "depth_ratio": round(float(c.depth_ratio), 2)}
        if c.shape != "circle":
            d["width_mm"], d["height_mm"] = round(c.wh[0] * mm, 2), round(c.wh[1] * mm, 2)
        out.append(d)
    return out


#: A plate's two faces surround one bore; the inner face's hole is the duct's outer wall, a
#: wall's thickness wider. Holes further apart in size than this are two mouths, not one.
SAME_HOLE_TOL = 0.25
#: A plate is thin against the hole it surrounds: the gap between twins, in bores. Small bores
#: carry flanges thicker than that, so a plate up to PLATE_MAX_M thick passes whatever the bore.
PLATE_GAP = 0.5
PLATE_MAX_M = 0.03


def drop_flange_twins(candidates: list[Opening], centre=(0.0, 0.0, 0.0)) -> list[Opening]:
    """A flange plate at a duct's end has two flat faces with the same hole: the outside and the
    inside. Both read as openings a plate's thickness apart, facing opposite ways, over the same
    spot. Only the outer one is a mouth; the inner one is the same mouth seen from inside. Seen
    on bend_elbow_003, which came back with four openings for a duct that has two.

    Every measure is the hole's own, never the whole part's: a manifold's short run has two real
    mouths closer together than a tenth of the part, and a short reducer has two of different
    size. Neither is a plate."""
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
            if a.on_extremity and b.on_extremity:
                continue                                   # two mouths at the part's ends: a run, however short
            d_a, d_b = a.equivalent_diameter, b.equivalent_diameter
            if abs(d_a - d_b) > SAME_HOLE_TOL * max(d_a, d_b, 1e-9):
                continue                                   # different holes: a reducer's two ends
            between = _sub(b.centroid, a.centroid)
            gap = abs(_dot(between, a.normal))
            if gap > max(PLATE_GAP * min(d_a, d_b), PLATE_MAX_M):
                continue                                   # a duct apart, not a plate apart
            across = _norm(_sub(between, tuple(_dot(between, a.normal) * c for c in a.normal)))
            if across > 0.5 * max(d_a, d_b, 1e-9):
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


#: Concentric rings in one plane, all facing the same way, are one flanged end drawn as bands -
#: a gasket face, a step, a chamfer - not several openings. The gap allowed between the bands,
#: in bores, and the floor under it for small bores.
STACK_GAP = 0.1
STACK_GAP_M = 0.003
#: A band reaches the hole of the band around it; a coaxial fitting's inner port stops short of
#: the outer port's hole by the annular passage. A passage thinner than this fraction of the
#: bore is not told from a band.
BAND_FIT = 0.03


def drop_stacked_rings(candidates: list[Opening]) -> list[Opening]:
    """A flanged end modelled as concentric bands gives several ring faces over the same spot,
    facing the same way, each with a hole a little smaller than the last. The fluid passes
    through the smallest: that one is the mouth; the rest are the flange around it. Seen on
    duct_radius_elbow (seven openings for two mouths) and duct_square_round (six for two).

    A coaxial fitting also has concentric rings facing the same way in one plane - and both are
    ports. What tells them apart is the gap: a band's outer edge reaches the next band's hole
    (or beyond it, when a face is drawn twice); a coaxial inner port's outer edge stops short of
    the outer port's hole by the annular passage, however close the two bores are. Bands chain -
    the chamfer reaches the wall's hole, the wall reaches the gasket face's hole - so the rings
    are grouped through every such link, in any order, and each group keeps its smallest hole.
    A ring whose outer extent is unknown joins no group."""
    rings = [k for k, c in enumerate(candidates) if c.kind == "ring"]
    parent = {k: k for k in rings}

    def root(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for n, i in enumerate(rings):
        a = candidates[i]
        for j in rings[n + 1:]:
            b = candidates[j]
            if _dot(a.normal, b.normal) < 0.95:
                continue                                   # not facing the same way
            d_a, d_b = a.equivalent_diameter, b.equivalent_diameter
            between = _sub(b.centroid, a.centroid)
            if abs(_dot(between, a.normal)) > max(STACK_GAP * min(d_a, d_b), STACK_GAP_M):
                continue                                   # not in one plane
            across = _norm(_sub(between, tuple(_dot(between, a.normal) * c for c in a.normal)))
            if across > 0.5 * max(d_a, d_b, 1e-9):
                continue                                   # not over the same spot
            small, big = (b, a) if d_b < d_a else (a, b)
            if not _reaches(small.outer_wh, big.wh):
                continue                                   # a gap between them: two ports of a coaxial fitting
            parent[root(i)] = root(j)

    keep: dict[int, int] = {}                              # group -> the ring with the smallest hole
    for i in rings:
        g = root(i)
        if g not in keep or candidates[i].equivalent_diameter < candidates[keep[g]].equivalent_diameter:
            keep[g] = i
    # The survivor stands for the whole stacked end: its rim is free when any band's rim is (the
    # inner band's rim touches the next band, but the outermost band's rim meets air), and it is
    # clear ahead when any band is.
    for i in rings:
        g = root(i)
        survivor = candidates[keep[g]]
        if i != keep[g]:
            survivor.rim_free = survivor.rim_free or candidates[i].rim_free
            survivor.clear_ahead = survivor.clear_ahead or candidates[i].clear_ahead
    kept = set(keep.values())
    return [c for k, c in enumerate(candidates) if c.kind != "ring" or k in kept]


def _reaches(outer_wh, hole_wh) -> bool:
    """Whether a face's outer extent reaches the hole it sits in - no annular gap between them
    wider than BAND_FIT of the hole. An unknown extent reaches nothing."""
    if tuple(outer_wh) == (0.0, 0.0):
        return False
    return all(o >= (1.0 - BAND_FIT) * h for o, h in zip(sorted(outer_wh), sorted(hole_wh)))


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
