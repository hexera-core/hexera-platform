# Responsibility: Decide, by one general rule, which thin layers of a placed ECXML model are meshed with cells through them and which become a thin-layer thermal resistance on the interface between their neighbours - and say how each one is carried.
# Owns: the layer rule (aspect against the cells around it, sideways conduction against its neighbours), the choice of which neighbour fills the layer's place, the resistance each one carries (R = t/k, less what the grown neighbour adds) and the words for it.
# Boundaries: pure Python on a first placement; it moves no plane itself - cad/ingest/ecxml_place.py places the model again with the moves this returns. The mesher tags the interface faces and writes the solver entries (engines/snapgrid/mesher.py).
# Collaborates with: cad/ingest/ecxml_place.py (the caller), engines/snapgrid/mesher.py (the faces and the solver entries).
"""Thin layers in an electronics thermal model: mesh them, or carry them as a resistance.

Semiconductor and package models hold layers from millimetres down to nanometres: die attach and
thermal interface bond lines (10-100 um), metal and dielectric films (0.1-2 um), barrier and seed
films (1-30 nm), hybrid-bonding interfaces (tens of nm). Every thermal tool treats the thin ones
the same way:

* Simcenter Flotherm: a "collapsed" cuboid of thickness t and conductivity k is a through-plane
  resistance t/k, with no sideways conduction - recommended because it avoids thin, high aspect
  ratio cells (Siemens KB MG517829);
* Ansys Icepak: a "contact resistance" thin solid for adhesives and interface materials - through
  the plane only - or a conducting thin plate when it must spread heat sideways;
* COMSOL: the Thin Layer's "thermally thick approximation" (resistive layer: R = d/k, no sideways
  flux) versus its "thermally thin" one (a conductive layer);
* OpenFOAM: a coupled-temperature interface with `thicknessLayers` / `kappaLayers` (OpenFOAM 11
  `coupledTemperature`, v2412 `compressible::turbulentTemperatureRadCoupledMixed`) - the same
  layers on both sides of the interface, the full R on each.

So the rule here, for every plate-like solid (its thickness at most a tenth of its next size):

1. it is MESHED - cells through it, like every other part - unless its cells would be too flat:
   at the cells asked through it (`min_cells_across`), a cell more than MAX_LAYER_ASPECT (1000,
   the aspect checkMesh flags) times wider than it is thick, against the in-plane cell size
   around it (the background cell, or its own width over the same cells when that is smaller);
2. a layer too thin for that becomes a THIN-LAYER RESISTANCE when it is thermally thin: its
   sideways conductance (k x t) is at most LATERAL_SHARE (1%) of the strongest neighbour's, so
   leaving out its sideways conduction changes the spreading by under 1%, and through it the heat
   meets R = t / k. It must also carry no power of its own, have a conductivity that does not vary
   with temperature, lie inside the domain and overlap no other part;
3. anything else stays meshed, however flat its cells: a thin copper plane that spreads heat is
   never turned into a resistance. Its aspect is reported.

The layer's place is filled by the neighbour that conducts better (so the extra thickness costs the
least resistance), and that neighbour's added resistance (t / k_neighbour) is taken off the layer's
own, so the resistance through the stack stays exactly the file's: R_applied = t/k - t/k_grown,
never below zero (said when it would be). The plane that moves must not change any other part's
thickness by more than 1%, or the layer stays meshed. Layers stacked on one another collapse onto
one plane together and their resistances add.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: A layer is plate-like when its thickness is at most this share of its next smallest size.
PLATE_RATIO = 0.1
#: The flattest cell a layer is meshed with: checkMesh's own aspect-ratio limit.
MAX_LAYER_ASPECT = 1000.0
#: A layer whose sideways conductance (k t) is at most this share of its strongest neighbour's is
#: thermally thin: its through-plane resistance is all that matters.
LATERAL_SHARE = 0.01
#: A plane that moves must not change any part's thickness by more than this share.
MOVE_THICKNESS_SHARE = 0.01
#: Air's conductivity, W/mK, for the choice of which side fills a layer that air borders.
K_AIR = 0.026

KINDS = ("solid3dBlock", "solid2dBlock")


@dataclass(frozen=True)
class LayerRule:
    """What the rule weighs: the grid the layer would be meshed in."""
    background_cells: int = 50
    min_cells_across: int = 2
    max_aspect: float = MAX_LAYER_ASPECT
    lateral_share: float = LATERAL_SHARE

    @classmethod
    def from_plan(cls, plan) -> LayerRule:
        return cls(background_cells=int(getattr(plan, "background_cells", 50)),
                   min_cells_across=int(getattr(plan, "min_cells_across", 2)))


@dataclass
class Collapse:
    """What the second placement does: the objects carried as resistances (by file order), the
    planes that move (per axis: from -> to), and the records."""
    skip: dict[int, dict] = field(default_factory=dict)          # object order -> its record
    moves: list[dict[float, float]] = field(default_factory=lambda: [{}, {}, {}])
    kept: list[dict] = field(default_factory=list)               # thin layers left meshed

    def __bool__(self) -> bool:
        return bool(self.skip)


def conductivity(material, axis: int) -> tuple[float, float] | None:
    """(through, sideways) conductivity of a material for a layer normal to `axis`, W/mK, or None
    when it is unknown or varies with temperature (no constant resistance can stand for it)."""
    if material is None:
        return None
    c = material.conductivity if hasattr(material, "conductivity") else material
    kind = c.get("kind")
    if kind == "isotropic":
        k = float(c["W_mK"])
        return k, k
    if kind == "orthotropic":
        ks = [float(v) for v in c["W_mK"]]
        side = [ks[i] for i in range(3) if i != axis]
        return ks[axis], sum(side) / 2
    return None


def _neighbour_k(material, axis: int) -> float | None:
    """A neighbour's through conductivity: as conductivity(), or the stated base value of a
    temperature-dependent one (a neighbour only weighs the choice and the correction)."""
    got = conductivity(material, axis)
    if got is not None:
        return got[0]
    if material is None:
        return None
    c = material.conductivity
    if c.get("kind") == "linear_in_temperature":
        return float(c["W_mK"])
    if c.get("kind") == "piecewise_linear_in_temperature":
        pts = c.get("points_K_W_mK") or []
        return float(pts[len(pts) // 2][1]) if pts else None
    return None


def _area(lo, hi, lat) -> float:
    return max(0.0, hi[lat[0]] - lo[lat[0]]) * max(0.0, hi[lat[1]] - lo[lat[1]])


def _overlap(alo, ahi, blo, bhi, lat) -> float:
    return _area([max(alo[i], blo[i]) for i in range(3)], [min(ahi[i], bhi[i]) for i in range(3)],
                 lat)


class _Boxes:
    """Every live part's material boxes as arrays, so a layer finds what touches or overlaps it
    without walking every box of a 20,000-part board in Python."""

    def __init__(self, rows: list[tuple[int, list[float], list[float]]]) -> None:
        import numpy as np

        self.part = np.array([r[0] for r in rows], dtype=np.int64)
        self.lo = np.array([r[1] for r in rows], dtype=float).reshape(-1, 3)
        self.hi = np.array([r[2] for r in rows], dtype=float).reshape(-1, 3)

    def near(self, lo, hi, pad: float) -> list[tuple[int, list[float], list[float]]]:
        """The boxes that reach within `pad` of the box lo..hi on every axis."""
        import numpy as np

        if not len(self.part):
            return []
        lo_, hi_ = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
        hit = np.flatnonzero(((self.lo <= hi_ + pad) & (self.hi >= lo_ - pad)).all(axis=1))
        return [(int(self.part[j]), self.lo[j].tolist(), self.hi[j].tolist()) for j in hit]


def decide(placement, rule: LayerRule | None = None) -> Collapse:
    """Which thin layers of a FIRST placement become resistances, and the plane moves that close
    their place (see the module docstring). Every thin layer the rule looked at and kept meshed is
    listed in `kept` with why."""
    from meshpipeline.cad.ingest.ecxml_build import _material

    rule = rule or LayerRule()
    out = Collapse()
    model = placement.model
    noise = placement.tol.noise
    dom = placement.domain
    longest = max(dom.hi[i] - dom.lo[i] for i in range(3))
    H0 = longest / max(1, rule.background_cells)
    n = max(1, rule.min_cells_across)
    live = [k for k, p in enumerate(placement.parts) if not p.dropped]
    # every live part's material boxes (what a layer touches or overlaps)
    boxes = _Boxes([(k, list(b.lo), list(b.hi)) for k in live
                    for b in _material(placement.parts[k])])

    candidates = []
    for k in live:
        p = placement.parts[k]
        if p.kind not in KINDS or len(p.objects) != 1 or p.clipped or p.cutters:
            continue
        b = p.box
        ext = [b.hi[i] - b.lo[i] for i in range(3)]
        order = sorted(range(3), key=lambda i: ext[i])
        a, t = order[0], ext[order[0]]
        if t <= 0 or t > PLATE_RATIO * ext[order[1]]:
            continue
        lat = [i for i in range(3) if i != a]
        cell_in_plane = min(H0, min(ext[i] for i in lat) / n)
        aspect = cell_in_plane / (t / n)
        row = {"part": k, "name": p.objects[0].name, "label": p.objects[0].label,
               "path": list(p.objects[0].path), "order": p.objects[0].order,
               "material": p.material, "axis": "xyz"[a], "thickness_m": t,
               "aspect_if_meshed": aspect, "box_m": b.as_dict()}
        if aspect <= rule.max_aspect:
            continue                        # meshed, as every part is: nothing to say
        why, host = _ineligible(placement, p, model, a, dom, noise, boxes, k)
        if why:
            out.kept.append({**row, "meshed": True, "why": why})
            continue
        candidates.append((k, a, t, lat, row, host))

    # sideways conduction against the neighbours, and the neighbours themselves
    chosen: dict[int, dict] = {}
    for k, a, t, lat, row, host in candidates:
        p = placement.parts[k]
        kt, ks = conductivity(model.materials.get(p.material or ""), a)  # type: ignore[misc]
        below, above = _touching(p.box, a, lat, boxes, k, noise)
        foot = _area(p.box.lo, p.box.hi, lat)
        if host is not None:
            # the layer is painted over the part it lies in (it comes later in the file): that
            # part fills its place by itself, and the layer's resistance sits on its face there
            hb = placement.parts[host].box
            if abs(hb.lo[a] - p.box.lo[a]) <= noise:
                above = [(host, foot)]
                below = [(j, ov) for j, ov in below if j != host]
            else:
                below = [(host, foot)]
                above = [(j, ov) for j, ov in above if j != host]
        strongest = 0.0
        for side in (below, above):
            for (j, _ov) in side:
                q = placement.parts[j]
                got = conductivity(model.materials.get(q.material or ""), a)
                k_side = got[1] if got else (_neighbour_k(model.materials.get(q.material or ""), a)
                                             or 0.0)
                qe = q.box.hi[a] - q.box.lo[a]
                strongest = max(strongest, k_side * qe)
        lateral = ks * t
        row.update(k_through_W_mK=kt, k_sideways_W_mK=ks, R_m2K_W=t / kt if kt > 0 else math.inf,
                   sideways_conductance_W_K=lateral, neighbours_strongest_W_K=strongest,
                   footprint_m2=foot,
                   covered_below=sum(ov for _j, ov in below) / foot if foot else 0.0,
                   covered_above=sum(ov for _j, ov in above) / foot if foot else 0.0,
                   below=sorted({placement.names[j] for j, _ in below}),
                   above=sorted({placement.names[j] for j, _ in above}))
        if kt <= 0:
            out.kept.append({**row, "meshed": True, "why": "its conductivity is zero"})
            continue
        if strongest <= 0 or lateral > rule.lateral_share * strongest:
            out.kept.append({**row, "meshed": True, "why": (
                f"it spreads heat sideways: k x t = {lateral:.3g} W/K, more than "
                f"{rule.lateral_share:.0%} of its strongest neighbour's ({strongest:.3g} W/K)"
                if strongest > 0 else "no solid touches it on either side")})
            continue
        chosen[k] = {**row, "_below": below, "_above": above, "_host": host}

    hosted = {k: r for k, r in chosen.items() if r["_host"] is not None}
    for k, r in hosted.items():
        _hosted(placement, k, r, out)
    stacked = {k: r for k, r in chosen.items() if r["_host"] is None}
    if not stacked:
        return out
    # stacks: layers that touch one another collapse together onto one plane
    groups = _stacks(placement, stacked)
    objboxes = _object_boxes(placement)
    for members in groups:
        _collapse_group(placement, members, stacked, objboxes, noise, out, model)
    return out


def _hosted(placement, k: int, chosen: dict, out: Collapse) -> None:
    """A layer painted over the part it lies in, on that part's face: no plane moves - the part
    fills the layer's place once the layer is not painted - and the resistance sits on that face."""
    p = placement.parts[k]
    a = "xyz".index(chosen["axis"])
    hb = placement.parts[chosen["_host"]].box
    t = p.box.hi[a] - p.box.lo[a]
    on_lo = abs(hb.lo[a] - p.box.lo[a]) <= abs(hb.hi[a] - p.box.hi[a])
    row = {key: v for key, v in chosen.items() if not key.startswith("_")}
    row.update(meshed=False, plane_m=p.box.lo[a] if on_lo else p.box.hi[a],
               stack=[p.objects[0].label], stack_thickness_m=t,
               grown_below_m=0.0 if on_lo else t, grown_above_m=t if on_lo else 0.0,
               host=placement.names[chosen["_host"]],
               why=(f"meshed, its cells would be {row['aspect_if_meshed']:.0f} times wider than "
                    "thick, and it is thermally thin; it lies in "
                    f"{placement.names[chosen['_host']]}, which fills its place"))
    out.skip[p.objects[0].order] = row


