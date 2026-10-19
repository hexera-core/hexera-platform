# Responsibility: Place every part of an ECXML model at the coordinates the file gives, with no boolean between parts: the input of the placed-parts (snap-grid) mesher and of the geometry check's part-by-part picture.
# Owns: the placed parts (each one's material boxes, its vent cut-outs, its cylinder), the order in which overlapping parts are painted (the file's precedence rule as a total order), the planes the model states, and the per-part skin.
# Boundaries: no OpenCASCADE and no meshing; the physics records, tolerances, names and overlap rule are #163's (cad/ingest/ecxml_build.py) and are reused, never re-decided.
# Collaborates with: cad/ingest/ecxml.py (the model), cad/ingest/ecxml_build.py (helpers and the sidecar shape), engines/snapgrid (meshes the placement), application/geometry_check.py (draws it).
"""ECXML -> placed parts. Option B of the ECXML path: "no gluing".

Flotherm, 6SigmaET and Icepak never join the parts of an electronics model. Each part stays what
the file says - a box, a cylinder, a hollow enclosure - at the place the file gives it, and the
mesher decides cell by cell which part a cell belongs to. Two parts that touch then share cell
faces by construction, and nothing has to be fused.

This module does the placing. It reads the model exactly as cad/ingest/ecxml_build.py does (the
same snapping of coordinates to the file's own precision, the same domain, the same records of
fans, vents, sources and plates, the same overlap rule) and stops before any solid is built:

* every part is a list of material boxes (a block: its box; an enclosure: its six wall slabs; a
  heat sink: its blocks; a plate with a thickness: its slab) plus, for a cylinder, the cylinder
  inscribed in its box;
* a grille, a 2D fan or an axial fan sitting in an enclosure wall or a plate cuts that wall open
  over its outline (a box, or the fan's circle) - the same rule as the fused build;
* where parts overlap, the file's precedence decides (JEP181A 4.5.1: the later object wins;
  Icepak: the embedded one wins). The pairwise rule is turned into a PAINT ORDER - a cell belongs
  to the last part in that order that covers it - by a topological sort of the overlap graph, so
  the result is the same as cutting each part by every part that wins over it;
* every coordinate the model states (part faces, vent outlines, the rectangles of the fans and
  vents on the domain sides, source and resistance boxes) is collected per axis: the planes a
  Cartesian grid must hold so that every box is exact.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.ingest.ecxml import EcxmlError, EcxmlModel, Obj, axis_of_plane, sign_of_plane
from meshpipeline.cad.ingest.ecxml_build import (
    _AXES,
    FLUID_NAME,
    _Box,
    _device_cut,
    _direction,
    _domain,
    _enclosure,
    _face_object,
    _heatsink,
    _material,
    _pair_scan,
    _Part,
    _plate,
    _precision,
    _records_in_domain,
    _Tol,
    _um,
    _unique_namer,
    length_words,
)

#: More parts than a one-region-per-part conjugate case can carry: the solver reads one mesh and
#: one property set per region. (The fused build stops at 2000; placing costs nothing per part, so
#: the limit here is the case, not the geometry.)
MAX_PLACED_PARTS = 20_000


@dataclass
class Placement:
    """An ECXML model with every part placed and nothing joined."""
    model: EcxmlModel
    domain: _Box
    domain_source: str
    parts: list[_Part]                     # solid parts, file order (heat sinks after the rest)
    paint: list[int]                       # indices into parts, lowest precedence first
    records: dict[str, list]               # patches, fans, grilles, ... as the fused build keeps them
    fate: dict[int, str]                   # file order -> where the object went
    notes: list[str]
    report: list[str]
    tol: _Tol
    planes: list[list[float]]              # every plane the model states, per axis (inside the domain)
    names: list[str] = field(default_factory=list)   # display name per part (the region name to be)
    order_cycles: int = 0                  # precedence cycles the file's rule could not order
    snapped: Any = None                    # Obj -> its box as placed (the planes it shares)
    roundoff: list = field(default_factory=list)   # planes read as one beyond the precision

    @property
    def size(self) -> float:
        return math.dist(self.domain.lo, self.domain.hi)


def _records() -> dict[str, list]:
    return {k: [] for k in (
        "patches", "fans", "grilles", "flow_resistances", "volume_heat_sources",
        "surface_heat_sources", "baffles", "monitor_points", "compact_models", "not_built",
        "inactive", "thin_layers", "thin_layers_meshed")}


def place(model: EcxmlModel, layers=None) -> Placement:
    """Every active object of the model at its place: solid parts with their material boxes, the
    physics the mesh does not carry in records, and the order overlapping parts are painted in.

    THIN LAYERS (cad/ingest/ecxml_films): a layer too thin to mesh with sound cells and thermally
    thin is carried as a thin-layer thermal resistance - placed once, the rule decides, and the
    model is placed again with that layer's place closed by its better-conducting neighbour; the
    layer is a record (records["thin_layers"]) the mesher puts on the interface. `layers`: the
    ecxml_films.LayerRule (the grid it would be meshed in); None: the default rule; False: every
    layer meshed. Raises EcxmlError when there is nothing to place."""
    from meshpipeline.cad.ingest import ecxml_films

    first = _place(model, None)
    if layers is False:
        return first
    plan = ecxml_films.decide(first, layers or ecxml_films.LayerRule())
    if not plan:
        first.records["thin_layers_meshed"] = plan.kept
        return first
    second = _place(model, plan)
    second.records["thin_layers_meshed"] = plan.kept
    return second


def _place(model: EcxmlModel, collapse) -> Placement:
    objs = model.active_objects
    notes: list[str] = list(model.notes)
    report: list[str] = []
    precision = _precision(model, objs)
    noise = precision.noise
    snapped, moved, roundoff = _snap_roundoff(model, objs, precision)
    if collapse:
        snapped = _closing(snapped, collapse.moves, noise)
    domain, domain_source = _domain(model.domain, objs, snapped, noise)
    if domain is None:
        raise EcxmlError("the ECXML model holds no object with a size and no solution domain, so "
                         "there is nothing to mesh")
    parts: list[_Part] = []
    records = _records()
    devices: list[tuple[Obj, _Box]] = []
    fate: dict[int, str] = {}
    extra: list[_Box] = []                 # boxes whose faces the grid holds, beyond the parts

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
        if collapse and o.order in collapse.skip:
            row = collapse.skip[o.order]
            records["thin_layers"].append(row)
            fate[o.order] = "thin-layer thermal resistance (in the sidecar)"
            # its outline stays on the grid, so the faces that carry it are exactly its footprint
            a = "xyz".index(row["axis"])
            box = snapped(o)
            lo, hi = list(box.lo), list(box.hi)
            lo[a] = hi[a] = row["plane_m"]
            extra.append(_Box(lo, hi))
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
                         material=o.material or None, power=o.power or 0.0, make=_no_solid)
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
            parts.append(_Part(name="", kind=k, objects=[o], box=box, order=o.order,
                               material=o.material or None, power=o.power or 0.0,
                               make=_no_solid))
        elif k == "enclosure":
            parts.append(_enclosure(o, box, noise))
        elif k == "solid2dBlock":
            if not _plate(o, box, domain, noise, parts, records, notes):
                fate[o.order] = "boundary patch or baffle"
                extra.append(_flat(o, box))
        elif k in ("grille", "rectangular2dFan", "source2dBlock"):
            _face_object(o, box, domain, noise, records)
            if k != "source2dBlock":
                devices.append((o, box))
            fate[o.order] = "boundary patch or internal plane"
            extra.append(_flat(o, box))
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
                "meshed_as": "air: the fan's swept volume is part of the fluid region; its box "
                             "lies on grid planes, so a solver selects it exactly"})
            devices.append((o, box))
            fate[o.order] = "fan (air, in the sidecar)"
            extra.append(box)
        elif k == "sourceBlock":
            records["volume_heat_sources"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "power_W": o.power, "meshed_as": "not marked: the power is applied in the box, "
                                                 "whose faces lie on grid planes"})
            fate[o.order] = "volume heat source (in the sidecar)"
            extra.append(box)
        elif k == "flowResistance":
            records["flow_resistances"].append({
                "name": o.name, "path": list(o.path), "box_m": box.as_dict(),
                "loss_coefficient_xyz": o.props["loss_coefficient_xyz"],
                "free_area_ratio_xyz": o.props["free_area_ratio_xyz"],
                "meshed_as": "air: a porous zone in the fluid region, selected by its box, whose "
                             "faces lie on grid planes"})
            fate[o.order] = "flow resistance (air, in the sidecar)"
            extra.append(box)
    for (_path, material), group in heatsink_parts.items():
        parts.append(_heatsink(group, material, snapped))
    # devices, sources and probes outside the domain are not applied; partly outside, they keep
    # the box inside it (the fused build's rule, cad/ingest/ecxml_build._records_in_domain)
    notes += _records_in_domain(records, domain, noise)
    if len(parts) > MAX_PLACED_PARTS:
        raise EcxmlError(f"the model holds {len(parts):,} solid parts, more than the "
                         f"{MAX_PLACED_PARTS:,} regions a conjugate case can carry; export a "
                         "sub-assembly, or merge detailed components in the authoring tool")

    _open_walls(parts, devices, notes, noise)
    for p in parts:
        if _material(p) and all(any(b.hi[i] - b.lo[i] <= noise for i in range(3))
                                for b in _material(p)):
            p.dropped = thinner_why(noise)          # flattened: its two faces are one plane
        elif not p.box.overlaps(domain, 0.0):
            p.dropped = "it lies outside the solution domain"
        elif not p.box.inside(domain, noise):
            p.clipped = True
            p.notes.append("clipped to the solution domain")
        for o in p.objects:
            fate[o.order] = "solid region" if not p.dropped else f"not built ({p.dropped})"
    paint, cycles = _paint_order(parts, model.producer, noise)
    tol = _measure_plain(parts, domain, noise, moved)
    planes = _planes(parts, extra, records, domain)
    if cycles:
        report.append(f"Overlap: the file's precedence rule went round in a circle for {cycles} "
                      "part(s); those are painted in file order (the later object wins).")
    report += roundoff_lines(roundoff, precision)
    names = _names(parts)
    if collapse:
        report += thin_layer_lines(records["thin_layers"])
    return Placement(model=model, domain=domain, domain_source=domain_source, parts=parts,
                     paint=paint, records=records, fate=fate, notes=notes, report=report,
                     tol=tol, planes=planes, names=names, order_cycles=cycles,
                     snapped=snapped, roundoff=roundoff)


def _closing(snapped, moves: list[dict[float, float]], noise: float):
    """The snapped boxes with the planes a collapsed thin layer leaves moved onto the plane it
    collapses to (cad/ingest/ecxml_films.decide): its neighbour grows into its place."""
    cache: dict[int, _Box] = {}

    def to(axis: int, v: float) -> float:
        for frm, dest in moves[axis].items():
            if abs(v - frm) <= noise:
                return dest
        return v

    def closed(o: Obj) -> _Box:
        if o.order not in cache:
            b = snapped(o)
            cache[o.order] = _Box([to(i, b.lo[i]) for i in range(3)],
                                  [to(i, b.hi[i]) for i in range(3)])
        return cache[o.order]

    return closed


def thin_layer_lines(rows: list[dict]) -> list[str]:
    """The report's words for the thin layers carried as resistances."""
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r["thickness_m"])
    shown = "; ".join(
        f"{r['label']} ({length_words(r['thickness_m'])} of {r.get('material') or '?'}, "
        f"k {r['k_through_W_mK']:.3g} W/mK: R = {r['R_m2K_W']:.3g} m2K/W)" for r in rows[:8])
    more = f", and {len(rows) - 8} more" if len(rows) > 8 else ""
    return [f"Thin layers: {len(rows)} layer(s) too thin to mesh with sound cells (they would be "
            "over 1000 times wider than thick) and thermally thin (they carry under 1% of the "
            "sideways conduction beside them) are each a thin-layer thermal resistance R = t / k on "
            f"the interface between their neighbours, as Flotherm's collapsed cuboids and Icepak's "
            f"contact resistances are: {shown}{more}. The neighbour that conducts better fills "
            "the layer's place, and what it adds is taken off the layer's own resistance, so the "
            "resistance through the stack is the file's."]


