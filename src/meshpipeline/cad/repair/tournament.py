# Responsibility: the PURE half of tournament repair - what a candidate is, what a rehearsal of
#                 one reported, and the one scoring rule that orders them.
# Owns: the candidate sources and their precedence, plan-key deduplication, the lexicographic
#       score, the ranking, the eligibility gate, and the two postures (on/off and K).
# Boundaries: it touches NO OCP, starts no process, imports no mesher and performs no rehearsal.
#             Running a candidate on a clone is `Scratch`'s job and offering the winner is
#             `try_package`'s. It never raises, and it never mutates its inputs - `rank` returns a
#             new tuple and the caller's list is left as it was.
# Collaborates with: cad/repair/package.py (`TargetPackage`), cad/repair/ladder.py (`TierResult`,
#                    the only way a candidate may be probed), cad/repair/investigator_tools.py
#                    (`Scratch`, the candidate isolation a rehearsal will use).
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.ladder import TierResult
from meshpipeline.cad.repair.package import TargetPackage

logger = logging.getLogger(__name__)

#: Where a candidate came from, IN PRECEDENCE ORDER: the deterministic rules author first, then
#: the one-shot model, then the investigator. The order is a cost order as much as a trust one -
#: on an exact tie the cheapest and most reproducible candidate wins, so a tournament that
#: changes nothing measurable cannot quietly promote a model's answer over the rules'.
RULES = "rules"
ONE_SHOT = "one_shot"
INVESTIGATOR = "investigator"

SOURCE_ORDER: tuple[str, ...] = (RULES, ONE_SHOT, INVESTIGATOR)

#: A rehearsal's three endings. `not_rehearsed` is "never run" (no budget, no clone, K reached)
#: and `failed` is "run and came apart"; neither is a result, and both stay in the record.
REHEARSED = "rehearsed"
NOT_REHEARSED = "not_rehearsed"
FAILED = "failed"

#: K=1 IS TODAY'S BEHAVIOUR - one candidate, rehearsed or not, and no tournament at all. The
#: default is therefore the fail-closed one: with no flag plumbed, nothing changes.
K_DEFAULT = 1
K_MIN = 1
#: Above this, the rehearsals cost more than the choice is worth: each candidate is a clone plus
#: its own ladder run, so K multiplies the dearest part of a repair run.
K_MAX = 6

