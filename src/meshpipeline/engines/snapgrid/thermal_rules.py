# Responsibility: The electronics-cooling resolution rules of the snap grid: what a placed model needs resolved (heated parts, air gaps between solids, heat sources, fans and vents), the size sources each detail level asks for there, and the measurement of what a laid-out grid actually gives each of them.
# Owns: the thermal facts read from a placement (heated parts, gaps, openings, boundary-layer estimates), the rule sources, the plain-words names of the rules, and the resolution audit.
# Boundaries: pure numpy on a Placement and a blocks.Layout; it never decides the budget (blocks.build_layout does) and never names a part to treat it specially - every rule comes from geometry and the file's physics (power, fans, vents).
# Collaborates with: engines/snapgrid/blocks.py (adds the sources; lays out the grid), engines/snapgrid/grid.py (GridPlan), engines/snapgrid/mesher.py (reports the audit), engines/snapgrid/criteria.py (judges it).
"""What a conjugate heat-transfer mesh must resolve, and how well a grid does.

The rules follow the published practice of the electronics-cooling tools (Simcenter Flotherm,
Ansys Icepak, 6SigmaET) and the boundary-layer physics behind it:

* SOLIDS: cells through every part's thickness, more through the parts heat flows through first
  (the heat sources, what touches them, heat sinks): the temperature drop across them is the
  answer.
* GAPS: cells across every air gap where two solids face each other (fin channels, a chip under
  a lid, a board over its enclosure): the velocity and temperature profile across a channel sets
  its heat transfer and pressure drop. Flotherm's guidance is at least 3 cells across a fin gap.
* HEATED SURFACES: graded cells in the air next to every heated surface, the first one small
  enough that `band_cells` cells fit inside the thermal boundary layer. The layer's thickness is
  estimated from the surface's size and the flow the file makes (natural convection from the
  power, or the fans' flow): delta = 5 L / sqrt(Re_L) / Pr^(1/3) (laminar flat plate), with the
  velocity the larger of the buoyant sqrt(g beta dT L) and the fans' flow over the section.
* HEAT SOURCES: cells across each heat-producing part's width, so its hot spot and the heat
  spreading out of it are resolved.
* FANS, VENTS, GRILLES: cells across each opening (a fan's diameter, a vent's shorter side), on
  its face and a short way either side of it.
* GRADING: neighbouring cells differ by at most `growth`; in the heated band a cell is at most
  `air_aspect` times longer along the surface than across it.

All of it is a SIZE SOURCE in the grid's size field (a box and the cell size it asks for on each
axis): the field grows by `growth` per cell away from each source, so every rule is local.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad.ingest.ecxml_build import _Box, _material
from meshpipeline.cad.ingest.ecxml_place import Placement

# ------------------------------------------------------------------------------ physics -------
#: Air near room temperature: kinematic viscosity (m2/s) and Prandtl number.
AIR_NU = 1.6e-5
AIR_PR = 0.71
#: Gravity (m/s2) and the temperature rise of a heated surface over the air the estimate assumes
#: (the boundary layer's thickness goes as dT^-1/4, so a factor two in dT is 16% in thickness).
GRAVITY = 9.81
DESIGN_RISE_K = 30.0
#: A fan's operating point read from its curve: this share of its free-delivery flow (a fan in a
#: box runs at about half its free delivery; the estimate only sets a boundary-layer thickness).
FAN_CURVE_SHARE = 0.5
#: The boundary layer is never taken thicker than this many times the surface's length (a part
#: smaller than its own boundary layer is surrounded by a diffusive zone of about its own size).
MAX_LAYER_PER_LENGTH = 2.0
#: A part counts as a thin layer between a heat source and what lies beyond it (a thermal
#: interface, a die attach, a lid) when it is at most this share of the source's thickness.
THIN_LINK = 0.5
#: Heat sinks (the file's own heatsink objects) within this many contacts of a heat source are
#: heated, whatever lies between.
HEATSINK_HOPS = 3
#: Air thinner than this between two solids is no flow passage (a film the exporter's round-off
#: leaves; the build report warns of it): it keeps the one cell its planes give, no more.
MIN_GAP_M = 1e-6
#: Air between two faces is a passage the flow uses only when it is at least this share of the
#: boundary layer that would grow over those faces (a fin channel, a board over its enclosure:
#: millimetres against a layer of a few millimetres). A thinner film - air under a lid, a bond
#: line, an exporter's micrometre offset - carries its heat across by conduction, a straight-line
#: profile one cell resolves: it keeps the cells its planes give it, no more.
MIN_GAP_LAYER_SHARE = 0.05
#: Items of each rule measured at five points over their face (finest first); every further one
#: is measured at its centre. Every item is measured and every shortfall kept.
MAX_AUDIT_ROWS = 4000


@dataclass
class Facts:
    """What a placement says that the rules read: computed once per placement."""
    boxes_lo: np.ndarray                    # (B, 3) every live part's material boxes in the domain
    boxes_hi: np.ndarray
    box_part: np.ndarray                    # (B,) the part each box belongs to
    powered: dict[int, str] = field(default_factory=dict)      # part -> why it makes heat
    heated: dict[int, str] = field(default_factory=dict)       # part -> why it is heated
    layer_m: dict[int, float] = field(default_factory=dict)    # heated part -> boundary layer (m)
    length_m: dict[int, float] = field(default_factory=dict)   # heated part -> its length (m)
    velocity_m_s: float = 0.0               # the flow speed the estimate used
    velocity_why: str = ""
    gaps: list[dict] = field(default_factory=list)     # axis, lo, hi (boxes), width, parts
    faces: list[dict] = field(default_factory=list)    # heated faces: part, axis, side, box
    heat_boxes: list[dict] = field(default_factory=list)    # heat-producing boxes: box, part/name
    openings: list[dict] = field(default_factory=list)      # fans, vents: box, axis, across, name
    heated_rects: list[dict] = field(default_factory=list)  # heat-flux rectangles: box, axis


def facts(placement: Placement) -> Facts:
    """The thermal facts of a placement, read once and kept on it."""
    got = getattr(placement, "_snapgrid_thermal_facts", None)
    if isinstance(got, Facts):
        return got
    out = _read_facts(placement)
    placement._snapgrid_thermal_facts = out  # type: ignore[attr-defined]
    return out


def _live(placement: Placement) -> list[int]:
    return [k for k, p in enumerate(placement.parts) if not p.dropped]


def _boxes(placement: Placement) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dom = placement.domain
    lo, hi, owner = [], [], []
    for k in _live(placement):
        for b in _material(placement.parts[k]):
            c = b.common(dom)
            if c is not None:
                lo.append(c.lo)
                hi.append(c.hi)
                owner.append(k)
    return (np.array(lo, dtype=float).reshape(-1, 3), np.array(hi, dtype=float).reshape(-1, 3),
            np.array(owner, dtype=np.int64))


def _read_facts(placement: Placement) -> Facts:
    lo, hi, owner = _boxes(placement)
    f = Facts(boxes_lo=lo, boxes_hi=hi, box_part=owner)
    noise = max(placement.tol.noise, 1e-12 * placement.size)
    contacts, gaps = _neighbours(lo, hi, owner, placement, noise)
    f.velocity_m_s, f.velocity_why = _velocity(placement)
    f.gaps = [g for g in gaps if _is_passage(g, f.velocity_m_s, placement)]
    f.powered = _powered(placement, lo, hi, owner)
    f.heated = _heated(placement, f.powered, contacts, lo, hi, owner)
    for k in f.heated:
        L = _length(placement.parts[k].box.common(placement.domain) or placement.parts[k].box)
        f.length_m[k] = L
        f.layer_m[k] = boundary_layer(L, f.velocity_m_s, placement)
    f.faces = _heated_faces(placement, f, noise)
    f.heat_boxes = _heat_boxes(placement, f.powered)
    f.openings = _openings(placement)
    f.heated_rects = _heated_rects(placement)
    return f


def _length(box: _Box) -> float:
    """A part's length for its boundary layer: the shorter side of its largest face."""
    ext = sorted(box.hi[i] - box.lo[i] for i in range(3))
    return max(ext[1], 1e-9)