def _no_solid(_tol):
    raise EcxmlError("internal: a placed part has no OpenCASCADE solid (the placed-parts path "
                     "never builds one)")


def _flat(o: Obj, box: _Box) -> _Box:
    """A 2D object's rectangle (its plane at its location), whatever its normal size says."""
    axis = axis_of_plane(o.plane)
    hi = list(box.hi)
    hi[axis] = box.lo[axis]
    return _Box(box.lo, hi)


def _open_walls(parts: list[_Part], devices: list[tuple[Obj, _Box]], notes: list[str],
                noise: float) -> None:
    """The fused build's rule, without the boolean: a device sitting in a wall slab of an
    enclosure or a plate cuts that slab over its outline (cad/ingest/ecxml_build._open_walls)."""
    for part in parts:
        if not part.wall or not part.walls:
            continue
        opened = []
        for o, box in devices:
            for axis, lo, hi, slab in part.walls:
                cut = _device_cut(o, box, axis, lo, hi, slab, noise)
                if cut is not None:
                    part.cutters.append(("round" if o.kind == "axial3dFan" else "box", cut, axis))
                    opened.append(o.name)
                    break
        if opened:
            part.notes.append(f"opened where {', '.join(sorted(set(opened))[:8])} sit(s) in it")
            notes.append(f"{part.objects[0].label}: opened where "
                         f"{', '.join(sorted(set(opened))[:8])} sit(s) in its wall")


