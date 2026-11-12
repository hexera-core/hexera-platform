# Responsibility: Drive a repair REGION BY REGION - decompose the part, ask an author for one
#                 target package per region, have the session execute each as one reversible unit,
#                 ship the regions that committed and queue the ones that escalated.
# Owns: the region walk and its order, the run's budget arithmetic, the stop reasons, the ONE
#       whole-shape verification the run's verdict rests on, and the record of the whole run.
# Boundaries: it authors nothing and judges nothing. The author proposes a package and the session
#             judges it; this module only decides what is offered, in what order, and what the run
#             as a whole is called. It reads no file and writes none.
# Collaborates with: cad/repair/regions.py (the decomposition), cad/repair/package.py (what an
#                    author returns), cad/repair/session.py (try_package), cad/repair/loop.py (the
#                    operation-at-a-time sibling, whose budget and stop-reason vocabulary this
#                    shares), cad/repair/rules_author.py (the deterministic author),
#                    cad/repair/conservative.py (its caller).
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.localize import analyzer_only

# THE STOP-REASON VOCABULARY IS SHARED WITH THE OPERATION-AT-A-TIME LOOP, imported rather than
# restated: a reader downstream (pipeline/repair_receipt.py, the corpus) routes on these strings,
# and two loops spelling "scan_incomplete" differently would silently split that routing in two.
from meshpipeline.cad.repair.loop import (
    OP_BUDGET,
    RESOLVED,
    SCAN_INCOMPLETE,
    TIME_BUDGET,
    LoopBudget,
    RepairRun,
)
from meshpipeline.cad.repair.package import TargetPackage
from meshpipeline.cad.repair.regions import Region, decompose
from meshpipeline.cad.repair.session import (
    ESCALATED,
    HEAL_OP,
    ROLLED_BACK,
    PackageVerdict,
    RepairSession,
)

logger = logging.getLogger(__name__)

#: Every region was offered its one package and what remains was not repaired. The region-level
#: sibling of the old loop's `no_candidate`: there is nothing further to try, because a region
#: gets one package per run here (mid-run revision is Plan 2's, with the model).
REGIONS_EXHAUSTED = "regions_exhausted"

#: At least one region is queued for a person. NOT `resolved`, whatever is left in the geometry:
#: a customer receiving a knowingly partial repair has to be told so (spec decision 7), and the
#: committed regions shipped anyway - which is the whole reason this loop exists beside the old
#: one, whose stall limit is global and ends the run for two unrelated failures anywhere.
ESCALATED_REGIONS = ESCALATED

_ERRORS = (DefectSeverity.error, DefectSeverity.fatal)

#: THE SEAM OF THE WHOLE PROJECT. `(region, its defects) -> a package, or None`. A later plan
#: substitutes a language model for the deterministic author without touching this file, so the
#: loop must never learn which kind of author it has: `None` means "nothing to propose for this
#: region", which is not a failure - the region is simply left alone.
Author = Callable[[Region, tuple], "TargetPackage | None"]


@dataclass(frozen=True, slots=True)
class RegionRun:
    """What a region-scoped run did, region by region, and why it stopped.

    `escalated` IS THE QUEUE, not a failure list: those regions burned their own budget without
    satisfying their claim and go in front of a person, while every committed region is already in
    `shape`. Reading `packages` alone cannot tell the two apart from the ops' own verdicts, which
    is why the package verdict travels here.

    `defects_before` and `defects_after` ARE BOTH REPORTED. A run holds the only two scans that
    can answer "did the repair cause this?" - the upload's and the survivors' - and a loop that
    kept only what was left would make that question unanswerable afterwards (the disposition
    ledger is built on these two).
    """

    shape: object
    packages: tuple[PackageVerdict, ...]
    regions: tuple[Region, ...]
    escalated: tuple[int, ...]
    stopped_because: str
    complete_scan: bool
    timings: dict = field(default_factory=dict)
    defects_before: tuple = ()
    defects_after: tuple = ()

    @property
    def resolved(self) -> bool:
        return self.stopped_because == RESOLVED

    def to_dict(self) -> dict:
        return {"stopped_because": self.stopped_because, "resolved": self.resolved,
                "complete_scan": bool(self.complete_scan),
                "escalated": [int(i) for i in self.escalated],
                "regions": [{"index": int(r.index), "located": bool(r.located),
                             "fingerprints": sorted(r.fingerprints),
                             "bbox_min": list(r.bbox_min) if r.bbox_min else None,
                             "bbox_max": list(r.bbox_max) if r.bbox_max else None}
                            for r in self.regions],
                "packages": [p.to_dict() for p in self.packages],
                "defects_remaining": [d.to_dict() for d in self.defects_after],
                "timings": dict(self.timings)}