def _ambient_k(placement: Placement) -> float:
    try:
        t = float((placement.model.ambient or {}).get("temperature_K") or 0.0)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        t = 0.0
    return t if t > 150.0 else 300.0


def buoyant_velocity(length_m: float, placement: Placement | None = None) -> float:
    """The natural-convection velocity scale of a heated surface: sqrt(g beta dT L)."""
    beta = 1.0 / (_ambient_k(placement) if placement is not None else 300.0)
    return math.sqrt(GRAVITY * beta * DESIGN_RISE_K * max(length_m, 1e-9))


def boundary_layer(length_m: float, velocity_m_s: float, placement: Placement | None = None
                   ) -> float:
    """The thermal boundary layer's thickness on a surface of `length_m` in air moving at
    `velocity_m_s` (or, if faster, the surface's own buoyant flow): 5 L / sqrt(Re_L) / Pr^(1/3),
    never more than MAX_LAYER_PER_LENGTH times L."""
    L = max(length_m, 1e-9)
    u = max(velocity_m_s, buoyant_velocity(L, placement))
    re = max(u * L / AIR_NU, 1e-9)
    delta = 5.0 * L / math.sqrt(re) / AIR_PR ** (1.0 / 3.0)
    return min(delta, MAX_LAYER_PER_LENGTH * L)


def _velocity(placement: Placement) -> tuple[float, str]:
    """The fans' flow over the section it crosses (the smallest enclosure around a fan, else the
    solution domain), the largest over the fans; 0 when the file has no fan with a flow."""
    best, why = 0.0, ""
    dom = placement.domain
    rows = list(placement.records.get("fans") or []) + [
        r for r in placement.records.get("patches") or [] if r.get("role") == "fan"]
    walls = [placement.parts[k].box for k in _live(placement) if placement.parts[k].wall
             and placement.parts[k].kind == "enclosure"]
    for r in rows:
        q = _fan_flow(r.get("flow") or {})
        if q <= 0:
            continue
        axis = _fan_axis(r)
        if axis is None:
            continue
        centre = r.get("centre_m") or [(a + b) / 2 for a, b in zip(r["box_m"]["min"], r["box_m"]["max"])] \
            if r.get("box_m") else r.get("centre_m")
        if centre is None:
            continue
        around = [w for w in walls if all(w.lo[i] - 1e-9 <= centre[i] <= w.hi[i] + 1e-9
                                         for i in range(3))]
        box = min(around, key=lambda w: w.volume()) if around else dom
        cross = [i for i in range(3) if i != axis]
        area = (box.hi[cross[0]] - box.lo[cross[0]]) * (box.hi[cross[1]] - box.lo[cross[1]])
        if area <= 0:
            continue
        u = q / area
        if u > best:
            best = u
            why = (f"{r.get('name')}: {q * 1e3:.3g} L/s over {area * 1e4:.3g} cm2 "
                   f"({'its enclosure' if around else 'the solution domain'})")
    return best, why


def _fan_flow(flow: dict) -> float:
    kind = flow.get("kind")
    if kind == "fixed_flow_rate":
        return abs(float(flow.get("m3_s") or 0.0))
    if kind == "fan_curve":
        pts = flow.get("points_m3_s_Pa") or []
        qmax = max((abs(float(p[0])) for p in pts if p), default=0.0)
        return FAN_CURVE_SHARE * qmax
    return 0.0