def _wins(producer: str, noise: float):
    icepak = producer.strip().lower() == "icepak"

    def wins(a: _Part, b: _Part) -> bool:
        if icepak and a.box.inside(b.box, noise) and not b.box.inside(a.box, noise):
            return True
        if icepak and b.box.inside(a.box, noise) and not a.box.inside(b.box, noise):
            return False
        return a.order > b.order

    return wins


def _paint_order(parts: list[_Part], producer: str, noise: float) -> tuple[list[int], int]:
    """The parts in the order a cell is painted (lowest precedence first), so that a cell belongs
    to the last part covering it - the same result as cutting each part by every part that wins
    over it (the fused build's rule, cad/ingest/ecxml_build._resolve_overlaps). Overlap is judged
    on the material (a part inside a housing does not overlap the housing). Ties and parts that
    overlap nothing keep file order. A circle in the pairwise rule (possible only with Icepak's
    embedded-wins exception) is broken by file order and counted."""
    import numpy as np

    live = [k for k, p in enumerate(parts) if not p.dropped]
    wins = _wins(producer, noise)
    pieces: list[_Box] = []
    owner: list[int] = []
    for k in live:
        for b in _material(parts[k]):
            pieces.append(b)
            owner.append(k)
    after: dict[int, set[int]] = {k: set() for k in live}     # loser -> winners (painted later)
    if len(pieces) > 1:
        lo = np.array([b.lo for b in pieces])
        hi = np.array([b.hi for b in pieces])
        _best, _pair, overlap, _touch = _pair_scan(lo, hi, noise)
        seen: set[tuple[int, int]] = set()
        for a, b in overlap:
            ka, kb = sorted((owner[a], owner[b]))
            if ka == kb or (ka, kb) in seen:
                continue
            seen.add((ka, kb))
            pa, pb = parts[ka], parts[kb]
            if wins(pa, pb):
                after[kb].add(ka)
                pb.winners.append(pa)
            else:
                after[ka].add(kb)
                pa.winners.append(pb)
    # Kahn's algorithm: a part is painted once every part it wins over has been painted; among
    # the ready ones the earliest in the file goes first
    indegree = dict.fromkeys(live, 0)
    for ws in after.values():
        for w in ws:
            indegree[w] += 1
    ready = [(parts[k].order, k) for k in live if indegree[k] == 0]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        _o, k = heapq.heappop(ready)
        order.append(k)
        for w in after[k]:
            indegree[w] -= 1
            if indegree[w] == 0:
                heapq.heappush(ready, (parts[w].order, w))
    left = sorted((k for k in live if k not in set(order)), key=lambda k: parts[k].order)
    return order + left, len(left)


