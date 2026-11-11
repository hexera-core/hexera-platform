# Responsibility: Hold ONE shape through a repair - apply an operation to it, judge the result, and
#                 keep or restore it, so that every attempt is reversible and every verdict is
#                 recorded whichever way it went.
# Owns: the snapshot, the order the judgements happen in, the progress rule, and the op log.
# Boundaries: it applies operations somebody else chose. It never decides which operation to try,
#             never writes a report, and never decides when the run is over.
# Collaborates with: cad/repair/operations.py (what it applies), cad/repair/caps.py (what refuses),
#                    cad/repair/localize.py (the re-inspection), cad/repair/loop.py (the driver).
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import (
    Accounting,
    cumulative_refusal,
    sanity_refusal,
)
from meshpipeline.cad.repair.caps import (
    measure as _measure,
)
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import GONE, EntityRef
from meshpipeline.cad.repair.localize import analyzer_only, face_scan_budget_s
from meshpipeline.cad.repair.localize import localize_scan as _localize_scan
from meshpipeline.cad.repair.package import TargetPackage, evaluate, specificity

# THE ONE DEFINITION OF A HOLD, shared with both planners so they cannot disagree about what a
# hold covers (plan.py does not import this module, so this is not a cycle).
from meshpipeline.cad.repair.plan import held_fingerprints

logger = logging.getLogger(__name__)

KEPT = "kept"
ROLLED_BACK = "rolled_back"
FAILED = "failed"

#: The one operation whose keep/rollback re-inspection runs the face-level scan (floor-fix
#: ruling 1b). Every other operation is judged on the status walk alone, as before the analyzer.
HEAL_OP = "heal_self_intersections"

#: A new defect at this severity or worse means the operation made things worse. An `info`
#: finding is a remark, not damage.
_HELD_AGAINST = (DefectSeverity.warning, DefectSeverity.error, DefectSeverity.fatal)


def _geometric_defect_key(defect) -> tuple | None:
    """Name a finding by its measured geometry when a kernel rebuild renames its entity."""
    location = defect.location or {}
    low, high = location.get("bbox_min"), location.get("bbox_max")
    if not low or not high:
        return None
    return (defect.code, defect.severity, defect.entity_type, tuple(low), tuple(high))


@dataclass(frozen=True, slots=True)
class OpVerdict:
    """What happened when one operation was tried, kept or not."""

    op: str
    targets: tuple[str, ...]
    defect_code: str
    status: str
    reason: str = ""
    #: `kernel` when the operation's own history explained what became of the targets, `geometric`
    #: when the answer came from comparing the scans instead. REPORTED rather than inferred later,
    #: because a training signal that cannot tell these apart overstates its confidence.
    basis: str = ""
    evidence: dict = field(default_factory=dict)
    delta: dict = field(default_factory=dict)
    #: Stamped by the loop when a directive proposed this operation; empty for an autonomous one.
    item_id: str | None = None
    source: str = ""
    directive_id: str | None = None
    planner_confidence: float | None = None
    #: Seconds, from the run's injected clock. `apply_s` is the operation itself, `scan_s` the
    #: re-inspection, `measure_s` the whole-shape `caps.measure` calls (twice per operation: a
    #: `BRepCheck_Analyzer` plus area, volume and counts - a cost centre of its own). All default
    #: to 0.0 so older constructions and tolerant rebuilds keep working; a path that never reached
    #: a step leaves that step at 0.0.
    apply_s: float = 0.0
    scan_s: float = 0.0
    measure_s: float = 0.0

    @property
    def kept(self) -> bool:
        return self.status == KEPT

    def to_dict(self) -> dict:
        return {"op": self.op, "targets": list(self.targets), "defect_code": self.defect_code,
                "status": self.status, "reason": self.reason, "basis": self.basis,
                "evidence": dict(self.evidence), "delta": dict(self.delta),
                "apply_s": self.apply_s, "scan_s": self.scan_s, "measure_s": self.measure_s,
                **({"item_id": self.item_id} if self.item_id else {}),
                **({"source": self.source} if self.source else {}),
                **({"directive_id": self.directive_id} if self.directive_id else {}),
                **({"planner_confidence": self.planner_confidence}
                   if self.planner_confidence is not None else {})}


COMMITTED = "committed"
REVERTED = "reverted"
ESCALATED = "escalated"

#: Reason prefix for a package that was never executed, because it was malformed. It shares the
#: `reverted` status - the vocabulary is the three the region loop routes on - so the prefix is
#: what tells "never ran" from "ran and the claim failed". Same idiom as `try_op`'s `cap:` and
#: `declined:`.
REFUSED = "refused:"