def _fan_axis(r: dict) -> int | None:
    d = str(r.get("flow_direction") or "")
    if len(d) == 2 and d[1] in "xyz":
        return "xyz".index(d[1])
    plane = str(r.get("axis") or r.get("plane") or "")
    if len(plane) == 3:
        from meshpipeline.cad.ingest.ecxml import axis_of_plane
        try:
            return axis_of_plane(plane)
        except KeyError:
            return None
    normal = r.get("normal")
    if normal:
        return int(np.argmax(np.abs(np.asarray(normal, dtype=float))))
    return None


# ------------------------------------------------------------------------------ neighbours ----
def _neighbours(lo: np.ndarray, hi: np.ndarray, owner: np.ndarray, placement: Placement,
                noise: float) -> tuple[set[tuple[int, int]], list[dict]]:
    """Parts that touch (share a face over an area), and the air gaps where two material boxes
    face each other: for each box face, the nearest box beyond it that faces it over a width at
    least the gap (a channel, not a corner), or the domain's side when nothing is nearer."""
    n = len(lo)
    contacts: set[tuple[int, int]] = set()
    best: dict[tuple[int, int, int], tuple[float, int]] = {}    # (box, axis, side) -> (gap, other)
    for start in range(0, n, 256):
        rows = np.arange(start, min(n, start + 256))
        ext = np.minimum(hi[rows][:, None, :], hi[None]) - np.maximum(lo[rows][:, None, :], lo[None])
        over = ext > noise
        both = over.sum(axis=2) == 2
        touch = both & (np.abs(ext) <= noise).any(axis=2) & (np.arange(n)[None, :] != rows[:, None])
        for r, c in zip(*np.nonzero(touch)):
            a, b = int(owner[rows[r]]), int(owner[c])
            if a != b:
                contacts.add((min(a, b), max(a, b)))
        facing = both & (ext < -noise).any(axis=2)
        for r, c in zip(*np.nonzero(facing)):
            i, j = int(rows[r]), int(c)
            a = int(np.argmin(ext[r, c]))
            g = float(-ext[r, c, a])
            lat = [x for x in range(3) if x != a]
            if min(ext[r, c, lat[0]], ext[r, c, lat[1]]) < g:
                continue                       # they face over less than the gap: not a channel
            side = 1 if lo[j, a] >= hi[i, a] else 0
            key = (i, a, side)
            if g < best.get(key, (math.inf, -1))[0]:
                best[key] = (g, j)
    dom = placement.domain
    gaps: list[dict] = []
    seen: set[tuple[int, int, int]] = set()
    for i in range(n):
        for a in range(3):
            lat = [x for x in range(3) if x != a]
            for side in (0, 1):
                g, j = best.get((i, a, side), (math.inf, -1))
                wall = (dom.hi[a] - hi[i, a]) if side else (lo[i, a] - dom.lo[a])
                if j < 0 and noise < wall and all(
                        hi[i, x] - lo[i, x] >= wall for x in lat):
                    # nothing nearer: the gap between the face and the domain's side
                    s_lo, s_hi = lo[i].copy(), hi[i].copy()
                    if side:
                        s_lo[a], s_hi[a] = hi[i, a], dom.hi[a]
                    else:
                        s_lo[a], s_hi[a] = dom.lo[a], lo[i, a]
                    gaps.append({"axis": a, "lo": s_lo, "hi": s_hi, "width": float(wall),
                                 "parts": (int(owner[i]), -1), "boxes": (i, -1)})
                    continue
                if j < 0:
                    continue
                key = (min(i, j), max(i, j), a)
                if key in seen:
                    continue
                seen.add(key)
                s_lo = np.maximum(lo[i], lo[j])
                s_hi = np.minimum(hi[i], hi[j])
                s_lo[a] = min(hi[i, a], hi[j, a])
                s_hi[a] = max(lo[i, a], lo[j, a])
                gaps.append({"axis": a, "lo": s_lo, "hi": s_hi, "width": g,
                             "parts": (int(owner[i]), int(owner[j])), "boxes": (i, j)})
    gaps = [g for g in gaps if _has_air(g["lo"], g["hi"], lo, hi, g["boxes"])]
    gaps.sort(key=lambda r: r["width"])
    return contacts, gaps


def _is_passage(g: dict, velocity: float, placement: Placement) -> bool:
    """Whether the air between two faces is a flow passage: no mere film (MIN_GAP_M), and at
    least MIN_GAP_LAYER_SHARE of the boundary layer over the faces it separates (their shorter
    side as the length, the file's flow as the speed)."""
    a, w = g["axis"], g["width"]
    if w < MIN_GAP_M:
        return False
    lat = [x for x in range(3) if x != a]
    length = max(min(float(g["hi"][x] - g["lo"][x]) for x in lat), w)
    return w >= MIN_GAP_LAYER_SHARE * boundary_layer(length, velocity, placement)


def _has_air(s_lo: np.ndarray, s_hi: np.ndarray, lo: np.ndarray, hi: np.ndarray,
             own: tuple[int, int]) -> bool:
    """Some of the slab between two faces is air: the other boxes in it do not fill it (a part
    sitting between the two faces, over all of them, is no gap)."""
    from meshpipeline.cad.ingest.ecxml_build import _union_volume

    near = ((lo < s_hi) & (hi > s_lo)).all(axis=1)
    for b in own:
        if b >= 0:
            near[b] = False
    idx = np.flatnonzero(near)
    if not len(idx):
        return True
    slab = _Box(s_lo.tolist(), s_hi.tolist())
    if len(idx) > 64:                     # many small parts in it: some air is left between them
        return True
    filled = _union_volume(slab, [_Box(lo[i].tolist(), hi[i].tolist()) for i in idx.tolist()])
    return filled < slab.volume() * (1 - 1e-6)


