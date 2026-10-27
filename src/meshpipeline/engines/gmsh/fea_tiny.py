# Responsibility: MESH-9 - at mesh time, mesh a CAD face far narrower (or a CAD edge far shorter) than the planned element size there as part of a neighbour (gmsh compound surfaces and curves: a smooth neighbour first, else - as Ansys defeatures - collapsed onto its edge across the crease), never changing the CAD; then put the merged face's elements in its neighbour's named group and record which face went into which.
# Boundaries: gmsh model in, compound constraints and a record out; it never edits the staged file, never merges across parts, into or out of a joint or out of a stress hot spot, and on the CFD path never takes a group's only face into another group. Sizes come from the plan as sized for the model (engines/fea_plan.py zones), never from a fixed length.
# Collaborates with: engines/gmsh/fea_adapter.py (calls plan_merges/apply before sizing, settle_nodes after the surface stage, fold_in before the files are written), engines/gmsh/driver.py (the CFD gmsh path's CAD solids, the same rule at its one size), engines/fea_size_field.group_zones.
"""MESH-9: TINY FACES AND SHORT EDGES ARE MESHED AS PART OF THEIR NEIGHBOURS.

Ansys meshes through features smaller than its Defeature Size, which by default is 0.005 x the element
size (Meshing Options: "Mechanical Defeature Size Factor (Default: 0.005)", "CFD Defeature Size
Factor (Default: 0.005)"), and its virtual topology merges sliver faces and short edges into their
neighbours. gmsh meshes every CAD face as drawn: a face a few microns wide must be meshed across its
width, so the triangles on and beside it are slivers, the tetrahedra on them are slivers too, and the
fill can refuse them outright.

Here, at mesh time and on the model only (the CAD file is never changed):

* A face is TINY when its width (twice its area over its perimeter) is under TINY_SHARE of the
  smallest element size the plan can put anywhere on it or its neighbours - read off the plan's zones
  the way MESH-6 bounds them, so the share is of the local size, never of a fixed length.
* A tiny face is meshed as ONE gmsh compound surface with ONE neighbour: a SMOOTH one first (the two
  faces meet within CREASE_DEG), the one it shares the most boundary with; with none, as Ansys's mesh
  defeaturing drops a feature at or under its Defeature Size whatever the angle, it COLLAPSES onto its
  edge with the neighbour across the crease it shares most with (the corner moves by less than its
  width; the crease the sliver drew as two edges is one). Never into another part (both faces bound the
  same solids), never into or out of a face of a joint between parts (bonded, tied or contact), never a
  face a stress hot spot names (a hole, fillet, notch or capped corner), and on the CFD path a face that
  is its group's only face never goes into another group (a neighbour of its own group comes first).
* The curves left between the same two (merged) faces either side of a vertex that now joins only
  them are meshed as one compound curve when the vertex was a corner of a merged face (and the curves
  run on within CREASE_DEG), or when one of them is a SHORT edge (under TINY_SHARE of the local size):
  the vertex is no longer a node. The faces those curves bound are compounds too (each alone when not
  merged), so every face meets its neighbours on the same nodes.
* gmsh puts every element back on the CAD face it lies on (Mesh.CompoundClassify 1); the nodes of a
  compound are then put back exactly on their CAD face or curve, with their parameters, before the
  fill (so the curving of quadratic elements reads the right surface), and after the mesh is done a
  merged face's elements are moved to the neighbour's group: the neighbour's named face is its own
  area plus the tiny face's, and the record names every merge (quality.json "fea_tiny").

A model with no tiny face and no short edge is untouched: no compound is set, and the mesh is the same
mesh, to the byte.
"""
from __future__ import annotations

import math
import sys

#: MESH-9: a face narrower, or an edge shorter, than this share of the local planned element size is
#: meshed through - Ansys's default Defeature Size Factor (Mechanical and CFD alike).
TINY_SHARE = 0.005
#: MESH-9: two faces meeting at more than this angle (between their outward normals) meet at a crease,
#: and a crease is never meshed across - the feature angle the snappy path extracts edges at (30).
CREASE_DEG = 30.0
#: the rule
RULE = "MESH-9"
#: zone kinds that name a stress feature: their faces are never merged away
_HOT = ("hole", "fillet", "notch", "corner")


def _length(gmsh, c: int) -> float:
    try:
        return float(gmsh.model.occ.getMass(1, int(c)))
    except Exception:  # noqa: BLE001 - a curve OpenCASCADE cannot measure has no length here
        return 0.0


