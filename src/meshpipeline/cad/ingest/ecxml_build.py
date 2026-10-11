# Responsibility: Turn an ECXML model into the canonical multi-solid STEP a conjugate-heat-transfer mesh is built from: one named solid per part, and the air around them - and prove it is the file's model.
# Owns: each object's exact OpenCASCADE solid, the overlap rule (later overwrites earlier), vents opened in the walls they sit in, the air region (domain minus every solid), the tolerances (from the file's own precision and its smallest gap), the fidelity checks, the build report and the physics sidecar.
# Boundaries: the physics is carried, never turned into solver settings; nothing outside the uploaded file is read; a model the checks cannot vouch for is refused with the reason, never meshed wrong.
# Collaborates with: cad/ingest/ecxml.py (the model), cad/ingest/canonical.py (the caller), cad/regions.py (reads the solid names back).
"""ECXML -> named solids + air, written as one STEP, with the physics beside it.

What becomes geometry (each a named solid, one region of a multi-region mesh):

* solid3dBlock, printedCircuitBoard, twoResistorModel: their box;
* solidCylinder: the cylinder whose axis is normal to its plane, filling its bounding box
  (elliptical when the two in-plane sizes differ);
* enclosure: the box minus its inside, walls ``wallThickness`` thick;
* heatsink: its solid3dBlock parts fused into one solid per material;
* solid2dBlock inside the domain with a thickness: a thin plate of that thickness (a plate on the
  domain boundary is a wall patch instead, and a zero-thickness plate cannot be a volume);
* the air: the solution domain (or, when the file sets none, the bounding box of every object, as
  JEP181A's example does) minus every solid - one fluid region per connected air space.

Where solids overlap, the object later in the file wins (JEP181A 4.5.1, Flotherm, 6SigmaET,
scSTREAM); for Icepak a solid fully inside another wins, as Icepak does by default. A grille, a 2D
fan or an axial fan that sits in an enclosure wall or a plate opens that wall over its own outline:
that is what a vent in a housing is.

What is carried as metadata only (the mesh does not mark it; a solver step or the user uses it):
materials, powers, ambient conditions, fans and their flow, grilles and flow resistances with their
loss coefficients, volume and surface heat sources, the 2-resistor models' resistances, monitor
points, and the boundary patches the 2D objects on the domain's faces stand for.

Fidelity. The solids are built in millimetres - OpenCASCADE's fixed tolerances (1e-7 model units)
are then 0.1 nanometre, far below any feature of an electronics model - and every build is checked
before it is written:

* tolerances come from the file: coordinates closer than the file's own number precision (float32
  at the model's size, at least the 10 nm an 8-decimal value can say) are one plane; the booleans'
  fuzzy value stays at least ten times below the smallest real gap and the thinnest part; a model
  whose smallest gap the file's precision cannot tell from contact is refused;
* every solid's volume is the file's, exactly where it can be computed independently (boxes,
  cylinders, enclosures minus their cut-outs, heat sinks, and every overlap between boxes), and
  its bounding box is the file's; the fuse changes no volume;
* the solids and the air fill the domain exactly;
* every active object of the file is accounted for, once;
* the fuse shares a face only between parts the file makes touch, and shares every true contact.

Everything that was resolved, dropped or not supported goes into a build report the user sees.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from meshpipeline.cad.ingest.ecxml import (
    SPEC,
    TWO_D,
    Domain,
    EcxmlError,
    EcxmlModel,
    Obj,
    axis_of_plane,
    read_ecxml,
    sign_of_plane,
)

#: Written beside the canonical STEP: the ECXML physics, region by region.
SIDECAR_SUFFIX = ".ecxml.json"
SIDECAR_VERSION = 2

#: OpenCASCADE builds and the STEP is written in millimetres (model units per metre).
UNIT = 1000.0
#: The finest length an 8-decimal ECXML value (JEP181A's examples) can state.
PRINT_RESOLUTION_M = 1e-8
#: Flotherm writes float32 values (0.300000011920929): two writes of one plane differ by a few
#: units in the last place - 2^-24 of the coordinate each. Four of them are the file's noise.
FLOAT32_NOISE = 4 * 2.0 ** -24
#: The smallest gap and the thinnest part must be this many times the tolerance used on them.
GAP_FACTOR = 10.0
#: How far a vent's cutter reaches past the wall it opens, at least.
CUT_REACH_M = 1e-5
#: Relative error allowed between a solid's volume and the file's.
VOLUME_RTOL = 1e-6
#: THE SIZE BOUND - from measured cost, not a part count. Converting and checking a model (build,
#: fidelity checks, STEP, the geometry check's scout) was timed on generated models (8-core
#: machine, 2026-10-06; PR #163). All on one board: 28 s at 500 parts, 72 s at 1,000, 276 s at
#: 2,000. Spread over cards of 100 parts: 82 s at 2,020, 640 s at 4,040. The booleans grow with
#: the footprints one face carries (a board's top face is cut once per part on it, and
#: OpenCASCADE's face builder sorts its holes against each other); the STEP write and the scout
#: that reads the STEP back grow with the square of the part count however the parts are laid
#: out. Memory: ~0.5 MB per part above a 700 MB base (1.6 GB at 2,000, 1.9 GB at 4,040). Fitted
#: from above: a*N + b*N^2 + c*sum(parts touching one part)^2 seconds. A model is converted when
#: its predicted cost fits two thirds of the geometry check's 900 s limit and 6 GB: about 2,900
#: parts on one board, 3,500 spread out. Above that this exact, face-for-face conversion is the
#: wrong tool, and the refusal says so.
SECONDS_PER_PART = 0.03
SECONDS_PER_PART_SQUARED = 3.9e-5
SECONDS_PER_CONTACT_SQUARED = 2.0e-5
MB_BASE = 700.0
MB_PER_PART = 0.5
TIME_BUDGET_S = 600.0
MEMORY_BUDGET_MB = 6000.0
#: Narrow gaps are listed up to this fraction of the model's diagonal (two background cells of a
#: mesh that starts at a fortieth of it): anything wider needs no refinement to stay open.
THIN_GAP_LIMIT_REL = 1 / 20
MAX_THIN_GAPS = 2000
FLUID_NAME = "air"
#: Box kinds: a solid that is exactly its box.
BOX_KINDS = ("solid3dBlock", "printedCircuitBoard", "twoResistorModel", "solid2dBlock")

_AXES = "xyz"


class FidelityError(EcxmlError):
    """The built solids would not be the file's model. The message says which check failed."""


class ModelTooLargeError(EcxmlError):
    """Converting the model exactly would cost more time or memory than a conversion is allowed."""


class _Box:
    __slots__ = ("hi", "lo")

    def __init__(self, lo, hi) -> None:
        self.lo = tuple(float(v) for v in lo)
        self.hi = tuple(float(v) for v in hi)

    def overlaps(self, other: _Box, tol: float) -> bool:
        """Interiors overlap by more than `tol` on every axis (touching faces do not)."""
        return all(min(self.hi[i], other.hi[i]) - max(self.lo[i], other.lo[i]) > tol
                   for i in range(3))

    def inside(self, other: _Box, tol: float) -> bool:
        return all(self.lo[i] >= other.lo[i] - tol and self.hi[i] <= other.hi[i] + tol
                   for i in range(3))

    def common(self, other: _Box) -> _Box | None:
        lo = [max(self.lo[i], other.lo[i]) for i in range(3)]
        hi = [min(self.hi[i], other.hi[i]) for i in range(3)]
        return _Box(lo, hi) if all(hi[i] > lo[i] for i in range(3)) else None

    def volume(self) -> float:
        return math.prod(max(0.0, self.hi[i] - self.lo[i]) for i in range(3))

    def as_dict(self) -> dict:
        return {"min": list(self.lo), "max": list(self.hi)}


@dataclass(frozen=True)
class _Tol:
    noise: float                  # m: coordinates closer than this are one plane
    fuzzy: float                  # m: OpenCASCADE's fuzzy value for every boolean
    gap: float                    # m: the smallest real gap between two parts (inf: none)
    gap_where: str
    thick: float                  # m: the thinnest part
    thick_where: str
    snapped: int = 0              # coordinates moved onto a shared plane
    snap_shift: float = 0.0       # m: the largest such move
    contacts_max: int = 0         # the most parts touching one part (footprints on one face)
    contacts_sq: int = 0          # the sum over parts of (parts touching it)^2
    contacts_where: str = ""


@dataclass
class _Part:
    """One solid to be written: a region of the mesh."""
    name: str
    kind: str
    objects: list[Obj]
    box: _Box
    order: int
    material: str | None
    make: Callable[[_Tol], object]    # builds the solid once the tolerances are known
    shape: object = None
    power: float = 0.0
    wall: bool = False            # an enclosure or a plate: devices in it open it
    notes: list[str] = field(default_factory=list)
    walls: list[tuple[int, float, float, _Box]] = field(default_factory=list)  # axis, lo, hi, slab
    blocks: list[_Box] = field(default_factory=list)     # a heat sink's blocks
    cutters: list[tuple[str, _Box, int]] = field(default_factory=list)  # vents: kind, box, axis
    winners: list[_Part] = field(default_factory=list)   # parts that overwrite some of it
    clipped: bool = False
    dropped: str = ""             # why it is not meshed, when it is not


@dataclass
class EcxmlBuild:
    named: list[tuple[str, object]]          # (region name, OCC shape in mm) in write order
    sidecar: dict
    notes: list[str]
    report: list[str] = field(default_factory=list)

    @property
    def region_names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.named)


# ------------------------------------------------------------------------------ OCC helpers -
def _occ_box(box: _Box):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(*(v * UNIT for v in box.lo)),
                               gp_Pnt(*(v * UNIT for v in box.hi))).Solid()


def _occ_cylinder(box: _Box, axis: int, noise: float):
    """The cylinder along `axis` that fills `box`: circular when the two cross sizes agree,
    elliptical otherwise - an exact ellipse swept along the axis, not an approximation."""
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Vec

    a, b, h = _cylinder_axes(box, axis)
    cross = [i for i in range(3) if i != axis]
    d = [0.0, 0.0, 0.0]
    d[axis] = 1.0
    base = [(box.lo[i] + box.hi[i]) / 2 * UNIT for i in range(3)]
    base[axis] = box.lo[axis] * UNIT
    if abs(a - b) <= noise:
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*d)), a * UNIT,
                                        h * UNIT).Solid()
    major = cross[0] if a > b else cross[1]
    x = [0.0, 0.0, 0.0]
    x[major] = 1.0
    ellipse = gp_Elips(gp_Ax2(gp_Pnt(*base), gp_Dir(*d), gp_Dir(*x)), max(a, b) * UNIT,
                       min(a, b) * UNIT)
    face = BRepBuilderAPI_MakeFace(
        BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(ellipse).Edge()).Wire()).Face()
    sweep = [0.0, 0.0, 0.0]
    sweep[axis] = h * UNIT
    return BRepPrimAPI_MakePrism(face, gp_Vec(*sweep)).Shape()


def _box_maker(box: _Box) -> Callable[[_Tol], object]:
    def make(_tol: _Tol) -> object:
        return _occ_box(box)
    return make


def _cylinder_maker(box: _Box, axis: int) -> Callable[[_Tol], object]:
    def make(tol: _Tol) -> object:
        return _occ_cylinder(box, axis, tol.noise)
    return make


def _cylinder_axes(box: _Box, axis: int) -> tuple[float, float, float]:
    cross = [i for i in range(3) if i != axis]
    return ((box.hi[cross[0]] - box.lo[cross[0]]) / 2, (box.hi[cross[1]] - box.lo[cross[1]]) / 2,
            box.hi[axis] - box.lo[axis])


