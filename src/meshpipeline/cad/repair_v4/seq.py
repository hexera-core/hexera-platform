# Responsibility: the `search_seq` arm - repair defects ONE AT A TIME, verify each LOCALLY, and pay the full
#                 4-mesher panel only at the end of a round (owner direction 2026-11-24, seq-arm-design.md).
# Owns: the loop, the defect ordering, the rule table and its parameter derivation, the AI pick, the row fields.
# Boundaries: `verify_local`, the touched-entity set and entity re-location across ops are LANE C's; this module
#             calls them through `VerifyLocal` and runs unchanged against their stub. The ops, the sites, the
#             verdict, the delivery gate and the guards (B22 catastrophic loss) are used as they are, not copied.
"""One defect, one repair, local verification, at most three rounds.

The owner's objection to the search arms is that they evaluate hundreds of candidates per step. This arm is
linear in the number of defects: diagnose once per round, order the defects, and for each take ONE repair from
a rule table, with its parameters read off the defect's own measurement - no ladders, no sweep, no frontier.

WHAT MAKES IT AFFORDABLE is that a kept repair is judged LOCALLY (lane C's `verify_local`: validity on the
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
import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult, relocate, verify_local
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.workspace import Diff, UnknownTarget, Workspace

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


# --- lane C's interface (C17, `local_verify`), and what this arm does when it cannot answer -------------------

#: Lane C owns the measurement; this arm owns the decision. Their `Defect` is the one defect model (so a defect
#: does not get re-described on the way between us), their `LocalResult.outcome` / `.line()` is the trace's own
#: word for what happened, and `verify_local` re-locates the defect itself.
#:
#: TWO FAILURE MODES, kept apart, because an arm that confuses them reports a lie:
#:   - `CheckerUnavailable` - a worker timed out or crashed. INFRASTRUCTURE. The defect is skipped and recorded,
#:     and the file's row says the check could not be had; it is never read as "the repair did not work".
#:   - `NotImplementedError` - lane C's build has not landed yet (C17 published the signatures first). Same
#:     treatment, its own reason, so a wave run before their build reads as "not measured" and not as "the arm
#:     cannot repair". Both stop the alternatives for that defect: nothing would check the next one either.
UNAVAILABLE: tuple[type[BaseException], ...] = (CheckerUnavailable, NotImplementedError)

#: lane C's `Defect.kind` values, named here so this module never spells them as bare strings
VALIDITY = "validity"
MESHER = "mesher"


# --- defects: lane C's `Defect`, built from this state's sites -------------------------------------------------

#: `Defect.kind` is lane C's two-valued one; the ORDER below is over the CODE.
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


def defects_of(verdict: Any, sites: Sequence[Site]) -> list[Defect]:
    """This state's defects as lane C's `Defect`s: layer 1's hard findings first, then every mesher failure the
    sites located, ordered by `defect_rank`.

    ONE DEFECT PER (code, site). Three panel members reporting one failure at one place is ONE thing to repair,
    and the `member` carried is the first that reported it - the member lane C's local check re-runs.

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
    hard = list(getattr(getattr(verdict, "valid", None), "hard", ()) or ())
    for finding in hard:
        code = str(getattr(finding, "code", "") or "")
        if not code or code == "body_overlap":      # clustered below, never one defect per pair
            continue
        ents = tuple(getattr(finding, "entities", ()) or ())
        site = next((by_entity[e] for e in ents if e in by_entity), fallback)
        if site is None or (code, site.id) in seen:
            continue
        seen.add((code, site.id))
        out.append(Defect(id=site.id, kind=VALIDITY, code=code, entities=ents,
                          xyz=_points(site), box=_box(site), measurement=_measure(site, ents)))
    out.extend(_overlap_clusters([f for f in hard if str(getattr(f, "code", "")) == "body_overlap"],
                                 by_entity, fallback))

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
            out.append(Defect(id=site.id, kind=MESHER, code=code, entities=faces or tuple(site.entities),
                              member=str(failure.get("mesher") or "") or None, xyz=_points(site), box=_box(site),
                              measurement=_measure(site, faces or tuple(site.entities))))
    return order_defects(out)