def _area(gmsh, s: int) -> float:
    try:
        return float(gmsh.model.occ.getMass(2, int(s)))
    except Exception:  # noqa: BLE001
        return 0.0


def _curves(gmsh, s: int) -> list[int]:
    return [abs(int(t)) for _d, t in gmsh.model.getBoundary([(2, int(s))], oriented=False, combined=False)]


def _ends(gmsh, c: int) -> list[int]:
    return [abs(int(t)) for _d, t in gmsh.model.getBoundary([(1, int(c))], oriented=False, combined=False)]


def _faces_of(gmsh, c: int) -> list[int]:
    return sorted({int(s) for s in gmsh.model.getAdjacencies(1, int(c))[0]})


def _volumes_of(gmsh, s: int) -> tuple[int, ...]:
    return tuple(sorted(int(v) for v in gmsh.model.getAdjacencies(2, int(s))[0]))


def _faces_near(gmsh, s: int) -> set[int]:
    """The faces sharing a curve with face s."""
    return {n for c in _curves(gmsh, s) for n in _faces_of(gmsh, c) if n != s}


def outward_signs(gmsh) -> dict[int, int]:
    """Each surface's orientation in the one solid it bounds (+1: its own normal points out of it)."""
    sign: dict[int, int] = {}
    for _d, v in gmsh.model.getEntities(3):
        for _dd, t in gmsh.model.getBoundary([(3, int(v))], oriented=True, combined=False):
            sign.setdefault(abs(int(t)), 1 if int(t) > 0 else -1)
    return sign


def _mid(gmsh, c: int) -> list[float] | None:
    try:
        lo, hi = gmsh.model.getParametrizationBounds(1, int(c))
        return [float(v) for v in gmsh.model.getValue(1, int(c), [0.5 * (float(lo[0]) + float(hi[0]))])]
    except Exception:  # noqa: BLE001
        return None


def _normal(gmsh, s: int, xyz: list[float], sign: int) -> list[float] | None:
    try:
        uv = gmsh.model.getParametrization(2, int(s), xyz)
        n = [float(v) for v in gmsh.model.getNormal(int(s), list(uv))]
    except Exception:  # noqa: BLE001
        return None
    norm = math.sqrt(sum(v * v for v in n))
    return [sign * v / norm for v in n] if norm > 0 else None


def crease_deg(gmsh, c: int, a: int, b: int, signs: dict[int, int]) -> float | None:
    """The angle between faces `a` and `b`'s outward normals at the middle of curve `c` (0: they run on
    smoothly; 90: a right-angle edge); None where it cannot be read."""
    p = _mid(gmsh, c)
    if p is None:
        return None
    na, nb = _normal(gmsh, a, p, signs.get(a, 1)), _normal(gmsh, b, p, signs.get(b, 1))
    if na is None or nb is None:
        return None
    d = max(-1.0, min(1.0, sum(x * y for x, y in zip(na, nb, strict=True))))
    return math.degrees(math.acos(d))


def _tangent(gmsh, c: int, p: int) -> list[float] | None:
    """Curve `c`'s unit tangent at its end point `p`, pointing away from `p` into the curve."""
    try:
        lo, hi = gmsh.model.getParametrizationBounds(1, int(c))
        lo_, hi_ = float(lo[0]), float(hi[0])
        xyz = [float(v) for v in gmsh.model.getValue(0, int(p), [])]
        t = float(gmsh.model.getParametrization(1, int(c), xyz)[0])
        at_lo = abs(t - lo_) <= abs(t - hi_)
        d = [float(v) for v in gmsh.model.getDerivative(1, int(c), [lo_ if at_lo else hi_])]
    except Exception:  # noqa: BLE001
        return None
    n = math.sqrt(sum(v * v for v in d))
    if n <= 0:
        return None
    return [(v if at_lo else -v) / n for v in d]


def runs_on(gmsh, a: int, b: int, p: int) -> bool:
    """Curves `a` and `b` meeting at point `p` run on within CREASE_DEG (no corner there)."""
    ta, tb = _tangent(gmsh, a, p), _tangent(gmsh, b, p)
    if ta is None or tb is None:
        return False
    d = max(-1.0, min(1.0, -sum(x * y for x, y in zip(ta, tb, strict=True))))
    return math.degrees(math.acos(d)) < CREASE_DEG


