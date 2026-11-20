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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import mesh_probe, oracle, package_review, review_tier
from meshpipeline.cad.repair.caps import (
    Accounting,
    Measure,
    cumulative_refusal,
    sanity_refusal,
)
from meshpipeline.cad.repair.caps import (
    measure as _measure,
)
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import GONE, EntityRef, run_grid
from meshpipeline.cad.repair.localize import (
    analyzer_only,
    face_scan_budget_s,
    in_region,
    scoped_to,
)
from meshpipeline.cad.repair.localize import localize_scan as _localize_scan
from meshpipeline.cad.repair.package import TargetPackage, evaluate, specificity

# THE ONE DEFINITION OF A HOLD, shared with both planners so they cannot disagree about what a
# hold covers (plan.py does not import this module, so this is not a cycle).
from meshpipeline.cad.repair.plan import held_fingerprints
from meshpipeline.cad.repair.regions import Region

logger = logging.getLogger(__name__)

KEPT = "kept"
ROLLED_BACK = "rolled_back"
FAILED = "failed"

#: The one operation whose keep/rollback re-inspection runs the face-level scan (floor-fix
#: ruling 1b). Every other operation is judged on the status walk alone, as before the analyzer.
HEAL_OP = "heal_self_intersections"

#: THE SHARE OF THE RUN'S REMAINING TIME ONE MESH PROBE MAY TAKE, and the seconds the region loop
#: keeps in hand for its final whole-shape verification. Both are the oracle's now (the ladder is
#: one policy module); re-exported here because `region_loop` and the tests read them off
#: `session`.
MESH_PROBE_FRACTION = oracle.MESH_PROBE_FRACTION
FINAL_PROBE_SLACK_S = oracle.FINAL_PROBE_SLACK_S


def mesh_probe_budget_s(remaining_s: float, *, final: bool = False) -> float:
    """Seconds one mesh probe may take with `remaining_s` left before the run's deadline.

    The rule is `oracle.probe_budget_s`'s, for the ladder's tier-2 checks: the whole-part probe
    mid-run (`whole`) and the run's last scan (`final`). See the oracle for what each gets and
    why; this is the session's door onto it, reading the configured per-probe timeout.
    """
    check = oracle.CHECK_FINAL if final else oracle.CHECK_WHOLE
    return oracle.probe_budget_s(check, remaining_s,
                                 timeout_s=float(rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S))


#: A new defect at this severity or worse means the operation made things worse. An `info`
#: finding is a remark, not damage.
_HELD_AGAINST = (DefectSeverity.warning, DefectSeverity.error, DefectSeverity.fatal)


def _is_mesh(defect) -> bool:
    """Is this one of the mesh probe's findings (a `mesh_*` code) rather than the kernel scan's?"""
    return getattr(defect.code, "value", str(defect.code)).startswith("mesh_")


def _unlocated_mesh(defect) -> bool:
    """A mesh finding with no box: a whole-part verdict (no volume, a timeout, an unplaceable
    failure). It is about the part, not a place, so no scope - and no one operation - owns it."""
    location = defect.location or {}
    return _is_mesh(defect) and (not location.get("bbox_min") or not location.get("bbox_max"))


def _scope_blind(defect) -> bool:
    """A mesh finding no scoped probe can speak to: unlocated (about the whole part), or located
    but of the volume stage (`mesh_probe.volume_stage`), which a 2D-only scoped probe never
    builds. Either way only a WHOLE-SHAPE probe may clear it (controller ruling R11a)."""
    return _unlocated_mesh(defect) or (_is_mesh(defect) and mesh_probe.volume_stage(defect))


def _judged_whole(package) -> bool:
    """Must this package be proved by WHOLE-SHAPE scans rather than scoped ones?

    Yes when any operation in its plan ANSWERS a volume-stage code (`mesh_probe.VOLUME_CODES`, the
    family `_scope_blind` already refuses a scoped probe the right to clear). Such an operation is
    aimed at a failure only the volume mesh shows, so a scoped (2D-only) probe would carry the
    failure through unseen and roll every such operation back `target_remains` - the package could
    never commit however right it was. The whole-shape probe builds the volume, so it can judge it.
    """
    return any(frozenset(getattr(item.get("op"), "answers", ())) & mesh_probe.VOLUME_CODES
               for item in package.plan)


def _same_shape(before, after) -> bool:
    """Is `after` the very shape `before` was - the same object, or the kernel's same TShape?"""
    if before is after:
        return True
    is_same = getattr(before, "IsSame", None)
    try:
        return bool(is_same(after)) if callable(is_same) else before == after
    except Exception:  # noqa: BLE001 - unknown means changed; the probe still has to pass
        return False


def _as_delivered(shape):
    """`shape` as a customer receives it: written to STEP by the writer promotion uses
    (`conservative._write_step`) and read back. None when either side fails - never a guess.
    Looked up at call time so a test can patch it."""
    import tempfile

    try:
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader

        from meshpipeline.cad.repair.conservative import _write_step

        with tempfile.TemporaryDirectory(prefix="crs-delivered-") as tmp:
            path = Path(tmp) / "delivered.step"
            _write_step(shape, path)
            reader = STEPControl_Reader()
            if reader.ReadFile(str(path)) != IFSelect_RetDone or not reader.TransferRoots():
                return None
            delivered = reader.OneShape()
            return None if delivered is None or delivered.IsNull() else delivered
    except Exception as exc:  # noqa: BLE001 - an export that fails proves no cure
        logger.info("session: delivered round-trip failed: %s: %s", type(exc).__name__, exc)
        return None


def _face_names_of(shape) -> frozenset[str]:
    """Every face of `shape` named exactly as `OpOutcome.rebuilt` names them - at the part's
    quantum (`operations._face_names` over `operations._diagonal_of`, the very pair the rebuilding
    operations use on the shape they were handed). Looked up at call time so a test can patch it."""
    from meshpipeline.cad.repair.operations import _diagonal_of, _face_names

    return frozenset(_face_names(shape, diagonal_mm=_diagonal_of(shape)))


def _revealed(op, outcome, targets, defect_code, previous, found, *,
              pre_op_names: Callable[[], frozenset[str]] = frozenset,
              standing: frozenset | None = None) -> tuple:
    """The mesh findings an operation REVEALED rather than caused. APPLIES TO EVERY MESH OPERATION.

    gmsh reports only its FIRST failure. An operation that clears the mesh failure it aimed at
    therefore lets the probe get further, and the next failure it meets appears in the rescan as
    if new - read as `new_defect`, that rolls the operation back exactly where it worked
    (measured: fragment_bodies on abc/00506275 clears the edge through a face, and the probe then
    reports overlapping facets on face 943, which was always there behind it).

    A newly appearing finding is revealed when ALL of (controller ruling, P2 Task 3 fix round 1):
      (a) the operation is a MESH OPERATION - its own `answers` hold the mesh code it was aimed
          at - and that defect CLEARED in the rescan: nothing with its code remains at the target
          or at what the mapping says the target became. An operation that does not answer the
          mesh code (a kernel fixer a directive or MESH_OPS aims at a mesh failure) earns no
          amnesty: it does not declare what it rebuilt, so (c) would be hollow for it;
      (b) the new finding is itself a `mesh_*` code - a kernel finding is never excused here;
      (c) it does not sit on geometry the operation created, moved or CHANGED: its fingerprint is
          in neither `outcome.added` nor the mapping's moved names, and it is not a face of
          `outcome.rebuilt` whose name did NOT exist on the pre-op shape. If it does, it is
          damage, and rolls back as it always did (ruling P2-R7).

    WHY A REBUILT FACE WITH A SURVIVING NAME IS NOT THE OPERATION'S OWN. `rebuilt` is the candidate
    set: every face of a body the op replaced whole - on a two-body part, every face of the part.
    A face whose fingerprint is unchanged crossed the rebuild unchanged to the grid's precision,
    so a mesh failure there is one gmsh's serial coverage now reaches, not one the rebuild made.
    WHERE THE PRE-OP NAMES COME FROM: the session holds only the pre-op DEFECTS' fingerprints, not
    every face's, so the pre-op shape's faces are fingerprinted (`_face_names_of`, the way
    `rebuilt` is computed) - lazily, and only when a candidate actually lands on a rebuilt face.
    THE CAVEAT, stated: the fingerprint grid has a quantum, so a sub-quantum deformation could in
    principle keep a name and pass as unchanged. That is bounded: a revealed finding stays on the
    defect list (it blocks `resolved`) and the run's final whole-shape probe still scores the part.
    A revealed finding is NOT dropped: it stays in the rescan and so in the session's list, where
    the loop must answer or escalate it, and `try_op` records it under the delta's `revealed` key.

    AN UNLOCATED EVIDENCE FINDING (no volume, `mesh_other`, a failure the probe could not place)
    that newly appears is judged by the same (a) and (b), and passes (c) BY CONSTRUCTION: it names
    no face, so it cannot sit on geometry the operation made (final review I1). (a) already means
    the pre-op list held a mesh finding - the target - so a part that meshed and now does not is
    never excused here; that is `new_defect`. A no-answer never reaches this (`_mesh_judged`).
    """
    if not _is_mesh_code(defect_code) or defect_code not in frozenset(
            getattr(op, "answers", ())):
        return ()
    wanted = getattr(defect_code, "value", str(defect_code))
    for ref in targets:
        _fate, successor = outcome.mapping.fate_of(ref.fingerprint)
        names = {ref.fingerprint, successor} - {""}
        if any(d.fingerprint in names and d.code.value == wanted for d in found):
            return ()
    previous, found = _mesh_judged(previous, found, standing=standing)
    before = {(d.fingerprint, d.code.value) for d in previous}
    touched = set(outcome.added) | set(outcome.mapping.moved) | set(outcome.mapping.moved.values())
    rebuilt = frozenset(outcome.rebuilt)
    candidates = [d for d in found
                  if _is_mesh(d) and (d.fingerprint, d.code.value) not in before
                  and (_unlocated_mesh(d)
                       or (d.fingerprint and d.fingerprint not in touched))]
    if any(d.fingerprint and d.fingerprint in rebuilt for d in candidates):
        # A REBUILT FACE IS THE OP'S OWN ONLY IF THE REBUILD CHANGED ITS NAME (see above).
        existed = pre_op_names()
        candidates = [d for d in candidates
                      if not d.fingerprint or d.fingerprint not in rebuilt
                      or d.fingerprint in existed]
    return tuple(candidates)


