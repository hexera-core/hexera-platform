# Responsibility: the `search_seq` arm - repair defects ONE AT A TIME, verify each LOCALLY, and pay the full
#                 4-mesher panel only at the end of a round (owner direction 2026-11-24, seq-arm-design.md).
# Owns: the loop, the defect ordering, the rule table and its parameter derivation, the AI pick, the row fields.
# Boundaries: `verify_local`, the touched-entity set and entity re-location across ops are `local_verify`'s; v5's
#             class diagnosis is `repair_v5.diagnose`, handed in as `diagnose`. The ops, the sites, the verdict,
#             the delivery gate and the guards (B22 catastrophic loss) are used as they are, not copied.
"""One defect, one repair, local verification, at most three rounds.

The owner's objection to the search arms is that they evaluate hundreds of candidates per step. This arm is
linear in the number of defects: diagnose once per round, order the defects, and for each take ONE repair from
a rule table, with its parameters read off the defect's own measurement - no ladders, no sweep, no frontier.

WHAT MAKES IT AFFORDABLE is that a kept repair is judged LOCALLY (`verify_local`: validity on the
touched solids, and for a mesher defect a region mesh with the member that failed), and the whole-part panel runs
once at the end of a round. A local pass is not a whole-part pass - bodies interact, facets are shared - which is
what the rounds are for: the end-of-round panel is the measurement, and a new failure sends the next round at it.

WHAT IT CANNOT DO, stated where it is implemented rather than in a report: a greedy order cannot find a repair
whose first step alone looks worse than doing nothing. The rule table's two-step recipes are the measured answer
to that - most of lane D's oracle wins are 2-step chains whose first step alone changes nothing - and nothing
here searches past them.

THE GUARDS ARE NOT RELAXED. Every kept state goes through the same delivery gate as the other arms, B22's
catastrophic-loss refusal included, and a repair that keeps under a tenth of the part is refused here exactly as
it is there.
"""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult, relocate, verify_local
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.ops.shells import scaled_sew_ladder
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict, PanelWallExhausted
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.workspace import Diff, UnknownTarget, Workspace
from meshpipeline.cad.repair_v5 import events as EV

#: Validity defects first, in the order a repair of one can only help the next (a body that is inside out or
#: overlapping another is a worse thing to mesh than an open shell), then the mesher kinds. Within a rank, by
#: site id, so the arm is deterministic. A code this table does not name sorts last and keeps its own order.
VALIDITY_ORDER: tuple[str, ...] = (
    "non_positive_volume",
    "body_overlap",
    "self_intersection",
    "open_shell",
    "brep_invalid",
)
MESHER_ORDER: tuple[str, ...] = (
    "overlapping_facets",
    "edge_through_face",
    "plc_intersection",
    "hxt_3d_failed",
    "no_volume",
    "low_quality",
    "timeout",
)
#: how many repairs one defect may be given before it is skipped (the table's top entry, then at most two more)
MAX_ALTERNATIVES = 3
#: the design's ceiling: a round that keeps nothing also stops, so this bounds only the productive case
MAX_ROUNDS = 3


# --- `local_verify`'s interface, and what this arm does when it cannot answer ---------------------------------

#: `local_verify` measures; this arm decides. Its `Defect` is the one defect model (so a defect is never
#: re-described on the way), its `LocalResult.outcome` / `.line()` is the trace's own word for what happened, and
#: `verify_local` re-locates the defect itself.
#:
#: TWO FAILURE MODES, kept apart, because an arm that confuses them reports a lie:
#:   - `CheckerUnavailable` - a worker timed out or crashed. INFRASTRUCTURE. The defect is skipped and recorded,
#:     and the file's row says the check could not be had; it is never read as "the repair did not work".
#:   - `NotImplementedError` - a verifier stub. Same treatment, its own reason, so it reads as "not measured" and
#:     not as "the arm cannot repair". Both stop the alternatives for that defect: nothing would check the next
#:     one either.
UNAVAILABLE: tuple[type[BaseException], ...] = (CheckerUnavailable, NotImplementedError)

#: `Defect.kind` values, named here so this module never spells them as bare strings
VALIDITY = "validity"
MESHER = "mesher"
#: the outcome a diagnosis gives a failure that is the CHECKER's (a crash or timeout with nothing located): it is
#: recorded, never repaired, and is not a defect class
CHECKER_FAILED = "checker_failed"


# --- defects: `local_verify.Defect`, built from this state's sites ------------------------------------------

#: `Defect.kind` is two-valued; the ORDER below is over the CODE.
_ORDER: dict[str, tuple[int, int]] = {
    **{code: (0, i) for i, code in enumerate(VALIDITY_ORDER)},
    **{code: (1, i) for i, code in enumerate(MESHER_ORDER)},
}


def defect_rank(defect: Defect) -> tuple:
    """Validity before mesher, each code in its ruling's order, then the site id: deterministic, and a code the
    tables do not name sorts LAST rather than first - an unmeasured kind must not pre-empt a measured one."""
    where = _ORDER.get(defect.code, (0 if defect.kind == VALIDITY else 1, len(MESHER_ORDER) + 1))
    return (where[0], where[1], defect.id, defect.code)


def order_defects(defects: Sequence[Defect]) -> list[Defect]:
    return sorted(defects, key=defect_rank)


#: LAYER 1 NAMES ITS OWN ENTITY LISTS. `valid_kernel` emits `S<k>`, `F<k>` and `E<k>` over `TopExp` order (solids,
#: faces, edges) on the STEP it read; the workspace names bodies `B...`, faces `F...` and edges `E...` with its OWN
#: stable numbering and carries each one's `TopExp` index on the BREP (`ws.indices`). The two enumerations are the
#: same order over the same topology, so the translation is by INDEX, per kind. `F<k>`/`E<k>` look like workspace
#: ids and are NOT: layer-1 `F12` is the 12th face in map order, workspace `F12` is whatever face was minted 12th.
#: Until v5 step 5 only `S<k>` was translated and a layer-1 face or edge name passed through as if it were a stable
#: id, so `tighten_tolerances`, `split_closed` and every edge op on a `brep_invalid` defect aimed at the WRONG
#: entity (the bug family that killed v4's prototype). A name the index cannot place is REFUSED, never guessed: a
#: wrong id would aim an op at the wrong entity, which is worse than skipping the defect.
LAYER1_SOLID = "S"
#: layer-1 letter -> the workspace's prefix for the same kind. Shells (`H<k>`) have no workspace id.
LAYER1_PREFIX: dict[str, str] = {"S": "B", "F": "F", "E": "E"}


def layer1_ids(ws: Any, entities: Sequence[StableId]) -> tuple[list[StableId], list[StableId]]:
    """(the workspace stable ids for layer-1 `entities`, the names that could not be translated).

    THE ONE PLACE a layer-1 entity name becomes a workspace id. `S<k>`/`F<k>`/`E<k>` are kernel indices,
    translated through the workspace's own index of that kind; a workspace body id (`B...`) passes through (it
    cannot be a layer-1 name); anything else (a shell `H<k>`, a malformed name, an index the workspace does not
    have) is returned as unmapped. This is the fix for B24's worst bug (bodies) and v5 step 5's (faces/edges):
    defects built straight from layer 1's names aim at entities the workspace has never heard of, or at the wrong
    ones.
    """
    live = getattr(ws, "ids", None)
    by_index: dict[tuple[str, int], StableId] = {}
    for sid, index in (getattr(ws, "indices", None) or {}).items():
        if live is None or sid in live:          # a retired id keeps no index worth aiming at
            by_index[(str(sid)[:1], int(index))] = sid
    out: list[StableId] = []
    unmapped: list[StableId] = []
    for e in entities:
        name = str(e)
        if name[:1] == "B":
            out.append(e)
            continue
        prefix = LAYER1_PREFIX.get(name[:1])
        try:
            index = int(name[1:])
        except ValueError:
            index = None
        mapped = by_index.get((prefix, index)) if prefix is not None and index is not None else None
        if mapped is None:
            unmapped.append(e)
        else:
            out.append(mapped)
    return out, unmapped


#: the pre-step-5 name, kept for its callers and tests: it now translates faces and edges too
body_ids_of = layer1_ids


def defects_of(verdict: Any, sites: Sequence[Site], ws: Any = None) -> list[Defect]:
    """This state's defects as `Defect`s: layer 1's hard findings first, then every mesher failure the
    sites located, ordered by `defect_rank`.

    ONE DEFECT PER (code, site). Three panel members reporting one failure at one place is ONE thing to repair,
    and the `member` carried is the first that reported it - the member the local check re-runs.

    BODY_OVERLAP IS CLUSTERED (owner note 1): the connected components of the overlapping-pair graph are one
    defect each, carrying the cluster's bodies as `entities` and its pairs as `pairs`, so the arm repairs a
    cluster PAIR BY PAIR and one bad pair neither blocks nor fails the rest.
    """
    out: list[Defect] = []
    by_entity: dict[StableId, Site] = {}
    for site in sites:
        for e in site.entities:
            by_entity.setdefault(e, site)
    fallback = next((s for s in sites if s.kind in ("unlocated", "invalid")), sites[0] if sites else None)

    seen: set[tuple[str, str]] = set()
    unmapped_total: list[StableId] = []

    def ids_of(entities: Sequence[StableId]) -> tuple[tuple[StableId, ...], tuple[StableId, ...]]:
        if ws is None:
            return tuple(entities), ()
        mapped, bad = layer1_ids(ws, entities)
        # a shell (`H<k>`) has no workspace id BY DESIGN: dropped, never "unmapped" (which skips the defect)
        bad = [e for e in bad if str(e)[:1] in LAYER1_PREFIX]
        unmapped_total.extend(bad)
        return tuple(mapped), tuple(bad)

    hard = list(getattr(getattr(verdict, "valid", None), "hard", ()) or ())
    for finding in hard:
        code = str(getattr(finding, "code", "") or "")
        if not code or code == "body_overlap":      # clustered below, never one defect per pair
            continue
        raw = tuple(getattr(finding, "entities", ()) or ())
        ents, bad = ids_of(raw)
        site = next((by_entity[e] for e in (*ents, *raw) if e in by_entity), fallback)  # type: ignore[assignment]
        if site is None or (code, site.id) in seen:
            continue
        seen.add((code, site.id))
        measurement = _measure(site, ents)
        if bad:
            # LOUD, and its own reason: never again indistinguishable from "the ids died in an earlier repair"
            measurement["unmapped_layer1_entities"] = list(bad)
        out.append(Defect(id=site.id, kind=VALIDITY, code=code, entities=ents,  # type: ignore[arg-type]
                          xyz=_points(site), box=_box(site), measurement=measurement))
    out.extend(_overlap_clusters([f for f in hard if str(getattr(f, "code", "")) == "body_overlap"],
                                 by_entity, fallback, ids_of))

    for site in sites:
        graph = site.graph or {}
        faces = tuple(f for f in (graph.get("mesher_faces") or []) if str(f)[:1] == "F")
        for failure in graph.get("failures") or []:
            if failure.get("mesher") == "valid":
                continue
            code = str(failure.get("error_kind") or "")
            if not code or (code, site.id) in seen:
                continue
            seen.add((code, site.id))
            out.append(Defect(id=site.id, kind=MESHER, code=code, entities=faces or tuple(site.entities),  # type: ignore[arg-type]
                              member=str(failure.get("mesher") or "") or None, xyz=_points(site), box=_box(site),
                              measurement=_measure(site, faces or tuple(site.entities))))
    return order_defects(out)