def _overlap_clusters(findings: Sequence[Any], by_entity: dict[StableId, Site],
                      fallback: Site | None) -> list[Defect]:
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
    for finding in findings:
        ents = list(getattr(finding, "entities", ()) or ())
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
        out.append(Defect(id=f"{site.id}:overlap:{root}", kind=VALIDITY, code="body_overlap", entities=bodies,
                          pairs=tuple(sorted(cluster_pairs)), xyz=_points(site), box=_box(site),
                          measurement=_measure(site, bodies)))
    return out


def _points(site: Site) -> tuple:
    out = []
    for p in (site.graph or {}).get("points") or site.xyz or []:
        try:
            out.append((float(p[0]), float(p[1]), float(p[2])))
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
    if graph.get("coarse"):
        out["coarse"] = True
    return out


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
    # 1 file, 5 oracle-only rows of 58: this kind is mostly solved by the arms already.
    "low_quality": (("reapproximate",), ("split_by_proximity",)),
    # 1 file. The 2-step leader is the only entry with more than one winning path.
    "no_volume": (("split_by_proximity", "remove_small"), ("defeature",),
                  ("remove_small", "unify_same_domain")),
    # 4 files. defeature and remove_small tie at 3 files; defeature leads on paths (10 vs 6).
    "open_shell": (("defeature",), ("remove_small",), ("defeature", "defeature"),
                   ("fill_loop", "remove_small")),
    # 11 files, the broadest evidence in the table - and the kind nothing yet clears (see the caveat above).
    "overlapping_facets": (("split_angle", "run_stage:condition"), ("remove_small", "split_angle"),
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
#: at most this many measured face pairs are handed to `split_by_proximity` (its own schema takes a list)
MAX_PROXIMITY_PAIRS = 8
#: a clash or gap is closed by a fuzzy value at least the measured distance; 1.1 covers the measurement's own
#: rounding (the sites round `dist_mm` to 4 decimals) without turning it into a rung.
FUZZY_MARGIN = 1.1


#: THRESHOLDS NO MEASUREMENT GIVES. Three ops need a required parameter that is not a property of the defect:
#: `split_angle`'s dihedral limit, `reapproximate`'s degree and tolerance, `split_continuity`'s order. The design
#: says parameters come from the measurement and not from a ladder - so these are ONE value each, marked in the
#: trace as a DEFAULT rather than passed off as measured, and taken from the most frequent value among lane D's
#: oracle winners where that value is unambiguous (`max_deg` 60 and `order` 2 are; the oracle's `params` records
#: do not align op-for-op with its `path`, so nothing finer is read out of them).
DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "split_angle": {"max_deg": 60},
    "split_continuity": {"order": 2},
    "reapproximate": {"max_degree": 9, "tol_mm": 0.025},
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
    entities = list(pair) if pair else list(defect.entities)
    # ONLY IDS THAT ARE STILL LIVE. An earlier repair in this round renames faces; lane C's `relocate` carries
    # the defect, but a measurement this module took on the old state still names the old ids, and an op refuses
    # them ("replaced by op 'remove_small' (now: F16); use the new ids"). Derived parameters are therefore
    # filtered to the ids the workspace still has, and a defect with nothing live left is not guessed at.
    if live is not None:
        entities = [e for e in entities if e in live]
    faces = [e for e in entities if str(e)[:1] == "F"]
    solids = [e for e in entities if str(e)[:1] in ("S", "B")]   # lane C names bodies B..; layer 1 names them S..
    if not entities and (defect.entities or pair):
        raise NoParameters("none of the defect's entities are live any more")

    if op.startswith("run_stage:"):
        return [], {"stage": op.split(":", 1)[1], "params": {}}
    if op == "remove_small":
        face = m.get("narrowest_face")
        if not face:
            raise NoParameters("no located face with a measured width at this defect")
        width = REMOVE_SMALL_MARGIN * (float(face["length_mm"]) + float(face["width_mm"]))
        return [face["id"]], {"max_width_mm": width}
    if op in ("conformalise", "fuse"):
        clash = m.get("clash")
        bodies = solids or [b for b in (clash or {}).values() if isinstance(b, str) and b[:1] in ("S", "B")]
        if not bodies:
            raise NoParameters("no bodies named at this defect")
        measured = (m.get("clash_by_pair") or {}).get(pair_key(*pair)) if pair else None
        if measured is None and clash is not None:
            measured = float(clash["dist_mm"])
        fuzzy = 0.0 if measured is None else FUZZY_MARGIN * float(measured)
        if op == "fuse":
            return sorted(set(bodies)), {"fuzzy_mm": fuzzy}
        keep = sorted(set(bodies))[0]
        return sorted(set(bodies)), {"mode": f"cut_by:{keep}", "fuzzy_mm": fuzzy}
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
              "split_continuity", "fill_loop"):
        if not faces:
            # a mesher defect that re-located onto a BODY (its faces are gone) has no face to aim a face op at
            where = "re-located onto " + "+".join(solids) if solids else "none were located"
            raise NoParameters(f"{op} needs the faces the defect is on, and {where}")
        if op == "unify_same_domain":
            return faces, {"scope": "targets", "keep": []}
        if op == "defeature":
            # `source: located` is the oracle's own winning parameter for defeature, on 16 distinct files: remove
            # the features at the faces the failure was LOCATED on, rather than whatever the op would find alone
            return faces, {"source": "located"}
        return faces, dict(DEFAULT_PARAMS.get(op, {}))
    raise NoParameters(f"no parameter rule for {op!r}")


