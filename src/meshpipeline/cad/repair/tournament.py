# Responsibility: tournament repair - gather up to K candidate packages for one region, rehearse
#                 each on its own clone, and NOMINATE the one that survived.
# Owns: three sections, in this order. (1) THE PURE HALF: what a candidate is, what a rehearsal of
#       one reported, plan-key deduplication, the lexicographic score, the ranking, the
#       eligibility gate, `winner`, and the postures (off/shadow/live and K). (2) THE REHEARSAL:
#       one candidate played on one clone, through `Scratch`, `ladder` and `deviation`.
#       (3) THE AUTHOR: `tournament_author`, which gathers from the rules author, the one-shot
#       model author and the investigator, rehearses inside the region's wall, offers the winner
#       to the region loop, and records every round.
# Boundaries: NOTHING HERE COMMITS, REVIEWS, OR STANDS IN FOR A REVIEW. A rehearsal runs on a
#             scratch clone that can never commit and yields no review signal, so a win is a
#             NOMINATION: the region loop hands the package to `session.try_package` on the real
#             session, where the predicate and the review tier judge it. It starts no process and
#             imports no mesher; the kernel collaborators are imported LAZILY so the pure half's
#             import stays cheap. It never raises - a source or a candidate that came apart is a
#             recorded result - and it never mutates its inputs.
# Collaborates with: cad/repair/package.py (`TargetPackage`), cad/repair/ladder.py (`TierResult`,
#                    the only way a candidate may be probed), cad/repair/investigator_tools.py
#                    (`Scratch`, the candidate isolation a rehearsal uses),
#                    cad/repair/investigator_budget.py (`InvestigationClock` and the per-part
#                    budget - the policy, which this module restates nowhere),
#                    cad/repair/rules_author.py, cad/repair/model_author.py and
#                    cad/repair/investigator.py (the three candidate sources),
#                    cad/repair/region_loop.py (which calls the author and offers the winner),
#                    cad/repair/conservative.py (which selects it and reports the rounds).
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
    return posture() == _ON


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
             scope=None, accounting=None) -> Rehearsal:
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

        # THE REAL SESSION'S DECLARATIONS ride along when the loop reported them: the clone's caps
        # compare against the upload, so without them an earlier package's declared change reads
        # as unexplained and a candidate the real session would keep fails its rehearsal.
        extra = {} if accounting is None else {"accounting": accounting}
        scratch = make_scratch(session_shape, original=original, defects=tuple(defects),
                               reserved=frozenset(reserved),
                               remaining_s=lambda: _left(clock, started, budget_s, now),
                               clock=now, **extra)
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


# -- the author --------------------------------------------------------------------------------
#
# THE WIRING, and the part of this module that spends a run's money. It gathers up to K candidates
# from the three sources, rehearses them on clones inside the region's wall, and OFFERS THE WINNER
# TO THE REGION LOOP - which hands it to `session.try_package` on the real session.
#
# TWO RULES ABOVE EVERYTHING ELSE HERE:
#   1. `winner()` returning None OFFERS NOTHING. Not `rank(...)[0]`, not the best loser. A
#      tournament where no candidate survived its clone has learned that this region's candidates
#      do not work, and the honest ending is the answer - dressing a loser up as a winner would
#      spend a review on it and could commit damage no rehearsal endorsed.
#   2. A WIN IS A NOMINATION, NEVER AN ACCEPTANCE. A rehearsal happens on a scratch clone which
#      can never commit and which yields NO review signal, so the winner's package must still pass
#      `package_review` / the review tier on the real session. Nothing here commits, reviews, or
#      stands in for a review.

#: What this author reports itself as, beside `regions-rules-v1`, `regions-model-v1` and
#: `regions-investigator-v1`.
LABEL = "regions-tournament-v1"