def local_floor(gmsh, plan: dict, entities: list[tuple[int, int]], tags_of) -> dict[tuple[int, int], float]:
    """The smallest element size the plan can put anywhere on each entity (MESH-6's box bound of every
    zone's law there; a part's own base on and in the part): the local size MESH-9's share is of. Lower
    than the size gmsh will use, never higher - so a feature is only ever called tiny against an element
    at least that small."""
    import numpy as np

    from meshpipeline.engines.fea_size_field import group_zones
    from meshpipeline.engines.gmsh.fea_adapter import BOX_PAD_MODEL, _box_gap, _boxes, _threshold_low

    base = float(plan["base_size_m"])
    g1 = max(float(plan.get("growth") or 1.3) - 1.0, 1e-6)
    out = dict.fromkeys(entities, base)
    if not entities:
        return out
    x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(-1, -1)
    pad_model = BOX_PAD_MODEL * (math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0)
    box = _boxes(gmsh, entities, pad_model)
    low = np.full(len(entities), base)
    for g in group_zones(plan.get("zones") or []):
        size = float(g["size"])
        if size >= base:
            continue
        tags = sorted({t for f in g.get("faces") or [] for t in tags_of(f)})
        if not tags:
            continue
        reach = float(g.get("reach") or 0.0)
        law = (size, base, reach, reach + (base - size) / g1)
        gap = np.full(len(entities), np.inf)
        for sb in _boxes(gmsh, [(2, t) for t in tags], pad_model):
            gap = np.minimum(gap, _box_gap(box, sb))
        # a Threshold's lowest at that distance; PLAN-fea-17's part base wherever the part's faces are
        low = np.minimum(low, np.where(gap <= 0.0, size, base) if g.get("part") else _threshold_low(gap, *law))
    for k, dt in enumerate(entities):
        out[dt] = float(low[k])
    return out