# ------------------------------------------------------------------------------ heat ----------
def _powered(placement: Placement, lo: np.ndarray, hi: np.ndarray, owner: np.ndarray
             ) -> dict[int, str]:
    """The parts that make heat: their own power, or a volume heat source inside them."""
    out: dict[int, str] = {}
    for k in _live(placement):
        if abs(float(placement.parts[k].power or 0.0)) > 0.0:
            out[k] = "it makes heat"
    for r in placement.records.get("volume_heat_sources") or []:
        if not float(r.get("power_W") or 0.0):
            continue
        b = r.get("box_in_domain_m") or r.get("box_m")
        if not b:
            continue
        s_lo, s_hi = np.asarray(b["min"], dtype=float), np.asarray(b["max"], dtype=float)
        inside = ((np.minimum(hi, s_hi) - np.maximum(lo, s_lo)) > 0).all(axis=1)
        for k in np.unique(owner[inside]).tolist():
            out.setdefault(int(k), "a heat source lies in it")
    return out


def _heated(placement: Placement, powered: dict[int, str], contacts: set[tuple[int, int]],
            lo: np.ndarray, hi: np.ndarray, owner: np.ndarray) -> dict[int, str]:
    """The parts whose surfaces carry the heat out: the heat sources; every part touching one;
    beyond a thin layer touching a source (a thermal interface, a die attach), what lies on its
    far side; and any heat sink within a few contacts of a source."""
    nbr: dict[int, set[int]] = {}
    for a, b in contacts:
        nbr.setdefault(a, set()).add(b)
        nbr.setdefault(b, set()).add(a)
    parts = placement.parts

    def thick(k: int) -> float:
        b = parts[k].box
        return min(b.hi[i] - b.lo[i] for i in range(3))

    out: dict[int, str] = dict(powered)
    for s in powered:
        for k in nbr.get(s, ()):
            out.setdefault(k, "it touches a heat source")
            if thick(k) <= THIN_LINK * thick(s):
                for j in nbr.get(k, ()):
                    if j != s:
                        out.setdefault(j, "a thin layer joins it to a heat source")
        # heat sinks a few contacts away
        frontier, seen = {s}, {s}
        for _hop in range(HEATSINK_HOPS):
            frontier = {j for k in frontier for j in nbr.get(k, ()) if j not in seen}
            seen |= frontier
            for j in frontier:
                if parts[j].kind == "heatsink":
                    out.setdefault(j, "a heat sink on a heat source")
    return out


def _heated_faces(placement: Placement, f: Facts, noise: float) -> list[dict]:
    """Every face of a heated part's material boxes that air can reach: not on the domain's
    side, and not covered whole by another box it touches."""
    dom = placement.domain
    lo, hi, owner = f.boxes_lo, f.boxes_hi, f.box_part
    out: list[dict] = []
    for bi in np.flatnonzero(np.isin(owner, list(f.heated))).tolist():
        k = int(owner[bi])
        for a in range(3):
            lat = [x for x in range(3) if x != a]
            for side in (0, 1):
                plane = hi[bi, a] if side else lo[bi, a]
                if abs(plane - (dom.hi[a] if side else dom.lo[a])) <= noise:
                    continue
                # another box over the whole face, on its far side
                beyond = (lo[:, a] <= plane + noise) & (lo[:, a] >= plane - noise) if side else \
                    (hi[:, a] <= plane + noise) & (hi[:, a] >= plane - noise)
                covers = beyond & (lo[:, lat[0]] <= lo[bi, lat[0]] + noise) & \
                    (hi[:, lat[0]] >= hi[bi, lat[0]] - noise) & \
                    (lo[:, lat[1]] <= lo[bi, lat[1]] + noise) & \
                    (hi[:, lat[1]] >= hi[bi, lat[1]] - noise)
                covers[bi] = False
                if covers.any():
                    continue
                f_lo, f_hi = lo[bi].copy(), hi[bi].copy()
                f_lo[a] = f_hi[a] = plane
                out.append({"part": k, "box": bi, "axis": a, "side": side, "lo": f_lo,
                            "hi": f_hi})
    return out


def _heat_boxes(placement: Placement, powered: dict[int, str]) -> list[dict]:
    """The boxes heat is made in: each powered part's own box, and each volume heat source."""
    dom = placement.domain
    out: list[dict] = []
    for k in powered:
        p = placement.parts[k]
        if not float(p.power or 0.0):
            continue
        c = p.box.common(dom)
        if c is not None:
            out.append({"part": k, "name": placement.names[k] if placement.names else p.name,
                        "lo": np.array(c.lo), "hi": np.array(c.hi)})
    for r in placement.records.get("volume_heat_sources") or []:
        if not float(r.get("power_W") or 0.0):
            continue
        b = r.get("box_in_domain_m") or r.get("box_m")
        c = _Box(b["min"], b["max"]).common(dom) if b else None
        if c is not None:
            out.append({"part": -1, "name": str(r.get("name")), "lo": np.array(c.lo),
                        "hi": np.array(c.hi)})
    return out


def _openings(placement: Placement) -> list[dict]:
    """The fans, vents and grilles: the box the flow passes through, its axis and the size to
    resolve across it (a fan's diameter, a vent's shorter side)."""
    from meshpipeline.cad.ingest.ecxml_place import _rect_box

    dom = placement.domain
    out: list[dict] = []
    rec = placement.records
    for r in rec.get("fans") or []:
        if r.get("kind") == "axial_3d" and r.get("box_m"):
            axis = _fan_axis(r)
            if axis is None:
                continue
            box = _Box(r["box_m"]["min"], r["box_m"]["max"]).common(dom)
            if box is None:
                continue
            d = [float(v) for v in r.get("outer_diameter_m") or []]
            across = min(d) if d else min(box.hi[i] - box.lo[i] for i in range(3) if i != axis)
            hub = float(r.get("hub_diameter_m") or 0.0)
            ring = (across - hub) / 2 if 0 < hub < across else across
            out.append({"name": str(r.get("name")), "what": "fan", "axis": axis,
                        "lo": np.array(box.lo), "hi": np.array(box.hi), "across": across,
                        "ring": ring})
    rows = [r for r in rec.get("fans") or [] if r.get("kind") != "axial_3d"] + \
        list(rec.get("grilles") or []) + \
        [r for r in rec.get("patches") or [] if r.get("role") in ("vent", "fan")]
    for r in rows:
        if not r.get("size_m") or not r.get("size_axes"):
            continue
        box = _rect_box(r)
        axis = next((i for i in range(3) if "xyz"[i] not in r["size_axes"]), None)
        if axis is None:
            continue
        clipped = _clip(box, dom)
        if clipped is None:
            continue
        across = min(float(s) for s in r["size_m"])
        if across <= 0:
            continue
        what = "fan" if (r.get("role") == "fan" or "flow" in r) else "vent"
        out.append({"name": str(r.get("name")), "what": what, "axis": axis,
                    "lo": np.array(clipped.lo), "hi": np.array(clipped.hi), "across": across,
                    "ring": across})
    return out


