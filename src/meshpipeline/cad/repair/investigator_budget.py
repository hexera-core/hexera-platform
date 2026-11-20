# Responsibility: Say what one investigation of one region may SPEND - tool calls, model calls and
#                 wall seconds - from the part's predicted mesh cost tier, and hold the clock that
#                 keeps one region from eating the run.
# Owns: the three compute-tier budgets, the fail-closed tier lookup, the proof reserve rule and
#       `InvestigationClock`.
# Boundaries: pure arithmetic and settings reads. It runs no mesher, reads no shape, makes no model
#             call, and decides nothing by itself: a driver asks it for numbers. Budgets are
#             PER-PART COMPUTE TIERS (spec pillar 2), not fixed attempt counts, so a cheap part
#             cannot buy a heavy part's spend by failing more often.
# Collaborates with: cad/repair/grind.py (the tier, via `tier_of`), cad/repair/oracle.py (the
#                    scoped probe share the proof reserve holds back), cad/repair/model_author.py
#                    (`_GRACE_S`, the grace its thread bridge abandons a worker at),
#                    settings/cad_repair.py (the postures and the tunables).
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from meshpipeline.cad.repair import grind, model_author, oracle
from meshpipeline.cad.repair.grind import CHEAP, HEAVY, STANDARD
from meshpipeline.settings import cad_repair as rcfg


@dataclass(frozen=True, slots=True)
class InvestigationBudget:
    """What one region's investigation may spend.

    `tool_calls` counts TOOL CALLS and not rounds: a native round can carry many of them, which is
    why `converse_tools` hands the caller the whole reply (`len(reply.tool_calls)`).
    `model_calls` counts answered calls; a `ToolsUnsupported` capability answer falls back to the
    JSON transport and is not charged.
    """

    tier: str
    tool_calls: int
    model_calls: int
    wall_s: float


#: The owner-tunable table. Only the gym (Tasks 13 and 25) moves these numbers; nothing reads them
#: from the environment, because a budget is a policy decision measured offline, not a deployment
#: knob. The tiers are grind's: a part gmsh refused in the surface stage is cheap to reason about,
#: one it refused only after building the whole volume is standard, and one it never answered for -
#: or one with over 1,000 faces - is heavy.
BUDGETS: dict[str, InvestigationBudget] = {
    CHEAP: InvestigationBudget(CHEAP, 6, 8, 60.0),
    STANDARD: InvestigationBudget(STANDARD, 12, 16, 150.0),
    HEAVY: InvestigationBudget(HEAVY, 20, 26, 240.0),
}


#: Added to a tier's turns under `CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS`. MODEST ON PURPOSE: the
#: measured lever is which region is reached first, not how long it may sit there (lane F on wave
#: 1: more region budget alone gives about no gain), and the walls are untouched so a region still
#: cannot spend its successors' clock or the proof reserve.
CLONE_FREEDOM_TURNS = 4


def budget_for(tier: object) -> InvestigationBudget:
    """The budget for a tier. FAILS CLOSED: `grind.UNKNOWN`, an unrecognised name, a non-string
    and `None` all get the SMALLEST budget, never a guessed larger one.

    `grind.tier_of` returns `UNKNOWN` rather than a guess when the face count is missing, so the
    common cause of an unrecognised tier is a part nobody has measured - exactly the part that
    should not be handed a heavy spend.
    """
    if _clone_freedom():
        return _with_more_turns(_base_budget_for(tier))
    return _base_budget_for(tier)


def _clone_freedom() -> bool:
    return getattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off") == "live"


def _with_more_turns(budget: InvestigationBudget) -> InvestigationBudget:
    """`budget` with more TURNS and the same wall: a policy that asks for many attempts has to pay
    for the calls they cost, and the wall is the clock everyone else shares."""
    return replace(budget, tool_calls=budget.tool_calls + CLONE_FREEDOM_TURNS,
                   model_calls=budget.model_calls + CLONE_FREEDOM_TURNS)


def _base_budget_for(tier: object) -> InvestigationBudget:
    if isinstance(tier, str):
        found = BUDGETS.get(tier)
        if found is not None:
            return found
    return BUDGETS[CHEAP]


def budget_for_part(defects: Iterable[object], *, solids: int | None,
                    faces: int | None) -> InvestigationBudget:
    """The budget for the part these defects describe. Never raises: a predictor that somehow
    does gets the cheap budget, by the same fail-closed rule."""
    try:
        tier = grind.tier_of(defects, solids=solids, faces=faces)
    except Exception:  # noqa: BLE001 - a budget must never break the repair it bounds
        tier = ""
    return budget_for(tier)