def _ineligible(placement, p, model, a: int, dom, noise: float, boxes, k: int
                ) -> tuple[str, int | None]:
    """Why a too-thin layer cannot be a resistance (it is then meshed), or "" - and the part it
    lies in, when it lies in one: painted over it (the layer comes later in the file) on one of
    that part's own faces, as a Flotherm layer laid into a block does."""
    if p.power:
        return "it gives off heat, which a resistance cannot carry", None
    mat = model.materials.get(p.material or "")
    if mat is None:
        return "its material is not defined, so its conductivity is unknown", None
    if conductivity(mat, a) is None:
        return "its conductivity varies with temperature", None
    b = p.box
    if b.lo[a] <= dom.lo[a] + noise or b.hi[a] >= dom.hi[a] - noise:
        return "it lies on a side of the domain", None
    over = sorted({j for j, lo, hi in boxes.near(b.lo, b.hi, noise) if j != k and all(
        min(hi[i], b.hi[i]) - max(lo[i], b.lo[i]) > noise for i in range(3))})
    if not over:
        return "", None
    if len(over) == 1:
        q = placement.parts[over[0]]
        hb = q.box
        inside = all(hb.lo[i] - noise <= b.lo[i] and b.hi[i] <= hb.hi[i] + noise for i in range(3))
        on_face = (abs(hb.lo[a] - b.lo[a]) <= noise) != (abs(hb.hi[a] - b.hi[a]) <= noise)
        if inside and on_face and q not in p.winners and len(q.objects) == 1 \
                and q.kind in KINDS + ("printedCircuitBoard",):
            return "", over[0]
    return "another part overlaps it", None