def _heated_rects(placement: Placement) -> list[dict]:
    """Rectangles that put heat into the air or a wall (surface heat sources, heat-flux plates)."""
    from meshpipeline.cad.ingest.ecxml_place import _rect_box

    rows = list(placement.records.get("surface_heat_sources") or []) + [
        r for r in placement.records.get("patches") or [] if r.get("role") == "heat_flux"]
    out = []
    for r in rows:
        if not r.get("size_m") or not r.get("size_axes") or not float(r.get("power_W") or 0.0):
            continue
        box = _clip(_rect_box(r), placement.domain)
        if box is None:
            continue
        axis = next((i for i in range(3) if "xyz"[i] not in r["size_axes"]), None)
        if axis is None:
            continue
        L = sorted(float(s) for s in r["size_m"])[0]
        out.append({"name": str(r.get("name")), "axis": axis, "lo": np.array(box.lo),
                    "hi": np.array(box.hi), "length": L})
    return out


def _clip(b: _Box, dom: _Box) -> _Box | None:
    lo = [max(b.lo[i], dom.lo[i]) for i in range(3)]
    hi = [min(b.hi[i], dom.hi[i]) for i in range(3)]
    if any(hi[i] < lo[i] for i in range(3)):
        return None
    return _Box(lo, hi)


# ------------------------------------------------------------------------------ the sources ---
def graded_reach(cells: int, growth: float) -> float:
    """How many first-cell sizes `cells` cells growing by `growth` span: (g^n - 1) / (g - 1)."""
    if cells <= 0:
        return 0.0
    if growth <= 1.0 + 1e-9:
        return float(cells)
    return (growth ** cells - 1.0) / (growth - 1.0)


def band_first_cell(layer_m: float, plan) -> float:
    """The first air cell next to a heated surface: `band_cells` cells growing by `growth` fill
    the boundary layer."""
    return layer_m / max(graded_reach(plan.band_cells, plan.growth), 1e-12)


def part_cells(placement: Placement, plan, k: int) -> int:
    """Cells a plan asks through part k's thickness: more through a heated part."""
    f = facts(placement)
    hot = plan.hot_cells_across if plan.hot_cells_across else plan.min_cells_across
    return max(plan.min_cells_across, hot) if k in f.heated else plan.min_cells_across


def sources(placement: Placement, plan, H: float) -> list[tuple[list[float], list[float], tuple]]:
    """The size sources the thermal rules add for this plan: (lo, hi, size per axis), inf where a
    rule asks nothing on an axis. Sources no finer than the background size H are left out."""
    f = facts(placement)
    m = math.log(plan.growth)
    inf = math.inf
    out: list[tuple[list[float], list[float], tuple]] = []
    asp = plan.air_aspect if plan.air_aspect and plan.air_aspect > 0 else inf

    def add(lo, hi, size) -> None:
        if min(size) < H:
            out.append(([float(v) for v in lo], [float(v) for v in hi],
                        tuple(float(s) for s in size)))

    # gaps between solids: cells across, and along the surfaces at most `air_aspect` times that
    if plan.gap_cells > 0:
        lo_b, hi_b, own = f.boxes_lo, f.boxes_hi, f.box_part
        for g in f.gaps:
            a, w = g["axis"], g["width"]
            if w < MIN_GAP_M:
                continue                       # an exporter's round-off film, reported elsewhere
            s = w / plan.gap_cells
            # the cells the two faces' own sizes already grade across it (nothing to add then)
            own_s = []
            for bi in g["boxes"]:
                if bi < 0:
                    continue
                t = hi_b[bi, a] - lo_b[bi, a]
                own_s.append(t / max(1, part_cells(placement, plan, int(own[bi]))))
            if own_s and m > 0:
                s0 = max(own_s)
                n = 2.0 * math.log(1.0 + m * (w / 2.0) / max(s0, 1e-300)) / m
                if n >= plan.gap_cells:
                    continue
            # across the gap only: along it, the heated band's sizes and the grading set the cells
            size = [inf, inf, inf]
            size[a] = s
            add(g["lo"], g["hi"], size)
    # the air next to every heated surface: the first cell fits band_cells into the layer
    if plan.band_cells > 0:
        for face in f.faces:
            s0 = band_first_cell(f.layer_m[face["part"]], plan)
            size = [asp * s0] * 3
            size[face["axis"]] = s0
            add(face["lo"], face["hi"], size)
        for r in f.heated_rects:
            s0 = band_first_cell(boundary_layer(r["length"], f.velocity_m_s, placement), plan)
            size = [asp * s0] * 3
            size[r["axis"]] = s0
            add(r["lo"], r["hi"], size)
    # across each heat-producing box's width (its two longer sides; all three for a cube)
    if plan.spread_cells > 0:
        for hb in f.heat_boxes:
            ext = hb["hi"] - hb["lo"]
            top = float(ext.max())
            size = [e / plan.spread_cells if e >= 0.5 * top else inf for e in ext.tolist()]
            add(hb["lo"], hb["hi"], size)
    # fans, vents and grilles: across the opening, on it and a short way either side
    if plan.opening_cells > 0:
        dom = placement.domain
        for op in f.openings:
            s = min(op["across"] / plan.opening_cells, op["ring"] / 3.0)
            a = op["axis"]
            lo, hi = op["lo"].copy(), op["hi"].copy()
            reach = op["across"] / 2.0
            lo[a] = max(dom.lo[a], lo[a] - reach)
            hi[a] = min(dom.hi[a], hi[a] + reach)
            add(lo, hi, [s, s, s])
    return out


