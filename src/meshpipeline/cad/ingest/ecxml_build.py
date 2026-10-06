# Responsibility: Turn an ECXML model into the canonical multi-solid STEP a conjugate-heat-transfer mesh is built from: one named solid per part, and the air around them.
# Owns: each object's exact OpenCASCADE solid, the overlap rule (later overwrites earlier), vents opened in the walls they sit in, the air region (domain minus every solid), and the physics sidecar.
# Boundaries: geometry in the file's own metres, never scaled; the physics is carried, never turned into solver settings; nothing outside the uploaded file is read.
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
SIDECAR_VERSION = 1

#: Box bounds closer than this (metres) are one value. ECXML writers print 8 decimals, and Flotherm
#: writes float32 values (0.300000011920929), so the same plane is often written 0.0709 here and
#: 0.07090001 there; left apart, a boolean makes a 10 nm sliver between a board and the part
#: sitting on it. The thinnest real layer in a system model (a copper foil, a thermal pad) is tens
#: of micrometres, far above this.
SNAP_M = 1e-6
#: How far a vent's cutter reaches past the wall it opens, at least: well clear of OpenCASCADE's
#: 0.1 um tolerance (these solids are in metres), which merges a face only a few tolerances away.
CUT_REACH_M = 1e-5
#: More solids than any meshable system model; a model this detailed is simplified at the source.
MAX_SOLIDS = 2000
FLUID_NAME = "air"

_AXES = "xyz"


class _Box:
    __slots__ = ("hi", "lo")

    def __init__(self, lo, hi) -> None:
        self.lo = tuple(float(v) for v in lo)
        self.hi = tuple(float(v) for v in hi)

    def overlaps(self, other: _Box, tol: float = SNAP_M) -> bool:
        """Interiors overlap (touching faces do not)."""
        return all(min(self.hi[i], other.hi[i]) - max(self.lo[i], other.lo[i]) > tol
                   for i in range(3))

    def inside(self, other: _Box, tol: float = SNAP_M) -> bool:
        return all(self.lo[i] >= other.lo[i] - tol and self.hi[i] <= other.hi[i] + tol
                   for i in range(3))

    def volume(self) -> float:
        return math.prod(max(0.0, self.hi[i] - self.lo[i]) for i in range(3))

    def as_dict(self) -> dict:
        return {"min": list(self.lo), "max": list(self.hi)}


@dataclass
class _Part:
    """One solid to be written: a region of the mesh."""
    name: str
    kind: str
    objects: list[Obj]
    box: _Box
    shape: object
    order: int
    material: str | None
    power: float = 0.0
    wall: bool = False            # an enclosure or a plate: devices in it open it
    notes: list[str] = field(default_factory=list)
    walls: list[tuple[int, float, float, _Box]] = field(default_factory=list)  # axis, lo, hi, slab
    dropped: str = ""             # why it is not meshed, when it is not


@dataclass
class EcxmlBuild:
    named: list[tuple[str, object]]          # (region name, OCC shape) in write order
    sidecar: dict
    notes: list[str]

    @property
    def region_names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.named)


# ------------------------------------------------------------------------------ helpers -----
def _snapper(values_per_axis: list[list[float]]):
    reps: list[list[float]] = []
    for vals in values_per_axis:
        out: list[float] = []
        for v in sorted(vals):
            if not out or v - out[-1] > SNAP_M:
                out.append(v)
        reps.append(out)

    def snap(axis: int, v: float) -> float:
        import bisect

        r = reps[axis]
        i = bisect.bisect_left(r, v)
        near = [r[j] for j in (i - 1, i) if 0 <= j < len(r) and abs(r[j] - v) <= SNAP_M]
        return min(near, key=lambda rep: abs(rep - v)) if near else v

    return snap


def _occ_box(box: _Box):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(*box.lo), gp_Pnt(*box.hi)).Solid()


