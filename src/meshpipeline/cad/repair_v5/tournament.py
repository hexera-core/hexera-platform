# Responsibility: CRS v5's ONE per-defect repair: a TOURNAMENT. The class menu's entrants each run on their own
#                 copy through heats (apply + safety guards -> local validity -> local mesh), the survivors are
#                 ranked, the winner is kept. Every candidate's outcome is a MATCH record: applied, or why not.
# Owns: `Entrant`, the entrant sources (`rule_seed`, `class_menu`, `DEFAULT_SOURCES`), parameter `variants`, the
#       part-size `Budget`, `Match`/`MatchLog`, `loss_guard`, `Tournament` (seq's `repair_defect` hook).
# Boundaries: the loop (order, relocation, rounds, panel) is `repair_v4.seq.repair_sequentially`; applying a repair
#             and deriving its parameters is `seq.apply_repair`/`seq.params_for`; aimability is `seq.aim_reason`;
#             local checks are `local_verify.verify_local`; the catastrophic-loss rule is `loss.check_loss`. This
#             module runs and ranks them, and keeps the log. It adds no op, no check and no guard of its own.
"""One tournament per defect (owner decision 2026-11-24, "TOURNAMENT FOR EVERY DEFECT").

ENTRANTS come from a list of SOURCES, in tier order. Step 3 has two: the RULE SEED (the class's rule repair, with
parameters from the measured cause - it always enters and wins ties, so v5 is never worse than its rule) and the
CLASS MENU (the class's other repairs, then parameter variants one step either side of the measured value). A
later source (retrieval, an agent) is one more function appended to the list. A source with `paid = True` (step 12's
AGENT, `agent_entrants.AgentSource`) is consulted only AFTER the free tiers' heats, with what they did in its brief
(`Brief.tried`, `Brief.solved`); it decides itself whether to enter (owner: only when no free entrant won, plus a
small exploration share of the solved ones) and its survivors are ranked with the free ones, so the seed still wins
ties. An entrant a source REJECTS (an agent proposal off the class menu) is logged as `rejected`, never run.

HEATS, per candidate, on its own copy (ops never mutate the state they are given):
  (a) apply: dropped when it does not apply (the op's or the parameter rule's reason) or a safety guard trips
      (`loss_guard`: catastrophic loss against the ORIGINAL input, or an output that is inside out);
  (b) local validity on the touched bodies: dropped when the defect's code remains or a new hard code appears;
  (c) local mesh with the failing member on the touched bodies (mesher defects; `verify_local` does b and c).
RANK: defect gone > no new failures > least geometric deviation > least volume change > rule seed > cheapest.

NOTHING IS FILTERED SILENTLY. v4 lost 52 of 72 prototype steps to filtering it never reported. Here a repair the
defect cannot be aimed at is an `unaimable` match with its reason, an entrant over the part's cap is `not_entered`,
one an early stop skipped is `not_run`, and each says why.
"""

from __future__ import annotations

import collections
import dataclasses
import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import Defect, LocalResult
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpFailed
from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.verdict import wall
from meshpipeline.cad.repair_v4.verdict.assemble import PanelWallExhausted
from meshpipeline.cad.repair_v4.workspace import Workspace
from meshpipeline.cad.repair_v5 import events as EV

Repair = Q.Repair
Pair = tuple[StableId, StableId]

#: the owner's cap: at most six entrants in one tournament
MAX_ENTRANTS = 6

# --- PART-SIZE BUDGET. PROVISIONAL: placeholders pending step 1's paired-baseline data (wall and memory vs face
# count). They exist because v4's VMs were OOM-killed: every parallel copy holds its own shape in a worker. ---------
#: up to this many faces: all entrants, in parallel copies
SMALL_PART_MAX_FACES = 1_000
#: up to this many faces: fewer entrants, fewer at once
LARGE_PART_MAX_FACES = 10_000
LARGE_PART_MAX_ENTRANTS = 3
LARGE_PART_PARALLEL = 2
# above LARGE_PART_MAX_FACES: entrants one at a time in tier order, stopping at the first that survives every heat


@dataclass(frozen=True)
class Budget:
    entrants: int
    parallel: int
    early_stop: bool
    why: str


def budget_for(n_faces: int) -> Budget:
    if n_faces <= SMALL_PART_MAX_FACES:
        return Budget(MAX_ENTRANTS, MAX_ENTRANTS, False, f"{n_faces} faces: parallel")
    if n_faces <= LARGE_PART_MAX_FACES:
        return Budget(LARGE_PART_MAX_ENTRANTS, LARGE_PART_PARALLEL, False, f"{n_faces} faces: large, fewer entrants")
    return Budget(MAX_ENTRANTS, 1, True, f"{n_faces} faces: largest, sequential with early stop")


def face_count(ws: Any) -> int:
    return sum(1 for sid in (getattr(ws, "ids", None) or {}) if str(sid)[:1] == "F")


# --- entrants -------------------------------------------------------------------------------------------------

RULE_SEED = "rule_seed"
CLASS_MENU = "class_menu"
#: step 12: the model's entrants (`agent_entrants.AgentSource`)
AGENT = "agent"