def plan_merges(gmsh, plan: dict, *, face_tags: dict[str, list[int]] | None = None,
                protected: set[int] | None = None, groups: dict[int, str] | None = None,
                floor_of=None, frozen: set[int] | None = None) -> dict:
    """MESH-9's decisions on the current (synchronised, not yet meshed) model, at the plan's sizes as the
    model reads them: {"faces": {tiny surface: neighbour}, "chains": [[curves]...], "alone": [surfaces],
    "rows": [...] (the record)}. Empty lists everywhere when nothing is tiny. `groups` (the CFD path): each
    surface's named group (inlet, outlet, wall...) - a face only ever merges into a face of its own group;
    `floor_of(entities)` -> {entity: smallest size there} in place of the plan's zones (the CFD path's).
    `frozen`: surfaces whose mesh this pass does not make (an earlier pass's kept parts, LAD-fea-11) - never
    merged, merged into, or bounded by a compound curve."""
    frozen = {int(t) for t in frozen or ()}
    protected = set(protected or ()) | frozen
    base = float(plan["base_size_m"])
    limit_base = TINY_SHARE * base

    def tags_of(name: str) -> list[int]:
        if face_tags is not None:
            return [int(t) for t in face_tags.get(str(name)) or []]
        try:
            return [int(str(name).rsplit("_", 1)[-1])]
        except ValueError:
            return []

    hot = {t for z in plan.get("zones") or [] if z.get("kind") in _HOT for f in z.get("faces") or [] for t in tags_of(f)}
    surfaces = sorted(int(t) for _d, t in gmsh.model.getEntities(2))
    curves = sorted(int(t) for _d, t in gmsh.model.getEntities(1))
    length = {c: _length(gmsh, c) for c in curves}
    bound = {s: _curves(gmsh, s) for s in surfaces}
    width = {}
    for s in surfaces:
        per = sum(length.get(c, 0.0) for c in bound[s])
        width[s] = 2.0 * _area(gmsh, s) / per if per > 0 else 0.0
    # CANDIDATES by the base size first (the local size is never above it), then held to their own floor
    cand_s = [s for s in surfaces if 0.0 < width[s] < limit_base and s not in frozen]
    cand_c = [c for c in curves if 0.0 < length[c] < limit_base and not frozen & set(_faces_of(gmsh, c))]
    empty: dict = {"faces": {}, "chains": [], "alone": [], "rows": [], "short": []}
    if not cand_s and not cand_c:
        return empty
    near: dict[int, set[int]] = {s: {n for c in bound[s] for n in _faces_of(gmsh, c)} for s in cand_s}
    ents = sorted({(2, s) for s in cand_s} | {(2, n) for s in cand_s for n in near[s]}
                  | {(2, f) for c in cand_c for f in _faces_of(gmsh, c)})
    floor = floor_of(ents) if floor_of is not None else local_floor(gmsh, plan, ents, tags_of)

    def h_face(s: int) -> float:
        return min([floor.get((2, s), base)] + [floor.get((2, n), base) for n in near.get(s, ())])

    tiny = {s for s in cand_s if width[s] < TINY_SHARE * h_face(s)}
    short = {c for c in cand_c if length[c] < TINY_SHARE * min([floor.get((2, f), base) for f in _faces_of(gmsh, c)]
                                                                or [base])}
    if not tiny and not short:
        return empty
    signs = outward_signs(gmsh)
    target: dict[int, int] = {}
    rows: list[dict] = []
    group_size: dict[str, int] = {}
    for g in (groups or {}).values():
        group_size[g] = group_size.get(g, 0) + 1
    for s in sorted(tiny):
        if s in protected or s in hot:
            continue
        vols = _volumes_of(gmsh, s)
        if len(vols) != 1:
            continue
        # each neighbour: (a smooth join, the same named group, the boundary shared, the largest crease)
        shared: dict[int, list] = {}
        for c in bound[s]:
            others = [n for n in _faces_of(gmsh, c) if n != s]
            if len(others) != 1:
                continue
            n = others[0]
            if n in tiny or n in protected or _volumes_of(gmsh, n) != vols:
                continue
            same = groups is None or groups.get(n) == groups.get(s)
            # the CFD path: a face that is its group's only face never goes into another group
            if not same and group_size.get(str(groups.get(s)), 0) <= 1:
                continue
            ang = crease_deg(gmsh, c, s, n, signs)
            if ang is None:
                continue
            row = shared.setdefault(n, [True, same, 0.0, 0.0])
            row[0] = row[0] and ang < CREASE_DEG
            row[2] += length.get(c, 0.0)
            row[3] = max(row[3], ang)
        if shared:
            # A SMOOTH NEIGHBOUR FIRST (the face is meshed as part of it); else, as Ansys defeatures, the
            # sliver collapses onto its edge with the neighbour across the crease it shares most with
            n = sorted(shared, key=lambda k: (not shared[k][0], not shared[k][1], -shared[k][2], k))[0]
            target[s] = n
            rows.append({"face": int(s), "into": int(n), "width_m": width[s], "local_size_m": h_face(s),
                         "share": width[s] / h_face(s), "crease_deg": round(float(shared[n][3]), 2),
                         "collapsed": not shared[n][0]})
    # the regions: a target and the tiny faces in it
    region = {s: target.get(s, s) for s in surfaces}
    erased = {c for c in curves if len({region[f] for f in _faces_of(gmsh, c)}) == 1 and len(_faces_of(gmsh, c)) == 2
              and any(f in target for f in _faces_of(gmsh, c))}
    corners = {p for c in erased for p in _ends(gmsh, c)}
    # the curves between each pair of regions, joined at vertices that now join only them
    pair_of: dict[int, tuple[int, int]] = {}
    for c in curves:
        if c in erased:
            continue
        fs = _faces_of(gmsh, c)
        if frozen & set(fs):
            continue
        rs = tuple(sorted({region[f] for f in fs}))
        if len(fs) == 2 and len(rs) == 2:
            pair_of[c] = rs
    links: dict[int, set[int]] = {c: set() for c in pair_of}
    for _d, p in gmsh.model.getEntities(0):
        p = int(p)
        live = [int(c) for c in gmsh.model.getAdjacencies(0, p)[0] if int(c) not in erased]
        if len(live) != 2 or live[0] == live[1]:
            continue
        a, b = live
        if a not in pair_of or b not in pair_of or pair_of[a] != pair_of[b]:
            continue
        if not ((a in short or b in short) or (p in corners and runs_on(gmsh, a, b, p))):
            continue
        links[a].add(b)
        links[b].add(a)
    chains: list[list[int]] = []
    seen: set[int] = set()
    for c in sorted(links):
        if c in seen or not links[c]:
            continue
        comp, todo = [], [c]
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            todo.extend(links[x] - seen)
        # A CLOSED LOOP of curves (a whole rim) keeps one vertex: the chain is cut where two long curves meet
        if all(len(links[x]) == 2 for x in comp):
            cut = next(((a, b) for a in sorted(comp) for b in sorted(links[a]) if a not in short and b not in short), None)
            if cut is None:
                continue
            links[cut[0]].discard(cut[1])
            links[cut[1]].discard(cut[0])
        if len(comp) > 1:
            chains.append(sorted(comp))
    in_chain = {c for ch in chains for c in ch}
    regions: dict[int, list[int]] = {}
    for s, r in region.items():
        regions.setdefault(r, []).append(s)
    merged_regions = {r for r, ss in regions.items() if len(ss) > 1}
    touched = {f for c in in_chain for f in _faces_of(gmsh, c)}
    alone = sorted({s for s in touched if region[s] not in merged_regions})
    short_rows = [{"curve": int(c), "length_m": length[c]} for c in sorted(short & in_chain)]
    return {"faces": {int(k): int(v) for k, v in sorted(target.items())}, "chains": chains, "alone": alone,
            "rows": rows, "short": short_rows}


