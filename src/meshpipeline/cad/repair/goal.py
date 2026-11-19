# Responsibility: Say what a repair run is FINISHING FOR, and produce the finishing verdict that
#                 goal asks for.
# Owns: the time a goal reserves out of the driver's deadline (`reserve_s`) and the finishing
#       verdict itself (`finishing_verdict`) - kernel-clean, a tier-2 meshability score and the
#       committed packages' review record.
# Boundaries: it reads a shape and measurements and answers. It repairs nothing, decides nothing
#             about routing, and NEVER raises - an internal error is reported as a verdict with an
#             `error` key, because a verdict that blew up must not lose the repair that produced
#             it. It never reaches the product mesher: the `meshing` goal's answer is the NAME of
#             the tier-3 path, and the existing promotion path still runs it, once, outside repair.
# Collaborates with: cad/repair/oracle.py (the ladder's tiers and every budget this module spends),
#                    cad/repair/grind.py (the expected mesh cost recorded beside the verdict),
#                    cad/repair/mesh_probe.py (the tier-2 probe), cad/repair/contracts.py
#                    (`RepairGoal`), cad/repair/conservative.py (the one caller today).
from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshpipeline.cad.repair import grind, mesh_probe, oracle
from meshpipeline.cad.repair.contracts import DefectSeverity, RepairGoal

if TYPE_CHECKING:
    from meshpipeline.cad.repair.caps import Measure

logger = logging.getLogger(__name__)

# THE ORACLE SEAM, CLOSED. `cad/repair/oracle.py` owns the ladder's tiers and their budgets, so
# this module states no number of its own: the three names below are RE-EXPORTS of the oracle's
# (the `cad/repair/session.py` pattern), kept because callers and tests read them from here, and
# the verdict's budget is the oracle's `verdict` rule rather than a second copy of its arithmetic.
# Two constants that could drift apart were the bug; `is`-identity with the oracle is the fix.

#: Seconds. The hard cap on any tier-2 whole-part probe this plan's code starts.
TIER2_HARD_CAP_S = oracle.TIER2_HARD_CAP_S

#: Elements. A tier-2 verdict over this is `over_element_cap`: a QUALITY SIGNAL, never a failure.
TIER2_ELEMENT_CAP = oracle.TIER2_ELEMENT_CAP

#: Seconds kept in hand beyond a probe's timeout for what FOLLOWS it: the kernel walk, the
#: measure, the after-checks and the STEP write.
FINAL_PROBE_SLACK_S = oracle.FINAL_PROBE_SLACK_S

#: The tier this verdict's meshability reading comes from, reported so a reader never mistakes a
#: coarse probe's answer for the product mesher's.
VERDICT_TIER = "2"

#: What the `meshing` goal answers. The single tier-3 run stays with the existing promotion path,
#: so repair names it rather than performing it.
TIER3_PRODUCT_MESHER = "tier3_product_mesher"

#: `meshability["skipped"]`: empty when the probe ran, this when there was no time left for it.
#: A skipped probe is a no-answer (`score` None), never a refusal (`score` 0.0).
NO_TIME = "no_time"

#: The goals that finish on their own evidence rather than on the product mesher's.
_SELF_FINISHING = (RepairGoal.standalone, RepairGoal.benchmark)

#: A defect at this severity or worse says the kernel is not clean. A warning or an info finding
#: is a remark about a shape an operator may still use.
_NOT_CLEAN = (DefectSeverity.error, DefectSeverity.fatal)

#: Failure kinds that mean the probe was NEVER HEARD FROM rather than that gmsh refused the part.
#: These score `None`: scoring a timeout 0.0 would record "this part does not mesh" on the
#: evidence of our own clock running out.
_NO_ANSWER_KINDS = frozenset({mesh_probe.TIMEOUT, mesh_probe.PROBE_ERROR})