def _judged(defects) -> tuple:
    """The defects the ENDING is decided on: everything but the face analyzer's own findings.

    The same non-demotion rule the operation-at-a-time loop applies (`loop._judged`): a crossing
    only the analyzer measured exists to target the heal and to ask a person, and one no operation
    removed must not turn a run the flag-off world calls `resolved` into something worse.
    """
    return tuple(d for d in defects if not analyzer_only(d))


def _errors_remain(defects) -> bool:
    return any(d.severity in _ERRORS for d in _judged(defects))


def _abandoned(packages: tuple[PackageVerdict, ...], defects) -> bool:
    """Is a defect still present at a fingerprint a discarded package's operation aimed at?

    `resolved` MEANS THE RUN SUCCEEDED, not merely that no error-severity defect remains. Those
    differ exactly when the run gave up on a warning, and calling that resolved reads downstream
    as "repaired".

    THE RULE IS `loop._abandoned`'S, PORTED RATHER THAN REINTERPRETED, in both its particulars:

    * WHAT WAS AIMED AT, not the whole region. A region is a conservative, over-merged cluster, so
      a package that failed on one edge says nothing about a defect at the far end of the same
      box; marking the whole region would fail runs the baseline calls resolved for geometry no
      operation ever touched.
    * A ROLLED-BACK HEAL ABANDONS NOTHING (floor-fix ruling 2). It aims at the analyzer's crossing
      and its partner faces, and a partner's unrelated warning must not become "abandoned" because
      the heal could not re-trim the tangle. A crossing the status walk also reported is an error
      and is still caught above.

    Both exist so that a difference between this driver and the baseline is attributable to how
    the packages were authored - which is the only reason `loop.py` is kept alive.

    THE KEY IS THE OPERATION, NOT THE PACKAGE, and that is the whole of this function's fidelity
    to the baseline. `loop._abandoned` reads `not v.kept` off the session log, so an attempt that
    failed on a target marks it however the run went on. Keying on the package instead would let a
    rolled-back operation inside a COMMITTED package contribute nothing - the package shipped, so
    its failure would be invisible - and a run that tried and failed on a warning would be handed
    to a customer as `resolved`, which reads as "repaired". The divergence flatters this driver,
    which is exactly the corruption the retained baseline exists to detect, so:

    * EVERY OPERATION THAT DID NOT SURVIVE is tried: one that rolled itself back, and every
      operation of a package that was discarded whole (a `kept` operation inside a reverted
      package left nothing behind either).
    * MINUS WHAT A COMMITTING PACKAGE RECLASSIFIED. Reclassification is a discharge under spec
      decision 2 - the defect remains, explained, with its rationale in the report - and it is
      the ONLY thing that excuses a survivor at a tried fingerprint. `PackageVerdict.reclassified`
      is already the intersection of the predicate's claim with what the scoped scan still found,
      so this credits relabelling that really discharged something rather than what was asked for.
      A reverted package reclassified nothing: its geometry and its claim were both thrown away.
    """
    tried = {fingerprint for verdict in packages
             for op in verdict.verdicts
             if op.op != HEAL_OP and not (op.kept and verdict.committed)
             for fingerprint in op.targets}
    discharged = {fingerprint for verdict in packages if verdict.committed
                  for fingerprint in verdict.reclassified}
    return any(d.fingerprint in tried and d.fingerprint not in discharged
               for d in _judged(defects))


