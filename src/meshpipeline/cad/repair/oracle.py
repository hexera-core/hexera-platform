# Responsibility: Name the verification ladder repair asks its questions of - tiers 0 to 3 - and own
#                 the policy that goes with each rung: how much machine it may use, and how many
#                 seconds a caller with so much time left may give it.
# Owns: the tier table, the check names a caller states its question in, the check->tier mapping,
#       and the probe budget and final-reserve rules.
# Boundaries: it is pure policy - a table a caller consults. It starts no process, imports no
#             mesher, no kernel and no settings, and holds no state. The CALLER reads its own
#             timeout out of settings and passes it in, so one rule can be exercised over a table
#             of timeouts without touching the environment.
# Collaborates with: cad/repair/session.py (tier 0-2 budgets), cad/repair/region_loop.py (the final
#                    reserve). Tier 3 is the product mesher and is NOT reachable from the loop: no
#                    check maps to it, so it never gets an in-loop budget.
from __future__ import annotations

from dataclasses import dataclass

#: Seconds any tier-2 probe THIS PLAN STARTS may take. gmsh enforces no ceiling mid-run, so the
#: cap is a budget the caller hands the child plus coarse sizing that keeps the work small.
TIER2_HARD_CAP_S = 60.0

#: Elements a tier-2 verdict may rest on. Read AFTER the run: over the cap is a quality signal
#: (`over_element_cap`), never a failure.
TIER2_ELEMENT_CAP = 1_000_000

#: fTetWild optimisation iterations the tier-2b routing pass runs. Coarse on purpose: the question
#: is "would the tolerant rung eat this?", not "is this the best tolerant mesh?".
TIER2B_MAX_ITS = 10

#: THE SHARE OF THE RUN'S REMAINING TIME ONE MESH PROBE MAY TAKE (final review I2), the
#: `localize.face_scan_budget_s` pattern. A half, so a probe that times out - a real family on the
#: corpus - always leaves the run as much time again for its after-checks, its final whole-shape
#: probe and the STEP write; a whole-remainder budget let one probe spend the run.
MESH_PROBE_FRACTION = 0.5

#: Seconds the region loop keeps IN HAND beyond one probe's timeout for its final whole-shape
#: verification (`final_reserve_s`): the final scan's kernel walk and measure.
FINAL_PROBE_SLACK_S = 10.0


@dataclass(frozen=True, slots=True)
class Tier:
    """One rung of the ladder, with the resources and ceilings its policy allows.

    `in_loop` is the invariant that matters: a tier the repair loop may ask. `processes` is how
    many child processes the rung starts (tier 0 runs in the caller; tier 3 is not ours to start),
    and `threads` is what a child may use - worker CPU only, so one.
    """

    name: str
    label: str
    in_loop: bool
    processes: int
    threads: int
    hard_cap_s: float | None
    element_cap: int | None


TIER_0 = Tier("0", "kernel checks", True, 0, 1, None, None)
TIER_1 = Tier("1", "scoped surface probe", True, 1, 1, None, None)
TIER_2 = Tier("2", "whole-part probe", True, 1, 1, TIER2_HARD_CAP_S, TIER2_ELEMENT_CAP)
TIER_2B = Tier("2b", "tolerant-mesh quick pass", True, 1, 1, None, None)
TIER_3 = Tier("3", "product mesher", False, 0, 0, None, None)

#: The ladder, cheapest rung first.
TIERS: tuple[Tier, ...] = (TIER_0, TIER_1, TIER_2, TIER_2B, TIER_3)

# The checks a caller may ask for. A check says WHAT IS BEING CHECKED; the oracle answers which
# rung serves it and for how long. Callers name the check rather than the tier so a policy change
# that moves a question to another rung is one edit here.
CHECK_ATTEMPT = "attempt"    #: one operation's keep-or-roll-back judgement (tier 0)
CHECK_SCOPED = "scoped"      #: a region's surfaces after an edit (tier 1)
CHECK_WHOLE = "whole"        #: the whole part, mid-run (tier 2)
CHECK_FINAL = "final"        #: the run's LAST scan, which the loop reserved time for (tier 2)
CHECK_VERDICT = "verdict"    #: a goal's finishing verdict (tier 2, under the hard cap)
CHECK_ROUTING = "routing"    #: would the tolerant rung eat this? (tier 2b)