def reserve_s(goal: RepairGoal | None, *, tolerant_live: bool,
              tolerant_timeout_s: float) -> float:
    """Seconds the goal's finishing verdict needs, to be held back from the DRIVER's deadline.

    `None` and `meshing` reserve NOTHING, so an unset goal hands the driver byte-for-byte the
    deadline it has today and a meshing job keeps every second for the repair itself - its verdict
    is the handback, which costs no probe here.

    `standalone` and `benchmark` reserve one tier-2 probe's hard cap plus the slack that follows
    any probe, and the tolerant rung's timeout on top when that posture is live - because a
    reserve that did not cover the rung it is going to run would let the finishing verdict spend
    the margin the after-checks and the STEP write need.
    """
    if goal is None or goal is RepairGoal.meshing:
        return 0.0
    reserve = TIER2_HARD_CAP_S + FINAL_PROBE_SLACK_S
    if tolerant_live:
        # THE TOLERANT (TIER 2B) SEAM. `cad/repair/tolerant_probe.py` (Task 4) is the rung; this
        # is only the time it is allowed. Until its posture exists, the one caller passes
        # `tolerant_live=False`, so this branch is unreachable in production and tested here.
        reserve += max(0.0, float(tolerant_timeout_s))
    return reserve


def _kernel_clean(final: Measure, defects: tuple) -> bool:
    """Is the delivered shape clean BY THE KERNEL'S OWN JUDGEMENT (tier 0)?

    Valid, and carrying no error or fatal finding that the kernel scan made. A `mesh_*` code is
    deliberately ignored: those are the mesh probe's verdict about meshability, which this
    verdict reports SEPARATELY and as a score. Folding them in here would make one number out of
    two different questions, and a part that is kernel-clean but hard to mesh is exactly the case
    standalone repair exists to deliver.
    """
    if not bool(final.valid):
        return False
    for defect in defects:
        code = getattr(defect.code, "value", str(defect.code))
        if str(code).startswith("mesh_"):
            continue
        if defect.severity in _NOT_CLEAN:
            return False
    return True


def _review(packages: tuple) -> list[dict]:
    """The semantic review record, COMMITTED PACKAGES ONLY.

    A reverted or escalated package's geometry never shipped, so its review says nothing about
    what the customer receives; listing it would inflate the record with judgements on work that
    was thrown away.
    """
    return [{"region_index": int(package.region_index),
             "reviewed": package.reviewed,
             "review_reason": package.review_reason,
             "deviation_mm": package.deviation_mm}
            for package in packages if package.committed]


def _default_prober(*, shape, diagonal_mm: float, budget_s: float):
    """The tier-2 probe: write `shape` to a temporary BREP and mesh it in a fresh interpreter.

    THE BREP IS THE POINT, not an inconvenience. gmsh is never imported into this process and an
    OCP pointer never crosses into the child (`mesh_probe`'s ABI rule), so the shape travels as
    bytes on disk. A shape that cannot be written is a PROBE_ERROR - a no-answer, which scores
    `None` - rather than a refusal of the part.
    """
    from OCP.BRepTools import BRepTools

    with tempfile.TemporaryDirectory(prefix="repair-goal-") as tmp:
        path = Path(tmp) / "shape.brep"
        if not BRepTools.Write_s(shape, str(path)):
            return mesh_probe.ProbeResult(False, 0, mesh_probe.MeshFailure(
                mesh_probe.PROBE_ERROR, (), None,
                "the finishing verdict could not write the shape to a BREP"))
        return mesh_probe.probe_file(path, diagonal_mm=float(diagonal_mm), volume=True,
                                     budget_s=float(budget_s))