def applicable(placement: Placement) -> set[str]:
    """The plan fields whose rule has something to act on in this model (a model with no fan has
    no opening rule to relax, and none is said to be relaxed)."""
    f = facts(placement)
    out = set()
    if f.gaps:
        out.add("gap_cells")
    if f.faces or f.heated_rects:
        out |= {"band_cells", "air_aspect"}
    if f.heat_boxes:
        out.add("spread_cells")
    if f.openings:
        out.add("opening_cells")
    if f.heated:
        out.add("hot_cells_across")
    return out


# ------------------------------------------------------------------------------ the words -----
#: What each relaxable rule is called, and how it is said when the budget takes cells from it.
RULE_WORDS = {
    "gap_cells": "air gaps between solids (fin channels, gaps between parts) get {got} cell(s) "
                 "across (asked {want})",
    "band_cells": "the air next to heated surfaces gets {got} cell(s) inside its thermal boundary "
                  "layer (asked {want})",
    "spread_cells": "heat-producing parts get {got} cell(s) across their width (asked {want})",
    "opening_cells": "fans, vents and grilles get {got} cell(s) across (asked {want})",
    "hot_cells_across": "heated solids (heat sources, fins, what they sit on) get {got} cell(s) "
                        "through their thickness (asked {want})",
    "air_aspect": "cells next to heated surfaces may be up to {got:g}x longer along the surface "
                  "than across it (asked {want:g}x)",
}


def describe_plan(plan) -> list[str]:
    """The rules a plan asks for, one short phrase each (what the thermal panel lists)."""
    out = [f"at least {plan.min_cells_across} cell{'s' if plan.min_cells_across != 1 else ''} "
           "through every part"]
    if plan.hot_cells_across and plan.hot_cells_across > plan.min_cells_across:
        out.append(f"{plan.hot_cells_across} through heat sources, fins and what they sit on")
    if plan.gap_cells:
        out.append(f"{plan.gap_cells} across every air gap between solids")
    if plan.band_cells:
        out.append(f"{plan.band_cells} inside the warm air layer on every heated surface")
    if plan.spread_cells:
        out.append(f"{plan.spread_cells} across every heat source")
    if plan.opening_cells:
        out.append(f"{plan.opening_cells} across every fan and vent")
    out.append(f"neighbouring cells within {plan.growth:g}x")
    return out


# ------------------------------------------------------------------------------ the audit -----
class _Locator:
    """Which block holds a point: blocks bucketed on a coarse grid over the domain."""

    def __init__(self, layout) -> None:
        self.layout = layout
        blo = np.array([[layout.coords(b, a)[0] for a in range(3)] for b in layout.blocks])
        bhi = np.array([[layout.coords(b, a)[-1] for a in range(3)] for b in layout.blocks])
        self.blo, self.bhi = blo, bhi
        self.lo = blo.min(axis=0)
        self.hi = bhi.max(axis=0)
        self.n = 24
        span = np.maximum(self.hi - self.lo, 1e-300)
        self.step = span / self.n
        self.bins: dict[tuple[int, int, int], list[int]] = {}
        a0 = np.clip(((blo - self.lo) / self.step).astype(np.int64), 0, self.n - 1)
        a1 = np.clip(((bhi - self.lo) / self.step).astype(np.int64), 0, self.n - 1)
        for bi in range(len(blo)):
            for i in range(a0[bi, 0], a1[bi, 0] + 1):
                for j in range(a0[bi, 1], a1[bi, 1] + 1):
                    for k in range(a0[bi, 2], a1[bi, 2] + 1):
                        self.bins.setdefault((i, j, k), []).append(bi)

    def find(self, p: np.ndarray) -> int:
        i, j, k = np.clip(((p - self.lo) / self.step).astype(np.int64), 0, self.n - 1).tolist()
        key = (int(i), int(j), int(k))
        for bi in self.bins.get(key, ()):
            if np.all(p >= self.blo[bi]) and np.all(p < self.bhi[bi]):
                return bi
        for bi in self.bins.get(key, ()):
            if np.all(p >= self.blo[bi]) and np.all(p <= self.bhi[bi]):
                return bi
        return -1

    def cells_along(self, p: np.ndarray, axis: int, start: float, end: float) -> list[float]:
        """The widths of the cells a line on `axis` through p crosses between `start` and `end`
        (the parts of cells cut at the two ends included), hopping from block to block."""
        out: list[float] = []
        lo, hi = min(start, end), max(start, end)
        x = lo
        q = p.copy()
        guard = 0
        while x < hi - 1e-15 * max(1.0, abs(hi)) and guard < 100_000:
            guard += 1
            q[axis] = x + 1e-12 * max(1.0, abs(hi - lo))
            bi = self.find(q)
            if bi < 0:
                break
            lines = self.layout.coords(self.layout.blocks[bi], axis)
            nxt = lines[lines > x + 1e-15 * max(1.0, abs(x))]
            stop = min(hi, float(nxt[0]) if len(nxt) else hi, float(self.bhi[bi, axis]))
            j = int(np.searchsorted(lines, x, side="right")) - 1
            while j < len(lines) - 1 and x < hi - 1e-15 * max(1.0, abs(hi)):
                a0, a1 = float(lines[j]), float(lines[j + 1])
                seg_lo, seg_hi = max(a0, x), min(a1, hi)
                if seg_hi > seg_lo:
                    out.append(seg_hi - seg_lo)
                x = seg_hi
                j += 1
                if x >= self.bhi[bi, axis] - 1e-15 * max(1.0, abs(x)):
                    break
            if stop <= x:
                continue
        return out