def physical_groups(gmsh) -> dict[int, str]:
    """Each surface's physical group name (the CFD path's inlet / outlet / wall groups)."""
    out: dict[int, str] = {}
    for _d, p in gmsh.model.getPhysicalGroups(2):
        name = gmsh.model.getPhysicalName(2, p)
        for t in gmsh.model.getEntitiesForPhysicalGroup(2, p):
            out.setdefault(int(t), str(name))
    return out


#: the CFD path's floor: sample points per parameter direction of a face (its curvature and the passage
#: field are read there)
CFD_SAMPLES = 5


def cfd_floor(gmsh, h: float, h_min: float, curvature_nodes: int, size_at=None):
    """The CFD gmsh path's smallest element size on each entity, as its own sizing makes it: the clamp size
    h, finer where gmsh's curvature sizing asks (`curvature_nodes` per turn of the entity's own or its
    curves' tightest bend), finer where the passage field asks (`size_at(dim, tag, x, y, z, h)`, at
    sample points of the entity) - never below gmsh's MeshSizeMin `h_min`."""
    def bend(d: int, t: int) -> float:
        try:
            lo, hi = gmsh.model.getParametrizationBounds(d, t)
        except Exception:  # noqa: BLE001
            return 0.0
        n = CFD_SAMPLES
        if d == 1:
            par = [float(lo[0]) + (float(hi[0]) - float(lo[0])) * (k + 0.5) / n for k in range(n)]
        else:
            par = [v for i in range(n) for j in range(n)
                   for v in (float(lo[0]) + (float(hi[0]) - float(lo[0])) * (i + 0.5) / n,
                             float(lo[1]) + (float(hi[1]) - float(lo[1])) * (j + 0.5) / n)]
        try:
            return max([abs(float(k)) for k in gmsh.model.getCurvature(d, t, par)] + [0.0])
        except Exception:  # noqa: BLE001
            return 0.0

    def one(d: int, t: int) -> float:
        size = float(h)
        kappa = max([bend(d, t)] + [bend(1, c) for c in (_curves(gmsh, t) if d == 2 else [])])
        if curvature_nodes and kappa > 0:
            size = min(size, 2.0 * math.pi / (kappa * float(curvature_nodes)))
        if size_at is not None:
            try:
                lo, hi = gmsh.model.getParametrizationBounds(d, t)
                n = CFD_SAMPLES
                par = [v for i in range(n) for j in range(n)
                       for v in (float(lo[0]) + (float(hi[0]) - float(lo[0])) * i / (n - 1),
                                 float(lo[1]) + (float(hi[1]) - float(lo[1])) * j / (n - 1))] if d == 2 else []
                pts = gmsh.model.getValue(d, t, par) if par else []
                for k in range(0, len(pts), 3):
                    size = min(size, float(size_at(d, t, pts[k], pts[k + 1], pts[k + 2], float(h))))
            except Exception:  # noqa: BLE001 - the passage field unread here: the clamp and the bends stand
                pass
        return max(float(h_min), size)

    return lambda entities: {(int(d), int(t)): one(int(d), int(t)) for d, t in entities}


def cfd_plan(floor: float) -> dict:
    """The CFD gmsh path's one size, as plan_merges reads a plan: its smallest element everywhere."""
    return {"base_size_m": float(floor), "zones": [], "growth": 1.3}


def active(merges: dict | None) -> bool:
    return bool(merges and (merges.get("faces") or merges.get("chains")))