def _touching(box, a: int, lat: list[int], boxes, k: int, noise: float):
    """The parts whose material meets the layer's two faces over its footprint: ([(part, area)]
    below, [(part, area)] above)."""
    below: dict[int, float] = {}
    above: dict[int, float] = {}
    for j, lo, hi in boxes.near(box.lo, box.hi, noise):
        if j == k:
            continue
        ov = _overlap(lo, hi, box.lo, box.hi, lat)
        if ov <= 0:
            continue
        if abs(hi[a] - box.lo[a]) <= noise:
            below[j] = below.get(j, 0.0) + ov
        elif abs(lo[a] - box.hi[a]) <= noise:
            above[j] = above.get(j, 0.0) + ov
    return sorted(below.items()), sorted(above.items())


def _stacks(placement, chosen: dict[int, dict]) -> list[list[int]]:
    """The chosen layers grouped where one touches another across its thickness (same axis)."""
    parent = {k: k for k in chosen}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for k, row in chosen.items():
        for j, _ov in row["_below"] + row["_above"]:
            if j in chosen and chosen[j]["axis"] == row["axis"]:
                parent[find(j)] = find(k)
    out: dict[int, list[int]] = {}
    for k in chosen:
        out.setdefault(find(k), []).append(k)
    return [sorted(v) for v in out.values()]


