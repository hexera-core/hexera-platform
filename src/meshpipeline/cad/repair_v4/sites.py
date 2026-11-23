# Responsibility: Turn a failing verdict into failure SITES - the unit the v4 search works on - and
#                 into the observation the proposer reads.
# Owns: `find_sites`, `observation`, the clustering rule, the kind precedence and the size cap.
# Boundaries: no OCCT here; geometry comes from the `sites_*` kernel functions (sites_kernel.py)
#             through a WorkerPool. It reads a Verdict by field name and does not import the
#             verdict module. The picture is render.py; the search and the proposer are not here.
"""Failure sites: where the meshers (and the layer-1 checker) actually objected.

v3 worked on kernel-defect box clusters and forced attempts on untried defects, so the model fixed
fingerprints rather than the part. Here a site is a cluster of B-rep entities that real mesher
failures implicate, and a kernel warning belongs to a site only when it lies inside it. A warning
outside every site is never reported and never forced on anybody.

THE CLUSTERING RULE. Every entity a failing member (or a layer-1 hard finding) names, plus every
point a mesher reported that no entity could be found for, is a node. Two nodes join one site when
their bounding boxes are within `CLUSTER_FACTOR` x h of each other (chained), so the same failure
seen by gmsh and by Netgen is one site, and two failures on opposite sides of a part are two.
Entities and points are never merged by any other rule.

STABLE IDS. `Located.entity` is already a stable id (the panel member maps its mesher's tags to
faces). `Finding.entities` from layer 1 are KERNEL names ("F12" is kernel index 12 of the checked
shape), so they are translated through `ws.indices`; they are never read as stable ids.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import defaultdict
from typing import Any, cast

from meshpipeline.cad.repair_v4.types import Site, SiteKind, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import UnknownTarget, Workspace

#: Entities within this many element sizes of each other (and implicated by a failure) are one site.
CLUSTER_FACTOR = 2.0
#: Face pairs closer than this many element sizes are a proximity candidate.
PROXIMITY_FACTOR = 1.0
#: A warning is "inside" a site when its box centre is within the site's box grown by this x h.
SOFT_MARGIN_FACTOR = 1.0
MAX_NODES = 1500
MAX_SOFT_ENTITIES = 3000
MAX_PROXIMITY_PAIRS = 8
MAX_DISTANCE_FACES = 4
MAX_GRAPH_SEEDS = 12
GRAPH_HOPS = 2
CALL_TIMEOUT_S = 120.0
#: The observation goes to a model on a per-file dollar budget; this is a cost control. The hard
#: limit the tests pin is 60 kB; the cap sits under it so the final framing cannot cross it.
OBSERVATION_MAX_BYTES = 56_000

#: Most informative first. Used to name a merged site and to order sites.
_PRIORITY: tuple[SiteKind, ...] = (
    "intersection", "self_overlap", "open", "overlap", "invalid", "periodic", "pole", "sliver",
    "proximity", "timeout", "unlocated",
)
_MESHER_KINDS: dict[str, SiteKind] = {
    "overlapping_facets": "self_overlap",
    "edge_through_face": "intersection",
    "open_loop": "open",
    "no_volume": "open",
    "periodic_surface": "periodic",
    "pole": "pole",
    "timeout": "timeout",
}
_HARD_KINDS: dict[str, SiteKind] = {
    "open_shell": "open",
    "self_intersection": "self_overlap",
    "body_overlap": "overlap",
    "brep_invalid": "invalid",
    "non_manifold": "invalid",
    "non_positive_volume": "invalid",
    "no_solids": "invalid",
}
_KERNEL_PREFIX = {"F": "F", "E": "E", "S": "B"}
_TYPE_OF_PREFIX = {"F": "face", "E": "edge", "B": "solid"}
_NAME = re.compile(r"([A-Z])(\d+)")

Box = tuple[float, float, float, float, float, float]


def _number(stable_id: str) -> int:
    m = _NAME.fullmatch(stable_id)
    return int(m.group(2)) if m else 0


def _member_kind(member: Any) -> SiteKind:
    kind = _MESHER_KINDS.get(str(getattr(member, "error_kind", None) or ""))
    if kind is not None:
        return kind
    return "timeout" if getattr(member, "status", "") == "timeout" else "invalid"


class _Node:
    """One entity or one unresolved point, with who implicated it and why."""

    def __init__(self, entity: StableId | None, xyz: tuple | None) -> None:
        self.entity = entity
        self.xyz: list[tuple] = [xyz] if xyz is not None else []
        self.kinds: set[SiteKind] = set()
        self.sources: set[str] = set()
        self.box: Box | None = None
        self.proximity_dist: float | None = None


def _gap(a: Box, b: Box) -> float:
    total = 0.0
    for axis in range(3):
        d = max(a[axis] - b[axis + 3], b[axis] - a[axis + 3], 0.0)
        total += d * d
    return total**0.5


def _union_box(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), min(b[2] for b in boxes),
            max(b[3] for b in boxes), max(b[4] for b in boxes), max(b[5] for b in boxes))


def _call(pool: WorkerPool, fn: str, payload: dict) -> dict | None:
    """The kernel's answer, or None. A site is evidence the search may still use without the extra
    geometry, so a failed call degrades the site instead of losing it."""
    result = pool.call(fn, payload, timeout_s=CALL_TIMEOUT_S)
    if not result.ok or result.value is None:
        return None
    return result.value


def _live(ws: Workspace, stable_id: str | None) -> bool:
    return stable_id is not None and stable_id in ws.ids and stable_id in ws.indices


def _ref(ws: Workspace, stable_id: StableId) -> dict:
    return {"id": stable_id, "type": _TYPE_OF_PREFIX[stable_id[0]], "index": ws.indices[stable_id]}


def _reverse(ws: Workspace) -> dict[tuple[str, int], StableId]:
    return {(sid[0], idx): sid for sid, idx in ws.indices.items() if sid in ws.ids}


def _kernel_entities(ws: Workspace, names: list[str], reverse: dict) -> list[StableId]:
    """Layer-1 entity names (kernel index) -> live stable ids. Shells ("H") have none."""
    out: list[StableId] = []
    for name in names:
        m = _NAME.fullmatch(str(name))
        prefix = _KERNEL_PREFIX.get(m.group(1)) if m else None
        if m is None or prefix is None:
            continue
        sid = reverse.get((prefix, int(m.group(2))))
        if sid is not None:
            out.append(sid)
    return out


def find_sites(pool: WorkerPool, ws: Workspace, verdict: Any, *, h_mm: float) -> list[Site]:
    """The failure sites of `verdict`, best first. `h_mm` is the target element size.

    No failing member and no hard finding means no sites (and no kernel call). Soft findings never
    create a site; they attach only inside one.
    """
    panel = list(getattr(verdict, "panel", []) or [])
    valid = verdict.valid
    failing = [m for m in panel if getattr(m, "status", "pass") != "pass"]
    hard = [f for f in valid.hard if f.code in _HARD_KINDS]
    if not failing and not hard:
        return []
    reverse = _reverse(ws)

    nodes: dict[StableId, _Node] = {}
    points: list[_Node] = []
    whole_part: dict[SiteKind, set[str]] = defaultdict(set)

    def implicate(entity: StableId | None, xyz: tuple | None, kind: SiteKind, source: str) -> None:
        if entity is not None:
            node = nodes.setdefault(entity, _Node(entity, None))
            if xyz is not None and xyz not in node.xyz:
                node.xyz.append(xyz)
        else:
            node = _Node(None, xyz)
            points.append(node)
        node.kinds.add(kind)
        node.sources.add(source)

    for member in failing:
        kind = _member_kind(member)
        used = False
        for loc in getattr(member, "located", []) or []:
            xyz = tuple(float(v) for v in loc.xyz) if loc.xyz is not None else None
            if loc.entity is not None:
                if _live(ws, loc.entity):
                    implicate(loc.entity, xyz, kind, member.name)
                    used = True
            elif xyz is not None:
                implicate(None, xyz, kind, member.name)
                used = True
        if not used:
            whole_part["timeout" if kind == "timeout" else "unlocated"].add(member.name)
    for finding in hard:
        resolved = _kernel_entities(ws, list(finding.entities), reverse)
        for sid in resolved:
            implicate(sid, None, _HARD_KINDS[finding.code], "valid")
        if not resolved:
            whole_part["unlocated"].add("valid")

    sites: list[Site] = []
    if nodes or points:
        sites.extend(_geometric_sites(pool, ws, valid, nodes, points, reverse, h_mm))
    for kind, names in whole_part.items():
        sites.append(Site("", kind, [], [], sorted(names), None, {}, None))

    def order(site: Site) -> tuple:
        return (_PRIORITY.index(site.kind), -len(site.meshers_failing),
                min((_number(e) for e in site.entities), default=0), site.entities[:1])

    sites.sort(key=order)
    for n, site in enumerate(sites, start=1):
        site.id = f"S{n}"
    _attach_graphs(pool, ws, sites, reverse)
    return sites


def _geometric_sites(pool: WorkerPool, ws: Workspace, valid: Any, nodes: dict[StableId, _Node],
                     points: list[_Node], reverse: dict, h_mm: float) -> list[Site]:
    if len(nodes) + len(points) > MAX_NODES:
        keep = sorted(nodes, key=lambda s: (s[0], _number(s)))[: max(MAX_NODES - len(points), 0)]
        nodes = {s: nodes[s] for s in keep}
        points = points[:MAX_NODES]

    # A point no entity names: ask the kernel which faces it lies on, so the proposer gets ids.
    if points:
        answer = _call(pool, "sites_locate_points", {
            "brep_path": ws.brep_path, "points": [list(p.xyz[0]) for p in points], "radius": h_mm})
        faces = answer["faces"] if answer else []
        for node, indices in zip(points, faces, strict=False):
            for index in indices:
                sid = reverse.get(("F", int(index)))
                if sid is None:
                    continue
                target = nodes.setdefault(sid, _Node(sid, None))
                target.kinds |= node.kinds
                target.sources |= node.sources
                if node.xyz[0] not in target.xyz:
                    target.xyz.append(node.xyz[0])
                node.kinds = set()
        points = [p for p in points if p.kinds]

    prox = _call(pool, "sites_proximity", {
        "brep_path": ws.brep_path, "max_dist": PROXIMITY_FACTOR * h_mm,
        "max_pairs": MAX_PROXIMITY_PAIRS})
    for pair in (prox or {}).get("pairs", []):
        for key in ("a", "b"):
            sid = reverse.get(("F", int(pair[key])))
            if sid is None:
                continue
            node = nodes.setdefault(sid, _Node(sid, None))
            node.kinds.add("proximity")
            node.sources.add("proximity")
            d = float(pair["dist"])
            node.proximity_dist = d if node.proximity_dist is None else min(node.proximity_dist, d)

    geometry = _call(pool, "sites_geometry", {
        "brep_path": ws.brep_path, "entities": [_ref(ws, sid) for sid in sorted(nodes)]}) if nodes else None
    boxes = (geometry or {}).get("geometry", {})
    everyone = [*nodes.values(), *points]
    for node in everyone:
        if node.entity is not None and node.entity in boxes:
            node.box = cast(Box, tuple(float(v) for v in boxes[node.entity]["box"]))
        elif node.xyz:
            x, y, z = node.xyz[0]
            node.box = (x, y, z, x, y, z)

    parent = list(range(len(everyone)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    reach = CLUSTER_FACTOR * h_mm
    for i, a in enumerate(everyone):
        if a.box is None:
            continue
        for j in range(i + 1, len(everyone)):
            b = everyone[j]
            if b.box is not None and _gap(a.box, b.box) <= reach:
                parent[find(i)] = find(j)
    groups: dict[int, list[_Node]] = defaultdict(list)
    for i, node in enumerate(everyone):
        groups[find(i)].append(node)

    components = list(groups.values())
    real = [c for c in components if any(n.sources != {"proximity"} for n in c)]
    # A pair of close faces nobody's failure touches is not a site - unless nothing else located.
    components = real or components[:MAX_PROXIMITY_PAIRS]

    sites = [_site_of(c, boxes) for c in components]
    _attach_soft(pool, ws, valid, sites, reverse, h_mm)
    _attach_distances(pool, ws, sites, components, h_mm)
    return sites


def _site_of(component: list[_Node], boxes: dict) -> Site:
    entities = sorted((n.entity for n in component if n.entity is not None),
                      key=lambda s: (s[0], _number(s)))
    xyz: list[tuple] = []
    for node in component:
        for p in node.xyz:
            if p not in xyz:
                xyz.append(p)
    if not xyz:
        for sid in entities[:3]:
            c = boxes.get(sid, {}).get("centroid")
            if c:
                xyz.append(tuple(float(v) for v in c))
    kinds = {k for n in component for k in n.kinds}
    kind = next(k for k in _PRIORITY if k in kinds)
    meshers = sorted({s for n in component for s in n.sources if s != "proximity"})
    site = Site("", kind, entities, xyz[:20], meshers, None, {}, None)
    site_box = _union_box([n.box for n in component if n.box is not None])
    site.graph["box"] = list(site_box) if site_box else None
    return site


def _attach_soft(pool: WorkerPool, ws: Workspace, valid: Any, sites: list[Site], reverse: dict,
                 h_mm: float) -> None:
    """Attach layer-1 warnings that lie INSIDE a site's box. Nothing else ever reports one."""
    wanted: dict[StableId, list[Any]] = defaultdict(list)
    for finding in valid.soft:
        for sid in _kernel_entities(ws, list(finding.entities), reverse):
            wanted[sid].append(finding)
    boxed = [s for s in sites if s.graph.get("box")]
    soft_ids = sorted(wanted)[:MAX_SOFT_ENTITIES]
    if boxed and soft_ids:
        answer = _call(pool, "sites_geometry", {
            "brep_path": ws.brep_path, "entities": [_ref(ws, sid) for sid in soft_ids]})
        where = (answer or {}).get("geometry", {})
        margin = SOFT_MARGIN_FACTOR * h_mm
        for site in boxed:
            b = site.graph["box"]
            found = []
            for sid in soft_ids:
                c = where.get(sid, {}).get("centroid")
                if c and all(b[a] - margin <= c[a] <= b[a + 3] + margin for a in range(3)):
                    for finding in wanted[sid]:
                        found.append({"code": finding.code, "entity": sid, "detail": dict(finding.detail)})
            site.graph["soft"] = found
            if site.kind == "invalid" and any(
                    f["code"] in ("small_face", "small_edge") and f["entity"] in site.entities for f in found):
                site.kind = "sliver"
    for site in sites:
        site.graph.setdefault("soft", [])