def apply(gmsh, merges: dict) -> None:
    """The compound constraints (before the curve stage)."""
    if not active(merges):
        return
    gmsh.option.setNumber("Mesh.CompoundClassify", 1)
    # the model's own entities, before meshing adds the compounds' (settle_nodes tells them apart)
    merges["entities"] = [(int(d), int(t)) for d, t in gmsh.model.getEntities()]
    # a member meshed before (a mesh file read back - LAD-fea-11's kept parts - holds every curve an earlier
    # pass meshed) starts afresh: a compound never meets segments an earlier compound laid on its curves
    stale = [dt for dt in members(merges) if sum(len(x) for x in gmsh.model.mesh.getElements(*dt)[1])]
    if stale:
        gmsh.model.mesh.clear(stale)
    regions: dict[int, list[int]] = {}
    for s, n in merges["faces"].items():
        regions.setdefault(int(n), [int(n)]).append(int(s))
    for ss in regions.values():
        gmsh.model.mesh.setCompound(2, sorted(ss))
    for s in merges.get("alone") or []:
        gmsh.model.mesh.setCompound(2, [int(s)])
    for ch in merges.get("chains") or []:
        gmsh.model.mesh.setCompound(1, sorted(int(c) for c in ch))
    print(f"[GMSH-FEA] tiny faces meshed as part of a neighbour ({RULE}): {merges['faces']}; "
          f"curves meshed through: {merges.get('chains')}", file=sys.stderr)


def members(merges: dict) -> list[tuple[int, int]]:
    """The CAD entities whose mesh a compound made (their nodes to settle back onto the CAD)."""
    faces = {int(s) for s in merges.get("faces") or {}} | {int(n) for n in (merges.get("faces") or {}).values()}
    faces |= {int(s) for s in merges.get("alone") or []}
    curves = {int(c) for ch in merges.get("chains") or [] for c in ch}
    return sorted({(1, c) for c in curves} | {(2, s) for s in faces})


def settle_nodes(gmsh, merges: dict) -> int:
    """After the surface stage: every node gmsh classified back on a compound's CAD face or curve put
    exactly on it, with its parameters there (a compound is meshed on a triangulation of its faces, so
    its nodes sit on that triangulation's facets, and their parameters are not the face's own). Returns
    how many nodes were set."""
    if not active(merges):
        return 0
    import numpy as np

    n = 0
    own = members(merges)
    # THE COMPOUNDS' OWN ENTITIES: gmsh puts a compound surface's triangles back on the CAD faces, but a
    # compound curve keeps its segments on the new curve it made (the CAD curves keep the first mesh they
    # were given, which nothing uses). Every node on a new entity goes onto the nearest of its CAD
    # members, and every segment back onto the CAD curve it lies on - so the curving of quadratic
    # elements reads the CAD curve, not the triangulation the compound was meshed on.
    before = {(int(d), int(t)) for d, t in merges.get("entities") or []}
    for d, t in gmsh.model.getEntities():
        d, t = int(d), int(t)
        if d not in (1, 2) or (d, t) in before:
            continue
        cand = [c for dd, c in own if dd == d]
        tags, xyz, par = gmsh.model.mesh.getNodes(d, t, includeBoundary=False, returnParametricCoord=True)
        if not cand:
            continue
        if len(tags):
            pts = [float(v) for v in xyz]
            best = np.full(len(tags), np.inf)
            at = np.asarray(xyz, dtype=float).reshape(-1, 3).copy()
            for c in cand:
                got = np.asarray(gmsh.model.getClosestPoint(d, c, pts)[0], dtype=float).reshape(-1, 3)
                dist = np.linalg.norm(got - np.asarray(pts).reshape(-1, 3), axis=1)
                closer = dist < best
                best[closer], at[closer] = dist[closer], got[closer]
            p = np.asarray(par, dtype=float).reshape(len(tags), -1)
            for k, tag in enumerate(tags):
                gmsh.model.mesh.setNode(int(tag), at[k].tolist(), p[k].tolist() if p.size else [])
                n += 1
        if d == 1:
            _segments_home(gmsh, t, cand)
    # each node then on the CAD entity its segments and triangles lie on (gmsh keeps every other node
    # where it was, its parameters with it), and every node of a member put exactly on it, with its
    # parameters there
    gmsh.model.mesh.reclassifyNodes()
    # the compounds' own entities, now empty, leave the model: a mesh file written later (LAD-fea-11's kept
    # parts) never carries them, so a later pass's compounds never meet one under their own tag
    spent = [(d, t) for d, t in gmsh.model.getEntities() if d in (1, 2) and (int(d), int(t)) not in before
             and not sum(len(x) for x in gmsh.model.mesh.getElements(d, t)[1])]
    for d, t in sorted(spent, reverse=True):          # surfaces first: a curve bounding one stays
        try:
            gmsh.model.removeEntities([(d, t)])
        except Exception:  # noqa: BLE001 - an entity gmsh holds on to stays; it has no element
            pass
    for d, t in own:
        tags, xyz, _par = gmsh.model.mesh.getNodes(d, t, includeBoundary=False, returnParametricCoord=True)
        if not len(tags):
            continue
        uv = gmsh.model.getParametrization(d, t, [float(v) for v in xyz])
        on = gmsh.model.getValue(d, t, list(uv))
        for k, tag in enumerate(tags):
            gmsh.model.mesh.setNode(int(tag), [float(v) for v in on[3 * k:3 * k + 3]],
                                    [float(v) for v in uv[d * k:d * k + d]])
            n += 1
    return n