def _list(shapes):
    from OCP.TopTools import TopTools_ListOfShape

    out = TopTools_ListOfShape()
    for s in shapes:
        out.Append(s)
    return out


def _boolean(kind: str, args, tools, tol: _Tol):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

    algo = {"cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common, "fuse": BRepAlgoAPI_Fuse}[kind]()
    algo.SetArguments(_list(args))
    algo.SetTools(_list(tools))
    if tol.fuzzy > 0:
        algo.SetFuzzyValue(tol.fuzzy * UNIT)
    algo.SetRunParallel(False)
    algo.Build()
    if not algo.IsDone() or algo.Shape().IsNull():
        raise EcxmlError(f"OpenCASCADE could not {kind} the parts (a {kind} of "
                         f"{len(args)} + {len(tools)} shapes failed)")
    return algo.Shape()


def _unify(shape):
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    try:
        u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
        u.Build()
        return u.Shape()
    except Exception:  # noqa: BLE001 - an unmerged face is still exact
        return shape


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def _props(shape):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props


def _volume(shape) -> float:
    """m3"""
    return abs(float(_props(shape).Mass())) / UNIT ** 3


def _centroid(shape) -> list[float]:
    """m"""
    c = _props(shape).CentreOfMass()
    return [float(c.X()) / UNIT, float(c.Y()) / UNIT, float(c.Z()) / UNIT]


def _area(shape) -> float:
    """m2"""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return abs(float(props.Mass())) / UNIT ** 2


def _bounds(shape) -> _Box:
    """The shape's exact bounding box, m."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return _Box([x0 / UNIT, y0 / UNIT, z0 / UNIT], [x1 / UNIT, y1 / UNIT, z1 / UNIT])


def _compound(shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for s in shapes:
        builder.Add(comp, s)
    return comp


def _unique_namer() -> Callable[..., str]:
    from meshpipeline.contracts.patch_names import MAX_NAME_LENGTH, mesh_safe, unreserved

    taken: set[str] = set()

    def name(raw: str, fallback: str = "part") -> str:
        base = unreserved(mesh_safe(raw, fallback=fallback))
        cand, k = base, 1
        while cand.casefold() in taken:
            k += 1
            suffix = f"_{k}"
            cand = base[:MAX_NAME_LENGTH - len(suffix)] + suffix
        taken.add(cand.casefold())
        return cand

    return name


def _um(m: float) -> str:
    """A length for a person: micrometres below a millimetre, millimetres above."""
    return f"{m * 1e6:.3g} um" if m * 1e6 < 999.5 else f"{m * 1e3:.4g} mm"


def _material(p: _Part) -> list[_Box]:
    """The boxes a part's material fills: an enclosure's wall slabs, a heat sink's blocks, or
    its own box."""
    if p.kind == "enclosure" and p.walls:
        return [w[3] for w in p.walls]
    return p.blocks or [p.box]


# ------------------------------------------------------------------------------ box algebra -
def _union_volume(clip: _Box, boxes: list[_Box]) -> float:
    """The volume of `clip` covered by the union of `boxes` - exact, by cutting `clip` on every
    box bound into a grid and summing the covered cells."""
    import numpy as np

    parts = [c for c in (clip.common(b) for b in boxes) if c is not None]
    if not parts:
        return 0.0
    axes = []
    for i in range(3):
        axes.append(np.unique(np.array([clip.lo[i], clip.hi[i]]
                                       + [v for p in parts for v in (p.lo[i], p.hi[i])])))
    covered = np.zeros((len(axes[0]) - 1, len(axes[1]) - 1, len(axes[2]) - 1), dtype=bool)
    for p in parts:
        idx = [(int(np.searchsorted(axes[i], p.lo[i])), int(np.searchsorted(axes[i], p.hi[i])))
               for i in range(3)]
        covered[idx[0][0]:idx[0][1], idx[1][0]:idx[1][1], idx[2][0]:idx[2][1]] = True
    d = [np.diff(a) for a in axes]
    cell = d[0][:, None, None] * d[1][None, :, None] * d[2][None, None, :]
    return float((cell * covered).sum())


def _pair_scan(lo, hi, noise: float, *, rows: int = 256):
    """Every pair of boxes, a block of rows at a time: the smallest positive distance (with its
    pair), the pairs whose interiors overlap, and the pairs that touch face to face."""
    import numpy as np

    n = len(lo)
    best, best_pair = math.inf, (-1, -1)
    overlap: list[tuple[int, int]] = []
    touch: list[tuple[int, int]] = []
    for start in range(0, n, rows):
        i = np.arange(start, min(n, start + rows))
        a_lo, a_hi = lo[i][:, None, :], hi[i][:, None, :]
        ext = np.minimum(a_hi, hi[None, :, :]) - np.maximum(a_lo, lo[None, :, :])  # overlap length
        sep = np.maximum(-ext, 0.0)
        dist = np.sqrt((sep ** 2).sum(axis=2))
        upper = np.arange(n)[None, :] > i[:, None]
        pos = np.where(upper & (dist > noise), dist, np.inf)
        k = int(np.argmin(pos))
        if pos.flat[k] < best:
            best = float(pos.flat[k])
            best_pair = (int(i[k // n]), int(k % n))
        inner = upper & (ext > noise).all(axis=2)
        overlap += [(int(i[r]), int(c)) for r, c in zip(*np.nonzero(inner))]
        flat = (np.abs(ext) <= noise).sum(axis=2) == 1
        face = upper & flat & ((ext > noise).sum(axis=2) == 2)
        touch += [(int(i[r]), int(c)) for r, c in zip(*np.nonzero(face))]
    return best, best_pair, overlap, touch


# ------------------------------------------------------------------------------ tolerances --
def _noise(model: EcxmlModel, objs: list[Obj]) -> float:
    """The file's own number precision at this model's size, metres: float32 noise at the
    largest coordinate the build uses. That is the domain's and those of the objects that reach
    into it, clipped to it. An object wholly outside the solution domain is never built, and a
    monitor point is no geometry, so neither coarsens the tolerance. (A real Flotherm export
    keeps parts metres outside a 50 mm domain and a probe at z = 33 m; counted, they made the
    precision 8 um and every 10 um gap of the model unreadable.)"""
    d = model.domain
    lo_d = hi_d = None
    largest = 0.0
    if d is not None:
        lo_d = d.location
        hi_d = tuple(d.location[i] + d.size[i] for i in range(3))
        largest = max(*(abs(v) for v in lo_d), *(abs(v) for v in hi_d))
    for o in objs:
        if o.location is None or o.size is None or o.kind in (
                "assembly", "heatsink", "monitorPoint", "externalMcadFile"):
            continue
        lo, hi = _extent(o)
        if lo_d is not None and hi_d is not None:
            if any(hi[i] < lo_d[i] or lo[i] > hi_d[i] for i in range(3)):
                continue                       # wholly outside the domain: never built
            lo = [min(max(lo[i], lo_d[i]), hi_d[i]) for i in range(3)]
            hi = [min(max(hi[i], lo_d[i]), hi_d[i]) for i in range(3)]
        largest = max(largest, *(abs(v) for v in lo), *(abs(v) for v in hi))
    return max(PRINT_RESOLUTION_M, FLOAT32_NOISE * largest)


def _snapper(values_per_axis: list[list[float]], noise: float):
    reps: list[list[float]] = []
    for vals in values_per_axis:
        out: list[float] = []
        for v in sorted(vals):
            if not out or v - out[-1] > noise:
                out.append(v)
        reps.append(out)
    moved = {"count": 0, "max": 0.0}

    def snap(axis: int, v: float) -> float:
        import bisect

        r = reps[axis]
        i = bisect.bisect_left(r, v)
        near = [r[j] for j in (i - 1, i) if 0 <= j < len(r) and abs(r[j] - v) <= noise]
        out = min(near, key=lambda rep: abs(rep - v)) if near else v
        if out != v:
            moved["count"] += 1
            moved["max"] = max(moved["max"], abs(out - v))
        return out

    return snap, moved


def _snap_function(model: EcxmlModel, objs: list[Obj], noise: float):
    """An object's box with every bound moved onto the plane it is meant to share."""
    by_axis: list[list[float]] = [[], [], []]
    for o in objs:
        if o.location is not None and o.size is not None:
            lo, hi = _extent(o)
            for i in range(3):
                by_axis[i] += [lo[i], hi[i]]
    if model.domain is not None:
        d = model.domain
        for i in range(3):
            by_axis[i] += [d.location[i], d.location[i] + d.size[i]]
    snap, moved = _snapper(by_axis, noise)

    def snapped(o: Obj) -> _Box:
        return _Box([snap(i, o.lo[i]) for i in range(3)], [snap(i, o.hi[i]) for i in range(3)])

    return snapped, moved


def _measure(parts: list[_Part], domain: _Box, noise: float, moved: dict) -> _Tol:
    """The smallest real gap and the thinnest part, and the fuzzy value they allow."""
    import numpy as np

    boxes: list[_Box] = []
    owners: list[str] = []
    owner_ix: list[int] = []
    for k, p in enumerate(parts):
        label = p.objects[0].props.get("heatsink") or p.objects[0].label
        if p.walls:
            pieces = [w[3] for w in p.walls]
        elif p.blocks:
            pieces = p.blocks
        else:
            pieces = [p.box]
        for b in pieces:
            c = b.common(domain)
            if c is not None:
                boxes.append(c)
                owners.append(label)
                owner_ix.append(k)
    gap, gap_where = math.inf, ""
    touching: list[set[int]] = [set() for _ in parts]
    thick, thick_where = math.inf, ""
    for box, who in zip(boxes, owners):
        t = min(box.hi[i] - box.lo[i] for i in range(3))
        if t < thick:
            thick, thick_where = t, who
        for i in range(3):                     # an air sliver between a part and the domain side
            for g in (box.lo[i] - domain.lo[i], domain.hi[i] - box.hi[i]):
                if noise < g < gap:
                    gap, gap_where = g, f"{who} and the domain's side"
    if len(boxes) > 1:
        lo = np.array([b.lo for b in boxes])
        hi = np.array([b.hi for b in boxes])
        best, (ga, gb), overlap, touch = _pair_scan(lo, hi, noise)
        for ta, tb in touch:                   # who carries whose footprint
            a, b = owner_ix[ta], owner_ix[tb]
            if a != b:
                touching[a].add(b)
                touching[b].add(a)
        if best < gap:
            # the pair the scan found - not the last touching pair of the loop above
            gap, gap_where = best, f"{owners[ga]} and {owners[gb]}"
        for ia, ib in overlap:                 # what an overlap leaves of the earlier part
            for i in range(3):
                for t in (abs(boxes[ia].lo[i] - boxes[ib].lo[i]),
                          abs(boxes[ia].hi[i] - boxes[ib].hi[i])):
                    if noise < t < thick:
                        thick, thick_where = t, (f"the part of {owners[ia]} or {owners[ib]} "
                                                 "their overlap leaves")
    fuzzy = min(noise, gap / GAP_FACTOR, thick / GAP_FACTOR)
    busiest = max(range(len(parts)), key=lambda k: len(touching[k]), default=-1)
    return _Tol(noise=noise, fuzzy=fuzzy, gap=gap, gap_where=gap_where, thick=thick,
                thick_where=thick_where, snapped=int(moved["count"]), snap_shift=float(moved["max"]),
                contacts_max=len(touching[busiest]) if busiest >= 0 else 0,
                contacts_sq=sum(len(t) ** 2 for t in touching),
                contacts_where=(parts[busiest].objects[0].props.get("heatsink")
                                or parts[busiest].objects[0].label) if busiest >= 0 else "")


def _thinnest(p: _Part, domain: _Box, noise: float) -> tuple[float, str]:
    """How thin the part's region is anywhere, from the file's own numbers: the smallest side of
    a box its material fills (a layer, a wall, a fin), or of the sliver a part overwriting it
    leaves beside that part. A mesh must put cells across this to keep the region whole."""
    best, what = math.inf, ""
    mine = [c for c in (b.common(domain) for b in _material(p)) if c is not None]
    for b in mine:
        for i in range(3):
            t = b.hi[i] - b.lo[i]
            if noise < t < best:
                best, what = t, f"its {_AXES[i]} size"
    taken = [qb for q in p.winners for qb in _material(q)]
    for q in p.winners:
        for qb in _material(q):
            for b in mine:
                c = b.common(qb)
                if c is None:
                    continue
                for i in range(3):
                    for t, lo_i, hi_i in ((c.lo[i] - b.lo[i], b.lo[i], c.lo[i]),
                                          (b.hi[i] - c.hi[i], c.hi[i], b.hi[i])):
                        if not noise < t < best:
                            continue
                        # the slab beside q, over q's footprint - unless other winners fill it
                        slab = _Box([lo_i if k == i else c.lo[k] for k in range(3)],
                                    [hi_i if k == i else c.hi[k] for k in range(3)])
                        if _union_volume(slab, taken) < slab.volume() * (1 - 1e-9):
                            best, what = t, f"the sliver {q.name} leaves of it"
    return best, what


def _thin_gaps(parts: list[_Part], domain: _Box, noise: float, limit: float) -> list[dict]:
    """The narrow air where parts face each other across a gap (a fin pitch counts, within one
    part), and between a part and the domain's side: each pair's smallest such gap below `limit`.
    A mesh must put cells across that air, or the two faces come out touching."""
    import numpy as np

    boxes: list[_Box] = []
    owner: list[int] = []
    for k, p in enumerate(parts):
        for b in _material(p):
            c = b.common(domain)
            if c is not None:
                boxes.append(c)
                owner.append(k)
    best: dict[tuple[str, str], float] = {}
    n = len(boxes)
    lo = np.array([b.lo for b in boxes]).reshape(-1, 3)
    hi = np.array([b.hi for b in boxes]).reshape(-1, 3)

    def air(slab: _Box) -> bool:
        """Some of the slab is air: the parts in it do not fill it."""
        near = np.nonzero(((lo < slab.hi) & (hi > slab.lo)).all(axis=1))[0]
        return _union_volume(slab, [boxes[int(k)] for k in near]) < slab.volume() * (1 - 1e-9)

    for k, box in zip(owner, boxes):
        side = (parts[k].name, "domain")
        for i in range(3):
            for g, e_lo, e_hi in ((box.lo[i] - domain.lo[i], domain.lo[i], box.lo[i]),
                                  (domain.hi[i] - box.hi[i], box.hi[i], domain.hi[i])):
                if noise < g < min(limit, best.get(side, math.inf)) and air(_Box(
                        [e_lo if j == i else box.lo[j] for j in range(3)],
                        [e_hi if j == i else box.hi[j] for j in range(3)])):
                    best[side] = g
    for start in range(0, n, 256):
        rows_ix = np.arange(start, min(n, start + 256))
        ext = (np.minimum(hi[rows_ix][:, None, :], hi[None])
               - np.maximum(lo[rows_ix][:, None, :], lo[None]))
        # facing: they overlap in two directions and stand apart in the third
        facing = ((ext > noise).sum(axis=2) == 2) & ((ext < -noise).sum(axis=2) == 1)
        gap = np.where(facing, (-ext).max(axis=2), np.inf)
        hit = (np.arange(n)[None, :] > rows_ix[:, None]) & (gap < limit)
        for r, c in zip(*np.nonzero(hit)):
            ia, ib = int(rows_ix[r]), int(c)
            width = float(gap[r, c])
            na, nb = sorted((parts[owner[ia]].name, parts[owner[ib]].name))
            if width >= best.get((na, nb), math.inf):
                continue
            ax = int(np.argmin(ext[r, c]))
            s_lo = [float(max(lo[ia][j], lo[ib][j])) for j in range(3)]
            s_hi = [float(min(hi[ia][j], hi[ib][j])) for j in range(3)]
            s_lo[ax] = float(min(hi[ia][ax], hi[ib][ax]))
            s_hi[ax] = float(max(lo[ia][ax], lo[ib][ax]))
            if air(_Box(s_lo, s_hi)):        # a layer of another part between them is no gap
                best[(na, nb)] = width
    ranked = sorted(best.items(), key=lambda kv: kv[1])
    return [{"between": list(pair), "gap_m": w} for pair, w in ranked[:MAX_THIN_GAPS]]


def predicted_cost(n_parts: int, contacts_sq: int) -> tuple[float, float]:
    """Seconds and megabytes to convert and check a model (on an 8-core machine), from the
    measured cost model above: per part, per part squared (the STEP and its re-reading), and per
    (parts touching one part)^2 summed over the parts (a face carrying k footprints is cut k
    ways, and its k holes are sorted against each other)."""
    seconds = (SECONDS_PER_PART * n_parts + SECONDS_PER_PART_SQUARED * n_parts ** 2
               + SECONDS_PER_CONTACT_SQUARED * contacts_sq)
    return seconds, MB_BASE + MB_PER_PART * n_parts


def _check_affordable(n_parts: int, tol: _Tol) -> None:
    seconds, megabytes = predicted_cost(n_parts, tol.contacts_sq)
    if seconds <= TIME_BUDGET_S and megabytes <= MEMORY_BUDGET_MB:
        return
    what = (f"about {seconds / 60:.0f} minutes" if seconds > TIME_BUDGET_S
            else f"about {megabytes / 1024:.1f} GB of memory")
    raise ModelTooLargeError(
        f"the model has {n_parts:,} solid parts ({tol.contacts_max:,} of them on one part, "
        f"{tol.contacts_where}); building them exactly, face for face, would take {what} - more "
        f"than the {TIME_BUDGET_S / 60:.0f} minutes and {MEMORY_BUDGET_MB / 1024:.0f} GB a "
        "conversion is allowed. The snap-grid conversion (Option B), which places the parts on "
        "a grid without joining them face for face, is the way on for a model this size; until "
        "it is available, export a sub-assembly (one board, one module) from the authoring tool")


def _check_resolvable(tol: _Tol, model_size: float) -> None:
    for what, value, where in (("gap", tol.gap, tol.gap_where),
                               ("thickness", tol.thick, tol.thick_where)):
        if value < GAP_FACTOR * tol.noise:
            raise FidelityError(
                f"the smallest {what} in the model, {_um(value)} ({where}), is within "
                f"{GAP_FACTOR:g} times the file's own number precision at this model's size "
                f"({_um(tol.noise)} for a {_um(model_size)} model), so it cannot be told apart "
                "from contact. Make that gap or part clearly larger, or put the parts in contact, "
                "in the authoring tool - or export the sub-assembly that holds it, whose smaller "
                "size gives finer precision")


# ------------------------------------------------------------------------------ the build ---
def build(model: EcxmlModel) -> EcxmlBuild:
    objs = model.active_objects
    notes: list[str] = list(model.notes)
    report: list[str] = []
    noise = _noise(model, objs)
    snapped, moved = _snap_function(model, objs, noise)
    domain_box, domain_source = _domain(model.domain, objs, snapped, noise)
    if domain_box is None:
        raise EcxmlError("the ECXML model holds no object with a size and no solution domain, so "
                         "there is nothing to mesh")
    model_size = math.dist(domain_box.lo, domain_box.hi)
    namer = _unique_namer()
    fluid_base = namer(FLUID_NAME)

    parts: list[_Part] = []
    records: dict[str, list] = {k: [] for k in (
        "patches", "fans", "grilles", "flow_resistances", "volume_heat_sources",
        "surface_heat_sources", "baffles", "monitor_points", "compact_models", "not_built",
        "inactive")}
    devices: list[tuple[Obj, _Box]] = []        # objects that open the walls they sit in
    fate: dict[int, str] = {}                   # file order -> where the object went

    for o in model.objects:
        if not o.active and o.kind not in ("assembly", "heatsink"):
            records["inactive"].append({"name": o.name, "kind": o.kind, "path": list(o.path)})
    heatsink_parts: dict[tuple, list[Obj]] = {}
    for o in objs:
        k = o.kind
        if k in ("assembly", "heatsink"):
            continue
        if o.props.get("heatsink"):
            heatsink_parts.setdefault((o.path, o.material), []).append(o)
            continue
        if k == "monitorPoint":
            assert o.location is not None
            records["monitor_points"].append({"name": o.name, "path": list(o.path),
                                              "location_m": list(o.location)})
            fate[o.order] = "monitor point"
            continue
        if k == "externalMcadFile":
            records["not_built"].append({
                "name": o.name, "kind": k, "path": list(o.path),
                "file_name": o.props.get("file_name"), "material": o.material,
                "power_W": o.power,
                "why": ("it refers to an external MCAD file, which an upload does not carry; "
                        "Hexera never opens a path or URL named inside a file. Add that geometry "
                        "to the model in the authoring tool, or upload it as STEP on its own")})
            notes.append(f"{o.label}: the external MCAD file {str(o.props.get('file_name'))[:80]!r} "
                         "is not part of the upload, so that part is left out")
            fate[o.order] = "not built (external MCAD file)"
            continue
        box = snapped(o)
        if k in ("solid3dBlock", "printedCircuitBoard", "twoResistorModel"):
            part = _Part(name="", kind=k, objects=[o], box=box, order=o.order,
                         material=o.material or None, power=o.power or 0.0,
                         make=_box_maker(box))
            if k == "printedCircuitBoard":
                part.notes.append(f"printed circuit board in the {o.plane[1:].upper()} plane")
            if k == "twoResistorModel":
                axis = axis_of_plane(o.plane)
                case = ("+" if sign_of_plane(o.plane) > 0 else "-") + _AXES[axis]
                board = ("-" if sign_of_plane(o.plane) > 0 else "+") + _AXES[axis]
                records["compact_models"].append({
                    "name": o.name, "path": list(o.path), "kind": "two_resistor (JESD15-3)",
                    "box_m": box.as_dict(), "power_W": o.power,
                    "theta_jc_K_W": o.props["theta_jc_K_W"],
                    "theta_jb_K_W": o.props["theta_jb_K_W"],
                    "case_face": case, "board_face": board})
                part.notes.append("2-resistor compact model: a block with no material of its own; "
                                  f"case face {case}, board face {board}")
            parts.append(part)
        elif k == "solidCylinder":
            axis = axis_of_plane(o.plane)
            parts.append(_Part(name="", kind=k, objects=[o], box=box, order=o.order,
                               material=o.material or None, power=o.power or 0.0,
                               make=_cylinder_maker(box, axis)))
        elif k == "enclosure":
            parts.append(_enclosure(o, box, noise))
        elif k == "solid2dBlock":
            if not _plate(o, box, domain_box, noise, parts, records, notes):
                fate[o.order] = "boundary patch or baffle"
        elif k in ("grille", "rectangular2dFan", "source2dBlock"):
            _face_object(o, box, domain_box, noise, records)
            if k != "source2dBlock":
                devices.append((o, box))
            fate[o.order] = "boundary patch or internal plane"
        elif k == "axial3dFan":
            axis = axis_of_plane(o.plane)
            cross = [i for i in range(3) if i != axis]
            centre = [(box.lo[i] + box.hi[i]) / 2 for i in range(3)]
            records["fans"].append({
                "name": o.name, "path": list(o.path), "kind": "axial_3d",
                "box_m": box.as_dict(), "centre_m": centre,
                "axis": o.plane, "flow_direction": _direction(o.plane),
                "outer_diameter_m": [box.hi[i] - box.lo[i] for i in cross],
                "hub_diameter_m": o.props["hub_diameter_m"],
                "hub_size_note": "JEP181A 4.4.14 defines hubSize as the hub diameter; Icepak's "
                                 "export table calls the same field a radius",
                "depth_m": box.hi[axis] - box.lo[axis], "flow": o.props["flow"],
                "meshed_as": "air: the fan's swept volume is part of the fluid region; a solver "
                             "applies the fan there (hub to tip)"})
            devices.append((o, box))
            fate[o.order] = "fan (air, in the sidecar)"
        elif k == "sourceBlock":
            records["volume_heat_sources"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "power_W": o.power, "meshed_as": "not marked: the power is applied in the box, "
                                                 "in whatever region it overlaps"})
            fate[o.order] = "volume heat source (in the sidecar)"
        elif k == "flowResistance":
            records["flow_resistances"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "loss_coefficient_xyz": o.props["loss_coefficient_xyz"],
                "free_area_ratio_xyz": o.props["free_area_ratio_xyz"],
                "meshed_as": "air: a porous zone in the fluid region, selected by its box"})
            fate[o.order] = "flow resistance (air, in the sidecar)"
    for (_path, material), group in heatsink_parts.items():
        parts.append(_heatsink(group, material, snapped))
    notes += _records_in_domain(records, domain_box, noise)

    tol = _measure(parts, domain_box, noise, moved)
    _check_affordable(len(parts), tol)
    _check_resolvable(tol, model_size)
    for p in parts:
        p.shape = p.make(tol)
        for o in p.objects:
            fate[o.order] = "solid region"

    _open_walls(parts, devices, notes, tol)
    _clip_to_domain(parts, domain_box, notes, tol)
    _resolve_overlaps(parts, model.producer, notes, report, tol)
    for p in parts:
        if p.shape is None:
            records["not_built"].append({
                "name": p.objects[0].props.get("heatsink") or p.objects[0].name, "kind": p.kind,
                "path": list(p.objects[0].path), "material": p.material, "power_W": p.power,
                "why": p.dropped or "it could not be built"})
            for o in p.objects:
                fate[o.order] = f"not built ({p.dropped})"
    parts = [p for p in parts if p.shape is not None]

    materials_per_heatsink: dict[str, int] = {}
    for p in parts:
        if p.kind == "heatsink":
            hs = str(p.objects[0].props["heatsink"])
            materials_per_heatsink[hs] = materials_per_heatsink.get(hs, 0) + 1
    for p in parts:
        if p.kind == "heatsink":
            hs = str(p.objects[0].props["heatsink"])
            raw = hs if materials_per_heatsink[hs] == 1 else f"{hs}_{p.material or 'part'}"
        else:
            raw = p.objects[0].name
        p.name = namer(raw)
    before = {p.name: _volume(p.shape) for p in parts}
    airs = _air(domain_box, parts, notes, tol)
    if not airs and parts:
        if model.domain is None:
            report.append(
                "No air: the file sets no solution domain, so the domain is the parts' own "
                "bounding box, and they fill it. The model is conduction only. To model the air "
                "around the parts, give the model a solution domain larger than the parts in the "
                "authoring tool and export it again.")
        else:
            report.append("No air: the solids fill the whole solution domain the file sets. The "
                          "model is conduction only (no air region).")
    named: list[tuple[str, object]] = []
    fluid_rows = []
    for i, (shape, vol, sides) in enumerate(airs):
        nm = fluid_base if i == 0 else namer(f"{FLUID_NAME}_{i + 1}")
        named.append((nm, shape))
        before[nm] = vol
        fluid_rows.append({"name": nm, "type": "fluid", "kind": "air",
                           "volume_m3": vol, "centroid_m": _centroid(shape),
                           "open_to_domain_sides": sides,
                           "notes": [] if i == 0 else [_air_space_note(sides)]})
    for p in parts:
        named.append((p.name, p.shape))

    named, fused = _conformal(named, notes, tol)
    by_name = dict(named)
    report += _where_sources_land(records["volume_heat_sources"], named,
                                  {str(r["name"]) for r in fluid_rows}, tol)
    for row in fluid_rows:
        row["surface_area_m2"] = _area(by_name[str(row["name"])])
    regions = list(fluid_rows)
    for p in parts:
        shape = by_name[p.name]
        thin, thin_is = _thinnest(p, domain_box, tol.noise)
        regions.append({
            "name": p.name, "type": "solid", "kind": p.kind,
            "material": p.material, "power_W": p.power,
            "objects": [{"name": o.name, "path": list(o.path), "kind": o.kind,
                         "box_m": snapped(o).as_dict(), "power_W": o.power,
                         "material": o.material or None} for o in p.objects],
            "volume_m3": _volume(shape), "centroid_m": _centroid(shape),
            "surface_area_m2": _area(shape),
            # the thinnest the region is anywhere: what a mesh must put cells across
            "thinnest_m": thin if math.isfinite(thin) else None, "thinnest_is": thin_is,
            "notes": p.notes})
    thin_gaps = _thin_gaps(parts, domain_box, tol.noise, THIN_GAP_LIMIT_REL * model_size)

    checks, contacts = _fidelity(model, objs, parts, regions, named, before, fused, domain_box,
                                 fate, records, tol)
    report[:0] = _report_head(model, objs, parts, regions, records, tol, notes, thin_gaps)
    report += checks
    domain_patches = _domain_patches(domain_box, records["patches"])
    sidecar = {
        "format": "ECXML", "spec": SPEC, "sidecar_version": SIDECAR_VERSION,
        "model_name": model.name, "producer": model.producer,
        "units": {"length": "m", "temperature": "K", "pressure": "Pa", "power": "W",
                  "thermal_conductivity": "W/mK", "thermal_resistance": "K/W",
                  "specific_heat": "J/kgK", "density": "kg/m3", "flow_rate": "m3/s"},
        "geometry_unit": "mm (the STEP carries the file's metres x 1000)",
        "domain": {"box_m": domain_box.as_dict(), "source": domain_source,
                   "ambient": model.domain.ambient if model.domain else None},
        "materials": {n: m.as_dict() for n, m in model.materials.items()},
        "regions": regions,
        "patches": domain_patches + records["patches"],
        "fans": records["fans"], "grilles": records["grilles"],
        "flow_resistances": records["flow_resistances"],
        "volume_heat_sources": records["volume_heat_sources"],
        "surface_heat_sources": records["surface_heat_sources"],
        "baffles": records["baffles"], "compact_models": records["compact_models"],
        "monitor_points": records["monitor_points"],
        "inactive_objects": records["inactive"], "not_built": records["not_built"],
        "ignored_elements": model.ignored_elements,
        # what the file states, and where it lands: in a meshed solid, applied by a source's box
        # or rectangle, or left out with a part that is not meshed (listed in not_built)
        "total_power_W": sum(float(o.power or 0.0) for o in objs
                             if o.kind not in ("assembly", "heatsink")),
        "power_W": {
            "in_solid_regions": sum(float(p.power or 0.0) for p in parts),
            "in_heat_sources": sum(float(s.get("power_W") or 0.0)
                                   for k in ("volume_heat_sources", "surface_heat_sources")
                                   for s in records[k])
                               + sum(float(p.get("power_W") or 0.0) for p in records["patches"]
                                     if p.get("role") == "heat_flux"),
            "in_plates_not_meshed": sum(float(b.get("power_W") or 0.0)
                                        for b in records["baffles"])
                                    + sum(float(p.get("power_W") or 0.0)
                                          for p in records["patches"] if p.get("role") == "wall"),
            "in_parts_not_built": sum(float(r.get("power_W") or 0.0)
                                      for r in records["not_built"]),
        },
        "tolerances_m": {"file_precision": tol.noise, "boolean_fuzzy": tol.fuzzy,
                         "smallest_gap": tol.gap if math.isfinite(tol.gap) else None,
                         "smallest_gap_between": tol.gap_where,
                         "thinnest_part": tol.thick if math.isfinite(tol.thick) else None,
                         "thinnest_part_is": tol.thick_where,
                         "coordinates_snapped": tol.snapped, "largest_snap": tol.snap_shift},
        "build_report": report,
        # pairs of solid regions that share a face (the conduction paths the mesh couples)
        "solid_contacts": sorted(sorted(c) for c in contacts),
        # the narrow air between facing parts (and a part and the domain's side): a mesh must
        # keep each open, so these set how fine it must be there
        "thin_gaps": thin_gaps,
        "notes": notes,
    }
    if model.ignored_elements:
        notes.append("elements the ECXML schema does not define were skipped: "
                     + ", ".join(f"{k} x{v}" for k, v in sorted(model.ignored_elements.items())[:12]))
    shown = ("Checked:", "Overlap:", "Switched off", "Not built:", "Not meshed:", "Not read",
             "No air:", "Heat sources:")
    notes[:0] = [line for line in report if line.startswith(shown)]
    return EcxmlBuild(named=named, sidecar=sidecar, notes=notes, report=report)