def _occ_cylinder(box: _Box, axis: int):
    """The cylinder along `axis` that fills `box`: circular when the two cross sizes agree,
    elliptical otherwise - an exact ellipse swept along the axis, not an approximation."""
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Vec

    size = [box.hi[i] - box.lo[i] for i in range(3)]
    cross = [i for i in range(3) if i != axis]
    d = [0.0, 0.0, 0.0]
    d[axis] = 1.0
    base = [(box.lo[i] + box.hi[i]) / 2 for i in range(3)]
    base[axis] = box.lo[axis]
    a, b = size[cross[0]] / 2, size[cross[1]] / 2
    if abs(a - b) <= SNAP_M:
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*d)), a,
                                        size[axis]).Solid()
    major = cross[0] if a > b else cross[1]
    x = [0.0, 0.0, 0.0]
    x[major] = 1.0
    ellipse = gp_Elips(gp_Ax2(gp_Pnt(*base), gp_Dir(*d), gp_Dir(*x)), max(a, b), min(a, b))
    face = BRepBuilderAPI_MakeFace(
        BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(ellipse).Edge()).Wire()).Face()
    sweep = [0.0, 0.0, 0.0]
    sweep[axis] = size[axis]
    return BRepPrimAPI_MakePrism(face, gp_Vec(*sweep)).Shape()


def _list(shapes):
    from OCP.TopTools import TopTools_ListOfShape

    out = TopTools_ListOfShape()
    for s in shapes:
        out.Append(s)
    return out