@dataclass(frozen=True)
class Entrant:
    repair: Repair
    tier: str
    #: per step, merged over `params_for`'s derived parameters (a parameter VARIANT); () = as derived
    overrides: tuple[dict, ...] = ()
    note: str = ""
    #: why the source refused this proposal (off the class menu, a parameter out of range...): logged, never run
    rejected: str = ""

    @property
    def seed(self) -> bool:
        return self.tier == RULE_SEED

    @property
    def key(self) -> tuple:
        return (self.repair, tuple(tuple(sorted(o.items())) for o in self.overrides))


@dataclass(frozen=True)
class Brief:
    """What a source is told: the defect (re-located onto `ws`), its class menu (rule repair first), the pair."""

    defect: Defect
    menu: tuple[Repair, ...]
    ws: Workspace
    pair: Pair | None = None
    #: for a PAID source only: every candidate the free tiers ran on this defect, and whether one of them won
    tried: tuple[Match, ...] = ()
    solved: bool = False


#: a source of entrants. Step 12 appends an agent source (`paid = True`) to `DEFAULT_SOURCES`.
EntrantSource = Callable[[Brief], list[Entrant]]


def is_paid(source: EntrantSource) -> bool:
    return bool(getattr(source, "paid", False))


def rule_seed(brief: Brief) -> list[Entrant]:
    return [Entrant(brief.menu[0], RULE_SEED, note="the class's rule repair")] if brief.menu else []


def class_menu(brief: Brief) -> list[Entrant]:
    """The menu's other repairs, then the parameter variants of every repair on it (the seed's first)."""
    out = [Entrant(r, CLASS_MENU, note="class menu") for r in brief.menu[1:]]
    for r in brief.menu:
        out.extend(Entrant(r, CLASS_MENU, (v,), note=f"variant of the measured {next(iter(v))}")
                   for v in variants(r, brief))
    return out


DEFAULT_SOURCES: tuple[EntrantSource, ...] = (rule_seed, class_menu)

#: op -> (the parameter a variant moves, the ordered values it steps along). A variant is ONE step either side of the
#: value `params_for` derived, plus the op's own default: around the measurement, never a sweep.
_SPLIT_ANGLES = (45, 60, 90)           # = ops.shells._SPLIT_ANGLES, the staged/det angles
_TRIM_PADS = (0.01, 0.05)              # = search._TRIM_PADS (not imported: search is the v4 driver)
VARIANT_STEPS: dict[str, tuple[str, tuple[float, ...]]] = {
    "split_aligned": ("segments", ()),           # () = the op's own schema enum
    "split_angle": ("max_deg", _SPLIT_ANGLES),
    # round 2 fix G: the fine angular split, one step either side of the measured angle
    "split_turn": ("max_deg", (5, 10, 15, 22.5, 30, 45, 90)),     # divisors of 90 (`facet_local.snap_turn`)
    "tighten_tolerances": ("target_rel", (1e-6, 1e-5, 1e-4)),
    "trim_unbounded": ("pad_fraction", _TRIM_PADS),
    # v5 step 8: the sliver threshold, a decade either side of the default share of the part's volume
    "fragment_drop_small": ("max_fraction", (1e-4, 1e-3, 1e-2)),
    # step 11: the ladder is the MEASURED gap levels (`cause["sew_ladder_mm"]`, open_shell): "sew at the next gap"
    "sew_gap": ("tol_mm", ()),
}
#: op -> the cause key holding a MEASURED ladder, used before the op's schema enum
CAUSE_LADDERS = {"sew_gap": "sew_ladder_mm"}


def _ladder(op: str, key: str, given: tuple[float, ...]) -> tuple[float, ...]:
    if given:
        return given
    schema = getattr(REGISTRY.get(op), "params_schema", {}) or {}
    return tuple(sorted((schema.get("properties", {}).get(key) or {}).get("enum") or ()))


def variants(repair: Repair, brief: Brief) -> list[dict]:
    """Override dicts for the one-op `repair`'s variants on this defect; [] when it has none or no derivable value."""
    if len(repair) != 1 or repair[0] not in VARIANT_STEPS:
        return []
    op = repair[0]
    key, given = VARIANT_STEPS[op]
    measured = (brief.defect.cause or {}).get(CAUSE_LADDERS.get(op, ""))
    ladder = tuple(sorted(float(v) for v in measured if v and v > 0)) if measured else _ladder(op, key, given)
    live = set(brief.ws.ids) if getattr(brief.ws, "ids", None) else None
    try:
        value = Q.params_for(op, brief.defect, pair=brief.pair, live=live)[1].get(key)
    except Q.NoParameters:
        return []
    if not ladder or not isinstance(value, (int, float)) or value <= 0:
        return []
    at = min(range(len(ladder)), key=lambda i: abs(math.log(ladder[i]) - math.log(value)))
    picks = [ladder[i] for i in (at - 1, at + 1) if 0 <= i < len(ladder)]
    default = Q.DEFAULT_PARAMS.get(op, {}).get(key)
    if default is not None:
        picks.append(default)
    return [{key: v} for v in dict.fromkeys(picks) if v != value]