def _is_mesh_code(code) -> bool:
    return getattr(code, "value", str(code)).startswith("mesh_")


def _standing(previous) -> frozenset:
    """The codes of the unlocated mesh findings a defect list holds, no-answers included."""
    return frozenset(d.code for d in previous if _unlocated_mesh(d))


def _mesh_judged(previous, found, *, standing: frozenset | None = None) -> tuple[tuple, tuple]:
    """Both sides of the progress rule, less the mesh findings that cannot speak to one operation.

    A NO-ANSWER (`mesh_probe.NO_ANSWER_CODES`: a timeout, a dead probe) IS NEVER NEW AND NEVER
    CLEARED: a probe that timed out after an operation says nothing about what the operation did,
    and one that finished after timing out before says nothing either. And WHEN EITHER SIDE TIMED
    OUT, no mesh finding is compared at all for this operation - one side was not walked, so what
    appears on the other is not news.

    AN UNLOCATED EVIDENCE FINDING IS JUDGED (final review I1). It is the mesher's answer about the
    whole part - no volume, `mesh_other`, a failure it could not place - so one that appears after
    an operation is `new_defect` unless the pre-op list already held that code - or held a
    no-answer, so the before side was never walked (the either-side-timed-out rule) - or
    `_revealed` excuses it. `standing` is those pre-op unlocated codes, read off `previous` unless
    the caller names them: a scoped judgement's `previous` was cut to the region, which drops
    every unlocated finding, so `try_op` reads them off the whole list first. It is still kept out
    of `previous`: it is the part's, not any scope's, so it may not inflate the count the
    "improved to warnings" allowance reads.

    Non-mesh codes are judged exactly as before; with the probe off nothing here has a mesh code,
    so both sides come back as they went in.
    """
    timed_out = any(d.code in mesh_probe.NO_ANSWER_CODES for d in (*previous, *found))
    held = _standing(previous) if standing is None else frozenset(standing)
    unwalked = bool(held & mesh_probe.NO_ANSWER_CODES)

    def keep_before(d) -> bool:
        return not _unlocated_mesh(d) and not (timed_out and _is_mesh(d))

    def keep_after(d) -> bool:
        if timed_out and _is_mesh(d):
            return False
        return not (_unlocated_mesh(d) and (unwalked or d.code in held))
    return (tuple(d for d in previous if keep_before(d)),
            tuple(d for d in found if keep_after(d)))


def _geometric_defect_key(defect) -> tuple | None:
    """Name a finding by its measured geometry when a kernel rebuild renames its entity."""
    location = defect.location or {}
    low, high = location.get("bbox_min"), location.get("bbox_max")
    if not low or not high:
        return None
    return (defect.code, defect.severity, defect.entity_type, tuple(low), tuple(high))


def _same_geometry(left: tuple, right: tuple, *, tol_mm: float) -> bool:
    """Do two geometric keys name the SAME finding, allowing for the kernel's own rounding?

    WHY THIS IS NOT EQUALITY. The key is built from measured floats, and a rebuild re-approximates
    geometry it never touched: on the Formula body that found this, two sliver faces nobody aimed
    at came back with their boxes moved by 0.00037 mm - eight parts in a hundred million of a 4.6 m
    car. Compared exactly, a finding that had been there all along read as brand new, and since the
    warning allowance is disabled while any error is still open, the consequence was that no hole
    fill could ever be kept on a part with more than one error.

    WHY THE OPERATION'S OWN TOLERANCE IS THE RIGHT RULER. It is the distance the operation was
    permitted to move geometry by. Two findings of the same kind whose measured boxes agree inside
    it cannot be distinguished by anything this session is allowed to care about, so treating them
    as one is not a loosening of the check - it is the check stated in the units it is judged in.
    """
    if left[:3] != right[:3]:
        return False
    return all(abs(a - b) <= tol_mm
               for side_left, side_right in ((left[3], right[3]), (left[4], right[4]))
               for a, b in zip(side_left, side_right, strict=False))


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

#: Reason prefix for an operation that judged its aim and ran nothing. Same idiom, and named here
#: rather than spelled out at each reader: the region loop's `_abandoned` has to tell a decline
#: from a failed attempt, and a literal in two modules is a routing split waiting to happen.
DECLINED = "declined:"

#: Reason prefix for a package that passed its predicate and was then REJECTED BY THE SEMANTIC
#: REVIEWER. `reverted`, because a reject undoes the package exactly as a failed predicate does;
#: the prefix says the claim held and the reviewer still refused it.
REVIEW_REJECTED = "review:"

#: THE REASONS A REVIEW REJECT CARRIES IN `PackageVerdict.reason` - GENERIC, ON PURPOSE. `reason`
#: is what the author's next observation shows it (observe._attempt_of), and an author handed the
#: grader's reasoning would learn to word its way past the grader. The reviewer's own words go
#: to `review_reason`, which reaches the report and the reviewer's next evidence, never the
#: author (controller ruling, P3 Task 4 fix round 1).
REVIEW_REJECTED_REASON = f"{REVIEW_REJECTED} rejected by review"
#: THE REASON ON A KEPT VERDICT that came through the mesh-cure door (`RepairSession._mesh_cure`):
#: the claimed target finding REMAINS, and the part - which gmsh refused before - meshes as
#: delivered. Never empty, so a reader can count the keeps that came this way, and never
#: rewritten as "cleared": the unmet claim stays on the verdict (`delta["unmet"]`).
MESHES_AFTER_TARGET_REMAINS = "meshes_after_target_remains"
#: A region's SECOND review reject escalates it to a person: no retry-until-accept.
REVIEW_TWICE_REASON = f"{REVIEW_REJECTED} rejected by review twice"
#: A region with one review reject gets ONE more authored attempt; a package after that is
#: escalated unrun, whatever became of the extra attempt.
REVIEW_SPENT_REASON = (f"{REVIEW_REJECTED} rejected by review, and the one attempt allowed after "
                       "it is spent")


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
    #: `cleared` claims this package did NOT meet, and committed anyway because the operation aimed
    #: at them made the part mesh as delivered (`MESHES_AFTER_TARGET_REMAINS`). REPORTED, never
    #: rewritten as cleared; absent from `to_dict` when empty, so every other verdict is unchanged.
    unmet_cleared: frozenset[str] = frozenset()
    #: Seconds. AGGREGATED from the operations' own `OpVerdict` timings plus the package's
    #: boundary and scoped scans, so the same work is never timed twice.
    scan_s: float = 0.0
    apply_s: float = 0.0
    #: Seconds in `caps.measure`: the package's own two whole-shape measures PLUS its operations'.
    #: REPORTED because it is the second cost centre the spec names beside the scan, and a package
    #: adds two more whole-shape `BRepCheck_Analyzer` walks on top of the two each operation takes.
    #: Without this field the region loop's own measures appeared in no timing at all, so the
    #: instrumentation built to make that cost visible could not see the cost this driver added.
    measure_s: float = 0.0
    #: MEASURED EVIDENCE, NEVER A GATE: the sampled largest distance in mm from the surface this
    #: package changed to THE UPLOAD as repair received it (`cad/repair/deviation.py`). None - and absent
    #: from `to_dict` - when it was not measured: the mesh probe posture is off, the package did
    #: not pass its predicate, or the measurement failed or ran out of its budget. A reviewer
    #: reads it (CAD_REPAIR_PACKAGE_REVIEW), so a package the REVIEWER rejected keeps it too - the
    #: record shows what the verdict was made on; no code branches on it.
    deviation_mm: float | None = None
    #: HOW SPARSE `deviation_mm` IS, so a reader can weigh it. All None when no measurement was
    #: attempted (posture off, not committed). `deviation_faces == 0` with `deviation_mm` absent
    #: is MEASURED-EMPTY - there was nothing to sample - which is not the same as not measured.
    #: `deviation_samples` is points actually measured, `deviation_faces` faces they lie on,
    #: `deviation_faces_skipped` selected faces that had no in-face point, and `deviation_s` the
    #: seconds the measurement took (beside apply_s/scan_s/measure_s).
    deviation_samples: int | None = None
    deviation_faces: int | None = None
    deviation_faces_skipped: int | None = None
    deviation_s: float | None = None
    #: THE SEMANTIC REVIEW (CAD_REPAIR_PACKAGE_REVIEW, cad/repair/package_review.py): "accept" or
    #: "reject", the reviewer's reason (or the named no-answer that made it a reject), and the
    #: seconds the review took on the run's clock. All None - and absent from `to_dict` - when the
    #: posture is off or the package never passed its predicate (the reviewer is consulted for
    #: nothing else). `review_evidence` is the evidence the verdict was made on, clipped to
    #: `package_review.EVIDENCE_RECORD_MAX` bytes under a stated marker.
    reviewed: str | None = None
    review_reason: str | None = None
    review_s: float | None = None
    review_evidence: dict | None = None
    #: WHO ACCEPTED OR REFUSED IT: "reviewer" (the model) or "auto" (the audited auto-accept
    #: tier, cad/repair/review_tier.py). Absent when the review posture is off, so a flag-off
    #: report is byte-for-byte what it is today.
    review_tier: str | None = None
    #: THE MEASURED OBJECTION, and the ONLY thing a review reject may ever hand the author
    #: (cad/repair/package_review.py: `review_package_objection`). A field name from a closed
    #: tuple plus the value the SESSION measured for it - never a word the reviewer wrote.
    #: `review_reason` is the reviewer's own prose and goes to the report only; `observe` carries
    #: this key and not that one, which is the seal the objection design exists for.
    review_objection: dict | None = None

    @property
    def committed(self) -> bool:
        return self.status == COMMITTED

    def to_dict(self) -> dict:
        return {"region_index": int(self.region_index), "status": self.status,
                "reason": self.reason, "verdicts": [v.to_dict() for v in self.verdicts],
                "reclassified": sorted(self.reclassified),
                **({"unmet_cleared": sorted(self.unmet_cleared)} if self.unmet_cleared else {}),
                "scan_s": self.scan_s, "apply_s": self.apply_s,
                "measure_s": self.measure_s,
                **{k: v for k, v in (
                    ("deviation_mm", self.deviation_mm),
                    ("deviation_samples", self.deviation_samples),
                    ("deviation_faces", self.deviation_faces),
                    ("deviation_faces_skipped", self.deviation_faces_skipped),
                    ("deviation_s", self.deviation_s),
                    ("reviewed", self.reviewed),
                    ("review_reason", self.review_reason),
                    ("review_s", self.review_s),
                    ("review_evidence", self.review_evidence),
                    ("review_tier", self.review_tier),
                    ("review_objection", self.review_objection)) if v is not None}}