def _attach_distances(pool: WorkerPool, ws: Workspace, sites: list[Site], components: list[list[_Node]],
                      h_mm: float) -> None:
    requests: list[tuple[Site, dict]] = []
    for site, component in zip(sites, components, strict=True):
        faces = [e for e in site.entities if e[0] == "F"][:MAX_DISTANCE_FACES]
        pairs = [(a, b) for i, a in enumerate(faces) for b in faces[i + 1:]]
        for a, b in pairs:
            requests.append((site, {"a": _ref(ws, a), "b": _ref(ws, b)}))
        prox = [n.proximity_dist for n in component if n.proximity_dist is not None]
        if prox:
            site.dist_over_h = round(min(prox) / h_mm, 4)
    if not requests:
        return
    answer = _call(pool, "sites_distance", {
        "brep_path": ws.brep_path, "pairs": [r for _, r in requests]})
    for (site, _), item in zip(requests, (answer or {}).get("distances", []), strict=False):
        if item.get("dist") is None or item.get("adjacent"):
            continue  # a shared edge is distance 0 for a reason that says nothing about the gap
        d = round(float(item["dist"]) / h_mm, 4)
        site.dist_over_h = d if site.dist_over_h is None else min(site.dist_over_h, d)


def _attach_graphs(pool: WorkerPool, ws: Workspace, sites: list[Site], reverse: dict) -> None:
    """The k = 2 neighbourhood of each site, from the kernel, with kernel indices named as stable ids."""
    asks = []
    for site in sites:
        seeds = [_ref(ws, e) for e in site.entities if e[0] in "FE"][:MAX_GRAPH_SEEDS]
        if seeds:
            asks.append({"id": site.id, "seeds": seeds})
    answer = _call(pool, "sites_graph", {
        "brep_path": ws.brep_path, "k": GRAPH_HOPS, "sites": asks}) if asks else None
    graphs = (answer or {}).get("graphs", {})
    for site in sites:
        soft, box = site.graph.get("soft", []), site.graph.get("box")
        graph: dict = {"k": GRAPH_HOPS}
        raw = graphs.get(site.id)
        if raw is not None:
            def name(prefix: str, value: Any) -> Any:
                return reverse.get((prefix, int(value)), f"{prefix}#{value}")
            graph["nodes"] = [{**n, "id": name("F", n["id"][1:])} if isinstance(n.get("id"), str) else n
                              for n in raw["nodes"]]
            graph["edges"] = [
                {**e, "faces": [name("F", str(f)[1:]) for f in e["faces"]], "edge": name("E", str(e["edge"])[1:])}
                for e in raw["edges"]]
            graph["truncated"] = bool(raw.get("truncated"))
        elif asks and any(a["id"] == site.id for a in asks):
            graph["error"] = "the neighbourhood could not be measured"
        elif not site.entities:
            graph["note"] = "no entity could be located for this failure"
        graph["soft"] = soft
        graph["box"] = box
        site.graph = graph