# --- the guards (heat a): the existing rules, not new ones ------------------------------------------------------


@dataclass(frozen=True)
class Guarded:
    refused: str | None
    #: |1 - output volume / input volume| against the ORIGINAL input (None when not measured)
    volume_change: float | None = None


Guard = Callable[[Any, Workspace, Workspace], Guarded]


def loss_guard(pool: Any, before: Workspace, after: Workspace) -> Guarded:
    """`loss.check_loss` against the lineage's ORIGINAL input: refuses a catastrophic loss (B22: <10% of the part
    or of any body) and an output with more inside-out (or unorientable) solids than the input had. A degenerate
    Boolean is the op's own refusal (`cut_degenerate`) and arrives as not-applied. Unmeasured refuses nothing."""
    from meshpipeline.cad.repair_v4.loss import check_loss

    report = check_loss(pool, after.original, after.brep_path)
    change = abs(1.0 - float(report.volume_ratio)) if report.volume_ratio is not None else None
    if report.catastrophic:
        return Guarded(f"catastrophic_loss: {report.reason} (part kept {report.part_kept}, lost bodies "
                       f"{list(report.lost_bodies)})", change)
    inv = report.detail.get("inverted") or {}
    worse = int(inv.get("output") or 0) > int(inv.get("input") or 0) or int(inv.get("unorientable_output") or 0) > len(
        inv.get("unorientable_input") or ())
    if worse:
        return Guarded(f"inside_out: the output has more inverted solids than the input ({inv})", change)
    return Guarded(None, change)


#: keys under `Diff.params["measured"]` that are a geometric deviation in mm (faces.py `_two_way`, rebuild_face's
#: boundary shift, fill_loop's fit, tweak's moved distance). The split/imprint ops are exact and measure none.
DEVIATION_KEYS = ("old_to_new", "new_to_old", "boundary_shift", "fill_to_surroundings", "moved_mm")


def deviation_of(diffs: Sequence[Any]) -> float | None:
    got = [float(v) for d in diffs for k, v in ((getattr(d, "params", None) or {}).get("measured") or {}).items()
           if k in DEVIATION_KEYS and isinstance(v, (int, float)) and math.isfinite(v)]
    return max(got) if got else None


# --- the match log --------------------------------------------------------------------------------------------

#: the reason key of a candidate stopped by the FILE wall (`PanelWallExhausted`): a wall stop, never a failed repair
WALL = "wall"

#: results that mean the candidate never reached heat (a)
NOT_TRIED = ("unaimable", "not_entered", "not_run", "rejected")


@dataclass
class Match:
    """One candidate of one tournament."""

    defect_id: str
    defect_class: str
    cause: str
    tier: str
    op: str
    params: list[dict]
    applied: bool
    #: the counts' key for why (e.g. `OpDeclined`, `NoParameters`, `unaimable`, `catastrophic_loss`), and in words
    reason_key: str
    reason: str
    #: the furthest heat reached: aim, apply, guard, validity, mesh
    heat: str
    #: unaimable | not_entered | not_run | rejected | not_applied | guard_refused | not_checked | still_there |
    #: new_defect | survived | won
    result: str
    pair: str | None = None
    deviation_mm: float | None = None
    volume_change: float | None = None
    wall_s: float = 0.0
    #: close_faces (step 7): predicted crossings per member before and after this candidate, and the local mesh's
    #: answer (`member: status/kind`) - the face-by-face instrument
    predicted_before: dict | None = None
    predicted_after: dict | None = None
    mesh_result: str | None = None
    #: which tournament of the run this candidate was in (1, 2, ...; 0 = not a tournament, e.g. the conformal pass)
    tournament: int = 0

    def line(self) -> str:
        params = "; ".join(", ".join(f"{k}={Q._short(v)}" for k, v in sorted(p.items())) for p in self.params if p)
        extra = "" if self.result in NOT_TRIED else (
            f" (dev {_num(self.deviation_mm)}, vol {_num(self.volume_change)}, {self.wall_s:.1f} s)")
        why = f": {self.reason}" if self.reason else ""
        return f"  match [{self.tier}] {self.op}{{{params}}} -> {self.result} at {self.heat}{why}{extra}"

    def row(self) -> dict[str, Any]:
        return asdict(self)


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.3g}"


