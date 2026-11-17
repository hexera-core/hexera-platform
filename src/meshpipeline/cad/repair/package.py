# Responsibility: Define the target package - a region, a plan and the claim its author makes
#                 about the outcome - and judge that claim against what was measured.
# Owns: the `TargetPackage`, `Predicate` and `PackageBudget` values, `specificity`, and `evaluate`.
# Boundaries: pure comparison of a claim fixed BEFORE execution against measurements taken after.
#             It imports no kernel, runs nothing, and judges no quality: the author cannot grade its
#             own work, it can only state something a separate measurement checks. It does not
#             enforce `PackageBudget.deviation_mm`; that would make a package a cap waiver, which a
#             planner directive explicitly is not (cad/repair/directives.py).
# Collaborates with: cad/repair/regions.py (the `Region` a package targets), cad/repair/caps.py
#                    (the `Measure` it compares).
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from meshpipeline.cad.repair.caps import Measure, volume_incomparable
from meshpipeline.cad.repair.regions import Region


@dataclass(frozen=True, slots=True)
class PackageBudget:
    max_ops: int
    timeout_s: float
    model_calls: int
    #: RECORDED, NOT ENFORCED. Whether a package may move a cap is an open product question.
    deviation_mm: float


# `reclassified` is the discharge-by-judgement path: a defect the author asserts is intentional
# (a chamfer reported as a sliver) may remain and the predicate still holds. A clean part means
# "nothing unexplained remains", not "the inspector finds nothing". It is the main abuse surface of
# the design, so callers must surface how many defects a package reclassified.
#
# `unchanged` is the collateral-damage guard: an entity the author said it would not touch whose
# defect disappeared FAILS the predicate. An unpredicted change is what this design exists to catch.


@dataclass(frozen=True, slots=True)
class Predicate:
    cleared: frozenset[str]
    reclassified: frozenset[str]
    unchanged: frozenset[str]
    max_area_delta_mm2: float
    max_volume_delta_mm3: float


@dataclass(frozen=True, slots=True)
class TargetPackage:
    region: Region
    intent: str
    plan: tuple[dict, ...]
    predicate: Predicate
    budget: PackageBudget
    rationale: str = ""


def specificity(predicate: Predicate) -> float:
    """0.0 when the claim names no entity, rising towards 1.0 with each entity it commits to.

    A predicate asserting only what is always true (the shape is still valid) would let an author
    pass by promising nothing, so the caller refuses a zero.
    """
    n = len(predicate.cleared | predicate.reclassified | predicate.unchanged)
    return n / (n + 1.0)


def _names(items: Iterable[str]) -> str:
    return ", ".join(sorted(items))