# --- the observation ----------------------------------------------------------------------------

#: (sites, entities, graph nodes, graph edges, soft, xyz) kept per site, loosest first.
_LEVELS = (
    (8, 24, 20, 30, 10, 4),
    (8, 16, 12, 16, 6, 3),
    (6, 12, 8, 8, 4, 2),
    (6, 8, 4, 4, 2, 2),
    (4, 6, 0, 0, 0, 1),
    (3, 4, 0, 0, 0, 1),
    (2, 3, 0, 0, 0, 1),
    (1, 2, 0, 0, 0, 1),
)


def _round(value: Any, digits: int = 3) -> Any:
    if isinstance(value, float):
        return float(f"{value:.{digits}g}")
    if isinstance(value, dict):
        return {k: _round(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v, digits) for v in value]
    return value


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.loads(json.dumps(value, default=str))


def _site_entry(ws: Workspace, site: Site, level: tuple) -> dict:
    _, n_ent, n_nodes, n_edges, n_soft, n_xyz = level
    graph = site.graph or {}
    entry: dict = {
        "id": site.id, "kind": site.kind,
        "entities": site.entities[:n_ent],
        "meshers_failing": site.meshers_failing,
        "dist_over_h": site.dist_over_h,
        "xyz": [[round(float(c), 3) for c in p] for p in site.xyz[:n_xyz]],
        "labels": {e: ws.labels[e]["name"] for e in site.entities[:n_ent]
                   if isinstance(ws.labels.get(e), dict) and ws.labels[e].get("name")},
    }
    if len(site.entities) > n_ent:
        entry["entities_omitted"] = len(site.entities) - n_ent
    sub: dict = {k: graph[k] for k in ("k", "error", "note", "truncated") if k in graph}
    if n_nodes:
        sub["nodes"] = _round(list(graph.get("nodes", []))[:n_nodes])
        sub["edges"] = _round(list(graph.get("edges", []))[:n_edges])
    else:
        sub["node_count"] = len(graph.get("nodes", []))
    if graph.get("box"):
        sub["box"] = _round(graph["box"])
    sub["soft"] = _round(list(graph.get("soft", []))[:n_soft])
    if len(graph.get("soft", [])) > n_soft:
        sub["soft_omitted"] = len(graph["soft"]) - n_soft
    entry["graph"] = sub
    return entry