def _boolean(kind: str, args, tools):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

    algo = {"cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common, "fuse": BRepAlgoAPI_Fuse}[kind]()
    algo.SetArguments(_list(args))
    algo.SetTools(_list(tools))
    algo.SetFuzzyValue(SNAP_M / 5)
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


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(float(props.Mass()))


def _centroid(shape) -> list[float]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    c = props.CentreOfMass()
    return [float(c.X()), float(c.Y()), float(c.Z())]


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


# ------------------------------------------------------------------------------ the build ---
def _snap_function(model: EcxmlModel, objs: list[Obj]) -> Callable[[Obj], _Box]:
    """An object's box with every bound moved onto the plane it is meant to share (SNAP_M)."""
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
    snap = _snapper(by_axis)

    def snapped(o: Obj) -> _Box:
        return _Box([snap(i, o.lo[i]) for i in range(3)], [snap(i, o.hi[i]) for i in range(3)])

    return snapped


def build(model: EcxmlModel) -> EcxmlBuild:
    objs = model.active_objects
    notes: list[str] = list(model.notes)
    snapped = _snap_function(model, objs)
    domain_box, domain_source = _domain(model.domain, objs, snapped)
    if domain_box is None:
        raise EcxmlError("the ECXML model holds no object with a size and no solution domain, so "
                         "there is nothing to mesh")
    tol = max(SNAP_M, 1e-7 * math.dist(domain_box.lo, domain_box.hi))
    namer = _unique_namer()
    fluid_base = namer(FLUID_NAME)

    parts: list[_Part] = []
    records: dict[str, list] = {k: [] for k in (
        "patches", "fans", "grilles", "flow_resistances", "volume_heat_sources",
        "surface_heat_sources", "baffles", "monitor_points", "compact_models", "not_built",
        "inactive")}
    devices: list[tuple[Obj, _Box]] = []        # objects that open the walls they sit in

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
            continue
        box = snapped(o)
        if k in ("solid3dBlock", "printedCircuitBoard", "twoResistorModel"):
            part = _Part(name="", kind=k, objects=[o], box=box, shape=_occ_box(box),
                         order=o.order, material=o.material or None, power=o.power or 0.0)
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
            parts.append(_Part(name="", kind=k, objects=[o], box=box,
                               shape=_occ_cylinder(box, axis_of_plane(o.plane)), order=o.order,
                               material=o.material or None, power=o.power or 0.0))
        elif k == "enclosure":
            parts.append(_enclosure(o, box))
        elif k == "solid2dBlock":
            _plate(o, box, domain_box, tol, parts, records, notes)
        elif k in ("grille", "rectangular2dFan", "source2dBlock"):
            _face_object(o, box, domain_box, tol, records)
            if k != "source2dBlock":
                devices.append((o, box))
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
        elif k == "sourceBlock":
            records["volume_heat_sources"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "power_W": o.power, "meshed_as": "not marked: the power is applied in the box, "
                                                 "in whatever region it overlaps"})
        elif k == "flowResistance":
            records["flow_resistances"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "loss_coefficient_xyz": o.props["loss_coefficient_xyz"],
                "free_area_ratio_xyz": o.props["free_area_ratio_xyz"],
                "meshed_as": "air: a porous zone in the fluid region, selected by its box"})
    for (_path, material), group in heatsink_parts.items():
        parts.append(_heatsink(group, material, snapped))

    if len(parts) > MAX_SOLIDS:
        raise EcxmlError(f"the model holds {len(parts):,} solid parts, more than the {MAX_SOLIDS:,} "
                         "a multi-region mesh can carry; export a sub-assembly, or simplify detailed "
                         "components (pins, layers) in the authoring tool first")

    _open_walls(parts, devices, model.producer, notes, tol)
    _clip_to_domain(parts, domain_box, notes)
    _resolve_overlaps(parts, model.producer, notes)
    for p in parts:
        if p.shape is None:
            records["not_built"].append({
                "name": p.objects[0].props.get("heatsink") or p.objects[0].name, "kind": p.kind,
                "path": list(p.objects[0].path), "material": p.material, "power_W": p.power,
                "why": p.dropped or "it could not be built"})
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
    airs = _air(domain_box, parts, notes)
    named: list[tuple[str, object]] = []
    fluid_rows = []
    for i, (shape, vol) in enumerate(airs):
        nm = fluid_base if i == 0 else namer(f"{FLUID_NAME}_{i + 1}")
        named.append((nm, shape))
        fluid_rows.append({"name": nm, "type": "fluid", "kind": "air",
                           "volume_m3": vol, "centroid_m": _centroid(shape),
                           "notes": [] if i == 0 else
                           ["an air space not connected to the main air (a sealed cavity)"]})
    for p in parts:
        named.append((p.name, p.shape))

    named = _conformal(named, notes)
    by_name = dict(named)
    regions = []
    for row in fluid_rows:
        regions.append(row)
    for p in parts:
        shape = by_name[p.name]
        regions.append({
            "name": p.name, "type": "solid", "kind": p.kind,
            "material": p.material, "power_W": p.power,
            "objects": [{"name": o.name, "path": list(o.path), "kind": o.kind,
                         "box_m": snapped(o).as_dict(), "power_W": o.power,
                         "material": o.material or None} for o in p.objects],
            "volume_m3": _volume(shape), "centroid_m": _centroid(shape),
            "notes": p.notes})
    domain_patches = _domain_patches(domain_box, records["patches"])
    sidecar = {
        "format": "ECXML", "spec": SPEC, "sidecar_version": SIDECAR_VERSION,
        "model_name": model.name, "producer": model.producer,
        "units": {"length": "m", "temperature": "K", "pressure": "Pa", "power": "W",
                  "thermal_conductivity": "W/mK", "thermal_resistance": "K/W",
                  "specific_heat": "J/kgK", "density": "kg/m3", "flow_rate": "m3/s"},
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
        "notes": notes,
    }
    if model.ignored_elements:
        notes.append("elements the ECXML schema does not define were skipped: "
                     + ", ".join(f"{k} x{v}" for k, v in sorted(model.ignored_elements.items())[:12]))
    return EcxmlBuild(named=named, sidecar=sidecar, notes=notes)


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


def _domain(domain: Domain | None, objs: list[Obj], snapped) -> tuple[_Box | None, str]:
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
    if any(hi[i] - lo[i] <= SNAP_M for i in range(3)):
        return None, ""
    return _Box(lo, hi), ("bounding box of every object (the file sets no solutionDomain; "
                          "JEP181A's own example uses this domain)")


