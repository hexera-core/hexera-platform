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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.types import Site, StableId
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


# --- lane C's interface, and a stub to build against ---------------------------------------------------------


@dataclass(frozen=True)
class LocalResult:
    """What `verify_local` answers. `gone` is about THIS defect on the touched entities; `new_hard` is any hard
    code that was not there before, on those same entities. `measured` False means the question was not answered
    (lane C could not re-locate the defect, or the region mesh failed) - and an unanswered question is never read
    as a pass."""

    gone: bool
    new_hard: tuple[str, ...] = ()
    measured: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def kept(self) -> bool:
        """Keep the repair only on a measured answer that removed the defect and added no hard code."""
        return bool(self.measured and self.gone and not self.new_hard)


class VerifyLocal(Protocol):
    """Lane C's `verify_local(ws_before, ws_after, defect, touched) -> LocalResult`."""

    def __call__(self, ws_before: Workspace, ws_after: Workspace, defect: Defect,
                 touched: Sequence[StableId]) -> LocalResult: ...


def unanswered_local(ws_before: Workspace, ws_after: Workspace, defect: Defect,
                     touched: Sequence[StableId]) -> LocalResult:
    """The stub this arm runs against until lane C's lands: it ANSWERS NOTHING (`measured` False).

    With it the arm applies no repair, keeps nothing, and reports every defect skipped as
    `local_verification_unavailable` - which is the honest outcome of an arm whose verifier does not exist yet,
    and is why a wave run before lane C lands reads as "not measured" rather than "the arm cannot repair"."""
    return LocalResult(gone=False, measured=False, detail={"stub": "lane C's verify_local is not wired yet"})


# --- defects -------------------------------------------------------------------------------------------------


@dataclass
class Defect:
    """One thing wrong with the part, as the verdict and the sites report it together."""

    id: str
    #: the code the rule table is keyed on: a layer-1 finding code, or a mesher `error_kind`
    kind: str
    #: layer 1 or layer 2 - a validity defect is repaired before any mesher defect
    layer: int
    site: Site
    entities: tuple[StableId, ...]
    #: the defect's own measurement, read off the site's graph; what the parameters are derived from
    measurement: dict[str, Any] = field(default_factory=dict)

    @property
    def rank(self) -> tuple:
        table = VALIDITY_ORDER if self.layer == 1 else MESHER_ORDER
        where = table.index(self.kind) if self.kind in table else len(table)
        return (self.layer, where, self.site.id, self.id)


