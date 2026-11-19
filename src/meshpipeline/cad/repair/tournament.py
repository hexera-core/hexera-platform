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