def _enclosure(o: Obj, box: _Box) -> _Part:
    t = float(o.props["wall_thickness_m"])
    outer = _occ_box(box)
    part = _Part(name="", kind="enclosure", objects=[o], box=box, shape=outer, order=o.order,
                 material=o.material or None, power=o.power or 0.0, wall=True)
    part.walls = _enclosure_walls(box, t)
    if part.walls:
        inner = _Box([box.lo[i] + t for i in range(3)], [box.hi[i] - t for i in range(3)])
        part.shape = _boolean("cut", [outer], [_occ_box(inner)])
        part.notes.append(f"enclosure: walls {t * 1000:g} mm thick")
    else:
        part.notes.append(f"enclosure: its walls ({t * 1000:g} mm) meet in the middle, so it is "
                          "solid throughout")
    return part


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
            "thickness_m": box.hi[axis] - box.lo[axis]}


def _plate(o: Obj, box: _Box, domain: _Box, tol: float, parts: list[_Part], records: dict,
           notes: list[str]) -> None:
    face = _on_domain_face(o, box, domain, tol)
    rect = _rect(o, box, domain)
    if face:
        records["patches"].append({
            "name": o.name, "path": list(o.path), "object": "solid2dBlock", "role": "wall",
            "suggested_type": "wall", "domain_face": face, **rect,
            "material": o.material or None, "power_W": o.power,
            "order": o.order,
            "meaning": "a thin plate on the domain boundary: a conducting wall of this material "
                       "and thickness"})
        return
    axis = axis_of_plane(o.plane)
    thickness = box.hi[axis] - box.lo[axis]
    span = min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
    if SNAP_M < thickness <= span:
        # a plate is thinner than it is wide; anything else in the normal size is not a thickness
        part = _Part(name="", kind="solid2dBlock", objects=[o], box=box, shape=_occ_box(box),
                     order=o.order, material=o.material or None, power=o.power or 0.0,
                     wall=True)
        part.walls.append((axis, box.lo[axis], box.hi[axis], box))
        part.notes.append("a 2D plate in the authoring tool, built as a solid of the thickness the "
                          "file gives it so that it blocks the flow and conducts")
        parts.append(part)
        return
    records["baffles"].append({
        "name": o.name, "path": list(o.path), **rect, "material": o.material or None,
        "power_W": o.power,
        "meshed_as": "not in the mesh: a plate without a usable thickness is no volume, and the "
                     "multi-region mesh has no internal baffle yet; the air flows through it"})
    notes.append(f"{o.label}: a plate inside the domain without a usable thickness ("
                 f"{thickness * 1000:g} mm) is not in the mesh (the air flows through it); give it "
                 "its real thickness in the authoring tool to mesh it")


def _face_object(o: Obj, box: _Box, domain: _Box, tol: float, records: dict) -> None:
    face = _on_domain_face(o, box, domain, tol)
    rect = _rect(o, box, domain if face else None)
    row: dict = {"name": o.name, "path": list(o.path), "object": o.kind, **rect,
                 "order": o.order}
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


def _heatsink(group: list[Obj], material: str, snapped) -> _Part:
    boxes = [snapped(o) for o in group]
    shapes = [_occ_box(b) for b in boxes]
    shape = shapes[0] if len(shapes) == 1 else _unify(_boolean("fuse", shapes[:1], shapes[1:]))
    lo = [min(b.lo[i] for b in boxes) for i in range(3)]
    hi = [max(b.hi[i] for b in boxes) for i in range(3)]
    hs = group[0].props["heatsink"]
    part = _Part(name="", kind="heatsink", objects=list(group), box=_Box(lo, hi), shape=shape,
                 order=max(o.order for o in group), material=material or None,
                 power=sum(float(o.power or 0.0) for o in group))
    # the region is named after the heatsink; its parts keep their own names in `objects`
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


def _device_prism(o: Obj, box: _Box, axis: int, lo: float, hi: float, slab: _Box,
                  tol: float):
    """The solid a device cuts out of a wall slab (see _device_cut), or None."""
    cut = _device_cut(o, box, axis, lo, hi, slab, tol)
    if cut is None:
        return None
    return _occ_cylinder(cut, axis) if o.kind == "axial3dFan" else _occ_box(cut)