def _overlap_clusters(findings: Sequence[Any], by_entity: dict[StableId, Site], fallback: Site | None,
                      ids_of: Callable[[Sequence[StableId]], tuple[tuple[StableId, ...], tuple[StableId, ...]]],
                      ) -> list[Defect]:
    """One defect per CONNECTED cluster of overlapping bodies (owner note 1).

    Two bodies are in one cluster when a `body_overlap` finding names them together, transitively: a chain of
    three interpenetrating bodies is ONE defect with two pairs, because repairing one pair changes the geometry
    the next is measured on, and because a cut aimed at one pair can push a clash onto the third body.
    """
    parent: dict[StableId, StableId] = {}

    def find(x: StableId) -> StableId:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs: list[tuple[StableId, StableId]] = []
    unmapped: list[StableId] = []
    for finding in findings:
        ents, bad = ids_of(list(getattr(finding, "entities", ()) or ()))
        unmapped.extend(bad)
        ents = list(ents)  # type: ignore[assignment]
        for i, a in enumerate(ents):
            for b in ents[i + 1:]:
                pairs.append((a, b) if a <= b else (b, a))
                parent[find(a)] = find(b)
    clusters: dict[StableId, set[tuple[StableId, StableId]]] = collections.defaultdict(set)
    for a, b in pairs:
        clusters[find(a)].add((a, b))
    out: list[Defect] = []
    for root, cluster_pairs in sorted(clusters.items()):
        bodies = tuple(sorted({e for pair in cluster_pairs for e in pair}))
        site = next((by_entity[e] for e in bodies if e in by_entity), fallback)
        if site is None:
            continue
        measurement = _measure(site, bodies)
        if unmapped:
            measurement["unmapped_layer1_entities"] = sorted(set(map(str, unmapped)))
        out.append(Defect(id=f"{site.id}:overlap:{root}", kind=VALIDITY, code="body_overlap", entities=bodies,  # type: ignore[arg-type]
                          pairs=tuple(sorted(cluster_pairs)), xyz=_points(site), box=_box(site),
                          measurement=measurement))
    return out


def _points(site: Site) -> tuple:
    """The site's located points as (x, y, z). `graph["points"]` holds the mesher's located notes as DICTS
    (`{"xyz": [...], "entity": ..., ...}`, sites.py); `site.xyz` holds bare triples. Reading `p[0]` off a dict raised
    KeyError and silently dropped EVERY located point for every class (found in v5 round 2, fix E), which also left
    geometric relocation with nothing to go on."""
    out = []
    for p in (site.graph or {}).get("points") or site.xyz or []:
        if isinstance(p, dict):
            p = p.get("xyz")
        try:
            out.append((float(p[0]), float(p[1]), float(p[2])))  # type: ignore[index]
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return tuple(out)


def _box(site: Site) -> tuple | None:
    box = (site.graph or {}).get("box")
    try:
        return tuple(float(v) for v in box) if box and len(box) == 6 else None
    except (TypeError, ValueError):
        return None


def _measure(site: Site, entities: Sequence[StableId]) -> dict[str, Any]:
    """The defect's measurement, read off the site's graph. Only what a parameter is derived from, and only what
    the site really carries: a key that is absent stays absent, because a derived parameter with an invented
    measurement behind it is a ladder with one rung."""
    graph = site.graph or {}
    out: dict[str, Any] = {}
    if graph.get("diagonal_mm"):
        out["diagonal_mm"] = float(graph["diagonal_mm"])
    best: dict[str, Any] | None = None
    named = set(entities) or set(graph.get("mesher_faces") or [])
    for node in graph.get("nodes") or []:
        fid, area, width = node.get("id"), node.get("area"), node.get("min_width")
        if not isinstance(fid, str) or fid[:1] != "F" or not area or not width or width <= 0:
            continue
        if named and fid not in named:
            continue
        entry = {"id": fid, "width_mm": float(width), "length_mm": float(area) / float(width)}
        if best is None or entry["width_mm"] < best["width_mm"]:
            best = entry
    if best is not None:
        out["narrowest_face"] = best
    clashes = [c for c in (graph.get("clashes") or []) if c.get("dist_mm") is not None]
    if clashes:
        # per PAIR, so a cluster's pair is repaired with ITS own measured clash, not the cluster's worst
        out["clash_by_pair"] = {pair_key(c.get("a"), c.get("b")): float(c["dist_mm"]) for c in clashes}
        worst = max(clashes, key=lambda c: float(c["dist_mm"]))
        out["clash"] = {"a": worst.get("a"), "b": worst.get("b"), "dist_mm": float(worst["dist_mm"])}
    if graph.get("free_loops"):
        out["free_loops"] = len(graph["free_loops"])
        # THE EDGES fill_loop TAKES (smallest loop first, as sites sorts them). Only the count was kept before
        # v5, so fill_loop was aimed at the defect's FACES and validate_call refused every call.
        loops = [list(loop["edges"]) for loop in graph["free_loops"] if isinstance(loop, dict) and loop.get("edges")]
        if loops:
            out["free_loop_edges"] = loops
    edges = _site_edges(site)
    if edges:
        out["site_edges"] = edges
    if graph.get("coarse"):
        out["coarse"] = True
    return out


#: at most this many edges are carried as a defect's `site_edges`
MAX_SITE_EDGES = 8


def _site_edges(site: Site) -> list[StableId]:
    """The edges an edge op can aim at for this site, as STABLE ids: the edges the site names, then every edge of its
    neighbourhood graph that lies between two of the site's own faces. Read from the site (sites.py translates the
    kernel's names) rather than from a layer-1 finding, whose `F<k>`/`E<k>` names are kernel indices."""
    faces = {e for e in site.entities if str(e)[:1] == "F"}
    out = [e for e in site.entities if str(e)[:1] == "E"]
    for g in (site.graph or {}).get("edges") or []:
        edge, between = g.get("edge"), set(g.get("faces") or [])
        if isinstance(edge, str) and edge[:1] == "E" and len(between) == 2 and between <= faces and edge not in out:
            out.append(edge)
    return out[:MAX_SITE_EDGES]


def pair_key(a: Any, b: Any) -> str:
    return "|".join(sorted((str(a), str(b))))


# --- the rule table ------------------------------------------------------------------------------------------

#: One repair: the ops to apply in order, 1 or 2 of them. Two steps because most of lane D's oracle wins are
#: 2-step chains whose FIRST step alone changes nothing measurable - a greedy arm that judged step 1 on its own
#: would throw them away. `run_stage:<stage>` is the staged healer's macro, the one "op" the sweep cannot build.
Repair = tuple[str, ...]

#: THE TABLE, seeded from lane D's oracle pass-1 winners (`lane-d21/pass1/winners_by_defect.jsonl`), keyed by the
#: defect kind the table itself records (`label_family`), ranked by DISTINCT FILES with ties broken by paths -
#: LEAD-3's ruling of 2026-11-24, for the reason B21c recorded: path counts over-weight a pattern that is
#: concentrated in a couple of files. GAMED rows are excluded, and so are rows a v4 arm already solves, so what
#: is left is what the oracle found and the arms did not.
#:
#: Counts are (distinct files / winning paths) among ORACLE-ONLY rows, written beside each entry so a refresh is
#: a mechanical diff rather than an argument. Read `rule_table_from_winners` for how they are derived.
#:
#: The honest caveat on the biggest kind: `overlapping_facets` is 11 files of evidence here, and lane B10 measured
#: that no op in the library yet clears it on any of 8 real parts. A rule table cannot invent an op; what this
#: arm can show is whether the oracle's ORDER (split or thin first, then the stage) does better than a sweep.
RULE_TABLE: dict[str, tuple[Repair, ...]] = {
    # 10 files. conformalise and fuse tie at 5 files; conformalise leads on paths (10 vs 7).
    "body_overlap": (("conformalise",), ("fuse",), ("fuse", "conformalise"), ("defeature", "conformalise")),
    # 3 files, every entry thin: 2 files each, and the leader wins on paths (4 vs 2).
    "brep_invalid": (("defeature", "run_stage:bodies"), ("defeature",), ("run_stage:bodies", "defeature")),
    # 6 files. defeature alone leads on files (4); conformalise is 2 files but 8 paths.
    "edge_through_face": (("defeature",), ("conformalise",), ("defeature", "defeature"),
                          ("defeature", "run_stage:bodies")),
    # 1 file. Kept because an arm with no entry falls to the AI, and one measured win beats a guess.
    "hxt_3d_failed": (("defeature",),),
    # DEMOTED ON TWO WAVES OF EVIDENCE, no longer a stopgap (LEAD-3's approval, 2026-11-24). `reapproximate`
    # was the oracle's leader on ONE file; measured in the arm it has never once applied:
    #     wave 202611242007   reapproximate 0 applied of 10 tried    split_by_proximity 5 of 10
    #     wave 202611242110   reapproximate 0 applied of  7 tried    split_by_proximity 5 of 10
    # every refusal `no_valid_face: the new carrier does not give a valid face`. Seventeen refusals on real
    # parts outweigh one oracle file, and `split_by_proximity` made every low_quality repair in either wave.
    "low_quality": (("split_by_proximity",), ("reapproximate",)),
    # 1 file. The 2-step leader is the only entry with more than one winning path.
    "no_volume": (("split_by_proximity", "remove_small"), ("defeature",),
                  ("remove_small", "unify_same_domain")),
    # 4 files. defeature and remove_small tie at 3 files; defeature leads on paths (10 vs 6).
    "open_shell": (("defeature",), ("remove_small",), ("defeature", "defeature"),
                   ("fill_loop", "remove_small")),
    # 11 files, the broadest evidence in the table - and the kind nothing yet clears (see the caveat above).
    # C23: `split_aligned` FIRST, AHEAD OF THE ORACLE'S ORDER, with its reason: the oracle never had this
    # op, and B10 measured that nothing in the library clears this kind. Measured on a real part instead: on
    # f9501251fb30 (7 ABC duplicates of one part) the meshers' discretisations of a shaft and its coaxial bore cross;
    # aligned splitting took gmsh_default from overlapping_facets and gmsh_prod from HXT 'other' to a mesh. The
    # oracle's rows stay behind it.
    "overlapping_facets": (("split_aligned",), ("split_angle", "run_stage:condition"), ("remove_small", "split_angle"),
                           ("remove_small", "defeature"), ("remove_small", "run_stage:condition"),
                           ("defeature", "run_stage:bodies")),
    # 1 file, 30 oracle-only rows: one file's pattern, ranked by paths within it.
    "plc_intersection": (("conformalise",), ("defeature", "split_angle"), ("remove_small", "split_angle")),
    # 4 files. defeature alone leads on files (2); the 2-step rows are one file each.
    "self_intersection": (("defeature",), ("defeature", "conformalise"), ("defeature", "fuse"),
                          ("conformalise",)),
}


def rule_table_from_winners(rows: Sequence[dict], *, max_repairs: int = 5) -> dict[str, tuple[Repair, ...]]:
    """`RULE_TABLE` derived from lane D's oracle rows, so a refreshed table is a diff and not a rewrite.

    One entry per (defect kind, path prefix of at most 2 ops), ranked by DISTINCT FILES then winning paths then
    the name. GAMED rows are dropped (lane D's `gaming` flag: a "win" that deleted the part), and so are rows a
    v4 arm already solves (`solved_by_v4_arm`) - the table exists to carry what the arms do NOT reach.
    """
    files: dict[tuple[str, Repair], set[str]] = collections.defaultdict(set)
    paths: collections.Counter[tuple[str, Repair]] = collections.Counter()
    for row in rows:
        if row.get("gaming") or row.get("solved_by_v4_arm"):
            continue
        kind = str(row.get("label_family") or "")
        path = tuple(str(op) for op in (row.get("path") or [])[:2])
        if not kind or not path:
            continue
        files[(kind, path)].add(str(row.get("sha256") or ""))
        paths[(kind, path)] += 1
    out: dict[str, list[Repair]] = collections.defaultdict(list)
    for (kind, path), _shas in sorted(files.items(), key=lambda kv: (kv[0][0], -len(kv[1]), -paths[kv[0]], kv[0][1])):
        if len(out[kind]) < max_repairs:
            out[kind].append(path)
    return {k: tuple(v) for k, v in sorted(out.items())}