#: How a round ended. Every one of these is a FIRST-CLASS ANSWER and travels in the report: a
#: region nobody could propose for, one whose candidates all lost, and one that was never given
#: the time to try are three different facts about a run.
NOT_LOCATED = "not_located"
NO_CANDIDATE = "no_candidate"
NO_ELIGIBLE = "no_eligible_candidate"
NO_TIME = "no_time"
OFFERED = "offered"
#: Shadow's ending: the tournament ran on clones and would have offered this, and the rules drove.
WOULD_OFFER = "would_offer"

_SHADOW = "shadow"
_OFF = "off"

#: "THIS SOURCE WAS NOT INJECTED", which is NOT the same as an injected `None` ("this source is
#: absent from the tournament"). Without the distinction a caller that silenced a source would get
#: the real one built behind it - and for the one-shot author that means a model call nobody asked
#: for.
_DEFAULT: object = object()

#: `k()` under a name this author can read: its own `k=` parameter shadows the function.
_configured_k = k


def posture() -> str:
    """The tournament posture: `live`, `shadow`, or `off`. FAILS CLOSED - only those two exact
    spellings are on, so "on", "true", "Live" and an unset row all read off."""
    raw = str(getattr(rcfg, "CAD_REPAIR_TOURNAMENT", _OFF) or _OFF)
    return raw if raw in (_ON, _SHADOW) else _OFF


def _clamped_k(value: object) -> int:
    """An explicitly asked-for K, clamped into `[K_MIN, K_MAX]` by the same rule as the configured
    one: a caller cannot buy a bigger tournament than the ceiling by passing a number."""
    try:
        wanted = int(str(value).strip())
    except (TypeError, ValueError):
        return K_DEFAULT
    return max(K_MIN, min(K_MAX, wanted))


def tournament_summary(tournaments, packages=()) -> dict:
    """The run's tournament row: `{regions, candidates, rehearsed, winners_by_source, offered,
    committed, seconds}`.

    `offered` AND `committed` ARE BOTH REPORTED, and the gap between them is the point: a win is a
    nomination, so a row where they differ is the review tier having done its job, not a defect in
    the tournament. `committed` is read from the SESSION's own package verdicts - a region the
    tournament offered in and whose package the real session committed - because nothing on a
    clone can answer it.
    """
    from meshpipeline.cad.repair.session import COMMITTED

    rounds = [dict(t) for t in tournaments]
    cands = [dict(c) for t in rounds for c in t.get("candidates", ())]
    wins: dict[str, int] = {}
    for each in rounds:
        won = each.get("offered") or each.get("would_offer")
        for cand in each.get("candidates", ()):
            if won and cand.get("plan_key") == won:
                wins[str(cand.get("source"))] = wins.get(str(cand.get("source")), 0) + 1
    # A COMMITTED REGION ENDS ITS OWN ATTEMPTS (region_loop), so a region has at most one
    # committed verdict and it is the one the tournament's last offer earned. Matching on the
    # region rather than on a position is therefore exact AND order-free.
    offered_in = {each["region"] for each in rounds if each.get("offered")}
    return {
        "regions": len({each["region"] for each in rounds}),
        "candidates": len(cands),
        "rehearsed": sum(1 for c in cands if c.get("status") == REHEARSED),
        "winners_by_source": dict(sorted(wins.items())),
        "offered": sum(1 for each in rounds if each.get("offered")),
        "committed": sum(1 for p in packages
                         if int(getattr(p, "region_index", -1)) in offered_in
                         and str(getattr(p, "status", "")) == COMMITTED),
        "seconds": round(sum(float(each.get("seconds", 0.0)) for each in rounds), 6),
    }