def _terminal(session: RepairSession, defects, *, escalated, packages,
              fallback: str) -> str:
    """What to call a finished run. ONE place, so no exit path can disagree with another.

    AN INCOMPLETE SCAN WINS OVER EVERY OTHER REASON, exactly as in `loop._terminal`: a short list
    from a stopped walk supports no claim of absence, so it does not matter what else happened -
    what we owe the caller is that we could not see. AND AN ESCALATION WINS OVER `resolved`,
    because a run with a region in front of a person is a partial repair however little is left
    in the geometry.
    """
    if not session.complete_scan:
        return SCAN_INCOMPLETE
    if escalated:
        return ESCALATED_REGIONS
    if not _errors_remain(defects) and not _abandoned(packages, defects):
        return RESOLVED
    return fallback


def _with_tolerance(package: TargetPackage, tolerance_mm: float) -> TargetPackage:
    """The package as its author wrote it, with the RUN'S tolerance filled into any plan item
    that did not ask for one.

    THE LOOP OWNS THE TOLERANCE, not the author. It is a deployment's deviation ceiling worked out
    from the part's own size (`conservative.repair_step_file`), not a choice about this region, and
    a model author has no business inventing one. An item that DID state a tolerance is left
    exactly as written, so an author that needs a tighter one can still ask.
    """
    if all("tolerance_mm" in item for item in package.plan):
        return package
    plan = tuple({"tolerance_mm": float(tolerance_mm), **item} for item in package.plan)
    return replace(package, plan=plan)