def curve_midsides(gmsh, merges: dict) -> int:
    """After the quadratic order is set: every midside node on a compound's CAD faces and curves put at
    the point of that face or curve nearest its edge's middle. gmsh places a midside node in the face's
    own parameters, from those of the edge's two ends - and a corner of a merged tiny face is a CAD point
    of no face of the neighbour's (strip_block's strip corners), so its parameters there are not known and
    the midside node lands across the face (an element turned inside out). Returns how many were set."""
    if not active(merges):
        return 0
    import numpy as np

    moved = 0
    nt, xyz, _p = gmsh.model.mesh.getNodes()
    pts = np.zeros((int(nt.max()) + 1 if len(nt) else 1, 3))
    pts[np.asarray(nt, dtype=np.int64)] = np.asarray(xyz, dtype=float).reshape(-1, 3)
    for d, t in members(merges):
        per, want = (6, 9) if d == 2 else (3, 8)            # gmsh's 6-node triangle, 3-node line
        # only the midside nodes ON this face or curve: one on an edge along a curve belongs to the curve
        mine = {int(n) for n in gmsh.model.mesh.getNodes(d, t, includeBoundary=False)[0]}
        types, _tags, nodes = gmsh.model.mesh.getElements(d, t)
        for et, nd in zip(types, nodes, strict=True):
            if int(et) != want:
                continue
            el = np.asarray(nd, dtype=np.int64).reshape(-1, per)
            ids: list[int] = []
            at: list = []
            for slot, (a, b) in zip(midside_slots(d), midside_ends(d), strict=True):
                keep = np.asarray([int(n) in mine for n in el[:, slot]], dtype=bool)
                ids.extend(int(n) for n in el[keep, slot])
                at.append(0.5 * (pts[el[keep, a]] + pts[el[keep, b]]))
            if not ids:
                continue
            mid = np.concatenate(at)
            got, par = gmsh.model.getClosestPoint(d, t, [float(v) for v in mid.ravel()])
            got = np.asarray(got, dtype=float).reshape(-1, 3)
            par = np.asarray(par, dtype=float).reshape(len(ids), -1)
            for k, n in enumerate(ids):
                gmsh.model.mesh.setNode(n, got[k].tolist(), par[k].tolist())
                moved += 1
    return moved


def midside_slots(d: int) -> tuple[int, ...]:
    """The midside slots of gmsh's quadratic triangle (d = 2) or line (d = 1)."""
    return (3, 4, 5) if d == 2 else (2,)


def midside_ends(d: int) -> tuple[tuple[int, int], ...]:
    """The corners each midside slot of midside_slots(d) lies between."""
    return ((0, 1), (1, 2), (2, 0)) if d == 2 else ((0, 1),)


def _segments_home(gmsh, compound: int, curves: list[int]) -> None:
    """A compound curve's segments moved onto the CAD curves they lie on (each by its middle)."""
    import numpy as np

    types, tags, nodes = gmsh.model.mesh.getElements(1, int(compound))
    if not sum(len(x) for x in tags):
        return
    mine = sorted({int(c) for c in curves})
    homes: dict[int, list[tuple[int, list[int], list[int]]]] = {}
    for et, tg, nd in zip(types, tags, nodes, strict=True):
        per = len(nd) // max(len(tg), 1)
        seg = np.asarray(nd, dtype=np.int64).reshape(-1, per)
        mids = []
        for a, b in seg[:, :2].tolist():
            pa = gmsh.model.mesh.getNode(int(a))[0]
            pb = gmsh.model.mesh.getNode(int(b))[0]
            mids.extend([0.5 * (float(x) + float(y)) for x, y in zip(pa, pb, strict=True)])
        best = np.full(len(seg), np.inf)
        home = np.zeros(len(seg), dtype=np.int64)
        for c in mine:
            got = np.asarray(gmsh.model.getClosestPoint(1, c, mids)[0], dtype=float).reshape(-1, 3)
            dist = np.linalg.norm(got - np.asarray(mids).reshape(-1, 3), axis=1)
            closer = dist < best
            best[closer], home[closer] = dist[closer], c
        for c in mine:
            sel = home == c
            if sel.any():
                homes.setdefault(c, []).append((int(et), [int(x) for x in np.asarray(tg)[sel]],
                                                [int(x) for x in seg[sel].ravel()]))
    gmsh.model.mesh.removeElements(1, int(compound))
    for c in mine:
        if c in homes:
            gmsh.model.mesh.removeElements(1, c)
            for et, tg, nd in homes[c]:
                gmsh.model.mesh.addElementsByType(c, et, tg, nd)