def _report_head(model, objs, parts, regions, records, tol: _Tol, notes,
                 thin_gaps: list[dict] | None = None) -> list[str]:
    solids = [r for r in regions if r["type"] == "solid"]
    airs = [r for r in regions if r["type"] == "fluid"]
    lines = [f"Read {len([o for o in objs if o.kind not in ('assembly', 'heatsink')])} active "
             f"objects from {model.producer!r}: built {len(solids)} solid region(s) and "
             f"{len(airs)} air region(s)."]
    if tol.snapped and tol.snap_shift > 1e-12:
        lines.append(f"{tol.snapped} coordinate(s) within the file's precision "
                     f"({_um(tol.noise)}) of another were read as the same plane; the largest "
                     f"move was {_um(tol.snap_shift)}.")
    gap = _um(tol.gap) + f" ({tol.gap_where})" if math.isfinite(tol.gap) else "none"
    if thin_gaps and thin_gaps[0]["gap_m"] >= tol.gap * (1 - 1e-9):
        # the narrowest AIR between parts: two parts' boxes may stand closer with a third part
        # filling the space between them (a fin's foot between two fins), which is no gap
        a, b = thin_gaps[0]["between"]
        gap = (_um(thin_gaps[0]["gap_m"]) + f" (air between {a} and "
               + ("the domain's side" if b == "domain" else b) + ")")
    lines.append(f"Smallest gap between parts: {gap}; thinnest part: {_um(tol.thick)} "
                 f"({tol.thick_where}); the solids are joined with a tolerance of "
                 f"{_um(tol.fuzzy)}, at most a tenth of both.")
    if records["inactive"]:
        names = ", ".join(r["name"] for r in records["inactive"][:8])
        more = f" and {len(records['inactive']) - 8} more" if len(records["inactive"]) > 8 else ""
        lines.append(f"Switched off in the file, so not meshed: {names}{more}.")
    lines += _not_built_lines(records["not_built"])
    for b in records["baffles"]:
        lines.append(f"Not meshed: {b['name']}, a plate without a usable thickness "
                     f"({_um(b['thickness_m'])}): the air flows through it; give it its real "
                     "thickness in the authoring tool to mesh it.")
    meta = {k: len(records[k]) for k in ("fans", "grilles", "flow_resistances",
                                         "volume_heat_sources", "surface_heat_sources",
                                         "monitor_points")}
    if any(meta.values()):
        lines.append("Kept beside the mesh, not in it: " + ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in meta.items() if v) + ".")
    if model.ignored_elements:
        lines.append("Not read (not in the ECXML schema): " + ", ".join(
            f"{k} x{v}" for k, v in sorted(model.ignored_elements.items())[:12]) + ".")
    lines += [n for n in notes if "opened where" in n]
    return lines