def defects_of(verdict: Any, sites: Sequence[Site]) -> list[Defect]:
    """The defect list for a state: layer 1's hard findings, then every mesher failure the sites located.

    One defect per (code, site): a code the verdict reports twice at one site is one thing to repair, and a code
    at two sites is two. A finding no site carries is still a defect - it is attached to the site that names its
    entities if there is one, and otherwise to the `unlocated`/coarse site, which is what the sweep does too."""
    out: list[Defect] = []
    by_entity: dict[StableId, Site] = {}
    for site in sites:
        for e in site.entities:
            by_entity.setdefault(e, site)
    fallback = next((s for s in sites if s.kind in ("unlocated", "invalid")), sites[0] if sites else None)

    hard = list(getattr(getattr(verdict, "valid", None), "hard", ()) or ())
    for i, finding in enumerate(hard):
        code = str(getattr(finding, "code", "") or "")
        ents = tuple(getattr(finding, "entities", ()) or ())
        site = next((by_entity[e] for e in ents if e in by_entity), fallback)
        if site is None:
            continue
        out.append(Defect(id=f"v{i}:{code}", kind=code, layer=1, site=site, entities=ents,
                          measurement=_measure(site, code, ents)))

    for site in sites:
        for j, failure in enumerate((site.graph or {}).get("failures") or []):
            if failure.get("mesher") == "valid":
                continue
            kind = str(failure.get("error_kind") or "")
            if not kind:
                continue
            ents = tuple(f for f in ((site.graph or {}).get("mesher_faces") or []) if str(f)[:1] == "F")
            out.append(Defect(id=f"{site.id}:m{j}:{kind}", kind=kind, layer=2, site=site,
                              entities=ents or tuple(site.entities),
                              measurement=_measure(site, kind, ents or tuple(site.entities))))
    # one defect per (kind, site): the same code reported by three meshers at one site is one thing to repair
    seen: set[tuple[str, str]] = set()
    unique: list[Defect] = []
    for d in out:
        key = (d.kind, d.site.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(d)
    return order_defects(unique)


def order_defects(defects: Sequence[Defect]) -> list[Defect]:
    """Validity first, then the mesher kinds, each in its table's order; ties by site then defect id."""
    return sorted(defects, key=lambda d: d.rank)


def _measure(site: Site, code: str, entities: Sequence[StableId]) -> dict[str, Any]:
    """The defect's measurement, read off the site's graph. Only what the parameters are derived from, and only
    what the site really carries - a key that is not there is absent, never a default, because a derived
    parameter with an invented measurement behind it is a ladder with one rung."""
    graph = site.graph or {}
    out: dict[str, Any] = {}
    if graph.get("diagonal_mm"):
        out["diagonal_mm"] = float(graph["diagonal_mm"])
    # the narrowest located face at the defect, and its length: `remove_small`'s width comes from these
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
        worst = max(clashes, key=lambda c: float(c["dist_mm"]))
        out["clash"] = {"a": worst.get("a"), "b": worst.get("b"), "dist_mm": float(worst["dist_mm"])}
    loops = graph.get("free_loops") or []
    if loops:
        out["free_loops"] = len(loops)
    if graph.get("box"):
        out["box"] = list(graph["box"])
    if graph.get("coarse"):
        out["coarse"] = True
    return out


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
#: a clash or gap is closed by a fuzzy value at least the measured distance; 1.1 covers the measurement's own
#: rounding (the sites round `dist_mm` to 4 decimals) without turning it into a rung.
FUZZY_MARGIN = 1.1


class NoParameters(ValueError):
    """The defect's measurement does not carry what this op needs. The repair is not attempted: a parameter this
    arm cannot derive is a ladder with one rung, and the design says parameters come from the measurement."""


def params_for(op: str, defect: Defect) -> tuple[list[StableId], dict[str, Any]]:
    """(targets, params) for `op` on `defect`, derived from the defect's measurement. Raises `NoParameters` when
    the measurement does not carry what the op needs."""
    m = defect.measurement
    entities = list(defect.entities)
    faces = [e for e in entities if str(e)[:1] == "F"]
    solids = [e for e in entities if str(e)[:1] == "S"]

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
        bodies = solids or [b for b in (clash or {}).values() if isinstance(b, str) and b[:1] == "S"]
        if not bodies:
            raise NoParameters("no bodies named at this defect")
        fuzzy = 0.0 if clash is None else FUZZY_MARGIN * float(clash["dist_mm"])
        if op == "fuse":
            return sorted(set(bodies)), {"fuzzy_mm": fuzzy}
        keep = sorted(set(bodies))[0]
        return sorted(set(bodies)), {"mode": f"cut_by:{keep}", "fuzzy_mm": fuzzy}
    if op in ("defeature", "unify_same_domain", "rebuild_face", "reapproximate", "split_angle",
              "split_by_proximity", "split_continuity", "fill_loop"):
        if not faces:
            raise NoParameters(f"{op} needs the faces the defect is on, and none were located")
        if op == "unify_same_domain":
            return faces, {"scope": "targets", "keep": []}
        return faces, {}
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
    kind = defect.kind
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
                "local_s": round(self.local_s, 1), "full_s": round(self.full_s, 1)}


# --- the loop ------------------------------------------------------------------------------------------------


@dataclass
class _Applied:
    ws: Workspace
    diffs: list[Diff]
    touched: tuple[StableId, ...]


def apply_repair(pool: Any, ws: Workspace, repair: Repair, defect: Defect, *, timeout_s: float,
                 run_stage: Callable[[Any, Workspace, str, dict, float], _Applied] | None = None) -> _Applied:
    """Apply `repair`'s ops in order, on the defect's own entities. Raises what the ops raise (`OpDeclined`,
    `OpFailed`, `UnknownTarget`, `NoParameters`): the caller records it against the defect and moves on.

    The SECOND step's targets are derived from the defect again, not from the first step's output, because a
    two-step recipe is "do this, then re-condition what it touched" - the stage macros take no targets at all."""
    current, diffs, touched = ws, [], []
    for op in repair:
        targets, params = params_for(op, defect)
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
    return _Applied(current, diffs, tuple(dict.fromkeys(touched)))


@dataclass
class SeqRound:
    """One round, for the trace: what was diagnosed, what was kept, and what the end-of-round panel said."""

    index: int
    defects: list[dict] = field(default_factory=list)
    kept: int = 0
    clean: bool | None = None
    score: tuple | None = None