#: The on-value of the tournament posture. Anything else - unset, "on", "true", "Live" - reads
#: off, the same single spelling the other v3 postures use.
_ON = "live"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One proposed repair of one region, and where it came from.

    `plan_key` identifies the candidate BY ITS OPERATIONS and nothing else: two sources that
    propose the same plan have proposed the same candidate, whatever intent they wrote on it, and
    rehearsing it twice would pay twice for one answer.
    """

    source: str
    package: TargetPackage
    plan_key: str


@dataclass(frozen=True, slots=True)
class Rehearsal:
    """What rehearsing one candidate on its own clone reported.

    A `Rehearsal` IS NOT A REVIEW. `kept_ops` is a claim about the operations - they applied and
    stuck - not about semantic acceptability, and a scratch session can never commit, so the
    winner still has to pass review on the real session. A tournament win is a nomination.

    `cleared` / `revealed` / `remaining` are the accounting the score is actually made of:
    revealed defects are the ones this plan created, and they are the thing no amount of cleared
    ones buys off.
    """

    candidate: Candidate
    status: str
    kept_ops: int
    tier0: TierResult | None
    tier1: TierResult | None
    tier2: TierResult | None
    cleared: frozenset[str]
    revealed: frozenset[str]
    remaining: frozenset[str]
    deviation_mm: float | None
    seconds: float
    reason: str = ""


def live() -> bool:
    """Whether the tournament posture is on. Fails closed: only the exact `live` is on."""
    return str(getattr(rcfg, "CAD_REPAIR_TOURNAMENT", "off") or "off") == _ON


def k() -> int:
    """How many candidates one region may be given, clamped into `[K_MIN, K_MAX]`.

    An unreadable or absent value is `K_DEFAULT`, which is 1 - never a guess at what the operator
    meant. A value outside the range is clamped AND LOGGED, because a silently honoured 99 would
    be a budget decision nobody made.
    """
    raw = getattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", K_DEFAULT)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("CAD_REPAIR_TOURNAMENT_K is not an integer (%r); using K=%d", raw,
                       K_DEFAULT)
        return K_DEFAULT
    clamped = max(K_MIN, min(K_MAX, value))
    if clamped != value:
        logger.warning("CAD_REPAIR_TOURNAMENT_K=%r is outside [%d, %d]; using K=%d", raw, K_MIN,
                       K_MAX, clamped)
    return clamped


def plan_key(package: TargetPackage) -> str:
    """The sha256 of the canonical JSON of `package.plan`.

    Canonical means sorted keys and no insignificant whitespace, so two authors that wrote the
    same operations in a different dict order collide - which is the point. Anything the plan
    carries that JSON cannot represent is stringified rather than raising: an unhashable plan
    would otherwise take a repair run down over a scoring detail.
    """
    text = json.dumps([dict(entry) for entry in package.plan], sort_keys=True,
                      separators=(",", ":"), default=repr)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate(source: str, package: TargetPackage) -> Candidate:
    """`package` as a candidate from `source`, with its key computed once."""
    return Candidate(source=str(source), package=package, plan_key=plan_key(package))


def dedupe(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """`candidates` with equal plans collapsed, KEEPING THE FIRST SOURCE IN PRECEDENCE ORDER.

    Distinct plans keep their input order, so the caller's own ordering survives; only the
    duplicates are resolved, and they resolve towards rules, then one-shot, then investigator. An
    unknown source sorts after every known one rather than being dropped.
    """
    best: dict[str, Candidate] = {}
    order: list[str] = []
    for cand in candidates:
        key = cand.plan_key
        if key not in best:
            best[key] = cand
            order.append(key)
        elif _source_rank(cand.source) > _source_rank(best[key].source):
            best[key] = cand
    return tuple(best[key] for key in order)


def _source_rank(source: str) -> int:
    """Bigger is better, so it drops straight into the score. An unknown source is worst."""
    try:
        return len(SOURCE_ORDER) - SOURCE_ORDER.index(source)
    except ValueError:
        return 0


def _tier_rank(result: TierResult | None) -> int:
    """A rung's three-valued answer as a rank: a measured pass 2, no answer 1, a refusal 0.

    A None ranks above a False AT THIS POSITION ONLY and never above a measured True - reading a
    timeout as a refusal would rank a candidate nobody could measure below one that was measured
    and failed, and reading it as a pass would be worse still.
    """
    if result is None:
        return 1
    return {True: 2, None: 1, False: 0}[result.passed]


def score(r: Rehearsal) -> tuple:
    """The lexicographic key of a rehearsal, LARGER IS BETTER.

    In order: it was actually rehearsed; tier 0 passed; it revealed nothing; the tier-2 answer;
    the tier-1 answer; how much it cleared; how little it moved the surface; how few operations
    it kept; and its source. The shape of the key is the policy: correctness first, then damage,
    then evidence, then benefit, then cost, then determinism. Nothing below "revealed nothing"
    can buy a revealed defect off, because the positions above it are already decided.
    """
    deviation = r.deviation_mm if r.deviation_mm is not None else math.inf
    return (
        r.status == REHEARSED,
        r.tier0 is not None and r.tier0.passed is True,
        len(r.revealed) == 0,
        _tier_rank(r.tier2),
        _tier_rank(r.tier1),
        len(r.cleared),
        -float(deviation),
        -int(r.kept_ops),
        _source_rank(r.candidate.source),
    )


def rank(rehearsals: Iterable[Rehearsal]) -> tuple[Rehearsal, ...]:
    """`rehearsals` best first. PURE AND STABLE: equal scores keep input order, the input is not
    mutated, and NOTHING IS DROPPED - a loser stays in the record so the run can say it lost."""
    return tuple(sorted(rehearsals, key=score, reverse=True))


def eligible(r: Rehearsal) -> bool:
    """Whether `r` may be offered to `try_package` at all.

    Four conditions, all required: it was rehearsed, tier 0 passed on the clone, it revealed no
    new defect, and it cleared something. The last is what stops a no-op winning a tournament of
    one; the third is what stops the loop trading a known defect for an unknown one.
    """
    return (r.status == REHEARSED
            and r.tier0 is not None and r.tier0.passed is True
            and not r.revealed
            and bool(r.cleared))


def winner(rehearsals: Iterable[Rehearsal]) -> Rehearsal | None:
    """The best ELIGIBLE rehearsal, or None when no candidate survived.

    None is a first-class answer and the caller must say so rather than offering the best loser:
    a tournament where nothing survived has learned that this region's candidates do not work,
    which is information, and dressing one up as a winner would spend a review on it.
    """
    for r in rank(rehearsals):
        if eligible(r):
            return r
    return None


# -- rehearsal ---------------------------------------------------------------------------------
#
# THE PART THAT ACTUALLY REHEARSES, and the only part of this module that touches a shape. It
# still owns no kernel code: every shape operation goes through `Scratch` (the clone), `ladder`
# (the probe) and `deviation` (the measurement), each imported LAZILY so that scoring a recorded
# rehearsal - the pure half above - never pays for a kernel load.
#
# A REHEARSAL IS NOT A REVIEW, and a scratch session cannot commit (`session.try_package` refuses
# one before it reads the predicate). That is what makes a loser free, and it is also what makes
# the winner a NOMINATION: it must still pass `package_review` on the real session.

#: Below this, a rehearsal is not worth opening a clone for: the copy, three rungs and a
#: deviation measurement cannot finish, so the honest answer is `not_rehearsed` rather than a
#: half-measured one that would be SCORED against candidates that did finish.
REHEARSAL_MIN_S = 2.0

#: Tier 2 is asked only when at least a quarter of its own hard cap is left. A rung started with
#: less than that is a rung that will time out, and a timeout costs the whole-part probe's setup
#: for a None that ranks no better than not asking at all.
TIER2_MIN_SHARE = 4

#: The deviation measurement's own ceiling, whatever the rehearsal has left.
DEVIATION_BUDGET_S = 10.0


def _time_source(clock):
    """The clock to read seconds from. An `InvestigationClock` carries the run's own in `.clock`,
    so a rehearsal never constructs a second one - two clocks over one deadline is how a region
    starves its successors."""
    import time

    source = getattr(clock, "clock", None)
    return source if callable(source) else time.monotonic


def _left(clock, started: float, wall: float, now) -> float:
    """Seconds this rehearsal may still spend: its own share, AND the run's if a clock knows.

    The lesser of the two, so a region that is out of time stops even with wall left, and a wall
    that is spent stops even with run left. Never negative.
    """
    left = float(wall) - (now() - float(started))
    ask = getattr(clock, "left", None)
    if ask is not None:
        try:
            left = min(left, float(ask(started, wall)))
        except Exception:  # noqa: BLE001 - a clock that cannot answer does not stop a rehearsal
            logger.debug("tournament: the clock could not say what is left", exc_info=True)
    return max(0.0, left)


def _kept_status() -> str:
    """The session's own name for a kept operation, READ from the session rather than copied: a
    second spelling of "kept" here would let a rehearsal disagree with the commit it rehearses.
    Imported lazily so the pure half's import stays cheap."""
    from meshpipeline.cad.repair.session import KEPT

    return KEPT