def audit(placement: Placement, layout, plan, asked=None) -> dict:
    """Measure what the laid-out grid gives every rule, against what `asked` (default: the plan)
    asks: the cells through each heated solid and across each gap, each heat source and each
    opening, and the first cell and the cells inside the boundary layer next to each heated
    surface. Counts are read off the grid lines, never estimated."""
    asked = asked or plan
    f = facts(placement)
    loc = _Locator(layout)
    names = placement.names or [p.name for p in placement.parts]
    rows: dict[str, list[dict]] = {"gap": [], "band": [], "spread": [], "opening": []}
    # what the final grid's background alone gives (a budget that coarsened it is accounted
    # for): every item finer than that is measured; anything coarser the background resolves
    dom = placement.domain
    H = max(max(dom.hi[i] - dom.lo[i] for i in range(3)) / asked.background_cells,
            float(getattr(layout, "H", 0.0) or 0.0))

    def nm(k: int) -> str:
        return "the domain's side" if k < 0 else str(names[k])

    def points(lo, hi, axis: int, n_done: int) -> list[np.ndarray]:
        """Five points over the rectangle for the first MAX_AUDIT_ROWS items (finest first),
        its centre for the rest: every item is measured, the many cheaply."""
        pts = _samples(lo, hi, axis)
        return pts if n_done < MAX_AUDIT_ROWS else pts[:1]

    # gaps: cells across, at the centre of the faces' overlap (and four points around it)
    if asked.gap_cells > 0:
        for g in f.gaps:
            a, w = g["axis"], g["width"]
            if w < MIN_GAP_M or w / asked.gap_cells >= H:
                continue                           # the background alone gives it the cells
            got = min(len(loc.cells_along(pt, a, g["lo"][a], g["hi"][a]))
                      for pt in points(g["lo"], g["hi"], a, len(rows["gap"])))
            a_, b_ = g["parts"]
            rows["gap"].append({"what": (f"gap between {nm(a_)} and {nm(b_)}" if a_ != b_ else
                                         f"gap inside {nm(a_)}"),
                                "parts": [nm(a_), nm(b_)], "axis": "xyz"[a], "width_m": w,
                                "want": asked.gap_cells, "got": got,
                                "ok": got >= asked.gap_cells})
    # heated surfaces: the first air cell, and the cells inside the boundary layer where the air
    # in front of the face is at least the layer deep - every heated part's faces, and every
    # rectangle that puts heat in (a surface heat source, a heat-flux plate)
    if asked.band_cells > 0:
        per: dict[object, dict] = {}
        lo_b, hi_b = f.boxes_lo, f.boxes_hi
        surfaces: list[tuple] = [(face["part"], f"air on {nm(face['part'])}", face["axis"], (face["side"],),
                     face["lo"], face["hi"], f.layer_m[face["part"]], face["box"])
                    for face in f.faces]
        surfaces += [(("rect", r["name"]), f"air on heat source {r['name']}", r["axis"], (0, 1),
                      r["lo"], r["hi"], boundary_layer(r["length"], f.velocity_m_s, placement), -1)
                     for r in f.heated_rects]
        for key, what, a, sides, s_lo, s_hi, delta, own in surfaces:
            first_want = band_first_cell(delta, asked)
            if first_want >= H:
                continue
            for side in sides:
                plane = s_hi[a] if side else s_lo[a]
                for pt in points(s_lo, s_hi, a, len(per)):
                    free = _free_run(pt, a, side, plane, lo_b, hi_b, placement.domain, own)
                    if free <= 0:
                        continue
                    end = plane + (min(free, delta) if side else -min(free, delta))
                    widths = loc.cells_along(pt, a, plane, end)
                    if not widths:
                        continue
                    first = widths[0] if side else widths[-1]
                    row = per.setdefault(key, {"what": what, "parts": [what[7:]],
                                               "layer_m": delta, "first_want_m": first_want,
                                               "first_m": 0.0, "want": asked.band_cells,
                                               "got": None})
                    row["first_m"] = max(row["first_m"], first)
                    if free >= delta:
                        n = len(widths)
                        row["got"] = n if row["got"] is None else min(row["got"], n)
        for row in per.values():
            if row["got"] is None:              # every face is in a gap: the gap rule judges
                row["got"] = row["want"]
                row["in_gap"] = True
            row["ok"] = row["got"] >= row["want"] and row["first_m"] <= 1.25 * row["first_want_m"]
            rows["band"].append(row)
    # heat sources: cells across their width
    if asked.spread_cells > 0:
        for hb in f.heat_boxes:
            ext = hb["hi"] - hb["lo"]
            top = float(ext.max())
            centre = (hb["lo"] + hb["hi"]) / 2
            fewest: int | None = None
            for a in range(3):
                if ext[a] < 0.5 * top or ext[a] / asked.spread_cells >= H:
                    continue
                n = len(loc.cells_along(centre.copy(), a, hb["lo"][a], hb["hi"][a]))
                fewest = n if fewest is None else min(fewest, n)
            if fewest is None:
                continue
            rows["spread"].append({"what": f"heat source {hb['name']}", "parts": [hb["name"]],
                                   "want": asked.spread_cells, "got": fewest,
                                   "ok": fewest >= asked.spread_cells})
    # fans and vents: cells across the opening, on each axis across it
    if asked.opening_cells > 0:
        for op in f.openings:
            a = op["axis"]
            want = asked.opening_cells
            if min(op["across"] / want, op["ring"] / 3.0) >= H:
                continue                           # the background alone gives it the cells
            centre = (op["lo"] + op["hi"]) / 2
            fewest = None
            for c in (x for x in range(3) if x != a):
                if op["hi"][c] - op["lo"][c] < 0.99 * op["across"]:
                    continue
                n = len(loc.cells_along(centre.copy(), c, op["lo"][c], op["hi"][c]))
                # across the shorter side (a vent) or the diameter (a fan)
                n = int(round(n * op["across"] / max(op["hi"][c] - op["lo"][c], 1e-300)))
                fewest = n if fewest is None else min(fewest, n)
            if fewest is None:
                continue
            rows["opening"].append({"what": f"{op['what']} {op['name']}", "parts": [op["name"]],
                                    "want": want, "got": fewest, "ok": fewest >= want})
    counts = {key: len(r) for key, r in rows.items()}
    for key in rows:
        rows[key].sort(key=lambda r: (r["ok"], r["got"] - r["want"]))
    short = [r for key in rows for r in rows[key] if not r["ok"]]
    return {"rows": rows, "counts": counts, "short": short,
            "velocity_m_s": f.velocity_m_s, "velocity_from": f.velocity_why or
            "natural convection (no fan with a flow in the file)",
            "heated_parts": [nm(k) for k in f.heated], "measured": True}