# --- parameters, from the defect's own measurement -----------------------------------------------------------

#: `remove_small` measures a face as the larger of its inertia width and its second bounding-box extent, which
#: for a tilted strip is about its LENGTH, so the limit has to clear the strip's own length or the op refuses the
#: face it was aimed at. 1.05 is the margin the det sweep's aimed row already uses; this is that rule, applied to
#: a measurement rather than to a ladder rung.
REMOVE_SMALL_MARGIN = 1.05
#: the aspect at which a face is narrow enough for a removal to be SIZED from it - the same 8:1 the det sweep's
#: aimed rows use (`search.NARROW_ASPECT`), named here so this module does not import the search driver.
NARROW_ASPECT = 8.0
#: at most this many measured face pairs are handed to `split_by_proximity` (its own schema takes a list)
MAX_PROXIMITY_PAIRS = 8
#: v5 step 7: a close_faces pair's facets may each sag this share of the measured gap (two faces: under half)
CLOSE_FACES_SAG_SHARE = 0.25
#: a clash or gap is closed by a fuzzy value at least the measured distance; 1.1 covers the measurement's own
#: rounding (the sites round `dist_mm` to 4 decimals) without turning it into a rung.
FUZZY_MARGIN = 1.1
#: v5 step 9: `imprint_crossing` takes at most this many target faces (its schema's cap), and finds this many crossing
#: partners per face when the crossing detector measured none (a mesher's one named surface)
MAX_IMPRINT_FACES = 32
IMPRINT_PARTNERS = 4


#: THRESHOLDS NO MEASUREMENT GIVES. Three ops need a required parameter that is not a property of the defect:
#: `split_angle`'s dihedral limit, `reapproximate`'s degree and tolerance, `split_continuity`'s order. The design
#: says parameters come from the measurement and not from a ladder - so these are ONE value each, marked in the
#: trace as a DEFAULT rather than passed off as measured, and taken from the most frequent value among lane D's
#: oracle winners where that value is unambiguous (`max_deg` 60 and `order` 2 are; the oracle's `params` records
#: do not align op-for-op with its `path`, so nothing finer is read out of them).
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "split_angle": {"max_deg": 60},
    # C23: part-wide (it finds every near-coaxial group itself); 8 rays, the value measured on f9501251fb30
    "split_aligned": {"segments": 8, "scope": "all"},
    "split_continuity": {"order": 2},
    "reapproximate": {"max_degree": 9, "tol_mm": 0.025},
    # v5 step 5: the same tolerance as reapproximate's (a carrier swap within it, the deviation measured by the op)
    "to_canonical": {"tol_mm": 0.025},
    # v5 wiring. The normalise stage's own target (`stages.DEFAULTS["normalise"]`): 100x under layer 1's
    # large_tolerance line (1e-3 x diagonal), and inside the op's [1e-7, 1e-4] window
    "tighten_tolerances": {"target_rel": 1e-5},
    # the det sweep's first pad (`search._TRIM_PADS`): the finding measures the open sides, not a pad
    "trim_unbounded": {"pad_fraction": 0.01},
    # a planar fill ignores G1, and the achieved continuity is measured by the op, so G0 is the honest ask
    "fill_loop": {"continuity": "G0"},
    # v5 step 8: the same honest G0 ask for a face rebuilt from its own boundary
    "rebuild_face": {"continuity": "G0"},
    # v5 step 8: pieces under 0.1% of the PART's volume are slivers of the fragment (variants step x10 either side)
    "fragment_drop_small": {"max_fraction": 1e-3},
}
#: the ops whose parameters are a default rather than a measurement, for the trace's own honesty
DEFAULTED: frozenset[str] = frozenset(DEFAULT_PARAMS)


class NoParameters(ValueError):
    """The defect's measurement does not carry what this op needs. The repair is not attempted: a parameter this
    arm cannot derive is a ladder with one rung, and the design says parameters come from the measurement."""