def repair_sequentially(
    pool: Any,
    start: Workspace,
    original: str,
    *,
    judge: Any,
    sites_of: Callable[[Any, Workspace, Any], list[Site]],
    verify_local: VerifyLocal = unanswered_local,
    ai_pick: AIPick | None = None,
    run_stage: Callable[[Any, Workspace, str, dict, float], _Applied] | None = None,
    table: dict[str, tuple[Repair, ...]] | None = None,
    start_verdict: Any = None,
    op_timeout_s: float = 300.0,
    max_rounds: int = MAX_ROUNDS,
    wall_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    trace: Any = None,
) -> tuple[Workspace, Any, SeqCounters, list[SeqRound]]:
    """The arm: (best workspace, its full verdict, the counters, the rounds).

    One full panel per round, plus the one that is already paid for the start state. A repair is KEPT only on a
    measured local pass; anything else reverts it and the defect is recorded as skipped with its reason. The
    whole-part panel at the end of the round is the measurement - a local pass is not a whole-part pass.
    """
    counters = SeqCounters()
    rounds: list[SeqRound] = []
    best = start
    verdict = start_verdict if start_verdict is not None else judge.full(pool, start, original)
    if start_verdict is None:
        counters.full_checks += 1
    best_score = judge.score(verdict)

    def out_of_wall() -> bool:
        return wall_s is not None and (clock() - t0) >= wall_s

    t0 = clock()
    for index in range(1, max_rounds + 1):
        if out_of_wall() or bool(getattr(verdict, "clean", False)):
            break
        this = SeqRound(index=index)
        rounds.append(this)
        counters.rounds = index
        sites = sites_of(pool, best, verdict)
        defects = defects_of(verdict, sites)
        for d in defects:
            counters.found[d.kind] += 1
        current = best
        for defect in defects:
            if out_of_wall():
                counters.skipped[defect.kind] += 1
                counters.skipped_why["wall"] += 1
                continue
            record = {"defect": defect.id, "kind": defect.kind, "outcome": None, "repair": None, "by": None}
            this.defects.append(record)
            pick = pick_repair(defect, table=table, ai_pick=ai_pick,
                               applies=lambda op: op.startswith("run_stage:") or op in REGISTRY)
            if pick.by == "ai":
                counters.ai_picks += 1
            alternatives = [pick.repair, *[r for r in pick.candidates if r != pick.repair]][:MAX_ALTERNATIVES]
            kept = False
            for repair in [r for r in alternatives if r is not None]:
                record["repair"], record["by"] = list(repair), pick.by
                try:
                    applied = apply_repair(pool, current, repair, defect, timeout_s=op_timeout_s,
                                           run_stage=run_stage)
                except (NoParameters, OpDeclined, OpFailed, UnknownTarget, ValueError) as why:
                    record["outcome"] = f"not_applied: {type(why).__name__}: {why}"
                    counters.skipped_why[type(why).__name__] += 1
                    continue
                local = verify_local(current, applied.ws, defect, applied.touched)
                counters.local_checks += 1
                counters.local_s += float(local.seconds or 0.0)
                if local.kept:
                    current, kept = applied.ws, True
                    counters.fixed[defect.kind] += 1
                    record["outcome"] = "kept"
                    break
                record["outcome"] = ("local_fail: new " + ",".join(local.new_hard)) if local.new_hard else (
                    "local_fail: not gone" if local.measured else "local_unmeasured")
                if not local.measured:
                    # the verifier could not answer, so the next alternative cannot be judged either: stop here
                    # rather than applying two more repairs nothing will check (and say which it was)
                    counters.skipped_why["local_verification_unavailable"] += 1
                    break
            if not kept:
                counters.skipped[defect.kind] += 1
        this.kept = sum(1 for r in this.defects if r.get("outcome") == "kept")
        if trace is not None:
            trace.record("seq_round", index=index, defects=this.defects, kept=this.kept)
        if this.kept == 0:
            break                       # a round that keeps nothing stops: the next would diagnose the same state
        # THE measurement: one full 4-mesher panel for the whole round's work
        full_t = clock()
        new_verdict = judge.full(pool, current, original)
        counters.full_checks += 1
        counters.full_s += clock() - full_t
        new_score = judge.score(new_verdict)
        this.clean, this.score = bool(getattr(new_verdict, "clean", False)), tuple(new_score)
        if new_score > best_score:
            best, verdict, best_score = current, new_verdict, new_score
        else:
            # the round's repairs did not improve the whole part: keep the better state and diagnose IT again
            verdict = new_verdict if current is best else verdict
            break
    return best, verdict, counters, rounds