def _extent(o: Obj) -> tuple[list[float], list[float]]:
    """The bounds an object contributes to snapping: its box, except that a 2D object whose size
    along its normal is larger than it is wide (no thickness, a stray value) is its plane."""
    lo, hi = list(o.lo), list(o.hi)
    if o.kind in TWO_D and o.plane:
        axis = axis_of_plane(o.plane)
        span = min(hi[i] - lo[i] for i in range(3) if i != axis)
        if hi[axis] - lo[axis] > span:
            hi[axis] = lo[axis]
    return lo, hi


def _direction(plane: str) -> str:
    return ("+" if sign_of_plane(plane) > 0 else "-") + _AXES[axis_of_plane(plane)]


def _domain(domain: Domain | None, objs: list[Obj], snapped, noise: float
            ) -> tuple[_Box | None, str]:
    if domain is not None:
        as_obj = Obj(kind="solutionDomain", name="", path=(), order=0, active=True,
                     location=domain.location, size=domain.size)
        return snapped(as_obj), "solutionDomain"
    boxes = []
    for o in objs:
        if o.location is None or o.size is None or o.kind in (
                "assembly", "heatsink", "monitorPoint", "externalMcadFile"):
            continue
        box = snapped(o)
        if o.kind in TWO_D:
            # a 2D object is its rectangle; the size along its normal is at most a thermal
            # thickness, and real exports write anything there (3333 m has been seen)
            axis = axis_of_plane(o.plane)
            hi = list(box.hi)
            hi[axis] = box.lo[axis]
            box = _Box(box.lo, hi)
        boxes.append(box)
    if not boxes:
        return None, ""
    lo = [min(b.lo[i] for b in boxes) for i in range(3)]
    hi = [max(b.hi[i] for b in boxes) for i in range(3)]
    if any(hi[i] - lo[i] <= noise for i in range(3)):
        return None, ""
    return _Box(lo, hi), ("bounding box of every object (the file sets no solutionDomain; "
                          "JEP181A's own example uses this domain)")