def _enclosure_walls(box: _Box, t: float) -> list[tuple[int, float, float, _Box]]:
    """An enclosure's six wall slabs (axis, inner bound, outer bound, slab box); none when its
    walls meet in the middle."""
    if not all(box.hi[i] - box.lo[i] > 2 * t + SNAP_M for i in range(3)):
        return []
    walls = []
    for axis in range(3):
        for lo, hi in ((box.lo[axis], box.lo[axis] + t), (box.hi[axis] - t, box.hi[axis])):
            slab_lo, slab_hi = list(box.lo), list(box.hi)
            slab_lo[axis], slab_hi[axis] = lo, hi
            walls.append((axis, lo, hi, _Box(slab_lo, slab_hi)))
    return walls


def _open_walls(parts: list[_Part], devices: list[tuple[Obj, _Box]], producer: str,
                notes: list[str], tol: float) -> None:
    for part in parts:
        if not part.wall or not part.walls:
            continue
        cutters, opened = [], []
        for o, box in devices:
            for axis, lo, hi, slab in part.walls:
                prism = _device_prism(o, box, axis, lo, hi, slab, tol)
                if prism is not None:
                    cutters.append(prism)
                    opened.append(o.name)
                    break
        if cutters:
            part.shape = _boolean("cut", [part.shape], cutters)
            part.notes.append(f"opened where {', '.join(sorted(set(opened))[:8])} sit(s) in it")
            notes.append(f"{part.objects[0].label}: opened where "
                         f"{', '.join(sorted(set(opened))[:8])} sit(s) in its wall")


def _clip_to_domain(parts: list[_Part], domain: _Box, notes: list[str]) -> None:
    dom = None
    for p in parts:
        if p.box.inside(domain):
            continue
        if not p.box.overlaps(domain, 0.0):
            notes.append(f"{p.objects[0].label}: outside the solution domain, so not meshed")
            p.shape = None
            p.dropped = "it lies outside the solution domain"
            continue
        if dom is None:
            dom = _occ_box(domain)
        p.shape = _boolean("common", [p.shape], [dom])
        p.notes.append("clipped to the solution domain")


def _resolve_overlaps(parts: list[_Part], producer: str, notes: list[str]) -> None:
    """Where solids overlap, the winner keeps the overlap: the later object in the file, or for
    Icepak a solid fully inside another (JEP181A 4.5.1). Cut from the originals, so the result does
    not depend on the order the cuts are made in."""
    icepak = producer.strip().lower() == "icepak"
    live = [p for p in parts if p.shape is not None]

    def wins(a: _Part, b: _Part) -> bool:
        if icepak and a.box.inside(b.box) and not b.box.inside(a.box):
            return True
        if icepak and b.box.inside(a.box) and not a.box.inside(b.box):
            return False
        return a.order > b.order

    originals = {id(p): p.shape for p in live}
    for p in live:
        winners = [originals[id(q)] for q in live
                   if q is not p and q.box.overlaps(p.box) and wins(q, p)]
        if not winners:
            continue
        before = _volume(p.shape)
        cut = _boolean("cut", [p.shape], winners)
        after = _volume(cut) if _solids(cut) else 0.0
        if after <= 1e-9 * max(before, 1e-30) or not _solids(cut):
            notes.append(f"{p.objects[0].label}: wholly overwritten by later objects, so not meshed")
            p.shape = None
            p.dropped = "objects that take precedence overwrite all of it (JEP181A 4.5.1)"
            continue
        if after < before * (1 - 1e-9):
            p.notes.append(f"{100 * (1 - after / before):.3g}% of it is overwritten by objects "
                           "that take precedence (JEP181A 4.5.1)")
        p.shape = _unify(cut)


