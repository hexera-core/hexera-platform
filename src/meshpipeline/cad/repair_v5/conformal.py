# Responsibility: CRS v5's CONFORMAL ASSEMBLY PASS (lever 2, build step 6): once per round, before the per-defect
#                 tournaments, on a multi-body part: misalignment check (`tweak`), fuzzy General-Fuse imprint at a
#                 tolerance stepped from the measured gaps and capped below the smallest real gap, overlap check,
#                 Boolean chosen by volume. Judged like a tournament winner; kept or reverted, and logged.
# Owns: the pass's bounds (`near_bound`, `fuzzy_schedule`, `tweak_plan`, `boolean_for`), its keep rule
#       (`PassFacts`, `keep_decision`) and `ConformalPass` (seq's `pre_round` hook).
# Boundaries: no OCCT here. The measurements are `conformal_kernel` fns; the ops are the registry's (`tweak`,
#             `conformalise` in fragment / cut_by mode, `fuse`); the loss guard is the tournament's; the full
#             verdict is the caller's judge. The loop calls it (`seq.repair_sequentially(pre_round=...)`).
"""Cubit's order for an assembly, before any defect is repaired one at a time:

1. MISALIGNMENT: two bodies whose nearest faces are parallel and a tiny gap apart (measured: `v5_body_gaps`) get the
   smaller body translated onto its partner (`tweak`), when the gap is at most `near_bound` - the fidelity bound
   min(0.1 mm, 1e-3 x diagonal), the same line that separates a modelling slip from a real clearance. The moved
   distance is the deviation.
2. IMPRINT: `conformalise` in FRAGMENT mode (BOPAlgo_Builder, SetFuzzyValue) over the bodies in contact or near
   contact, at a fuzzy value STEPPED up from the measured gaps (1e-7, then each near gap x1.1, smallest first) and
   CAPPED below the smallest real gap (one over the bound): a fuzzy that reached a real gap would close it. The first
   step that applies (the op itself refuses an invalid result or one with no shared face) is kept.
3. OVERLAP CHECK after the imprint (re-measured), and each overlapping pair resolved by a Boolean chosen BY VOLUME:
   the overlap a large share of the smaller body (>= `FUSE_SHARE`) -> `fuse`; else the smaller is cut by the larger
   (`conformalise cut_by:<larger>`, which also makes the pair conformal).

THE PASS IS JUDGED AS ONE CANDIDATE: kept only when the loss guard passes, no new hard code and no newly failing
mesher appear in a full verdict, and the overlaps + crossings it targets drop (or the part is clean). Otherwise the
whole pass is reverted and the log says why. Every step is a `Match` of tier `conformal_pass`, and so is the
pass's own verdict.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import _forward, _new_diffs
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, validate_call
from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
from meshpipeline.cad.repair_v4.workspace import Workspace
from meshpipeline.cad.repair_v5 import tournament as T

TIER = "conformal_pass"

#: = body_kernel.fidelity_bound (not imported: parent modules never import a kernel module)
NEAR_ABS_MM = 0.1
NEAR_REL = 1e-3
#: gaps are measured out to this many near bounds: a gap past the bound and inside the reach is a REAL gap (it caps
#: the fuzzy); none measured there -> the reach itself is the cap
REACH_BOUNDS = 10.0
#: the first fuzzy step: the kernel's own precision (an exact contact still needs a hair of fuzzy across a STEP
#: round trip)
FIRST_FUZZY_MM = 1e-7
MAX_FUZZY_STEPS = 4
#: overlap / smaller body's volume at or above this -> fuse (the smaller is mostly inside: a cut would gut it)
FUSE_SHARE = 0.5
MAX_TWEAKS = 8
MAX_IMPRINT_BODIES = 32
MAX_BOOLEANS = 16
CONTACT_MM = 1e-7
MEASURE_TIMEOUT_S = 120.0
#: a body-gap measurement is not started with less than this of the pass's share left (CRS v5 r2)
MIN_MEASURE_S = 10.0
#: mesher kinds the pass targets: an edge through a face, and two faces' facets crossing (non-conformal contact)
CROSSING_KINDS = frozenset({"edge_through_face", "overlapping_facets"})


def near_bound(diagonal_mm: float | None) -> float:
    return min(NEAR_ABS_MM, NEAR_REL * float(diagonal_mm)) if diagonal_mm else NEAR_ABS_MM


def fuzzy_schedule(near_gaps: Sequence[float], cap_mm: float) -> list[float]:
    """The fuzzy values to try, in order: FIRST_FUZZY_MM, then each measured near gap x FUZZY_MARGIN (smallest first),
    keeping only values strictly below `cap_mm` and strictly increasing; at most MAX_FUZZY_STEPS."""
    out: list[float] = []
    for v in [FIRST_FUZZY_MM, *sorted(g * Q.FUZZY_MARGIN for g in near_gaps if g > CONTACT_MM)]:
        if v < cap_mm and (not out or v > out[-1] * (1 + 1e-9)):
            out.append(v)
    return out[:MAX_FUZZY_STEPS]


def cap_of(pairs: Sequence[dict], bound_mm: float, reach_mm: float) -> float:
    """The smallest REAL gap (over the near bound) that must stay open; the reach when none was measured."""
    return min((float(p["gap_mm"]) for p in pairs if float(p["gap_mm"]) > bound_mm), default=reach_mm)


def tweak_plan(pairs: Sequence[dict], volumes: dict, bound_mm: float) -> list[tuple[int, int, float]]:
    """(moved, partner, gap) kernel solid indices for each misalignment: parallel nearest faces, a gap in
    (contact, bound]; the SMALLER body moves. Smallest gap first; each body in at most one tweak."""
    used: set[int] = set()
    out: list[tuple[int, int, float]] = []
    for p in sorted(pairs, key=lambda p: float(p["gap_mm"])):
        gap = float(p["gap_mm"])
        if not (CONTACT_MM < gap <= bound_mm and p.get("parallel")):
            continue
        a, b = int(p["a"]), int(p["b"])
        if a in used or b in used:
            continue
        va, vb = float(volumes.get(str(a), 0.0)), float(volumes.get(str(b), 0.0))
        moved, partner = (a, b) if va <= vb else (b, a)
        used |= {a, b}
        out.append((moved, partner, gap))
        if len(out) >= MAX_TWEAKS:
            break
    return out


def boolean_for(overlap_mm3: float, vol_a: float, vol_b: float, a: StableId, b: StableId
                ) -> tuple[str, list[StableId], dict, str]:
    """(op, targets, params, why) resolving one overlapping pair, chosen by volume."""
    small, large = (a, b) if vol_a <= vol_b else (b, a)
    share = overlap_mm3 / max(min(vol_a, vol_b), 1e-12)
    if share >= FUSE_SHARE:
        return "fuse", sorted([a, b]), {"fuzzy_mm": 0.0}, f"overlap is {share:.0%} of the smaller body {small}: fuse"
    return ("conformalise", sorted([a, b]), {"mode": f"cut_by:{large}", "fuzzy_mm": 0.0},
            f"overlap is {share:.0%} of the smaller body {small}: cut it by the larger {large}")


# --- the keep rule --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PassFacts:
    """What the pass is judged on, read off one full verdict."""

    hard: frozenset[str]
    overlaps: int
    crossings: int
    failing: frozenset[str]
    clean: bool

    @property
    def targeted(self) -> int:
        return self.overlaps + self.crossings


def facts_of(verdict: Any) -> PassFacts:
    hard = [getattr(f, "code", None) for f in getattr(getattr(verdict, "valid", None), "hard", None) or ()]
    panel = getattr(verdict, "panel", None) or ()
    failing = [m for m in panel if str(getattr(m, "status", "")) != "pass"]
    return PassFacts(hard=frozenset(c for c in hard if c), overlaps=sum(c == "body_overlap" for c in hard),
                     crossings=sum(getattr(m, "error_kind", None) in CROSSING_KINDS for m in failing),
                     failing=frozenset(str(getattr(m, "name", "?")) for m in failing),
                     clean=bool(getattr(verdict, "clean", False)))


def keep_decision(before: PassFacts, after: PassFacts, refused: str | None = None) -> tuple[bool, str, str]:
    """(keep, the Match result, why). Kept only when the guard passes, no hard code and no failing mesher is new,
    and the targeted failures (overlaps + crossing meshers) drop - or the part is clean."""
    if refused:
        return False, "guard_refused", refused
    new_codes = sorted(after.hard - before.hard)
    if new_codes:
        return False, "new_defect", f"new hard code(s): {', '.join(new_codes)}"
    newly = sorted(after.failing - before.failing)
    if newly:
        return False, "new_defect", f"mesher(s) that passed now fail: {', '.join(newly)}"
    if after.clean:
        return True, "won", "the part is clean"
    if after.overlaps > before.overlaps or after.crossings > before.crossings or after.targeted >= before.targeted:
        return False, "still_there", (f"overlaps {before.overlaps}->{after.overlaps}, crossings "
                                      f"{before.crossings}->{after.crossings}: no drop")
    return True, "won", f"overlaps {before.overlaps}->{after.overlaps}, crossings {before.crossings}->{after.crossings}"


# --- the pass -------------------------------------------------------------------------------------------------


def _bodies(ws: Workspace) -> list[StableId]:
    return sorted(s for s in (getattr(ws, "ids", None) or {}) if str(s)[:1] == "B")


def _by_index(ws: Workspace) -> dict[int, StableId]:
    return {int(i): s for s, i in (getattr(ws, "indices", None) or {}).items() if str(s)[:1] == "B"}


def _carried(ids: Sequence[StableId], ws_from: Workspace, ws_to: Workspace) -> list[StableId]:
    if ws_to is ws_from:
        return [i for i in ids if i in ws_to.ids]
    return sorted(_forward(ids, _new_diffs(ws_from, ws_to)) & {s for s in ws_to.ids if s[:1] == "B"})


@dataclass
class ConformalPass:
    """seq's `pre_round` hook: (pool, state, its full verdict, *, counters, step) -> (state, verdict) when the pass
    is kept, else None (reverted, or nothing to do)."""

    judge: Any
    original: str
    log: T.MatchLog = field(default_factory=T.MatchLog)
    guard: T.Guard = T.loss_guard
    op_timeout_s: float = 300.0
    clock: Callable[[], float] = time.monotonic
    deadline: float | None = None
    trace: Any = None
    #: per-round lines for the row (the kept/reverted verdicts), beside the step lines
    outcomes: list[str] = field(default_factory=list)
    #: CRS v5 r2: the seconds ONE round's pass may spend on its measurements and steps (None: up to `deadline`).
    #: Measured on the step-13 wave: the pass took 150-1325 s on 14 files that then tried NO defect (1e6f8d8e11c7
    #: 1142 s, f027b94e6e67 1325 s, 9d0d49a626e9 1320 s): a re-measure after every Boolean (65-120 s each on 4ef8e443ffa2)
    #: ran until the class loop's whole share was gone. Its own full check (the keep rule) still runs after.
    budget_s: float | None = None
    _until: float | None = None

    def __call__(self, pool: Any, ws: Workspace, verdict: Any, *, counters: Q.SeqCounters,
                 step: Callable[[str], None]) -> tuple[Workspace, Any] | None:
        now = self.clock()
        ends = [t for t in (self.deadline, now + self.budget_s if self.budget_s is not None else None) if t is not None]
        self._until = min(ends) if ends else None
        before = facts_of(verdict)
        if len(_bodies(ws)) < 2:
            return None                                     # single body: the pass does not apply (no line)
        if before.clean or not before.targeted:
            step("conformal pass: not run (no body_overlap or crossing mesher failure to judge it on)")
            return None
        bound = near_bound(ws.part_diag_mm)
        reach = REACH_BOUNDS * bound
        measured = self._measure(pool, ws, reach, step)
        if measured is None:
            return None
        pairs, volumes = measured["pairs"], measured.get("volumes") or {}
        by_index = _by_index(ws)
        near = [float(p["gap_mm"]) for p in pairs if CONTACT_MM < float(p["gap_mm"]) <= bound]
        cap = cap_of(pairs, bound, reach)
        schedule = fuzzy_schedule(near, cap)
        overlapping = [p for p in pairs if (p.get("overlap_mm3") or 0) > 0 or ("overlap_mm3" in p
                                                                               and p["overlap_mm3"] is None)]
        cause = (f"{len(_bodies(ws))} bodies, {len(pairs)} pairs within {reach:.3g} mm: {len(near)} near "
                 f"(<= {bound:.3g} mm), {len(overlapping)} overlapping; real-gap cap {cap:.3g} mm"
                 + (f" [measurement truncated: {measured['truncated']}]" if measured.get("truncated") else ""))
        step(f"conformal pass: {cause}; fuzzy steps {[float(f'{v:.3g}') for v in schedule]}")
        current, records = ws, []

        # 1. misalignment
        tweaked: set[int] = set()
        for moved, partner, gap in tweak_plan(pairs, volumes, bound):
            mv = _carried([by_index[moved]], ws, current) if moved in by_index else []
            pt = _carried([by_index[partner]], ws, current) if partner in by_index else []
            if len(mv) != 1 or len(pt) != 1:
                step(f"  tweak S{moved} onto S{partner}: not aimed (an earlier step changed the bodies)")
                continue
            got = self._step(pool, current, "tweak", [mv[0], pt[0]], {"max_mm": bound}, cause, records, step,
                             note=f"gap {gap:.3g} mm")
            if got is not current:
                tweaked |= {moved, partner}
            current = got

        # 2. imprint (fragment) over the contact bodies that do not overlap anything, fuzzy stepped
        in_overlap = {int(p["a"]) for p in overlapping} | {int(p["b"]) for p in overlapping}
        top = schedule[-1] if schedule else 0.0
        contact = sorted(({int(x) for p in pairs if float(p["gap_mm"]) <= max(top, CONTACT_MM)
                           for x in (p["a"], p["b"])} | tweaked) - in_overlap)
        bodies = _carried([by_index[i] for i in contact if i in by_index], ws, current)[:MAX_IMPRINT_BODIES]
        if len(bodies) >= 2 and schedule:
            for fuzzy in schedule:
                got = self._step(pool, current, "conformalise", bodies, {"mode": "fragment", "fuzzy_mm": fuzzy},
                                 cause, records, step, note=f"imprint at fuzzy {fuzzy:.3g} mm")
                if got is not current:
                    current = got
                    break
        else:
            step(f"  imprint: not run ({len(bodies)} non-overlapping contact bodies, {len(schedule)} fuzzy steps)")

        # 3. overlap check after the imprint, Boolean by volume
        # RE-MEASURED AFTER EVERY APPLIED BOOLEAN: a fuse retires one body's id and a cut renames pieces, so the
        # overlaps measured before it no longer name the live bodies (jig, 575448ea0b6f: 2 of 22 pairs resolved
        # from one measurement, the rest "changed by an earlier Boolean"). Largest overlap first; a pair whose
        # Boolean did not apply is not retried on the same state.
        again = (self._measure(pool, current, reach, step) if current is not ws else measured) if overlapping else None
        tried: set[tuple[StableId, StableId]] = set()
        booleans = 0
        while again is not None and booleans < MAX_BOOLEANS:
            spent = self._left()
            if spent is not None and spent <= 0:
                step("  overlap Booleans: stopped (the pass's share of the wall is spent)")
                break
            idx_now, vols = _by_index(current), again.get("volumes") or {}
            still = sorted((p for p in again["pairs"] if (p.get("overlap_mm3") or 0) > 0
                            and int(p["a"]) in idx_now and int(p["b"]) in idx_now
                            and tuple(sorted((idx_now[int(p["a"])], idx_now[int(p["b"])]))) not in tried),
                           key=lambda p: -float(p["overlap_mm3"]))
            if not still:
                break
            p = still[0]
            a, b = idx_now[int(p["a"])], idx_now[int(p["b"])]
            tried.add(tuple(sorted((a, b))))  # type: ignore[arg-type]
            op, targets, params, why = boolean_for(float(p["overlap_mm3"]), float(vols.get(str(p["a"]), 0)),
                                                   float(vols.get(str(p["b"]), 0)), a, b)
            booleans += 1
            got = self._step(pool, current, op, targets, params, cause, records, step, note=why)
            if got is not current:
                current, tried = got, set()
                again = self._measure(pool, current, reach, step)

        if current is ws:
            self._final(records, cause, False, "not_applied", "nothing applied", step)
            return None
        return self._judge(pool, ws, current, verdict, before, cause, records, counters, step)

    # -- pieces --

    def _left(self) -> float | None:
        return None if self._until is None else self._until - self.clock()

    def _measure(self, pool: Any, ws: Workspace, reach: float, step: Callable[[str], None]) -> dict | None:
        left = self._left()
        if left is not None and left < MIN_MEASURE_S:
            step(f"conformal pass: measurement not run (the pass's share of the wall is spent: {left:.0f} s left)")
            return None
        timeout = MEASURE_TIMEOUT_S if left is None else min(MEASURE_TIMEOUT_S, left)
        try:
            result = pool.call("v5_body_gaps", {"brep_path": ws.brep_path, "reach_mm": reach,
                                                "part_diag_mm": ws.part_diag_mm}, timeout_s=timeout)
        except Exception as why:  # noqa: BLE001 - an unmeasurable part skips the pass; it never loses the file
            step(f"conformal pass: not run (measurement failed: {type(why).__name__}: {why})")
            return None
        if not getattr(result, "ok", False) or not getattr(result, "value", None):
            step(f"conformal pass: not run (measurement failed: {getattr(result, 'status', '?')}: "
                 f"{getattr(result, 'error', '')})")
            return None
        return dict(result.value)

    def _step(self, pool: Any, ws: Workspace, op: str, targets: list[StableId], params: dict, cause: str,
              records: list[T.Match], step: Callable[[str], None], *, note: str) -> Workspace:
        m = T.Match(defect_id=TIER, defect_class=TIER, cause=cause[:160], tier=TIER, op=op, params=[dict(params)],
                    applied=False, reason_key="", reason=note, heat="apply", result="", pair="+".join(targets))
        t0 = self.clock()
        left = self._left()
        if left is not None and left <= 0:
            m.result, m.reason_key = "not_run", "not_run"
            m.reason = ("the class loop's wall" if self.deadline is not None and t0 >= self.deadline
                        else "the pass's share of the wall")
            records.append(m)
            step(m.line())
            return ws
        timeout = self.op_timeout_s if left is None else max(1.0, min(self.op_timeout_s, left))
        try:
            errors = validate_call(op, list(targets), dict(params), ws)
            if errors:
                raise Q.NoParameters(f"{op} rejected the pass's parameters: {errors[0]}")
            new, diff = REGISTRY[op].apply(pool, ws.fork(), list(targets), dict(params), timeout_s=timeout)
            declined = diff.params.get("declined") if isinstance(diff.params, dict) else None
            if declined is not None:
                detail = diff.params.get("detail")
                raise OpDeclined(f"{declined}: {detail}" if detail else str(declined))
        except Q.APPLY_FAILED as why:
            m.result, m.reason_key = "not_applied", Q.apply_failure_reason(why)
            m.reason = f"{note}; {type(why).__name__}: {why}"[:400]
            m.wall_s = round(self.clock() - t0, 3)
            records.append(m)
            step(m.line())
            return ws
        m.applied, m.result, m.heat = True, "survived", "apply"
        m.deviation_mm = T.deviation_of([diff])
        m.wall_s = round(self.clock() - t0, 3)
        records.append(m)
        step(m.line())
        return new

    def _final(self, records: list[T.Match], cause: str, applied: bool, result: str, why: str,
               step: Callable[[str], None], volume_change: float | None = None) -> None:
        steps = [r for r in records if r.applied]
        m = T.Match(defect_id=TIER, defect_class=TIER, cause=cause[:160], tier=TIER, op=TIER,
                    params=[{"steps": "+".join(r.op for r in steps) or "-"}], applied=applied,
                    reason_key="" if result == "won" else (why.split(":", 1)[0] if result == "guard_refused"
                                                           else result),
                    reason=why, heat="guard" if result == "guard_refused" else "validity", result=result,
                    deviation_mm=max((r.deviation_mm or 0.0 for r in steps), default=None) if steps else None,
                    volume_change=volume_change)
        records.append(m)
        line = f"conformal pass -> {'KEPT' if result == 'won' else 'reverted'}: {why}"
        step(line)
        self.outcomes.append(line)
        self.log.records.extend(records)
        if self.trace is not None:
            for r in records:
                self.trace.record("v5_match", **r.row())

    def _judge(self, pool: Any, ws: Workspace, current: Workspace, verdict: Any, before: PassFacts, cause: str,
               records: list[T.Match], counters: Q.SeqCounters, step: Callable[[str], None]
               ) -> tuple[Workspace, Any] | None:
        guarded = self.guard(pool, ws, current)
        if guarded.refused:
            self._final(records, cause, True, "guard_refused", guarded.refused, step, guarded.volume_change)
            return None
        t0 = self.clock()
        if self.deadline is not None and t0 >= self.deadline:
            # CRS v5 r2: the class loop's wall is a hard cap - no full check is started past it
            self._final(records, cause, True, "not_checked", "the class loop's wall: no full check started", step,
                        guarded.volume_change)
            return None
        try:
            after = self.judge.full(pool, current, self.original)
        except (*Q.UNAVAILABLE, InconclusiveVerdict) as why:
            self._final(records, cause, True, "not_checked", f"the full check could not be had: {why}", step,
                        guarded.volume_change)
            return None
        counters.full_checks += 1
        counters.full_s += self.clock() - t0
        if getattr(after, "inconclusive", False):
            self._final(records, cause, True, "not_checked", "the full check was inconclusive", step,
                        guarded.volume_change)
            return None
        keep, result, why = keep_decision(before, facts_of(after), None)
        self._final(records, cause, True, result, why, step, guarded.volume_change)
        return (current, after) if keep else None
