# Responsibility: SPLIT A MIXED BODY (thin walls and thick regions, cad/midsurface's "mixed" verdict) into the part
# meshed as shell and the part kept solid: the thick regions found as what the shell walls do not hold, each grown a
# measured distance into the walls round it (MIXED-2, placeholder), the body cut there, the walls' mid-faces trimmed
# to the same cut - so every shell edge on the solid lies on a cut face, the wall's own cross-section.
# Boundaries: geometry only, deterministic, OpenCASCADE (offsets, Booleans, one thread). It meshes nothing and
# writes no file; it reads the extraction's record and sewn mid-surface and never changes how they are made.
# Collaborates with: cad/midsurface.py (the record, the sewn surface, its helpers), engines/mixed_mesh.py (meshes the
# two parts and joins them), engines/mixed_links.py (the join), docs/design/shell/README.md section 2.5.
"""MIXED-1..3: WHERE A MIXED BODY IS SHELL AND WHERE IT IS SOLID.

MIXED-1, THE THICK CORE: each shell wall's slab (its sewn mid-face thickened to its wall, half each side, a hair
over) is taken from the body. What is left and holds a face the extraction called thick or solid (a boss, a pad, a
block, a flange, a casting's lump) is a thick core. What is left and holds none is the metal of a junction between
shell walls (a fillet, a corner a shell model leaves out) and is not kept - as in a shell-only part.

MIXED-2, THE CUT: each core is grown CUT_WALLS of the thickest wall that runs into it, in every direction (a 3D
offset), and the body's metal inside the grown core is the solid region. Inside a wall the grown core's side is
the wall's cross-section, square to it (a straight edge's offset is a plane, a boss's circle a cylinder, a tube's
end a plane across it): the cut is made where the wall's stress is a plate's again. A wall's stress is
three-dimensional only about one wall from a junction (Saint-Venant; Abaqus' shell-to-solid coupling spreads
each shell node over half a wall either side); two walls keeps the join out of it. Each shell mid-face is trimmed by
the same grown core, so its edge lies on the cut face, along the wall's middle.

MIXED-3, ACCOUNTED FOR: the solid region's volume plus each trimmed mid-face's area times its wall holds the body's
volume within the shell check's band (VOLUME_BAND: a shell model leaves its junctions' fillets out); a split that
does not is not used, and the body is meshed solid, with the reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad import midsurface as ms

logger = logging.getLogger(__name__)

#: MIXED-2: how far each thick core is grown into the walls that run into it, in walls (the thickest of them)
CUT_WALLS = 2.0
#: MIXED-1: a wall's slab reaches this share of a wall past each of its faces (the faces are the slab's to take)
SLAB_SLACK = 0.02
#: MIXED-1: left-over pieces holding no thick face, together more than this share of the body's volume, are walls the
#: slabs missed - the split is not used. 5%, not 2% (2 Nov): a box with 4 mm walls has 3% of its metal in the corners
#: of its L junctions round the bottom, which a shell model leaves out (MIXED-3's volume band still holds the whole)
LEFT_OVER_MAX = 0.05
#: MIXED-1b: a thick core is at least this share of the thinnest wall thick (twice its volume over its area)
CORE_MIN_WALLS = 0.5
#: MIXED-3: the split must hold the body's metal within this band (the shell check's own)
VOLUME_BAND = ms.VOLUME_BAND


@dataclass
class Split:
    """The split of one body: the solid region, the trimmed mid-faces (each with the index of the sewn face it came
    from), the cut faces of the solid region (each with the wall of the shell that ends on it), the words."""
    solid: object = None
    shell_faces: list = field(default_factory=list)
    shell_from: list[int] = field(default_factory=list)
    cut_faces: list = field(default_factory=list)
    cut_walls: list[float] = field(default_factory=list)
    cores: int = 0
    core_volumes: list[float] = field(default_factory=list)
    solid_volume: float = 0.0
    shell_volume: float = 0.0
    body_volume: float = 0.0
    dropped_volume: float = 0.0
    cut_distance: list[float] = field(default_factory=list)
    ok: bool = False
    why: str = ""
    notes: list[str] = field(default_factory=list)


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS

    m = ms._map(shape, TopAbs_SOLID)
    return [TopoDS.Solid_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def slab(face, half: float):
    """The solid a face's wall fills: the face moved `half` back along its normal, then thickened 2 x half forward
    (OCC's simple offset, its side walls ruled along the normal)."""
    from OCP.BRepOffset import BRepOffset_MakeSimpleOffset

    back = ms.half_offset(face, half)
    back = ms._faces(back)[0] if ms._faces(back) else back
    mk = BRepOffset_MakeSimpleOffset(back, 2.0 * half)
    mk.SetBuildSolidFlag(True)
    mk.Perform()
    if not mk.IsDone():
        raise ValueError(f"a wall's slab could not be made ({mk.GetErrorMessage()})")
    out = mk.GetResultShape()
    # the offset's solid can come out turned inside out (its faces' normals inward): the mid-face is inside it
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    p = ms._inner_point(face)
    if p is not None and BRepClass3d_SolidClassifier(out, gp_Pnt(*map(float, p)), 1e-7).State() != TopAbs_IN:
        out = out.Reversed()
    return out


def _bop(kind: str, a, tools: list, fuzzy: float = 0.0):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.TopTools import TopTools_ListOfShape

    op = {"cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common, "fuse": BRepAlgoAPI_Fuse}[kind]()
    args, tl = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(a)
    for t in tools:
        tl.Append(t)
    op.SetArguments(args)
    op.SetTools(tl)
    op.SetRunParallel(False)
    if fuzzy:
        op.SetFuzzyValue(fuzzy)
    op.Build()
    if not op.IsDone():
        raise ValueError(f"a Boolean {kind} failed")
    return op.Shape()


def _pt(p) -> tuple[float, float, float]:
    return (p.X(), p.Y(), p.Z())


def _edge_key(edge) -> tuple:
    """An edge's key from its geometry alone, exact (no rounding): its end points, sorted, and its curve's middle
    point - the same edge made in any order has the same key."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopExp import TopExp

    ends = tuple(sorted(_pt(BRep_Tool.Pnt_s(v)) for v in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge))
                        if not v.IsNull()))
    if BRep_Tool.Degenerated_s(edge):
        return (ends, ())
    c = BRepAdaptor_Curve(edge)
    return (ends, _pt(c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))))


def _children(shape) -> list:
    """A shape's own children as stored (orientation and location relative to it)."""
    from OCP.TopoDS import TopoDS_Iterator

    it, out = TopoDS_Iterator(shape, False, False), []
    while it.More():
        out.append(it.Value())
        it.Next()
    return out


def _canonical_wire(wire, b):
    """The wire's edges in their connected order, started at the edge with the least key (a closed wire is a cycle:
    only where it starts was left to the order OCC made it in)."""
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Wire

    edges = [TopoDS.Edge_s(e) for e in _children(wire)]
    keys = [(_edge_key(e), int(e.Orientation() == TopAbs_REVERSED)) for e in edges]
    left = sorted(range(len(edges)), key=lambda i: keys[i])
    order = [left.pop(0)] if left else []
    while left:
        end = TopExp.LastVertex_s(edges[order[-1]], True)
        nxt = [i for i in left if TopExp.FirstVertex_s(edges[i], True).IsSame(end)]
        if not nxt:                       # not one chain (a wire OCC left unordered): the keys' order, whole
            order = sorted(range(len(edges)), key=lambda i: keys[i])
            break
        order.append(nxt[0])              # `left` is in key order: of several next edges, the least key
        left.remove(nxt[0])
    nw = TopoDS_Wire()
    b.MakeWire(nw)
    for i in order:
        b.Add(nw, edges[i])
    nw.Closed(wire.Closed())
    nw.Orientation(wire.Orientation())
    nw.Location(wire.Location())
    return nw, tuple(keys[i] for i in order)


def canonical(shape):
    """MIXED-2b: the same shape with its solids, shells, faces, wires and edges in an order read from their geometry
    alone (exact keys: the end and middle points of edges), so a shape OCC hands back with the same faces in a
    different order run to run becomes the same shape, byte for byte, and every Boolean and mesh made from it
    repeats. The faces, edges, surfaces and curves are OCC's own, untouched: only their order changes."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_FACE, TopAbs_FORWARD, TopAbs_SHELL, TopAbs_SOLID, TopAbs_WIRE
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell, TopoDS_Solid

    b = BRep_Builder()

    def face(f):
        f = TopoDS.Face_s(f)
        outer = BRepTools.OuterWire_s(f)
        wires = []
        for w in _children(f):
            if w.ShapeType() != TopAbs_WIRE:
                return (), f              # a face holding more than wires is left as it is
            nw, k = _canonical_wire(TopoDS.Wire_s(w), b)
            wires.append((0 if w.IsSame(outer) else 1, k, nw))
        wires.sort(key=lambda r: (r[0], r[1]))
        # an empty copy keeps the face's surface, tolerance, orientation and location; its wires go in as stored
        # (relative to the face), so it is added to with its own orientation and location set aside
        nf = f.EmptyCopied()
        ori, loc = nf.Orientation(), nf.Location()
        nf.Orientation(TopAbs_FORWARD)
        nf.Location(TopLoc_Location())
        for _o, _k, nw in wires:
            b.Add(nf, nw)
        nf.Orientation(ori)
        nf.Location(loc)
        # two faces with the same boundary (two halves of a sphere on one circle) told apart by the surface's point
        # at the middle of the face's parameters
        from OCP.BRepAdaptor import BRepAdaptor_Surface

        u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
        mid = _pt(BRepAdaptor_Surface(f).Value(0.5 * (u0 + u1), 0.5 * (v0 + v1)))
        return (tuple(k for _o, k, _w in wires), mid), nf

    def holder(s):
        kind = s.ShapeType()
        made = {TopAbs_SHELL: TopoDS_Shell, TopAbs_SOLID: TopoDS_Solid, TopAbs_COMPOUND: TopoDS_Compound}[kind]()
        {TopAbs_SHELL: b.MakeShell, TopAbs_SOLID: b.MakeSolid, TopAbs_COMPOUND: b.MakeCompound}[kind](made)
        parts = []
        for c in _children(s):
            t = c.ShapeType()
            if t == TopAbs_FACE:
                parts.append(face(c))
            elif t in (TopAbs_SHELL, TopAbs_SOLID, TopAbs_COMPOUND):
                parts.append(holder(c))
            else:
                return (), s              # holds an edge or a vertex of its own: left as it is
        parts.sort(key=lambda r: r[0])
        for _k, p in parts:
            b.Add(made, p)
        made.Closed(s.Closed())
        made.Orientation(s.Orientation())
        made.Location(s.Location())
        return tuple(k for k, _p in parts), made

    if shape.ShapeType() not in (TopAbs_SHELL, TopAbs_SOLID, TopAbs_COMPOUND):
        return shape
    return holder(shape)[1]


def grown(core, dist: float):
    """The core grown `dist` in every direction (OCC's offset by join, rounded at its edges), in canonical order.

    MIXED-2b, REPEATABLE (2 Nov): on a block with flat sides OCC's offset hands back the same faces, surfaces and
    curves (the same records, bit for bit) in a different order run to run (it orders by memory address; not
    threads - one thread changes nothing), and the solid region's tetrahedra differed by about 0.5% (a stiffened
    panel's mount: 20,421 and 20,321). canonical() puts them in an order read from their geometry, so the grown core,
    every Boolean on it and both meshes repeat byte for byte. (Sweeping only the core's inner faces, which repeats
    too, was tried and dropped: it swallowed a box's corner walls, emptied a flanged tube's shell and made an L
    bracket 7.5% soft.)"""
    from OCP.BRepOffset import BRepOffset_Skin
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape
    from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    # the core comes out of a Boolean with its faces cut where the slabs met them: one face per surface first (the
    # offset of a corner column cut into four faces per side came back with no solid)
    u = ShapeUpgrade_UnifySameDomain(core, True, True, True)
    u.Build()

    # MIXED-2c: OCC's offset as it was; then with its complete intersection (inter=True), when that gives no solid;
    # then of the core with its concave faces tighter than the growth taken out (OCC's defeaturing extends their
    # neighbours to the sharp corner). A concave round tighter than the growth (a boss's 2 mm foot round grown
    # 4 mm) turns inside out in the plain offset, which hands back no solid; the complete intersection trims it (the
    # rounded boss: 40,963 mm3 grown from 19,043), and where it cannot (a whole round along an L's inside corner),
    # the round is taken out first - grown that far, it is inside the grown core anyway (a point within d of the
    # round is within d + r (1 - 1/sqrt 2) of the faces it rounds). The plain offset is tried first, so every core
    # it grew grows as before. Last, the faces' offsets met at sharp edges (join by intersection), not rounded: where
    # a wall leaves a face of the core its cut is the same plane, d off; only round the core's edges and corners does
    # the solid reach further (d / sin(half the edge's angle) at an edge) - a little more solid, never less (a 12 mm
    # block with a half-hole notch grew no other way: 18,543 mm3 from 1,119)
    def tries():
        for inter in (False, True):
            for shape in (u.Shape(), core):
                yield shape, inter, GeomAbs_Arc
        tight = _tight_concave(u.Shape(), dist)
        cut = _defeatured(u.Shape(), tight) if tight else None
        if cut is not None:
            yield cut, False, GeomAbs_Arc
            yield cut, True, GeomAbs_Arc
        for inter in (False, True):
            for shape in (u.Shape(), core):
                yield shape, inter, GeomAbs_Intersection

    for shape, inter, join in tries():
        mk = BRepOffsetAPI_MakeOffsetShape()
        try:
            mk.PerformByJoin(shape, float(dist), 1e-6 * max(dist, 1.0), BRepOffset_Skin, inter, False, join)
        except Exception as exc:  # noqa: BLE001 - OCC raises on some offsets: the next way is tried
            logger.debug("mixed: an offset raised (%s)", exc)
            continue
        if not mk.IsDone():
            continue
        out = _solids(mk.Shape())
        vol = sum(ms._volume(s) for s in out)
        # an offset outward holds more than the core; one turned inside out or empty is no growth
        if out and vol > ms._volume(core):
            return canonical(out[0] if len(out) == 1 else ms._compound(out))
    raise ValueError("a thick core could not be grown")


def _tight_concave(shape, dist: float) -> list:
    """The shape's faces that curve INTO the material (concave, seen from outside) more tightly than `dist`: at the
    middle of each curved face, a principal curvature towards its outward normal over 1 / dist (OCC signs a
    surface's curvature against its own normal; a reversed face's outward normal is the other way)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.TopAbs import TopAbs_REVERSED

    out = []
    for f in ms._faces(shape):
        s = BRepAdaptor_Surface(f)
        if s.GetType() == GeomAbs_Plane:
            continue
        u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
        p = BRepLProp_SLProps(s, 0.5 * (u0 + u1), 0.5 * (v0 + v1), 2, 1e-9)
        if not p.IsCurvatureDefined():
            continue
        sign = -1.0 if f.Orientation() == TopAbs_REVERSED else 1.0
        if max(sign * p.MaxCurvature(), sign * p.MinCurvature()) > 1.0 / dist:
            out.append(f)
    return out


def _defeatured(shape, faces: list):
    """The shape with `faces` taken out by OCC's defeaturing, one thread; None when it cannot."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing

    df = BRepAlgoAPI_Defeaturing()
    df.SetShape(shape)
    for f in faces:
        df.AddFaceToRemove(f)
    df.SetRunParallel(False)
    try:
        df.Build()
    except Exception as exc:  # noqa: BLE001 - no defeatured core: no more ways
        logger.debug("mixed: defeaturing raised (%s)", exc)
        return None
    got = _solids(df.Shape()) if df.IsDone() else []
    return got[0] if len(got) == 1 and len(ms._faces(got[0])) < len(ms._faces(shape)) else None


def _near(shape, p: np.ndarray, tol: float) -> bool:
    return ms._dist(ms._vertex(p), shape) <= tol


def split_body(body, kept: list[tuple[object, float, float]], thick_faces: list, *, scale: float) -> Split:
    """MIXED-1..3 on one solid `body` (its own units; `scale` mm per unit). `kept`: the body's sewn shell mid-faces,
    each (face, wall, offset: the face's distance from its wall's true middle, SHELL-7), units of the body.
    `thick_faces`: the body's faces the extraction called thick or solid. Never raises: a split that cannot be made
    says why (`ok` False)."""
    out = Split()
    try:
        _split(body, kept, thick_faces, scale, out)
    except Exception as exc:  # noqa: BLE001 - the body stays solid, with the reason
        logger.debug("mixed: the split failed (%s)", exc)
        out.ok, out.why = False, f"the thick regions could not be cut out ({exc})"
    return out


def _split(body, kept, thick_faces, scale: float, out: Split) -> None:
    out.body_volume = ms._volume(body)
    t_min = min(t for _f, t, _o in kept)
    tol = 1e-3 * t_min
    slabs = [slab(f, 0.5 * t * (1.0 + SLAB_SLACK) + abs(o)) for f, t, o in kept]
    left = _solids(_bop("cut", body, slabs))
    # MIXED-1: the cores - left-over pieces holding a thick face (a point inside one of the thick faces on the piece)
    marks = [p for p in (ms._inner_point(f) for f in thick_faces) if p is not None]
    cores, dropped = [], 0.0
    for piece in left:
        vol = ms._volume(piece)
        # MIXED-1b: a piece thinner than half the thinnest wall (twice its volume over its area: a slab's own
        # thickness) is no thick region but a rim the slabs left - the 0.6 mm ring round each hole of a 4 mm plate
        # whose bores were read thick; grown it crashed OCC's offset. It is left-over metal as a junction's is
        if any(_near(piece, p, tol) for p in marks) and 2.0 * vol >= CORE_MIN_WALLS * t_min * ms._area(piece):
            cores.append(piece)
        else:
            dropped += vol
    out.dropped_volume = dropped
    if not cores:
        out.why = "no thick region is left once the shell walls are taken out"
        return
    if dropped > LEFT_OVER_MAX * out.body_volume:
        out.why = (f"{100 * dropped / out.body_volume:.0f}% of the metal is neither in a shell wall nor in a thick "
                   "region")
        return
    # MIXED-2: each core grown CUT_WALLS of the thickest wall running into it; the solid is the body's metal inside
    regions = []
    for core in cores:
        walls = [t for (f, t, _o) in kept if ms._dist(f, core) <= 0.5 * t * (1 + SLAB_SLACK) + tol + 0.5 * t]
        d = CUT_WALLS * (max(walls) if walls else t_min)
        out.cut_distance.append(d * scale)
        out.core_volumes.append(ms._volume(core) * scale ** 3)
        regions.append(grown(core, d))
    zone = regions[0] if len(regions) == 1 else _bop("fuse", regions[0], regions[1:])
    solid = _solids(_bop("common", body, [zone]))
    if not solid:
        out.why = "the grown thick regions hold none of the body"
        return
    out.solid = solid[0] if len(solid) == 1 else ms._compound(solid)
    out.cores = len(cores)
    out.solid_volume = sum(ms._volume(s) for s in solid)
    # the trimmed mid-faces: the sewn surface less the grown cores, cut as ONE shape so the faces keep the edges they
    # share (cut face by face, an L bracket's two legs came back with an edge each where they meet: the shell mesh
    # hinged there, a seventh free mode); each piece is owned by the sewn face it lies on
    pieces = ms._faces(_bop("cut", ms._compound([f for f, _t, _o in kept]), [zone]))
    for p in pieces:
        q = ms._inner_point(p)
        if q is None:
            continue
        k = min(range(len(kept)), key=lambda i: (ms._dist(ms._vertex(q), kept[i][0]), i))
        t = kept[k][1]
        # every piece is kept, however narrow: the sewn surface holds junction strips half a wall wide (a stiffener's
        # mid-face carried down to the plate's mid-plane), and dropping them parted a stiffened panel's stiffeners
        # from its plate
        out.shell_faces.append(p)
        out.shell_from.append(k)
        out.shell_volume += ms._area(p) * t
    # the cut faces: the solid's faces inside the body (no point of theirs on the body's own skin)
    skin = ms._compound(ms._faces(body))
    for f in ms._faces(out.solid):
        p = ms._inner_point(f)
        if p is None or _near(skin, p, tol):
            continue
        walls = [t for (sf, t, _o) in kept if ms._dist(sf, f) <= tol + 0.5 * t]
        out.cut_faces.append(f)
        out.cut_walls.append(max(walls) if walls else t_min)
    held = (out.solid_volume + out.shell_volume) / out.body_volume if out.body_volume > 0 else 0.0
    if not VOLUME_BAND[0] <= held <= VOLUME_BAND[1]:
        out.why = f"the split holds {100 * held:.0f}% of the body's metal (outside {VOLUME_BAND})"
        return
    if not out.shell_faces:
        out.why = "no shell wall is left once the thick regions are cut out"
        return
    out.ok = True


__all__ = ["CUT_WALLS", "Split", "grown", "slab", "split_body"]