def params_for(op: str, defect: Defect, *, pair: tuple[StableId, StableId] | None = None,
               live: set[StableId] | None = None) -> tuple[list[StableId], dict[str, Any]]:
    """(targets, params) for `op` on `defect`, derived from the defect's own measurement. Raises `NoParameters`
    when the measurement does not carry what the op needs.

    With `pair` (one pair of a body_overlap cluster) the targets are that pair's two bodies and the fuzzy value
    is THAT pair's measured clash - not the cluster's worst, which would over-cut the mild pairs."""
    m = defect.measurement
    # v5: a diagnosed cause may name an AIM (the faces/edges a measured fault is on, e.g. the pole faces of a
    # brep_invalid defect whose entities also name its solid): the op is aimed there while any of it is live
    aim = [e for e in ((defect.cause or {}).get("aim") or []) if live is None or e in live]
    entities = list(pair) if pair else (aim or list(defect.entities))
    # ONLY IDS THAT ARE STILL LIVE. An earlier repair in this round renames faces; `relocate` carries
    # the defect, but a measurement this module took on the old state still names the old ids, and an op refuses
    # them ("replaced by op 'remove_small' (now: F16); use the new ids"). Derived parameters are therefore
    # filtered to the ids the workspace still has, and a defect with nothing live left is not guessed at.
    if live is not None:
        entities = [e for e in entities if e in live]
    faces = [e for e in entities if str(e)[:1] == "F"]
    solids = [e for e in entities if str(e)[:1] in ("S", "B")]   # the workspace names bodies B..; layer 1 names them S..

    # --- WHOLE-PART ops: they take no targets, so a defect whose own ids died can still be given one ---------------
    if op.startswith("run_stage:"):
        return [], {"stage": op.split(":", 1)[1], "params": {}}
    if op == "split_aligned":  # C23: part-wide, it finds the near-coaxial groups itself - no located face needed
        # v5 step 7: a diagnosed near-coaxial close pair names the faces to split at the SAME angles (both of them)
        group = [f for f in ((defect.cause or {}).get("aligned_faces") or []) if live is None or f in live]
        if len(group) >= 2:
            return group, {**DEFAULT_PARAMS[op], **_aligned_segments(defect), "scope": "targets"}
        return [], {**DEFAULT_PARAMS[op], **_aligned_segments(defect)}
    if op == "sew":
        # the rungs are the staged healer's and the det sweep's own (`scaled_sew_ladder`), sized from the measured
        # diagonal; the op keeps the TIGHTEST rung that closes the shell, so the ladder is not a sweep of attempts
        diag = m.get("diagonal_mm")
        ladder = scaled_sew_ladder(float(diag)) if diag else []
        if not ladder:
            raise NoParameters("sew's tolerances are sized from the part's diagonal, and "
                               + ("no rung fits the kernel's window for it" if diag else "it was not measured"))
        return [], {"tol_ladder": ladder}
    if op == "make_volume":  # no targets: every face outside a solid (the op's own default)
        return [], {}
    if op == "sew_gap":
        # v5 step 11: ONE tolerance, the open_shell_volume detector's measured gap x margin (`open_shell.shell_cause`)
        tol = (defect.cause or {}).get("sew_tol_mm")
        if not tol:
            raise NoParameters("sew_gap sews at the measured free-edge gap, and no partnered free edge was measured")
        return [], {"tol_mm": float(tol)}
    if op == "split_non_manifold":  # whole part: every unit with an edge shared by more than two faces
        return [], {}
    if op == "drop_sheet":  # v5 round 2, whole part: every stray sheet (the op re-checks each condition itself)
        return [], {}
    if op == "trim_unbounded":
        return [], dict(DEFAULT_PARAMS[op])
    if op == "rescale":
        # v5 units rule (`repair_v5.brep_faults.units_verdict`): the factor IS the measurement, never a default
        factor = (defect.cause or {}).get("factor")
        if not factor:
            raise NoParameters("rescale needs the units rule's factor, and the cause carries none (report only)")
        return [], {"factor": float(factor)}
    # --- ops aimed by the MEASUREMENT (edges the site carries), with the defect's own edges as the fallback ---------
    if op == "fill_loop":
        loops = [loop for loop in (m.get("free_loop_edges") or []) if live is None or all(e in live for e in loop)]
        # v5 step 11: the detector's own loop (`open_shell`: the smallest UNPARTNERED loop) when the cause aims edges
        aimed = [e for e in aim if str(e)[:1] == "E"]
        edges = aimed if aimed and len(aimed) == len(aim) else (
            loops[0] if loops else [e for e in entities if str(e)[:1] == "E"])
        if not edges:
            raise NoParameters("fill_loop needs a free loop's edges, and none were measured at this defect")
        return list(edges), dict(DEFAULT_PARAMS[op])
    if op == "rebuild_edge_as_intersection":
        edges = [e for e in (m.get("site_edges") or []) if live is None or e in live]
        edges = edges or [e for e in entities if str(e)[:1] == "E"]
        if not edges:
            raise NoParameters("rebuild_edge_as_intersection needs an edge between two of the defect's faces, "
                               "and the site names none")
        return edges, {}
    if op == "remove_duplicate_faces":
        # v5 step 9: the duplicate pairs v5's crossing detector measured (`repair_v5.crossing`), across bodies, live
        dups = [list(p) for p in ((defect.cause or {}).get("duplicate_pairs") or [])
                if len(p) == 2 and (live is None or all(f in live for f in p))]
        if not dups:
            raise NoParameters("remove_duplicate_faces needs measured duplicate face pairs across two bodies, and "
                               "the crossing detector found none live at this defect")
        return [], {"pairs": dups}

    if not entities and (defect.entities or pair):
        raise NoParameters("none of the defect's entities are live any more")
    if op == "orient_and_close":  # the defect's bodies; none named = every solid (the op's own default)
        return [e for e in solids if str(e)[:1] == "B"], {}
    if op == "tighten_tolerances":  # the defect's entities are the scope; none = the whole part
        return [e for e in entities if str(e)[:1] in ("F", "E", "B")], dict(DEFAULT_PARAMS[op])
    if op in ("split_turn", "cut_knife"):
        # round 2 fix G (`repair_v5.facet_local`): the measured cause carries both the faces (every curved face of
        # the local crossing pairs; the knife pair) and the parameters (the angle; the width and the reach)
        cause = defect.cause or {}
        given = (cause.get("op_params") or {}).get(op)
        aimed = [f for f in (cause.get("turn_faces" if op == "split_turn" else "pair") or [])
                 if str(f)[:1] == "F" and (live is None or f in live)]
        if not given or not aimed or (op == "cut_knife" and len(aimed) != 2):
            raise NoParameters(f"{op} is aimed and sized by a measured facet_crossing_local cause, and this defect "
                               + ("carries no parameters for it" if not given else "has its faces no longer live"))
        return aimed, dict(given)
    if op in _CAUSE_PARAMS:
        # v5 step 10 (`repair_v5.quality`): the low_quality cause carries the measured entity (`aim`) and the
        # op's parameters sized from it; refit_cluster takes the sliver AND the neighbour it merges into
        cause = defect.cause or {}
        want = {k: cause[k] for k in _CAUSE_PARAMS[op] if cause.get(k) is not None}
        prefix = "B" if op == "drop_solid" else "E" if op == "collapse_edge" else "F"
        aimed = [e for e in aim if str(e)[:1] == prefix]
        if op == "drop_solid":
            aimed = aimed[:1]             # the small solid only, never the partner a tiny_solid cause names
        if op == "refit_cluster":
            partner = cause.get("neighbour")
            if not partner or (live is not None and partner not in live):
                raise NoParameters("refit_cluster needs the neighbour the sliver continues smoothly into, and every "
                                   f"neighbour meets it above the feature angle ({cause.get('protected')})")
            aimed = [*aimed[:1], partner]
            want = {"tol_mm": cause["refit_tol_mm"]} if cause.get("refit_tol_mm") else {}
        if not aimed or len(want) != len(_CAUSE_PARAMS[op]):
            raise NoParameters(f"{op} is sized from a measured low_quality cause, and this defect carries "
                               f"{'no live ' + _KIND_WORD[prefix] if not aimed else 'no ' + '/'.join(_CAUSE_PARAMS[op])}")
        return aimed, want
    if op == "remove_small" and (defect.cause or {}).get("max_width_mm") and aim:
        # v5 step 10: the measured small entity, at the width remove_small itself measures it (never refused)
        return [e for e in aim if str(e)[:1] in "FE"], {"max_width_mm": float(defect.cause["max_width_mm"])}
    if op == "remove_small":
        face = m.get("narrowest_face")
        if not face:
            raise NoParameters("no located face with a measured width at this defect")
        w, length = float(face["width_mm"]), float(face["length_mm"])
        # ONLY FROM A FACE THAT IS ACTUALLY NARROW. `length` is area/width, so on a big face with one small
        # dimension it is huge: wave 202611242007 derived limits of 1211 mm and 974 mm this way and the op
        # declined all 13 of them ("nothing at or under X mm to remove"). The det sweep's margin rule was only
        # ever applied to faces of aspect >= NARROW_ASPECT, and that condition is the missing half of it.
        if w <= 0 or length / w < NARROW_ASPECT:
            raise NoParameters(f"the narrowest face at this defect is {length / max(w, 1e-9):.1f}:1, under the "
                               f"{NARROW_ASPECT:g}:1 a removal is sized from: nothing here is a sliver")
        width = REMOVE_SMALL_MARGIN * (length + w)
        diagonal = m.get("diagonal_mm")
        if diagonal and width > float(diagonal):
            # a limit larger than the part is a derivation bug, not a parameter choice (LEAD-3)
            raise NoParameters(f"a removal limit of {width:.3g} mm is larger than the part "
                               f"({float(diagonal):.3g} mm): the measurement does not size this op")
        return [face["id"]], {"max_width_mm": width}
    if op == "tweak":
        # v5 step 6: the smaller-id body is moved onto the other; the bound is the conformal pass's near bound
        # (the fidelity bound of the part's diagonal) - the op itself declines a gap over it or non-parallel faces
        bodies = sorted(set(solids or [b for b in cause_bodies(defect) if live is None or b in live]))
        if len(bodies) != 2:
            raise NoParameters(f"tweak moves one body onto one partner, and the defect names {len(bodies)} bodies")
        diag = m.get("diagonal_mm")
        return bodies, {"max_mm": min(0.1, 1e-3 * float(diag)) if diag else 0.1}
    if op in ("conformalise", "fuse"):
        clash = m.get("clash")
        bodies = solids or [b for b in (clash or {}).values() if isinstance(b, str) and b[:1] in ("S", "B")]
        if not bodies:
            # v5: a mesher defect carries faces; its diagnosed cause names the bodies of the measured face pair
            bodies = [b for b in cause_bodies(defect) if live is None or b in live]
        if not bodies:
            raise NoParameters("no bodies named at this defect")
        # THE FUZZY VALUE IS ZERO, and that is what the measurement says - not an absence of one.
        #
        # The design said "fuse/cut fuzzy = the measured gap/clash", and I implemented 1.1x the clash. Two
        # things from the wave and from lane D's table say that was wrong. Lane D's oracle won body_overlap
        # with `fuzzy_mm: 0.0` on every one of its 7 conformalise files and its 8 fuse files - there is not a
        # single non-zero fuzzy among them. And on 8c6a3f2186e7, whose three pairs really do overlap (308, 458
        # and 24 mm3 of common volume, measured), a 1.375 mm fuzzy made `conformalise` return `cut_degenerate`
        # and `fuse` say `nothing_to_fuse: the bodies do not touch or overlap` - a fuzzy that large moves the
        # Boolean's own tolerance far enough that it stops seeing the interface it was given.
        #
        # So the clash is what SELECTS and ORDERS the repair, and it is reported in the trace; the Boolean runs
        # at the kernel's own tolerance.
        measured = (m.get("clash_by_pair") or {}).get(pair_key(*pair)) if pair else None
        if measured is None and clash is not None:
            measured = float(clash["dist_mm"])
        fuzzy = 0.0
        if op == "fuse":
            return sorted(set(bodies)), {"fuzzy_mm": fuzzy}
        # `cut_by:<id>` names the body that is KEPT whole; the other is cut back off it. With no volume for the
        # pair the choice is the lower id, which is deterministic and no worse than a coin toss - lane D's
        # winners name every body in turn across its files, so the table has no preference to copy.
        keep = sorted(set(bodies))[0]
        return sorted(set(bodies)), {"mode": f"cut_by:{keep}", "fuzzy_mm": fuzzy}
    if op == "split_by_proximity" and not m.get("clash_by_pair") and (defect.cause or {}).get("face_pairs"):
        # v5 step 7 (close_faces): the diagnosed close pairs; each face's facets may sag at most a quarter of the
        # pair's gap, so the two together stay under half of it (the class's sag < gap/2 rule)
        pairs = [list(p) for p in defect.cause["face_pairs"] if len(p) == 2
                 and (live is None or all(e in live for e in p))]
        if not pairs:
            raise NoParameters("split_by_proximity needs the diagnosed close pairs live, and none is")
        return [], {"face_pairs": pairs[:MAX_PROXIMITY_PAIRS], "target_ratio": CLOSE_FACES_SAG_SHARE}
    if op in ("boolean_by_volume", "drop_duplicate_body", "fragment_drop_small"):
        # v5 step 8 (overlap_self_intersection): a cluster's PAIR, else the defect's bodies (a self-intersecting solid)
        own = [e for e in defect.entities if str(e)[:1] == "B" and (live is None or e in live)]
        bodies = sorted(set(solids or own or [b for b in cause_bodies(defect) if live is None or b in live]))
        if op == "fragment_drop_small":
            if not bodies:
                raise NoParameters("fragment_drop_small needs the defect's bodies, and none are live")
            return bodies, dict(DEFAULT_PARAMS[op])
        if len(bodies) != 2:
            raise NoParameters(f"{op} resolves one pair of bodies, and the defect names {len(bodies)}")
        return bodies, {}
    if op == "split_by_proximity":
        # a WHOLE-PART op (it takes no targets): the pairs and the ratio are the site's own measured clashes
        pairs = [list(k.split("|")) for k in (m.get("clash_by_pair") or {})]
        if live is not None:
            pairs = [p for p in pairs if all(e in live for e in p)]
        if not pairs:
            raise NoParameters("split_by_proximity needs measured face pairs that are still live, and has none")
        diag, worst = m.get("diagonal_mm"), max((m.get("clash_by_pair") or {}).values(), default=None)
        if not diag or worst is None:
            raise NoParameters("split_by_proximity needs a measured gap and the part's diagonal")
        return [], {"face_pairs": [p for p in pairs if len(p) == 2][:MAX_PROXIMITY_PAIRS],
                    "target_ratio": min(0.25, max(0.06, float(worst) / float(diag)))}
    if op in ("defeature", "unify_same_domain", "rebuild_face", "reapproximate", "split_angle",
              "split_continuity", "split_closed", "fix_poles", "to_canonical", "imprint_crossing",
              "rebuild_boundary_edge", "rebuild_seam"):
        if not faces:
            # a mesher defect that re-located onto a BODY (its faces are gone) has no face to aim a face op at
            where = "re-located onto " + "+".join(solids) if solids else "none were located"
            raise NoParameters(f"{op} needs the faces the defect is on, and {where}")
        if op == "unify_same_domain":
            return faces, {"scope": "targets", "keep": []}
        if op == "imprint_crossing":
            # v5 step 9: the crossing faces (the detector's aim); partners per face = the pairs it measured (>= 1)
            measured = len((defect.cause or {}).get("crossing") or ())
            return faces[:MAX_IMPRINT_FACES], {"max_partners": max(1, min(16, measured or IMPRINT_PARTNERS))}
        if op == "defeature":
            # `source: located` is the oracle's own winning parameter for defeature, on 16 distinct files: remove
            # the features at the faces the failure was LOCATED on, rather than whatever the op would find alone
            return faces, {"source": "located"}
        if op == "rebuild_face":
            # v5 step 8: ONE face (the op's schema), the first of the aim - diagnose puts a face whose own boundary
            # crosses itself first. Until now it was handed every face and no continuity, and validate refused it.
            return faces[:1], dict(DEFAULT_PARAMS[op])
        # round 2 (`repair_v5.facet_local`): a cause may carry the op's parameters from its own measurement
        return faces, {**DEFAULT_PARAMS.get(op, {}), **(((defect.cause or {}).get("op_params") or {}).get(op) or {})}
    raise NoParameters(f"no parameter rule for {op!r}")


def _aligned_segments(defect: Defect) -> dict[str, Any]:
    """`split_aligned`'s sector count from v5's measured cause: the smallest count the op accepts at or above the
    segments the facet sag needs to fall under half the gap (`diagnose._close_cause`), capped at the op's largest.
    Nothing measured: {} (the default stands, and the trace says it is a default)."""
    need = (defect.cause or {}).get("segments_needed")
    if not need:
        return {}
    op = REGISTRY.get("split_aligned")
    allowed = sorted((op.params_schema["properties"]["segments"]["enum"]) if op is not None else ())
    if not allowed:
        return {}
    return {"segments": next((s for s in allowed if s >= int(need)), allowed[-1])}