def _meshability(*, shape, final: Measure, deadline_s: float, clock, prober) -> dict:
    """One tier-2 reading, or an honest statement that there was no time to take one."""
    budget_s = oracle.probe_budget_s(oracle.CHECK_VERDICT, float(deadline_s) - float(clock()),
                                     timeout_s=oracle.TIER2_HARD_CAP_S)
    reading = {"tier": VERDICT_TIER, "meshes": None, "kind": "", "stage": "",
               "volume_elements": 0, "over_element_cap": False,
               "budget_s": budget_s, "seconds": 0.0, "skipped": ""}
    if budget_s <= 0.0:
        reading["skipped"] = NO_TIME
        return reading

    started = float(clock())
    result = prober(shape=shape, diagonal_mm=float(final.diagonal_mm), budget_s=budget_s)
    reading["seconds"] = max(0.0, float(clock()) - started)
    if result.meshed:
        reading["meshes"] = True
        reading["volume_elements"] = int(result.volume_elements)
        reading["over_element_cap"] = int(result.volume_elements) > TIER2_ELEMENT_CAP
        return reading

    failure = result.failure
    kind = "" if failure is None else str(failure.kind)
    reading["kind"] = kind
    reading["stage"] = "" if failure is None else str(failure.stage)
    # A NO-ANSWER LEAVES `meshes` None. gmsh refusing the part is a measured False; our clock
    # running out, or the child dying, is not a verdict about the customer's geometry at all.
    reading["meshes"] = None if kind in _NO_ANSWER_KINDS else False
    return reading


def _grind(reading: dict, final: Measure) -> dict[str, str]:
    """The expected mesh cost of the delivered part, RECORDED BESIDE the verdict and never read.

    `grind` is a pure predictor over the tier-2 reading's failure kind, the stage it was refused
    in and the part's size. Nothing in `cad/` branches on the answer - Task 8's investigator
    budget is its first reader - so this is a column in the record, not a decision.

    A SKIPPED PROBE PREDICTS NOTHING. With no reading, `kind` is `""`, which grind reads as "this
    meshed" and would call cheap: a claim about a part nobody probed. Withholding the face count
    is how this says "not enough is known", which is grind's `UNKNOWN` - the same honest answer a
    part with no face count gets, and the same reason.
    """
    faces = None if reading["skipped"] else getattr(final, "faces", None)
    return grind.record(str(reading["kind"]), str(reading["stage"]),
                        solids=getattr(final, "solids", None), faces=faces)


def _score(reading: dict) -> float | None:
    """The meshability score: 1.0 meshed, 0.0 refused, `None` not answered.

    `None` IS A THIRD VALUE, NOT A ZERO. A gym or a tournament that read a timeout as 0.0 would
    learn that a slow part is a broken part.
    """
    meshes = reading["meshes"]
    if meshes is None:
        return None
    return 1.0 if meshes else 0.0


def finishing_verdict(goal: RepairGoal | None, *, shape, final: Measure, defects: tuple,
                      packages: tuple, deadline_s: float,
                      clock: Callable[[], float] = time.monotonic,
                      prober: Callable[..., Any] | None = None) -> dict | None:
    """What this run finished with, read for `goal`. `None` when no goal was declared.

    NEVER RAISES. An internal error becomes `{"profile": ..., "error": "<type>"}`: the repair that
    produced the shape is the deliverable, and a verdict that could abort it would make reporting
    more dangerous than not reporting.

    `deadline_s` is the run's one absolute deadline on `clock`. `packages` is the region driver's
    package verdicts, empty for the operation-at-a-time driver.
    """
    if goal is None:
        return None
    if goal is RepairGoal.meshing:
        # NO PROBE. A meshing job's verdict is the product mesher, which runs ONCE at the end on
        # the existing promotion path - naming it is the whole answer, and a tier-2 reading here
        # would be a second opinion nobody routes on.
        return {"profile": RepairGoal.meshing.value, "final_verdict": TIER3_PRODUCT_MESHER}
    if goal not in _SELF_FINISHING:
        return None

    profile = goal.value
    try:
        reading = _meshability(shape=shape, final=final, deadline_s=deadline_s, clock=clock,
                               prober=_default_prober if prober is None else prober)
        return {"profile": profile,
                "kernel_clean": _kernel_clean(final, tuple(defects)),
                "meshability": reading,
                "score": _score(reading),
                "grind": _grind(reading, final),
                "review": _review(tuple(packages))}
    except Exception as exc:  # noqa: BLE001 - the finishing verdict never raises
        logger.info("repair goal verdict (%s): %s: %s", profile, type(exc).__name__, exc)
        return {"profile": profile, "error": type(exc).__name__}