def _enclosure_walls(box: _Box, t: float, noise: float) -> list[tuple[int, float, float, _Box]]:
    """An enclosure's six wall slabs (axis, inner bound, outer bound, slab box); none when its
    walls meet in the middle."""
    if not all(box.hi[i] - box.lo[i] > 2 * t + noise for i in range(3)):
        return []
    walls = []
    for axis in range(3):
        for lo, hi in ((box.lo[axis], box.lo[axis] + t), (box.hi[axis] - t, box.hi[axis])):
            slab_lo, slab_hi = list(box.lo), list(box.hi)
            slab_lo[axis], slab_hi[axis] = lo, hi
            walls.append((axis, lo, hi, _Box(slab_lo, slab_hi)))
    return walls


def _enclosure(o: Obj, box: _Box, noise: float) -> _Part:
    t = float(o.props["wall_thickness_m"])
    walls = _enclosure_walls(box, t, noise)
    inner = _Box([box.lo[i] + t for i in range(3)], [box.hi[i] - t for i in range(3)])

    def make(_tol: _Tol):
        outer = _occ_box(box)
        return _cut_exact(outer, _occ_box(inner)) if walls else outer

    part = _Part(name="", kind="enclosure", objects=[o], box=box, order=o.order,
                 material=o.material or None, power=o.power or 0.0, wall=True, make=make,
                 walls=walls)
    if walls:
        part.notes.append(f"enclosure: walls {_um(t)} thick")
    else:
        part.notes.append(f"enclosure: its walls ({_um(t)}) meet in the middle, so it is "
                          "solid throughout")
    return part


def _cut_exact(shape, tool):
    """A cut of two shapes built from the same snapped numbers: no fuzzy value is needed."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    algo = BRepAlgoAPI_Cut(shape, tool)
    if not algo.IsDone() or algo.Shape().IsNull():
        raise EcxmlError("OpenCASCADE could not hollow an enclosure")
    return algo.Shape()


def _on_domain_face(o: Obj, box: _Box, domain: _Box, tol: float) -> str:
    """The domain face ("-x" ... "+z") a 2D object lies on, or "". A 2D object is the rectangle at
    its location along its plane's normal (JEP181A 5.1: the walls of the example's wind tunnel sit
    on the domain faces whichever way their thickness points)."""
    axis = axis_of_plane(o.plane)
    at = box.lo[axis]
    if abs(at - domain.lo[axis]) <= tol:
        return "-" + _AXES[axis]
    if abs(at - domain.hi[axis]) <= tol:
        return "+" + _AXES[axis]
    return ""


def _rect(o: Obj, box: _Box, clip: _Box | None = None) -> dict:
    axis = axis_of_plane(o.plane)
    cross = [i for i in range(3) if i != axis]
    lo, hi = list(box.lo), list(box.hi)
    hi[axis] = lo[axis]
    if clip is not None:
        for i in cross:
            lo[i], hi[i] = max(lo[i], clip.lo[i]), min(hi[i], clip.hi[i])
    w, h = (max(0.0, hi[i] - lo[i]) for i in cross)
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
    normal = [0.0, 0.0, 0.0]
    normal[axis] = float(sign_of_plane(o.plane))
    return {"centre_m": centre, "plane": o.plane, "normal": normal,
            "size_m": [w, h], "size_axes": [_AXES[i] for i in cross], "area_m2": w * h,
            "thickness_m": box.hi[axis] - box.lo[axis], "order": o.order}


def _plate(o: Obj, box: _Box, domain: _Box, noise: float, parts: list[_Part], records: dict,
           notes: list[str]) -> bool:
    """A solid2dBlock: a wall patch on the domain's side, a thin solid inside the domain, or a
    baffle (sidecar only). True when it became a solid."""
    face = _on_domain_face(o, box, domain, noise)
    rect = _rect(o, box, domain)
    if face:
        records["patches"].append({
            "name": o.name, "path": list(o.path), "object": "solid2dBlock", "role": "wall",
            "suggested_type": "wall", "domain_face": face, **rect,
            "material": o.material or None, "power_W": o.power,
            "meaning": "a thin plate on the domain boundary: a conducting wall of this material "
                       "and thickness"})
        return False
    axis = axis_of_plane(o.plane)
    thickness = box.hi[axis] - box.lo[axis]
    span = min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
    if noise < thickness <= span:
        # a plate is thinner than it is wide; anything else in the normal size is not a thickness
        part = _Part(name="", kind="solid2dBlock", objects=[o], box=box, order=o.order,
                     material=o.material or None, power=o.power or 0.0, wall=True,
                     make=_box_maker(box))
        part.walls.append((axis, box.lo[axis], box.hi[axis], box))
        part.notes.append("a 2D plate in the authoring tool, built as a solid of the thickness the "
                          "file gives it so that it blocks the flow and conducts")
        parts.append(part)
        return True
    records["baffles"].append({
        "name": o.name, "path": list(o.path), **rect, "material": o.material or None,
        "power_W": o.power,
        "meshed_as": "not in the mesh: a plate without a usable thickness is no volume, and the "
                     "multi-region mesh has no internal baffle yet; the air flows through it"})
    return False


def _face_object(o: Obj, box: _Box, domain: _Box, noise: float, records: dict) -> None:
    face = _on_domain_face(o, box, domain, noise)
    rect = _rect(o, box, domain if face else None)
    row: dict = {"name": o.name, "path": list(o.path), "object": o.kind, **rect}
    if o.kind == "grille":
        row.update(loss_coefficient=o.props["loss_coefficient"],
                   free_area_ratio=o.props["free_area_ratio"],
                   loss_coefficient_definition="dP / (0.5 rho u^2); which velocity u means "
                                               "depends on the producer (JEP181A 4.5.2)")
    elif o.kind == "rectangular2dFan":
        row.update(flow=o.props["flow"], flow_direction=_direction(o.plane))
    else:
        row.update(power_W=o.power)
    if face:
        # the plane's sign is the way the object points: a fan pointing into the domain blows in
        inward = (face[0] == "-") == (sign_of_plane(o.plane) > 0)
        role, suggested = {
            "grille": ("vent", "opening"),
            "rectangular2dFan": ("fan", "inlet" if inward else "outlet"),
            "source2dBlock": ("heat_flux", "wall"),
        }[o.kind]
        records["patches"].append({**row, "role": role, "suggested_type": suggested,
                                   "domain_face": face})
        return
    if o.kind == "grille":
        records["grilles"].append({**row, "meshed_as": "air: an internal resistance plane the "
                                                       "solver applies across the fluid"})
    elif o.kind == "rectangular2dFan":
        records["fans"].append({**row, "kind": "rectangular_2d",
                                "meshed_as": "air: an internal fan plane the solver applies "
                                             "across the fluid"})
    else:
        records["surface_heat_sources"].append({**row, "meshed_as": "not marked: the power "
                                                                    "is applied on this "
                                                                    "rectangle"})


#: The sidecar lists that place a device, a source or a probe, and what one entry is called.
_PLACED_RECORDS = {"fans": "fan", "grilles": "grille", "flow_resistances": "flow resistance",
                   "volume_heat_sources": "volume heat source",
                   "surface_heat_sources": "surface heat source", "baffles": "plate",
                   "monitor_points": "monitor point", "compact_models": "2-resistor model"}


def _row_box(row: dict) -> _Box | None:
    """Where a sidecar entry sits, m: its box, its rectangle (flat along its normal) or its
    point."""
    if "box_m" in row:
        return _Box(row["box_m"]["min"], row["box_m"]["max"])
    if "location_m" in row:
        return _Box(row["location_m"], row["location_m"])
    if "centre_m" in row and "size_m" in row and "size_axes" in row:
        lo, hi = list(row["centre_m"]), list(row["centre_m"])
        for k, a in enumerate(row["size_axes"]):
            i = _AXES.index(a)
            lo[i] = row["centre_m"][i] - row["size_m"][k] / 2
            hi[i] = row["centre_m"][i] + row["size_m"][k] / 2
        return _Box(lo, hi)
    return None


def _records_in_domain(records: dict, domain: _Box, noise: float) -> list[str]:
    """The devices, sources, plates and probes of the sidecar held against the solution domain,
    as the parts are: one wholly outside it is not applied - it moves to `not_built` (its power
    counted there, never as applied) - and one partly outside it gets the box it keeps inside.
    Real exports carry both (a Flotherm sample keeps a 23 W source and its probes metres outside
    a 50 mm domain, and a 150 mm fan over it). Returns notes for the user."""
    notes: list[str] = []
    for key, what in _PLACED_RECORDS.items():
        kept = []
        for row in records[key]:
            box = _row_box(row)
            if box is None:
                kept.append(row)
                continue
            solid = all(box.hi[i] - box.lo[i] > noise for i in range(3))
            if solid:
                inside = box.common(domain)
                outside = inside is None
            else:                              # a rectangle or a point: touching the side counts
                outside = any(box.hi[i] < domain.lo[i] - noise or box.lo[i] > domain.hi[i] + noise
                              for i in range(3))
                inside = None if outside else _Box(
                    [min(max(box.lo[i], domain.lo[i]), domain.hi[i]) for i in range(3)],
                    [min(max(box.hi[i], domain.lo[i]), domain.hi[i]) for i in range(3)])
            name = "/".join(list(row.get("path") or []) + [str(row.get("name"))])
            if outside:
                if key != "compact_models":    # its block is listed as not built already
                    records["not_built"].append({
                        "name": row.get("name"), "kind": what, "path": list(row.get("path") or []),
                        "power_W": row.get("power_W"),
                        "why": "it lies outside the solution domain"})
                continue
            assert inside is not None
            if not box.inside(domain, noise):
                row["partly_outside_domain"] = True
                row["box_in_domain_m"] = inside.as_dict()
                extra = ""
                if key == "fans" and "box_m" in row and not all(
                        domain.lo[i] - noise <= c <= domain.hi[i] + noise
                        for i, c in enumerate(row.get("centre_m") or [])):
                    extra = "; its centre lies outside, so the domain holds only a piece of it"
                notes.append(f"{name}: the {what} sticks out of the solution domain - only the part "
                             f"inside is in the model{extra}")
            kept.append(row)
        records[key] = kept
    kept = []
    for row in records["patches"]:
        if float(row.get("area_m2") or 0.0) > 0.0:
            kept.append(row)
            continue
        records["not_built"].append({
            "name": row.get("name"), "kind": str(row.get("object") or "patch"),
            "path": list(row.get("path") or []), "power_W": row.get("power_W"),
            "why": "it lies in the plane of a side of the solution domain, but outside that side"})
    records["patches"] = kept
    return notes


def _not_built_lines(rows: list[dict]) -> list[str]:
    """The report's 'Not built' lines: one per object, or one per reason when many share it."""
    by_why: dict[str, list[str]] = {}
    for r in rows:
        by_why.setdefault(str(r["why"]), []).append(str(r["name"]))
    lines = []
    for why, names in by_why.items():
        if len(names) <= 3:
            lines += [f"Not built: {n} - {why}." for n in names]
            continue
        shown = ", ".join(names[:8])
        more = f" and {len(names) - 8} more" if len(names) > 8 else ""
        lines.append(f"Not built: {len(names)} objects - {why}: {shown}{more}.")
    return lines