def _clock_of(deadline_s, *, reserve_s: float | None, proof_reserve_s: float | None,
              share: float | None, clock):
    """THE RUN'S ONE CLOCK, built ONCE per author and shared by every region and every rehearsal.

    Two clocks over one deadline is how a region starves its successors: each would think it had
    the whole remainder to itself. `InvestigationClock` is Task 8's and states the policy; this
    author states none of its own. An UNSTATED reserve or share is the configured one
    (`investigator.configured_reserves`) - the final verification's reserve and the region share -
    never 0 and 1. Imported lazily so the pure half's import stays cheap.
    """
    from meshpipeline.cad.repair.investigator import configured_reserves
    from meshpipeline.cad.repair.investigator_budget import InvestigationClock

    held_s, proof_s, part = configured_reserves(deadline_s, clock, reserve_s=reserve_s,
                                                proof_reserve_s=proof_reserve_s, share=share)
    return InvestigationClock(deadline_s, reserve_s=held_s, proof_reserve_s=proof_s,
                              share=part, clock=clock)


def _budget_of(defects, original):
    """The region's spend, from the PART'S predicted mesh cost tier (`investigator_budget`). Fails
    closed to the cheap tier, so a part nobody measured never buys a heavy budget."""
    from meshpipeline.cad.repair import investigator_budget as ib

    return ib.budget_for_part(tuple(defects), solids=getattr(original, "solids", None),
                              faces=getattr(original, "faces", None))


def _fits_shadow(timing, wall: float) -> bool:
    """Whether a SHADOW round may run at all: the whole wall plus the author's grace must fit
    above the run's reserve. A policy with no opinion lets it run."""
    ask = getattr(timing, "fits_shadow", None)
    if ask is None:
        return True
    try:
        return bool(ask(float(wall)))
    except Exception:  # noqa: BLE001 - a policy that cannot answer does not stop the rules run
        logger.debug("tournament: the clock could not say whether a shadow fits", exc_info=True)
        return False


def _record(cand: Candidate, r: Rehearsal) -> dict:
    return {"source": cand.source, "plan_key": cand.plan_key, "status": r.status,
            "score": list(score(r)), "eligible": bool(eligible(r)), "reason": r.reason,
            "seconds": round(float(r.seconds), 6)}