# --- the pick ------------------------------------------------------------------------------------------------

#: What an AI pick is asked for and what it may answer: ONE repair, out of the candidates this arm names. The
#: model never invents an op here and never sees a file name (B21b's rule): it is given the defect kind, the
#: measurement, and the list to choose from.
AIPick = Callable[["Defect", Sequence[Repair]], Repair | None]


@dataclass
class Pick:
    repair: Repair | None
    #: `table`, `ai`, or `none` - what decided, so a row can separate the table's wins from the model's
    by: str
    why: str
    candidates: tuple[Repair, ...] = ()


def pick_repair(defect: Defect, *, table: dict[str, tuple[Repair, ...]] | None = None,
                ai_pick: AIPick | None = None, applies: Callable[[str], bool] | None = None) -> Pick:
    """ONE repair for `defect`. The table decides where it has an unambiguous leader; the AI decides only where
    the table is empty, or where its top two entries are equally well evidenced.

    "Equally well evidenced" is the tie the design means: two repairs the table cannot separate. The table is
    already ranked by distinct files, so a tie here is a genuine one, not an artefact of the ordering."""
    # the table is keyed on the defect's CODE (lane C's `kind` is only validity/mesher)
    kind = defect.code
    rows = (table if table is not None else RULE_TABLE).get(kind, ())
    rows = tuple(r for r in rows if applies is None or all(applies(op) for op in r))
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
    found: collections.Counter[str] = field(default_factory=collections.Counter)
    fixed: collections.Counter[str] = field(default_factory=collections.Counter)
    skipped: collections.Counter[str] = field(default_factory=collections.Counter)
    #: why defects were skipped, by reason, so "the verifier was not there" never reads as "unrepairable"
    skipped_why: collections.Counter[str] = field(default_factory=collections.Counter)
    #: seconds in local verification and in the full checks, kept apart: the arm's whole claim is the ratio
    local_s: float = 0.0
    full_s: float = 0.0

    def row(self) -> dict[str, Any]:
        return {"full_checks": self.full_checks, "local_checks": self.local_checks, "rounds": self.rounds,
                "ai_picks": self.ai_picks, "defects_found": dict(self.found), "defects_fixed": dict(self.fixed),
                "defects_skipped": dict(self.skipped), "skipped_why": dict(self.skipped_why),
                "overlap_pairs_fixed": self.pairs_fixed, "overlap_pairs_skipped": self.pairs_skipped,
                "inconclusive_verdicts": self.inconclusive_verdicts,
                "resolved_by_earlier_repair": self.resolved_by_earlier_repair,
                "local_s": round(self.local_s, 1), "full_s": round(self.full_s, 1)}


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


