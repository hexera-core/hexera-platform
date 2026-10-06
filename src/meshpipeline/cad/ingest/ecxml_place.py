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
    _noise,
    _pair_scan,
    _Part,
    _plate,
    _snap_function,
    _Tol,
    _um,
    _unique_namer,
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

    @property
    def size(self) -> float:
        return math.dist(self.domain.lo, self.domain.hi)


def _records() -> dict[str, list]:
    return {k: [] for k in (
        "patches", "fans", "grilles", "flow_resistances", "volume_heat_sources",
        "surface_heat_sources", "baffles", "monitor_points", "compact_models", "not_built",
        "inactive")}


def place(model: EcxmlModel) -> Placement:
    """Every active object of the model at its place: solid parts with their material boxes, the
    physics the mesh does not carry in records, and the order overlapping parts are painted in.
    Raises EcxmlError when there is nothing to place."""
    objs = model.active_objects
    notes: list[str] = list(model.notes)
    report: list[str] = []
    noise = _noise(model, objs)
    snapped, moved = _snap_function(model, objs, noise)
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
    if len(parts) > MAX_PLACED_PARTS:
        raise EcxmlError(f"the model holds {len(parts):,} solid parts, more than the "
                         f"{MAX_PLACED_PARTS:,} regions a conjugate case can carry; export a "
                         "sub-assembly, or merge detailed components in the authoring tool")

    _open_walls(parts, devices, notes, noise)
    for p in parts:
        if not p.box.overlaps(domain, 0.0):
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
    names = _names(parts)
    return Placement(model=model, domain=domain, domain_source=domain_source, parts=parts,
                     paint=paint, records=records, fate=fate, notes=notes, report=report,
                     tol=tol, planes=planes, names=names, order_cycles=cycles)


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


def placed_skin(placement: Placement) -> dict:
    """The placed parts as the geometry stage draws them: one block per part (its own name, so its
    own colour), built from the file's boxes and cylinders - no fused STEP needed. Metres."""
    from meshpipeline.render.skin_mesh import prepare_skin, skin_patch

    patches = []
    for p, name in zip(placement.parts, placement.names):
        if p.dropped:
            continue
        tris = part_triangles(p)
        if not len(tris):
            continue
        patch = skin_patch(prepare_skin(tris), name=name)
        patch["kind"] = p.kind
        patch["material"] = p.material
        patch["power_W"] = p.power
        patches.append(patch)
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
            "parts": True, "patches": patches}


def placed_stl(placement: Placement, dest) -> None:
    """The placed parts as one ASCII STL with a named solid per part (metres): what the geometry
    check measures and pictures in place of a fused STEP."""
    from pathlib import Path

    with Path(dest).open("w") as fh:
        for p, name in zip(placement.parts, placement.names):
            if p.dropped:
                continue
            tris = part_triangles(p)
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