def _fingerprints(defects, region, diagonal_mm: float, scope) -> frozenset[str]:
    """The scoped defect list as a set of fingerprints - ONE scope, both sides of the comparison.

    The session's progress rule, unchanged: a whole-shape list compared against a scoped scan is
    a comparison between two different parts, so both sides go through `localize.scoped_to`.
    """
    return frozenset(str(d.fingerprint)
                     for d in scope(tuple(defects), region, diagonal_mm=float(diagonal_mm))
                     if getattr(d, "fingerprint", ""))


def _tier2_floor() -> float:
    """The least time tier 2 may be started with. Read from the oracle rather than copied, so the
    rung's own hard cap and this floor cannot drift apart."""
    from meshpipeline.cad.repair import oracle

    return float(oracle.TIER2_HARD_CAP_S) / TIER2_MIN_SHARE


def _ended(cand: Candidate, status: str, *, seconds: float, reason: str = "", kept_ops: int = 0,
           tier0=None, tier1=None, tier2=None, cleared=frozenset(), revealed=frozenset(),
           remaining=frozenset(), deviation_mm: float | None = None) -> Rehearsal:
    return Rehearsal(candidate=cand, status=status, kept_ops=int(kept_ops), tier0=tier0,
                     tier1=tier1, tier2=tier2, cleared=frozenset(cleared),
                     revealed=frozenset(revealed), remaining=frozenset(remaining),
                     deviation_mm=deviation_mm, seconds=float(seconds), reason=reason)