def _collapse_group(placement, members: list[int], chosen: dict[int, dict], objboxes,
                    noise: float, out: Collapse, model) -> None:
    """One stack of layers onto one plane: the side whose neighbours conduct better grows into
    their place. Checked: no other part's thickness changes by more than MOVE_THICKNESS_SHARE."""
    a = "xyz".index(chosen[members[0]]["axis"])
    parts = placement.parts
    lo_g = min(parts[k].box.lo[a] for k in members)
    hi_g = max(parts[k].box.hi[a] for k in members)
    planes = sorted({v for k in members for v in (parts[k].box.lo[a], parts[k].box.hi[a])})

    def k_of(side: str) -> float:
        ks = []
        for k in members:
            for j, _ov in chosen[k]["_below" if side == "below" else "_above"]:
                if j in chosen:
                    continue
                got = _neighbour_k(model.materials.get(parts[j].material or ""), a)
                if got:
                    ks.append(got)
        covered = all(chosen[k]["covered_" + side] >= 1.0 - 1e-9 for k in members)
        if not covered:
            ks.append(K_AIR)
        return min(ks) if ks else K_AIR

    k_below, k_above = k_of("below"), k_of("above")
    # the better conductor grows: below grows up to the top plane, or above grows down
    tries = [hi_g, lo_g] if k_below >= k_above else [lo_g, hi_g]
    skip_orders = {parts[k].objects[0].order for k in members}
    for target in tries:
        moving = [v for v in planes if abs(v - target) > noise]
        problem = _move_problem(objboxes, a, moving, target, skip_orders, noise)
        if problem:
            continue
        for v in moving:
            out.moves[a][v] = target
        grown_below = target - lo_g          # how far the side below grows (0 or the stack)
        grown_above = hi_g - target
        for k in members:
            row = {key: v for key, v in chosen[k].items() if not key.startswith("_")}
            row.update(meshed=False, plane_m=target, stack=[parts[j].objects[0].label for j in members],
                       stack_thickness_m=hi_g - lo_g, grown_below_m=grown_below,
                       grown_above_m=grown_above, k_below_W_mK=k_below, k_above_W_mK=k_above,
                       why=(f"meshed, its cells would be {row['aspect_if_meshed']:.0f} times wider "
                            "than thick, and it is thermally thin (sideways conductance "
                            f"{row['sideways_conductance_W_K']:.3g} W/K against "
                            f"{row['neighbours_strongest_W_K']:.3g} W/K beside it)"))
            out.skip[parts[k].objects[0].order] = row
        return
    for k in members:
        row = {key: v for key, v in chosen[k].items() if not key.startswith("_")}
        out.kept.append({**row, "meshed": True,
                         "why": "closing its place would change another part's thickness by more "
                                f"than {MOVE_THICKNESS_SHARE:.0%}"})