THINNER_WHY = "it is thinner than the file's own precision"


def thinner_why(noise: float) -> str:
    """Why a part the file's precision flattens is not built (the words the report keys on)."""
    return (f"{THINNER_WHY} ({_um(noise)}) on some axis, so the file cannot place both of its "
            "faces")


#: Exporters round a plane they compute (a location plus a size, a unit conversion) differently
#: from the same plane written as a number. Analog Devices' public models, written to 8 places
#: (10 nm), carry steps of 1 to 6 units of that last place between faces that are meant to meet;
#: float32 exports carry steps of about their own noise (30 nm at 0.12 m). So two planes of
#: DIFFERENT objects are one plane when they are closer than ROUNDOFF_UNITS units of the file's
#: print limit, or ROUNDOFF_NOISE times its binary-float noise - a reach that comes from the file's
#: own digits, never a fixed length: a file written to 12 places reads a 10 nm step as 10 nm.
#: (Never when it would change an object's extent by more than ROUNDOFF_THICKNESS_SHARE.)
ROUNDOFF_UNITS = 10.0
ROUNDOFF_NOISE = 2.0
#: ... and never when it would change the extent of an object a plane bounds by more than this
#: share of it: that is the object's own size, not round-off (a 50 nm film beside a 30 nm one keeps
#: its 50 nm, and a 10 nm film its own two faces, whatever the reach).
ROUNDOFF_THICKNESS_SHARE = 0.01


def roundoff_reach(precision) -> float:
    """How close two planes of different objects may be and still be read as one, metres."""
    return max(precision.noise, ROUNDOFF_UNITS * precision.printed,
               ROUNDOFF_NOISE * precision.float32)


def reach_words(precision) -> str:
    """Where the round-off reach comes from, in words."""
    if ROUNDOFF_UNITS * precision.printed >= max(precision.noise,
                                                ROUNDOFF_NOISE * precision.float32):
        return (f"{ROUNDOFF_UNITS:g} units of the last digit the file writes, "
                f"{length_words(precision.printed)}")
    if ROUNDOFF_NOISE * precision.float32 >= precision.noise:
        return (f"{ROUNDOFF_NOISE:g} times the float32 noise of its numbers, "
                f"{length_words(precision.float32)}")
    return "the file's own precision"


