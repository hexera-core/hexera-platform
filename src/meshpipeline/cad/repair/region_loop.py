# Responsibility: Drive a repair REGION BY REGION - decompose the part, ask an author for target
#                 packages for each region until one holds or its attempts run out, have the
#                 session execute each as one reversible unit, ship the regions that committed and
#                 queue the ones that escalated.
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
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import grind, oracle
from meshpipeline.cad.repair.contracts import DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import analyzer_only

# THE STOP-REASON VOCABULARY IS SHARED WITH THE OPERATION-AT-A-TIME LOOP, imported rather than
# restated: a reader downstream (pipeline/repair_receipt.py, the corpus) routes on these strings,
# and two loops spelling "scan_incomplete" differently would silently split that routing in two.
from meshpipeline.cad.repair.loop import (
    OP_BUDGET,
    RESOLVED,
    SCAN_INCOMPLETE,
    TIME_BUDGET,
    UNMESHABLE,
    LoopBudget,
    RepairRun,
    only_mesh_errors,
)
from meshpipeline.cad.repair.package import TargetPackage
from meshpipeline.cad.repair.regions import Region, decompose
from meshpipeline.cad.repair.session import (
    DECLINED,
    ESCALATED,
    HEAL_OP,
    REVIEW_REJECTED,
    ROLLED_BACK,
    PackageVerdict,
    RepairSession,
)

logger = logging.getLogger(__name__)

#: Every region spent its packages and what remains was not repaired. The region-level sibling of
#: the old loop's `no_candidate` AND of its `stalled`: a region is offered packages until one
#: holds, its author runs dry or its attempt bound is reached, so this says there is nothing
#: further the authors on hand will try - not that only one thing was tried.
REGIONS_EXHAUSTED = "regions_exhausted"

#: At least one region is queued for a person. NOT `resolved`, whatever is left in the geometry:
#: a customer receiving a knowingly partial repair has to be told so (spec decision 7), and the
#: committed regions shipped anyway - which is the whole reason this loop exists beside the old
#: one, whose stall limit is global and ends the run for two unrelated failures anywhere.
ESCALATED_REGIONS = ESCALATED

_ERRORS = (DefectSeverity.error, DefectSeverity.fatal)