def apply_repair(pool: Any, ws: Workspace, repair: Repair, defect: Defect, *, timeout_s: float,
                 pair: tuple[StableId, StableId] | None = None,
                 run_stage: Callable[[Any, Workspace, str, dict, float], _Applied] | None = None) -> _Applied:
    """Apply `repair`'s ops in order, on this defect's own entities (or, in a cluster, on `pair`'s two bodies).

    Raises what the ops raise (`OpDeclined`, `OpFailed`, `UnknownTarget`) and `NoParameters`: the caller records
    it against the defect and moves to the next alternative.

    BOTH STEPS THEN ONE CHECK. A two-step recipe is "do this, then re-condition what it touched", and the table
    exists because the oracle's wins are chains whose FIRST step alone changes nothing measurable - verifying
    between the steps would throw away exactly those. The pair is kept or reverted as a unit.
    """
    current, diffs, touched, used = ws, [], [], []
    here = defect
    for step_i, op in enumerate(repair):
        if step_i:
            # THE SECOND STEP'S TARGETS ARE NOT THE FIRST'S. Ops rename faces: the first end-to-end run had
            # `remove_small` replace F3 with F16 and then `split_angle` refused F3 ("use the new ids"), which
            # killed every two-step recipe on that file. Lane C's `relocate` carries the defect's entities (and
            # pairs) through the ops' own history, which is exactly the question it exists to answer.
            moved = relocate(pool, ws, current, here)
            if moved is None:
                raise NoParameters(f"the defect could not be carried to {op!r}: nothing of it is left to aim at")
            here = moved
        targets, params = params_for(op, here, pair=pair, live=set(current.ids))
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
) -> SeqOutcome:
    """The arm. One defect at a time, one repair each, local verification, ONE full panel per round.

    A repair is KEPT only on a local pass (`gone` and no new hard code). Anything else - still there, a new
    defect, an op that declined, a parameter that cannot be derived, a check that could not be had - reverts it
    and records the defect as skipped with its reason. The end-of-round panel is the measurement: a local pass is
    not a whole-part pass, which is what the rounds are for.
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
        return wall_s is not None and (clock() - t0) >= wall_s

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
        defects = _with_diagonal(pool, best, defects_of(verdict, sites_of(pool, best, verdict)))
        for d in defects:
            counters.found[d.code] += 1
        current = best
        diagnosed_on = best      # every defect in this list was diagnosed on THIS state
        for listed in defects:
            if out_of_wall():
                counters.skipped[listed.code] += 1
                counters.skipped_why["wall"] += 1
                into(this)(f"{defect_line(listed)} -> not attempted [the file's wall]")
                continue
            # RE-LOCATE BEFORE REPAIRING (seq-arm-design.md). An earlier repair in this round renamed faces, so
            # the ids this defect was diagnosed with can be stale: the first end-to-end run had `remove_small`
            # replace F3 with F16 and the NEXT defect was refused for naming F3. Lane C's `relocate` carries it.
            defect = listed
            if current is not diagnosed_on:
                moved = relocate(pool, diagnosed_on, current, listed)
                if moved is None:
                    # nothing of it is left to aim at. The design's reading: an earlier repair took it with it.
                    counters.resolved_by_earlier_repair += 1
                    into(this)(f"{defect_line(listed)} -> resolved_by_earlier_repair "
                               f"[nothing of it is left to locate]")
                    continue
                defect = moved
            if defect.code == "body_overlap" and defect.pairs:
                current = _repair_cluster(pool, current, defect, counters=counters, table=table, ai_pick=ai_pick,
                                          verify=verify, run_stage=run_stage, op_timeout_s=op_timeout_s,
                                          local_timeout_s=local_timeout_s, step=into(this))
                continue
            current = _repair_one(pool, current, defect, counters=counters, table=table, ai_pick=ai_pick,
                                  verify=verify, run_stage=run_stage, op_timeout_s=op_timeout_s,
                                  local_timeout_s=local_timeout_s, step=into(this))
        this.kept = counters.kept_in_round
        counters.kept_in_round = 0
        if this.kept == 0:
            this.full_check = "not run: the round kept nothing"
            break                       # the next round would diagnose the same state and ask the same questions
        full_t = clock()
        new_verdict = judge.full(pool, current, original)
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

    def lines(self) -> list[str]:
        """The whole file's trace, readable top to bottom: every step, and each round's full check."""
        out: list[str] = []
        for r in self.rounds:
            out.append(f"round {r.index}:")
            out.extend(f"  {line}" for line in r.steps)
            out.append(f"  full check: {r.full_check}")
        return out


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


def _alternatives(pick: Pick) -> list[Repair]:
    first = [pick.repair] if pick.repair is not None else []
    return [*first, *[r for r in pick.candidates if r != pick.repair]][:MAX_ALTERNATIVES]


def _pick_for(defect: Defect, table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
              counters: SeqCounters) -> Pick:
    pick = pick_repair(defect, table=table, ai_pick=ai_pick,
                       applies=lambda op: op.startswith("run_stage:") or op in REGISTRY)
    if pick.by == "ai":
        counters.ai_picks += 1
    return pick


def _repair_one(pool: Any, ws: Workspace, defect: Defect, *, counters: SeqCounters,
                table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
                verify: Callable[..., LocalResult], run_stage: Any, op_timeout_s: float,
                local_timeout_s: float, step: Callable[[str], None]) -> Workspace:
    """One defect, one repair at a time until one is kept or the alternatives run out. Returns the workspace to
    carry on from: the repaired one if a repair was kept, otherwise the one it was given (the revert)."""
    pick = _pick_for(defect, table, ai_pick, counters)
    if not pick.repair:
        counters.skipped[defect.code] += 1
        counters.skipped_why["no_rule"] += 1
        step(f"{defect_line(defect)} -> no repair [{pick.why}]")
        return ws
    for repair in _alternatives(pick):
        try:
            applied = apply_repair(pool, ws, repair, defect, timeout_s=op_timeout_s, run_stage=run_stage)
        except (NoParameters, OpDeclined, OpFailed, UnknownTarget) as why:
            counters.skipped_why[type(why).__name__] += 1
            step(f"{defect_line(defect)} -> {repair_line(repair, [], pick)} -> not applied: {why}")
            continue
        try:
            local = verify(pool, ws, applied.ws, defect, list(applied.touched), timeout_s=local_timeout_s)
        except UNAVAILABLE as why:
            counters.skipped[defect.code] += 1
            counters.skipped_why["local_check_unavailable"] += 1
            step(f"{defect_line(defect)} -> {repair_line(repair, applied.params, pick)} -> "
                 f"NOT CHECKED: {type(why).__name__}: {why}")
            return ws                   # nothing would check the next alternative either
        counters.local_checks += 1
        counters.local_s += float(local.seconds or 0.0)
        step(f"{defect_line(defect)} -> {repair_line(repair, applied.params, pick)} -> {local.line()}")
        if local.gone and not local.new_hard:
            counters.fixed[defect.code] += 1
            counters.kept_in_round += 1
            return applied.ws
    counters.skipped[defect.code] += 1
    return ws


def _repair_cluster(pool: Any, ws: Workspace, defect: Defect, *, counters: SeqCounters,
                    table: dict[str, tuple[Repair, ...]] | None, ai_pick: AIPick | None,
                    verify: Callable[..., LocalResult], run_stage: Any, op_timeout_s: float,
                    local_timeout_s: float, step: Callable[[str], None]) -> Workspace:
    """A body_overlap CLUSTER, repaired PAIR BY PAIR (owner note 1).

    One local check per pair, and a pair that cannot be repaired is skipped and recorded WITHOUT blocking the
    rest of the cluster - the whole point of clustering rather than one defect per pair. The cluster counts as
    fixed only if every pair ended gone; otherwise it is a skipped defect with its pairs counted separately, so
    "3 of 4 pairs" never reads as a repaired part.
    """
    current = ws
    gone = 0
    for pair in defect.pairs:
        pick = _pick_for(defect, table, ai_pick, counters)
        if not pick.repair:
            counters.pairs_skipped += 1
            counters.skipped_why["no_rule"] += 1
            step(f"{defect_line(defect, pair=pair)} -> no repair [{pick.why}]")
            continue
        kept = False
        for repair in _alternatives(pick):
            try:
                applied = apply_repair(pool, current, repair, defect, timeout_s=op_timeout_s, pair=pair,
                                       run_stage=run_stage)
            except (NoParameters, OpDeclined, OpFailed, UnknownTarget) as why:
                counters.skipped_why[type(why).__name__] += 1
                step(f"{defect_line(defect, pair=pair)} -> {repair_line(repair, [], pick)} -> not applied: {why}")
                continue
            try:
                local = verify(pool, current, applied.ws, defect, list(applied.touched), pair=pair,
                               timeout_s=local_timeout_s)
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
        counters.fixed[defect.code] += 1
    else:
        counters.skipped[defect.code] += 1
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