@dataclass
class MatchLog:
    records: list[Match] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        """Per run: attempts (candidates that reached heat a), applied, not_applied by reason, unaimable, and the
        rest of the outcomes by name."""
        tried = [r for r in self.records if r.result not in NOT_TRIED]
        return {"attempts": len(tried), "applied": sum(r.applied for r in tried),
                "not_applied": dict(collections.Counter(r.reason_key for r in tried if not r.applied)),
                "unaimable": sum(r.result == "unaimable" for r in self.records),
                "results": dict(collections.Counter(r.result for r in self.records)),
                "won_by_tier": dict(collections.Counter(r.tier for r in self.records if r.result == "won")),
                "by_class": self.by_class()}

    def by_class(self) -> dict[str, Any]:
        """Per defect class: tournaments, tournaments won, and per SOURCE (tier) its win rate - tournaments it won
        over tournaments it had a candidate reach heat (a) in (owner: a source that does not beat the rule in a class
        is switched off, so this is the number that decides it)."""
        games: dict[int, list[Match]] = collections.defaultdict(list)
        for r in self.records:
            if r.tournament:
                games[r.tournament].append(r)
        out: dict[str, Any] = {}
        for rows in games.values():
            row = out.setdefault(rows[0].defect_class, {"tournaments": 0, "won": 0, "tiers": {}})
            row["tournaments"] += 1
            row["won"] += any(r.result == "won" for r in rows)
            for tier in dict.fromkeys(r.tier for r in rows if r.result not in NOT_TRIED):
                t = row["tiers"].setdefault(tier, {"entered": 0, "won": 0})
                t["entered"] += 1
                t["won"] += any(r.tier == tier and r.result == "won" for r in rows)
        for row in out.values():
            for t in row["tiers"].values():
                t["rate"] = round(t["won"] / t["entered"], 3)
        return out


# --- the tournament -------------------------------------------------------------------------------------------

#: ties: deviations within this (mm) and volume changes within this (fraction) are equal, so the seed wins them
DEVIATION_TIE_MM = 1e-4
VOLUME_TIE = 1e-4

# --- CRS v5 r2: every defect gets a turn. MEASURED on the step-13 wave (4a263b160, 95 dev rows): 45 files had 222
# defects never tried, because ONE tournament could take the class loop's whole share (b25999e0f8be: 680 s on its
# first defect, six cheap ones never reached; e357818b22f8 1000 s; 7e626274654e 992 s). Each tournament now gets a
# SLICE of what the loop has left: its fair share among the defects still to try, never under `MIN_SLICE_S`; an op's
# timeout is the slice, no entrant starts after it, and a defect its slice cut short gets a second turn at the
# round's end (`seq`) with what the others left. Per-class tournament wall on the same wave (median / p90 s):
# overlap 8/79, invalid_brep 11/165, low_quality 17/120, close_faces 63/329.
#: the shortest slice a tournament is given (a slice shorter than this could not apply the cheapest repair menu)
MIN_SLICE_S = 30.0
#: an applied candidate whose local check would get less than this before the loop's deadline is not checked (a
#: SLICE stop: the check cannot finish, and the loop's wall is a hard cap)
MIN_LOCAL_S = 5.0
#: why an entrant was stopped by its tournament's slice (`Match.reason_key`); the defect may get a second turn
SLICE = Q.SLICE
#: CRS v5 r2 fix I: an overlap cluster plays at most this many pair tournaments per round (the most any cluster of
#: the round-2 wave played with an entrant actually run was 9), and none with under `MIN_PAIR_S` of the loop left
MAX_CLUSTER_PAIRS = 16
MIN_PAIR_S = 1.0
#: why a cluster's pair was not played: the cluster's per-round cap (`MAX_CLUSTER_PAIRS`)
CLUSTER_CAP = "cluster_cap"


@dataclass
class _Run:
    entrant: Entrant
    match: Match
    ws: Workspace | None = None          # the candidate's state when it survived
    gone: bool = False
    new_hard: bool = False
    checked: bool = False                # a local check was had (for the counters)
    local_s: float = 0.0


def _take(run: _Run, done: _Run) -> None:
    """Copy a finished heat (run on a private copy) back onto the run the tournament logs."""
    run.match, run.ws, run.gone, run.new_hard = done.match, done.ws, done.gone, done.new_hard
    run.checked, run.local_s = done.checked, done.local_s


def pairs_by_volume(defect: Defect) -> list[Pair]:
    """A cluster's pairs, LARGEST measured overlap first (round 2: the owner's order; `overlap.attach_overlap_facts`
    puts `pair_common_mm3` on the cause), unmeasured pairs after them in their own order. Each pair is still
    re-located on the state the previous pair left (`play`), so the order is the only thing this changes. Pure."""
    volumes = (defect.cause or {}).get("pair_common_mm3") or {}

    def key(item: tuple[int, Pair]) -> tuple[int, float, int]:
        k, (a, b) = item
        v = volumes.get(f"{a}|{b}" if a <= b else f"{b}|{a}")
        return (0, -float(v), k) if v is not None else (1, 0.0, k)

    return [p for _, p in sorted(enumerate(defect.pairs), key=key)]


