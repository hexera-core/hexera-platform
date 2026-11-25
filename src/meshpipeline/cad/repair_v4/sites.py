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

STABLE IDS. `Located.entity` from a REAL panel member is the face's FINGERPRINT (`mesh_child._located`:
`identity.fingerprint_of` over the gmsh surface's box), not a stable id; it is translated through `ws.ids`
(stable id -> fingerprint) to every live face that carries it, and a fingerprint no live face carries is no location.
A stable id is also accepted as-is (tests and any member that already maps). `Finding.entities` from layer 1 are KERNEL names ("F12" is kernel index 12 of the checked
shape), so they are translated through `ws.indices`; they are never read as stable ids.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
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
MAX_CLUSTER_PAIRS = 6000
CLUSTER_CHUNK = 500
MESSAGE_CHARS = 200
MAX_NOTES = 6
MAX_CLASH_PAIRS = 6
MAX_SOFT_ENTITIES = 3000
MAX_PROXIMITY_PAIRS = 8  # standalone proximity sites, when nothing else was located
PROXIMITY_FETCH = 200  # pairs asked of the kernel; attachment, not this, limits what a site keeps
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
    "proximity", "unbounded", "timeout", "unlocated",
)
_MESHER_KINDS: dict[str, SiteKind] = {
    "overlapping_facets": "self_overlap",
    "edge_through_face": "intersection",
    "open_loop": "open",
    "no_volume": "open",
    "periodic_surface": "periodic",
    # a mesh that exists but is too poor: thin or near-touching geometry is the usual cause (lane C5)
    "low_quality": "proximity",
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
    # an infinite surface (`valid.UNBOUNDED`): `trim_unbounded` bounds it to the part's real extent (lane C7)
    "unbounded_geometry": "unbounded",
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
        #: who said what about this node: {mesher, error_kind, message, tag, xyz, entity}
        self.notes: list[dict] = []


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


#: a box coordinate within this share of a cell of a half-cell is rounded both ways when matching a fingerprint
_HALF_CELL_SLACK = 0.01
_MAX_AMBIGUOUS_COORDS = 6  # 2^6 variants per face at most


def _near_fingerprints(pool: WorkerPool, ws: Workspace) -> dict[str, list[StableId]]:
    """fingerprint -> live faces, including every fingerprint a face's box gives when a coordinate lying on a half-cell
    boundary is rounded the other way. gmsh's surface box and the kernel's agree to ~1e-12 mm, but `round()` at x.5
    cells goes either way on that noise (measured: 2 of 8 faces of a filleted box), so exact equality drops real
    locations. Only boundary coordinates are varied: a fingerprint is never matched to a face whose box is a cell
    or more away."""
    import itertools

    from meshpipeline.cad.repair.identity import MIN_QUANTUM_MM, fingerprint_of

    faces = [sid for sid in ws.ids if sid[:1] == "F" and _live(ws, sid)]
    answer = _call(pool, "sites_geometry", {"brep_path": ws.brep_path, "entities": [_ref(ws, f) for f in faces]})
    geometry = (answer or {}).get("geometry", {})
    q = ws.quantum if ws.quantum > 0 else MIN_QUANTUM_MM
    out: dict[str, list[StableId]] = defaultdict(list)
    for sid in faces:
        box = (geometry.get(sid) or {}).get("box")
        if not box or len(box) != 6:
            continue
        choices: list[list[float]] = []
        for v in box:
            frac = (float(v) / q) % 1.0
            if abs(frac - 0.5) <= _HALF_CELL_SLACK and sum(len(c) > 1 for c in choices) < _MAX_AMBIGUOUS_COORDS:
                cell = math.floor(float(v) / q)
                choices.append([cell * q, (cell + 1) * q])  # values that round to each neighbouring cell
            else:
                choices.append([float(v)])
        for combo in itertools.product(*choices):
            fp = fingerprint_of("face", {"bbox_min": list(combo[:3]), "bbox_max": list(combo[3:])}, quantum=q)
            if fp and sid not in out[fp]:
                out[fp].append(sid)
    return out


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


class LineageMismatch(ValueError):
    """A `SiteRegistry` was handed a workspace that is not the continuation of the one it follows."""


#: A new site inherits an old id only if the entities they share are at least this fraction of BOTH.
#: One shared big plane is not an identity: a different defect on the same face is a new site.
MIN_OVERLAP_FRACTION = 0.5


def _natural(site_id: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", site_id))


class SiteRegistry:
    """Carries site ids across rounds of one search, so a proposer that discussed "S@F12" still
    finds that geometry under that name after an op has rebuilt it.

    An id anchored on an entity is only stable while the entity lives, and ops retire entities
    exactly when they make progress (sew re-mints edges, a split replaces a face). So the registry
    remembers each site's failing entities, carries them forward through the workspace's own op
    history (`Diff.modified` old -> successors; `removed` dropped), and matches new sites to old ones.

    MATCHING. A new site inherits an old id only when the shared entities are >=
    `MIN_OVERLAP_FRACTION` of both sets (greedy, largest overlap first, ties by natural id order, one
    to one). A rename is never silent: each site's graph carries `was` - the old ids it descends
    from but does not carry. On a MERGE the winner keeps the larger predecessor's id and `was` lists
    the others; on a SPLIT the larger part keeps the id and the splinter, which gets a fresh id,
    has `was` naming the original; a new defect that merely shares a face with an old site gets a
    fresh id with `was` naming it, so it is never read as "still unfixed". `id_carried` is set when
    the carried id differs from the fresh anchor id. Old ids that match nothing are retired: they
    are listed in `retired` after `apply` and their ids are never issued again (a fresh site that
    would have taken one gets `-2`, `-3`).

    LINEAGE. One registry follows ONE linear lineage. `apply` refuses (LineageMismatch) a workspace
    whose history does not begin with the diffs this registry has already seen, or whose original
    differs: a fork, a sibling branch or a rollback would otherwise forward entities through the wrong ops.
    """

    def __init__(self) -> None:
        self._previous: dict[str, set[StableId]] = {}
        self._diffs: list[Any] = []
        self._original: str | None = None
        self.retired: list[str] = []
        self._issued: set[str] = set()  # every id ever shown: a retired id is never reused

    def _check_lineage(self, ws: Workspace) -> list[Any]:
        if self._original is not None and ws.original != self._original:
            raise LineageMismatch(f"registry follows {self._original!r}, workspace is {ws.original!r}")
        seen = len(self._diffs)
        if ws.history[:seen] != self._diffs:
            raise LineageMismatch(
                "the workspace history does not continue the one this registry has followed "
                "(a fork, a sibling branch or a rollback): use one registry per linear lineage")
        return list(ws.history[seen:])

    def _forward(self, history: list[Any]) -> dict[str, set[StableId]]:
        moved: dict[str, set[StableId]] = {}
        for site_id, entities in self._previous.items():
            now = set(entities)
            for diff in history:
                step: set[StableId] = set()
                for e in now:
                    if e in diff.modified:
                        step.update(diff.modified[e])
                    elif e not in diff.removed:
                        step.add(e)
                now = step
            moved[site_id] = now
        return moved

    def apply(self, ws: Workspace, sites: list[Site]) -> None:
        carried = self._forward(self._check_lineage(ws))
        mine = [set(site.graph.get("failure_entities") or site.entities) for site in sites]
        scored = []
        predecessors: dict[int, list[str]] = defaultdict(list)
        for n, site in enumerate(sites):
            for old_id, old in carried.items():
                shared = len(mine[n] & old)
                if shared:
                    predecessors[n].append(old_id)
                    if (shared / len(mine[n]) >= MIN_OVERLAP_FRACTION
                            and shared / len(old) >= MIN_OVERLAP_FRACTION):
                        # fully deterministic: no memory addresses, so two equal halves of a split
                        # always pick the same holder, run after run
                        scored.append((-shared, _natural(old_id), old_id,
                                       tuple(sorted(mine[n], key=_natural)), site.id, n))
        scored.sort()
        done: dict[int, str] = {}
        taken: set[str] = set()
        for _, _, old_id, _, _, n in scored:
            if n in done or old_id in taken:
                continue
            done[n] = old_id
            taken.add(old_id)
        for n, site in enumerate(sites):
            inherited = done.get(n)
            fresh = site.id
            if inherited is not None:
                if fresh != inherited:
                    site.graph["id_carried"] = True
                site.id = inherited
            predecessors_of = sorted((p for p in predecessors[n] if p != inherited), key=_natural)
            if predecessors_of:
                site.graph["was"] = predecessors_of
        for n, site in enumerate(sites):  # a site that carried nothing must not take a carried id
            if n in done:
                continue
            base, k = site.id, 1
            # An entity-less site (a point, a whole-part failure) has no entity to be renamed
            # through and its id is derived from what it IS, so it keeps that id every round; only
            # an entity-anchored id can mean a DIFFERENT site and must not be reissued.
            reissue_guard = bool(site.entities)
            while site.id in taken or (reissue_guard and site.id in self._issued):
                k += 1
                site.id = f"{base}-{k}"
            taken.add(site.id)
        self.retired = sorted((o for o in carried if o not in taken), key=_natural)
        self._previous = {site.id: mine[n] for n, site in enumerate(sites) if site.entities}
        self._issued |= {site.id for site in sites if site.entities}
        self._diffs = list(ws.history)
        self._original = ws.original


def find_sites(pool: WorkerPool, ws: Workspace, verdict: Any, *, h_mm: float,
               registry: SiteRegistry | None = None) -> list[Site]:
    """The failure sites of `verdict`, best first. `h_mm` is the target element size.

    With a `registry`, ids are carried across rounds (see `SiteRegistry`); without one they are
    anchored on the lowest failing entity, which is stable only while that entity lives.

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
    unplaced: dict[str, SiteKind] = {}  # member -> its kind, for the members that said nothing about where
    whole_codes: dict[SiteKind, set[str]] = defaultdict(set)  # which layer-1 codes put a site on the whole part

    def implicate(entity: StableId | None, xyz: tuple | None, kind: SiteKind, source: str,
                  note: dict) -> None:
        if entity is not None:
            node = nodes.setdefault(entity, _Node(entity, None))
            if xyz is not None and xyz not in node.xyz:
                node.xyz.append(xyz)
        else:
            node = _Node(None, xyz)
            points.append(node)
        node.kinds.add(kind)
        node.sources.add(source)
        node.notes.append({**note, "entity": entity, "xyz": xyz})

    by_fingerprint: dict[str, list[StableId]] = defaultdict(list)
    for sid, fp in ws.ids.items():
        if sid[:1] == "F" and _live(ws, sid):
            by_fingerprint[fp].append(sid)
    near: dict[str, list[StableId]] | None = None  # built on the first fingerprint that misses: one kernel call

    def faces_named(entity: str | None) -> list[StableId]:
        nonlocal near
        if entity is None:
            return []
        if _live(ws, entity):
            return [entity]
        if entity in by_fingerprint:
            return by_fingerprint[entity]
        if not entity.startswith("face:"):
            return []
        if near is None:
            near = _near_fingerprints(pool, ws)
        return near.get(entity, [])

    def on_our_grid(loc: Any) -> str | None:
        """The located face's box named on THIS workspace's grid (`ws.quantum`): the member fingerprinted it on its own
        grid, which is 2x off when the two diagonals straddle a power of two (lane D17)."""
        box = getattr(loc, "box", None)
        if not box or len(box) != 6 or ws.quantum <= 0:
            return None
        from meshpipeline.cad.repair.identity import fingerprint_of

        return fingerprint_of("face", {"bbox_min": list(box[:3]), "bbox_max": list(box[3:])}, quantum=ws.quantum) or None

    for member in failing:
        kind = _member_kind(member)
        used = False
        for loc in getattr(member, "located", []) or []:
            xyz = tuple(float(v) for v in loc.xyz) if loc.xyz is not None else None
            note = {"mesher": member.name, "error_kind": getattr(member, "error_kind", None),
                    "message": str(getattr(member, "message", "") or "")[:MESSAGE_CHARS],
                    "tag": str(getattr(loc, "mesher_tag", "") or "")}
            named = faces_named(on_our_grid(loc)) or faces_named(loc.entity)
            if named:
                for sid in named:  # two faces sharing one box share its fingerprint: both are implicated
                    implicate(sid, xyz, kind, member.name, note)
                used = True
            elif xyz is not None:
                # no entity, or one that is not live (a stale or foreign id): the point still
                # says where, and the kernel resolves it to live faces
                implicate(None, xyz, kind, member.name, note)
                used = True
        if not used:
            if kind == "timeout":
                whole_part["timeout"].add(member.name)
            else:
                unplaced[member.name] = kind
    for finding in hard:
        resolved = _kernel_entities(ws, list(finding.entities), reverse)
        for sid in resolved:
            implicate(sid, None, _HARD_KINDS[finding.code], "valid",
                      {"mesher": "valid", "error_kind": finding.code, "message": "", "tag": finding.code})
        if not resolved:
            # The finding NAMES the defect even when it cannot place it (an open shell is reported
            # against a shell "H1", which has no stable id; `no_solids` names nothing). Filing it as
            # "unlocated" threw the kind away, and `DetGenerator` sweeps nothing for "unlocated", so
            # the canonical open shell produced no candidate at all. Keep the kind, drop only the place.
            whole_part[_HARD_KINDS[finding.code]].add("valid")
            whole_codes[_HARD_KINDS[finding.code]].add(finding.code)

    sites: list[Site] = []
    if nodes or points:
        sites.extend(_geometric_sites(pool, ws, valid, nodes, points, reverse, h_mm))
    for kind, names in whole_part.items():
        sites.append(Site("", kind, [], [], sorted(names), None, {"hard_codes": sorted(whole_codes.get(kind, ()))}, None))
    if unplaced:
        sites.append(_coarse_site(ws, valid, unplaced, reverse))
    for site in sites:
        if site.entities and not any(e[:1] in "FE" for e in site.entities) and not site.graph.get("coarse"):
            # a layer-1 finding that names only a BODY (self_intersection on S6): no face to aim at, so it is coarse
            site.graph["coarse"] = {"reason": "the finding names the body, not a face", "members": ["valid"],
                                    "whole_part": False}  # the bodies ARE the evidence (body_overlap names them)

    def order(site: Site) -> tuple:
        return (_PRIORITY.index(site.kind), -len(site.meshers_failing),
                min((_number(e) for e in site.entities), default=0), site.entities[:1])

    sites.sort(key=order)
    _assign_ids(sites)
    _attach_graphs(pool, ws, sites, reverse, h_mm)
    _attach_free_loops(pool, ws, sites, reverse)
    if registry is not None:
        registry.apply(ws, sites)
    return sites


#: soft findings a coarse site carries at most (they are the part-wide sweep's only precise targets)
MAX_COARSE_SOFT = 40


def _coarse_site(ws: Workspace, valid: Any, unplaced: dict[str, SiteKind], reverse: dict) -> Site:
    """One site for the members that failed WITHOUT saying where: the whole part, as its bodies (or, with no body, as
    nothing), with the reason, the most informative of those members' kinds, and the part's own small-entity findings.
    It replaces the `unlocated` site, for which the deterministic sweep had nothing at all (lane C5: 8 of 13 conclusive
    smoke failures). Coarse means the sweeps it gets are part-wide; it never pretends to know a face."""
    kinds = set(unplaced.values())
    kind: SiteKind = next((k for k in _PRIORITY if k in kinds and k not in ("unlocated", "timeout")), "invalid")
    bodies = sorted((sid for sid in ws.ids if sid[:1] == "B" and _live(ws, sid)), key=_number)
    soft: list[dict] = []
    for finding in getattr(valid, "soft", []) or []:
        if finding.code in ("small_face", "small_edge"):
            for sid in _kernel_entities(ws, list(finding.entities), reverse):
                if len(soft) < MAX_COARSE_SOFT:
                    soft.append({"code": finding.code, "entity": sid, "detail": dict(finding.detail)})
    members = sorted(unplaced)
    reason = (f"{', '.join(members)} failed without saying where ({', '.join(sorted(set(unplaced.values())))}); "
              "the site is the whole part")
    return Site("", kind, bodies, [], members, None,
                {"hard_codes": [], "soft": soft,
                 # whole_part: nothing placed it, so the bodies listed are every body, not evidence about any (lane C10)
                 "coarse": {"reason": reason, "members": members, "whole_part": True}}, None)


def _assign_ids(sites: list[Site]) -> None:
    """Ids from WHAT the site is, not where it sits in this round's ordering: a proposer that
    discussed "S@F12" last round must find the same geometry under that name now. The anchor is the
    lowest entity (stable ids are never reused), a whole-part site is named by its kind, a point-only
    site by a hash of its first point. Collisions get -2, -3 in sort order."""
    taken: dict[str, int] = {}
    for site in sites:
        failing = site.graph.get("failure_entities") or site.entities
        if site.graph.get("coarse") and not site.graph.get("failure_entities"):
            base = f"S@coarse-{site.kind}"
            taken[base] = taken.get(base, 0) + 1
            site.id = base if taken[base] == 1 else f"{base}-{taken[base]}"
            continue
        if failing:
            anchor = min(failing, key=lambda e: (e[0], _number(e)))
        elif site.xyz:
            anchor = "p" + hashlib.sha1(repr([round(c, 3) for c in site.xyz[0]]).encode()).hexdigest()[:6]
        else:
            anchor = site.kind
        base = f"S@{anchor}"
        taken[base] = taken.get(base, 0) + 1
        site.id = base if taken[base] == 1 else f"{base}-{taken[base]}"


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
        dists = (answer or {}).get("dists") or [[] for _ in faces]
        for node, indices, ds in zip(points, faces, dists, strict=False):
            # lane D28: how far the mesher's point lies from the nearest face (None: no face within h) and which
            # faces it lies on, kept on the point's notes so the site's `points` row carries the measurement
            for n in node.notes:
                n["nearest_face_mm"] = round(ds[0], 6) if ds else None
                n["faces_at"] = [f"F{int(i)}" for i in indices]
            for index in indices:
                sid = reverse.get(("F", int(index)))
                if sid is None:
                    continue
                target = nodes.setdefault(sid, _Node(sid, None))
                target.kinds |= node.kinds
                target.sources |= node.sources
                target.notes.extend({**n, "entity": sid} for n in node.notes)
                if node.xyz[0] not in target.xyz:
                    target.xyz.append(node.xyz[0])
                node.kinds = set()
        points = [p for p in points if p.kinds]

    # Close face pairs are CONTEXT for a failure site, never a failure: they are attached after the
    # failure nodes have been clustered, and cannot chain two failures into one site.
    prox = _call(pool, "sites_proximity", {
        "brep_path": ws.brep_path, "max_dist": PROXIMITY_FACTOR * h_mm,
        "max_pairs": PROXIMITY_FETCH})
    prox_pairs = []
    for pair in (prox or {}).get("pairs", []):
        a, b = reverse.get(("F", int(pair["a"]))), reverse.get(("F", int(pair["b"])))
        if a is not None and b is not None:
            prox_pairs.append((a, b, pair))

    want = set(nodes) | {x for a, b, _ in prox_pairs for x in (a, b)}
    geometry = _call(pool, "sites_geometry", {
        "brep_path": ws.brep_path, "entities": [_ref(ws, sid) for sid in sorted(want)]}) if want else None
    boxes = (geometry or {}).get("geometry", {})

    def boxed(node: _Node) -> None:
        if node.entity is not None and node.entity in boxes:
            node.box = cast(Box, tuple(float(v) for v in boxes[node.entity]["box"]))
        elif node.xyz:
            x, y, z = node.xyz[0]
            node.box = (x, y, z, x, y, z)

    everyone = [*nodes.values(), *points]
    for node in everyone:
        boxed(node)

    parent = list(range(len(everyone)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Candidates by box gap (cheap), then CONFIRMED by exact distance when both are entities: a big
    # plane has a huge box and would otherwise chain every failure near it into one mega-site.
    reach = CLUSTER_FACTOR * h_mm
    candidates: list[tuple[int, int]] = []
    for i, a in enumerate(everyone):
        if a.box is None:
            continue
        for j in range(i + 1, len(everyone)):
            b = everyone[j]
            if b.box is not None and _gap(a.box, b.box) <= reach:
                candidates.append((i, j))
    # Nearest first, so a cap or a deadline loses the FAR pairs, not whichever have low indices.
    def centre(node: _Node) -> tuple[float, float, float]:
        box = cast(Box, node.box)
        return ((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2)

    def rank(pair: tuple[int, int]) -> float:
        # Centre distance: small features close together come first, and a huge plane's box (the
        # thing that makes box gap a poor guide) puts its centre far from most of its neighbours.
        i, j = pair
        return math.dist(centre(everyone[i]), centre(everyone[j]))

    candidates.sort(key=lambda pair: (rank(pair), pair))
    # FAIL CLOSED. An entity pair merges only if the kernel MEASURED it within reach. A pair beyond
    # the cap, in a chunk that failed or timed out, or whose distance came back None is NOT merged:
    # under-merging costs a few extra small sites, one wrong merge in a dense region rebuilds v3's
    # 314-fingerprint region. Such pairs are counted per site as `unmeasured_pairs`.
    measured: dict[tuple[int, int], float] = {}
    entity_pairs = [(i, j) for i, j in candidates
                    if everyone[i].entity is not None and everyone[j].entity is not None
                    and everyone[i].entity != everyone[j].entity]
    for start in range(0, min(len(entity_pairs), MAX_CLUSTER_PAIRS), CLUSTER_CHUNK):
        chunk = entity_pairs[start:min(start + CLUSTER_CHUNK, MAX_CLUSTER_PAIRS)]
        answer = _call(pool, "sites_distance", {"brep_path": ws.brep_path, "pairs": [
            {"a": _ref(ws, cast(str, everyone[i].entity)), "b": _ref(ws, cast(str, everyone[j].entity))}
            for i, j in chunk]})
        if answer is None:
            break  # the kernel is not answering: do not keep asking
        for pair, item in zip(chunk, answer.get("distances", []), strict=False):
            if item.get("dist") is not None:
                measured[pair] = float(item["dist"])
    entity_set = set(entity_pairs)
    unresolved: list[tuple[int, int]] = []
    for i, j in candidates:
        if (i, j) in entity_set:
            if (i, j) not in measured:
                unresolved.append((i, j))
            elif measured[(i, j)] <= reach:
                parent[find(i)] = find(j)
        elif everyone[i].entity is None and everyone[j].entity is None:
            # two mesher points: their distance IS the box gap (a box of zero size)
            parent[find(i)] = find(j)
        else:
            # A point against a face: the point had no face within h, and its distance to a big
            # face's box proves nothing about the face. Not merged; counted.
            unresolved.append((i, j))
    unmeasured_nodes: dict[int, int] = defaultdict(int)
    for i, j in unresolved:
        if find(i) != find(j):  # a pair merged by another path is no longer in doubt
            unmeasured_nodes[find(i)] += 1
            unmeasured_nodes[find(j)] += 1
    groups: dict[int, list[_Node]] = defaultdict(list)
    for i, node in enumerate(everyone):
        groups[find(i)].append(node)
    roots = list(groups)
    components = [groups[r] for r in roots]
    clashes: list[list[dict]] = [[] for _ in components]
    unmeasured = [0] * len(components)
    for k, root in enumerate(roots):
        unmeasured[k] = unmeasured_nodes.get(root, 0)

    owner = {n.entity: k for k, c in enumerate(components) for n in c if n.entity is not None}
    context: dict[StableId, _Node] = {}
    standalone: list[list[_Node]] = []
    for a, b, pair in prox_pairs:
        # CARRY what the kernel measured. These three decide which ops a clash gets (`search._clash_pairs`,
        # `_contact_rows`): rebuilding the entry without them made every pair read as a near miss, so no contact row
        # could be proposed in production however many contacts the kernel found (lane C12 found this on the tip).
        entry = {"a": a, "b": b, "dist_mm": round(float(pair["dist"]), 4),
                 "point_a": pair.get("point_a"), "point_b": pair.get("point_b"),
                 "touching": bool(pair.get("touching")),
                 "area_a_mm2": pair.get("area_a_mm2"), "area_b_mm2": pair.get("area_b_mm2")}
        home = owner.get(a, owner.get(b))
        if home is not None:
            if len(clashes[home]) >= MAX_CLASH_PAIRS:
                continue  # the site already holds its nearest pairs (the kernel list is nearest first)
            for x in (a, b):
                if x in owner and x not in context:
                    node = nodes[x]  # a failing face: the pair is context for it
                else:
                    node = context.setdefault(x, _Node(x, None))
                    if x not in owner:
                        node.kinds.add("proximity")
                        node.sources.add("proximity")
                        boxed(node)
                        components[home].append(node)
                        owner[x] = home
                node.proximity_dist = (entry["dist_mm"] if node.proximity_dist is None
                                       else min(node.proximity_dist, entry["dist_mm"]))
            clashes[home].append(entry)
        elif not everyone and len(standalone) < MAX_PROXIMITY_PAIRS:
            # nothing was located anywhere: close faces are the best lead there is
            pair_nodes = []
            for x in (a, b):
                node = _Node(x, None)
                node.kinds.add("proximity")
                node.sources.add("proximity")
                node.proximity_dist = entry["dist_mm"]
                boxed(node)
                pair_nodes.append(node)
            standalone.append(pair_nodes)
            clashes.append([entry])
            unmeasured.append(0)
    components.extend(standalone)

    sites = [_site_of(c, boxes) for c in components]
    for site, count in zip(sites, unmeasured, strict=True):
        site.graph["unmeasured_pairs"] = count
    _attach_soft(pool, ws, valid, sites, reverse, h_mm)
    _attach_distances(pool, ws, sites, components, clashes, h_mm)
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
    # a component of proximity-only nodes carries no failure kind (lane D21: StopIteration killed find_sites on 5 of
    # the oracle's first 24 dev files; the search caught it and expanded BLIND, losing every site of the state)
    kind: SiteKind = next((k for k in _PRIORITY if k in kinds), "proximity")
    meshers = sorted({s for n in component for s in n.sources if s != "proximity"})
    site = Site("", kind, entities, xyz[:20], meshers, None, {}, None)
    seen_notes: set[tuple] = set()
    failures: list[dict] = []
    located: list[dict] = []
    for node in component:
        for note in node.notes:
            if note["mesher"] == "proximity":
                continue
            key = (note["mesher"], note["error_kind"], note["tag"], note["message"])
            if key not in seen_notes and len(failures) < MAX_NOTES:
                seen_notes.add(key)
                failures.append({k: note[k] for k in ("mesher", "error_kind", "tag", "message")})
            if note["xyz"] is not None and len(located) < MAX_NOTES:
                located.append({"xyz": [round(c, 3) for c in note["xyz"]], "entity": note["entity"],
                                "mesher": note["mesher"], "tag": note["tag"],
                                **{k: note[k] for k in ("nearest_face_mm", "faces_at") if k in note}})
    # The failure-implicated entities only: proximity context faces come and go with h and must
    # not move a site's identity.
    site.graph["failure_entities"] = sorted(
        (n.entity for n in component if n.entity is not None and n.sources != {"proximity"}),
        key=lambda e: (e[0], _number(e)))
    # D16: the faces a MESHER's own located failure named (C5 put locations on the panel members), without the
    # layer-1 and proximity context: what a site-targeted removal (`defeature` at the site) aims at
    site.graph["mesher_faces"] = sorted(
        (n.entity for n in component if n.entity is not None and n.entity[:1] == "F"
         and n.sources - {"proximity", "valid"}),
        key=lambda e: (e[0], _number(e)))
    site.graph["failures"] = failures
    site.graph["points"] = located
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
                      clashes: list[list[dict]], h_mm: float) -> None:
    requests: list[tuple[Site, dict]] = []
    for site, component, known in zip(sites, components, clashes, strict=True):
        faces = [e for e in site.entities if e[0] == "F"][:MAX_DISTANCE_FACES]
        pairs = [(a, b) for i, a in enumerate(faces) for b in faces[i + 1:]]
        for a, b in pairs:
            requests.append((site, {"a": _ref(ws, a), "b": _ref(ws, b)}))
        prox = [n.proximity_dist for n in component if n.proximity_dist is not None]
        if prox:
            site.dist_over_h = round(min(prox) / h_mm, 4)
        site.graph["clashes"] = list(known)
    if not requests:
        return
    answer = _call(pool, "sites_distance", {
        "brep_path": ws.brep_path, "pairs": [r for _, r in requests]})
    for (site, ask), item in zip(requests, (answer or {}).get("distances", []), strict=False):
        if item.get("dist") is None or item.get("adjacent"):
            continue  # a shared edge is distance 0 for a reason that says nothing about the gap
        d = round(float(item["dist"]) / h_mm, 4)
        site.dist_over_h = d if site.dist_over_h is None else min(site.dist_over_h, d)
        # `touching` by the KERNEL's threshold, not a local copy of it. Imported here, not at module scope: no
        # parent-side module imports a kernel one, and the worker's import order is load-bearing.
        from meshpipeline.cad.repair_v4.sites_kernel import CONTACT_MM

        # `sites_distance` answers for any entity kind, so it reports no areas; a clash without them is still
        # proposed on, with each side offered in turn (`search._contact_rows`), rather than guessing which face
        # is the smaller.
        entry = {"a": ask["a"]["id"], "b": ask["b"]["id"], "dist_mm": round(float(item["dist"]), 4),
                 "point_a": item.get("point_a"), "point_b": item.get("point_b"),
                 "touching": float(item["dist"]) <= CONTACT_MM,
                 "area_a_mm2": None, "area_b_mm2": None}
        if not any({c["a"], c["b"]} == {entry["a"], entry["b"]} for c in site.graph["clashes"]):
            site.graph["clashes"].append(entry)
    for site in sites:
        site.graph["clashes"] = sorted(site.graph.get("clashes", []),
                                       key=lambda c: (c["dist_mm"], c["a"], c["b"]))[:MAX_CLASH_PAIRS]


#: Free loops kept per site (smallest first): the ones `fill_loop` can take are the small ones.
MAX_FREE_LOOPS = 6


def _attach_free_loops(pool: WorkerPool, ws: Workspace, sites: list[Site], reverse: dict) -> None:
    """Put the part's closed free-edge loops on every `open` site as `graph["free_loops"]`.

    An open finding names a shell (no stable id) or a solid, so the site carries no EDGES, and `fill_loop`
    takes edges. The kernel finds the free loops once; each is `{"edges": [stable ids], "span_mm"}` and the part's
    `diagonal_mm` rides with them so a consumer can read the span as a fraction without a second call. Loops are not
    entities of the site: the site's id and identity are unchanged. A failed call leaves `free_loops` empty."""
    open_sites = [s for s in sites if s.kind == "open"]
    if not open_sites:
        return
    answer = _call(pool, "sites_free_loops", {"brep_path": ws.brep_path})
    loops: list[dict] = []
    for loop in (answer or {}).get("loops", []):
        ids = [reverse[("E", int(i))] for i in loop["edges"] if ("E", int(i)) in reverse]
        if len(ids) == len(loop["edges"]):
            loops.append({"edges": sorted(ids, key=_number), "span_mm": float(loop["span"])})
    loops.sort(key=lambda l: (l["span_mm"], l["edges"]))
    for site in open_sites:
        site.graph["free_loops"] = loops[:MAX_FREE_LOOPS]
        site.graph["free_loops_total"] = len(loops)
        site.graph["diagonal_mm"] = float((answer or {}).get("diagonal", 0.0))
        volume = (answer or {}).get("volume")
        if isinstance(volume, dict):
            # What topology can say about `make_volume` / `sew` (sites_kernel.volume_possibility). ABSENT when the call
            # failed, and absent means unknown: the generator proposes. Not shown to the model (`observation` selects).
            site.graph["volume"] = dict(volume)


def _attach_graphs(pool: WorkerPool, ws: Workspace, sites: list[Site], reverse: dict, h_mm: float) -> None:
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
        graph: dict = {"k": GRAPH_HOPS, "h_mm": h_mm}
        if site.graph.get("coarse"):
            graph["coarse"] = site.graph["coarse"]
        for key in ("failures", "points", "clashes", "failure_entities", "mesher_faces", "unmeasured_pairs",
                    "hard_codes"):
            # `unmeasured_pairs` is a COUNT (line 562 writes an int); every other key is a list. A site that was
            # never clustered (an `unlocated` one) has neither, and a `[]` there crashed `observation`'s int().
            graph[key] = site.graph.get(key, 0 if key == "unmeasured_pairs" else [])
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


def _cap(value: Any, limit: int = 2000) -> Any:
    """`value` if its JSON fits `limit` bytes, else a marker holding its head: these two fields are
    caller-supplied and must not be able to blow the observation's size."""
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else {"truncated": text[:limit]}


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
    n_fail = max(1, n_ent // 4)
    entry["failures"] = list(graph.get("failures", []))[:n_fail]  # who said what, for THIS site
    entry["points"] = _round(list(graph.get("points", []))[:n_xyz])  # where, by mesher and entity
    entry["clashes"] = _round(list(graph.get("clashes", []))[:max(1, n_ent // 4)])  # which faces, where
    if len(site.entities) > n_ent:
        entry["entities_omitted"] = len(site.entities) - n_ent
    sub: dict = {k: graph[k] for k in ("k", "error", "note", "truncated") if k in graph}
    if graph.get("hard_codes"):
        sub["hard_codes"] = list(graph["hard_codes"])  # why a whole-part site exists: e.g. no_solids
    if graph.get("coarse"):
        sub["coarse"] = dict(graph["coarse"])  # the whole part, because these members said nothing about where
    if graph.get("free_loops"):
        sub["free_loops"] = _round(graph["free_loops"])  # the edges fill_loop takes, smallest loop first
        sub["diagonal_mm"] = _round(graph.get("diagonal_mm"))
    if graph.get("unmeasured_pairs"):
        sub["unmeasured_pairs"] = graph["unmeasured_pairs"]  # near pairs left UNmerged: may be one part
    if graph.get("id_carried"):
        entry["id_carried"] = True
    if graph.get("was"):
        entry["was"] = graph["was"]  # old ids this site descends from but does not carry
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


def observation(ws: Workspace, verdict: Any, sites: list[Site], budget: Any, *,
                h_mm: float | None = None) -> dict:
    """What the proposer is told: the part's state, what each mesher said, and each failure site.

    JSON-serialisable and no larger than `OBSERVATION_MAX_BYTES`: it is read by a model with a
    per-file dollar budget. When the full picture does not fit it is thinned in a fixed order -
    graph detail first, then entities, then whole sites - and the omissions are counted, never
    silent (`trimmed`; `oversize` if even one site will not fit). `h_mm`, the element size every
    figure is to be read against, is top-level (taken from the sites when not passed). Pixels (`view_png`) are not part of it. A site naming an entity that is no longer live
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
    if h_mm is None:
        h_mm = next((s.graph["h_mm"] for s in sites if s.graph.get("h_mm")), None)
    base: dict = {
        "h_mm": h_mm,
        "valid": {"ok": bool(valid.ok), "units": valid.units, "solids": valid.solids,
                  "round_trip_ok": bool(valid.round_trip_ok),
                  "hard": [{"code": f.code, "entities": list(f.entities)[:10]} for f in valid.hard[:12]]},
        "panel": [{"name": m.name, "status": m.status, "error_kind": m.error_kind,
                   "message": str(m.message or "")[:MESSAGE_CHARS], "min_sicn": _round(m.min_sicn),
                   "elements": m.elements, "located": len(m.located or [])}
                  for m in getattr(verdict, "panel", [])],
        "tier": getattr(verdict, "tier", None),
        "clean": getattr(verdict, "clean", None),
        "faithful": _cap(_plain(getattr(verdict, "faithful", None))),
        "budget": _cap(_plain(budget) if isinstance(budget, dict) or dataclasses.is_dataclass(budget)
                       else {"value": budget}),
        "stale_sites": stale,
        # near face pairs the clustering could not measure and so left unmerged (per site: the
        # same count, summed): a high total means sites may be pieces of one larger failure
        "unmeasured_pairs": sum(int(s.graph.get("unmeasured_pairs", 0)) for s in live),
    }

    def build(level: tuple) -> dict:
        kept = live[: level[0]]
        out = dict(base)
        out["sites"] = [_site_entry(ws, s, level) for s in kept]
        out["sites_omitted"] = len(live) - len(kept)
        return out

    for n, level in enumerate(_LEVELS):
        out = build(level)
        out["trimmed"] = n > 0
        if len(json.dumps(out).encode()) <= OBSERVATION_MAX_BYTES:
            return out
    # Even one thinned site does not fit: say so, and drop what is droppable rather than send more.
    out = build(_LEVELS[-1])
    out["panel"] = [{"name": m["name"], "status": m["status"], "error_kind": m["error_kind"]}
                    for m in base["panel"]]
    out["valid"] = {"ok": base["valid"]["ok"], "hard": [h["code"] for h in base["valid"]["hard"]]}
    out["trimmed"] = True
    if len(json.dumps(out).encode()) > OBSERVATION_MAX_BYTES:
        out["sites"], out["sites_omitted"] = [], len(live)
    out["oversize"] = True
    return out