def evaluate(predicate: Predicate, *, before: Measure, after: Measure,
             defects_before: Iterable[str], defects_after: Iterable[str],
             kept_ops: int | None = None,
             declared_area_mm2: float = 0.0, declared_volume_mm3: float = 0.0) -> str:
    """Return "" when the claim held, else the first way it failed, in words.

    Checked in a fixed order - nothing kept, cleared, unchanged, reclassified, area, volume - and
    entities are listed sorted, so identical inputs always give the identical message.

    `kept_ops` IS HOW MANY OF THE PACKAGE'S OPERATIONS SURVIVED, and it is a SECOND, independent
    reason a `cleared` claim can fail. A claim is checked by looking for its name in the scan that
    judges it, so a name that scan cannot produce - because something renamed the entity behind
    its back - would read as a repair. That hole is closed at the source (`operations.copied_wire`:
    a probe may not nudge the geometry it probes), but the safety property may not rest on one
    fix: a package whose every operation declined or rolled itself back has repaired nothing BY
    CONSTRUCTION, whatever the names say, so it cannot have cleared anything. `None` means the
    caller did not state what it kept, and then only the names are judged - every real caller
    states it (`session.try_package`).

    A pure RECLASSIFICATION still commits with nothing kept, deliberately: relabelling a defect as
    intentional is a discharge by judgement (spec decision 2) and no geometry is supposed to move
    for it. The guard is about `cleared` alone.

    `declared_area_mm2` / `declared_volume_mm3` ARE WHAT THE KEPT OPERATIONS SAID THEY WOULD ADD,
    summed by `caps.Accounting`, and movement is judged NET of them - the rule `caps._accounted`
    already applies to the run. An operation that patches a hole adds surface area it declares, so
    against the RAW delta no hole-patching package could ever commit under any author, including a
    model one: the author would have to predict a patch's area to the millimetre before the kernel
    has built it. That is a missing channel, not an author's limitation.
    WHY TRUSTING A DECLARATION IS NOT A LOOPHOLE, since the next reader will reasonably ask: a
    declaration is the operation's own statement about what it added, and `caps.cumulative_refusal`
    independently refuses the whole run when the MEASURED movement does not match what was
    declared ("an operation that declared 50 mm2 and moved 200 is refused - not for moving surface,
    but for not understanding what it did"). An author cannot inflate this by declaring more,
    because it does not write the declarations: its operations do, and the caps police them. What
    is left for the predicate to judge is what the plan moved BEYOND its own account, which is the
    only part the author could have predicted.
    """
    was, now = set(defects_before), set(defects_after)

    if kept_ops is not None and kept_ops <= 0 and predicate.cleared:
        # FIRST, because it does not depend on the names at all. Nothing was kept, so the shape
        # the scan judged is the shape the claim was made about, and a defect cannot have been
        # cleared by an operation that was undone.
        return ("no operation was kept, so nothing was repaired: the claim to have cleared "
                f"{_names(predicate.cleared)} cannot hold over geometry the package did not "
                "change")

    remaining = predicate.cleared & now
    if remaining:
        return f"cleared defects still present: {_names(remaining)}"

    unreported = predicate.cleared - was
    if unreported:
        return f"cleared defects were never reported: {_names(unreported)}"

    # `unchanged` fails in BOTH directions. A defect that went away is collateral damage; one that
    # appeared is new damage exactly where the author promised not to touch. An entity that was
    # healthy before and stays healthy is a legitimate, checkable claim, so no phantom check here.
    vanished = (predicate.unchanged & was) - now
    if vanished:
        return f"defects claimed unchanged disappeared: {_names(vanished)}"
    appeared = (predicate.unchanged & now) - was
    if appeared:
        return f"defects claimed unchanged appeared: {_names(appeared)}"

    phantom = predicate.reclassified - was
    if phantom:
        return f"reclassified defects were never reported: {_names(phantom)}"

    # NET OF THE DECLARATIONS, as `caps._accounted` judges the run: what the predicate bounds is
    # the movement no operation accounted for. Silence means zero, so a plan that declared nothing
    # is judged exactly as it was before.
    area = abs(after.surface_area_mm2 - before.surface_area_mm2 - float(declared_area_mm2))
    if area > predicate.max_area_delta_mm2:
        return (f"surface area moved {area:.6g} mm2 beyond the {float(declared_area_mm2):.6g} mm2 "
                f"its operations declared, past the claimed "
                f"{predicate.max_area_delta_mm2:.6g} mm2")

    if volume_incomparable(before, after):
        # THE SAME THREE CASES `caps.cumulative_refusal` REFUSES TO COMPARE, and read from the one
        # function that defines them so the two cannot drift. A shape with no solid on either side
        # has no inside: OpenCASCADE's integral over an open shell is a number, not a volume. And a
        # body that CLOSED is volume no operation could have declared - whether a shell closed is
        # the kernel's answer, not a prediction - which is precisely the outcome a hole-patching
        # package exists to produce. Comparing against it refused the repair for succeeding.
        # NOTHING IS WAIVED BY THIS: the run's own `cumulative_refusal` still bounds a closed
        # body's volume absolutely (positive, and under the cube of its diagonal), and
        # `sanity_refusal` still refuses an operation that LOSES a solid.
        return ""
    volume = abs(after.volume_mm3 - before.volume_mm3 - float(declared_volume_mm3))
    if volume > predicate.max_volume_delta_mm3:
        return (f"volume moved {volume:.6g} mm3 beyond the {float(declared_volume_mm3):.6g} mm3 "
                f"its operations declared, past the claimed "
                f"{predicate.max_volume_delta_mm3:.6g} mm3")
    return ""