#: WHAT EACH OP CAN AIM AT, as `params_for` itself derives its targets: faces, bodies, edges, or nothing (a
#: whole-part op that takes no targets). One source of truth with `params_for` by construction - an op added there
#: and not here is UNAIMABLE (`aim_reason` says so) rather than mis-aimed, so this fails SHUT.
_AIMS_AT: dict[str, frozenset[str]] = {
    "conformalise": frozenset({"B"}),
    "fuse": frozenset({"B"}),
    "defeature": frozenset({"F"}),
    "unify_same_domain": frozenset({"F"}),
    "rebuild_face": frozenset({"F"}),
    "reapproximate": frozenset({"F"}),
    "split_angle": frozenset({"F"}),
    "split_continuity": frozenset({"F"}),
    "split_closed": frozenset({"F"}),
    # v5 step 5 (invalid_brep_tolerance rule seeds by fault kind): face ops
    "fix_poles": frozenset({"F"}),
    "to_canonical": frozenset({"F"}),
    # v5 round 2: the faces the detector found missing an edge / carrying a seam off its surface
    "rebuild_boundary_edge": frozenset({"F"}),
    "rebuild_seam": frozenset({"F"}),
    # an EDGE op (the free loop's edges, from the measurement): it was {"F"} until v5, so it was handed faces and
    # validate_call refused it every time
    "fill_loop": frozenset({"E"}),
    "rebuild_edge_as_intersection": frozenset({"E"}),
    "remove_small": frozenset({"F"}),
    "split_by_proximity": frozenset(),      # whole-part: its pairs come from the measurement, not from targets
    # v5: whole-part, it finds the near-coaxial groups itself. MISSING until v5, so `can_aim` failed shut and
    # C23's `split_aligned` row - first for overlapping_facets - was filtered out before any seq run could try it.
    "split_aligned": frozenset(),
    # v5 wiring: whole-part ops, or ops whose targets are an optional scope (none = the whole part)
    "sew": frozenset(),
    "make_volume": frozenset(),
    "trim_unbounded": frozenset(),
    "orient_and_close": frozenset(),
    "tighten_tolerances": frozenset(),
    "rescale": frozenset(),
    # v5 step 6 (the conformal pass aims it itself; here so a menu that names it is aimed, not filtered): two bodies
    "tweak": frozenset({"B"}),
    # v5 step 10 (low_quality): the measured sliver/small face, short edge, small solid; refit_cluster = sliver +
    # the neighbour it merges into
    "merge_sliver": frozenset({"F"}),
    "collapse_edge": frozenset({"E"}),
    "drop_solid": frozenset({"B"}),
    "refit_cluster": frozenset({"F"}),
    # v5 step 9 (surface_crossing): the crossing faces; the duplicate pairs ride in the measured cause
    "imprint_crossing": frozenset({"F"}),
    "remove_duplicate_faces": frozenset(),
    # v5 step 8 (overlap_self_intersection): body ops
    "boolean_by_volume": frozenset({"B"}),
    "drop_duplicate_body": frozenset({"B"}),
    "fragment_drop_small": frozenset({"B"}),
    # v5 step 11 (ops/manifold.py): whole-part, parameterised from the open_shell_volume cause
    "sew_gap": frozenset(),
    "split_non_manifold": frozenset(),
    # v5 round 2 (ops/sheets.py, owner-approved): whole-part, the stray sheets it finds itself
    "drop_sheet": frozenset(),
    # round 2 fix G (ops/local_facets.py): the curved faces of the local crossing pairs; the knife pair
    "split_turn": frozenset({"F"}),
    "cut_knife": frozenset({"F"}),
}
#: v5 step 10: ops whose parameters a low_quality CAUSE carries (`repair_v5.quality`), by name, for `params_for`
_CAUSE_PARAMS: dict[str, tuple[str, ...]] = {
    "merge_sliver": ("max_dev_mm", "feature_deg"),
    "collapse_edge": ("max_length_mm",),
    "drop_solid": ("max_volume_fraction",),
    "refit_cluster": ("tol_mm",),
}
#: ops aimed by a MEASUREMENT key when the defect's own entities are not of their kind
_MEASURED_AIM: dict[str, str] = {"fill_loop": "free_loop_edges", "rebuild_edge_as_intersection": "site_edges"}
#: body ops a v5 cause can aim when the defect itself carries faces (`cause_bodies`)
_CAUSE_AIM = frozenset({"conformalise", "fuse", "tweak"})


def cause_bodies(defect: Defect) -> list[StableId]:
    """The bodies v5's diagnosed cause names (`diagnose`: a close pair's `body_a`/`body_b`, or its contact `pairs`),
    as workspace ids, in order, without repeats."""
    cause = defect.cause or {}
    rows = [cause, *[p for p in cause.get("pairs") or [] if isinstance(p, dict)]]
    got = [r.get(k) for r in rows for k in ("body_a", "body_b")]
    return list(dict.fromkeys(b for b in got if isinstance(b, str) and b[:1] == "B"))
_KIND_WORD = {"F": "face", "E": "edge", "B": "body"}


def aim_reason(op: str, defect: Defect) -> str | None:
    """None when `op` can be aimed at what this defect carries, else WHY NOT, in words - for the match log, where an
    unaimable repair is an explicit attempt and never a silent omission."""
    if op.startswith("run_stage:"):
        return None                 # the stage macro takes no targets: it re-conditions the whole state
    wants = _AIMS_AT.get(op)
    if wants is None:
        return f"{op} has no aim rule (seq._AIMS_AT)"
    aim = list((defect.cause or {}).get("aim") or [])
    if not wants or any(str(e)[:1] in wants for e in (*aim, *defect.entities)):
        return None
    key = _MEASURED_AIM.get(op)
    if key and (defect.measurement or {}).get(key):
        return None
    if op in _CAUSE_AIM and cause_bodies(defect):
        return None
    carried = sorted({_KIND_WORD.get(str(e)[:1], str(e)[:1]) for e in defect.entities})
    return (f"{op} aims at {'/'.join(_KIND_WORD[k] for k in sorted(wants))}s; the defect carries "
            f"{'/'.join(carried) + 's' if carried else 'no entities'}" + (f" and no measured {key}" if key else ""))


def can_aim(op: str, defect: Defect) -> bool:
    """Can `op` be aimed at what this defect ACTUALLY CARRIES? (`aim_reason` is None.)

    THE CAP WAS HIDING A WORKING ENTRY. `self_intersection`'s table is (defeature), (defeature, conformalise),
    (defeature, fuse), (conformalise) - and `MAX_ALTERNATIVES` is 3, so plain `conformalise`, its one BODY op
    and a measured oracle winner, was never reached. On wave 202611242110 `relocate` carried both of
    that kind's defects onto BODIES, so all six attempts failed with "defeature needs the faces the defect is
    on, and re-located onto B<k>" while the repair that could have applied sat at index 3.

    A CAP OVER AN UNFILTERED LIST IS A SILENT CORRECTNESS RISK, NOT JUST A BUDGET - the same shape as this
    arm's own analyser reading "applied 0" because param-laden keys hid the three repairs that did apply. So
    the filter runs BEFORE the cap, and an op whose targets this defect cannot supply is not a candidate at all.
    """
    return aim_reason(op, defect) is None


# --- the pick ------------------------------------------------------------------------------------------------

#: What an AI pick is asked for and what it may answer: ONE repair, out of the candidates this arm names. The
#: model never invents an op here and never sees a file name (B21b's rule): it is given the defect kind, the
#: measurement, and the list to choose from.
AIPick = Callable[["Defect", Sequence[Repair]], Repair | None]

#: v5's per-defect repair (the tournament): (pool, current state, the defect re-located onto it, *, counters, step)
#: -> the state to carry on from. It tallies the defect fixed/skipped on `counters` itself, as `_repair_one` does.
RepairDefect = Callable[..., Workspace]

#: v5's round pre-pass (the conformal assembly pass): (pool, best state, its full verdict, *, counters, step) ->
#: (the kept state, its full verdict), or None when it kept nothing
PreRound = Callable[..., "tuple[Workspace, Any] | None"]

#: v5's diagnosis hook: (pool, state, its verdict, its sites, its defects) -> the defects with their class, cause
#: and rule filled, in repair order (`repair_v5.diagnose.diagnose`)
Diagnose = Callable[[Any, Workspace, Any, list[Site], list[Defect]], list[Defect]]


@dataclass
class Pick:
    repair: Repair | None
    #: `table`, `ai`, or `none` - what decided, so a row can separate the table's wins from the model's
    by: str
    why: str
    candidates: tuple[Repair, ...] = ()


def pick_repair(defect: Defect, *, table: dict[str, tuple[Repair, ...]] | None = None,
                ai_pick: AIPick | None = None, applies: Callable[[str], bool] | None = None,
                key: str | None = None) -> Pick:
    """ONE repair for `defect`. The table decides where it has an unambiguous leader; the AI decides only where
    the table is empty, or where its top two entries are equally well evidenced.

    "Equally well evidenced" is the tie the design means: two repairs the table cannot separate. The table is
    already ranked by distinct files, so a tie here is a genuine one, not an artefact of the ordering."""
    # the table is keyed on the defect's CODE (`kind` is only validity/mesher); v5 keys it on the defect's
    # CLASS instead (`repair_v5.loop.table_key`), handed in as `key`
    kind = key if key is not None else defect.code
    rows = (table if table is not None else RULE_TABLE).get(kind, ())
    # BOTH filters run BEFORE the cap: the op must exist, AND it must be aimable at what this defect carries
    rows = tuple(r for r in rows
                 if (applies is None or all(applies(op) for op in r)) and all(can_aim(op, defect) for op in r))
    if not rows:
        if ai_pick is None:
            return Pick(None, "none", f"no rule for {kind!r} and no model to ask", ())
        chosen = ai_pick(defect, ())
        return Pick(chosen, "ai" if chosen else "none", f"no rule for {kind!r}; the model was asked", ())
    if len(rows) == 1:
        return Pick(rows[0], "table", f"the only rule for {kind!r}", rows)
    if _tied(kind, rows[0], rows[1]) and ai_pick is not None:
        chosen = ai_pick(defect, rows)
        if chosen in rows:
            return Pick(chosen, "ai", f"the top rules for {kind!r} are tied; the model chose", rows)
        # a model that answers something it was not offered is ignored, loudly, and the table's order stands
        return Pick(rows[0], "table", f"the model's answer {chosen!r} was not one of the candidates", rows)
    return Pick(rows[0], "table", f"the top rule for {kind!r}", rows)


#: Which pairs of top entries count as tied. The table carries its evidence in its ORDER, not in numbers at
#: runtime, so a tie is declared only where this module knows the two are equally evidenced; the generated table
#: (`rule_table_from_winners`) is what re-derives that, and until it is passed in, nothing is tied.
TIED: dict[str, tuple[tuple[Repair, Repair], ...]] = {
    "body_overlap": ((("conformalise",), ("fuse",)),),      # 5 files each; conformalise leads only on paths
    "open_shell": ((("defeature",), ("remove_small",)),),   # 3 files each; defeature leads only on paths
}


def _tied(kind: str, first: Repair, second: Repair) -> bool:
    return any({first, second} == {a, b} for a, b in TIED.get(kind, ()))


# --- what the row says ---------------------------------------------------------------------------------------