def _snap_roundoff(model: EcxmlModel, objs: list[Obj], precision):
    """The fused build's snapping (every bound onto the plane it is meant to share, planes within
    the file's precision one plane), widened to exporter round-off: planes closer than
    roundoff_reach(precision) are one plane - the one most objects state (the domain's own sides
    first) - as long as reading them as one changes no object's extent by more than
    ROUNDOFF_THICKNESS_SHARE of it. So a part's own two faces never become one plane, and a 50 nm
    film keeps its 50 nm beside a 30 nm one however far the reach is; a 60 nm step between the
    faces of two 90 um solder bumps is still one plane. Returns snapped(o) -> _Box, the moved
    counts, and the merges beyond the file's precision (what the report says)."""
    noise = precision.noise
    merge = roundoff_reach(precision)
    by_axis: list[list[tuple[float, int, int]]] = [[], [], []]   # (value, owner, 0 lo / 1 hi)
    extent: dict[tuple[int, int], float] = {}
    names: dict[int, str] = {-1: "the solution domain"}
    for o in objs:
        if o.location is not None and o.size is not None:
            lo, hi = o.lo, o.hi
            names[o.order] = o.label
            for i in range(3):
                by_axis[i] += [(lo[i], o.order, 0), (hi[i], o.order, 1)]
                extent[(o.order, i)] = abs(hi[i] - lo[i])
    if model.domain is not None:
        d = model.domain
        for i in range(3):
            by_axis[i] += [(d.location[i], -1, 0), (d.location[i] + d.size[i], -1, 1)]
            extent[(-1, i)] = abs(d.size[i])

    def room(w: tuple[float, int, int], i: int) -> float:
        """How far a plane of this object may move: a share of its extent (a flat object - a
        vent, a fan face - moves whole)."""
        e = extent.get((w[1], i), math.inf)
        return math.inf if e <= noise else ROUNDOFF_THICKNESS_SHARE * e

    rep_of: dict[tuple[int, int, int], float] = {}         # (owner, axis, lo/hi) -> plane
    merges: list[dict] = []
    for i, vals in enumerate(by_axis):
        vals.sort()
        clusters: list[list[tuple[float, int, int]]] = []
        reach_left: list[float] = []          # the least room of any member, per cluster
        for v in vals:
            if clusters:
                span = v[0] - clusters[-1][0][0]
                if span <= noise or (span <= merge
                                     and span <= min(reach_left[-1], room(v, i))):
                    clusters[-1].append(v)
                    reach_left[-1] = min(reach_left[-1], room(v, i))
                    continue
            clusters.append([v])
            reach_left.append(room(v, i))
        for cl in clusters:
            values = sorted({w[0] for w in cl})
            if any(w[1] == -1 for w in cl):
                rep = next(w[0] for w in cl if w[1] == -1)
            elif values[-1] - values[0] <= noise:
                rep = values[0]                      # the fused build's choice
            else:
                count: dict[float, int] = {}
                for w in cl:
                    count[w[0]] = count.get(w[0], 0) + 1
                rep = max(values, key=lambda x: (count[x], -x))
            for w in cl:
                rep_of[(w[1], i, w[2])] = rep
            if values[-1] - values[0] > noise:
                his = [w for w in cl if w[2] == 1 and w[1] >= 0]
                los = [w for w in cl if w[2] == 0 and w[1] >= 0]
                closed = sorted({(names[a[1]], names[b[1]], b[0] - a[0]) for a in his for b in los
                                 if a[1] != b[1] and b[0] - a[0] > noise},
                                key=lambda r: -r[2])
                merges.append({"axis": "xyz"[i], "at_m": rep, "step_m": values[-1] - values[0],
                               "objects": sorted({names[w[1]] for w in cl})[:6],
                               "gaps_closed": [{"between": [a, b], "gap_m": g}
                                               for a, b, g in closed[:4]]})
    moved = {"count": 0, "max": 0.0}
    cache: dict[int, _Box] = {}

    def snapped(o: Obj) -> _Box:
        if o.order not in cache:
            box = []
            for side, vals in ((0, o.lo), (1, o.hi)):
                row = []
                for i in range(3):
                    v = rep_of.get((o.order, i, side), vals[i])
                    if v != vals[i]:
                        moved["count"] += 1
                        moved["max"] = max(moved["max"], abs(v - vals[i]))
                    row.append(v)
                box.append(row)
            cache[o.order] = _Box(box[0], box[1])
        return cache[o.order]

    return snapped, moved, merges


def roundoff_lines(merges: list[dict], precision=None) -> list[str]:
    """The report's words for the planes read as one beyond the file's precision."""
    if not merges:
        return []
    reach = (roundoff_reach(precision) if precision is not None
             else max(m["step_m"] for m in merges))
    why = reach_words(precision) if precision is not None else "the file's own precision"
    worst = max(merges, key=lambda m: m["step_m"])
    line = (f"Exporter round-off: {len(merges)} group(s) of planes of different objects closer "
            f"than {length_words(reach)} ({why}) were read as one plane each (largest step "
            f"{length_words(worst['step_m'])}, on {worst['axis']} at {worst['at_m']:.9g} m: "
            + ", ".join(worst["objects"][:4]) + "). Planes that close which bound a part are "
            f"kept where the file puts them when reading them as one would change the part's "
            f"thickness by more than {ROUNDOFF_THICKNESS_SHARE:.0%}.")
    gaps = [g for m in merges for g in m["gaps_closed"]]
    if gaps:
        gaps.sort(key=lambda g: -g["gap_m"])
        line += (f" {len(gaps)} gap(s) that thin are closed, so those parts touch: "
                 + "; ".join(f"{g['between'][0]} and {g['between'][1]} "
                             f"({length_words(g['gap_m'])})" for g in gaps[:6])
                 + ("..." if len(gaps) > 6 else "") + ".")
    return [line]