def _heatsink(group: list[Obj], material: str, snapped) -> _Part:
    boxes = [snapped(o) for o in group]
    lo = [min(b.lo[i] for b in boxes) for i in range(3)]
    hi = [max(b.hi[i] for b in boxes) for i in range(3)]
    hs = group[0].props["heatsink"]

    def make(tol: _Tol):
        shapes = [_occ_box(b) for b in boxes]
        if len(shapes) == 1:
            return shapes[0]
        return _unify(_boolean("fuse", shapes[:1], shapes[1:], tol))

    part = _Part(name="", kind="heatsink", objects=list(group), box=_Box(lo, hi),
                 order=max(o.order for o in group), material=material or None,
                 power=sum(float(o.power or 0.0) for o in group), make=make, blocks=boxes)
    part.notes.append(f"heatsink {hs!r}: {len(group)} block(s) fused into one solid")
    return part


def _device_cut(o: Obj, box: _Box, axis: int, lo: float, hi: float, slab: _Box,
                tol: float) -> _Box | None:
    """The box of the hole a device cuts through a wall slab normal to `axis` (round for an
    axial fan, inscribed in this box), or None when the device does not lie in that wall. The
    cutter reaches well past both faces of the wall: only this wall is cut, and a cutter face a
    hair off a wall face would be merged into it by tolerance."""
    reach = max(hi - lo, CUT_REACH_M)
    if axis_of_plane(o.plane) != axis:
        return None
    if o.kind == "axial3dFan":
        if not (box.lo[axis] < hi - tol and box.hi[axis] > lo + tol):
            return None
    else:
        # the device's plane is at its location; a grille written with its thickness may also
        # put its other face in the wall (a sane thickness - never a stray huge normal size)
        span = min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
        planes = [box.lo[axis]] + ([box.hi[axis]] if box.hi[axis] - box.lo[axis] <= span else [])
        if not any(lo - tol <= at <= hi + tol for at in planes):
            return None
    cut = _Box([box.lo[i] if i != axis else lo - reach for i in range(3)],
               [box.hi[i] if i != axis else hi + reach for i in range(3)])
    return cut if cut.overlaps(slab, 0.0) else None


def _open_walls(parts: list[_Part], devices: list[tuple[Obj, _Box]], notes: list[str],
                tol: _Tol) -> None:
    for part in parts:
        if not part.wall or not part.walls:
            continue
        cutters, opened = [], []
        for o, box in devices:
            for axis, lo, hi, slab in part.walls:
                cut = _device_cut(o, box, axis, lo, hi, slab, tol.noise)
                if cut is not None:
                    round_ = o.kind == "axial3dFan"
                    cutters.append(_occ_cylinder(cut, axis, tol.noise) if round_ else _occ_box(cut))
                    part.cutters.append(("round" if round_ else "box", cut, axis))
                    opened.append(o.name)
                    break
        if cutters:
            part.shape = _boolean("cut", [part.shape], cutters, tol)
            part.notes.append(f"opened where {', '.join(sorted(set(opened))[:8])} sit(s) in it")
            notes.append(f"{part.objects[0].label}: opened where "
                         f"{', '.join(sorted(set(opened))[:8])} sit(s) in its wall")


def _clip_to_domain(parts: list[_Part], domain: _Box, notes: list[str], tol: _Tol) -> None:
    dom = None
    for p in parts:
        if p.box.inside(domain, tol.noise):
            continue
        if not p.box.overlaps(domain, 0.0):
            p.shape = None
            p.dropped = "it lies outside the solution domain"
            continue
        if dom is None:
            dom = _occ_box(domain)
        p.shape = _boolean("common", [p.shape], [dom], tol)
        p.clipped = True
        p.notes.append("clipped to the solution domain")


def _resolve_overlaps(parts: list[_Part], producer: str, notes: list[str], report: list[str],
                      tol: _Tol) -> None:
    """Where solids overlap, the winner keeps the overlap: the later object in the file, or for
    Icepak a solid fully inside another (JEP181A 4.5.1). Cut from the originals, so the result does
    not depend on the order the cuts are made in."""
    import numpy as np

    icepak = producer.strip().lower() == "icepak"
    live = [p for p in parts if p.shape is not None]
    if len(live) < 2:
        return

    def wins(a: _Part, b: _Part) -> bool:
        if icepak and a.box.inside(b.box, tol.noise) and not b.box.inside(a.box, tol.noise):
            return True
        if icepak and b.box.inside(a.box, tol.noise) and not a.box.inside(b.box, tol.noise):
            return False
        return a.order > b.order

    # overlap is judged on the material: an enclosure's walls, a heat sink's blocks - a part
    # inside a housing does not overlap the housing
    pieces: list[_Box] = []
    owner: list[int] = []
    for k, p in enumerate(live):
        for b in _material(p):
            pieces.append(b)
            owner.append(k)
    lo = np.array([b.lo for b in pieces])
    hi = np.array([b.hi for b in pieces])
    _best, _pair, overlap, _touch = _pair_scan(lo, hi, tol.noise)
    seen: set[tuple[int, int]] = set()
    for a, b in overlap:
        ka, kb = sorted((owner[a], owner[b]))
        if ka == kb or (ka, kb) in seen:
            continue
        seen.add((ka, kb))
        pa, pb = live[ka], live[kb]
        if wins(pa, pb):
            pb.winners.append(pa)
        else:
            pa.winners.append(pb)
    originals = {id(p): p.shape for p in live}
    for p in live:
        if not p.winners:
            continue
        before = _volume(p.shape)
        cut = _boolean("cut", [p.shape], [originals[id(q)] for q in p.winners], tol)
        after = _volume(cut) if _solids(cut) else 0.0
        who = ", ".join(q.objects[0].props.get("heatsink") or q.objects[0].name
                        for q in p.winners[:6])
        rule = ("the embedded solid wins (Icepak)" if icepak else
                "the later object in the file wins") + ", JEP181A 4.5.1"
        name = p.objects[0].props.get("heatsink") or p.objects[0].name
        if after <= 1e-9 * max(before, 1e-30) or not _solids(cut):
            report.append(f"Overlap: {who} overwrite(s) all of {name} ({rule}), so {name} is "
                          "not meshed.")
            p.shape = None
            p.dropped = "objects that take precedence overwrite all of it (JEP181A 4.5.1)"
            continue
        lost = before - after
        if lost <= VOLUME_RTOL * before:
            p.winners = []                     # touching, not overlapping: nothing was taken
            continue
        p.notes.append(f"{100 * lost / before:.3g}% of it is overwritten by objects that take "
                       "precedence (JEP181A 4.5.1)")
        report.append(f"Overlap: {who} overwrite(s) {lost * 1e9:,.6g} mm3 of {name} "
                      f"({100 * lost / before:.3g}% of it; {rule}).")
        p.shape = _unify(cut)


def _air_space_note(sides: list[str]) -> str:
    """What an air space apart from the main air is, for the user."""
    if not sides:
        return "an air space not connected to the main air (a sealed cavity)"
    return ("an air space not connected to the main air inside the domain; it is open to the "
            f"domain's {', '.join(sides)} side(s), so it meets the outside there")


def _domain_sides_of(shape, domain: _Box, tol: float) -> list[str]:
    """The sides of the domain ("-x" ... "+z") a shape has a face on: where an air space meets
    the domain's boundary, i.e. is open to the outside rather than sealed inside the parts."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    sides: set[str] = set()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        b = _bounds(exp.Current())
        for i in range(3):
            if b.hi[i] - b.lo[i] > tol:
                continue
            at = (b.lo[i] + b.hi[i]) / 2
            if abs(at - domain.lo[i]) <= tol:
                sides.add("-" + _AXES[i])
            elif abs(at - domain.hi[i]) <= tol:
                sides.add("+" + _AXES[i])
        exp.Next()
    return sorted(sides, key=lambda s: (_AXES.index(s[1]), s[0] == "+"))


def _air(domain: _Box, parts: list[_Part], notes: list[str], tol: _Tol
         ) -> list[tuple[object, float, list[str]]]:
    """The air: the domain minus every solid, one entry per connected space - (shape, volume,
    the domain sides it is open to). Largest first; spaces of equal volume (a heat sink's
    channels) in the order of their centroids, so their names do not depend on OpenCASCADE's."""
    box = _occ_box(domain)
    solids = [p.shape for p in parts if p.shape is not None]
    air = _boolean("cut", [box], solids, tol) if solids else box
    pieces = [(s, _volume(s)) for s in _solids(air)]
    pieces = [(s, v) for s, v in pieces if v > 1e-12 * domain.volume()]
    if not pieces:
        notes.append("the parts fill the whole domain: there is no air to mesh")
        return []
    keyed = [(-float(f"{v:.9g}"), [round(c / max(tol.noise, 1e-12)) for c in _centroid(s)], s, v)
             for s, v in pieces]
    keyed.sort(key=lambda k: (k[0], k[1]))
    reach = max(tol.noise, tol.fuzzy) * 2
    out = [(_unify(s), v, _domain_sides_of(s, domain, reach)) for _k, _c, s, v in keyed]
    if len(out) > 1:
        sealed = sum(1 for _s, _v, sides in out if not sides)
        notes.append(f"the air is {len(out)} separate spaces ({len(out) - sealed} open to the "
                     f"domain's sides, {sealed} sealed inside the parts); each is its own fluid "
                     "region")
    return out


#: Source x region intersections computed at most (each one boolean on two small shapes).
MAX_SOURCE_LANDINGS = 2000