def _air(domain: _Box, parts: list[_Part], notes: list[str]) -> list[tuple[object, float]]:
    box = _occ_box(domain)
    solids = [p.shape for p in parts if p.shape is not None]
    air = _boolean("cut", [box], solids) if solids else box
    pieces = [(s, _volume(s)) for s in _solids(air)]
    pieces = [(s, v) for s, v in pieces if v > 1e-12 * domain.volume()]
    pieces.sort(key=lambda sv: -sv[1])
    if not pieces:
        notes.append("the parts fill the whole domain: there is no air to mesh")
        return []
    if len(pieces) > 1:
        notes.append(f"the air is {len(pieces)} separate spaces (a sealed enclosure keeps its own "
                     "air); each is its own fluid region")
    return [(_unify(s), v) for s, v in pieces]


def _conformal(named: list[tuple[str, object]], notes: list[str]) -> list[tuple[str, object]]:
    """Every region split where it touches another (a General Fuse), so two regions that share a
    face share it exactly - the conformal interface a multi-region mesh couples across. A fuse
    that fails leaves the regions as they are: they still touch, only not face for face."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo

    if len(named) < 2:
        return named
    try:
        algo = BRepAlgoAPI_BuilderAlgo()
        algo.SetArguments(_list([s for _, s in named]))
        algo.SetFuzzyValue(SNAP_M / 5)
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
        return out
    except Exception as exc:  # noqa: BLE001 - the regions are right; only the face split is lost
        notes.append(f"the shared faces between regions could not be split face for face ({exc})")
        return named


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
    snapped = _snap_function(model, objs)
    domain_box, _source = _domain(model.domain, objs, snapped)
    if domain_box is None:
        return ()
    tol = max(SNAP_M, 1e-7 * math.dist(domain_box.lo, domain_box.hi))
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
        elif o.kind == "solid2dBlock" and not _on_domain_face(o, box, domain_box, tol):
            axis = axis_of_plane(o.plane)
            thickness = box.hi[axis] - box.lo[axis]
            span = min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
            if SNAP_M < thickness <= span:
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
        if icepak and a["box"].inside(b["box"]) and not b["box"].inside(a["box"]):
            return True
        if icepak and b["box"].inside(a["box"]) and not a["box"].inside(b["box"]):
            return False
        return a["order"] > b["order"]

    kept = [p for p in planned if p["box"].overlaps(domain_box, 0.0)
            and not any(q is not p and q["full_box"] and p["box"].inside(q["box"]) and wins(q, p)
                        for q in planned)]
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
    if any(p["full_box"] and domain_box.inside(p["box"]) for p in kept):
        return tuple(parts)
    devices = [(o, snapped(o)) for o in objs
               if o.kind in ("grille", "rectangular2dFan", "axial3dFan")
               and o.location is not None and o.size is not None]
    sealed = 0
    for p in kept:
        if p["kind"] != "enclosure" or not p["box"].inside(domain_box):
            continue
        walls = _enclosure_walls(p["box"], float(p["wall_thickness"]))
        if not walls or domain_box.inside(p["box"]):
            continue                      # solid throughout, or no air outside it
        if not any(_device_cut(o, box, axis, lo, hi, slab, tol) is not None
                   for o, box in devices for axis, lo, hi, slab in walls):
            sealed += 1
    extra = [namer(f"{FLUID_NAME}_{i + 2}") for i in range(sealed)]
    return (air, *extra, *parts)


# ------------------------------------------------------------------------------ STEP --------
def write_named_step(named: list[tuple[str, object]], dest: Path) -> None:
    """The regions as one STEP, each a top-level shape carrying its name - the form
    cad/regions.components_of reads back. Coordinates are written unchanged under OpenCASCADE's
    millimetre label (as cad/ingest/cad.write_step does); the ECXML's metres are recorded beside it,
    in the ingest sidecar, so the staging seam scales by the confirmed unit alone."""
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
    """Read `src`, build every region, write `dest` (STEP) and the physics sidecar beside it.
    Returns the build and the shape statistics of what was written."""
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
    region is fluid and which solid, of what and dissipating how much, and which boundary patches
    the file implies. Everything else stays in the sidecar."""
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