class _WhyCounter(collections.Counter):
    """`SeqCounters.skipped_why`: a Counter that also writes the key it is counted under into the newest `skipped`
    history entry that has no reason yet, so each skipped defect carries its own why (the counts are unchanged)."""

    def __init__(self, initial: Any = None, /, *, history: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self._history: list[dict[str, Any]] = history if history is not None else []
        if initial:
            # a mapping, or the (key, count) pairs `dataclasses.asdict` rebuilds a dict field from
            super().update(dict(initial) if not isinstance(initial, collections.abc.Mapping) else initial)

    def __setitem__(self, key: Any, value: Any) -> None:
        if value > self.get(key, 0):
            for entry in reversed(self._history):
                if entry["what"] == "skipped":
                    if entry["why"] is None:
                        entry["why"] = key
                    break
        super().__setitem__(key, value)

    def __reduce__(self) -> Any:   # pickles (and copies) as a plain Counter: the history link is per run
        return (collections.Counter, (dict(self),))


@dataclass
class SeqCounters:
    """The row fields this arm owes (seq-arm-design.md, "what is measured")."""

    full_checks: int = 0
    local_checks: int = 0
    rounds: int = 0
    ai_picks: int = 0
    #: body_overlap clusters are repaired pair by pair, so the pairs are counted beside the defects: a cluster
    #: that ended 3 pairs of 4 is a SKIPPED defect with 3 fixed pairs, and never reads as a repaired part
    pairs_fixed: int = 0
    pairs_skipped: int = 0
    #: how many repairs this round kept (reset by the loop at the end of each round)
    kept_in_round: int = 0
    #: defects that could no longer be located after an earlier repair in the same round, and were gone when
    #: re-checked: not a failure and not a repair of their own (seq-arm-design.md, "entity tracking")
    resolved_by_earlier_repair: int = 0
    #: full verdicts that came back INCONCLUSIVE (a panel member failed for pool reasons). They say nothing about
    #: the file, so they can neither rank a state nor end a round's work: the arm stops and the row says why.
    inconclusive_verdicts: int = 0
    #: failures the diagnosis found to be the checker's own (a crash or timeout with nothing located): not defects
    checker_failed: int = 0
    #: why the loop stopped on the FILE wall (a panel or local mesh not started: `PanelWallExhausted`), "" if it did
    #: not. Not a row field: `SeqOutcome.stopped_on_wall` is what the row reads.
    wall_stop: str = ""
    #: CRS v5 r2 (time management): the loop's state a per-defect `repair_defect` reads to size its slice of the wall
    #: - how many defects of this round are still to be tried, this one included - and the flag it sets when its
    #: slice (not the defect) ended it, so the loop gives that defect a second turn with what the round has left
    defects_left: int = 0
    sliced: bool = False
    #: seconds per phase of the loop (pre_round, sites, diagnose, repairs, full), for the row: where the wall went
    phase_s: collections.defaultdict[str, float] = field(default_factory=lambda: collections.defaultdict(float))
    found: collections.Counter[str] = field(default_factory=collections.Counter)
    fixed: collections.Counter[str] = field(default_factory=collections.Counter)
    skipped: collections.Counter[str] = field(default_factory=collections.Counter)
    #: why defects were skipped, by reason, so "the verifier was not there" never reads as "unrepairable"
    skipped_why: collections.Counter[str] = field(default_factory=collections.Counter)
    #: seconds in local verification and in the full checks, kept apart: the arm's whole claim is the ratio
    local_s: float = 0.0
    full_s: float = 0.0

    #: v5: the same three tallies by DEFECT CLASS (empty when the loop was not given a diagnosis)
    found_class: collections.Counter[str] = field(default_factory=collections.Counter)
    fixed_class: collections.Counter[str] = field(default_factory=collections.Counter)
    skipped_class: collections.Counter[str] = field(default_factory=collections.Counter)

    #: every tally, in order, per defect: {"what": found|fixed|skipped|resolved|unskipped, "defect", "why"}. The
    #: product's per-problem result (`repair_v5.result`) reads a defect's LAST entry; `why` is the `skipped_why` key
    #: the same step counted (filled by `_WhyCounter` the moment it is counted)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.skipped_why = _WhyCounter(self.skipped_why, history=self.history)

    def tally(self, what: str, defect: Defect) -> None:
        getattr(self, what)[defect.code] += 1
        if defect.cls:
            getattr(self, f"{what}_class")[defect.cls] += 1
        self.history.append({"what": what, "defect": defect, "why": None})
        if what in ("fixed", "skipped"):
            EV.emit("defect_result", what=what, **EV.defect_fields(defect))

    def row(self) -> dict[str, Any]:
        classes = ({"classes_found": dict(self.found_class), "classes_fixed": dict(self.fixed_class),
                    "classes_unrepaired": dict(self.skipped_class)} if self.found_class else {})
        return {**classes, "full_checks": self.full_checks, "local_checks": self.local_checks, "rounds": self.rounds,
                "ai_picks": self.ai_picks, "defects_found": dict(self.found), "defects_fixed": dict(self.fixed),
                "defects_skipped": dict(self.skipped), "skipped_why": dict(self.skipped_why),
                "overlap_pairs_fixed": self.pairs_fixed, "overlap_pairs_skipped": self.pairs_skipped,
                "inconclusive_verdicts": self.inconclusive_verdicts,
                "checker_failed": self.checker_failed,
                "resolved_by_earlier_repair": self.resolved_by_earlier_repair,
                "local_s": round(self.local_s, 1), "full_s": round(self.full_s, 1),
                **({"phase_s": {k: round(v, 1) for k, v in self.phase_s.items()}} if self.phase_s else {})}


#: the `skipped_why` key of a defect whose tournament its slice of the wall cut short (CRS v5 r2), and which got no
#: second turn either: a time stop, never a failed repair
SLICE = "slice"


def _untally_skip(counters: SeqCounters, defect: Defect, why: str) -> None:
    """Take back one `tally("skipped", defect)` and its `skipped_why[why]` (a defect queued for a second turn)."""
    counters.history.append({"what": "unskipped", "defect": defect, "why": why})
    for counter, key in ((counters.skipped, defect.code), (counters.skipped_class, defect.cls),
                         (counters.skipped_why, why)):
        if key and counter.get(key):
            counter[key] -= 1
            if not counter[key]:
                del counter[key]


# --- the trace the owner asked for (note 2): one readable line per step ---------------------------------------


def defect_line(defect: Defect, *, pair: tuple[StableId, StableId] | None = None) -> str:
    """`body_overlap on B2+B5 (clash 0.42 mm)` - the defect third of the step line: kind, WHERE, measurement."""
    where = "+".join(pair) if pair else ("+".join(defect.entities[:4]) + ("+..." if len(defect.entities) > 4 else ""))
    m = defect.measurement or {}
    bits: list[str] = []
    if pair and (m.get("clash_by_pair") or {}).get(pair_key(*pair)) is not None:
        bits.append(f"clash {m['clash_by_pair'][pair_key(*pair)]:.3g} mm")
    elif m.get("clash"):
        bits.append(f"clash {m['clash']['dist_mm']:.3g} mm")
    if m.get("narrowest_face"):
        bits.append(f"narrowest face {m['narrowest_face']['id']} {m['narrowest_face']['width_mm']:.3g} mm")
    if m.get("free_loops"):
        bits.append(f"{m['free_loops']} free loop(s)")
    if defect.member:
        bits.append(f"member {defect.member}")
    if defect.cls:
        bits.append(f"class {defect.cls}: {defect.rule}" if defect.rule else f"class {defect.cls}")
    return f"{defect.code} on {where or '?'}" + (f" ({', '.join(bits)})" if bits else "")


def repair_line(repair: Repair, params: Sequence[dict], pick: Pick) -> str:
    """`remove_small{max_width_mm=11.97} [table: the top rule for open_shell]` - the repair third: op, PARAMS,
    and who chose it with why, so a reader never has to guess whether the table or the model decided."""
    steps = " + ".join(
        f"{op}{{{', '.join(f'{k}={_short(v)}' for k, v in sorted(p.items()))}}}" if p else op
        for op, p in zip(repair, list(params) + [{}] * len(repair), strict=False))
    return f"{steps} [{pick.by}: {pick.why}]"


def _short(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, dict):
        return "{...}" if value else "{}"
    return str(value)


# --- the loop ------------------------------------------------------------------------------------------------


@dataclass
class _Applied:
    ws: Workspace
    diffs: list[Diff]
    touched: tuple[StableId, ...]
    params: list[dict]


#: Every way ONE repair can fail to apply. A ValueError from the workspace's own history check
#: (`workspace._check_record`) belongs here too: wave 202611242110 excluded 0051_00518960 in BOTH seq arms
#: because that one escaped - the arm had already repaired two pairs of a cluster, and the whole FILE was
#: thrown away over the third defect. Same shape as the ArmRow crashes: the expensive work done, the result
#: discarded by a cheap step at the end. A repair that cannot be applied is a SKIPPED DEFECT, never a lost file.
APPLY_FAILED: tuple[type[BaseException], ...] = (NoParameters, OpDeclined, OpFailed, UnknownTarget, ValueError)


def apply_failure_reason(exc: BaseException) -> str:
    """The `skipped_why` key for a repair that did not apply, keeping the interesting cases apart.

    `record_mismatch` is the workspace saying its own ids no longer describe its BREP (a history row's
    fingerprint against `ws.ids`). It is kept distinct from `NoParameters` - which is this arm REFUSING to
    invent a number - because the two mean opposite things about whose fault it is.
    """
    if isinstance(exc, NoParameters):
        return "NoParameters"
    if isinstance(exc, ValueError) and "the record is for a different shape" in str(exc):
        return "record_mismatch"
    if isinstance(exc, ValueError) and "history record" in str(exc):
        return "record_malformed"
    return type(exc).__name__


class DeadlinePool:
    """`pool` for one bounded phase: each call's timeout clipped to what is left before `until`; a call asked for
    after `until` is NOT started and answers as a timeout (its caller degrades, as for any failed measurement or a
    timed-out op). The seconds spent per kernel function are counted in `spent` (`skipped:<fn>` counts the calls not
    started). Used by the v5 diagnosis window, the staged healer's share and `apply_repair` (CRS v5 r2/r2h)."""

    def __init__(self, pool: Any, until: float, clock: Callable[[], float], spent: dict[str, float], *,
                 why: str = "not started: the diagnosis share of the wall is spent") -> None:
        self._pool, self._until, self._clock, self.spent, self._why = pool, until, clock, spent, why

    def call(self, fn_name: str, payload: dict, *, timeout_s: float) -> Any:
        left = self._until - self._clock()
        if left <= 0:
            from meshpipeline.cad.repair_v4.worker_pool import CallResult

            self.spent["skipped:" + fn_name] = self.spent.get("skipped:" + fn_name, 0.0) + 1
            return CallResult(ok=False, value=None, error=self._why, status="timeout", seconds=0.0)
        t0 = self._clock()
        try:
            return self._pool.call(fn_name, payload, timeout_s=max(1.0, min(timeout_s, left)))
        finally:
            self.spent[fn_name] = self.spent.get(fn_name, 0.0) + self._clock() - t0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


def apply_repair(pool: Any, ws: Workspace, repair: Repair, defect: Defect, *, timeout_s: float,
                 pair: tuple[StableId, StableId] | None = None,
                 run_stage: Callable[[Any, Workspace, str, dict, float], _Applied] | None = None,
                 overrides: Sequence[dict] = ()) -> _Applied:
    """Apply `repair`'s ops in order, on this defect's own entities (or, in a cluster, on `pair`'s two bodies).

    `overrides[i]` is merged over step i's derived parameters (v5's parameter VARIANTS around the measured value);
    the merged call is validated like any other, so a variant can never pass a value the op refuses.

    Raises what the ops raise (`OpDeclined`, `OpFailed`, `UnknownTarget`) and `NoParameters`: the caller records
    it against the defect and moves to the next alternative.

    BOTH STEPS THEN ONE CHECK. A two-step recipe is "do this, then re-condition what it touched", and the table
    exists because the oracle's wins are chains whose FIRST step alone changes nothing measurable - verifying
    between the steps would throw away exactly those. The pair is kept or reverted as a unit.
    """
    current, diffs, touched, used = ws, [], [], []  # type: ignore[var-annotated]
    here = defect
    # CRS v5 r2h: `timeout_s` bounds the WHOLE repair, not each kernel call in it: a two-step recipe, or an op that
    # retries its Boolean (round 2 D's robust Booleans: ~650 s on 3e7c2a1197c3's clusters), cannot add its calls'
    # timeouts up past the caller's slice. A call asked for after it is not started and answers as a timeout.
    pool = DeadlinePool(pool, time.monotonic() + timeout_s, time.monotonic, {},
                        why="not started: the repair's share of the wall is spent")
    for step_i, op in enumerate(repair):
        if step_i:
            # THE SECOND STEP'S TARGETS ARE NOT THE FIRST'S. Ops rename faces: the first end-to-end run had
            # `remove_small` replace F3 with F16 and then `split_angle` refused F3 ("use the new ids"), which
            # killed every two-step recipe on that file. `relocate` carries the defect's entities (and
            # pairs) through the ops' own history, which is exactly the question it exists to answer.
            moved = relocate(pool, ws, current, here)
            if moved is None:
                raise NoParameters(f"the defect could not be carried to {op!r}: nothing of it is left to aim at")
            here = moved
        targets, params = params_for(op, here, pair=pair, live=set(current.ids))
        if step_i < len(overrides) and overrides[step_i]:
            params = {**params, **overrides[step_i]}
        used.append(params)
        if op.startswith("run_stage:"):
            if run_stage is None:
                raise NoParameters(f"{op} needs the staged healer, which this caller did not provide")
            out = run_stage(pool, current, params["stage"], params["params"], timeout_s)
            current, step_diffs = out.ws, out.diffs
        else:
            errors = validate_call(op, targets, params, current)
            if errors:
                raise NoParameters(f"{op} rejected the derived parameters: {errors[0]}")
            current, diff = REGISTRY[op].apply(pool, current.fork(), list(targets), dict(params),
                                               timeout_s=timeout_s)
            declined = diff.params.get("declined") if isinstance(diff.params, dict) else None
            if declined is not None:
                # the body ops decline with an UNCHANGED workspace and the reason in the Diff, as stages reads it
                detail = diff.params.get("detail")
                raise OpDeclined(f"{declined}: {detail}" if detail else str(declined))
            step_diffs = [diff]
        diffs.extend(step_diffs)
        for d in step_diffs:
            touched.extend(d.generated or {})
            touched.extend(d.modified or {})
        touched.extend(targets)
    return _Applied(current, diffs, tuple(dict.fromkeys(touched)), used)


@dataclass
class SeqRound:
    """One round, for the row: its steps as readable lines, and what the end-of-round FULL check said."""

    index: int
    steps: list[str] = field(default_factory=list)
    kept: int = 0
    full_check: str | None = None


def repair_sequentially(
    pool: Any,
    start: Workspace,
    original: str,
    *,
    judge: Any,
    sites_of: Callable[[Any, Workspace, Any], list[Site]],
    verify: Callable[..., LocalResult] = verify_local,
    ai_pick: AIPick | None = None,
    run_stage: Callable[[Any, Workspace, str, dict, float], _Applied] | None = None,
    table: dict[str, tuple[Repair, ...]] | None = None,
    start_verdict: Any = None,
    op_timeout_s: float = 300.0,
    local_timeout_s: float = 600.0,
    max_rounds: int = MAX_ROUNDS,
    wall_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    trace: Any = None,
    diagnose: Diagnose | None = None,
    key_of: Callable[[Defect], str] | None = None,
    max_alternatives: int = MAX_ALTERNATIVES,
    repair_defect: RepairDefect | None = None,
    pre_round: PreRound | None = None,
    panel_credit_s: float = 0.0,
) -> SeqOutcome:
    """The arm. One defect at a time, one repair each, local verification, ONE full panel per round.

    A repair is KEPT only on a local pass (`gone` and no new hard code). Anything else - still there, a new
    defect, an op that declined, a parameter that cannot be derived, a check that could not be had - reverts it
    and records the defect as skipped with its reason. The end-of-round panel is the measurement: a local pass is
    not a whole-part pass, which is what the rounds are for.

    `repair_defect` (v5: `repair_v5.tournament`) REPLACES the per-defect alternatives (`_repair_one` /
    `_repair_cluster`): it is handed each defect, already re-located onto the current state, and returns the state
    to carry on from. The ordering, the relocation, the rounds and the panel stay here, one mechanism for both.

    `pre_round` (v5 step 6: `repair_v5.conformal.ConformalPass`) runs at the top of every round, before the round is
    diagnosed: a (state, full verdict) it returns is the round's start and the arm's best (it judged itself with a
    full check, which it counts); None leaves the round where it was.
    """
    counters = SeqCounters()
    rounds: list[SeqRound] = []
    best = start
    verdict = start_verdict if start_verdict is not None else judge.full(pool, start, original)
    if start_verdict is None:
        counters.full_checks += 1
    t0 = clock()

    def scored(v: Any) -> Any | None:
        """`judge.score(v)`, or None when the verdict is INCONCLUSIVE.

        An inconclusive verdict is a panel member failing for POOL reasons: it says nothing about the file, so
        `score` refuses it by design (assemble.score). Found by the first end-to-end run on a real part, where
        this host has no netgen interpreter: the arm crashed instead of saying so. It cannot rank a state it
        could not measure, so it stops and the row carries the count."""
        try:
            return judge.score(v)
        except InconclusiveVerdict:
            counters.inconclusive_verdicts += 1
            return None

    best_score = scored(verdict)
    if best_score is None:
        counters.skipped_why["start_verdict_inconclusive"] += 1
        return SeqOutcome(best=best, verdict=verdict, counters=counters, rounds=rounds)

    def out_of_wall() -> bool:
        # CRS v5 r2h: the full checks are counted in their own share (`panel_credit_s`), not in `wall_s`
        credit = min(counters.full_s, panel_credit_s)
        return bool(counters.wall_stop) or (wall_s is not None and (clock() - t0) >= wall_s + credit)

    def into(this: SeqRound) -> Callable[[str], None]:
        """A `step` bound to THIS round (never a closure over the loop variable)."""
        def step(line: str) -> None:
            this.steps.append(line)
            if trace is not None:
                trace.record("seq_step", round=this.index, line=line)
        return step

    for index in range(1, max_rounds + 1):
        if out_of_wall() or bool(getattr(verdict, "clean", False)):
            break
        this = SeqRound(index=index)
        rounds.append(this)
        counters.rounds = index
        EV.enter("conformal" if pre_round is not None else "diagnose", round=index)
        if pre_round is not None:
            phase_t = clock()
            got = pre_round(pool, best, verdict, counters=counters, step=into(this))
            counters.phase_s["pre_round"] += clock() - phase_t
            got_score = scored(got[1]) if got is not None else None
            if got is not None and got_score is not None:
                best, verdict, best_score = got[0], got[1], got_score
                if bool(getattr(verdict, "clean", False)):
                    this.full_check = "clean after the round's pre-pass"
                    break
        EV.enter("diagnose", round=index, **EV.NO_DEFECT)
        phase_t = clock()
        sites = sites_of(pool, best, verdict)
        defects = _with_diagonal(pool, best, defects_of(verdict, sites, best))
        counters.phase_s["sites"] += clock() - phase_t
        if diagnose is not None:
            # v5: every defect gets its CLASS and CAUSE before any repair (`repair_v5.diagnose.diagnose`)
            phase_t = clock()
            defects = diagnose(pool, best, verdict, sites, defects)
            counters.phase_s["diagnose"] += clock() - phase_t
            for d in [d for d in defects if d.cls == CHECKER_FAILED]:
                counters.checker_failed += 1
                EV.emit("checker_failed", severity="WARNING", reason_key=CHECKER_FAILED, reason=d.rule, **EV.defect_fields(d))
                into(this)(f"{d.code} at {d.id} -> {CHECKER_FAILED} [{d.rule}]")
            defects = [d for d in defects if d.cls != CHECKER_FAILED]
        for d in defects:
            counters.tally("found", d)
        EV.emit("diagnosed", round=index, defects=len(defects),
                classes=dict(collections.Counter(str(d.cls or d.code) for d in defects)))
        current = best
        diagnosed_on = best      # every defect in this list was diagnosed on THIS state
        phase_t = clock()
        # CRS v5 r2: a defect whose tournament its SLICE of the wall cut short (`counters.sliced`) gets ONE more turn
        # after every other defect of the round had its own, with whatever the round has left
        queue: list[tuple[Defect, bool]] = [(d, False) for d in defects]
        at = 0
        while at < len(queue):
            listed, second_turn = queue[at]
            at += 1
            # first turns share the round's wall among themselves; second turns share what the first turns left
            counters.defects_left = sum(1 for _, again in queue[at - 1:] if again == second_turn)
            if out_of_wall():
                counters.tally("skipped", listed)
                counters.skipped_why[SLICE if second_turn else "wall"] += 1
                into(this)(f"{defect_line(listed)} -> "
                           + ("no second turn [the class loop's wall]" if second_turn
                              else "not attempted [the file's wall]"))
                continue
            # RE-LOCATE BEFORE REPAIRING (seq-arm-design.md). An earlier repair in this round renamed faces, so
            # the ids this defect was diagnosed with can be stale: the first end-to-end run had `remove_small`
            # replace F3 with F16 and the NEXT defect was refused for naming F3. `relocate` carries it.
            if (listed.measurement or {}).get("unmapped_layer1_entities") and not listed.entities:
                # layer 1 named solids this workspace's body index cannot place (see `body_ids_of`). Its OWN
                # reason, so this can never again read as "the ids died in an earlier repair".
                counters.tally("skipped", listed)
                counters.skipped_why["layer1_entity_unmapped"] += 1
                into(this)(f"{defect_line(listed)} -> not attempted [layer-1 entity could not be mapped to a "
                           f"body: {', '.join(map(str, listed.measurement['unmapped_layer1_entities']))}]")
                continue
            defect = listed
            if current is not diagnosed_on:
                moved = relocate(pool, diagnosed_on, current, listed)
                if moved is None:
                    # nothing of it is left to aim at. RE-CHECKED, never assumed (the design's rule): gone there ->
                    # an earlier repair took it with it; still there -> unrepaired, with its own reason
                    _recheck_lost(pool, diagnosed_on, current, listed, verify=verify, counters=counters,
                                  step=into(this), timeout_s=local_timeout_s)
                    continue
                defect = moved
            EV.enter("tournament", problem_index=at, problem_total=len(queue), second_turn=second_turn,
                     **EV.defect_fields(defect))
            if repair_defect is not None:
                counters.sliced = False
                current = repair_defect(pool, current, defect, counters=counters, step=into(this))
                if counters.sliced and not second_turn and not counters.wall_stop:
                    # its slice ran out, not its menu: un-count the skip and queue its second turn at the round's end
                    _untally_skip(counters, listed, SLICE)
                    queue.append((listed, True))
                    into(this)(f"{defect_line(listed)} -> its slice of the wall ran out: a second turn at the "
                               f"round's end")
                counters.sliced = False
                continue
            if defect.code == "body_overlap" and defect.pairs:
                current = _repair_cluster(pool, current, defect, counters=counters, table=table, ai_pick=ai_pick,
                                          verify=verify, run_stage=run_stage, op_timeout_s=op_timeout_s,
                                          local_timeout_s=local_timeout_s, step=into(this), key_of=key_of,
                                          cap=max_alternatives)
                continue
            current = _repair_one(pool, current, defect, counters=counters, table=table, ai_pick=ai_pick,
                                  verify=verify, run_stage=run_stage, op_timeout_s=op_timeout_s,
                                  local_timeout_s=local_timeout_s, step=into(this), key_of=key_of,
                                  cap=max_alternatives)
        counters.phase_s["repairs"] += clock() - phase_t
        this.kept = counters.kept_in_round
        counters.kept_in_round = 0
        if this.kept == 0:
            this.full_check = "not run: the round kept nothing"
            break                       # the next round would diagnose the same state and ask the same questions
        full_t = clock()
        EV.enter("round_panel", **EV.NO_DEFECT)
        try:
            new_verdict = judge.full(pool, current, original)
        except PanelWallExhausted as why:
            # the FILE's wall, not a verdict: the round's work cannot be judged, the better state stands
            EV.wall_stop("round_panel", why)
            counters.wall_stop = str(why)
            this.full_check = f"not run: the file's wall ({why})"
            break
        counters.full_checks += 1
        counters.full_s += clock() - full_t
        new_score = scored(new_verdict)
        if new_score is None:
            # the round's work cannot be judged: the better state stands and the row says the check was lost
            this.full_check = "inconclusive: a panel member failed for pool reasons"
            break
        improved = new_score > best_score
        this.full_check = (f"{'clean' if getattr(new_verdict, 'clean', False) else 'not clean'}, "
                           f"score {tuple(new_score)}, {'improved' if improved else 'no better'} "
                           f"({clock() - full_t:.1f} s)")
        if trace is not None:
            trace.record("seq_round", index=index, kept=this.kept, full_check=this.full_check)
        if improved:
            best, verdict, best_score = current, new_verdict, new_score
        else:
            break                       # the round's work did not improve the part: keep the better state
    return SeqOutcome(best=best, verdict=verdict, counters=counters, rounds=rounds)


@dataclass
class SeqOutcome:
    best: Workspace
    verdict: Any
    counters: SeqCounters
    rounds: list[SeqRound]

    @property
    def stopped_on_wall(self) -> bool:
        return bool(self.counters.wall_stop)

    def lines(self) -> list[str]:
        """The whole file's trace, readable top to bottom: every step, and each round's full check."""
        out: list[str] = []
        for r in self.rounds:
            out.append(f"round {r.index}:")
            out.extend(f"  {line}" for line in r.steps)
            out.append(f"  full check: {r.full_check}")
        return out


def _wall_stop(counters: SeqCounters, defect: Defect, step: Callable[[str], None], why: BaseException, *,
               pair: tuple[StableId, StableId] | None = None) -> None:
    """A local mesh not started for lack of FILE wall: the loop stops (`out_of_wall`), the defect is unrepaired on
    the wall - never a failed repair, never a bad part."""
    counters.wall_stop = str(why)
    EV.wall_stop("local_check", why, defect)
    counters.tally("skipped", defect)
    counters.skipped_why["wall"] += 1
    step(f"{defect_line(defect, pair=pair)} -> not checked: the file's wall [{why}]")


def _recheck_lost(pool: Any, diagnosed_on: Workspace, current: Workspace, defect: Defect, *,
                  verify: Callable[..., LocalResult], counters: SeqCounters, step: Callable[[str], None],
                  timeout_s: float) -> None:
    """A defect `relocate` could not carry past an earlier repair, checked again on the region that repair changed
    (`verify_local` re-locates it itself and, with nothing located, judges the changed region)."""
    try:
        local = verify(pool, diagnosed_on, current, defect, timeout_s=timeout_s)
    except PanelWallExhausted as why:
        _wall_stop(counters, defect, step, why)
        return
    except UNAVAILABLE as why:
        counters.tally("skipped", defect)
        counters.skipped_why["local_check_unavailable"] += 1
        step(f"{defect_line(defect)} -> lost after an earlier repair, NOT CHECKED: {type(why).__name__}: {why}")
        return
    counters.local_checks += 1
    counters.local_s += float(local.seconds or 0.0)
    if local.gone and not local.new_hard:
        counters.resolved_by_earlier_repair += 1
        counters.history.append({"what": "resolved", "defect": defect, "why": None})
        EV.emit("defect_result", what="resolved", **EV.defect_fields(defect))
        step(f"{defect_line(defect)} -> resolved_by_earlier_repair [nothing of it is left to locate; "
             f"re-checked: {local.line()}]")
        return
    counters.tally("skipped", defect)
    counters.skipped_why["unlocatable_after_earlier_repair"] += 1
    step(f"{defect_line(defect)} -> unrepaired [an earlier repair moved it out of reach; re-checked: {local.line()}]")


def _with_diagonal(pool: Any, ws: Workspace, defects: list[Defect]) -> list[Defect]:
    """Every defect's measurement carries the PART's diagonal, measured once per round.

    Only `open` sites carry `diagonal_mm` of their own (sites.py), so a mesher defect had none and
    `split_by_proximity` - whose target ratio is the measured gap over the part - could not be parameterised at
    all. The diagonal is a property of the part, not of the site, so it is measured here and shared. A part whose
    diagonal cannot be measured leaves the defects exactly as they were: the op is then skipped, not guessed."""
    if not defects or all((d.measurement or {}).get("diagonal_mm") for d in defects):
        return defects
    try:
        from meshpipeline.cad.repair_v4.stages import diagonal_mm
        diag = float(diagonal_mm(pool, ws))
    except Exception:  # noqa: BLE001 - an unmeasurable diagonal is not a verdict and never ends the round
        return defects
    if not diag or diag <= 0.0:
        return defects
    return [d if (d.measurement or {}).get("diagonal_mm")
            else dataclasses.replace(d, measurement={**(d.measurement or {}), "diagonal_mm": diag})
            for d in defects]


def _alternatives(pick: Pick, cap: int = MAX_ALTERNATIVES) -> list[Repair]:
    first = [pick.repair] if pick.repair is not None else []
    return [*first, *[r for r in pick.candidates if r != pick.repair]][:cap]


def _pick_for(defect: Defect, table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
              counters: SeqCounters, key_of: Callable[[Defect], str] | None = None) -> Pick:
    pick = pick_repair(defect, table=table, ai_pick=ai_pick,
                       applies=lambda op: op.startswith("run_stage:") or op in REGISTRY,
                       key=key_of(defect) if key_of is not None else None)
    if pick.by == "ai":
        counters.ai_picks += 1
    return pick


def _repair_one(pool: Any, ws: Workspace, defect: Defect, *, counters: SeqCounters,
                table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
                verify: Callable[..., LocalResult], run_stage: Any, op_timeout_s: float,
                local_timeout_s: float, step: Callable[[str], None],
                key_of: Callable[[Defect], str] | None = None, cap: int = MAX_ALTERNATIVES) -> Workspace:
    """One defect, one repair at a time until one is kept or the alternatives run out. Returns the workspace to
    carry on from: the repaired one if a repair was kept, otherwise the one it was given (the revert)."""
    pick = _pick_for(defect, table, ai_pick, counters, key_of)
    if not pick.repair:
        counters.tally("skipped", defect)
        counters.skipped_why["no_rule"] += 1
        step(f"{defect_line(defect)} -> no repair [{pick.why}]")
        return ws
    for repair in _alternatives(pick, cap):
        try:
            applied = apply_repair(pool, ws, repair, defect, timeout_s=op_timeout_s, run_stage=run_stage)
        except APPLY_FAILED as why:
            counters.skipped_why[apply_failure_reason(why)] += 1
            step(f"{defect_line(defect)} -> {repair_line(repair, [], pick)} -> not applied: "
                 f"{apply_failure_reason(why)}: {why}")
            continue
        try:
            local = verify(pool, ws, applied.ws, defect, list(applied.touched), timeout_s=local_timeout_s)
        except PanelWallExhausted as why:
            _wall_stop(counters, defect, step, why)
            return ws
        except UNAVAILABLE as why:
            counters.tally("skipped", defect)
            counters.skipped_why["local_check_unavailable"] += 1
            step(f"{defect_line(defect)} -> {repair_line(repair, applied.params, pick)} -> "
                 f"NOT CHECKED: {type(why).__name__}: {why}")
            return ws                   # nothing would check the next alternative either
        counters.local_checks += 1
        counters.local_s += float(local.seconds or 0.0)
        step(f"{defect_line(defect)} -> {repair_line(repair, applied.params, pick)} -> {local.line()}")
        if local.gone and not local.new_hard:
            counters.tally("fixed", defect)
            counters.kept_in_round += 1
            return applied.ws
    counters.tally("skipped", defect)
    return ws


def _repair_cluster(pool: Any, ws: Workspace, defect: Defect, *, counters: SeqCounters,
                    table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
                    verify: Callable[..., LocalResult], run_stage: Any, op_timeout_s: float,
                    local_timeout_s: float, step: Callable[[str], None],
                    key_of: Callable[[Defect], str] | None = None, cap: int = MAX_ALTERNATIVES) -> Workspace:
    """A body_overlap CLUSTER, repaired PAIR BY PAIR (owner note 1).

    One local check per pair, and a pair that cannot be repaired is skipped and recorded WITHOUT blocking the
    rest of the cluster - the whole point of clustering rather than one defect per pair. The cluster counts as
    fixed only if every pair ended gone; otherwise it is a skipped defect with its pairs counted separately, so
    "3 of 4 pairs" never reads as a repaired part.
    """
    current = ws
    gone = 0
    for pair in defect.pairs:
        pick = _pick_for(defect, table, ai_pick, counters, key_of)
        if not pick.repair:
            counters.pairs_skipped += 1
            counters.skipped_why["no_rule"] += 1
            step(f"{defect_line(defect, pair=pair)} -> no repair [{pick.why}]")
            continue
        kept = False
        for repair in _alternatives(pick, cap):
            try:
                applied = apply_repair(pool, current, repair, defect, timeout_s=op_timeout_s, pair=pair,
                                       run_stage=run_stage)
            except APPLY_FAILED as why:
                counters.skipped_why[apply_failure_reason(why)] += 1
                step(f"{defect_line(defect, pair=pair)} -> {repair_line(repair, [], pick)} -> not applied: "
                     f"{apply_failure_reason(why)}: {why}")
                continue
            try:
                local = verify(pool, current, applied.ws, defect, list(applied.touched), pair=pair,
                               timeout_s=local_timeout_s)
            except PanelWallExhausted as why:
                counters.pairs_skipped += 1
                _wall_stop(counters, defect, step, why, pair=pair)
                return current
            except UNAVAILABLE as why:
                counters.pairs_skipped += 1
                counters.skipped_why["local_check_unavailable"] += 1
                step(f"{defect_line(defect, pair=pair)} -> {repair_line(repair, applied.params, pick)} -> "
                     f"NOT CHECKED: {type(why).__name__}: {why}")
                break
            counters.local_checks += 1
            counters.local_s += float(local.seconds or 0.0)
            step(f"{defect_line(defect, pair=pair)} -> {repair_line(repair, applied.params, pick)} -> "
                 f"{local.line()}")
            if local.gone and not local.new_hard:
                current, kept = applied.ws, True
                counters.pairs_fixed += 1
                counters.kept_in_round += 1
                gone += 1
                break
        if not kept:
            counters.pairs_skipped += 1
    if gone == len(defect.pairs):
        counters.tally("fixed", defect)
    else:
        counters.tally("skipped", defect)
        step(f"{defect_line(defect)} -> cluster partly repaired: {gone} of {len(defect.pairs)} pairs")
    return current


# --- the staged macro, as this arm reaches it -----------------------------------------------------------------


def stage_runner() -> Callable[[Any, Workspace, str, dict, float], _Applied]:
    """`run_stage:<stage>` for this arm: one stage of the staged healer, as a TRANSACTION, which is the same
    `stages.run_stage` the search's macro uses (search._stage_outcome) - so the two arms reach the same code and
    a chain the oracle won with is reachable here too.

    A stage that was rolled back, skipped, or could not be judged is an `OpDeclined`: for this arm those are all
    "the repair did not apply", and the next alternative is tried. The stage's own words are kept, because
    arms.py and triage.py group on them.
    """
    from meshpipeline.cad.repair_v4.stages import run_stage as _run_stage

    def run(pool: Any, ws: Workspace, stage: str, params: dict, timeout_s: float) -> _Applied:
        base = ws.fork()
        result = _run_stage(pool, base, stage, {"timeout_s": timeout_s, **(params or {})})
        if stage in result.inconclusive:
            raise CheckerUnavailable(f"stage {stage!r} could not be judged (checker unavailable)")
        if stage in result.rolled_back:
            raise OpDeclined(f"stage {stage!r} was rolled back: it lowered the cheap score")
        if stage in result.skipped:
            raise OpDeclined(f"stage {stage!r} changed nothing: {result.skipped[stage]}")
        diffs = list(result.ws.history[len(base.history):])
        if result.ws is base or not diffs:
            raise OpDeclined(f"stage {stage!r} changed nothing")
        touched: list[StableId] = []
        for d in diffs:
            touched.extend(d.generated or {})
            touched.extend(d.modified or {})
        return _Applied(result.ws, diffs, tuple(dict.fromkeys(touched)), [{"stage": stage}])

    return run