def _samples(lo: np.ndarray, hi: np.ndarray, axis: int) -> list[np.ndarray]:
    """The centre of a rectangle normal to `axis` and four points half way to its corners."""
    c = (np.asarray(lo, dtype=float) + np.asarray(hi, dtype=float)) / 2
    lat = [x for x in range(3) if x != axis]
    out = [c.copy()]
    for s0 in (-0.25, 0.25):
        for s1 in (-0.25, 0.25):
            p = c.copy()
            p[lat[0]] += s0 * (hi[lat[0]] - lo[lat[0]])
            p[lat[1]] += s1 * (hi[lat[1]] - lo[lat[1]])
            out.append(p)
    return out


def _free_run(pt: np.ndarray, a: int, side: int, plane: float, lo: np.ndarray, hi: np.ndarray,
              dom: _Box, own: int) -> float:
    """How far the air reaches from a face (at point pt, outwards on axis a) before a solid or
    the domain's side; 0 when a solid lies on the face there."""
    lat = [x for x in range(3) if x != a]
    across = (lo[:, lat[0]] < pt[lat[0]]) & (hi[:, lat[0]] > pt[lat[0]]) & \
        (lo[:, lat[1]] < pt[lat[1]]) & (hi[:, lat[1]] > pt[lat[1]])
    across[own] = False
    tiny = 1e-12 * max(1.0, abs(plane))
    if side:
        ahead = across & (hi[:, a] > plane + tiny)
        if (ahead & (lo[:, a] <= plane + tiny)).any():
            return 0.0
        starts = lo[ahead, a]
        return float(min(starts.min() if len(starts) else math.inf, dom.hi[a]) - plane)
    ahead = across & (lo[:, a] < plane - tiny)
    if (ahead & (hi[:, a] >= plane - tiny)).any():
        return 0.0
    ends = hi[ahead, a]
    return float(plane - max(ends.max() if len(ends) else -math.inf, dom.lo[a]))


def short_lines(res: dict, limit: int = 6) -> list[str]:
    """The rules a grid falls short of, in plain words, worst first."""
    out = []
    for key, label in (("gap", "air gap(s) have fewer cells across than asked"),
                       ("band", "heated surface(s) have a coarser warm-air layer than asked"),
                       ("spread", "heat source(s) have fewer cells across than asked"),
                       ("opening", "fan(s) or vent(s) have fewer cells across than asked")):
        bad = [r for r in (res.get("rows") or {}).get(key, []) if not r.get("ok")]
        if not bad:
            continue
        out.append(f"{len(bad)} {label}: " + "; ".join(_short_words(r) for r in bad[:limit])
                   + ("..." if len(bad) > limit else ""))
    return out


def _short_words(r: dict) -> str:
    """One shortfall in words: the cells it got of those asked, or - a warm-air layer with its
    cells but too coarse a first cell - the first cell's size against the one asked."""
    if r.get("first_m") and r["got"] >= r["want"]:
        return (f"{r['what']} (first cell {r['first_m'] * 1e3:.2g} mm, asked "
                f"{r['first_want_m'] * 1e3:.2g} mm)")
    return f"{r['what']} ({r['got']} of {r['want']})"


def summary_lines(res: dict) -> list[str]:
    """What the grid gives each rule, one plain line per rule (the build report and results)."""
    rows = res.get("rows") or {}
    out = []
    g = rows.get("gap") or []
    if g:
        worst = min(g, key=lambda r: r["got"])
        out.append(f"Air gaps: {len(g)} measured; the fewest cells across one is {worst['got']} "
                   f"({worst['what']}, {worst['width_m'] * 1e3:.3g} mm; asked {worst['want']}).")
    b = [r for r in rows.get("band") or [] if not r.get("in_gap")]
    if b:
        worst = min(b, key=lambda r: r["got"])
        first = max(r["first_m"] for r in b)
        out.append(f"Heated surfaces: {len(b)} parts; the warm-air layer on each has at least "
                   f"{worst['got']} cells (asked {worst['want']}); the first cell off a heated "
                   f"surface is at most {first * 1e3:.3g} mm.")
    s = rows.get("spread") or []
    if s:
        worst = min(s, key=lambda r: r["got"])
        out.append(f"Heat sources: {len(s)}; the fewest cells across one is {worst['got']} "
                   f"({worst['what']}; asked {worst['want']}).")
    o = rows.get("opening") or []
    if o:
        worst = min(o, key=lambda r: r["got"])
        out.append(f"Fans and vents: {len(o)}; the fewest cells across one is {worst['got']} "
                   f"({worst['what']}; asked {worst['want']}).")
    return out


__all__ = ["Facts", "RULE_WORDS", "audit", "band_first_cell", "boundary_layer", "describe_plan",
           "facts", "graded_reach", "part_cells", "short_lines", "sources", "summary_lines"]