def _measure_plain(parts: list[_Part], domain: _Box, noise: float, moved: dict) -> _Tol:
    """The smallest real gap between parts and the thinnest part (for the report) - no fuzzy
    value: nothing is joined, so nothing has to be closed by tolerance."""
    import numpy as np

    boxes: list[_Box] = []
    owners: list[str] = []
    for p in parts:
        if p.dropped:
            continue
        label = p.objects[0].props.get("heatsink") or p.objects[0].label
        for b in _material(p):
            c = b.common(domain)
            if c is not None:
                boxes.append(c)
                owners.append(label)
    gap, gap_where = math.inf, ""
    thick, thick_where = math.inf, ""
    for box, who in zip(boxes, owners):
        t = min(box.hi[i] - box.lo[i] for i in range(3))
        if t < thick:
            thick, thick_where = t, who
    if len(boxes) > 1:
        lo = np.array([b.lo for b in boxes])
        hi = np.array([b.hi for b in boxes])
        best, (ia, ib), _overlap, _touch = _pair_scan(lo, hi, noise)
        if best < gap:
            gap, gap_where = best, f"{owners[ia]} and {owners[ib]}"
    return _Tol(noise=noise, fuzzy=0.0, gap=gap, gap_where=gap_where, thick=thick,
                thick_where=thick_where, snapped=int(moved["count"]),
                snap_shift=float(moved["max"]))


def _planes(parts: list[_Part], extra: list[_Box], records: dict, domain: _Box
            ) -> list[list[float]]:
    """Every plane the model states, per axis, inside the domain: the faces of each part's
    material, of its vent cut-outs (across the wall: the wall's own faces bound it along it), of
    each cylinder's box, of the 2D objects, sources, resistances and fan boxes."""
    out: list[list[float]] = [[domain.lo[i], domain.hi[i]] for i in range(3)]

    def add(box: _Box, skip_axis: int | None = None) -> None:
        for i in range(3):
            if i == skip_axis:
                continue
            for v in (box.lo[i], box.hi[i]):
                if domain.lo[i] < v < domain.hi[i]:
                    out[i].append(v)

    for p in parts:
        if p.dropped:
            continue
        for b in _material(p):
            add(b)
        add(p.box)
        for _kind, cut, axis in p.cutters:
            add(cut, skip_axis=axis)
    for b in extra:
        add(b)
    for row in records["patches"]:
        add(_rect_box(row))
    return [sorted(set(v)) for v in out]


def _rect_box(row: dict) -> _Box:
    c, s = row["centre_m"], row["size_m"]
    axes = [_AXES.index(a) for a in row["size_axes"]]
    lo, hi = list(c), list(c)
    for k, i in enumerate(axes):
        lo[i], hi[i] = c[i] - s[k] / 2, c[i] + s[k] / 2
    return _Box(lo, hi)


def _names(parts: list[_Part]) -> list[str]:
    """The region name each part would carry (the fused build's namer, in its order: the air
    first, then the parts). Parts the mesh leaves empty are named again by the mesher."""
    namer = _unique_namer()
    namer(FLUID_NAME)
    per_heatsink: dict[str, int] = {}
    for p in parts:
        if p.kind == "heatsink" and not p.dropped:
            hs = str(p.objects[0].props["heatsink"])
            per_heatsink[hs] = per_heatsink.get(hs, 0) + 1
    out = []
    for p in parts:
        if p.kind == "heatsink":
            hs = str(p.objects[0].props["heatsink"])
            raw = hs if per_heatsink.get(hs, 0) <= 1 else f"{hs}_{p.material or 'part'}"
        else:
            raw = p.objects[0].name
        out.append(namer(raw) if not p.dropped else raw)
    return out


def part_triangles(p: _Part, *, segments: int = 32):
    """The part's skin as triangles (metres): each material box's six faces (two triangles
    each), a cylinder as a prism of `segments` sides, the vents' outlines left as they are drawn
    by the boxes (the picture shows the parts, the mesh shows the holes). numpy (n, 3, 3)."""
    import numpy as np

    if p.kind == "solidCylinder":
        axis = axis_of_plane(p.objects[0].plane)
        return _cylinder_tris(p.box, axis, segments)
    tris = [_box_tris(b) for b in _material(p)]
    return np.concatenate(tris) if tris else np.zeros((0, 3, 3))


def _box_tris(b: _Box):
    import numpy as np

    x0, y0, z0 = b.lo
    x1, y1, z1 = b.hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
    # each face's corners counter-clockwise seen from outside (outward normals)
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    tris = []
    for q0, q1, q2, q3 in quads:
        tris += [[v[q0], v[q1], v[q2]], [v[q0], v[q2], v[q3]]]
    return np.array(tris)