def observation(ws: Workspace, verdict: Any, sites: list[Site], budget: Any) -> dict:
    """What the proposer is told: the part's state, what each mesher said, and each failure site.

    JSON-serialisable and no larger than `OBSERVATION_MAX_BYTES`: it is read by a model with a
    per-file dollar budget. When the full picture does not fit it is thinned in a fixed order -
    graph detail first, then entities, then whole sites - and the omissions are counted, never
    silent. Pixels (`view_png`) are not part of it. A site naming an entity that is no longer live
    in `ws` is dropped and listed under `stale_sites`.
    """
    live: list[Site] = []
    stale: list[str] = []
    for site in sites:
        try:
            ws.resolve(site.entities)
            live.append(site)
        except UnknownTarget:
            stale.append(site.id)
    valid = verdict.valid
    base: dict = {
        "valid": {"ok": bool(valid.ok), "units": valid.units, "solids": valid.solids,
                  "round_trip_ok": bool(valid.round_trip_ok),
                  "hard": [{"code": f.code, "entities": list(f.entities)[:10]} for f in valid.hard[:12]]},
        "panel": [{"name": m.name, "status": m.status, "error_kind": m.error_kind,
                   "message": str(m.message or "")[:300], "min_sicn": _round(m.min_sicn),
                   "elements": m.elements, "located": len(m.located or [])}
                  for m in getattr(verdict, "panel", [])],
        "tier": getattr(verdict, "tier", None),
        "clean": getattr(verdict, "clean", None),
        "faithful": _plain(getattr(verdict, "faithful", None)),
        "budget": _plain(budget) if isinstance(budget, dict) or dataclasses.is_dataclass(budget)
        else {"value": budget},
        "stale_sites": stale,
    }

    def build(level: tuple) -> dict:
        kept = live[: level[0]]
        out = dict(base)
        out["sites"] = [_site_entry(ws, s, level) for s in kept]
        out["sites_omitted"] = len(live) - len(kept)
        return out

    out = build(_LEVELS[-1])
    for level in _LEVELS:
        out = build(level)
        if len(json.dumps(out).encode()) <= OBSERVATION_MAX_BYTES:
            return out
    return out