def tournament_author(shape, *, original, reserved: frozenset = frozenset(), job_id: str = "",
                      tolerance_mm: float = 0.0, deadline_s: float | None = None,
                      clock=None, k: int | None = None, shadow: bool = False, sink=None,
                      face_reader=None, reserve_s: float | None = None,
                      proof_reserve_s: float | None = None, share: float | None = None,
                      policy=None, rules=_DEFAULT, one_shot=_DEFAULT,
                      investigate=_DEFAULT, rehearse_with=None):
    """Build the tournament `author` the region loop calls: `(region, defects, attempts) -> package`.

    PER AUTHOR CALL:
      1. **Gathers candidates** in `SOURCE_ORDER`. The rules author is ALWAYS asked - it is free
         and deterministic - and then, while the deduplicated count is below K, the one-shot model
         author and the investigator. A source past K is NOT ASKED, so K bounds the spend rather
         than merely discarding an answer that was paid for. Every source's failure is RECORDED
         AND NEVER RAISED. The model and investigator calls spend their own run-wide caps
         (`CAD_REPAIR_LLM_AUTHOR_CALLS_MAX`, `CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX`) unchanged,
         because the sub-authors are the real ones and this author adds no budget of its own.
      2. **Rehearses** them with `rehearse_all` inside the region's wall, against the session's
         shape AT TOURNAMENT TIME (`observe_shape`) - the plan's `EntityRef`s resolve against that
         shape, and a stale one would resolve them against faces a commit has since renumbered.
      3. **Offers the WINNER**, which is `winner()`: the best ELIGIBLE rehearsal. `winner()`
         returning None offers NOTHING and ends the round `no_eligible_candidate`.
      4. **On a re-call** for the same region and the SAME defects (the offered package was
         reverted), replays the CACHED candidates it has not yet offered, RE-REHEARSED against
         the current shape, and SPENDS NO NEW SOURCE CALL - which is what makes a second attempt
         affordable. Once the cache is exhausted, or the region's defects changed, the sources
         are asked AGAIN with the current defects and the attempt history; a plan already offered
         for the region is never offered twice.

    IN SHADOW the rules package is returned UNCHANGED, whatever the tournament found; the round
    still runs on clones and records `would_offer`. It is skipped unless the whole wall fits
    (`fits_shadow`), because a shadow may never cost the run it watches anything.

    WHAT IT EXPOSES, read off the callable like every other author's attributes:
      - `tournaments`: one round per author call - `region`, `fingerprints`, `attempt`,
        `wall_s`, `sources` (what each was asked and what it said), `candidates`
        (`{source, plan_key, status, score, eligible, reason, seconds}`), `offered`, `end`,
        `seconds`.
      - `tournament_report(packages)`: the run's `tournament` row (`tournament_summary`).
      - `report()`: the one-shot author's own record, plus this author's label and posture.
      - `investigations`: the investigator's record, when one authored.
      - `refusals` / `observe_part` / `observe_shape` / `label`: the author seam, unchanged.
      - `clock`: the ONE `InvestigationClock` every region and every rehearsal shares.

    `rules`, `one_shot`, `investigate`, `rehearse_with` and `policy` are TEST SEAMS with the real
    collaborators as defaults, for the same reason the session's and the rehearsal's are.
    """
    import time as _time

    from meshpipeline.cad.repair.rules_author import LABEL as RULES_LABEL

    tick = clock if callable(clock) else _time.monotonic
    held_codes = frozenset(reserved)
    tournaments: list[dict] = []
    state: dict = {"shape": shape, "accounting": None}
    #: region index -> the ranked candidates and the plan keys already offered for it.
    cache: dict[int, dict] = {}
    timing = policy if policy is not None else _clock_of(
        deadline_s, reserve_s=reserve_s, proof_reserve_s=proof_reserve_s, share=share, clock=tick)
    play = rehearse_with if rehearse_with is not None else rehearse_all

    sources = _sources(shape, original=original, reserved=held_codes, job_id=job_id,
                       tolerance_mm=tolerance_mm, deadline_s=deadline_s, clock=tick,
                       face_reader=face_reader, shadow=shadow, sink=sink, rules=rules,
                       one_shot=one_shot, investigate=investigate)

    def allowed() -> int:
        return _configured_k() if k is None else _clamped_k(k)

    def observe_part(part_defects: tuple) -> None:
        for _, each in sources:
            seen = getattr(each, "observe_part", None)
            if callable(seen):
                seen(tuple(part_defects))

    def observe_shape(current) -> None:
        state["shape"] = current
        for _, each in sources:
            seen = getattr(each, "observe_shape", None)
            if callable(seen):
                seen(current)

    def observe_accounting(current) -> None:
        # The real session's declarations, for this author's rehearsal clones and for the
        # investigator source's scratch clone alike.
        state["accounting"] = current
        for _, each in sources:
            seen = getattr(each, "observe_accounting", None)
            if callable(seen):
                seen(current)

    def gather(entry: dict, region, defects: tuple, attempts: tuple,
               cap: int) -> tuple[Candidate, ...]:
        cands: list[Candidate] = []
        for source, ask in sources:
            if not callable(ask):
                # ABSENT: the posture that would have built it is off, or the caller silenced it.
                entry["sources"].append({"source": source, "outcome": "absent", "reason": ""})
                continue
            if source != RULES and len(dedupe(cands)) >= cap:
                entry["sources"].append({"source": source, "outcome": "not_asked",
                                         "reason": f"K={cap} already has its candidates"})
                continue
            try:
                package = ask(region, tuple(defects), tuple(attempts))
            except Exception as exc:  # noqa: BLE001 - a source that came apart is a result
                logger.info("tournament: the %s source came apart (%s: %s)", source,
                            type(exc).__name__, exc)
                entry["sources"].append({"source": source, "outcome": "failed",
                                         "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if package is None:
                entry["sources"].append({"source": source, "outcome": "no_package",
                                         "reason": ""})
                continue
            entry["sources"].append({"source": source, "outcome": "candidate", "reason": ""})
            cands.append(candidate(source, package))
        return dedupe(cands)[:cap]

    def author(region, defects: tuple, attempts: tuple = ()):
        cap = allowed()
        entry: dict = {"region": int(region.index),
                       "fingerprints": sorted(region.fingerprints),
                       "attempt": len(attempts) + 1, "wall_s": 0.0, "sources": [],
                       "candidates": [], "offered": None, "end": "", "seconds": 0.0}
        tournaments.append(entry)
        if not getattr(region, "located", False):
            # NOT A LOCUS, so no claim about it could be verified (rules_author says why). No
            # source is asked and no clone is opened.
            entry["end"] = NOT_LOCATED
            return None

        asked_about = frozenset(getattr(d, "fingerprint", "") for d in defects)
        held = cache.get(int(region.index))
        cands: tuple[Candidate, ...] = ()
        if held is not None and held["defects"] == asked_about:
            # A RE-CALL ON THE SAME QUESTION SPENDS NO SOURCE CALL. The ranking is this region's
            # own cache; only the candidates it has not yet offered are left, re-rehearsed below.
            cands = tuple(c for c in held["candidates"] if c.plan_key not in held["offered"])
            entry["sources"].append({"source": "cache", "outcome": "reused",
                                     "reason": f"{len(cands)} candidate(s) not yet offered"})
        if not cands:
            # FIRST CALL, CHANGED DEFECTS, OR AN EXHAUSTED CACHE: ask the sources again with the
            # region's CURRENT defects and its attempt history. The first candidate set is not the
            # region's whole repair allowance - the rules author's fallback (`sew_faces` ->
            # `fill_planar_hole`) exists only once it has seen the failed attempt, and a commit
            # that leaves a second defect asks a new question. Bounded by the loop's per-region
            # attempts and by each source's own run-wide caps; a plan already offered for this
            # region is never offered twice.
            offered = list(held["offered"]) if held is not None else []
            fresh = gather(entry, region, tuple(defects), tuple(attempts), cap)
            cands = tuple(c for c in fresh if c.plan_key not in offered)
            held = {"candidates": fresh, "offered": offered, "defects": asked_about}
            cache[int(region.index)] = held
        # Either the cache answered (held was not None) or the branch above just set it.
        assert held is not None

        driven =next((c.package for c in held["candidates"] if c.source == RULES), None)
        fallback = driven if shadow else None
        if not cands:
            entry["end"] = NO_CANDIDATE
            return fallback

        wall = float(timing.region_wall(_budget_of(defects, original)))
        entry["wall_s"] = round(wall, 6)
        if wall < REHEARSAL_MIN_S or (shadow and not _fits_shadow(timing, wall)):
            # OUT OF TIME IS NOT A VERDICT ON THE REGION. Nothing is rehearsed and nothing is
            # offered; in shadow the rules run goes on exactly as it would have. The floor is
            # `REHEARSAL_MIN_S` and not zero because a wall below it cannot produce a single
            # rehearsal (`rehearse`), so opening the round would buy K `not_rehearsed` records
            # and a clone's worth of setup for no answer.
            entry["end"] = NO_TIME
            return fallback

        rehearsals = play(cands, wall_s=wall, clock=timing, limit=cap,
                          session_shape=state["shape"], original=original,
                          defects=tuple(defects), region=region,
                          tolerance_mm=float(tolerance_mm), reserved=held_codes,
                          accounting=state["accounting"])
        ordered = rank(rehearsals)
        entry["candidates"] = [_record(r.candidate, r) for r in ordered]
        entry["seconds"] = round(sum(float(r.seconds) for r in ordered), 6)

        won = winner(ordered)
        if won is None:
            # THE HONEST ENDING. `rank()` has a first element and it is NOT offered.
            entry["end"] = NO_ELIGIBLE
            return fallback
        if shadow:
            entry["end"] = WOULD_OFFER
            entry["would_offer"] = won.candidate.plan_key
            return driven
        held["offered"].append(won.candidate.plan_key)
        entry["end"] = OFFERED
        entry["offered"] = won.candidate.plan_key
        # A NOMINATION. The region loop hands this to `session.try_package` on the REAL session,
        # where the predicate and the review tier judge it - the clone it was rehearsed on could
        # never commit and gave no review signal at all.
        return won.candidate.package

    def report() -> dict:
        base: dict = {}
        inner = getattr(dict(sources).get(ONE_SHOT), "report", None)
        if callable(inner):
            base = dict(inner())
        return {**base, "label": LABEL, "posture": _SHADOW if shadow else _ON}

    author.tournaments = tournaments  # type: ignore[attr-defined]
    author.tournament_report = (  # type: ignore[attr-defined]
        lambda packages=(): tournament_summary(tournaments, packages))
    author.report = report  # type: ignore[attr-defined]
    author.observe_part = observe_part  # type: ignore[attr-defined]
    author.observe_shape = observe_shape  # type: ignore[attr-defined]
    author.observe_accounting = observe_accounting  # type: ignore[attr-defined]
    author.refusals = getattr(dict(sources).get(RULES), "refusals", [])  # type: ignore[attr-defined]
    author.verdicts = getattr(dict(sources).get(ONE_SHOT), "verdicts", [])  # type: ignore[attr-defined]
    author.calls = getattr(dict(sources).get(ONE_SHOT), "calls", [])  # type: ignore[attr-defined]
    probed = getattr(dict(sources).get(INVESTIGATOR), "investigations", None)
    if probed is not None:
        # THE INVESTIGATOR'S OWN RECORD still travels, so a tournament that used it does not hide
        # the investigations behind itself (conservative reads this by `getattr`).
        author.investigations = probed  # type: ignore[attr-defined]
    author.label = LABEL if not shadow else RULES_LABEL  # type: ignore[attr-defined]
    author.clock = timing  # type: ignore[attr-defined]
    return author


def _sources(shape, *, original, reserved: frozenset, job_id: str, tolerance_mm: float,
             deadline_s, clock, face_reader, shadow: bool, sink, rules, one_shot,
             investigate) -> tuple[tuple[str, object | None], ...]:
    """The three candidate sources IN PRECEDENCE ORDER, each either an author or None.

    Built once per run, so each sub-author keeps its own run-wide caps and its own record exactly
    as it would have had it driven the loop alone - this author adds no budget and waives none.
    The investigator is built only when ITS OWN posture is live and a router is configured, which
    is the same condition `select_author` applies: a tournament does not turn the investigator on.
    Imported lazily: `model_author` imports this module's pure half, so the author half must not
    import it back at module scope.

    An INJECTED `None` is "this source is absent" and builds nothing; `_DEFAULT` is "nobody said",
    and only then is the real source built.
    """
    from meshpipeline.cad.repair import model_author as ma
    from meshpipeline.cad.repair.rules_author import rules_author

    rules_src = (rules_author(shape, original=original, reserved=reserved)
                 if rules is _DEFAULT else rules)
    model_src: object | None = one_shot
    if one_shot is _DEFAULT:
        model_src = ma.model_author(shape, original=original, reserved=reserved, job_id=job_id,
                                    face_reader=face_reader, deadline_s=deadline_s, clock=clock,
                                    shadow=shadow)
    probe_src: object | None = investigate
    if investigate is _DEFAULT:
        from meshpipeline.cad.repair import investigator as inv

        probe_src = None
        if inv.posture() == _ON and ma._configured():
            probe_src = inv.investigator(shape, original=original, reserved=reserved,
                                         job_id=job_id, tolerance_mm=tolerance_mm,
                                         deadline_s=deadline_s, clock=clock,
                                         face_reader=face_reader, shadow=shadow, sink=sink)
    return ((RULES, rules_src), (ONE_SHOT, model_src), (INVESTIGATOR, probe_src))