@dataclass(frozen=True, slots=True)
class PackageVerdict:
    """What happened to one target package, judged as a whole.

    THE THREE STATUSES ARE NOT INTERCHANGEABLE, because a later stage routes on them: a
    `committed` region ships, an `escalated` one is queued for a person because the package ran
    out of its budget without satisfying its claim, and a `reverted` one was tried and the claim
    did not hold. Conflating the last two sends work to the wrong place.
    """

    region_index: int
    status: str
    reason: str = ""
    #: What `try_op` said about each operation, in plan order, whatever the package's own outcome.
    #: A `kept` verdict here with a `reverted` package means the operation passed its own
    #: judgements and was then discarded with the rest of the plan - which is why the package
    #: verdict, not the op log, is the authority on what survived.
    verdicts: tuple[OpVerdict, ...] = ()
    #: Defects this package discharged BY RELABELLING rather than by repair: the entities its
    #: predicate called `reclassified` that the scoped re-inspection still reports. REPORTED
    #: rather than absorbed into the status, because "clean because nothing unexplained remains"
    #: is a legitimate discharge AND the main abuse surface of the design, so a caller must be
    #: able to see how much of a package's success came from relabelling. Empty when no scoped
    #: scan was taken (an inadmissible package or an exhausted budget discharged nothing).
    reclassified: frozenset[str] = frozenset()
    #: Seconds. AGGREGATED from the operations' own `OpVerdict` timings plus the package's
    #: boundary and scoped scans, so the same work is never timed twice.
    scan_s: float = 0.0
    apply_s: float = 0.0

    @property
    def committed(self) -> bool:
        return self.status == COMMITTED

    def to_dict(self) -> dict:
        return {"region_index": int(self.region_index), "status": self.status,
                "reason": self.reason, "verdicts": [v.to_dict() for v in self.verdicts],
                "reclassified": sorted(self.reclassified),
                "scan_s": self.scan_s, "apply_s": self.apply_s}