def _cylinder_tris(box: _Box, axis: int, segments: int):
    import numpy as np

    cross = [i for i in range(3) if i != axis]
    ra = (box.hi[cross[0]] - box.lo[cross[0]]) / 2
    rb = (box.hi[cross[1]] - box.lo[cross[1]]) / 2
    c0 = (box.hi[cross[0]] + box.lo[cross[0]]) / 2
    c1 = (box.hi[cross[1]] + box.lo[cross[1]]) / 2
    t = np.linspace(0.0, 2 * math.pi, segments + 1)[:-1]
    ring = np.zeros((segments, 3))
    ring[:, cross[0]] = c0 + ra * np.cos(t)
    ring[:, cross[1]] = c1 + rb * np.sin(t)
    bot, top = ring.copy(), ring.copy()
    bot[:, axis], top[:, axis] = box.lo[axis], box.hi[axis]
    cb, ct = np.zeros(3), np.zeros(3)
    cb[cross[0]] = ct[cross[0]] = c0
    cb[cross[1]] = ct[cross[1]] = c1
    cb[axis], ct[axis] = box.lo[axis], box.hi[axis]
    out = []
    for s in range(segments):
        n = (s + 1) % segments
        out += [[cb, bot[n], bot[s]], [ct, top[s], top[n]],
                [bot[s], bot[n], top[n]], [bot[s], top[n], top[s]]]
    return np.array(out)


#: Muted part colours for the stage (RGB 0..1), cycled in file order.
PART_COLOURS: tuple[tuple[float, float, float], ...] = (
    (0.55, 0.66, 0.78), (0.80, 0.63, 0.46), (0.57, 0.72, 0.58), (0.78, 0.57, 0.59),
    (0.67, 0.62, 0.78), (0.82, 0.75, 0.49), (0.50, 0.70, 0.72), (0.72, 0.61, 0.51),
    (0.63, 0.69, 0.50), (0.61, 0.63, 0.68), (0.76, 0.66, 0.73), (0.53, 0.62, 0.68))