_TIER_BY_CHECK: dict[str, Tier] = {
    CHECK_ATTEMPT: TIER_0,
    CHECK_SCOPED: TIER_1,
    CHECK_WHOLE: TIER_2,
    CHECK_FINAL: TIER_2,
    CHECK_VERDICT: TIER_2,
    CHECK_ROUTING: TIER_2B,
}

#: Every check the oracle answers for, in ladder order.
CHECKS: tuple[str, ...] = tuple(_TIER_BY_CHECK)


def tier_for(check: str) -> Tier:
    """Which rung serves `check`?

    An unknown check raises, because a caller bug must not silently pick a tier - and the cheapest
    tier would be the wrong guess as often as the dearest.
    """
    try:
        return _TIER_BY_CHECK[check]
    except KeyError:
        raise ValueError(
            f"unknown check {check!r}; the ladder answers {sorted(_TIER_BY_CHECK)}") from None


def probe_budget_s(check: str, remaining_s: float, *, timeout_s: float) -> float:
    """Seconds `check` may take with `remaining_s` left before the run's deadline.

    `timeout_s` is the CALLER'S configured per-probe timeout (tier 2 reads
    `CAD_REPAIR_MESH_PROBE_TIMEOUT_S`, tier 2b `CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S`); the oracle
    does not read settings. A 0 means "do not probe", and every caller treats it that way: the
    session carries forward what it already knew.

    - `scoped` and `whole`: the lesser of the timeout and `MESH_PROBE_FRACTION` of what is left.
      One rule, two names: they are the same numbers today, and making them diverge is a change
      that must be scored in the gym.
    - `final`: the loop stopped authoring with a reserve in hand for exactly this scan, so it gets
      what remains MINUS `FINAL_PROBE_SLACK_S` - that slack belongs to the kernel walk, measure,
      checks and STEP write that follow, and a timed-out final probe must not spend it.
    - `verdict`: `final`'s rule under tier 2's hard cap, because a finishing verdict is not
      allowed to become the run.
    - `routing`: what remains past the slack, under tier 2b's own timeout.
    - `attempt`, and any check whose tier is not `in_loop` (tier 3): 0. Tier 0 starts no process,
      and the product mesher is not the loop's to start.
    - A NaN or non-positive `remaining_s` reads as nothing left: 0.
    """
    tier = tier_for(check)
    remaining = float(remaining_s)
    if not remaining > 0.0 or not tier.in_loop or tier.processes == 0:
        return 0.0
    if check in (CHECK_SCOPED, CHECK_WHOLE):
        return max(0.0, min(float(timeout_s), MESH_PROBE_FRACTION * remaining))
    past_slack = max(0.0, remaining - FINAL_PROBE_SLACK_S)
    if check == CHECK_FINAL:
        return past_slack
    if check == CHECK_VERDICT:
        return max(0.0, min(TIER2_HARD_CAP_S, past_slack))
    return max(0.0, min(float(timeout_s), past_slack))


def final_reserve_s(*, mesh_probe_live: bool, timeout_s: float) -> float:
    """Seconds a walk keeps in hand for its final whole-shape verification (final review I2).

    With the probe live the final scan probes the whole part, so the walk stops authoring while
    one probe's timeout plus `FINAL_PROBE_SLACK_S` is still left - the final verification always
    has room, and its probe then gets whatever remains. 0 with the probe off, so a flag-off walk
    stops exactly where it always did.
    """
    if not mesh_probe_live:
        return 0.0
    return float(timeout_s) + FINAL_PROBE_SLACK_S