@dataclass
class Tournament:
    """`seq.repair_sequentially`'s `repair_defect`: one tournament per defect (per pair, for an overlap cluster)."""

    menu_of: Callable[[Defect], tuple[Repair, ...]]
    verify: Callable[..., LocalResult]
    guard: Guard = loss_guard
    run_stage: Any = None
    log: MatchLog = field(default_factory=MatchLog)
    sources: Sequence[EntrantSource] = DEFAULT_SOURCES
    budget: Callable[[int], Budget] = budget_for
    op_timeout_s: float = 300.0
    local_timeout_s: float = 600.0
    clock: Callable[[], float] = time.monotonic
    #: a `clock()` instant after which no further entrant starts (the class loop's share of the wall)
    deadline: float | None = None
    trace: Any = None
    #: tournaments played so far (each candidate's `Match.tournament`)
    _games: int = 0
    #: CRS v5 r2: the `clock()` instant the current tournament's slice ends (None: no class-loop deadline given)
    _slice_end: float | None = None
    #: CRS v5 r2h: the full checks' own share (s): the seconds the loop spent in full checks (`counters.full_s`), up to
    #: this, move `deadline` later - a full check is not paid for out of the defects' slices
    panel_credit_s: float = 0.0
    _credit: float = 0.0

    def _deadline(self) -> float | None:
        return None if self.deadline is None else self.deadline + self._credit

    def __call__(self, pool: Any, ws: Workspace, defect: Defect, *, counters: Q.SeqCounters,
                 step: Callable[[str], None]) -> Workspace:
        self._credit = min(float(getattr(counters, "full_s", 0.0) or 0.0), self.panel_credit_s)
        if defect.code == "body_overlap" and defect.pairs:
            return self._cluster(pool, ws, defect, counters=counters, step=step)
        won, why = self.play(pool, ws, defect, counters=counters, step=step)
        if won is None:
            counters.tally("skipped", defect)
            counters.skipped_why[why] += 1
            counters.sliced = why == SLICE      # the loop may give it a second turn (and then un-counts this skip)
            return ws
        counters.tally("fixed", defect)
        counters.kept_in_round += 1
        return won

    def _cluster(self, pool: Any, ws: Workspace, defect: Defect, *, counters: Q.SeqCounters,
                 step: Callable[[str], None]) -> Workspace:
        """A body_overlap cluster, one tournament PER PAIR (owner note 1): one bad pair never blocks the rest. At most
        `MAX_CLUSTER_PAIRS` pair tournaments per round, and none started without wall to run an entrant in: the
        pairs not played are counted and said in ONE line (the next round re-diagnoses what is left)."""
        current, gone, played = ws, 0, 0
        pairs = pairs_by_volume(defect)
        for k, pair in enumerate(pairs):
            stop = self._cluster_stop(played)
            if stop is not None:
                key, reason = stop
                rest = len(pairs) - k
                counters.pairs_skipped += rest
                counters.skipped_why[key] += rest
                step(f"{Q.defect_line(defect)} -> {rest} of {len(pairs)} pairs not played: {reason}")
                break
            games = self._games
            won, why = self.play(pool, current, defect, pair=pair, counters=counters, step=step)
            played += self._games - games
            if won is None:
                counters.pairs_skipped += 1
                counters.skipped_why[why] += 1
                continue
            current, gone = won, gone + 1
            counters.pairs_fixed += 1
            counters.kept_in_round += 1
        if gone == len(defect.pairs):
            counters.tally("fixed", defect)
        else:
            counters.tally("skipped", defect)
            step(f"{Q.defect_line(defect)} -> cluster partly repaired: {gone} of {len(defect.pairs)} pairs")
        return current

    def _cluster_stop(self, played: int) -> tuple[str, str] | None:
        """(reason key, why) the cluster's NEXT pair is not played, or None. Measured on the round-2 wave (df39e5e29):
        overlap_self_intersection opened 809 tournaments for 51 found defects, 739 of them with no entrant run - every
        pair of a 151-326-pair cluster still opened one after the class loop's wall had ended ("the tournament's
        slice of the wall ended before this entrant could start")."""
        if self._file_wall_spent():
            return WALL, "the file's wall"
        if self.deadline is not None and self.deadline - self.clock() < MIN_PAIR_S:
            return SLICE, "the class loop's wall has ended"
        if played >= MAX_CLUSTER_PAIRS:
            return CLUSTER_CAP, (f"the cluster's cap of {MAX_CLUSTER_PAIRS} pair tournaments per round "
                                 f"(the next round re-diagnoses what is left)")
        return None

    # -- one tournament --

    def play(self, pool: Any, ws: Workspace, defect: Defect, *, counters: Q.SeqCounters,
             step: Callable[[str], None], pair: Pair | None = None) -> tuple[Workspace | None, str]:
        """(the winner's state, "") or (None, why nothing won). Every candidate is logged."""
        brief = Brief(defect, tuple(self.menu_of(defect)), ws, pair)
        head = Q.defect_line(defect, pair=pair)
        unrepairable = (defect.cause or {}).get("unrepairable")
        if unrepairable:
            # a detector's measured verdict that no repair applies by design (step 11: a genuine sheet body, a missing
            # face over the fill cap): recorded, never played, so it burns no wall
            step(f"{head} -> unrepairable: {unrepairable}")
            return None, "unrepairable"
        seen: set[tuple] = set()
        entrants = self._entrants(brief, [s for s in self.sources if not is_paid(s)], seen)
        if not entrants:
            step(f"{head} -> no entrant [no class menu row for this defect]")
            return None, "no_entrant"
        self._games += 1
        budget = self.budget(face_count(ws))
        self._slice_end = self._slice(counters)
        runs, aimable, entered = self._enter(defect, pair, entrants, budget)
        self._heats(pool, ws, defect, pair, entered, budget)
        paid = [s for s in self.sources if is_paid(s)]
        # the paid tier keeps the owner's exploration share on solved defects; only the slice gates it (CRS v5 r2)
        if paid and not self._in_slice():
            step(f"{head} -> the tournament's slice of the wall is spent: the paid tier is not consulted")
        if paid and self._in_slice() and not any(r.match.reason_key == WALL for r in entered):
            # the PAID tier (the agent), after the free heats and told what they did; it decides whether to enter.
            # Its own cap (<= 3) is the source's; the part-size budget still bounds how many run and how.
            solved = any(r.ws is not None for r in entered)
            extra = self._entrants(dataclasses.replace(brief, tried=tuple(r.match for r in runs), solved=solved),
                                   paid, seen)
            if extra:
                more, more_aimable, more_entered = self._enter(defect, pair, extra, budget)
                self._heats(pool, ws, defect, pair, more_entered, budget)
                entrants, runs = [*entrants, *extra], [*runs, *more]
                aimable, entered = [*aimable, *more_aimable], [*entered, *more_entered]
        for run in entered:
            if run.checked:
                counters.local_checks += 1
                counters.local_s += run.local_s
        survivors = [r for r in entered if r.ws is not None]
        winner = min(survivors, key=lambda r: self._rank(r, entrants.index(r.entrant)), default=None)
        for r in survivors:
            r.match.result = "won" if r is winner else "survived"
            if r is not winner:
                r.match.reason = "ranked below the winner"
        verdict = (f"won by [{winner.entrant.tier}] {winner.match.op}" if winner is not None
                   else "unrepaired: no candidate survived the heats")
        step(f"{head} -> tournament of {len(entered)} ({budget.why}): {verdict}")
        for run in runs:
            run.match.tournament = self._games
            self.log.records.append(run.match)
            step(run.match.line())
            if self.trace is not None:
                self.trace.record("v5_match", **run.match.row())
        walled = next((r.match.reason for r in entered if r.match.reason_key == WALL), "")
        if walled:
            counters.wall_stop = walled       # the loop stops on the FILE wall; a winner already had is still kept
        if winner is None:
            sliced = any(r.match.reason_key == SLICE for r in entered)
            return None, WALL if walled else SLICE if sliced else "no_survivor" if aimable else "unaimable"
        return winner.ws, ""

    # -- CRS v5 r2: the tournament's slice of the class loop's wall --

    def _slice(self, counters: Q.SeqCounters) -> float | None:
        """The `clock()` instant this tournament's slice ends: its fair share of what the class loop has left among
        the defects still to try (`counters.defects_left`, this one included), at least `MIN_SLICE_S`, never past the
        loop's deadline. Unused slice time is not lost: the next tournament divides whatever is left."""
        deadline = self._deadline()
        if deadline is None:
            return None
        now = self.clock()
        left = max(0.0, deadline - now)
        share = left / max(1, int(getattr(counters, "defects_left", 0) or 1))
        return now + min(left, max(MIN_SLICE_S, share))

    def _in_slice(self) -> bool:
        return self._slice_end is None or self.clock() < self._slice_end

    @staticmethod
    def _entrants(brief: Brief, sources: Sequence[EntrantSource], seen: set[tuple]) -> list[Entrant]:
        out: list[Entrant] = []
        for source in sources:
            for e in source(brief):
                if e.rejected:
                    out.append(e)                 # logged as rejected, whatever it repeats
                elif e.key in seen and is_paid(source):
                    # a paid proposal that repeats a free entrant is logged, not silently dropped
                    out.append(dataclasses.replace(e, rejected="already entered by an earlier tier with these "
                                                               "parameters"))
                elif e.key not in seen:
                    seen.add(e.key)
                    out.append(e)
        return out

    def _enter(self, defect: Defect, pair: Pair | None, entrants: list[Entrant],
               budget: Budget) -> tuple[list[_Run], list[_Run], list[_Run]]:
        """(every run, the aimable ones, the ones entered under `budget`); the rest are logged with why not."""
        runs = [_Run(e, self._match(defect, e, pair)) for e in entrants]
        aimable = []
        for run in runs:
            if run.entrant.rejected:
                self._set(run, applied=False, key="rejected", reason=f"rejected: {run.entrant.rejected}",
                          heat="aim", result="rejected")
                continue
            reason = self._unaimable(run.entrant.repair, defect)
            if reason:
                self._set(run, applied=False, key="unaimable", reason=reason, heat="aim", result="unaimable")
            else:
                aimable.append(run)
        entered = aimable[:budget.entrants]
        for run in aimable[budget.entrants:]:
            self._set(run, applied=False, key="not_entered", heat="aim", result="not_entered",
                      reason=f"over the {budget.entrants}-entrant cap ({budget.why})")
        return runs, aimable, entered

    @staticmethod
    def _unaimable(repair: Repair, defect: Defect) -> str | None:
        for op in repair:
            if not op.startswith("run_stage:") and op not in REGISTRY:
                return f"{op} is not a registered op"
            reason = Q.aim_reason(op, defect)
            if reason:
                return reason
        return None

    @staticmethod
    def _match(defect: Defect, e: Entrant, pair: Pair | None) -> Match:
        return Match(defect_id=defect.id, defect_class=defect.cls or "unknown",
                     cause=(defect.rule or defect.code)[:160], tier=e.tier, op="+".join(e.repair),
                     params=[dict(o) for o in e.overrides], applied=False, reason_key="", reason="", heat="aim",
                     result="", pair="+".join(pair) if pair else None)

    @staticmethod
    def _set(run: _Run, *, applied: bool, key: str, reason: str, heat: str, result: str) -> None:
        m = run.match
        m.applied, m.reason_key, m.reason, m.heat, m.result = applied, key, reason, heat, result

    def _heats(self, pool: Any, ws: Workspace, defect: Defect, pair: Pair | None, entered: list[_Run],
               budget: Budget) -> None:
        def one(run: _Run) -> _Run:
            # on a PRIVATE copy: after a file-wall kill a straggler thread may still finish, and must not rewrite a
            # run the log already holds (`_take` copies a finished heat back)
            mine = dataclasses.replace(run, match=dataclasses.replace(run.match))
            if not self._in_slice():
                # a queued heat whose turn came after the tournament's slice ended (CRS v5 r2)
                self._set(mine, applied=False, key=SLICE, heat="aim", result="not_run",
                          reason="the tournament's slice of the wall ended before this entrant could start")
                return mine
            return EV.heat_done(self._heat(pool, ws, defect, pair, mine), defect)

        if budget.parallel > 1 and len(entered) > 1:
            if self._file_wall_spent():
                for run in entered:
                    self._walled(run, "the file's wall: nothing left to start a heat with")
                return
            width = min(budget.parallel, len(entered))
            ex = ThreadPoolExecutor(max_workers=width)
            futures = [(ex.submit(one, run), run) for run in entered]
            try:
                seed_survived = False
                for i, (fut, run) in enumerate(futures):
                    # only an entrant QUEUED behind the pool's width (deterministic: never a race with a started one)
                    if seed_survived and i >= width and fut.cancel():
                        # CRS v5 r2: the rule seed survived and wins ties: an entrant not yet started is not run
                        self._set(run, applied=False, key="not_run", heat="aim", result="not_run",
                                  reason="the rule seed survived every heat (it wins ties): early stop")
                        continue
                    _take(run, fut.result())
                    seed_survived = seed_survived or (run.entrant.seed and run.ws is not None)
            except BaseException as why:
                if not wall.is_file_wall(why):
                    ex.shutdown(wait=True)
                    raise
                # CRS v5 step 7: the harness's file-wall kill (SIGALRM, raised HERE in the main thread while it waits
                # on the pool) used to escape and lose the whole file. It is a WALL STOP: the heats that finished keep
                # their results (a survivor can still win), the rest are walled, and the loop stops on the wall.
                EV.wall_stop("tournament_heats", why, defect)
                for fut, run in futures:
                    if fut.done() and not fut.cancelled() and fut.exception() is None:
                        _take(run, fut.result())
                    else:
                        self._walled(run, f"the file's wall: {type(why).__name__}: {why}")
                ex.shutdown(wait=False, cancel_futures=True)
                return
            ex.shutdown(wait=True)
            return
        stop = ""
        for run in entered:
            if not stop and self.deadline is not None and self.clock() >= self._deadline():
                stop = "the class loop's wall"
            if not stop and self._file_wall_spent():
                stop = "the file's wall"
            if not stop and not self._in_slice():
                stop = "the tournament's slice of the wall"
            if stop:
                key = {"the file's wall": WALL, "the tournament's slice of the wall": SLICE}.get(stop, "not_run")
                self._set(run, applied=False, key=key, heat="aim", result="not_run", reason=stop)
                continue
            try:
                _take(run, one(run))
            except BaseException as why:
                if not wall.is_file_wall(why):
                    raise
                EV.wall_stop("tournament_heat", why, defect)
                self._walled(run, f"the file's wall: {type(why).__name__}: {why}")
                stop = "the file's wall"
                continue
            if run.match.reason_key == WALL:
                stop = "the file's wall"
            elif budget.early_stop and run.ws is not None:
                stop = "an earlier entrant survived every heat (sequential early stop)"
            elif run.entrant.seed and run.ws is not None:
                stop = "the rule seed survived every heat (it wins ties): early stop"
            elif run.match.result == "not_checked":
                stop = "the local check is unavailable: nothing would check this entrant either"

    def _walled(self, run: _Run, reason: str) -> None:
        self._set(run, applied=False, key=WALL, heat="aim", result="not_run", reason=reason)

    @staticmethod
    def _file_wall_spent() -> bool:
        left = wall.remaining_s()
        return left is not None and left <= 0.0

    def _op_timeout(self) -> float:
        """The op's timeout, clipped to the FILE wall left (step 7): an op that could outrun it is what fired the
        harness's alarm inside the pool. Never under a second (an op given no time fails as a timeout, not a hang)."""
        left = wall.remaining_s()
        out = self.op_timeout_s if left is None else max(1.0, min(self.op_timeout_s, left))
        if self._slice_end is not None:
            # CRS v5 r2: and to the tournament's slice, so one slow op cannot eat the other defects' turns
            out = max(1.0, min(out, self._slice_end - self.clock()))
        return out

    def _local_timeout(self) -> float:
        """The local check's timeout: its own, clipped to what the class loop has left (not to the slice: an applied
        candidate is worth checking, and what the check overruns the next slices pay) - but never past the loop's
        deadline, which is a HARD cap (coordinator, CRS v5 r2): the fallback's wall starts there."""
        deadline = self._deadline()
        if deadline is None:
            return self.local_timeout_s
        return max(1.0, min(self.local_timeout_s, deadline - self.clock()))

    def _heat(self, pool: Any, ws: Workspace, defect: Defect, pair: Pair | None, run: _Run) -> _Run:
        e, m = run.entrant, run.match
        t0 = self.clock()
        timeout = self._op_timeout()
        try:
            kw: dict[str, Any] = {"overrides": e.overrides} if e.overrides else {}
            applied = Q.apply_repair(pool, ws, e.repair, defect, timeout_s=timeout, pair=pair,
                                     run_stage=self.run_stage, **kw)
        except (*Q.APPLY_FAILED, *Q.UNAVAILABLE) as why:
            EV.failure("heat_apply_failed", why, severity="WARNING", op=m.op, tier=m.tier, **EV.defect_fields(defect))
            key = (WALL if isinstance(why, PanelWallExhausted) else
                   SLICE if (isinstance(why, OpFailed) and why.status == "timeout" and timeout < self.op_timeout_s)
                   else Q.apply_failure_reason(why) if isinstance(why, Q.APPLY_FAILED) else "checker_unavailable")
            self._set(run, applied=False, key=key, reason=f"{type(why).__name__}: {why}", heat="apply",
                      result="not_applied")
            m.wall_s = round(self.clock() - t0, 3)
            return run
        m.params = list(applied.params)
        m.deviation_mm = deviation_of(applied.diffs)
        guarded = self.guard(pool, ws, applied.ws)
        m.volume_change = guarded.volume_change
        if guarded.refused:
            self._set(run, applied=True, key=guarded.refused.split(":", 1)[0], reason=guarded.refused,
                      heat="guard", result="guard_refused")
            m.wall_s = round(self.clock() - t0, 3)
            return run
        local_timeout = self._local_timeout()
        if local_timeout < min(MIN_LOCAL_S, self.local_timeout_s):
            self._set(run, applied=True, key=SLICE, heat="validity", result="not_checked",
                      reason=f"the class loop's wall (a hard cap) leaves {local_timeout:.0f} s: no local check")
            m.wall_s = round(self.clock() - t0, 3)
            return run
        try:
            kw = {"pair": pair} if pair is not None else {}
            local = self.verify(pool, ws, applied.ws, defect, list(applied.touched), timeout_s=local_timeout, **kw)
        except Q.UNAVAILABLE as why:
            EV.failure("heat_check_unavailable", why, severity="WARNING", op=m.op, tier=m.tier, **EV.defect_fields(defect))
            cut = local_timeout < self.local_timeout_s and not isinstance(why, PanelWallExhausted)
            key = WALL if isinstance(why, PanelWallExhausted) else SLICE if cut else "local_check_unavailable"
            self._set(run, applied=True, key=key, reason=f"{type(why).__name__}: {why}", heat="validity",
                      result="not_checked")
            m.wall_s = round(self.clock() - t0, 3)
            return run
        run.checked, run.local_s = True, float(local.seconds or 0.0)
        run.gone, run.new_hard = bool(local.gone), bool(local.new_hard)
        detail = local.detail or {}
        m.predicted_before, m.predicted_after = detail.get("predicted_before"), detail.get("predicted_after")
        if detail.get("member_status"):
            m.mesh_result = f"{detail.get('member')}: {detail['member_status']}" + (
                f"/{detail['member_error_kind']}" if detail.get("member_error_kind") else "")
        elif detail.get("fast_check"):
            m.mesh_result = f"not meshed: {detail['fast_check']}"
        mesh = defect.kind == Q.MESHER and bool(defect.member)
        if local.new_hard:
            self._set(run, applied=True, key="new_defect", reason=local.line(), heat="validity", result="new_defect")
        elif not local.gone:
            self._set(run, applied=True, key="still_there", reason=local.line(), heat="mesh" if mesh else "validity",
                      result="still_there")
        else:
            self._set(run, applied=True, key="", reason=local.line(), heat="mesh" if mesh else "validity",
                      result="survived")
            run.ws = applied.ws
        m.wall_s = round(self.clock() - t0, 3)
        return run

    @staticmethod
    def _rank(run: _Run, index: int) -> tuple:
        m = run.match
        return (not run.gone, run.new_hard, round((m.deviation_mm or 0.0) / DEVIATION_TIE_MM),
                round((m.volume_change or 0.0) / VOLUME_TIE), not run.entrant.seed, len(run.entrant.repair), index)