class RepairSession:
    """One shape, carried through a repair, with every attempt reversible.

    WHY IN MEMORY. The alternative - a STEP per iteration - is fully replayable and crash-safe,
    and was rejected: a round trip costs seconds per operation and PERTURBS TOLERANCES ITSELF,
    which pollutes the very delta being measured. The final result is still written once, as it
    always was.

    ROLLBACK IS O(1) because OpenCASCADE shapes are handle-based and no operation mutates its
    input: restoring a snapshot is restoring three references.

    ROLLBACK RESTORES THE HANDLE, which is exact for topology and counts; but a kernel operation can
    nudge a shared sub-shape's tolerance in place (measured: 1e-07 became 1.0000000000000002e-07
    across a fully rolled-back run). The caps are safe from it because `original` is measured once
    and never re-read - a future change that let the creep grow would silently widen every cap.

    `measurer` and `scanner` are injectable so the bookkeeping can be tested without a kernel.
    The ordering of the judgements below is the part most likely to be got subtly wrong, and it is
    pure logic.
    """

    def __init__(self, shape, *, measurer=_measure, scanner=None,
                 remaining_s: Callable[[], float] | None = None,
                 reserved: frozenset = frozenset(),
                 clock: Callable[[], float] = time.monotonic) -> None:
        #: Defect codes per-defect review holds for a PERSON, as the planner has them. The planner
        #: will not aim at a held entity; this is the other half - an operation that removes one
        #: without aiming at it (a whole-shape sweep) is rolled back below.
        self._reserved = frozenset(reserved)
        self._measure = measurer
        # AN INJECTED SCANNER IS CALLED AS IT ALWAYS WAS, `scanner(shape)`, and is never told
        # about the face scan: that budget is the default scanner's business. What it returns in
        # a status-walk re-inspection is filtered of analyzer-only findings instead (`_rescan`).
        self._injected = scanner is not None
        self._scan = scanner if scanner is not None else self._default_scan
        #: What is left of the run's time, asked at each scan. None until somebody knows - the
        #: loop assigns one - and then the whole repair allowance is assumed.
        self.remaining_s = remaining_s
        #: The run's clock, handed down by the loop so a test's fake clock drives every timing.
        self.clock = clock
        #: How many times the shape has been scanned, the first scan and every re-inspection.
        self.scan_count: int = 0
        self.shape = shape
        #: THE UPLOAD'S OWN MEASUREMENT, taken once and never replaced. Every cumulative check is
        #: against this, which is what stops six small operations drifting past a cap together.
        self.original = measurer(shape)
        self.accounting = Accounting()
        self._defects: tuple | None = None
        self.complete_scan: bool = True
        self.log: list[OpVerdict] = []
        #: The package-level truth, beside the per-operation `log`. A reader of the log alone
        #: cannot tell a kept operation that shipped from one discarded with its package.
        self.packages: list[PackageVerdict] = []

    def _default_scan(self, shape, *, face_scan: bool = True):
        """`localize_scan`, with a face-level scan only when the self-intersection heal is on.

        `_localize_scan` is looked up AT CALL TIME, so a test patching the module attribute is
        obeyed. With the flag off the budget is 0.0 and `remaining_s` is never even asked, which
        is what keeps a flag-off run's scans byte-for-byte what they were. `face_scan=False` is
        the status-walk re-inspection (`_rescan`): budget 0.0 there too, whatever the flag.
        """
        budget = 0.0
        if face_scan and rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS:
            remaining = self.remaining_s or (lambda: float(rcfg.CAD_REPAIR_TIMEOUT_S))
            budget = face_scan_budget_s(remaining())
        return _localize_scan(shape, face_scan_s=budget)

    def defects(self) -> tuple:
        """The current shape's located defects. Scanned once per shape and cached."""
        if self._defects is None:
            self.scan_count += 1
            found, complete = self._scan(self.shape)
            self._defects = tuple(found)
            self.complete_scan = bool(complete)
        return self._defects

    def _rescan(self, *, face_scan: bool) -> tuple:
        """Re-inspect the current shape for a keep/rollback judgement, and cache the result.

        SCAN PLACEMENT (floor-fix ruling 1). The face scan runs for the session's first scan and
        for the re-inspection that judges a heal; every other operation is judged on the status
        walk alone, exactly as before the analyzer existed. Without this a kept sew was rolled
        back `new_defect` for a crossing the flag-off world never measured (trj4_k1_geo-pe-*).
        An injected scanner is still called as `scanner(shape)`; its analyzer-only findings are
        dropped here instead, so a double cannot reintroduce the demotion either.
        """
        self.scan_count += 1
        if self._injected:
            found, complete = self._scan(self.shape)
        else:
            found, complete = self._default_scan(self.shape, face_scan=face_scan)
        if not face_scan:
            found = [d for d in found if not analyzer_only(d)]
        self._defects = tuple(found)
        self.complete_scan = bool(complete)
        return self._defects

    def _timed_measure(self, shape) -> tuple:
        """`(measurement, seconds)` - the whole-shape measure is a cost centre to be reported."""
        started = self.clock()
        measured = self._measure(shape)
        return measured, self.clock() - started

    def current(self):
        """The current shape's measurement. Taken fresh: the caller wants the run's end state."""
        return self._measure(self.shape)

    def write(self, destination: Path) -> None:
        """Write the current shape as STEP, in millimetres."""
        from meshpipeline.cad.repair.conservative import _write_step

        _write_step(self.shape, Path(destination))

    def try_op(self, op, targets: tuple[EntityRef, ...], defect_code, *,
               tolerance_mm: float, answered_held: frozenset[str] = frozenset()) -> OpVerdict:
        """Apply one operation and keep it only if it repaired what it aimed at.

        THE ORDER IS THE DESIGN:
          1. snapshot          - so a rollback restores exactly what arrived
          2. apply             - an exception here is `failed`, not a crash
          3. per-op sanity     - counts only, CHEAP
          4. cumulative caps   - against the original
          4b. declined         - the operation said no and why; recorded as such, nothing to check
          5. re-inspect        - EXPENSIVE, so it is paid for last
          6. progress rule     - is the targeted defect gone, and nothing new broken
          7. keep or restore   - either way, it goes in the log
        """
        code = getattr(defect_code, "value", str(defect_code))
        fingerprints = tuple(ref.fingerprint for ref in targets)

        if any(not ref.targetable for ref in targets):
            # NOTHING TO VERIFY AGAINST. An unlocated defect has no name, so the progress rule
            # would be vacuously true and the operation would be kept for doing anything at all.
            return self._record(OpVerdict(op=op.name, targets=fingerprints, defect_code=code,
                                          status=ROLLED_BACK, reason="untargetable"))

        if not self._carries(targets, defect_code):
            # THE PROGRESS RULE NEEDS POSITIVE EVIDENCE, NOT MERELY AN ABSENCE. Asked to fix a
            # defect the target does not actually have, every later check passes vacuously - the
            # defect is "gone" because it was never there - and the operation is KEPT for doing
            # nothing. That makes `kept` stop meaning "repaired something", which is precisely the
            # signal this slice exists to accumulate.
            #
            # It matters most for model guidance: the rules fallback proposes only from the defect
            # list, but a model can propose any target it likes, and this is
            # the check that stops a confident wrong guess being recorded as a success. Refused
            # before the kernel is called, so a bogus target costs nothing.
            return self._record(OpVerdict(op=op.name, targets=fingerprints, defect_code=code,
                                          status=ROLLED_BACK, reason="target_absent"))

        snapshot = (self.shape, self._defects, self.complete_scan)
        before, measure_s = self._timed_measure(self.shape)

        apply_started = self.clock()
        try:
            outcome = op.apply(self.shape, targets, tolerance_mm=tolerance_mm)
        except Exception as exc:  # noqa: BLE001 - a repair that breaks is never the file's fault
            logger.info("session: %s failed (%s: %s)", op.name, type(exc).__name__, exc)
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=FAILED,
                reason=f"{type(exc).__name__}: {exc}", delta={"before": before.to_dict()},
                apply_s=self.clock() - apply_started, measure_s=measure_s))
        apply_s = self.clock() - apply_started

        self.shape = outcome.shape
        after, after_s = self._timed_measure(self.shape)
        measure_s += after_s
        delta = {"before": before.to_dict(), "after": after.to_dict()}

        # HELD GEOMETRY IS NOT A SWEEP'S TO REMOVE. The planner refuses an operation whose
        # TARGETS carry a held defect, but that is only half the guard: a whole-shape sweep
        # removes faces nobody aimed at. A hold is recorded against the FACE's fingerprint, so
        # FixSmallFaces answering a `small_edge` on a held face would carry the face off while the
        # engineer is still being asked about it. Checked here, where what actually went is known,
        # and cheap enough to run before the caps.
        # `answered_held` IS THE ENGINEER'S ANSWER. RepairPlanner lets an engineer's directive -
        # and a replay of an operation this upload already kept - aim at a held defect, because
        # being asked and answering is the whole point of review. A blanket refusal here would
        # roll back the repair the engineer just approved. So the exemption is exactly the
        # entities that directive answers, and every OTHER held face is still protected: an
        # approval of one sliver is not a licence to sweep the rest of the part.
        held = held_fingerprints(self._defects, self._reserved) - frozenset(answered_held)
        removed_held = sorted(held.intersection(
            outcome.evidence.get("removed_fingerprints") or ()))
        if removed_held:
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=f"held_for_review:{len(removed_held)} removed entity(s) carry a defect "
                       f"an engineer is being asked about ({removed_held[0]})",
                evidence=dict(outcome.evidence), delta=delta,
                apply_s=apply_s, measure_s=measure_s))

        # The face-count twin of the area/volume declarations: only the sliver sweep sets
        # `faces_removed`, and only a removal of exactly that many is excused (caps.sanity_refusal).
        refusal = sanity_refusal(before, after, declared_faces_removed=int(
            outcome.evidence.get("faces_removed", 0) or 0))
        if not refusal:
            projected = self.accounting.plus(outcome.evidence)
            refusal = cumulative_refusal(
                self.original, after,
                declared_area_mm2=projected.declared_area_mm2,
                declared_volume_mm3=projected.declared_volume_mm3)
        if refusal:
            # REFUSED BEFORE THE RE-INSPECTION IS PAID FOR. That saving is the reason the caps run
            # in this order.
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=f"cap:{refusal}", evidence=dict(outcome.evidence), delta=delta,
                apply_s=apply_s, measure_s=measure_s))

        declined = outcome.evidence.get("declined")
        if declined:
            # A REFUSAL IS NOT A FAILED ATTEMPT. The operation said no, and why, by returning the
            # shape unchanged; the progress rule would see no progress and log the generic
            # `target_remains`, making "declined for a stated reason" indistinguishable from "tried
            # and it did not work". After the caps, so a declining operation cannot smuggle damage
            # through; before the re-inspection, which would be paid for to learn nothing.
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=f"declined:{str(declined)[:200]}", evidence=dict(outcome.evidence), delta=delta,
                apply_s=apply_s, measure_s=measure_s))

        previous = self.defects()
        # THE HEAL IS JUDGED ON THE FACE SCAN, so `crossing_remains`/`new_defect` still police it;
        # everything else on the status walk alone, against a `previous` without the analyzer's
        # findings, which is the comparison the flag-off run makes.
        judged_by_face_scan = (op.name == HEAL_OP
                               or code == DefectCode.self_intersection.value)
        scan_started = self.clock()
        found = self._rescan(face_scan=judged_by_face_scan)
        scan_s = self.clock() - scan_started
        if not judged_by_face_scan:
            carried = tuple(d for d in previous if analyzer_only(d))
            previous = tuple(d for d in previous if not analyzer_only(d))

        basis = self._basis(outcome, targets)
        reason = self._progress_refusal(
            outcome, targets, defect_code, previous, found, op_name=op.name)
        if reason:
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=reason, basis=basis, evidence=dict(outcome.evidence), delta=delta,
                apply_s=apply_s, scan_s=scan_s, measure_s=measure_s))

        if not judged_by_face_scan and carried:
            # THE ANALYZER'S FINDINGS STAY ON THE LIST after a kept operation that did not re-run
            # the face scan: they still target the heal and still reach `defects_remaining`, and
            # the heal's own re-inspection re-measures them. Marked as not re-scanned since, so
            # nobody reads them as a fresh measurement of the changed shape.
            from dataclasses import replace

            present = {(d.fingerprint, d.code) for d in found}
            self._defects = (*found, *(
                replace(d, measurements={**d.measurements, "not_rescanned_after": op.name})
                for d in carried if (d.fingerprint, d.code) not in present))

        self.accounting = self.accounting.plus(outcome.evidence)
        return self._record(OpVerdict(
            op=op.name, targets=fingerprints, defect_code=code, status=KEPT, basis=basis,
            evidence=dict(outcome.evidence), delta=delta,
            apply_s=apply_s, scan_s=scan_s, measure_s=measure_s))

    def try_package(self, package: TargetPackage) -> PackageVerdict:
        """Execute one target package as ONE reversible unit, and prove it against its own claim.

        THE SAFETY PROPERTY IS THE ORDERING, NOT THE CHECKS. The package's author - eventually a
        model - states its predicate BEFORE any of this runs, and this method only measures that
        claim. An author can never grade its own work; it can only say something a separate
        measurement checks.

        THE ORDER IS THE DESIGN, for the reason `try_op` gives:
          1. admit the package    - the budget's own sanity, then the predicate's specificity,
                                    locus and the plan's well-formedness. CHEAP, and a malformed
                                    package must not cost a scan. Refusals keep the `reverted`
                                    status with a `refused:` reason, because nothing ran.
          2. budget               - a plan larger than the budget is refused unapplied
          3. snapshot             - ONE, at the package boundary
          4. apply the plan       - through `try_op`, so every cap, hold, declaration and
                                    per-operation rollback still applies, operation by operation
          5. one scoped scan      - EXPENSIVE, so it is paid for last, and only ONE for the whole
                                    plan: that saving is the point of scoping a package to a region
          6. `package.evaluate`   - the claim against the measurements
          7. commit, or restore the BOUNDARY snapshot

        ALL OR NOTHING. The predicate was written about the whole plan, so a partially applied
        package was never what the author claimed: a failed predicate, a budget exhausted
        mid-plan and an incomplete scoped scan all revert to step 3's snapshot, not to the last
        operation's.

        AN INCOMPLETE SCOPED SCAN CAN NEVER COMMIT. A short list from a stopped walk supports no
        claim of absence, so it supports no predicate either.

        A plan item is `{"op": RepairOp, "targets": tuple[EntityRef, ...], "defect_code": ...,
        "tolerance_mm": float}` - the tolerance optional, and set by the author because a package
        names the operations it wants performed.

        A PLAN ITEM CANNOT CARRY `answered_held`, deliberately. In `try_op` that is THE ENGINEER'S
        ANSWER - the exemption that lets a human's directive touch geometry held for review - and
        a package plan is authored by a model. Reading it from the plan would let the author
        exempt itself from a hold on geometry an engineer is still being asked about, which is a
        package replacing an inner proof rather than adding an outer one. When hold provenance
        arrives it must carry a human-directive marker, not come from the plan.
        """
        region = package.region
        predicate = package.predicate

        refusal = self._package_refusal(package)
        if refusal:
            # A REFUSAL, NOT AN ESCALATION. The package was malformed, not defeated: there is
            # nothing here for a person to look at in the geometry, only an author to fix.
            # AND NOT A REVERT EITHER, which is why the reason is PREFIXED. The status vocabulary
            # is the three the loop routes on, so a fourth is not available; but "malformed, never
            # ran" and "ran, and the claim did not hold" are different diagnoses with different
            # remedies, and nothing was undone here because nothing was applied. `try_op` makes
            # the same distinction in text with `cap:` and `declined:`; this follows that idiom,
            # so a caller can tell the two apart from the verdict alone.
            return self._record_package(PackageVerdict(
                region_index=region.index, status=REVERTED, reason=f"{REFUSED}{refusal}"))

        if len(package.plan) > package.budget.max_ops:
            return self._record_package(PackageVerdict(
                region_index=region.index, status=ESCALATED,
                reason=f"budget: the plan's {len(package.plan)} operation(s) exceed the "
                       f"package budget of {package.budget.max_ops}"))

        started = self.clock()
        before = self._measure(self.shape)

        scan_started = self.clock()
        # THE WHOLE-SHAPE LIST, deliberately, and normally already cached and so free. The
        # predicate may only name entities in the region (checked above), so every comparison
        # `evaluate` makes against it is a comparison about this region; and reading the cached
        # whole-shape list avoids a second scan to learn what is already known.
        defects_before = tuple(d.fingerprint for d in self.defects())
        scan_s = self.clock() - scan_started

        # THE SNAPSHOT IS TAKEN AFTER THE BOUNDARY LIST IS MATERIALISED, so restoring it restores
        # the defect list the package found rather than an uncached `None` - which would make a
        # reversal cost a fresh whole-shape scan to re-learn what was already measured here.
        #
        # AND IT CARRIES THE ACCOUNTING, which `try_op`'s own snapshot does not need to: `try_op`
        # adds to the accounting only for an operation it KEPT, and that is the whole of its
        # contract ("Immutable, so a rolled-back operation cannot leave a trace", caps.Accounting).
        # A PACKAGE BREAKS THAT INVARIANT UNLESS IT RESTORES IT HERE: its operations are kept
        # individually and then discarded together, so without this a reverted package leaves the
        # area and volume DECLARATIONS of geometry that no longer exists. The next legitimate
        # operation anywhere in the run - in this region or any later one - is then refused for a
        # residual nothing accounted for, which is one discarded package poisoning the whole run.
        snapshot = (self.shape, self._defects, self.complete_scan, self.accounting)
        apply_s = 0.0
        verdicts: list[OpVerdict] = []

        for item in package.plan:
            spent = self.clock() - started
            if spent > package.budget.timeout_s:
                self.shape, self._defects, self.complete_scan, self.accounting = snapshot
                return self._record_package(PackageVerdict(
                    region_index=region.index, status=ESCALATED,
                    reason=f"budget: {spent:.6g}s of {package.budget.timeout_s:.6g}s spent after "
                           f"{len(verdicts)} of {len(package.plan)} operation(s)",
                    verdicts=tuple(verdicts), scan_s=scan_s, apply_s=apply_s))
            # AN OPERATION THAT ROLLED ITSELF BACK DOES NOT END THE PACKAGE. Its own judgements
            # already undid it, and the predicate - written about the whole plan - is what says
            # whether the plan as a whole did what was claimed.
            verdict = self.try_op(
                item["op"], tuple(item["targets"]), item["defect_code"],
                tolerance_mm=float(item.get("tolerance_mm", 0.0)))
            verdicts.append(verdict)
            apply_s += verdict.apply_s
            scan_s += verdict.scan_s

        scan_started = self.clock()
        found, complete = self._region_scan(region)
        scan_s += self.clock() - scan_started
        defects_after = tuple(d.fingerprint for d in found)
        # Computed from what the scan actually found, so this reports relabelling that really
        # discharged a surviving defect rather than merely what the author asked for.
        reclassified = frozenset(predicate.reclassified).intersection(defects_after)

        if not complete:
            # ESCALATED, NOT REVERTED, and the distinction is the routing: `reverted` tells a
            # reader the claim was tested and failed, and this claim was never judged at all - we
            # could not see enough of the region to decide. An unjudgeable region belongs in front
            # of a person, not filed as a failed claim. No `refused:` prefix: the package DID run.
            self.shape, self._defects, self.complete_scan, self.accounting = snapshot
            return self._record_package(PackageVerdict(
                region_index=region.index, status=ESCALATED,
                reason="scan incomplete: a scoped re-inspection that stopped early supports no "
                       "claim about what is absent from the region",
                verdicts=tuple(verdicts), reclassified=reclassified,
                scan_s=scan_s, apply_s=apply_s))

        reason = evaluate(predicate, before=before, after=self._measure(self.shape),
                           defects_before=defects_before, defects_after=defects_after)
        if reason:
            self.shape, self._defects, self.complete_scan, self.accounting = snapshot
            return self._record_package(PackageVerdict(
                region_index=region.index, status=REVERTED, reason=reason,
                verdicts=tuple(verdicts), reclassified=reclassified,
                scan_s=scan_s, apply_s=apply_s))

        return self._record_package(PackageVerdict(
            region_index=region.index, status=COMMITTED, verdicts=tuple(verdicts),
            reclassified=reclassified, scan_s=scan_s, apply_s=apply_s))

    @staticmethod
    def _package_refusal(package: TargetPackage) -> str:
        """"" if this package may be executed, otherwise why it may not. NO WORK IS DONE HERE.

        A budget that permits unbounded time is not a budget, so a non-positive `timeout_s` is
        malformed rather than permissive.

        An empty plan, an unlocated region and a predicate naming the empty fingerprint are all
        refused for one reason: none of them leaves anything for the scoped scan to judge.

        A predicate that names NO entity asserts only what is always true, so an author could
        pass by promising nothing. A predicate that names geometry OUTSIDE the region cannot be
        verified by the scoped scan that judges it - and padding the claim with out-of-region
        names is exactly how an author would inflate its specificity past this gate.
        """
        budget = package.budget
        if budget.timeout_s <= 0.0:
            # A BUDGET THAT PERMITS UNBOUNDED TIME IS NOT A BUDGET. Read as "no mid-plan timeout",
            # a zero would hand an author unlimited wall clock on a region by nothing worse than a
            # bug - and the per-region budget is the cost control of this whole loop, so that has
            # to be loud rather than silent. A region that cannot afford its plan escalates; it
            # does not quietly become free.
            return f"budget permits unbounded time: timeout_s is {budget.timeout_s:.6g}"
        if budget.max_ops < 0:
            # ZERO IS NOT MALFORMED HERE, and the asymmetry with `timeout_s` is the point: a zero
            # operation allowance permits NOTHING, so any non-empty plan escalates on it below -
            # which is a budget working. A NEGATIVE allowance is a value with no reading at all.
            return f"budget is nonsensical: max_ops is {budget.max_ops}"

        predicate = package.predicate
        if specificity(predicate) == 0.0:
            return ("predicate names no entity: a claim that promises nothing is true of any "
                    "outcome and cannot be verified")

        named = predicate.cleared | predicate.reclassified | predicate.unchanged
        if "" in named:
            # NOTHING TO VERIFY AGAINST, which is the refusal `try_op` already makes for an
            # untargetable target. `localize` names an entity with no measurable extent `""`, so
            # a claim about `""` is a claim about no entity in particular: the scan reports the
            # empty name whatever happened, and the predicate holds for doing anything at all.
            # It would even score specificity, which is how an author would get past that gate
            # while promising nothing.
            return "predicate names the empty fingerprint, which identifies no entity"

        outside = sorted(named - package.region.fingerprints)
        if outside:
            return ("predicate names entities outside region "
                    f"{package.region.index}: {', '.join(outside)}")

        if not package.region.located:
            # THE UNLOCATED REGION IS NOT A LOCUS. `regions.decompose` returns the defects with no
            # measurable extent as one trailing region with no box, precisely so they cannot be
            # silently dropped - and a scan scoped to it is the whole-shape scan, because
            # `_Scope` rejects nothing it cannot place. So there is nothing scoped about the
            # proof and nothing targetable in the region: those defects are a person's question,
            # not a package's.
            return f"region {package.region.index} is unlocated: it has no box to scope a proof to"

        if not package.plan:
            # AN EMPTY PLAN HAS NOTHING TO PROVE. A predicate naming only `unchanged` entities
            # would hold trivially, and the package would buy a commit with two scans and no work.
            return "plan is empty: a package with no operations has nothing to prove"

        for n, item in enumerate(package.plan):
            missing = [key for key in ("op", "targets", "defect_code") if not item.get(key)]
            if missing:
                return f"malformed plan item {n}: missing {', '.join(missing)}"
        return ""

    def _region_scan(self, region) -> tuple[tuple, bool]:
        """ONE re-inspection, scoped to the region, for the whole package.

        IT DOES NOT BECOME THE SESSION'S DEFECT LIST. A scoped list is exhaustive only within its
        scope, and `self._defects`/`self.complete_scan` are read as statements about the whole
        shape; replacing them with a region's findings would claim the rest of the part had been
        checked and found sound. The package's own verdict carries what this scan said instead.

        The face-level budget is the one the session's first scan uses, not the status-walk-only
        one a per-operation re-inspection gets: this is the scan a commit rests on, so within the
        region it is as thorough as the original inspection was.
        """
        self.scan_count += 1
        if self._injected:
            found, complete = self._scan(self.shape)
        else:
            budget = 0.0
            if rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS:
                remaining = self.remaining_s or (lambda: float(rcfg.CAD_REPAIR_TIMEOUT_S))
                budget = face_scan_budget_s(remaining())
            found, complete = _localize_scan(self.shape, face_scan_s=budget, within=region)
        return tuple(found), bool(complete)

    def _record_package(self, verdict: PackageVerdict) -> PackageVerdict:
        self.packages.append(verdict)
        return verdict

    def _carries(self, targets, defect_code) -> bool:
        """Does at least one target actually carry the defect it is being repaired for?

        Read from the CURRENT defect list, before anything is applied. One target carrying it is
        enough: an operation legitimately widens its aim to a defect's neighbours (the faces that
        own an edge, the shells that own a face), so demanding every target carry the defect would
        refuse the scoped operations this slice is built on.
        """
        wanted = getattr(defect_code, "value", str(defect_code))
        names = {ref.fingerprint for ref in targets}
        return any(d.fingerprint in names and d.code.value == wanted for d in self.defects())

    def _progress_refusal(self, outcome, targets, defect_code, previous, found, *,
                           op_name: str) -> str:
        """"" if this operation made progress, otherwise why it did not.

        PROGRESS IS ABOUT THE DEFECT, NOT THE ENTITY. A hole patch leaves the rim edges exactly
        where they were and merely stops them being a free boundary, so "the targeted entity
        disappeared" would reject every successful fill. What is asked is whether the defect's
        code still appears at the target - or at whatever the mapping says the target became.
        """
        wanted = {getattr(defect_code, "value", str(defect_code))}
        for ref in targets:
            # THE HISTORY IS A HINT ABOUT WHICH NAME TO LOOK FOR, NEVER A LICENCE TO SKIP LOOKING.
            # The re-inspection is the authority: a mapping that says an entity is gone (an
            # optimistic or stale record) must not override a scan that still reports the defect
            # there. A GONE target simply has no defect at its fingerprint when the claim is true.
            _fate, successor = outcome.mapping.fate_of(ref.fingerprint)
            names = {ref.fingerprint, successor} - {""}
            if any(d.fingerprint in names and d.code.value in wanted for d in found):
                return "target_remains"

        before_names = {(d.fingerprint, d.code.value) for d in previous}
        before_geometry = {key for d in previous if (key := _geometric_defect_key(d)) is not None}
        added = set(outcome.added)
        errors = (DefectSeverity.error, DefectSeverity.fatal)
        fewer_non_error_findings = (not any(d.severity in errors for d in found)
                                    and len(found) < len(previous))
        improved_to_warnings = (any(d.severity in errors for d in previous)
                                and fewer_non_error_findings)
        bounded_sliver_reduction = (op_name == "fix_small_faces"
                                    and fewer_non_error_findings)
        for defect in found:
            if (defect.fingerprint, defect.code.value) in before_names:
                continue
            geometric_key = _geometric_defect_key(defect)
            if geometric_key is not None and geometric_key in before_geometry:
                continue
            if defect.severity not in _HELD_AGAINST:
                continue
            if (defect.severity is DefectSeverity.warning
                    and (improved_to_warnings or bounded_sliver_reduction)):
                # A broad kernel repair can split a face while removing the invalid topology that
                # made the body unusable; the bounded sliver sweep can likewise rename neighbouring
                # warning geometry while reducing the total. The finding count must fall, so
                # neither path can excuse a warning explosion. Any surviving or newly introduced
                # error disables this allowance.
                continue
            if defect.fingerprint in added and defect.severity is DefectSeverity.warning:
                # GEOMETRY THE OPERATION SAID IT WAS ADDING. A sliver warning on a patch face is
                # the expected cost of the repair, not evidence it went wrong - and holding it
                # against the operation would make every hole fill impossible. Only a WARNING is
                # excused: an error or fatal finding (a self-intersecting patch, say) is damage, not
                # a cost, and no cap would catch it since the caps see only counts and sizes.
                continue
            return "new_defect"
        return ""

    @staticmethod
    def _basis(outcome, targets) -> str:
        """`kernel` only when the history is BOTH available and unambiguous about these targets.

        The claim is "the kernel explained this target's fate", which is narrower than "the kernel
        was consulted": a mapping can be built from a history that recorded every target as
        unmoved, and then the conclusion came purely from diffing the two scans.
        """
        if any(ref.ambiguous for ref in targets):
            # Two entities share a box. The kernel's record may be perfect and still be about the
            # other one, so nothing here may be called a kernel fact.
            return "geometric"
        explained = any(outcome.mapping.fate_of(ref.fingerprint)[0] in (GONE, "moved")
                        for ref in targets)
        return outcome.mapping.basis if explained else "geometric"

    def _record(self, verdict: OpVerdict) -> OpVerdict:
        self.log.append(verdict)
        return verdict