def _where_sources_land(sources: list[dict], named: list[tuple[str, object]],
                        fluids: set[str], tol: _Tol) -> list[str]:
    """Which regions each volume heat source's box lies in, by volume fraction (`lands_in` on its
    sidecar row) - what a solver needs to apply the power. Flotherm exports put a part's power in
    a sourceBlock on top of (or inside) the part rather than in the part's own powerDissipation,
    so most real models carry their heat this way. Returns the report line."""
    if not sources:
        return []
    bounds = [(name, shape, _bounds(shape)) for name, shape in named]
    work = []
    for s in sources:
        b = s.get("box_in_domain_m") or s["box_m"]
        box = _Box(b["min"], b["max"])
        if box.volume() <= 0.0:
            continue
        work.append((s, box, [(n, sh) for n, sh, bb in bounds if box.overlaps(bb, 0.0)]))
    if sum(len(c) for _s, _b, c in work) > MAX_SOURCE_LANDINGS:
        return [f"Heat sources: {len(sources)} volume source(s); where each lands was not worked "
                "out (too many to intersect); each applies its power in its box."]
    whole: dict[str, list[str]] = {"solid": [], "air": []}
    split = 0
    for s, box, cands in work:
        occ = _occ_box(box)
        found: list[tuple[str, float]] = []
        for name, shape in cands:
            got = _solids(_boolean("common", [occ], [shape], tol))
            vol = sum(_volume(x) for x in got)
            if vol > 1e-9 * box.volume():
                found.append((name, min(1.0, vol / box.volume())))
        found.sort(key=lambda nf: -nf[1])
        s["lands_in"] = [{"region": n, "fraction": f} for n, f in found]
        if found and found[0][1] >= 1 - 1e-6:
            whole["air" if found[0][0] in fluids else "solid"].append(found[0][0])
        else:
            split += 1
    total = sum(float(s.get("power_W") or 0.0) for s in sources)
    bits = []
    if whole["solid"]:
        names = sorted(set(whole["solid"]))
        bits.append(f"{len(whole['solid'])} lie wholly in one solid region ("
                    + ", ".join(names[:8]) + (", ..." if len(names) > 8 else "") + ")")
    if whole["air"]:
        bits.append(f"{len(whole['air'])} lie wholly in air")
    if split:
        bits.append(f"{split} span more than one region (fractions in the sidecar)")
    return [f"Heat sources: {len(sources)} volume source(s), {total:.6g} W; " + "; ".join(bits)
            + "."]


def _conformal(named: list[tuple[str, object]], notes: list[str], tol: _Tol
               ) -> tuple[list[tuple[str, object]], bool]:
    """Every region split where it touches another (a General Fuse), so two regions that share a
    face share it exactly - the conformal interface a multi-region mesh couples across. A fuse
    that fails leaves the regions as they are: they still touch, only not face for face."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo

    if len(named) < 2:
        return named, False
    try:
        algo = BRepAlgoAPI_BuilderAlgo()
        algo.SetArguments(_list([s for _, s in named]))
        if tol.fuzzy > 0:
            algo.SetFuzzyValue(tol.fuzzy * UNIT)
        algo.SetRunParallel(False)
        algo.Build()
        if not algo.IsDone() or algo.Shape().IsNull():
            raise RuntimeError("general fuse failed")
        out = []
        for name, shape in named:
            solids = []
            for solid in _solids(shape):
                images = list(algo.Modified(solid))
                if not images and algo.IsDeleted(solid):
                    continue
                solids += [s for img in images for s in _solids(img)] if images else [solid]
            if not solids:
                raise RuntimeError(f"region {name} vanished in the fuse")
            out.append((name, solids[0] if len(solids) == 1 else _compound(solids)))
        return out, True
    except Exception as exc:  # noqa: BLE001 - the regions are right; only the face split is lost
        notes.append(f"the shared faces between regions could not be split face for face ({exc})")
        return named, False


# ------------------------------------------------------------------------------ fidelity ----
def _expected_volume(p: _Part, domain: _Box) -> float | None:
    """The part's volume computed from the file's numbers alone (boxes, cylinder and ellipse
    areas, wall slabs and cut-outs), or None where only the geometry can say (a cylinder or an
    enclosure that something overwrites, a part the domain cuts through)."""
    if any(q.kind not in BOX_KINDS + ("heatsink",) for q in p.winners):
        return None
    if p.cutters and (p.winners or p.clipped):
        return None
    winner_boxes = [b for q in p.winners for b in (q.blocks or [q.box])]
    if p.kind == "heatsink":
        clip = p.box.common(domain)
        if clip is None:
            return 0.0
        return _union_volume(clip, p.blocks) - _union_volume(
            clip, [c for c in (b.common(w) for b in p.blocks for w in winner_boxes) if c])
    if p.kind in BOX_KINDS:
        base = p.box.common(domain)
        if base is None:
            return 0.0
        full = base.volume() - _union_volume(base, winner_boxes)
    elif p.winners or p.clipped:
        return None
    elif p.kind == "solidCylinder":
        a, b, h = _cylinder_axes(p.box, axis_of_plane(p.objects[0].plane))
        return math.pi * a * b * h
    elif p.kind == "enclosure":
        full = _union_volume(p.box, [w[3] for w in p.walls]) if p.walls else p.box.volume()
    else:
        return None
    removed = _cutout_volume(p)
    return None if removed is None else full - removed


def _cutout_volume(p: _Part) -> float | None:
    """The volume the vents and fans cut out of a wall or a plate, from their boxes: box cutters
    exactly (their union with the wall slabs), a round fan exactly (pi a b times the wall it goes
    through) when its circle lies within that wall alone. None where only the geometry can say."""
    if not p.cutters:
        return 0.0
    slabs = [w[3] for w in p.walls]
    boxes = [c for kind, c, _axis in p.cutters if kind == "box"]
    removed = _union_volume(p.box, [x for c in boxes for s in slabs
                                    if (x := c.common(s)) is not None])
    if p.kind == "enclosure":
        t = p.walls[0][2] - p.walls[0][1]
        inner = _Box([p.box.lo[i] + t for i in range(3)], [p.box.hi[i] - t for i in range(3)])
    else:
        inner = p.box
    for kind, c, axis in p.cutters:
        if kind != "round":
            continue
        if any(o is not c and o.overlaps(c, 0.0) for _k, o, _a in p.cutters):
            return None
        through = [s for a, _lo, _hi, s in p.walls if a == axis and s.common(c) is not None]
        cross = [i for i in range(3) if i != axis]
        if len(through) != 1 or not all(c.lo[i] >= inner.lo[i] and c.hi[i] <= inner.hi[i]
                                        for i in cross):
            return None
        a, b, _h = _cylinder_axes(c, axis)
        removed += math.pi * a * b * (through[0].hi[axis] - through[0].lo[axis])
    return removed


def _fidelity(model, objs, parts, regions, named, before, fused, domain, fate, records,
              tol: _Tol) -> tuple[list[str], set[frozenset]]:
    """The hard checks that the solids are the file's model; a FidelityError names the first
    one that fails. Returns the report lines for the ones that pass."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    lines: list[str] = []
    by_name = dict(named)
    volume = {r["name"]: float(r["volume_m3"]) for r in regions}

    # (a) every solid is the file's: volume (exact where it can be computed) and bounding box
    exact = 0
    for p in parts:
        want = _expected_volume(p, domain)
        got_before, got = before[p.name], volume[p.name]
        label = p.objects[0].props.get("heatsink") or p.objects[0].label
        if want is not None:
            if abs(got_before - want) > VOLUME_RTOL * want + 1e-21:
                raise FidelityError(
                    f"{label}: the built solid is {got_before * 1e9:.9g} mm3, but the file's "
                    f"numbers give {want * 1e9:.9g} mm3")
            exact += 1
        if abs(got - got_before) > VOLUME_RTOL * got_before + 1e-21:
            raise FidelityError(f"{label}: joining the parts changed its volume from "
                                f"{got_before * 1e9:.9g} to {got * 1e9:.9g} mm3")
        bounds = _bounds(by_name[p.name])
        expect = p.box.common(domain) or p.box
        slack = max(tol.noise, tol.fuzzy) * 2
        if p.winners or p.clipped or p.cutters:
            ok = bounds.inside(expect, slack)
        else:
            ok = all(abs(bounds.lo[i] - expect.lo[i]) <= slack
                     and abs(bounds.hi[i] - expect.hi[i]) <= slack for i in range(3))
        if not ok:
            raise FidelityError(f"{label}: the built solid spans {bounds.as_dict()}, but the file "
                                f"places it at {expect.as_dict()}")
    for r in regions:
        if r["type"] == "fluid" and abs(volume[r["name"]] - before[r["name"]]) > \
                VOLUME_RTOL * before[r["name"]] + 1e-21:
            raise FidelityError(f"joining the parts changed the volume of the air region "
                                f"{r['name']}")

    # (b) the solids and the air fill the domain exactly
    total = sum(volume.values())
    if abs(total - domain.volume()) > VOLUME_RTOL * domain.volume():
        raise FidelityError(f"the solids and the air add up to {total * 1e9:.9g} mm3, but the "
                            f"domain is {domain.volume() * 1e9:.9g} mm3")

    # (c) every active object accounted for, once; every region name unique
    missing = [o.label for o in objs if o.kind not in ("assembly", "heatsink")
               and o.order not in fate]
    if missing:
        raise FidelityError(f"the build lost {len(missing)} object(s) of the file: "
                            + ", ".join(missing[:8]))
    names = [n for n, _ in named]
    if len({n.casefold() for n in names}) != len(names):
        raise FidelityError("two regions came out under the same name")
    in_regions = {o.order for p in parts for o in p.objects}
    for p in parts:
        if p.name not in by_name:
            raise FidelityError(f"{p.objects[0].label} was built but is not in the STEP")
    solid_objects = [o for o in objs if fate.get(o.order) == "solid region"]
    stray = [o.label for o in solid_objects if o.order not in in_regions]
    if stray:
        raise FidelityError("built objects without a region: " + ", ".join(stray[:8]))

    # (d) the fuse shares faces only where the file makes parts touch, and at every true contact
    contacts: set[frozenset] = set()
    if fused:
        fmap = TopTools_IndexedMapOfShape()
        owners: dict[int, set[str]] = {}
        for name, shape in named:
            exp = TopExp_Explorer(shape, TopAbs_FACE)
            while exp.More():
                owners.setdefault(fmap.Add(exp.Current()), set()).add(name)
                exp.Next()
        air_names = {r["name"] for r in regions if r["type"] == "fluid"}
        for regs in owners.values():
            solid_regs = sorted(regs - air_names)
            for i in range(len(solid_regs)):
                for j in range(i + 1, len(solid_regs)):
                    contacts.add(frozenset((solid_regs[i], solid_regs[j])))
        allowed, required = _file_contacts(parts, domain, tol)
        invented = contacts - allowed
        if invented:
            pair = sorted(next(iter(invented)))
            raise FidelityError(f"joining the parts made {pair[0]} and {pair[1]} touch, but the "
                                "file keeps them apart")
        lost = required - contacts
        if lost:
            pair = sorted(next(iter(lost)))
            raise FidelityError(f"{pair[0]} and {pair[1]} touch in the file, but the joined "
                                "solids do not share the face between them")
    planned = plan_region_names(model)
    agree = "" if planned == tuple(names) else (
        f" (the intake's quick reading named {len(planned)} region(s); the built ones are the "
        "truth)")
    lines.append(
        f"Checked: {exact} of {len(parts)} solid volume(s) match the file's numbers exactly, "
        f"the rest are bounded by the file; every bounding box is the file's; solids plus air "
        f"fill the domain to {abs(total - domain.volume()) / domain.volume():.1e}; all "
        f"{len([o for o in objs if o.kind not in ('assembly', 'heatsink')])} active objects are "
        f"accounted for; {len(contacts)} contact(s) between solids are shared face for face, "
        f"none invented{agree}.")
    return lines, contacts