def rehearse(cand: Candidate, *, session_shape, original, defects, region, tolerance_mm: float,
             budget_s: float, clock=None, reserved: frozenset[str] = frozenset(),
             diagonal_mm: float | None = None, make_scratch=None, run_tier=None, measure=None,
             scope=None) -> Rehearsal:
    """Play `cand` on ITS OWN CLONE of `session_shape` and report what happened. NEVER RAISES.

    In order: open a clone; play the plan item by item through the clone's `try_op`, stopping at
    the FIRST item it declines or rolls back (the session's package rule - one failed item fails
    the whole package); ask tier 0, then tier 1 scoped to `region`, then tier 2 on the whole part
    when there is time for it; and measure how far the kept edits moved the surface.

    `session_shape` IS ONLY EVER COPIED. It is handed to `Scratch.of`, which makes the one copy
    in the programme, and no operation here is given the session, the session's shape or any
    handle another candidate holds - so two candidates in one tournament cannot meet.

    An exception anywhere is `FAILED` with the exception type as the reason: a candidate that
    came apart is a result about that candidate, not a repair run brought down over a tournament.
    The four injection seams (`make_scratch`, `run_tier`, `measure`, `scope`) are test seams with
    the real collaborators as defaults, for the same reason the session's are.
    """
    now = _time_source(clock)
    started = now()
    kept = 0

    def spent() -> float:
        return now() - started

    if float(budget_s) < REHEARSAL_MIN_S:
        return _ended(cand, NOT_REHEARSED, seconds=spent(),
                      reason=f"no_time: {float(budget_s):.6g}s offered, {REHEARSAL_MIN_S:.6g}s "
                             "is the least a rehearsal can use")
    try:
        if make_scratch is None or run_tier is None or measure is None or scope is None:
            from meshpipeline.cad.repair import deviation as _deviation
            from meshpipeline.cad.repair import ladder as _ladder
            from meshpipeline.cad.repair import localize as _localize
            from meshpipeline.cad.repair.investigator_tools import Scratch

            make_scratch = make_scratch or Scratch.of
            run_tier = run_tier or _ladder.run_tier
            measure = measure or _deviation.measure_deviation
            scope = scope or _localize.scoped_to

        diagonal = float(getattr(original, "diagonal_mm", 0.0) if diagonal_mm is None
                         else diagonal_mm)
        before = _fingerprints(defects, region, diagonal, scope)

        scratch = make_scratch(session_shape, original=original, defects=tuple(defects),
                               reserved=frozenset(reserved), remaining_s=float(budget_s),
                               clock=now)
        keep = _kept_status()

        for index, entry in enumerate(cand.package.plan):
            item = dict(entry)
            if _left(clock, started, budget_s, now) <= 0.0:
                return _ended(cand, FAILED, seconds=spent(), kept_ops=kept,
                              reason=f"budget: spent after {index} of "
                                     f"{len(cand.package.plan)} operation(s)")
            verdict = scratch.session.try_op(
                item["op"], tuple(item["targets"]), item["defect_code"],
                tolerance_mm=float(item.get("tolerance_mm", tolerance_mm)), within=region)
            if getattr(verdict, "status", "") != keep:
                # ONE FAILED ITEM FAILS THE PACKAGE. The predicate was written about the whole
                # plan, so a partially applied one was never what the author claimed - and a plan
                # that carries an operation the session refuses cannot commit.
                return _ended(cand, FAILED, seconds=spent(), kept_ops=kept,
                              reason=f"{getattr(verdict, 'op', item['op'])}: "
                                     f"{verdict.status} {getattr(verdict, 'reason', '')}".strip())
            kept += 1

        tier0 = run_tier(scratch.shape, "0", diagonal_mm=diagonal,
                         budget_s=_left(clock, started, budget_s, now))
        tier1 = run_tier(scratch.shape, "1", diagonal_mm=diagonal, within=region,
                         budget_s=_left(clock, started, budget_s, now))
        left = _left(clock, started, budget_s, now)
        reason = ""
        tier2 = None
        if left >= _tier2_floor():
            tier2 = run_tier(scratch.shape, "2", diagonal_mm=diagonal, within=None, budget_s=left)
        else:
            reason = f"tier2 skipped: no_time ({left:.6g}s left, {_tier2_floor():.6g}s needed)"

        moved = measure(session_shape, scratch.shape, tuple(scratch.edited_faces()),
                        budget_s=min(DEVIATION_BUDGET_S, _left(clock, started, budget_s, now)))
        after = _fingerprints(scratch.defects(), region, diagonal, scope)
        return _ended(cand, REHEARSED, seconds=spent(), kept_ops=kept, tier0=tier0, tier1=tier1,
                      tier2=tier2, cleared=before - after, revealed=after - before,
                      remaining=before & after, deviation_mm=getattr(moved, "mm", None),
                      reason=reason)
    except Exception as exc:  # noqa: BLE001 - a candidate that came apart is a result
        logger.info("tournament: rehearsal of a %s candidate came apart (%s: %s)", cand.source,
                    type(exc).__name__, exc)
        return _ended(cand, FAILED, seconds=spent(), kept_ops=kept,
                      reason=f"{type(exc).__name__}: {exc}")