def _measure_total(own_s: float, verdicts: Iterable[OpVerdict]) -> float:
    """A package's `caps.measure` seconds: its OWN two whole-shape measures plus its operations'.

    The same idiom as `scan_s` and `apply_s` - the package verdict is the one place a reader can
    get the whole cost of a region's work without re-deriving it from the op log, and a package
    adds measures the op log does not contain.
    """
    return float(own_s) + sum(v.measure_s for v in verdicts)


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
                 clock: Callable[[], float] = time.monotonic,
                 original: Measure | None = None, defects: tuple | None = None,
                 is_scratch: bool = False,
                 auto_accept_mm: float | None = None) -> None:
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
        #: How many of those were SCOPED to a region - an operation's own proof or a package's.
        #: Counted here rather than inferred by a caller's arithmetic: the whole cost claim of
        #: the region loop is `scan_count - scoped_scan_count` whole-shape walks, and a count
        #: reconstructed from verdict lengths silently miscounts an operation the caps refused
        #: before its re-inspection was ever paid for.
        self.scoped_scan_count: int = 0
        self.shape = shape
        #: THE UPLOAD'S OWN MEASUREMENT, taken once and never replaced. Every cumulative check is
        #: against this, which is what stops six small operations drifting past a cap together.
        # NORMALLY THE UPLOAD'S OWN WALK. `original` is given only by `scratch` below, which
        # must inherit the real session's measure rather than measure a clone: a clone measured
        # afresh would compute the caps from a shape that is already mid-repair, so exploration
        # would get a wider allowance than the run it is exploring for. Given, the measurer is
        # NOT called at construction - `self._measure` is still the real one, so every
        # per-operation `before`/`after` inside `try_op` is measured for real.
        self.original = measurer(shape) if original is None else original
        #: THE UPLOAD ITSELF, as repair received it: what a package's deviation is measured
        #: against (`_deviation_evidence`), never the shape the package happened to start from -
        #: a run of small packages must not hide how far they moved the part together.
        self._upload = shape
        #: THE SEMANTIC REVIEWER'S REJECTS, per region index, in order (its own reasons). Read
        #: back into the reviewer's evidence and used to cap a region's attempts after a reject.
        self._review_rejects: dict[int, list[str]] = {}
        #: Packages tried in a region SINCE its first review reject (the cap allows one).
        self._after_review_reject: dict[int, int] = {}
        self.accounting = Accounting()
        #: None until scanned. `defects` SEEDS it, which is how `scratch` starts without a
        #: whole-shape scan of its own: the clone is a copy, so the session's list already
        #: describes it entity for entity.
        self._defects: tuple | None = None if defects is None else tuple(defects)
        #: Is this a THROWAWAY CLONE an investigator may edit? A scratch session applies and
        #: rolls back operations exactly as a real one does - that is the point, the rehearsal
        #: has to be faithful - but it can never commit: `try_package`'s first act is to refuse.
        self.is_scratch = bool(is_scratch)
        self.complete_scan: bool = True
        #: Has the mesh probe run on this session yet? Until it has, there is nothing to carry
        #: forward when a probe is skipped (`_with_mesh`).
        self._mesh_probed: bool = False
        self.log: list[OpVerdict] = []
        #: The package-level truth, beside the per-operation `log`. A reader of the log alone
        #: cannot tell a kept operation that shipped from one discarded with its package.
        self.packages: list[PackageVerdict] = []
        #: Fingerprints of the faces every KEPT operation reported adding or rebuilding, in the
        #: order kept, and the shapes those operations took as input (whose diagonals named them)
        #: - what `try_package` hands `deviation.measure_deviation`. LIFECYCLE ASSUMPTION: the
        #: loop is sequential and `try_package` resets both lists before its plan, so entries
        #: from an operation run outside a package (`try_op` directly) are never read, and a
        #: package never sees another's. Appended only on a keep.
        self._kept_faces: list[str] = []
        self._kept_inputs: list = []
        #: THE CUSTOMER'S DEVIATION THRESHOLD, or None when the run declared none. The auto-accept
        #: tier is INERT WITHOUT IT (`review_tier.decide`): a deployment that turned the posture on
        #: but states no threshold still sends every package to the reviewer, which is the
        #: fail-closed direction. None is the default, so a session nobody told is today's session.
        self._auto_accept_mm = None if auto_accept_mm is None else float(auto_accept_mm)

    @classmethod
    def scratch(cls, shape, *, original: Measure, defects: tuple,
                reserved: frozenset = frozenset(),
                remaining_s: Callable[[], float] | None = None,
                clock: Callable[[], float] = time.monotonic,
                measurer=_measure, scanner=None) -> RepairSession:
        """A session over a THROWAWAY SHAPE, for rehearsing edits that must never ship.

        It is the real session - the same `try_op`, the same snapshot, the same caps, the same
        declaration accounting - over a shape the caller has already copied. That faithfulness is
        the whole point: an edit rehearsed under weaker rules teaches nothing about the edit that
        would actually be attempted. What it cannot do is COMMIT: `try_package` refuses on it, so
        no scratch session can review a package or carry one into a report.

        IT COSTS NOTHING TO OPEN. Neither the measurer nor the scanner is called here:

          * `original` is the UPLOAD'S measure, handed in, so every cap compares against the same
            numbers the real run compares against and exploration is capped the way the run is. A
            clone measured afresh would be measured mid-repair and quietly widen every allowance.
          * `defects` is the session's current list, which already describes this shape entity for
            entity (a copy keeps structure and indices), so there is no first whole-shape scan.

        The caller - never this method - makes the copy, because what counts as "the shape to
        copy" is the caller's business (`investigator_tools.Scratch` copies with
        `operations._copied`).
        """
        return cls(shape, measurer=measurer, scanner=scanner, remaining_s=remaining_s,
                   reserved=reserved, clock=clock, original=original, defects=tuple(defects),
                   is_scratch=True)

    def _default_scan(self, shape, *, face_scan: bool = True, within: Region | None = None,
                      final: bool = False):
        """`localize_scan`, with a face-level scan only when the self-intersection heal is on.

        `_localize_scan` is looked up AT CALL TIME, so a test patching the module attribute is
        obeyed. With the flag off the budget is 0.0 and `remaining_s` is never even asked, which
        is what keeps a flag-off run's scans byte-for-byte what they were. `face_scan=False` is
        the status-walk re-inspection (`_rescan`): budget 0.0 there too, whatever the flag.

        `within=None` IS THE WHOLE-SHAPE SCAN THIS HAS ALWAYS BEEN, and it is made without even a
        `within=None`, because the committed baseline is what a difference between the two drivers
        is attributed against and a test that watches this call (`test_repair_face_scan`) is
        entitled to see no trace of a scoping feature an unscoped run does not use.

        `quantum_diagonal_mm` IS ON EVERY SCAN THIS SESSION TAKES, scoped or not, and is the one
        argument that is not optional: it is the UPLOAD's diagonal, so every scan in the run names
        its entities on ONE grid. Named at the CURRENT shape's diagonal instead, a repair that
        moved the part's extent renamed every entity on the part - including entities nobody
        touched - and `_progress_refusal` then could not find the defect it had just named and
        kept an operation that had repaired nothing
        (docs/superpowers/specs/2026-11-19-target-remains-diagnosis.md). An entity's name must be
        a fact about the entity, not about what else happened to the part.
        """
        budget = 0.0
        if face_scan and rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS:
            remaining = self.remaining_s or (lambda: float(rcfg.CAD_REPAIR_TIMEOUT_S))
            budget = face_scan_budget_s(remaining())
        if within is None:
            found, complete = _localize_scan(shape, face_scan_s=budget,
                                             quantum_diagonal_mm=self.original.diagonal_mm)
        else:
            # THE UPLOAD'S DIAGONAL GROWS THE REGION'S BOX, the same number `scoped_to` and
            # `in_region` are handed below, so the scan's scope and the filters compared against
            # it are one box. The current shape's diagonal would drift as the repair reshapes the
            # part.
            found, complete = _localize_scan(shape, face_scan_s=budget, within=within,
                                             scope_diagonal_mm=self.original.diagonal_mm,
                                             quantum_diagonal_mm=self.original.diagonal_mm)
        return self._with_mesh(found, shape, within=within, final=final), complete

    def _with_mesh(self, found, shape, *, within: Region | None, cap_s: float | None = None,
                   final: bool = False):
        """`found`, followed by why gmsh will not mesh `shape` when CAD_REPAIR_MESH_PROBE is live.

        OFF RETURNS `found` ITSELF, untouched, and `mesh_probe` is never called - the flag-off
        scan is byte-for-byte what it was. Live, the probe's defects are APPENDED after the kernel
        scan's, never handed into it, so `localize_scan` is called exactly as it always was.

        ONE PLACE FOR EVERY DEFAULT SCAN PATH: the first scan and the per-operation re-inspection
        (both through `_default_scan`, whatever `face_scan` is, so an operation aimed at a mesh
        defect is judged by the progress rule on a rescan that can still see it) and a package's
        scoped proof (`_region_scan`). A scoped probe surface-meshes the region's faces only, so
        volume-mesh failures are seen by whole-shape scans alone (mesh_probe.mesh_defects) - and
        so a located one the session already holds in the region is appended to a scoped result
        unchanged, never read as cleared by a probe that could not see it (ruling R11a).

        AN INJECTED SCANNER NEVER REACHES THIS: it is the whole truth for the test that injected
        it, and a mesh defect added on top would change what that test asserts about.

        COMPLETENESS IS NOT TOUCHED. The probe never raises; a probe that could not finish is a
        `mesh_timeout` defect on the list, not a stopped kernel walk, so it does not lower
        `complete_scan`.

        The budget is `mesh_probe_budget_s` of what is left before the run's deadline - the lesser
        of CAD_REPAIR_MESH_PROBE_TIMEOUT_S and half the time left, or for the run's `final`
        whole-shape scan everything left but `FINAL_PROBE_SLACK_S` - and, for a package's proof, no more than the package
        has left (`cap_s`); a non-positive (or NaN) budget skips the probe. A session nobody gave
        a clock (`remaining_s` None) gives the probe its own timeout. The diagonal is the
        upload's, as every scope in this session is.

        ABSENCE IS NEVER CLAIMED FOR GROUND THE PROBE DID NOT WALK. A skipped probe, or one that
        came back with nothing but a `mesh_timeout`, is not evidence the part now meshes - and the
        result REPLACES the session's list, so returning `found` bare would erase a mesh error and
        let the run's final scan (taken exactly when time has run out) call the part resolved. So
        the mesh findings the session already holds for this scope are CARRIED FORWARD (`_carried`);
        a session that has never probed has none, and gets one unlocated `mesh_timeout` instead.
        """
        if rcfg.CAD_REPAIR_MESH_PROBE != "live":
            return found
        timeout = float(rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S)
        remaining = float(self.remaining_s() if self.remaining_s is not None else timeout)
        budget = (mesh_probe_budget_s(remaining, final=final) if self.remaining_s is not None
                  else timeout)
        if cap_s is not None:
            # A PACKAGE'S PROOF IS CAPPED BY WHAT THE PACKAGE HAS LEFT (`try_package`).
            budget = min(budget, float(cap_s))
        # `remaining` is checked as well as `budget`: `min` would pass a NaN over for the timeout.
        if not (remaining > 0.0 and budget > 0.0):
            if not self._mesh_probed:
                return (*found, mesh_probe._unlocated(mesh_probe.MeshFailure(  # noqa: SLF001
                    mesh_probe.TIMEOUT, (), None,
                    "the mesh probe was skipped: no time was left to run it")))
            return (*found, *self._carried(within))
        self._mesh_probed = True
        # Looked up on the module AT CALL TIME, so a test patching `mesh_probe.mesh_defects` is
        # obeyed.
        probed = tuple(mesh_probe.mesh_defects(shape, diagonal_mm=self.original.diagonal_mm,
                                               budget_s=budget, within=within))
        if probed and all(d.code in mesh_probe.NO_ANSWER_CODES for d in probed):
            # The probe ran out of time: report that, and keep what it knew before.
            carried = tuple(d for d in self._carried(within)
                            if d.code not in mesh_probe.NO_ANSWER_CODES)
            return (*found, *carried, *probed)
        if within is not None:
            # A SCOPED PROBE IS 2D-ONLY, so a located volume-stage finding in the region was not
            # looked for, and its absence is not a repair (ruling R11a). It stands as it was -
            # in the scan the progress rule and the package's predicate are judged on - until a
            # whole-shape probe says otherwise.
            present = {(d.fingerprint, d.code) for d in probed}
            blind = tuple(d for d in self._carried(within)
                          if mesh_probe.volume_stage(d) and (d.fingerprint, d.code) not in present)
            return (*found, *probed, *blind)
        return (*found, *probed)

    def _carried(self, within: Region | None) -> tuple:
        """The mesh findings the session holds for one scope, to stand in for a probe not taken.

        Whole shape: every one. Scoped: the LOCATED ones in the region - an unlocated mesh finding
        is about the whole part, which `_merged` carries forward on its own.
        """
        held = [d for d in (self._defects or ()) if _is_mesh(d)]
        if within is None:
            return tuple(held)
        return tuple(d for d in held if not _unlocated_mesh(d)
                     and in_region(d, within, diagonal_mm=self.original.diagonal_mm))

    def _mesh_cure(self, whole_before, found, *, within: Region | None,
                   cap_s: float | None = None) -> tuple[str, ...]:
        """The mesh failures the part held before an operation and no longer holds, when the
        operation made a part that gmsh REFUSED mesh. Empty otherwise. NEVER claims a cure unproved.

        WHY THIS EXISTS (lane E, measured on the T6 corpus): the progress rule asks whether the
        target's own finding is still at the target. A kernel WARNING aimed at - a short edge, a
        small face - often survives the very operation that makes the part mesh: on abc
        0052_00525324 `fix_small_edges` left its small_edge at the target's box and gmsh, which
        refused the part before ("Two facets intersect"), meshed it after. That was rolled back
        `target_remains`. The defect a repair exists to clear is the part not meshing, so a
        whole-part probe that now meshes the part is the stronger evidence, not the weaker.

        ALL OF THESE MUST HOLD, each one a safety property:
          - the mesh probe is live and the scan is not injected (an injected scanner is the
            whole truth for the test that injected it);
          - the WHOLE pre-op list held a mesh failure that is an ANSWER (not a timeout or a dead
            probe). A part that meshed before, or was never shown to fail, cannot be cured - so a
            clean part is never altered by this route;
          - a WHOLE-SHAPE probe of the current shape AS DELIVERED - written to STEP and read
            back (`_meshes_now`) - found nothing at all, inside its budget. A scoped probe is
            2D-only and proves nothing about the volume (ruling R11a); a timeout is no answer.
        The caller still runs the rest of the progress rule, so new damage still rolls back.
        """
        if rcfg.CAD_REPAIR_MESH_PROBE != "live" or self._injected:
            return ()
        failures = sorted({d.code.value for d in whole_before
                           if _is_mesh(d) and d.code not in mesh_probe.NO_ANSWER_CODES})
        if not failures:
            return ()
        if within is None and any(_is_mesh(d) and d.code not in mesh_probe.NO_ANSWER_CODES
                                  for d in found):
            # The re-inspection was whole-shape and its probe still failed the part: no cure,
            # and no second probe paid to learn it.
            return ()
        return tuple(failures) if self._meshes_now(cap_s=cap_s) else ()

    def _meshes_now(self, *, cap_s: float | None = None) -> bool:
        """Does a WHOLE-SHAPE gmsh probe of the current shape AS DELIVERED mesh it, in budget?

        AS DELIVERED, NOT AS HELD (`_as_delivered`): the part a customer meshes is the repaired
        STEP, and STEP carries no per-entity tolerance. Measured on abc 0052_00525324:
        `fix_small_edges` cures the part by widening tolerances, so the in-memory shape and its
        BREP round-trip mesh 3 times out of 3 and its STEP round-trip fails ("A segment and a
        facet intersect"). A cure that does not survive the export is not a repair, and a keep
        resting on the in-memory probe would promote a file that still does not mesh.
        """
        timeout = float(rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S)
        remaining = float(self.remaining_s() if self.remaining_s is not None else timeout)
        budget = mesh_probe_budget_s(remaining) if self.remaining_s is not None else timeout
        if cap_s is not None:
            budget = min(budget, float(cap_s))
        if not (remaining > 0.0 and budget > 0.0):
            return False
        delivered = _as_delivered(self.shape)
        if delivered is None:
            return False
        self._mesh_probed = True
        probed = tuple(mesh_probe.mesh_defects(delivered, diagonal_mm=self.original.diagonal_mm,
                                               budget_s=budget))
        return not probed

    def defects(self) -> tuple:
        """The current shape's located defects. Scanned once per shape and cached."""
        if self._defects is None:
            self.scan_count += 1
            found, complete = self._scan(self.shape)
            self._defects = tuple(found)
            self.complete_scan = bool(complete)
        return self._defects

    def _rescan(self, *, face_scan: bool, within: Region | None = None,
                final: bool = False) -> tuple:
        """Re-inspect the current shape for a keep/rollback judgement, and cache the result.

        SCAN PLACEMENT (floor-fix ruling 1). The face scan runs for the session's first scan and
        for the re-inspection that judges a heal; every other operation is judged on the status
        walk alone, exactly as before the analyzer existed. Without this a kept sew was rolled
        back `new_defect` for a crossing the flag-off world never measured (trj4_k1_geo-pe-*).
        An injected scanner is still called as `scanner(shape)`; its analyzer-only findings are
        dropped here instead, so a double cannot reintroduce the demotion either.

        `within` SCOPES THE RE-INSPECTION TO ONE REGION, and `None` - the default - is the
        whole-shape re-inspection this has always been, entity for entity and in the same order.
        That default is load-bearing: the operation-at-a-time loop is the retained benchmark
        baseline, so a difference between the two drivers has to be attributable to how the work
        was chosen rather than to a scan this one quietly stopped taking.

        WHAT IS RETURNED IS THE SCAN'S OWN FINDINGS, not the session's list, because the progress
        rule is judged on the scope that was walked. The session's list is merged (`_merged`): a
        scoped scan may replace only what it looked at.

        AN INJECTED SCANNER IS NEVER TOLD ABOUT THE SCOPE - it is called as `scanner(shape)`, as
        it always was - so under one its findings are filtered after the fact, the same idiom
        `localize_scan` uses for the face analyzer, which has no scoped form either.

        `final` marks the run's last whole-shape verification, whose mesh probe may spend all the
        time left rather than half of it (`mesh_probe_budget_s`).
        """
        self.scan_count += 1
        if within is not None:
            self.scoped_scan_count += 1
        if self._injected:
            found, complete = self._scan(self.shape)
            if within is not None:
                found = scoped_to(found, within, diagonal_mm=self.original.diagonal_mm)
        else:
            found, complete = self._default_scan(self.shape, face_scan=face_scan, within=within,
                                                 final=final)
        if not face_scan:
            found = [d for d in found if not analyzer_only(d)]
        found = tuple(found)
        if within is None:
            self._defects = found
            self.complete_scan = bool(complete)
        else:
            self._defects = self._merged(found, within)
            # A SCOPED SCAN'S COMPLETENESS IS ABOUT ITS REGION ONLY, so a True here says nothing
            # about the part and may never raise the session's flag; a False means the region's
            # own walk stopped, and since that short list is what the merge believes about the
            # region from now on, it drags the whole-shape flag down with it. False dominates, as
            # it does everywhere else.
            self.complete_scan = bool(self.complete_scan) and bool(complete)
        return found

    def _merged(self, found: tuple, within: Region) -> tuple:
        """The session's whole-shape defect list with ONE region's findings replaced.

        A SCOPED SCAN MAY REPLACE ONLY WHAT IT LOOKED AT. `self._defects` is read as a statement
        about the whole shape, so letting a region's findings become the whole list would claim
        the rest of the part had been re-checked and found sound - the same reason `_region_scan`
        keeps the package's proof out of the list entirely.

        SO WHAT IS OUTSIDE THE REGION IS CARRIED FORWARD FROM THE LAST WHOLE-SHAPE SCAN, and that
        is knowingly stale: an operation can legitimately alter geometry elsewhere (the sliver
        sweep is a whole-shape walk), and after a scoped proof nobody has looked. Stale in the
        CONSERVATIVE direction - a defect that has since been repaired is still reported, and no
        absence is ever claimed for ground that was not walked - which is the price of locality
        and is paid off by the run's one final whole-shape scan.

        Ordered as a scan orders its findings, so two identical runs still produce identical
        reports.
        """
        present = {(d.fingerprint, d.code) for d in found}
        # AN UNLOCATED MESH FINDING IS ABOUT THE WHOLE PART, so no region's scan replaces it.
        # `in_region` admits anything unplaceable, and a scoped probe meshes surfaces only, so
        # without this a `mesh_no_volume` would vanish from the list after any scoped rescan.
        # A LOCATED VOLUME-STAGE ONE is carried for the same reason (ruling R11a): the scoped
        # probe never built the volume it fails in (`_scope_blind`).
        outside = [d for d in (self._defects or ())
                   if (_scope_blind(d)
                       or not in_region(d, within, diagonal_mm=self.original.diagonal_mm))
                   and (d.fingerprint, d.code) not in present]
        return tuple(sorted((*outside, *found),
                            key=lambda d: (d.entity_type, d.entity_index, d.code.value)))

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
               tolerance_mm: float, answered_held: frozenset[str] = frozenset(),
               within: Region | None = None) -> OpVerdict:
        """`_try_op` with every name it makes - the operation's own included - on the RUN's grid.

        Scans already name at the upload's diagonal; this pins the operation's `added`/`rebuilt`
        names and the pre-op face names to the same grid (`identity.run_grid`), so a repair that
        moved the part across a power of two cannot make an op's name and a scan's name for one
        face disagree (PR #494 review, session.py:611).
        """
        with run_grid(self.original.diagonal_mm):
            return self._try_op(op, targets, defect_code, tolerance_mm=tolerance_mm,
                                answered_held=answered_held, within=within)

    def _try_op(self, op, targets: tuple[EntityRef, ...], defect_code, *,
                tolerance_mm: float, answered_held: frozenset[str] = frozenset(),
                within: Region | None = None) -> OpVerdict:
        """Apply one operation and keep it only if it repaired what it aimed at.

        `within` IS WHERE "LOCAL EDIT, LOCAL PROOF" IS ACTUALLY PAID FOR. Step 5 is the expensive
        step, and under a region it re-inspects that region instead of the part: a package of N
        operations then costs N scoped scans plus its own one, where it used to cost N whole-shape
        walks plus a scoped scan ON TOP - which made the region loop dearer than the baseline it
        exists to beat. `None` is the whole-shape re-inspection, unchanged, which is what the
        operation-at-a-time loop still pays.

        THE RESCAN IS SCOPED, NEVER SKIPPED. Step 6 needs positive evidence that the defect aimed
        at actually went (see `target_absent` below); without a rescan the rule is vacuous and an
        operation is kept for doing nothing.

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
                reason=f"{DECLINED}{str(declined)[:200]}", evidence=dict(outcome.evidence),
                delta=delta,
                apply_s=apply_s, measure_s=measure_s))

        previous = self.defects()
        # THE WHOLE PRE-OP LIST, before a scope cuts it: whether the part failed gmsh at all is a
        # fact about the part, read by `_mesh_cure` below.
        whole_before = previous
        # THE UNLOCATED MESH EVIDENCE THE PART HELD BEFORE THE OPERATION, read off the WHOLE list
        # before a scope cuts it: a whole-part finding still there after the op is not news,
        # whichever scope the re-inspection walked (`_mesh_judged`).
        standing = _standing(previous)
        # THE HEAL IS JUDGED ON THE FACE SCAN, so `crossing_remains`/`new_defect` still police it;
        # everything else on the status walk alone, against a `previous` without the analyzer's
        # findings, which is the comparison the flag-off run makes.
        judged_by_face_scan = (op.name == HEAL_OP
                               or code == DefectCode.self_intersection.value)
        scan_started = self.clock()
        found = self._rescan(face_scan=judged_by_face_scan, within=within)
        scan_s = self.clock() - scan_started
        if within is not None:
            # BOTH SIDES OF THE PROGRESS RULE MUST BE DRAWN FROM ONE SCOPE. `previous` is the
            # cached WHOLE-SHAPE list and `found` has just been scoped to this region, so left
            # unfiltered every defect elsewhere in the part reads as having disappeared. That
            # credits this operation with clearing geometry it never touched, and the credit is
            # not cosmetic: the rule's "the part got better overall" allowances below turn on
            # `len(found) < len(previous)`, so an inflated `previous` excuses a warning the
            # operation introduced into the very region it is being proved against. Filtered
            # through the same admission rule the scan itself applies (`localize.scoped_to`), so
            # the two sides cannot drift apart.
            # An unlocated mesh finding is the whole part's, not this region's: left in, it is an
            # error `found` can never hold, and `improved_to_warnings` would excuse new warnings.
            # A LOCATED volume-stage one stays IN: `_with_mesh` carries it into `found` too, so
            # it is on both sides - never new, never cleared, and an operation aimed at it reads
            # `target_remains` (ruling R11a: only a whole-shape probe may clear it).
            previous =tuple(d for d in scoped_to(previous, within,
                                                  diagonal_mm=self.original.diagonal_mm)
                             if not _unlocated_mesh(d))
        if not judged_by_face_scan:
            carried = tuple(d for d in previous if analyzer_only(d))
            previous = tuple(d for d in previous if not analyzer_only(d))

        basis = self._basis(outcome, targets)
        revealed = _revealed(op, outcome, targets, defect_code, previous, found,
                             pre_op_names=lambda: _face_names_of(snapshot[0]),
                             standing=standing)
        reason = self._progress_refusal(
            outcome, targets, defect_code, previous, found, op_name=op.name,
            revealed=frozenset((d.fingerprint, d.code.value) for d in revealed),
            standing=standing, tolerance_mm=tolerance_mm)
        cured: tuple[str, ...] = ()
        if reason == "target_remains" and not _same_shape(snapshot[0], self.shape):
            # AN OPERATION THAT CHANGED NOTHING IS NEVER CURED: a STEP round-trip alone can move
            # gmsh's answer, and that is the export's doing, not the operation's.
            cured = self._mesh_cure(whole_before, found, within=within)
            if cured:
                # THE DEFECT WAS THE PART NOT MESHING, AND IT MESHES NOW. Everything else the rule
                # polices is still asked: the same rule with no target to look for, so a new
                # defect, an unlocated mesh failure of the aimed code, or damage still rolls back.
                reason = self._progress_refusal(
                    outcome, (), defect_code, previous, found, op_name=op.name,
                    revealed=frozenset((d.fingerprint, d.code.value) for d in revealed),
                    standing=standing, tolerance_mm=tolerance_mm)
        if reason:
            self.shape, self._defects, self.complete_scan = snapshot
            if reason == "target_remains":
                # WHAT THE ROLLED-BACK RESULT DID CLEAR. `target_remains` is a verdict on the AIM,
                # not on the call: a whole-shape operation re-aimed at one of these makes the
                # identical call and would pass, so the planner must not suppress that aim
                # (`plan.RunHistory.mark_whole_shape_cleared`, PR #494 review, plan.py:401).
                still = {(d.fingerprint, d.code.value) for d in found}
                delta["cleared"] = sorted({d.fingerprint for d in previous if d.fingerprint
                                           and (d.fingerprint, d.code.value) not in still})
            return self._record(OpVerdict(
                op=op.name, targets=fingerprints, defect_code=code, status=ROLLED_BACK,
                reason=reason, basis=basis, evidence=dict(outcome.evidence), delta=delta,
                apply_s=apply_s, scan_s=scan_s, measure_s=measure_s))
        if cured:
            # KEPT BECAUSE THE PART NOW MESHES, not because the target's finding went: it stays on
            # the list (it is still there), and the mesh failures the whole-shape probe just
            # disproved leave it, so the run does not go on answering a failure that is gone.
            delta["mesh_cured"] = list(cured)
            delta["unmet"] = sorted(fingerprints)
            self._defects = tuple(d for d in (self._defects or ()) if not _is_mesh(d))
        if revealed:
            # ONLY ON A KEPT VERDICT: a rolled-back one never put these on the list, and the
            # training log must not say it revealed what nobody now holds.
            delta["revealed"] = [{"fingerprint": d.fingerprint, "code": d.code.value}
                                 for d in revealed]

        if not judged_by_face_scan and carried:
            # THE ANALYZER'S FINDINGS STAY ON THE LIST after a kept operation that did not re-run
            # the face scan: they still target the heal and still reach `defects_remaining`, and
            # the heal's own re-inspection re-measures them. Marked as not re-scanned since, so
            # nobody reads them as a fresh measurement of the changed shape.
            from dataclasses import replace

            # ADDED TO THE SESSION'S LIST, which is `found` itself on a whole-shape re-inspection
            # and the merge of this region's findings into the part's on a scoped one. Under a
            # scope `carried` holds only the region's analyzer findings, because `previous` was
            # filtered to the region above - the part's others are already in the merge, carried
            # by `_merged` rather than re-added here.
            listed = self._defects or ()
            present = {(d.fingerprint, d.code) for d in listed}
            self._defects = (*listed, *(
                replace(d, measurements={**d.measurements, "not_rescanned_after": op.name})
                for d in carried if (d.fingerprint, d.code) not in present))

        self.accounting = self.accounting.plus(outcome.evidence)
        self._kept_faces.extend((*outcome.added, *outcome.rebuilt))
        self._kept_inputs.append(snapshot[0])
        return self._record(OpVerdict(
            op=op.name, targets=fingerprints, defect_code=code, status=KEPT, basis=basis,
            reason=MESHES_AFTER_TARGET_REMAINS if cured else "",
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
                                    per-operation rollback still applies, operation by operation,
                                    and each one proved against THIS REGION (`try_op`'s `within`)
          5. one scoped scan      - EXPENSIVE, so it is paid for last, and only ONE for the whole
                                    plan: the plan's own proof, on top of the operations' scoped
                                    ones, which is the whole extra cost of packaging a region
          6. `package.evaluate`   - the claim against the measurements, told how many operations
                                    survived and what they declared (`package.evaluate` judges
                                    movement net of the declarations, as `caps._accounted` does,
                                    and refuses a `cleared` claim from a plan that kept nothing)
          7. commit, or restore the BOUNDARY snapshot
          8. with CAD_REPAIR_PACKAGE_REVIEW live, between a held claim and its commit: the
             semantic reviewer (`_review`), whose reject - or no-answer - is step 7's restore

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
        if self.is_scratch:
            # THE ONE THING A SCRATCH SESSION MAY NOT DO. Everything above this line is a
            # rehearsal on a clone; committing is what makes an edit real - it reviews, it
            # accounts and it hands a shape onward - so it is refused BEFORE the predicate is
            # even read, with nothing applied and nothing reviewed. `reverted` with a `refused:`
            # prefix because nothing ran, exactly as a malformed package is reported.
            return self._record_package(PackageVerdict(
                region_index=package.region.index, status=REVERTED,
                reason=f"{REFUSED} a scratch session cannot commit"))

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

        rejects = self._review_rejects.get(region.index, [])
        if rejects:
            # NO RETRY-UNTIL-ACCEPT. One review reject buys one more authored attempt; past that
            # the region goes to a person, unrun - the existing escalation route.
            if len(rejects) >= 2 or self._after_review_reject.get(region.index, 0) >= 1:
                return self._record_package(PackageVerdict(
                    region_index=region.index, status=ESCALATED,
                    reason=REVIEW_TWICE_REASON if len(rejects) >= 2 else REVIEW_SPENT_REASON))
            self._after_review_reject[region.index] = (
                self._after_review_reject.get(region.index, 0) + 1)

        if len(package.plan) > package.budget.max_ops:
            return self._record_package(PackageVerdict(
                region_index=region.index, status=ESCALATED,
                reason=f"budget: the plan's {len(package.plan)} operation(s) exceed the "
                       f"package budget of {package.budget.max_ops}"))

        started = self.clock()
        # WHOLE-SHAPE AND UNSCOPED, both of these `caps.measure` calls. Each is a
        # `BRepCheck_Analyzer` over the part plus its area, volume and counts, so a package pays
        # the part's price twice for a local edit, on top of the two every operation inside it
        # takes. SCOPING `caps.measure` ITSELF IS DEFERRED to its own plan - it changes what every
        # cap compares against, which is a larger change than this loop. What is owed until then
        # is that the cost is MEASURED rather than invisible, hence `_timed_measure` and
        # `PackageVerdict.measure_s`.
        before, measure_s = self._timed_measure(self.shape)

        scan_started = self.clock()
        # THE WHOLE-SHAPE LIST, deliberately, and normally already cached and so free. The
        # predicate may only name entities in the region (checked above), so every comparison
        # `evaluate` makes against it is a comparison about this region; and reading the cached
        # whole-shape list avoids a second scan to learn what is already known.
        boundary = self.defects()
        defects_before = tuple(d.fingerprint for d in boundary)
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
        # A VOLUME-STAGE ANSWER IS PROVED ON THE WHOLE SHAPE (`_judged_whole`): every operation's
        # re-inspection and the package's own proof below. The predicate still names only this
        # region's entities (`_package_refusal`), so a whole-shape `defects_after` can only see
        # MORE than a scoped one - the conservative direction for every check `evaluate` makes.
        whole = _judged_whole(package)
        scope = None if whole else region
        self._kept_faces = []
        self._kept_inputs = []

        for item in package.plan:
            spent = self.clock() - started
            if spent > package.budget.timeout_s:
                self.shape, self._defects, self.complete_scan, self.accounting = snapshot
                return self._record_package(PackageVerdict(
                    region_index=region.index, status=ESCALATED,
                    reason=f"budget: {spent:.6g}s of {package.budget.timeout_s:.6g}s spent after "
                           f"{len(verdicts)} of {len(package.plan)} operation(s)",
                    verdicts=tuple(verdicts), scan_s=scan_s, apply_s=apply_s,
                    measure_s=_measure_total(measure_s, verdicts)))
            # AN OPERATION THAT ROLLED ITSELF BACK DOES NOT END THE PACKAGE. Its own judgements
            # already undid it, and the predicate - written about the whole plan - is what says
            # whether the plan as a whole did what was claimed.
            # THE REGION TRAVELS INTO EVERY OPERATION, so each one is proved against the region
            # it was authored for rather than against the part. The package's own scan below is
            # then the only scan this plan adds to what an operation already costs.
            verdict = self.try_op(
                item["op"], tuple(item["targets"]), item["defect_code"],
                tolerance_mm=float(item.get("tolerance_mm", 0.0)), within=scope)
            verdicts.append(verdict)
            apply_s += verdict.apply_s
            scan_s += verdict.scan_s

        # THE PACKAGE'S CLOCK IS RE-READ BEFORE ITS PROOF IS PAID FOR. The loop above checks it only
        # BEFORE each operation, so a last operation (with its own probe) could spend the budget
        # and the verification probe would then spend a full probe budget on top. Exhausted here is
        # the same escalation as exhausted mid-plan; otherwise the proof's probe is capped by what
        # the package has left (`_with_mesh` still also caps it by the run's remaining time).
        spent = self.clock() - started
        if spent > package.budget.timeout_s:
            self.shape, self._defects, self.complete_scan, self.accounting = snapshot
            return self._record_package(PackageVerdict(
                region_index=region.index, status=ESCALATED,
                reason=f"budget: {spent:.6g}s of {package.budget.timeout_s:.6g}s spent after "
                       f"{len(verdicts)} of {len(package.plan)} operation(s), before the "
                       "package's proof",
                verdicts=tuple(verdicts), scan_s=scan_s, apply_s=apply_s,
                measure_s=_measure_total(measure_s, verdicts)))
        scan_started = self.clock()
        found, complete = self._region_scan(region, whole_shape=whole,
                                            cap_s=package.budget.timeout_s - spent)
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
                scan_s=scan_s, apply_s=apply_s,
                measure_s=_measure_total(measure_s, verdicts)))

        # THE SECOND WHOLE-SHAPE, UNSCOPED `caps.measure` OF THIS PACKAGE, timed for the same
        # reason as the first above, and scoped to the region no more than it is: scoping it is
        # deferred to its own plan.
        #
        # `evaluate` IS HANDED A WHOLE-SHAPE `defects_before` AND A SCOPED `defects_after`, AND
        # THAT IS SAFE ONLY BECAUSE `_package_refusal` REFUSES A PREDICATE NAMING ANYTHING OUTSIDE
        # `region.fingerprints`. Every set `evaluate` intersects with those two lists is therefore
        # a set of in-region names, so the whole-shape side contributes nothing the scoped side
        # could not have reported. RELAX THE LOCUS RULE AND THIS BREAKS SILENTLY: an out-of-region
        # name in `cleared` would be absent from the scoped `defects_after` and read as cleared by
        # a scan that never looked at it. Any change to the locus rule has to come back here.
        after, after_measure_s = self._timed_measure(self.shape)
        measure_s += after_measure_s
        # WHAT THIS PACKAGE'S SURVIVING OPERATIONS DECLARED, summed exactly as the run's own
        # accounting sums it, and built from the verdicts rather than read off `self.accounting`:
        # the predicate is a claim about THIS package, so what it is judged net of has to be this
        # package's declarations and not the run's. Only KEPT operations are counted - a rolled-
        # back one added nothing to the shape, so it may not buy the package any allowance.
        declared = Accounting()
        kept_ops = 0
        for op_verdict in verdicts:
            if op_verdict.kept:
                kept_ops += 1
                declared = declared.plus(op_verdict.evidence)
        judged = predicate
        unmet: frozenset[str] = frozenset()
        cured = frozenset(name for v in verdicts if v.kept and v.delta.get("mesh_cured")
                          for name in v.targets) & frozenset(predicate.cleared)
        if cured and self._meshes_now(cap_s=package.budget.timeout_s - spent):
            # A CLAIM TO HAVE CLEARED A TARGET WHOSE OPERATION CURED THE PART (`_mesh_cure`) is
            # held to the defect, not the finding: re-proved HERE by a whole-shape probe of the
            # package's own result, since a later operation of the plan could have undone it.
            # Every other check `evaluate` makes - unchanged, area, volume, nothing kept - stands.
            unmet = cured & frozenset(d.fingerprint for d in found)
            judged = replace(predicate, cleared=frozenset(predicate.cleared) - cured)
        reason = evaluate(judged, before=before, after=after,
                           defects_before=defects_before, defects_after=defects_after,
                           kept_ops=kept_ops,
                           declared_area_mm2=declared.declared_area_mm2,
                           declared_volume_mm3=declared.declared_volume_mm3)
        if reason:
            self.shape, self._defects, self.complete_scan, self.accounting = snapshot
            return self._record_package(PackageVerdict(
                region_index=region.index, status=REVERTED, reason=reason,
                verdicts=tuple(verdicts), reclassified=reclassified,
                scan_s=scan_s, apply_s=apply_s,
                measure_s=_measure_total(measure_s, verdicts)))

        deviation, deviation_status = self._deviation_evidence(
            cap_s=package.budget.timeout_s - (self.clock() - started))
        review: dict = {}
        if package_review.posture() == "live":
            # 8. THE SEMANTIC REVIEW, AFTER THE CLAIM HELD AND BEFORE THE COMMIT IS DURABLE. A
            # separate model reads the measured evidence (the author's intent only as a claim)
            # and a reject - or any no-answer - takes the failed predicate's revert below.
            review_started = self.clock()
            try:
                # 8a. THE TIER. A small, fully accounted, non-vertex-moving package under the
                # customer's threshold is accepted here, AUDITED and with no model call; anything
                # else - and anything the tier could not read - goes to the reviewer. The decision
                # is made on the same measured evidence the reviewer would have been sent.
                # WHAT THE KEPT OPERATIONS REVEALED, read off the delta `try_op` recorded (the
                # only place it survives): a finding that appeared under the repair and was
                # excused as revealed rather than as damage. It is excused, not absent - so it is
                # exactly the judgement call the reviewer's seat exists for.
                revealed: frozenset = frozenset()
                for kept_verdict in verdicts:
                    if kept_verdict.kept:
                        revealed |= frozenset(
                            str(entry.get("fingerprint", ""))
                            for entry in kept_verdict.delta.get("revealed", ()))
                tier, tier_reason = review_tier.decide(
                    verdicts=verdicts, reclassified=reclassified, revealed=revealed,
                    declared_matches=review_tier.declaration_covers(
                        before=before, after=after,
                        declared_area_mm2=declared.declared_area_mm2,
                        declared_volume_mm3=declared.declared_volume_mm3),
                    deviation=deviation, deviation_status=deviation_status,
                    threshold_mm=self._auto_accept_mm)
                review = self._review(package, verdicts, boundary, found, whole=whole,
                                      before=before, after=after, declared=declared,
                                      deviation={**deviation,
                                                 "deviation_status": deviation_status},
                                      tier=tier, tier_reason=tier_reason)
            except Exception as exc:  # noqa: BLE001 - a broken review is a reject, never a commit
                logger.warning("session: package review failed (%s)", type(exc).__name__)
                review = {"reviewed": package_review.REJECT,
                          "review_reason": f"review failed: {type(exc).__name__}",
                          "review_s": self.clock() - review_started,
                          "review_tier": review_tier.REVIEWER}
            if review["reviewed"] != package_review.ACCEPT:
                self.shape, self._defects, self.complete_scan, self.accounting = snapshot
                rejects = self._review_rejects.setdefault(region.index, [])
                rejects.append(str(review["review_reason"]))
                twice = len(rejects) >= 2
                return self._record_package(PackageVerdict(
                    region_index=region.index, status=ESCALATED if twice else REVERTED,
                    reason=REVIEW_TWICE_REASON if twice else REVIEW_REJECTED_REASON,
                    verdicts=tuple(verdicts), reclassified=reclassified,
                    scan_s=scan_s, apply_s=apply_s,
                    measure_s=_measure_total(measure_s, verdicts), **deviation, **review))

        return self._record_package(PackageVerdict(
            region_index=region.index, status=COMMITTED, verdicts=tuple(verdicts),
            reclassified=reclassified, unmet_cleared=unmet, scan_s=scan_s, apply_s=apply_s,
            measure_s=_measure_total(measure_s, verdicts), **deviation, **review))

    def _review(self, package: TargetPackage, verdicts: list[OpVerdict], boundary: tuple,
                found: tuple, *, whole: bool, before, after, declared: Accounting,
                deviation: dict, tier: str = review_tier.REVIEWER,
                tier_reason: str = "") -> dict:
        """The semantic reviewer's verdict on a package that passed its predicate, as the
        PackageVerdict fields `reviewed`, `review_reason` and `review_s`.

        BOTH SIDES OF THE DEFECT EVIDENCE ARE DRAWN FROM ONE SCOPE, as the progress rule's are:
        the boundary list is the cached whole-shape one, so for a scoped package it is cut to the
        region before it is compared with the region's proof.

        THE CLOCK. The call's timeout is the lesser of CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S and
        what is left of the RUN (`remaining_s`) less `package_review.DEADLINE_MARGIN_S`, the
        model_author pattern. It is NOT cut by the
        package's own budget: that budget bounds the plan and its proof, and letting an overrun
        there skip the review would commit exactly the unreviewed package this posture exists to
        forbid. And WITH NO RUN TIME LEFT THE VERDICT IS A REJECT ("no time left to review"),
        never a skip and never an accept - for the same reason.
        """
        mesh_probed = rcfg.CAD_REPAIR_MESH_PROBE == "live"
        was, now = tuple(boundary), tuple(found)
        if not whole:
            diagonal = self.original.diagonal_mm
            was = tuple(scoped_to(was, package.region, diagonal_mm=diagonal))
            now = tuple(scoped_to(now, package.region, diagonal_mm=diagonal))
        delta = {"verdicts": [v.to_dict() for v in verdicts],
                 "before": before.to_dict(), "after": after.to_dict(),
                 "declared_area_mm2": declared.declared_area_mm2,
                 "declared_volume_mm3": declared.declared_volume_mm3}
        started = self.clock()
        evidence = package_review.evidence_of(
            package, delta, deviation,
            package_review.probe_of(was, mesh_probed=mesh_probed),
            package_review.probe_of(now, mesh_probed=mesh_probed),
            original=self.original.to_dict(),
            review_rejects=tuple(self._review_rejects.get(package.region.index, ())))
        deadline = (started + float(self.remaining_s()) - package_review.DEADLINE_MARGIN_S
                    if self.remaining_s is not None else None)
        if tier == review_tier.AUTO:
            # NO MODEL CALL, AND NOTHING LESS IN THE RECORD. The evidence is built and clipped
            # exactly as it is for the reviewer, because the audit of an auto-accept has to show
            # what the acceptance was made on - an accept with no evidence behind it would make
            # this tier unauditable, which is the one thing it may not be.
            return {"reviewed": package_review.ACCEPT,
                    "review_reason": f"auto-accept: {tier_reason}",
                    "review_tier": review_tier.AUTO, "review_s": 0.0,
                    "review_evidence": package_review.clip_evidence(evidence)}
        verdict, reason, objection = package_review.review_package_objection(
            evidence, timeout_s=float(rcfg.CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S),
            deadline_s=deadline, clock=self.clock)
        out = {"reviewed": verdict, "review_reason": reason,
               "review_tier": review_tier.REVIEWER,
               "review_s": self.clock() - started,
               "review_evidence": package_review.clip_evidence(evidence)}
        if objection is not None:
            # THE ONLY THING THAT CROSSES TO THE AUTHOR. It is MEASURED (`_objection_of` reads the
            # session's own evidence, never the reply's text), so no word the reviewer wrote can
            # ride on it - and `review_reason`, which is the reviewer's prose, is deliberately NOT
            # in the attempt log `observe` builds.
            out["review_objection"] = objection
        return out

    def _deviation_evidence(self, *, cap_s: float) -> tuple[dict, str]:
        """The deviation fields for a package that passed its predicate ({} when not measured at
        all), and the `package_review` deviation status saying whether and why not it was.

        MEASURED AGAINST THE UPLOAD (`self._upload`, the shape repair received), on the faces this
        package's kept operations added or rebuilt - the spec's "deviation from the upload". Not
        against the package's own starting shape, which would let a run of packages each measure
        small while together moving the part far; the cost is the same.

        THE SAME POSTURE AS THE MESH PROBE, no new flag: attempted only when CAD_REPAIR_MESH_PROBE
        is live, so flag-off runs make no measurement and carry no new key. EVIDENCE, NOT A GATE -
        recorded on the verdict, never branched on. The budget is the least of 10 s, what is left
        of the run (`remaining_s`) and what is left of the package, so measuring cannot overspend
        either clock; its seconds are recorded as `deviation_s`. A failed or spent measurement
        records only that, leaving `deviation_mm` absent rather than invented. Looked up on the
        module at call time so a test can patch it.
        """
        if rcfg.CAD_REPAIR_MESH_PROBE != "live":
            return {}, package_review.DEVIATION_PROBE_OFF
        from meshpipeline.cad.repair import deviation
        from meshpipeline.cad.repair.operations import _diagonal_of

        budget = min(10.0, cap_s,
                     float(self.remaining_s()) if self.remaining_s is not None else 10.0)
        if not budget > 0:
            return {"deviation_s": 0.0}, package_review.DEVIATION_NO_TIME
        try:
            diagonals = tuple(dict.fromkeys(_diagonal_of(shape) for shape in self._kept_inputs))
        except Exception:  # noqa: BLE001 - evidence only
            diagonals = ()
        # The kept faces were named on the run's grid (`try_op`), so they are looked up on it.
        with run_grid(self.original.diagonal_mm):
            result = deviation.measure_deviation(
                self._upload, self.shape, tuple(dict.fromkeys(self._kept_faces)),
                budget_s=budget, diagonals=diagonals)
        out: dict = {"deviation_s": result.seconds}
        if result.faces is not None:
            out.update(deviation_samples=result.samples, deviation_faces=result.faces,
                       deviation_faces_skipped=result.faces_skipped)
        if result.mm is not None:
            out["deviation_mm"] = result.mm
            return out, package_review.DEVIATION_MEASURED
        if result.faces == 0:
            return out, package_review.DEVIATION_NO_FACES
        return out, package_review.DEVIATION_FAILED

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

    def _region_scan(self, region, *, whole_shape: bool = False,
                     cap_s: float | None = None) -> tuple[tuple, bool]:
        """ONE re-inspection, scoped to the region, for the whole package - or of the WHOLE shape
        when `whole_shape` (a plan answering a volume-stage code, `_judged_whole`).

        IT DOES NOT BECOME THE SESSION'S DEFECT LIST. A scoped list is exhaustive only within its
        scope, and `self._defects`/`self.complete_scan` are read as statements about the whole
        shape; replacing them with a region's findings would claim the rest of the part had been
        checked and found sound. The package's own verdict carries what this scan said instead.

        The face-level budget is the one the session's first scan uses, not the status-walk-only
        one a per-operation re-inspection gets: this is the scan a commit rests on, so within the
        region it is as thorough as the original inspection was.
        """
        self.scan_count += 1
        if not whole_shape:
            self.scoped_scan_count += 1
        if self._injected:
            found, complete = self._scan(self.shape)
        else:
            budget = 0.0
            if rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS:
                remaining = self.remaining_s or (lambda: float(rcfg.CAD_REPAIR_TIMEOUT_S))
                budget = face_scan_budget_s(remaining())
            if whole_shape:
                found, complete = _localize_scan(
                    self.shape, face_scan_s=budget,
                    quantum_diagonal_mm=self.original.diagonal_mm)
                found = self._with_mesh(found, self.shape, within=None, cap_s=cap_s)
            else:
                # ONE GROWN BOX FOR BOTH SIDES OF THE SCOPE, as in `_default_scan`.
                found, complete = _localize_scan(
                    self.shape, face_scan_s=budget, within=region,
                    scope_diagonal_mm=self.original.diagonal_mm,
                    quantum_diagonal_mm=self.original.diagonal_mm)
                found = self._with_mesh(found, self.shape, within=region, cap_s=cap_s)
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
                           op_name: str, revealed: frozenset = frozenset(),
                           standing: frozenset | None = None,
                           tolerance_mm: float = 0.0) -> str:
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
        if _is_mesh_code(defect_code) and any(
                _unlocated_mesh(d) and d.code.value in wanted
                and d.code not in mesh_probe.NO_ANSWER_CODES for d in found):
            # THE MESH FAILURE PERSISTED, JUST UNPLACEABLE NOW (final review I1b). gmsh still
            # fails with the kind aimed at, but the probe could not name a face, so the target's
            # fingerprint is gone from the rescan - which is not the failure going.
            return "target_remains"

        # Judged on what the mesh probe can actually speak to (`_mesh_judged`); identity when off.
        previous, found = _mesh_judged(previous, found, standing=standing)
        before_names = {(d.fingerprint, d.code.value) for d in previous}
        before_geometry = {key for d in previous if (key := _geometric_defect_key(d)) is not None}
        # KEPT AS A LIST TOO, because the tolerant comparison cannot be a set lookup: a key whose
        # floats moved by rounding hashes somewhere else entirely (see _same_geometry). The exact
        # set stays as the fast path and this is consulted only when it misses.
        before_boxes = tuple(before_geometry)
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
            if (defect.fingerprint, defect.code.value) in revealed:
                # REVEALED, NOT DAMAGE (`_revealed`): it stays on the defect list for the next
                # package to answer or escalate, and the verdict's delta names it.
                continue
            geometric_key = _geometric_defect_key(defect)
            if geometric_key is not None and geometric_key in before_geometry:
                continue
            if geometric_key is not None and tolerance_mm > 0 and any(
                    _same_geometry(geometric_key, known, tol_mm=tolerance_mm)
                    for known in before_boxes):
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