def run_regions(shape, *, author: Author, tolerance_mm: float,
                budget: LoopBudget | None = None, reserved: frozenset = frozenset(),
                session: RepairSession | None = None,
                clock: Callable[[], float] = time.monotonic) -> RegionRun:
    """Repair `shape` one region at a time, and report what each region's package did.

    `session` and `clock` are injectable so the walk and the budget arithmetic can be tested
    without a kernel and without waiting. `reserved` reaches a session this function CONSTRUCTS;
    an INJECTED session keeps whatever holds it was built with, so a test that injects one is
    testing the walk and not the session half of the hold guarantee. Production never injects.

    SEQUENTIALLY, IN `decompose` ORDER, and not because the work is coupled - regions are
    independent, which is the point of them. `RepairSession` owns ONE shape and snapshots it by
    reference, and concurrent `TopoDS_Shape` mutation is not something OpenCASCADE offers, so a
    parallel walk would need a session per region and a merge nobody has written. The saving in
    this design comes from the scans being SCOPED, not from them being concurrent; the order is
    fixed so that two identical runs produce identical reports.

    THE VERDICT COMES FROM ONE WHOLE-SHAPE SCAN AFTER THE MERGE, never from the per-region scoped
    scans. A scoped scan is exhaustive only inside its own region, so it can support a claim about
    that region and nothing about the part - and the part is what is being handed back.
    """
    limits = budget if budget is not None else LoopBudget.from_settings()
    # THE CLOCK STARTS BEFORE THE SESSION EXISTS, so every scan's slice of the budget is a slice
    # of what this run actually has left, including the first scan.
    started = clock()

    def remaining() -> float:
        return limits.timeout_s - (clock() - started)

    # `reserved` reaches the SESSION as well as the author: the author will not aim at a held
    # entity, and the session will not let a sweep remove one it never aimed at. Nothing here
    # grants a package the engineer's `answered_held` exemption - a package is authored, and an
    # answer to a hold is a person's (session.try_package).
    work = session if session is not None else RepairSession(
        shape, remaining_s=remaining, reserved=reserved, clock=clock)
    if work.remaining_s is None:
        work.remaining_s = remaining
    # WHAT THE SESSION ITSELF COUNTED, not arithmetic over the verdicts. Every scan a package
    # takes is now scoped - each operation's own proof as well as the package's - and an
    # operation the caps refused before its re-inspection took none at all, so only the session
    # knows. Read as a delta because a caller may inject a session that has already scanned.
    scoped_at_start = work.scoped_scan_count

    # THE FIRST SCAN, and its completeness read AFTER it: `complete_scan` starts out True, so
    # asking before any scan has run reports a truncated walk as exhaustive.
    before = work.defects()
    verification_scans = 1
    if not work.complete_scan:
        # A SHORT LIST FROM A STOPPED WALK IS NOT A LIST OF REGIONS. Decomposing it would scope
        # packages to the part of the part we happened to see and prove them there.
        return RegionRun(
            shape=work.shape, packages=(), regions=(), escalated=(),
            stopped_because=SCAN_INCOMPLETE, complete_scan=False,
            timings=_timings(started, clock, work, (), verification_scans,
                             work.scoped_scan_count - scoped_at_start),
            defects_before=before, defects_after=before)

    regions = decompose(before, diagonal_mm=work.original.diagonal_mm)
    packages: list[PackageVerdict] = []
    escalated: list[int] = []
    committed = False
    # WHAT THE RUN'S OPERATION ALLOWANCE HAS BEEN SPENT ON, counted here rather than read off
    # `len(work.log)`, because the log counts every ATTEMPT and this budget bounds mutation work.
    # A PACKAGE THAT KEPT NOTHING DOES NOT CHARGE AGAINST IT. With per-operation proofs scoped,
    # a region an earlier package already cleared is only discovered by packaging it (see below),
    # and charging those discoveries would let a part whose regions were co-swept burn the whole
    # allowance on packages that changed nothing and stop with `op_budget` before reaching a
    # region that still has real defects. The run stays bounded regardless: one package per
    # region, and each package's own `PackageBudget` bounds the attempts inside it.
    charged = len(work.log)
    # WALKED EVERY REGION unless a budget says otherwise; the loop runs out of regions rather than
    # of ideas, because a region is offered exactly one package per run.
    stopped = REGIONS_EXHAUSTED

    for region in regions:
        if clock() - started >= limits.timeout_s:
            # THE WHOLE RUN, not one region. A per-region allowance is the package's own
            # (`PackageBudget.timeout_s`); this is the job's deadline, and the regions already
            # committed stay committed.
            stopped = TIME_BUDGET
            break
        if charged >= limits.max_ops:
            stopped = OP_BUDGET
            break

        # THE REGION'S DEFECTS AS THEY ARE NOW, not as the first scan found them - but "now" is
        # the last whole-shape scan plus whatever a scoped proof has since re-measured, and that
        # is the price of locality. An earlier region's package can legitimately clear a later
        # region's defects (a sew joins the whole part, the sliver sweep walks it), and with every
        # per-operation proof scoped to its own region NOTHING HAS WALKED THIS ONE. So a region
        # whose geometry is already sound is not skipped for free any more: it is offered its one
        # package, and the package's own operation declines on geometry that is no longer there.
        # That costs an apply and one scoped scan - paid in an apply rather than in a scan, which
        # is the trade this driver exists to make - and it keeps no operation, so it does not
        # charge against the run's allowance above.
        # NEXT PLAN: one scoped scan per region before authoring would skip such a region again
        # at no extra scan cost, and is scheduled as its own change because it alters what the
        # loop attempts.
        mine = tuple(d for d in work.defects() if d.fingerprint in region.fingerprints)
        if not mine:
            continue
        package = author(region, mine)
        if package is None:
            # NOT A FAILURE AND NOT AN ESCALATION. The author had nothing to propose, so the
            # region is left exactly as it arrived and the final scan reports it.
            continue
        if charged + len(package.plan) > limits.max_ops:
            # LOOKED AT BEFORE THE PACKAGE IS STARTED, not only after the log has grown past the
            # allowance. A package is executed whole or not at all, so a plan that cannot fit in
            # what the run has left would spend `package.budget.max_ops - 1` operations past the
            # run's own budget - and with a model author asking for a six-operation package, the
            # run budget would be whatever the author felt like. Nothing is tried here, so the
            # region is not escalated: the run simply ran out, which is what `op_budget` says.
            stopped = OP_BUDGET
            break

        verdict = work.try_package(_with_tolerance(package, tolerance_mm))
        packages.append(verdict)
        if any(v.kept for v in verdict.verdicts):
            # THE WHOLE PLAN IS CHARGED THE MOMENT ANY OF IT WAS KEPT, including the operations
            # that rolled themselves back beside it: the package did real work on the shape, and
            # whether the package then survived its own predicate is not what this budget bounds.
            charged += len(verdict.verdicts)
        if verdict.committed:
            committed = True
        elif verdict.status == ESCALATED:
            escalated.append(int(region.index))

    if committed:
        # THE ONE WHOLE-SHAPE SCAN THE VERDICT RESTS ON, as thorough as the first inspection was,
        # and taken ONLY when something actually changed: a run that committed nothing is still
        # sitting on the list it started from, and re-measuring it would buy the same answer
        # twice. A sound part therefore costs exactly one scan.
        after = work._rescan(face_scan=True)  # noqa: SLF001 - the session's own whole-shape scan
        verification_scans += 1
    else:
        after = work.defects()

    stopped = _terminal(work, after, escalated=escalated, packages=tuple(packages),
                        fallback=stopped)
    logger.info("region loop: stopped=%s regions=%d packages=%d committed=%d escalated=%s",
                stopped, len(regions), len(packages),
                sum(1 for p in packages if p.committed), escalated)
    return RegionRun(
        shape=work.shape, packages=tuple(packages), regions=regions,
        escalated=tuple(escalated), stopped_because=stopped, complete_scan=work.complete_scan,
        timings=_timings(started, clock, work, tuple(packages), verification_scans,
                         work.scoped_scan_count - scoped_at_start),
        defects_before=before, defects_after=after)