def proof_reserve_s(remaining_s: float) -> float:
    """Seconds a CONCLUDED package still needs: its own scoped proof, plus its review.

    The scoped share is the oracle's own rule (`probe_budget_s("scoped", ...)`, the lesser of the
    configured per-probe timeout and half of what is left), so this module states no probe policy
    of its own. The reviewer's timeout is added only while review is live, because with it off the
    package commits on its predicate alone. Holding both back is what makes a forced conclusion
    worth something: a package concluded with no time to prove it is not a repair.
    """
    reserve = 0.0
    if rcfg.CAD_REPAIR_MESH_PROBE == "live":
        reserve += oracle.probe_budget_s(
            "scoped", remaining_s, timeout_s=float(rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S))
    if rcfg.CAD_REPAIR_PACKAGE_REVIEW == "live":
        reserve += float(rcfg.CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S)
    return max(0.0, reserve)


class InvestigationClock:
    """The run's ONE deadline, read on behalf of one region's investigation.

    The investigation never has a clock of its own: `deadline_s` is the run's absolute monotonic
    deadline (`conservative.py`: start + `CAD_REPAIR_TIMEOUT_S` - `DEADLINE_MARGIN_S`), `reserve_s`
    is what the walk keeps for its final whole-shape verification (`region_loop.final_reserve_s`),
    and `share` is the fraction of what remains above that reserve that ONE region may take, so
    the regions after this one still have time to be repaired.

    `deadline_s is None` means a run without a deadline - the corpus gym and most unit tests - and
    then only the budget's own wall bounds the investigation.
    """

    def __init__(self, deadline_s: float | None, *, reserve_s: float, proof_reserve_s: float,
                 share: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.deadline_s = None if deadline_s is None else float(deadline_s)
        self.reserve_s = max(0.0, float(reserve_s))
        self.proof_reserve_s = max(0.0, float(proof_reserve_s))
        self.share = max(0.0, float(share))
        self.clock = clock

    # -- the run ------------------------------------------------------------------------------

    def run_left(self) -> float:
        """Seconds of the RUN left above the final reserve. `None` deadline means unbounded."""
        if self.deadline_s is None:
            return float("inf")
        return max(0.0, self.deadline_s - self.clock() - self.reserve_s)

    # -- one region ---------------------------------------------------------------------------

    def region_wall(self, budget: InvestigationBudget) -> float:
        """Seconds this region's investigation may run for: the lesser of its budget's wall and its
        share of what is left above the reserve. NEVER NEGATIVE - a region that is out of time gets
        0, which a driver reads as "do not start"."""
        # The budget's wall is LOOKING time; the proof reserve is held on top of it. Capping at the
        # bare wall put CHEAP's 60 s under the shipping 80 s reserve, so every cheap region had to
        # conclude before its first tool call, at any run length.
        # A zero wall stays "do not start".
        looking = max(0.0, float(budget.wall_s))
        wall = looking + self.proof_reserve_s if looking > 0.0 else 0.0
        left = self.run_left()
        if left == float("inf"):
            return wall
        # Proof reserve PLUS the share of what lies above it: `share * left` alone sat below the
        # reserve for every run length, forcing a conclusion before the first tool call.
        above = left - self.proof_reserve_s
        return max(0.0, min(wall, max(0.0, left) if above <= 0.0
                            else self.proof_reserve_s + self.share * above))

    def left(self, started: float, wall: float | None = None) -> float:
        """Seconds left to an investigation that began at `started`: the lesser of what is left of
        the run above the reserve and what is left of this region's `wall`. With no wall it is the
        run's figure alone. A per-call model timeout is the lesser of this and
        `CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S`."""
        left = self.run_left()
        if wall is not None:
            left = min(left, float(wall) - (self.clock() - float(started)))
        return max(0.0, left)

    def must_conclude(self, started: float, wall: float) -> bool:
        """True once LESS THAN `proof_reserve_s` is left - of this region's wall or of the run.

        Conclusion is forced while the proof time still remains, not after it is gone: a package
        that cannot be proved and reviewed is not a repair, so the loop stops looking first.
        """
        return self.left(started, wall) < self.proof_reserve_s

    def fits_shadow(self, wall: float) -> bool:
        """True when the WHOLE of `wall` plus the author's grace fits above the reserve.

        A shadow investigation is recorded and never executed, so it may only run when it cannot
        cost the live run anything: `model_author._bounded_in_thread` abandons its worker at
        `timeout_s + _GRACE_S`, and an abandoned thread still holds its share of the process. The
        grace is READ from `model_author` at call time rather than copied, so the two can never
        drift apart.
        """
        return self.run_left() >= max(0.0, float(wall)) + float(model_author._GRACE_S)