def rehearse_all(candidates: Iterable[Candidate], *, wall_s: float, clock=None,
                 limit: int | None = None, **kw) -> tuple[Rehearsal, ...]:
    """Rehearse `candidates` SEQUENTIALLY on one wall, best effort. NEVER RAISES.

    SEQUENTIALLY, and not as an accident of implementation: OCP holds the GIL, so parallel
    rehearsals would not go faster, and one clone at a time is what keeps the memory of a
    tournament bounded by one extra copy rather than by K.

    `limit` is K (`k()` when not given), and a candidate past it is `NOT_REHEARSED` - which is
    why K=1 is TODAY'S BEHAVIOUR: one candidate rehearsed, the rest recorded as never run.
    Each rehearsal gets `min(what is left / how many are still owed one, wall_s)`, and one
    offered less than `REHEARSAL_MIN_S` is `NOT_REHEARSED` too.

    NOTHING IS DROPPED. Every candidate comes back with a `Rehearsal`, because a tournament has
    to be able to say what it did not get to as well as what lost.
    """
    now = _time_source(clock)
    started = now()
    cands = tuple(candidates)
    allowed = k() if limit is None else max(0, int(limit))
    out: list[Rehearsal] = []
    for index, cand in enumerate(cands):
        if index >= allowed:
            out.append(_ended(cand, NOT_REHEARSED, seconds=0.0,
                              reason=f"not_rehearsed: K={allowed} reached"))
            continue
        owed = min(allowed, len(cands)) - index
        share = min(_left(clock, started, wall_s, now) / owed, float(wall_s))
        if share < REHEARSAL_MIN_S:
            out.append(_ended(cand, NOT_REHEARSED, seconds=0.0,
                              reason=f"no_time: {share:.6g}s of this region's wall would be "
                                     f"this candidate's share"))
            continue
        out.append(rehearse(cand, budget_s=share, clock=clock, **kw))
    return tuple(out)