def fold_in(gmsh, merges: dict) -> dict[int, int]:
    """After the mesh is done (curved, checked): each merged face's elements moved onto its neighbour's
    surface, so the neighbour's named face covers both. Returns {surface: elements moved}."""
    moved: dict[int, int] = {}
    if not active(merges):
        return moved
    for s, n in sorted((merges.get("faces") or {}).items()):
        types, tags, nodes = gmsh.model.mesh.getElements(2, int(s))
        count = sum(len(t) for t in tags)
        if not count:
            moved[int(s)] = 0
            continue
        gmsh.model.mesh.removeElements(2, int(s))
        for et, tg, nd in zip(types, tags, nodes, strict=True):
            gmsh.model.mesh.addElementsByType(int(n), int(et), [int(x) for x in tg], [int(x) for x in nd])
        moved[int(s)] = int(count)
    return moved


def record(merges: dict, names, scale: float = 1.0) -> dict:
    """quality.json's "fea_tiny": every merge with the faces' names (`names(tags)` -> list of names), in
    millimetres at the metre model's size (`scale`: the model's length per metre)."""
    k = 1000.0 / float(scale)
    faces = [{"face": names([r["face"]]), "into": names([r["into"]]),
              "width_mm": round(float(r["width_m"]) * k, 6), "local_size_mm": round(float(r["local_size_m"]) * k, 6),
              "share": round(float(r["share"]), 6),
              # a sliver collapsed onto its edge across a crease (Ansys's defeaturing), and that crease
              "collapsed": bool(r.get("collapsed")), "crease_deg": r.get("crease_deg")}
             for r in merges.get("rows") or []]
    return {"rule": RULE, "share": TINY_SHARE, "crease_deg": CREASE_DEG, "merged": faces,
            "curves_meshed_through": len({c for ch in merges.get("chains") or [] for c in ch}),
            "chains": len(merges.get("chains") or []),
            "short_edges": [{"curve": r["curve"], "length_mm": round(float(r["length_m"]) * k, 6)}
                            for r in merges.get("short") or []]}


def words(rec: dict | None) -> str:
    """The record in plain words for the result screen (empty when nothing was merged)."""
    merged = list((rec or {}).get("merged") or [])
    short = len((rec or {}).get("short_edges") or [])
    if not merged and not short:
        return ""
    out = []
    if merged:
        shown = "; ".join(f"{', '.join(m.get('face') or [])} ({float(m.get('width_mm') or 0.0):.3g} mm wide) into "
                          f"{', '.join(m.get('into') or [])}" for m in merged[:6])
        more = f" and {len(merged) - 6} more" if len(merged) > 6 else ""
        out.append(f"Tiny faces: {len(merged)} CAD face(s) far narrower than the elements there were meshed as part "
                   f"of the face beside them ({shown}{more}). Each one's area is in that face's named group; the "
                   "CAD file is unchanged.")
        folded = sum(1 for m in merged if m.get("collapsed"))
        if folded:
            out.append(f"{folded} of them met that face at a sharp edge: as Ansys defeatures, each was folded onto the "
                       "edge (the shape there moves by less than its width).")
    if short:
        out.append(f"{short} short CAD edge(s) were meshed through, as part of the edge beside them.")
    return " ".join(out)


__all__ = ["CREASE_DEG", "RULE", "TINY_SHARE", "active", "apply", "cfd_floor", "cfd_plan", "crease_deg", "curve_midsides", "fold_in",
           "local_floor", "members", "outward_signs", "physical_groups", "plan_merges", "record", "runs_on", "words",
           "settle_nodes"]