#: THE SEAM OF THE WHOLE PROJECT. `(region, its defects, what it already tried) -> a package, or
#: None`. A later plan substitutes a language model for the deterministic author without touching
#: this file, so the loop must never learn which kind of author it has: `None` means "nothing
#: further to propose for this region", which is not a failure - the region is simply left alone.
#:
#: `attempts` IS EVERY PACKAGE ALREADY TRIED IN THIS REGION THIS RUN, in order, as the verdicts
#: the session returned - `()` on a region's first call. IT IS THE WHOLE POINT OF THE THIRD
#: ARGUMENT THAT IT CARRIES THE VERDICTS AND NOT A COUNT: the operation-at-a-time loop's entire
#: advantage over this driver was a `RunHistory` that outlived one iteration, so an author that
#: knows only HOW MANY times it was wrong has learned nothing and would propose the same package
#: again. A verdict carries what each operation did and, in words, how the claim failed, which is
#: exactly the channel a model author revises through - so this shape is a commitment to the next
#: plan, not an implementation detail of this one.
#:
#: AN AUTHOR MAY IGNORE IT, and the first call of every region passes `()`, so a region's first
#: package is byte-for-byte what the one-package-per-region walk authored.
Author = Callable[[Region, tuple, "tuple[PackageVerdict, ...]"], "TargetPackage | None"]


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
    #: WHAT WAS DELIBERATELY NOT AIMED AT AND WHY, in the baseline's own records
    #: (`{"defect": ..., "reason": ..., "detail": ...}`, `plan.RunHistory.refuse`). The author
    #: makes these, not the loop - the loop only carries them, because a refusal a customer is
    #: owed must not depend on which driver ran, and a benchmark row that reports refusals on one
    #: arm and silence on the other is not a comparison.
    refusals: tuple[dict, ...] = ()
    #: WHAT THE PART'S EXPECTED COST BOUGHT EACH REGION: `{"tier", "attempts", "rule"}`, or `{}`
    #: when `CAD_REPAIR_BUDGET_ATTEMPTS` is off or a caller stated `attempts` itself. Empty is
    #: absent from `to_dict`, so a flag-off report is byte-for-byte what it is today.
    attempt_budget: dict = field(default_factory=dict)
    #: ONE RECORD PER COMMITTED PACKAGE, in commit order - what `replay.replay_packages` re-applies
    #: (`replay_record`). Empty, and absent from `to_dict`, unless an AGENT drove the run: a
    #: rules-only run is byte-identically replayable from its bytes and needs no package log,
    #: which is the contract the native determinism tests pin.
    packages_replay: tuple[dict, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.stopped_because == RESOLVED

    def to_dict(self) -> dict:
        extra: dict = {}
        if self.attempt_budget:
            extra["attempt_budget"] = dict(self.attempt_budget)
        if self.packages_replay:
            extra["packages_replay"] = [dict(r) for r in self.packages_replay]
        return {**extra,
                "stopped_because": self.stopped_because, "resolved": self.resolved,
                "refusals": [dict(r) for r in self.refusals],
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
    * A DECLINE IS NOT A FAILED ATTEMPT, which is the baseline's own ruling, stated at its
      `declined` branch: "the operation judged the aim and ran nothing, so it says nothing about
      whether progress is possible". An operation that declined never attempted its target, so it
      cannot have abandoned it - a region whose only proposal was declined has had NOTHING tried
      in it, and a defect surviving there is a defect nobody claimed, which is what the final
      whole-shape scan reports in its own right rather than something a failed attempt left
      behind. Identified by the `declined:` reason prefix `try_op` records, the same idiom as
      `cap:` and `held_for_review:`; the granularity stays per-operation, so a package whose
      other operation really was rolled back still marks that operation's targets.
    * A ROLLED-BACK HEAL ABANDONS NOTHING (floor-fix ruling 2). It aims at the analyzer's crossing
      and its partner faces, and a partner's unrelated warning must not become "abandoned" because
      the heal could not re-trim the tangle. A crossing the status walk also reported is an error
      and is still caught above.

    All three exist so that a difference between this driver and the baseline is attributable to how
    the packages were authored - which is the only reason `loop.py` is kept alive.

    THE KEY IS THE OPERATION, NOT THE PACKAGE, and that is the whole of this function's fidelity
    to the baseline. `loop._abandoned` reads `not v.kept` off the session log, so an attempt that
    failed on a target marks it however the run went on. Keying on the package instead would let a
    rolled-back operation inside a COMMITTED package contribute nothing - the package shipped, so
    its failure would be invisible - and a run that tried and failed on a warning would be handed
    to a customer as `resolved`, which reads as "repaired". The divergence flatters this driver,
    which is exactly the corruption the retained baseline exists to detect, so:

    * EVERY OPERATION THAT DID NOT SURVIVE AND DID ATTEMPT SOMETHING is tried: one that rolled
      itself back, and every operation of a package that was discarded whole (a `kept` operation
      inside a reverted package left nothing behind either) - minus the declines above, which
      attempted nothing.
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
             and not op.reason.startswith(DECLINED)
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
    if only_mesh_errors(_judged(defects)):
        return UNMESHABLE
    return fallback


#: Worse first, the same ranking the operation-at-a-time planner orders its defects by
#: (`plan._SEVERITY_RANK`), restated here because a WALK order and a PROPOSAL order are two
#: different decisions that happen to share a scale today.
_SEVERITY_RANK = {DefectSeverity.fatal: 0, DefectSeverity.error: 1,
                  DefectSeverity.warning: 2, DefectSeverity.info: 3}


def _walk_order(regions: tuple[Region, ...], defects) -> tuple[Region, ...]:
    """The regions in the order they should be OFFERED PACKAGES: worst defect first.

    WHY NOT `decompose` ORDER. `decompose` sorts spatially (ascending `bbox_min`), which is the
    right order for a geometric function and the wrong one for a budget. A spatial walk spends the
    run's clock and its operation allowance on whatever sits at the lowest corner, so on a part
    with many scattered warnings and one error the run can exhaust its budget before it reaches
    the error at all - it has then done the cheap work and left the thing that makes the part
    unusable standing. Measured on the NIST MBE PMI corpus: `nist_ctc_02_asme1_ap242-e2.stp`
    decomposes into 103 regions and its first error-bearing region is at spatial index 47, so the
    run hit `time_budget` on warning-only regions and committed nothing; the baseline, whose
    single queue is implicitly severity-ordered because its planner always proposes for the worst
    defect it can claim, repaired it.
    THE BASELINE'S ORDER IS THEREFORE THE ONE TO MATCH, and matching it is what makes a difference
    between the two drivers attributable to how packages are AUTHORED - the only reason `loop.py`
    is kept alive.

    HERE AND NOT IN `decompose`, because the walk order is the loop's concern: `decompose` is
    pure geometry and several tests pin its ordering, and a region's severity is not a property of
    its bounding box but of the defects it holds AT THE MOMENT IT IS WALKED - which is why the
    rank is re-read from the run's own current defect list, exactly as the loop re-reads `mine`.

    DETERMINISTIC, because the whole before/after comparison rests on it (`localize_scan`'s
    docstring states the property): the rank is the only key and `decompose`'s spatial index is the
    tie-break, so two inspections of the same bytes produce the same walk, and regions of equal
    severity are still walked in spatial order.

    RANKED OVER ALL THE REGION'S DEFECTS, the face analyzer's own findings included. `_judged`
    drops those for the VERDICT, because a crossing no operation removed must not worsen a run -
    but a crossing is still the worst thing on the part and the heal that answers it should be
    reached before a sliver sweep somewhere else. An ordering is not a judgement.
    """
    worst: dict[int, int] = {}
    for defect in defects:
        severity = _SEVERITY_RANK.get(defect.severity, len(_SEVERITY_RANK))
        for region in regions:
            if defect.fingerprint in region.fingerprints:
                worst[region.index] = min(worst.get(region.index, severity), severity)
    stops_the_mesh = _mesh_located_regions(regions, defects)
    really_meets = _real_intersection_regions(regions, defects)

    def rank(region: Region) -> tuple:
        if region.index in really_meets:
            group = 0
        elif region.index in stops_the_mesh:
            group = 1
        else:
            group = 2
        return (group, worst.get(region.index, len(_SEVERITY_RANK)), region.index)

    return tuple(sorted(regions, key=rank))


#: What `measurements["located_by"]` says on a defect the surface-mesh clash search placed
#: (lane G, mesh_clash). Only those defects carry `clash_gap_mm`.
CLASH_LOCATED = "surface-mesh-clash"


def _real_intersection_regions(regions: tuple[Region, ...], defects) -> frozenset[int]:
    """The regions holding a clash the kernel measured as GENUINELY MEETING - `clash_gap_mm` 0.

    Of the pairs gmsh complains about, only these are something an operation can heal: a
    sub-element GAP is the mesher's own tolerance against geometry that never touches, and no
    heal or re-trim will close it. So the walk reaches the real ones first.

    ABSENT AND NONE ARE UNKNOWN, AND NEVER PROMOTE. The key is present only on a clash-located
    defect and is None when the kernel could not measure it, so reading it with a default of 0
    would rank EVERY region as a real intersection - the same as no ordering at all, and
    invisible in the numbers. `located_by` is checked too, so a 0.0 that came from somewhere
    else is not mistaken for a measured one.
    """
    import meshpipeline.settings.cad_repair as rcfg

    if getattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off") != "live":
        return frozenset()
    meeting = set()
    for defect in defects:
        measurements = getattr(defect, "measurements", None) or {}
        if measurements.get("located_by") != CLASH_LOCATED:
            continue
        gap = measurements.get("clash_gap_mm")
        if isinstance(gap, (int, float)) and not isinstance(gap, bool) and float(gap) == 0.0:
            if defect.fingerprint:
                meeting.add(defect.fingerprint)
    return frozenset(r.index for r in regions if meeting & set(r.fingerprints))


def _mesh_located_regions(regions: tuple[Region, ...], defects) -> frozenset[int]:
    """The regions holding a defect gmsh LOCATED, under `CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS`.
    Empty with the posture off, which leaves the walk exactly as severity ordered it.

    WHY THESE GO FIRST. A mesh-located defect IS the reason the part will not mesh; a sliver
    warning elsewhere is not, however severe the analyzer calls it. Measured on wave 1: 84% of the
    gmsh-located failure faces sat inside investigated regions, and yet 385 of 538 escalations
    were reached with no time left to make a single tool call, because the run's clock was shared
    across up to 35 regions in severity order. Severity still orders the two groups internally, so
    this re-ranks WHICH region is reached first and nothing about what any of them may do.

    AN UNLOCATED MESH FAILURE PROMOTES NOTHING: with no fingerprint it names no face, so it cannot
    say which region is the one that matters (`mesh_probe.mesh_defects` reports exactly one such
    defect for a failure it could not place).
    """
    import meshpipeline.settings.cad_repair as rcfg

    if getattr(rcfg, "CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS", "off") != "live":
        return frozenset()
    from meshpipeline.cad.repair import mesh_probe

    located = {d.fingerprint for d in defects
               if getattr(d, "code", None) in mesh_probe.MESH_CODES and d.fingerprint}
    return frozenset(r.index for r in regions if located & set(r.fingerprints))


#: A REGION THE FOCUS DID NOT NAME. The mesher handback (pipeline/repair_handback.py) hands repair
#: the faces the FEA ladder actually failed on, so a run under a focus must not spend the part's
#: clock on loci nobody complained about. Such a region is REPORTED and left standing - not
#: silently skipped - because a partial repair a customer is owed an account of is the whole
#: reason this loop reports refusals at all.
NOT_IN_FOCUS = "not_in_focus"


def focus_filter(walk: tuple[Region, ...],
                 focus_faces: tuple[int, ...] = ()) -> tuple[tuple[Region, ...], tuple[dict, ...]]:
    """`(walked, refused)` for a walk under `focus_faces` (1-based kernel face indices).

    An EMPTY focus is byte-for-byte the walk it was given and no refusal, which is what every
    caller but the handback passes.

    A region is IN FOCUS when it holds one of those faces (`Region.face_indices`, the kernel face
    indices of its member defects). Edges and vertices contribute only to a region's box, so a
    region whose defects are all edges is out of focus however close it sits - which is correct:
    the mesher named faces, and those are what it could not mesh.

    FILTERS, NEVER RE-ORDERS. `_walk_order`'s severity ranking is the budget's order and remains
    so; a focus only removes.

    A FOCUS NAMING NOTHING ON THE PART RUNS NOTHING. The alternative - falling back to the whole
    walk - would turn "the mesher named faces we cannot locate" into an unscoped repair, which is
    the one outcome the handback exists to avoid.
    """
    focus = frozenset(int(k) for k in focus_faces or ())
    if not focus:
        return tuple(walk), ()
    kept = tuple(r for r in walk if r.face_indices & focus)
    refused = tuple({"refusal": NOT_IN_FOCUS, "region": int(r.index),
                     "reason": "none of the faces this run was scoped to is in this region",
                     "focus_faces": sorted(focus)}
                    for r in walk if not (r.face_indices & focus))
    return kept, refused


def _refusals_of(author: Author) -> tuple[dict, ...]:
    """What the author refused to aim at, if it reports such a thing.

    READ OFF THE CALLABLE, exactly as `loop.py` reads a planner's `unplayable` off its planner,
    and OPTIONAL in the same way: the `author` seam is `(region, defects, attempts) -> package`
    and the loop must never learn which kind of author it has, so an author with nothing to say
    here simply has no attribute and the run reports no refusals.

    The deterministic author's refusals come from the choosing rules it wraps - the paired rim of
    a missing bore wall above all - and those are a decision a customer is owed in words. They
    reached the report on the operation-at-a-time path because that loop owns the one `RunHistory`
    the planner writes to; on this path the author rebuilds that ledger per call, so without this
    they were authored and thrown away.
    """
    return tuple(dict(record) for record in getattr(author, "refusals", ()))


def _observe_part(author: Author, defects) -> None:
    """Tell the author what the WHOLE part's defects are, once, before any region is authored for.

    READ OFF THE CALLABLE AND OPTIONAL, exactly like `_refusals_of` above and for the same reason:
    the seam is `(region, defects, attempts) -> package` and must stay that way, so an author with
    no part-level view simply has no attribute and nothing happens.

    WHY THE LOOP OWES AN AUTHOR THIS AT ALL, when the whole design of this driver is that an author
    sees one region. Because one of the choosing rules is not about a region and cannot be: the
    paired-rim guard refuses to patch the two ends of one missing tube wall, and A PAIRING IS A
    PROPERTY OF THE PART. Handed one region's defects, the guard recomputes the pairing from that
    list alone, so two rims of one passage that decompose into DIFFERENT regions never meet, never
    pair, and are both filled - the through-hole is sealed and nothing is even reported. A
    region-scoped view of a pairing is not a conservative view, it is a blind one, and the
    direction of that blindness is the one that ruins a part. The baseline loop got this for free
    from its whole-shape defect list; this is what buys it back.

    ONLY WHAT IS REFUSED IS WIDENED BY THIS, never what is proposed for. The author still authors
    for one region, against that region's defects, claiming only fingerprints that region holds.

    `defects` IS THE RUN'S FIRST WHOLE-SHAPE SCAN, and once per run - the same list the regions are
    decomposed from, taken before anything was changed. That is the upload's own answer, which is
    what the guard wants (rules_author: a pairing present on the upload is exactly what must not be
    patched) and it cannot then be lost as kept operations close one rim and leave the other
    looking single.
    """
    observe = getattr(author, "observe_part", None)
    if observe is not None:
        observe(tuple(defects))


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


def final_reserve_s() -> float:
    """Seconds the walk keeps in hand for its final whole-shape verification (final review I2).

    With the mesh probe live the final scan probes the whole part, so the walk stops authoring
    while one probe's timeout plus `oracle.FINAL_PROBE_SLACK_S` is still left - the final
    verification always has room, and its probe then gets whatever remains. 0 with the probe off,
    so a flag-off walk stops exactly where it always did. The rule is the oracle's; this is the
    walk's door onto it, reading the posture and the configured per-probe timeout.
    """
    return oracle.final_reserve_s(mesh_probe_live=rcfg.CAD_REPAIR_MESH_PROBE == "live",
                                  timeout_s=float(rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S))


#: The rule `attempt_budget` names when the allowance came from the part's cost tier. A reader
#: downstream routes on it, so it is a constant rather than a literal spelled at the call site.
BUDGET_RULE_TIER = "grind_tier"
#: The rule it names when a caller stated `attempts` itself: the explicit argument wins over the
#: tier, and the record says which one was in force rather than leaving the reader to guess.
BUDGET_RULE_EXPLICIT = "explicit"


def budget_attempts_live() -> bool:
    """Is the per-part attempt allowance in force? READ AT CALL TIME off the settings module, like
    every other posture here, so a test that patches the attribute is obeyed, and FAILING CLOSED:
    anything but the exact string `live` is off."""
    return str(getattr(rcfg, "CAD_REPAIR_BUDGET_ATTEMPTS", "off") or "off") == "live"


def agent_posture_live() -> bool:
    """Is an AGENT driving this run - the investigator or the tournament, in any non-off posture?

    WHAT IT GATES: `packages_replay`. Byte-identical replay is retired for agent-driven runs only
    (spec pillar 4); a rules-only run is still reproduced from its own bytes, so recording a
    package log for one would add a key to a report whose contract says it has none.

    READ BY `getattr` because both settings belong to other tasks' modules: with both postures
    absent this is `False`, which is the committed behaviour.
    """
    investigator = str(getattr(rcfg, "CAD_REPAIR_INVESTIGATOR", "off") or "off")
    tournament = str(getattr(rcfg, "CAD_REPAIR_TOURNAMENT", "off") or "off")
    return investigator in ("shadow", "live") or tournament == "live"


def _count(value: object) -> int | None:
    """A face or solid count as `grind` wants it, or None when there is not one.

    GUARDED WITH `math.isfinite` RATHER THAN A BARE `>= 0`: a `float("inf")` or a `NaN` arriving
    from a measurement that went wrong would sail through a comparison - `NaN >= 0` is False and
    `inf > HEAVY_FACES` is True - and either answer would be a number nobody measured. None is
    what `grind` reads as UNKNOWN, which is the cheapest budget.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 else None
    return None


def attempt_budget(defects, *, solids: object, faces: object,
                   attempts: int | None = None) -> dict:
    """How many packages one region of THIS part may be offered, and on what grounds.

    `{}` - and no report key at all - when the posture is off, which is the whole of the flag-off
    guarantee: the caller then falls back to `CAD_REPAIR_REGION_ATTEMPTS` exactly as it always did.

    AN EXPLICIT `attempts` STILL WINS, and is recorded as such. A caller that states a bound is
    not asking for a policy, and the corpus runner and the tests both state one.

    THE TIER COMES FROM THE RUN'S FIRST WHOLE-SHAPE SCAN and the UPLOAD's own counts, so the
    allowance is fixed once, before anything has moved: deciding it per attempt would let a
    package that changed the face count change the budget it is being judged under.

    NEVER RAISES, for `grind`'s own reason: an allowance must not break the repair it bounds. A
    predictor that somehow throws gets the cheapest tier's attempts.
    """
    if not budget_attempts_live():
        return {}
    if attempts is not None:
        return {"tier": "", "attempts": max(1, int(attempts)), "rule": BUDGET_RULE_EXPLICIT}
    try:
        tier = grind.tier_of(defects or (), solids=_count(solids), faces=_count(faces))
    except Exception:  # noqa: BLE001 - an allowance must never break the repair it bounds
        tier = grind.UNKNOWN
    return {"tier": str(tier), "attempts": max(1, grind.attempts_for(tier)),
            "rule": BUDGET_RULE_TIER}


def replay_record(package: TargetPackage, verdict: PackageVerdict, *,
                  tolerance_mm: float) -> dict:
    """What one COMMITTED package has to say about itself for an agent-driven run to be replayed.

    REPLAY MEANS RE-APPLYING THE RECORDED PACKAGES, not reproducing the bytes (spec pillar 4): an
    agent's proposal is not a function of the part, so the only reproducible thing about an agent
    run is what it decided. These are exactly the inputs `session.try_package` needs plus the
    verdict that stands in for the review, and nothing else - no model prose, no evidence blob.

    THE REGION TRAVELS AS ITS SORTED FINGERPRINTS, not as its index. An index is a position in one
    decomposition; a re-run decomposes the part itself, so the index may name a different region
    and a replay that trusted it would apply a package to the wrong geometry. Sorted, so two
    records of the same region are equal as written.
    """
    return {
        "region_fingerprints": sorted(package.region.fingerprints),
        "plan": [_plain_item(item) for item in package.plan],
        "predicate": {"cleared": sorted(package.predicate.cleared),
                      "reclassified": sorted(package.predicate.reclassified),
                      "unchanged": sorted(package.predicate.unchanged),
                      "max_area_delta_mm2": float(package.predicate.max_area_delta_mm2),
                      "max_volume_delta_mm3": float(package.predicate.max_volume_delta_mm3)},
        "tolerance_mm": float(tolerance_mm),
        "review_tier": verdict.review_tier,
        "reviewed": verdict.reviewed,
    }


def _plain_item(item: dict) -> dict:
    """One plan item as PLAIN DATA, so the record survives the JSON report it is delivered in.

    The live item holds a `RepairOp` and `EntityRef`s, which the report's `default=str` flattens
    into strings nothing can apply. So the operation travels by NAME, each target as its fields
    and the code as its value; `replay._package_of` resolves them back against the catalogue
    (PR #494 review, region_loop.py:503).
    """
    op, code = item.get("op"), item.get("defect_code")
    return {**item,
            "op": op if isinstance(op, str) else getattr(op, "name", str(op)),
            "targets": [{"entity_type": t.entity_type, "fingerprint": t.fingerprint,
                         "entity_index": int(t.entity_index), "ambiguous": bool(t.ambiguous)}
                        if isinstance(t, EntityRef) else t
                        for t in item.get("targets") or ()],
            "defect_code": getattr(code, "value", code)}


def _observe_shape(author: Author, shape, accounting=None, part_defects=None) -> None:
    """Tell the author the session's CURRENT shape before it is asked (final review M1).

    Read off the callable and optional, like `_observe_part`. An author that reads faces by index
    (the model author's observation) must read them from the shape the defects' indices belong to;
    a committed split or fragment renumbers faces, so the upload is the wrong shape after one.
    """
    observe = getattr(author, "observe_shape", None)
    if observe is not None:
        observe(shape)
    # AND THE DECLARATIONS THAT SHAPE CARRIES. An author that rehearses on a scratch clone of it
    # (the investigator, the tournament) opens a session whose caps compare against the UPLOAD,
    # so it needs the real session's accumulated declarations or every earlier package's declared
    # change reads as unexplained there. Immutable (`caps.Accounting`), so nothing an author does
    # with it reaches this session.
    seen = getattr(author, "observe_accounting", None)
    if seen is not None and accounting is not None:
        seen(accounting)
    # AND THE WHOLE PART'S MESH FAILURES, as codes: an author whose clone is seeded with one
    # region's defects cannot otherwise see that the part fails gmsh, and so cannot keep an edit
    # whose delivered part newly meshes (session._mesh_cure; Greptile PRRT_kwDOUFq_Ps656SQJ).
    # Read off the session's cached whole list - no scan is paid for it.
    told = getattr(author, "observe_mesh", None)
    if told is not None and part_defects is not None:
        from meshpipeline.cad.repair import mesh_probe as _mesh_probe

        told(frozenset(d.code.value for d in part_defects
                       if str(getattr(d.code, "value", d.code)).startswith("mesh_")
                       and d.code not in _mesh_probe.NO_ANSWER_CODES))


def run_regions(shape, *, author: Author, tolerance_mm: float,
                budget: LoopBudget | None = None, reserved: frozenset = frozenset(),
                session: RepairSession | None = None, attempts: int | None = None,
                clock: Callable[[], float] = time.monotonic,
                deadline_s: float | None = None,
                auto_accept_mm: float | None = None,
                focus_faces: tuple[int, ...] = ()) -> RegionRun:
    """Repair `shape` one region at a time, and report what each region's package did.

    `attempts` BOUNDS THE PACKAGES ONE REGION MAY BE OFFERED, defaulting to the deployment's
    CAD_REPAIR_REGION_ATTEMPTS. The run's `max_ops` and `timeout_s` are still the outer bounds and
    are checked before every attempt, not merely before every region.

    WITH `CAD_REPAIR_BUDGET_ATTEMPTS` LIVE the bound instead comes from what the PART is expected
    to cost a mesher (`attempt_budget`, keyed by `grind.tier_of` over the run's first whole-shape
    scan), and the run records which tier bought how many attempts. An explicit `attempts` still
    wins, and with the posture off nothing about this run differs by a byte.

    `deadline_s` IS THE RUN'S ONE ABSOLUTE DEADLINE, a `clock()` reading
    (`conservative.repair_step_file` takes it at entry, before the STEP is read, less a margin), so
    every slice of time - the probes', the author's, the walk's - is a slice of what the attempt
    node's clock actually has left. None, for a direct caller, is the old behaviour: the budget's
    `timeout_s` from when this function started. Given, the walk has the lesser of the two.

    `focus_faces` SCOPES THE WALK to the regions holding those 1-based kernel face indices; every
    other region is reported `NOT_IN_FOCUS` and left standing, unrun. `()` - what every caller but
    the mesher handback passes - is byte-for-byte the walk and the refusal list it was before.

    `session` and `clock` are injectable so the walk and the budget arithmetic can be tested
    without a kernel and without waiting. `reserved` reaches a session this function CONSTRUCTS;
    an INJECTED session keeps whatever holds it was built with, so a test that injects one is
    testing the walk and not the session half of the hold guarantee. Production never injects.

    SEQUENTIALLY, WORST REGION FIRST (`_walk_order`), and not because the work is coupled -
    regions are independent, which is the point of them. `RepairSession` owns ONE shape and snapshots it by
    reference, and concurrent `TopoDS_Shape` mutation is not something OpenCASCADE offers, so a
    parallel walk would need a session per region and a merge nobody has written. The saving in
    this design comes from the scans being SCOPED, not from them being concurrent; the order is
    fixed so that two identical runs produce identical reports.

    THE VERDICT COMES FROM ONE WHOLE-SHAPE SCAN AFTER THE MERGE, never from the per-region scoped
    scans. A scoped scan is exhaustive only inside its own region, so it can support a claim about
    that region and nothing about the part - and the part is what is being handed back.
    """
    limits = budget if budget is not None else LoopBudget.from_settings()
    # AT LEAST ONE, whatever a deployment or a caller says. A zero would turn the driver off by
    # misconfiguration rather than by the posture that exists to turn it off, and silently: every
    # region would be walked, authored for nothing and reported as exhausted.
    per_region = max(1, int(attempts if attempts is not None
                            else rcfg.CAD_REPAIR_REGION_ATTEMPTS))
    # THE CLOCK STARTS BEFORE THE SESSION EXISTS, so every scan's slice of the budget is a slice
    # of what this run actually has left, including the first scan.
    started = clock()

    def remaining() -> float:
        left = limits.timeout_s - (clock() - started)
        return left if deadline_s is None else min(left, deadline_s - clock())

    reserve = final_reserve_s()

    # `reserved` reaches the SESSION as well as the author: the author will not aim at a held
    # entity, and the session will not let a sweep remove one it never aimed at. Nothing here
    # grants a package the engineer's `answered_held` exemption - a package is authored, and an
    # answer to a hold is a person's (session.try_package).
    # `auto_accept_mm` REACHES A SESSION THIS FUNCTION CONSTRUCTS, exactly as `reserved` does: an
    # INJECTED session keeps the threshold it was built with, so a test that injects one is testing
    # the walk and not the tier. None - the default - is today's session: the auto-accept tier is
    # inert without a threshold, so a caller that declares none gets the reviewer on every package.
    work = session if session is not None else RepairSession(
        shape, remaining_s=remaining, reserved=reserved, clock=clock,
        auto_accept_mm=auto_accept_mm)
    if work.remaining_s is None:
        work.remaining_s = remaining
    # WHAT THE SESSION ITSELF COUNTED, not arithmetic over the verdicts. Every scan a package
    # takes is now scoped - each operation's own proof as well as the package's - and an
    # operation the caps refused before its re-inspection took none at all, so only the session
    # knows. Read as a delta because a caller may inject a session that has already scanned.
    scoped_at_start = work.scoped_scan_count

    # WHETHER THIS RUN KEEPS A PACKAGE LOG AT ALL, decided ONCE at entry rather than per package,
    # so a posture that changed mid-run cannot produce a half-written record.
    record_replay = (str(getattr(rcfg, "CAD_REPAIR_REGION_LOOP", "off") or "off") == "live"
                     and agent_posture_live())
    replays: list[dict] = []

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
            defects_before=before, defects_after=before,
            # NO FOCUS REFUSAL HERE: the scan never completed, so the part was never decomposed
            # and there are no regions a focus could have excluded.
            refusals=_refusals_of(author))

    # WHAT THE PART AS A WHOLE LOOKS LIKE, SAID ONCE, BEFORE IT IS CUT INTO REGIONS. The only
    # thing an author needs that a region cannot carry: the paired-rim guard (see `_observe_part`).
    # WHAT THIS PART IS WORTH SPENDING, FIXED HERE: after the one scan that can say what the part's
    # trouble is and before any region has been authored for. `{}` with the posture off, and then
    # `per_region` above - the deployment's flat `CAD_REPAIR_REGION_ATTEMPTS` - stands untouched.
    budget_record = attempt_budget(before, solids=work.original.solids,
                                   faces=work.original.faces, attempts=attempts)
    if budget_record:
        per_region = int(budget_record["attempts"])
    _observe_part(author, before)
    regions = decompose(before, diagonal_mm=work.original.diagonal_mm)
    # THE ORDER THE BUDGET IS SPENT IN, which is not the order the regions were found in. The
    # REPORT keeps `decompose`'s spatial order (it is the record of the decomposition, and a
    # region carries its own index, so nothing is lost); the WALK goes worst-first.
    # SCOPED TO THE FACES SOMETHING ELSE ALREADY NAMED, when a caller named any. `()` - every
    # caller but the mesher handback - is the walk `_walk_order` just produced, unchanged.
    walk, not_in_focus = focus_filter(_walk_order(regions, work.defects()), focus_faces)
    packages: list[PackageVerdict] = []
    escalated: list[int] = []
    committed = False
    # WHAT THE RUN'S OPERATION ALLOWANCE HAS BEEN SPENT ON, counted here rather than read off
    # `len(work.log)`, because the log counts every ATTEMPT and this budget bounds mutation work.
    # A PACKAGE THAT KEPT NOTHING DOES NOT CHARGE AGAINST IT. With per-operation proofs scoped,
    # a region an earlier package already cleared is only discovered by packaging it (see below),
    # and charging those discoveries would let a part whose regions were co-swept burn the whole
    # allowance on packages that changed nothing and stop with `op_budget` before reaching a
    # region that still has real defects. The run stays bounded regardless: `attempts` packages
    # per region, each package's own `PackageBudget` bounding the operations inside it, and this
    # allowance and the run's clock checked before every attempt.
    charged = len(work.log)
    # WALKED EVERY REGION unless a budget says otherwise; the loop runs out of regions rather than
    # of ideas, because a region is offered packages until one of them holds.
    stopped = REGIONS_EXHAUSTED
    # SET BY THE INNER LOOP WHEN THE RUN - not the region - IS OUT. A region's attempts ending is
    # ordinary; the job's deadline or its operation allowance ending stops the walk, and the flag
    # is what carries that out of the inner loop without the attempt bound and the run's bounds
    # sharing a break.
    out_of_budget = False

    for region in walk:
        # WHAT THIS REGION HAS ALREADY TRIED, which is the author's third argument. Per region and
        # not per run: a package that failed on this locus says nothing about the next one, and
        # regions are independent by construction (that is the point of them).
        tried: list[PackageVerdict] = []
        # WHAT THIS REGION'S OWN PROOF HAS ALREADY DISCHARGED. A committing package's predicate
        # was checked against the scoped re-inspection the commit rests on, so its `cleared` names
        # really are absent from the region - but that scoped list deliberately does NOT become
        # the session's whole-shape defect list (session._region_scan: a region's findings cannot
        # support a claim about the part), so `work.defects()` still reports them. Without this,
        # a region whose defect was repaired and proved gone would be authored for again until its
        # attempts ran out, every one of those packages declining on geometry that is no longer
        # there. `reclassified` joins it for the same reason it discharges a survivor in
        # `_abandoned`: relabelling is a discharge under spec decision 2.
        discharged: set[str] = set()
        # `escalated` IS THE QUEUE A PERSON WORKS THROUGH, so a region appears in it ONCE however
        # many of its attempts escalated - three entries would read as three regions of work.
        queued = False

        while True:
            if remaining() <= reserve:
                # THE WHOLE RUN, not one region and not one attempt. A per-region allowance is the
                # package's own (`PackageBudget.timeout_s`); this is the job's deadline, and the
                # regions already committed stay committed. CHECKED PER ATTEMPT, because an
                # attempt is the unit of work now - checking only per region would let one region
                # spend its whole bound past the deadline. `reserve` is the final verification's
                # share (`final_reserve_s`), 0 with the probe off.
                stopped, out_of_budget = TIME_BUDGET, True
                break
            if charged >= limits.max_ops:
                stopped, out_of_budget = OP_BUDGET, True
                break

            # THE REGION'S DEFECTS AS THEY ARE NOW, RE-READ BEFORE EVERY ATTEMPT, because a kept
            # operation changes them: the second package of a region must be authored against what
            # the first one left, or the author is answering a question that is no longer being
            # asked. "Now" is the last whole-shape scan, plus whatever a scoped proof has since
            # re-measured, minus what this region's own committed claims discharged - and that is
            # the price of locality.
            #
            # An earlier region's package can legitimately clear a later region's defects (a sew
            # joins the whole part, the sliver sweep walks it), and with every per-operation proof
            # scoped to its own region NOTHING HAS WALKED THIS ONE. So a region whose geometry is
            # already sound is not skipped for free: it is offered a package, and the package's own
            # operation declines on geometry that is no longer there. That costs an apply and one
            # scoped scan - paid in an apply rather than in a scan, which is the trade this driver
            # exists to make - and it keeps no operation, so it does not charge against the run's
            # allowance above.
            # NEXT PLAN: one scoped scan per region before authoring would skip such a region again
            # at no extra scan cost, and is scheduled as its own change because it alters what the
            # loop attempts.
            mine = tuple(d for d in work.defects()
                         if d.fingerprint in region.fingerprints
                         and d.fingerprint not in discharged)
            if not mine:
                # NOTHING OF THE REGION'S OWN IS LEFT, which is also how a region that committed
                # stops: the stop is "there is nothing to author for", never "an attempt was
                # spent". A region holding a second defect its first package did not claim is
                # therefore offered a second package, which is what a part with two holes in one
                # locus needs and what the baseline got for free from its whole-shape walk.
                break

            _observe_shape(author, work.shape, getattr(work, "accounting", None),
                           work.defects())
            package = author(region, mine, tuple(tried))
            if package is None:
                # NOT A FAILURE AND NOT AN ESCALATION. The author has nothing further to propose,
                # so the region is left exactly as it is and the final scan reports it. ASKED
                # AGAIN ONLY WHILE IT KEEPS ANSWERING: a `None` ends the region's attempts rather
                # than spending the rest of them on a question already answered - which for a
                # model author is also the rest of its calls.
                break
            if charged + len(package.plan) > limits.max_ops:
                # LOOKED AT BEFORE THE PACKAGE IS STARTED, not only after the log has grown past
                # the allowance. A package is executed whole or not at all, so a plan that cannot
                # fit in what the run has left would spend `package.budget.max_ops - 1` operations
                # past the run's own budget - and with a model author asking for a six-operation
                # package, the run budget would be whatever the author felt like. Nothing is tried
                # here, so the region is not escalated: the run simply ran out, which is what
                # `op_budget` says.
                stopped, out_of_budget = OP_BUDGET, True
                break

            offered = _with_tolerance(package, tolerance_mm)
            verdict = work.try_package(offered)
            packages.append(verdict)
            tried.append(verdict)
            if any(v.kept for v in verdict.verdicts):
                # THE WHOLE PLAN IS CHARGED THE MOMENT ANY OF IT WAS KEPT, including the operations
                # that rolled themselves back beside it: the package did real work on the shape,
                # and whether the package then survived its own predicate is not what this budget
                # bounds. A PACKAGE THAT KEPT NOTHING CHARGES NOTHING (Task 9's rule), which is
                # what keeps a region's retries from spending a later region's allowance on
                # attempts that changed no geometry at all.
                charged += len(verdict.verdicts)
            if verdict.committed:
                committed = True
                if record_replay:
                    # THE PACKAGE AS IT WAS OFFERED, with the run's tolerance already filled in,
                    # and only on a COMMIT: a reverted or escalated package left nothing behind,
                    # so replaying it would reproduce a decision the run itself threw away.
                    replays.append(replay_record(offered, verdict, tolerance_mm=tolerance_mm))
                discharged |= set(package.predicate.cleared) | set(verdict.reclassified)
                if not any(v.kept for v in verdict.verdicts):
                    # A PACKAGE THAT COMMITTED WITHOUT KEEPING ANYTHING ENDS THE REGION'S
                    # ATTEMPTS, and this is a cost rule rather than a judgement about the region.
                    # Such a package proved its claim on geometry it did not change: every
                    # operation in it declined or rolled itself back, which is how a region an
                    # earlier region's whole-shape sweep already cleared is discovered at all
                    # (session: a scoped proof deliberately does not become the session's
                    # whole-shape defect list, so `work.defects()` still reports what is gone).
                    # Authoring again would re-propose against the SAME geometry for the rest of
                    # the bound, buying an apply and a scoped scan per attempt to be told the
                    # same thing - and for a model author, a call each. What that leaves on the
                    # table is a second, real defect in the same region: it reaches the final
                    # whole-shape scan and the report, and the run is not called resolved for it.
                    # The fix is the one already scheduled above - one scoped scan per region
                    # BEFORE authoring - which is what makes a stale list distinguishable from a
                    # live one, and it changes what the loop attempts, so it is its own change.
                    break
            elif verdict.status == ESCALATED and not queued:
                escalated.append(int(region.index))
                queued = True
            if verdict.status == ESCALATED and verdict.reason.startswith(REVIEW_REJECTED):
                # THE REVIEWER'S CAP ENDS THE REGION (session: a second review reject, or the one
                # attempt after a first, escalates). Authoring again would only buy a model call
                # for a package the session will escalate unrun.
                break
            if len(tried) >= per_region:
                # THE REGION'S OWN BOUND. What is left in it reaches the final scan and the
                # report; the walk goes on to the next region, which is the whole reason this
                # driver exists beside the baseline, whose stall limit is global.
                break

        if out_of_budget:
            break

    if committed:
        # THE ONE WHOLE-SHAPE SCAN THE VERDICT RESTS ON, as thorough as the first inspection was,
        # and taken ONLY when something actually changed: a run that committed nothing is still
        # sitting on the list it started from, and re-measuring it would buy the same answer
        # twice. A sound part therefore costs exactly one scan.
        # `final`: its mesh probe may spend everything left (the reserve kept above).
        after = work._rescan(face_scan=True, final=True)  # noqa: SLF001 - the session's own scan
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
        defects_before=before, defects_after=after,
        # READ AT THE END, once the author has been asked everything it is going to be asked,
        # plus the regions a focus kept the author from being asked about at all - which a
        # customer owed an account of a partial repair has to be told, not have inferred.
        refusals=_refusals_of(author) + not_in_focus,
        attempt_budget=budget_record, packages_replay=tuple(replays))


def _timings(started: float, clock: Callable[[], float], session: RepairSession,
             packages: tuple[PackageVerdict, ...], verification_scans: int,
             scoped_scans: int) -> dict:
    """Where the run's time went, in the keys the report already carries plus the scoped ones.

    `scan_count` is every scan the session took, `verification_scans` the whole-shape ones this
    loop took to decide the run (the first, and the final one after the merge), and
    `scoped_scans` every scan that was scoped to one region - each operation's own proof as well
    as each package's. `measure_s` IS READ OFF THE PACKAGE, not off its operations: a package
    takes two whole-shape `caps.measure` calls of its own on top of the two each operation takes,
    and summing only the operations' would hide exactly the cost this driver added. The three together are what says whether scoping made a run cheaper, which
    is the claim this design has to be able to defend, and in this loop they account for each
    other exactly: `scan_count == verification_scans + scoped_scans`, because once a package owns
    an operation there is no whole-shape walk left between the run's first scan and its last.
    """
    return {"total_s": clock() - started, "scan_count": session.scan_count,
            "verification_scans": verification_scans, "scoped_scans": scoped_scans,
            "scan_s": sum(p.scan_s for p in packages),
            "apply_s": sum(p.apply_s for p in packages),
            "measure_s": sum(p.measure_s for p in packages)}


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
    # THE AUTHOR'S OWN REFUSALS TRAVEL FIRST, unchanged, because they are the records the
    # baseline's `RepairRun.refusals` carries and every reader downstream (the receipt, the
    # corpus's `refusals_by_reason`) already routes on their `reason`.
    refusals = run.refusals + tuple(
        {"refusal": "region_escalated", "region": int(verdict.region_index),
         "reason": verdict.reason}
        for verdict in run.packages if verdict.status == ESCALATED)
    return RepairRun(
        shape=run.shape, verdicts=tuple(verdicts), refusals=refusals,
        defects=run.defects_after, stopped_because=run.stopped_because,
        complete_scan=run.complete_scan, planner_label=planner_label,
        original=dict(original), final=dict(final),
        total_s=float(run.timings.get("total_s", 0.0)),
        scan_count=int(run.timings.get("scan_count", 0)))