def _timings(started: float, clock: Callable[[], float], session: RepairSession,
             packages: tuple[PackageVerdict, ...], verification_scans: int,
             scoped_scans: int) -> dict:
    """Where the run's time went, in the keys the report already carries plus the scoped ones.

    `scan_count` is every scan the session took, `verification_scans` the whole-shape ones this
    loop took to decide the run (the first, and the final one after the merge), and
    `scoped_scans` every scan that was scoped to one region - each operation's own proof as well
    as each package's. The three together are what says whether scoping made a run cheaper, which
    is the claim this design has to be able to defend, and in this loop they account for each
    other exactly: `scan_count == verification_scans + scoped_scans`, because once a package owns
    an operation there is no whole-shape walk left between the run's first scan and its last.
    """
    return {"total_s": clock() - started, "scan_count": session.scan_count,
            "verification_scans": verification_scans, "scoped_scans": scoped_scans,
            "scan_s": sum(p.scan_s for p in packages),
            "apply_s": sum(p.apply_s for p in packages),
            "measure_s": sum(v.measure_s for p in packages for v in p.verdicts)}


def as_repair_run(run: RegionRun, *, original: dict, final: dict,
                  planner_label: str = "") -> RepairRun:
    """The region run in the shape every stage AFTER the loop already reads.

    WHY A TRANSLATION RATHER THAN A SECOND REPORT PATH. `conservative.repair_step_file`, the
    attempt node, the receipt and the corpus all read `RepairRun`; giving the region loop its own
    delivery path would double every after-check that protects a customer's geometry. The
    region-level truth is not lost - it travels as its own report key.

    AN OPERATION KEPT INSIDE A PACKAGE THAT DID NOT COMMIT IS RESTAMPED `rolled_back`, and this is
    not cosmetic: the after-checks are handed the DECLARATIONS of every kept operation
    (`declared_area_mm2`), and a declaration from geometry that was discarded with its package
    would be credited against a result that no longer contains it - which is how one discarded
    package refuses the next legitimate operation anywhere in the run.
    """
    verdicts: list = []
    for verdict in run.packages:
        for op in verdict.verdicts:
            if verdict.committed:
                verdicts.append(op)
                continue
            verdicts.append(replace(
                op, status=ROLLED_BACK,
                reason=(f"discarded_with_package: region {verdict.region_index} "
                        f"{verdict.status} ({verdict.reason})")))
    # EVERY ESCALATION REACHES THE REPORT, in the key that already means "deliberately not
    # finished, and why" - a region queued for a person must not be a silence.
    refusals = tuple({"refusal": "region_escalated", "region": int(verdict.region_index),
                      "reason": verdict.reason}
                     for verdict in run.packages if verdict.status == ESCALATED)
    return RepairRun(
        shape=run.shape, verdicts=tuple(verdicts), refusals=refusals,
        defects=run.defects_after, stopped_because=run.stopped_because,
        complete_scan=run.complete_scan, planner_label=planner_label,
        original=dict(original), final=dict(final),
        total_s=float(run.timings.get("total_s", 0.0)),
        scan_count=int(run.timings.get("scan_count", 0)))