def _file_contacts(parts: list[_Part], domain: _Box, tol: _Tol
                   ) -> tuple[set[frozenset], set[frozenset]]:
    """Pairs of regions the file lets touch (their boxes meet or overlap) and pairs that must
    share a face (two whole boxes meeting face to face over an area)."""
    import numpy as np

    boxes, owner, simple = [], [], []
    for p in parts:
        pieces = [w[3] for w in p.walls] if p.kind == "enclosure" and p.walls else \
            (p.blocks or [p.box])
        for b in pieces:
            boxes.append(b)
            owner.append(p.name)
            simple.append(p.kind in BOX_KINDS + ("heatsink",) and not p.winners
                          and not p.clipped and not p.cutters)
    allowed: set[frozenset] = set()
    required: set[frozenset] = set()
    if len(boxes) < 2:
        return allowed, required
    lo = np.array([b.lo for b in boxes])
    hi = np.array([b.hi for b in boxes])
    slack = max(tol.noise, tol.fuzzy) * 2
    for start in range(0, len(boxes), 256):
        i = np.arange(start, min(len(boxes), start + 256))
        ext = np.minimum(hi[i][:, None, :], hi[None, :, :]) - \
            np.maximum(lo[i][:, None, :], lo[None, :, :])
        meet = (ext >= -slack).all(axis=2)
        for r, c in zip(*np.nonzero(meet)):
            ia, ib = int(i[r]), int(c)
            if owner[ia] != owner[ib]:
                allowed.add(frozenset((owner[ia], owner[ib])))
        face = ((np.abs(ext) <= tol.noise).sum(axis=2) == 1) & ((ext > tol.noise).sum(axis=2) == 2)
        for r, c in zip(*np.nonzero(face)):
            ia, ib = int(i[r]), int(c)
            if owner[ia] != owner[ib] and simple[ia] and simple[ib]:
                required.add(frozenset((owner[ia], owner[ib])))
    return allowed, required


def _domain_patches(domain: _Box, objects: list[dict]) -> list[dict]:
    out = []
    for axis in range(3):
        cross = [i for i in range(3) if i != axis]
        for sign, at in (("-", domain.lo[axis]), ("+", domain.hi[axis])):
            centre = [(domain.lo[i] + domain.hi[i]) / 2 for i in range(3)]
            centre[axis] = at
            normal = [0.0, 0.0, 0.0]
            normal[axis] = 1.0 if sign == "+" else -1.0
            size = [domain.hi[i] - domain.lo[i] for i in cross]
            on_face = [p for p in objects if p.get("domain_face") == f"{sign}{_AXES[axis]}"]
            if any(p["area_m2"] >= size[0] * size[1] * (1 - 1e-6) for p in on_face):
                continue                  # an object covers the whole side: it is the patch
            covered = [p["name"] for p in on_face]
            out.append({
                "name": f"domain_{_AXES[axis]}{'max' if sign == '+' else 'min'}",
                "object": "solutionDomain", "role": "domain_boundary",
                "suggested_type": "opening", "domain_face": f"{sign}{_AXES[axis]}",
                "centre_m": centre, "normal": normal, "size_m": size,
                "size_axes": [_AXES[i] for i in cross], "area_m2": size[0] * size[1],
                "partly_covered_by": covered,
                "meaning": "a side of the solution domain; ECXML sets no condition per side (its "
                           "ambientConditions describe the air outside the domain)"})
    return out


# ------------------------------------------------------------------------------ names only --
def plan_region_names(model: EcxmlModel) -> tuple[str, ...]:
    """The region names build() gives this model, worked out without building any geometry - for
    the intake, which asks while a person waits and must not wait for booleans. The same parts, in
    the same order, through the same namer; a part is left out where build() leaves it out by its
    box: outside the domain, or inside the box of a solid block that takes precedence. The air is
    left out when a solid fills the domain, and a sealed enclosure (no vent or fan in its walls)
    with air outside it adds its own ("air_2"). Only the geometry settles the rarer cases, where
    the lists may differ: a part hidden by several parts together or inside a cylinder, a sealed
    space that solids fill, or an enclosure the domain cuts."""
    objs = model.active_objects
    noise = _noise(model, objs)
    snapped, _moved = _snap_function(model, objs, noise)
    domain_box, _source = _domain(model.domain, objs, snapped, noise)
    if domain_box is None:
        return ()
    planned: list[dict] = []
    heatsinks: dict[tuple, list[Obj]] = {}
    for o in objs:
        if o.kind in ("assembly", "heatsink"):
            continue
        if o.props.get("heatsink"):
            heatsinks.setdefault((o.path, o.material), []).append(o)
            continue
        if o.location is None or o.size is None:
            continue
        box = snapped(o)
        if o.kind in ("solid3dBlock", "printedCircuitBoard", "twoResistorModel"):
            planned.append({"raw": o.name, "kind": o.kind, "box": box, "order": o.order,
                            "full_box": True})
        elif o.kind in ("solidCylinder", "enclosure"):
            planned.append({"raw": o.name, "kind": o.kind, "box": box, "order": o.order,
                            "full_box": False,
                            "wall_thickness": o.props.get("wall_thickness_m", 0.0)})
        elif o.kind == "solid2dBlock" and not _on_domain_face(o, box, domain_box, noise):
            axis = axis_of_plane(o.plane)
            thickness = box.hi[axis] - box.lo[axis]
            span = min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
            if noise < thickness <= span:
                planned.append({"raw": o.name, "kind": o.kind, "box": box, "order": o.order,
                                "full_box": True})
    for (_path, material), group in heatsinks.items():
        boxes = [snapped(o) for o in group]
        planned.append({"raw": "", "kind": "heatsink", "material": material,
                        "heatsink": str(group[0].props["heatsink"]),
                        "box": _Box([min(b.lo[i] for b in boxes) for i in range(3)],
                                    [max(b.hi[i] for b in boxes) for i in range(3)]),
                        "order": max(o.order for o in group), "full_box": False})
    icepak = model.producer.strip().lower() == "icepak"

    def wins(a: dict, b: dict) -> bool:
        if icepak and a["box"].inside(b["box"], noise) and not b["box"].inside(a["box"], noise):
            return True
        if icepak and b["box"].inside(a["box"], noise) and not a["box"].inside(b["box"], noise):
            return False
        return a["order"] > b["order"]

    kept = [p for p in planned if p["box"].overlaps(domain_box, 0.0)
            and not any(q is not p and q["full_box"] and p["box"].inside(q["box"], noise)
                        and wins(q, p) for q in planned)]
    per_heatsink: dict[str, int] = {}
    for p in kept:
        if p["kind"] == "heatsink":
            per_heatsink[p["heatsink"]] = per_heatsink.get(p["heatsink"], 0) + 1
    namer = _unique_namer()
    air = namer(FLUID_NAME)
    parts = []
    for p in kept:
        if p["kind"] == "heatsink":
            raw = p["heatsink"] if per_heatsink[p["heatsink"]] == 1 else \
                f"{p['heatsink']}_{p.get('material') or 'part'}"
        else:
            raw = p["raw"]
        parts.append(namer(raw))
    # the air: none where a solid fills the whole domain; one more space inside each sealed
    # enclosure that also has air outside it
    if any(p["full_box"] and domain_box.inside(p["box"], noise) for p in kept):
        return tuple(parts)
    devices = [(o, snapped(o)) for o in objs
               if o.kind in ("grille", "rectangular2dFan", "axial3dFan")
               and o.location is not None and o.size is not None]
    sealed = 0
    for p in kept:
        if p["kind"] != "enclosure" or not p["box"].inside(domain_box, noise):
            continue
        walls = _enclosure_walls(p["box"], float(p["wall_thickness"]), noise)
        if not walls or domain_box.inside(p["box"], noise):
            continue                      # solid throughout, or no air outside it
        if not any(_device_cut(o, box, axis, lo, hi, slab, noise) is not None
                   for o, box in devices for axis, lo, hi, slab in walls):
            sealed += 1
    extra = [namer(f"{FLUID_NAME}_{i + 2}") for i in range(sealed)]
    return (air, *extra, *parts)


# ------------------------------------------------------------------------------ STEP --------
def write_named_step(named: list[tuple[str, object]], dest: Path) -> None:
    """The regions as one STEP, each a top-level shape carrying its name - the form
    cad/regions.components_of reads back - in millimetres, under a millimetre label."""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("ecxml"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for name, shape in named:
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    if not writer.Transfer(doc, STEPControl_AsIs):
        raise EcxmlError("the parts could not be written as STEP")
    if writer.Write(str(dest)) != IFSelect_RetDone:
        raise EcxmlError("the STEP file could not be written")


def ecxml_to_step(src: Path, dest: Path) -> tuple[EcxmlBuild, dict]:
    """Read `src`, build every region, check it is the file's model, write `dest` (STEP, mm)
    and the physics sidecar beside it. Returns the build and the shape statistics of the STEP."""
    from meshpipeline.cad.ingest.cad import shape_stats

    model = read_ecxml(src)
    result = build(model)
    if not result.named:
        raise EcxmlError("the ECXML model holds nothing that can be meshed: no solid and no air")
    write_named_step(result.named, dest)
    Path(str(dest) + SIDECAR_SUFFIX).write_text(json.dumps(result.sidecar, indent=1, default=float))
    stats = shape_stats(_compound([s for _, s in result.named]))
    stats["regions"] = len(result.named)
    stats["fluid_regions"] = sum(1 for r in result.sidecar["regions"] if r["type"] == "fluid")
    return result, stats


#: Rows of each list the brief keeps; the sidecar keeps every one.
_BRIEF_ROWS = 200


def physics_summary(sidecar: dict) -> dict:
    """The thermal model in brief, for the geometry facts the intake and the builder read: which
    region is fluid and which solid, of what and dissipating how much, which boundary patches the
    file implies, and the build report. Everything else stays in the sidecar."""
    def rows(items, keys):
        return [{k: r.get(k) for k in keys if r.get(k) is not None} for r in items[:_BRIEF_ROWS]]

    regions = sidecar.get("regions") or []
    patches = sidecar.get("patches") or []
    brief = {
        "format": "ECXML", "spec": sidecar.get("spec"),
        "model_name": sidecar.get("model_name"), "producer": sidecar.get("producer"),
        "sidecar_suffix": SIDECAR_SUFFIX,
        "regions": rows(regions, ("name", "type", "kind", "material", "power_W")),
        "patches": rows(patches, ("name", "role", "suggested_type", "domain_face", "centre_m",
                                  "normal", "size_m", "size_axes")),
        "total_power_W": sidecar.get("total_power_W"),
        "materials": sorted(sidecar.get("materials") or {}),
        "ambient": (sidecar.get("domain") or {}).get("ambient"),
        "domain_source": (sidecar.get("domain") or {}).get("source"),
        "counts": {k: len(sidecar.get(k) or []) for k in (
            "fans", "grilles", "flow_resistances", "volume_heat_sources", "surface_heat_sources",
            "baffles", "compact_models", "monitor_points", "not_built", "inactive_objects")},
        "build_report": list(sidecar.get("build_report") or [])[:_BRIEF_ROWS],
        "length_unit": "m",
    }
    if len(regions) > _BRIEF_ROWS or len(patches) > _BRIEF_ROWS:
        brief["truncated"] = {"regions": len(regions), "patches": len(patches)}
    return brief


def read_ecxml_sidecar(path) -> dict | None:
    """The ECXML physics written beside a canonical STEP, or None."""
    side = Path(str(path) + SIDECAR_SUFFIX)
    if not side.is_file():
        return None
    try:
        return json.loads(side.read_text())
    except (OSError, ValueError):
        return None