def placed_skin(placement: Placement) -> dict:
    """The placed parts as the geometry stage draws them: one block per part under its own name
    and colour, its sharp edges beside it, built from the file's boxes and cylinders - no fused
    STEP needed. Metres."""
    import base64

    import numpy as np

    from meshpipeline.render.skin_mesh import prepare_skin, skin_patch

    patches = []
    edge_pts: list = []
    edge_lines: list = []
    offset = 0
    for k, (p, name) in enumerate(zip(placement.parts, placement.names)):
        if p.dropped:
            continue
        tris = part_triangles(p)
        if not len(tris):
            continue
        prepared = prepare_skin(tris)
        patch = skin_patch(prepared, name=name)
        patch.update(kind=p.kind, material=p.material, power_W=p.power,
                     color=list(PART_COLOURS[k % len(PART_COLOURS)]))
        patches.append(patch)
        lines = prepared["edge_lines"].reshape(-1, 3).astype(np.int64)
        lines[:, 1:] += offset
        edge_pts.append(prepared["edge_points"])
        edge_lines.append(lines)
        offset += len(prepared["edge_points"])
    out = {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
           "parts": True, "patches": patches}
    if edge_pts:
        b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")  # noqa: E731
        lines_all = np.concatenate(edge_lines).astype(np.uint32).ravel()
        out["edges"] = {"points_b64": b64(np.concatenate(edge_pts).astype(np.float32)),
                        "lines_b64": b64(lines_all), "count": int(len(lines_all) // 3)}
    return out


def placed_stl(placement: Placement, dest, *, scale: float = 1.0) -> None:
    """The placed parts as one ASCII STL with a named solid per part (metres x `scale`): what the
    geometry check measures and pictures in place of a fused STEP."""
    from pathlib import Path

    with Path(dest).open("w") as fh:
        for p, name in zip(placement.parts, placement.names):
            if p.dropped:
                continue
            tris = part_triangles(p) * scale
            fh.write(f"solid {name}\n")
            for t in tris:
                n = _normal(t)
                fh.write(f" facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n  outer loop\n")
                for v in t:
                    fh.write(f"   vertex {v[0]:.9e} {v[1]:.9e} {v[2]:.9e}\n")
                fh.write("  endloop\n endfacet\n")
            fh.write(f"endsolid {name}\n")


def _normal(t):
    import numpy as np

    n = np.cross(t[1] - t[0], t[2] - t[0])
    length = float(np.linalg.norm(n))
    return n / length if length > 0 else n


def placement_sidecar(placement: Placement, *, regions: list[dict] | None = None,
                      patches: list[dict] | None = None, build_report: list[str] | None = None,
                      solid_contacts=None, mesher: str = "", geometry_unit: str = "",
                      extra: dict | None = None) -> dict:
    """The physics in the fused build's sidecar shape (cad/ingest/ecxml_build: what reads one
    reads the other): materials, regions, patches, fans, vents, sources, compact models, monitor
    points, the power balance, the tolerances and the build report. Without `regions` the placed
    parts are the regions (the air first), as the geometry check reads them before any mesh."""
    from meshpipeline.cad.ingest.ecxml import SPEC
    from meshpipeline.cad.ingest.ecxml_build import SIDECAR_VERSION, _domain_patches

    model, records, tol = placement.model, placement.records, placement.tol
    objs = model.active_objects
    if regions is None:
        regions = [{"name": FLUID_NAME, "type": "fluid", "kind": "air",
                    "notes": ["the solution domain less every part"]}]
        for p, name in zip(placement.parts, placement.names):
            if p.dropped:
                continue
            regions.append({
                "name": name, "type": "solid", "kind": p.kind, "material": p.material,
                "power_W": p.power, "box_m": p.box.as_dict(),
                "objects": [{"name": o.name, "path": list(o.path), "kind": o.kind,
                             "power_W": o.power, "material": o.material or None}
                            for o in p.objects],
                "notes": list(p.notes)})
    if patches is None:
        patches = _domain_patches(placement.domain, records["patches"]) + records["patches"]
    out = {
        "format": "ECXML", "spec": SPEC, "sidecar_version": SIDECAR_VERSION,
        "model_name": model.name, "producer": model.producer,
        "units": {"length": "m", "temperature": "K", "pressure": "Pa", "power": "W",
                  "thermal_conductivity": "W/mK", "thermal_resistance": "K/W",
                  "specific_heat": "J/kgK", "density": "kg/m3", "flow_rate": "m3/s"},
        "geometry_unit": geometry_unit or "mm (the placed parts carry the file's metres x 1000)",
        "domain": {"box_m": placement.domain.as_dict(), "source": placement.domain_source,
                   "ambient": model.domain.ambient if model.domain else None},
        "materials": {n: m.as_dict() for n, m in model.materials.items()},
        "regions": regions,
        "patches": patches,
        "fans": records["fans"], "grilles": records["grilles"],
        "flow_resistances": records["flow_resistances"],
        "volume_heat_sources": records["volume_heat_sources"],
        "surface_heat_sources": records["surface_heat_sources"],
        "baffles": records["baffles"], "compact_models": records["compact_models"],
        "thin_layers": records["thin_layers"],
        "thin_layers_meshed": records["thin_layers_meshed"],
        "monitor_points": records["monitor_points"],
        "inactive_objects": records["inactive"], "not_built": records["not_built"],
        "ignored_elements": model.ignored_elements,
        "total_power_W": sum(float(o.power or 0.0) for o in objs
                             if o.kind not in ("assembly", "heatsink")),
        "power_W": {
            "in_solid_regions": sum(float(r.get("power_W") or 0.0) for r in regions
                                    if r.get("type") == "solid"),
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
        "tolerances_m": {"file_precision": tol.noise, "boolean_fuzzy": None,
                         "smallest_gap": tol.gap if math.isfinite(tol.gap) else None,
                         "smallest_gap_between": tol.gap_where,
                         "thinnest_part": tol.thick if math.isfinite(tol.thick) else None,
                         "thinnest_part_is": tol.thick_where,
                         "coordinates_snapped": tol.snapped, "largest_snap": tol.snap_shift},
        "build_report": list(build_report if build_report is not None
                             else describe(placement) + placement.report),
        "solid_contacts": sorted(sorted(c) for c in (solid_contacts or ())),
        "notes": placement.notes,
        "placed_parts": True,
    }
    if mesher:
        out["mesher"] = mesher
    out.update(extra or {})
    return out


def write_placed(src, dest) -> tuple[Placement, dict]:
    """The canonical form of an ECXML model on the placed-parts path: the parts as one STL of
    named solids in millimetres (the unit ECXML geometry is read in), the physics sidecar beside
    it (cad/ingest/ecxml_build.SIDECAR_SUFFIX). No boolean, so it never fails on a model the
    reader accepts. Returns the placement and the sidecar."""
    import json
    from pathlib import Path

    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import SIDECAR_SUFFIX, UNIT

    placement = place(read_ecxml(src))
    placed_stl(placement, dest, scale=UNIT)
    sidecar = placement_sidecar(placement)
    Path(str(dest) + SIDECAR_SUFFIX).write_text(json.dumps(sidecar, indent=1, default=float))
    return placement, sidecar


def describe(placement: Placement) -> list[str]:
    """Plain lines for the report head: what was placed and what was kept aside."""
    live = [p for p in placement.parts if not p.dropped]
    tol = placement.tol
    lines = [f"Placed {len(live)} solid part(s) from {placement.model.producer!r} at the "
             "coordinates the file gives; nothing is joined."]
    if tol.snapped and tol.snap_shift > 1e-12:
        lines.append(f"{tol.snapped} coordinate(s) within the file's precision "
                     f"({_um(tol.noise)}) of another were read as the same plane; the largest "
                     f"move was {_um(tol.snap_shift)}.")
    gap = (_um(tol.gap) + f" ({tol.gap_where})") if math.isfinite(tol.gap) else "none"
    thick = (_um(tol.thick) + f" ({tol.thick_where})") if math.isfinite(tol.thick) else "none"
    lines.append(f"Smallest gap between parts: {gap}; thinnest part: {thick}.")
    return lines


__all__ = ["MAX_PLACED_PARTS", "Placement", "describe", "part_triangles", "place",
           "placed_skin", "placed_stl"]
