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
from dataclasses import dataclass, field
from pathlib import Path

from meshpipeline.cad.repair.caps import (
    Accounting,
    cumulative_refusal,
    sanity_refusal,
)
from meshpipeline.cad.repair.caps import (
    measure as _measure,
)
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.identity import GONE, EntityRef
from meshpipeline.cad.repair.localize import localize_scan as _localize_scan

logger = logging.getLogger(__name__)

KEPT = "kept"
ROLLED_BACK = "rolled_back"
FAILED = "failed"

#: A new defect at this severity or worse means the operation made things worse. An `info`
#: finding is a remark, not damage.
_HELD_AGAINST = (DefectSeverity.warning, DefectSeverity.error, DefectSeverity.fatal)


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

    @property
    def kept(self) -> bool:
        return self.status == KEPT

    def to_dict(self) -> dict:
        return {"op": self.op, "targets": list(self.targets), "defect_code": self.defect_code,
                "status": self.status, "reason": self.reason, "basis": self.basis,
                "evidence": dict(self.evidence), "delta": dict(self.delta)}


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

    def __init__(self, shape, *, measurer=_measure, scanner=_localize_scan) -> None:
        self._measure = measurer
        self._scan = scanner
        self.shape = shape
        #: THE UPLOAD'S OWN MEASUREMENT, taken once and never replaced. Every cumulative check is
        #: against this, which is what stops six small operations drifting past a cap together.
        self.original = measurer(shape)
        self.accounting = Accounting()
        self._defects: tuple | None = None
        self.complete_scan: bool = True
        self.log: list[OpVerdict] = []

    def defects(self) -> tuple:
        """The current shape's located defects. Scanned once per shape and cached."""
        if self._defects is None:
            found, complete = self._scan(self.shape)
            self._defects = tuple(found)
            self.complete_scan = bool(complete)
        return self._defects

    def current(self):
        """The current shape's measurement. Taken fresh: the caller wants the run's end state."""
        return self._measure(self.shape)

    def write(self, destination: Path) -> None:
        """Write the current shape as STEP, in millimetres."""
        from meshpipeline.cad.repair.conservative import _write_step

        _write_step(self.shape, Path(destination))

    def try_op(self, op, targets: tuple[EntityRef, ...], defect_code, *,
               tolerance_mm: float) -> OpVerdict:
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
            # It matters most for the seat a MODEL takes in slice 8: the rules planner proposes
            # only from the defect list, but a model can propose any target it likes, and this is
            # the check that stops a confident wrong guess being recorded as a success. Refused
            # before the kernel is called, so a bogus target costs nothing.
            return self._record(OpVerdict(op=op.name, targets=fingerprints, defect_code=code,
                                          status=ROLLED_BACK, reason="target_absent"))

        snapshot = (self.shape, self._defects, self.complete_scan)
        before = self._measure(self.shape)

        try:
            outcome = op.apply(self.shape, targets, tolerance_mm=tolerance_mm)
        except Exception as exc:  # noqa: BLE001 - a repair that breaks is never the file's fault
            logger.info("session: %s failed (%s: %s)", op.name, type(exc).__name__, exc)
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=FAILED,
                reason=f"{type(exc).__name__}: {exc}", delta={"before": before.to_dict()}))

        self.shape = outcome.shape
        after = self._measure(self.shape)
        delta = {"before": before.to_dict(), "after": after.to_dict()}

        refusal = sanity_refusal(before, after)
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
                reason=f"cap:{refusal}", evidence=dict(outcome.evidence), delta=delta))

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
                reason=f"declined:{str(declined)[:200]}", evidence=dict(outcome.evidence), delta=delta))

        previous = self.defects()
        self._defects = None
        found = self.defects()

        basis = self._basis(outcome, targets)
        reason = self._progress_refusal(outcome, targets, defect_code, previous, found)
        if reason:
            self.shape, self._defects, self.complete_scan = snapshot
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=reason, basis=basis, evidence=dict(outcome.evidence), delta=delta))

        self.accounting = self.accounting.plus(outcome.evidence)
        return self._record(OpVerdict(
            op=op.name, targets=fingerprints, defect_code=code, status=KEPT, basis=basis,
            evidence=dict(outcome.evidence), delta=delta))

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

    def _progress_refusal(self, outcome, targets, defect_code, previous, found) -> str:
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
        added = set(outcome.added)
        for defect in found:
            if (defect.fingerprint, defect.code.value) in before_names:
                continue
            if defect.severity not in _HELD_AGAINST:
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