def _object_boxes(placement):
    """(orders, labels, lo, hi) of every object with a box, as placed - built once per decide."""
    import numpy as np

    objs = [o for o in placement.model.active_objects if o.location is not None
            and o.size is not None and o.kind not in ("assembly", "heatsink", "monitorPoint",
                                                     "externalMcadFile")]
    boxes = [placement.snapped(o) for o in objs]
    return ([o.order for o in objs], [o.label for o in objs],
            np.array([b.lo for b in boxes], dtype=float).reshape(-1, 3),
            np.array([b.hi for b in boxes], dtype=float).reshape(-1, 3))


def _move_problem(objboxes, a: int, moving: list[float], target: float, skip: set[int],
                  noise: float) -> str:
    """Why moving these planes onto `target` would change some other object's thickness by more
    than MOVE_THICKNESS_SHARE (or put both its faces on one plane), or ""."""
    import numpy as np

    orders, labels, los, his = objboxes
    if not orders or not moving:
        return ""
    mv = np.asarray(moving, dtype=float)
    touched = (np.abs(los[:, a][:, None] - mv[None, :]) <= noise).any(axis=1) | \
        (np.abs(his[:, a][:, None] - mv[None, :]) <= noise).any(axis=1)
    for j in np.flatnonzero(touched).tolist():
        if orders[j] in skip:
            continue
        lo, hi = float(los[j, a]), float(his[j, a])
        nlo = target if any(abs(lo - v) <= noise for v in moving) else lo
        nhi = target if any(abs(hi - v) <= noise for v in moving) else hi
        if nlo == lo and nhi == hi:
            continue
        before = hi - lo
        after = nhi - nlo
        if before <= noise and after <= noise:
            continue                         # a flat object (a vent, a fan face) moves whole
        if abs(after - before) > MOVE_THICKNESS_SHARE * max(before, noise):
            return labels[j]
    return ""


def applied_resistance(films: list[dict], k_lo: float | None, k_hi: float | None,
                       grown_below: float, grown_above: float) -> tuple[float, float]:
    """(the layers' own resistance sum(t/k), the resistance to apply on the interface) for faces
    between a region below (conductivity k_lo, or None for air) and one above: the layers' own
    less what the grown neighbour now adds (grown / k), never below zero, m2K/W. Air that grows is
    not taken off - its heat moves by convection, not by a resistance."""
    own = sum(float(f["thickness_m"]) / float(f["k_through_W_mK"]) for f in films)
    less = 0.0
    if k_lo and grown_below > 0:
        less += grown_below / k_lo
    if k_hi and grown_above > 0:
        less += grown_above / k_hi
    return own, max(0.0, own - less)


__all__ = ["Collapse", "LayerRule", "applied_resistance", "conductivity", "decide"]
