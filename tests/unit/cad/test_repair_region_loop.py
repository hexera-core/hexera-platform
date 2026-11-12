# Responsibility: Verify the region loop walks a part region by region - one target package per
#                 region, committed regions shipping while an escalated one queues, and ONE
#                 whole-shape scan after the merge deciding the run's verdict.
from __future__ import annotations

from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.region_loop import REGIONS_EXHAUSTED, run_regions
from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict, RepairSession

# NO KERNEL HERE. What is tested is the loop's own logic: the region walk, the budget arithmetic,
# which regions ship, which queue, and where the run's verdict comes from. The shape is a string
# that records the operations applied to it, the measurer and the scanner are doubles, and the
# clock only moves when the work being timed says it did.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)

_APPLY_S = 0.5
_SCAN_S = 1.0

#: The loop's tolerance, which no package in this file states - so what reaches an operation
#: proves the loop filled it in.
_TOLERANCE_MM = 0.1

#: Two defects 100 mm apart on a part whose diagonal is 200 mm. `decompose` merges boxes within
#: 1% of the diagonal (2 mm), so 100 mm apart is two regions and the fixture is only evidence of
#: anything if it stays that way.
_BOXES = {"a": (0.0, 1.0), "b": (100.0, 101.0)}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _defect(key: str, severity=DefectSeverity.error) -> EntityDefect:
    low, high = _BOXES[key]
    return EntityDefect(code=DefectCode.small_edge, severity=severity,
                        entity_type="edge", entity_index=1 if key == "a" else 2,
                        message=f"a short edge at {low}", fingerprint=f"edge:{key}",
                        location={"bbox_min": [low, low, low], "bbox_max": [high, high, high]})


class _Op:
    """Clears the one defect its key names, by returning a shape that records it was applied."""

    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, clock: _Clock, key: str) -> None:
        self.name = f"fix_{key}"
        self._clock = clock
        self._key = key
        #: What tolerance the operation was handed, so a test can prove where it came from.
        self.tolerances: list[float] = []

    def apply(self, shape, targets, *, tolerance_mm):
        self._clock.now += _APPLY_S
        self.tolerances.append(tolerance_mm)
        return OpOutcome(shape=f"{shape}+{self._key}", evidence={})


class _Part:
    """A part whose two defects are cleared by their own operations, one per region.

    The scanner answers about the SHAPE IT IS GIVEN - a defect is reported until the operation
    that clears it appears in the shape's name - so a rollback restoring the old handle restores
    the old defect list too, exactly as a real re-inspection would.
    """

    def __init__(self, *, keys: tuple[str, ...] = ("a", "b"),
                 incomplete_from: int | None = None,
                 severity=DefectSeverity.error) -> None:
        self.clock = _Clock()
        self.keys = keys
        self.scans = 0
        self.incomplete_from = incomplete_from
        self.severity = severity
        self.ops = {key: _Op(self.clock, key) for key in ("a", "b", "nothing")}

    def scanner(self, shape):
        self.clock.now += _SCAN_S
        self.scans += 1
        complete = self.incomplete_from is None or self.scans < self.incomplete_from
        found = [_defect(key, self.severity) for key in self.keys if f"+{key}" not in shape]
        return found, complete

    def measurer(self, shape):
        # Everything the caps read is unchanged; only a count they ignore moves, so a commit and a
        # rollback are both observable without any cap having an opinion.
        return Measure(**{**_SOUND.to_dict(), "vertices": 40 + str(shape).count("+")})

    def session(self) -> RepairSession:
        return RepairSession("original", measurer=self.measurer, scanner=self.scanner,
                             clock=self.clock)


def _author(part: _Part, *, escalate: tuple[int, ...] = (), decline: tuple[int, ...] = (),
            aim_elsewhere: tuple[int, ...] = (), plan_ops: int = 1):
    """One package per region: the operation that clears the region's defect, claiming exactly it.

    `escalate` gives a region a zero-operation budget, which is how a real package runs out of
    its allowance before it has applied anything (session: a plan larger than the budget is
    refused unapplied, status `escalated`). `decline` is the author with nothing to propose.
    `aim_elsewhere` plans an operation that clears nothing, so the claim fails and the package is
    reverted - an author that was tried and wrong, which is not the same as one that declined.
    """

    def author(region, defects):
        if region.index in decline:
            return None
        defect = defects[0]
        key = "nothing" if region.index in aim_elsewhere else defect.fingerprint.split(":")[1]
        predicate = Predicate(cleared=frozenset({defect.fingerprint}),
                              reclassified=frozenset(), unchanged=frozenset(),
                              max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
        # NO `tolerance_mm` IN THE PLAN ITEM, deliberately: the loop owns the run's tolerance.
        plan = tuple({"op": part.ops[key], "defect_code": DefectCode.small_edge,
                      "targets": (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                                            entity_index=defect.entity_index),)}
                     for _ in range(plan_ops))
        return TargetPackage(
            region=region, intent=f"clear {defect.fingerprint}", plan=plan, predicate=predicate,
            budget=PackageBudget(max_ops=0 if region.index in escalate else 4,
                                 timeout_s=1000.0, model_calls=0, deviation_mm=0.0))

    return author


def _budget(*, max_ops: int = 9, timeout_s: float = 1000.0) -> LoopBudget:
    return LoopBudget(max_ops=max_ops, stall_limit=9, timeout_s=timeout_s)


def _run(part: _Part, author, **budget):
    return run_regions("original", author=author, tolerance_mm=_TOLERANCE_MM,
                       budget=_budget(**budget), session=part.session(), clock=part.clock)


# THE BRIEF'S CASES


def test_a_sound_part_authors_no_packages_and_scans_once():
    # Review Focus: zero defects in, zero work done - and above all no second scan to prove it.
    part = _Part(keys=())
    run = _run(part, _author(part))

    assert run.packages == ()
    assert run.regions == ()
    assert run.timings["scan_count"] == 1
    assert run.stopped_because == "resolved"
    assert run.resolved is True


def test_each_region_gets_one_package():
    part = _Part()
    run = _run(part, _author(part))

    assert len(run.regions) == 2
    assert len(run.packages) == 2
    assert [p.region_index for p in run.packages] == [0, 1]
    assert [p.status for p in run.packages] == ["committed", "committed"]
    assert run.stopped_because == "resolved"
    assert run.defects_after == ()


def test_an_escalated_region_does_not_block_the_others():
    # THE FIRST region is starved, deliberately: starving the last one would pass just as well for
    # a loop that ended its walk on the first escalation, which is the property being tested.
    part = _Part()
    run = _run(part, _author(part, escalate=(0,)))

    assert run.escalated == (0,)
    assert [p.status for p in run.packages] == ["escalated", "committed"]
    # THE PRODUCT DECISION: region 1's repair is in the shape the run hands back, even though a
    # person now has to look at region 0.
    assert run.shape == "original+b"
    assert [d.fingerprint for d in run.defects_after] == ["edge:a"]
    assert run.stopped_because == "escalated"


def test_scoped_work_costs_fewer_whole_shape_verifications_than_one_per_operation():
    """The brief's inequality, measured on the scans the LOOP takes to decide the run's verdict.

    Its literal form - every scan the session took, against one per operation - cannot hold and
    says nothing about scoping: a re-inspection after each operation is what the progress rule
    needs and BOTH loops pay for one. What scoping buys is that neither an operation's proof nor
    a region's walks the whole part, so the whole-shape verification is paid twice for the RUN
    however many regions and operations it took.
    """
    part = _Part()
    run = _run(part, _author(part))

    assert run.timings["verification_scans"] == 2          # the first scan and the final one
    assert run.timings["verification_scans"] < 1 + sum(len(p.verdicts) for p in run.packages)
    # EVERY OTHER SCAN WAS SCOPED: one per operation, one per package, and nothing left over.
    assert run.timings["scoped_scans"] == (sum(len(p.verdicts) for p in run.packages)
                                           + len(run.packages))
    assert run.timings["scan_count"] == (run.timings["verification_scans"]
                                         + run.timings["scoped_scans"])


# WHAT THE BRIEF LEAVES TO THIS TASK


def test_an_incomplete_first_scan_stops_before_any_region_is_touched():
    part = _Part(incomplete_from=1)
    run = _run(part, _author(part))

    assert run.stopped_because == "scan_incomplete"
    assert run.complete_scan is False
    assert run.regions == () and run.packages == ()
    assert run.shape == "original"


def test_an_incomplete_final_scan_dominates_a_committed_region():
    # The run repaired what it aimed at; the scan that would prove it stopped early, so no
    # absence can be claimed and the reason the caller gets is that we could not see.
    part = _Part(keys=("a",), incomplete_from=4)
    run = _run(part, _author(part))

    assert [p.status for p in run.packages] == ["committed"]
    assert run.timings["verification_scans"] == 2
    assert run.complete_scan is False
    assert run.stopped_because == "scan_incomplete"


def test_a_region_the_author_proposes_nothing_for_is_left_alone():
    part = _Part()
    run = _run(part, _author(part, decline=(1,)))

    assert len(run.regions) == 2
    assert [p.region_index for p in run.packages] == [0]
    assert run.escalated == ()
    # NOT A FAILURE AND NOT AN ESCALATION: nothing was proposed, so nothing was tried. The
    # region's defect is still there, which is what stops the run being called resolved.
    assert [d.fingerprint for d in run.defects_after] == ["edge:b"]
    assert run.stopped_because == REGIONS_EXHAUSTED


def test_the_loop_hands_each_operation_the_run_s_tolerance():
    part = _Part(keys=("a",))
    _run(part, _author(part))

    assert part.ops["a"].tolerances == [_TOLERANCE_MM]


def test_the_time_budget_ends_the_walk_and_what_committed_still_ships():
    # The first scan costs 1.0 s and region 0's package 2.5 s more, so a 3.0 s run is out of time
    # when it reaches region 1 - and region 0's commit is not unwound by that.
    part = _Part()
    run = _run(part, _author(part), timeout_s=3.0)

    assert [p.region_index for p in run.packages] == [0]
    assert run.shape == "original+a"
    assert run.stopped_because == "time_budget"


def test_the_operation_budget_ends_the_walk():
    part = _Part()
    run = _run(part, _author(part), max_ops=1)

    assert [p.region_index for p in run.packages] == [0]
    assert run.stopped_because == "op_budget"


def test_the_run_reports_both_scans_so_a_disposition_can_be_computed():
    # Task 8's ledger needs the upload's defects AND the survivors; the region loop is the only
    # thing that holds both, so it reports both rather than only what is left.
    part = _Part()
    run = _run(part, _author(part, decline=(0, 1)))

    assert [d.fingerprint for d in run.defects_before] == ["edge:a", "edge:b"]
    assert [d.fingerprint for d in run.defects_after] == ["edge:a", "edge:b"]
    assert run.defects_before[0].measurements == run.defects_after[0].measurements


def test_the_report_carries_the_regions_the_packages_and_why_it_stopped():
    part = _Part()
    run = _run(part, _author(part, escalate=(1,)))
    out = run.to_dict()

    assert out["stopped_because"] == "escalated"
    assert out["resolved"] is False
    assert out["escalated"] == [1]
    assert [r["index"] for r in out["regions"]] == [0, 1]
    assert [p["status"] for p in out["packages"]] == ["committed", "escalated"]
    assert out["complete_scan"] is True


# THE DETERMINISTIC AUTHOR, which is the thing a model later replaces. Kept beside the loop's own
# tests because the two are one seam: what the loop does with a package, and what the rules put in
# one. The planner is a stub here - the choosing rules have their own tests in test_repair_plan.py.


def _region(*fingerprints: str, index: int = 0, located: bool = True):
    from meshpipeline.cad.repair.regions import Region

    return Region(index=index, fingerprints=frozenset(fingerprints),
                  bbox_min=(0.0, 0.0, 0.0) if located else None,
                  bbox_max=(1.0, 1.0, 1.0) if located else None, located=located)


class _Planner:
    """Proposes the candidate it was given, and records what it was asked about."""

    label = "stub"

    def __init__(self, candidate=None) -> None:
        self._candidate = candidate
        self.asked: list[tuple] = []

    def propose(self, defects, history, *, shape):
        self.asked.append((tuple(d.fingerprint for d in defects), shape))
        return self._candidate


def _candidate(fingerprint: str, op):
    from meshpipeline.cad.repair.plan import Candidate

    return Candidate(op=op, defect_code=DefectCode.small_edge,
                     defect_fingerprint=fingerprint,
                     targets=(EntityRef(entity_type="edge", fingerprint=fingerprint,
                                        entity_index=1),))


def _rules_author(candidate, **kwargs):
    from meshpipeline.cad.repair.rules_author import rules_author

    planner = _Planner(candidate)
    return rules_author("original", original=_SOUND, planner=planner, **kwargs), planner


def test_the_rules_author_claims_exactly_the_defect_its_operation_aims_at():
    op = _Op(_Clock(), "a")
    author, planner = _rules_author(_candidate("edge:a", op))

    package = author(_region("edge:a", "face:z"), (_defect("a"),))

    assert package is not None
    assert package.predicate.cleared == frozenset({"edge:a"})
    assert package.predicate.reclassified == frozenset()
    # NOTHING PROMISED ABOUT UNTOUCHED NEIGHBOURS: `unchanged` fails in both directions, and these
    # rules cannot predict what a kernel operation legitimately clears beside its target.
    assert package.predicate.unchanged == frozenset()
    assert [item["op"] for item in package.plan] == [op]
    assert "tolerance_mm" not in package.plan[0]
    assert package.budget.max_ops == 1
    assert planner.asked == [(("edge:a",), "original")]


def test_the_rules_author_bounds_its_claim_by_the_deployment_s_own_residual_caps():
    import meshpipeline.settings.cad_repair as rcfg

    author, _ = _rules_author(_candidate("edge:a", _Op(_Clock(), "a")))
    package = author(_region("edge:a"), (_defect("a"),))

    assert package is not None
    assert package.predicate.max_area_delta_mm2 == (
        rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO * _SOUND.surface_area_mm2)
    assert package.predicate.max_volume_delta_mm3 == (
        rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO * _SOUND.volume_mm3)


def test_the_rules_author_proposes_nothing_for_an_unlocated_region():
    author, planner = _rules_author(_candidate("edge:a", _Op(_Clock(), "a")))

    assert author(_region("", index=1, located=False), (_defect("a"),)) is None
    # NOT EVEN ASKED: there is no locus to scope a proof to, so there is nothing to choose.
    assert planner.asked == []


def test_the_rules_author_proposes_nothing_when_no_operation_claims_the_region():
    author, _ = _rules_author(None)

    assert author(_region("edge:a"), (_defect("a"),)) is None


def test_the_rules_author_refuses_a_claim_the_region_s_own_scan_could_not_verify():
    author, _ = _rules_author(_candidate("edge:elsewhere", _Op(_Clock(), "a")))

    assert author(_region("edge:a"), (_defect("a"),)) is None


def test_a_region_whose_package_was_tried_and_reverted_is_not_called_resolved():
    """`resolved` MEANS THE RUN SUCCEEDED, not that no error-severity defect remains.

    The two differ exactly here: a warning the loop gave up on. Calling this resolved would be
    read downstream as "repaired", which is the same ruling `loop._abandoned` makes per operation.
    """
    part = _Part(keys=("a",), severity=DefectSeverity.warning)
    run = _run(part, _author(part, aim_elsewhere=(0,)))

    assert [p.status for p in run.packages] == ["reverted"]
    assert [d.fingerprint for d in run.defects_after] == ["edge:a"]
    assert run.stopped_because == REGIONS_EXHAUSTED
    assert run.resolved is False


def test_an_operation_discarded_with_its_package_is_not_reported_as_kept():
    """What the stages after the loop read, and why the restamp matters.

    The after-checks are handed the DECLARATIONS of every kept operation. An operation kept inside
    a package that was then thrown away would hand them a declaration for geometry the result no
    longer contains - which is how one discarded package refuses the next legitimate operation.
    """
    from meshpipeline.cad.repair.region_loop import as_repair_run

    part = _Part()
    run = _run(part, _author(part, aim_elsewhere=(1,)))
    bridged = as_repair_run(run, original=_SOUND.to_dict(), final=_SOUND.to_dict(),
                            planner_label="regions-rules-v1")

    assert [p.status for p in run.packages] == ["committed", "reverted"]
    assert [v.kept for v in bridged.verdicts] == [True, False]
    assert "discarded_with_package" in bridged.verdicts[1].reason
    assert bridged.stopped_because == run.stopped_because
    assert bridged.defects == run.defects_after
    assert bridged.scan_count == run.timings["scan_count"]


def test_an_escalated_region_reaches_the_report_as_a_refusal():
    from meshpipeline.cad.repair.region_loop import as_repair_run

    part = _Part()
    run = _run(part, _author(part, escalate=(1,)))
    bridged = as_repair_run(run, original=_SOUND.to_dict(), final=_SOUND.to_dict())

    assert [r["refusal"] for r in bridged.refusals] == ["region_escalated"]
    assert bridged.refusals[0]["region"] == 1
    assert "budget" in bridged.refusals[0]["reason"]


# THE TWO PARTICULARS OF `_abandoned`, PORTED FROM THE BASELINE LOOP. They exist so that a
# difference between the two drivers on one fixture is attributable to how the packages were
# authored - which is the only reason `loop.py` is kept alive at all.


def test_a_defect_a_discarded_package_never_aimed_at_does_not_block_resolved():
    """A region is an over-merged cluster, so a package that failed on one edge says nothing
    about a warning at the far end of the same box. `loop._abandoned` marks what an operation
    TARGETED; marking the whole region would fail runs the baseline calls resolved."""
    from meshpipeline.cad.repair.region_loop import _abandoned

    verdict = PackageVerdict(
        region_index=0, status="reverted", reason="cleared defects still present: edge:a",
        verdicts=(OpVerdict(op="fix_a", targets=("edge:a",), defect_code="small_edge",
                            status="rolled_back"),))

    assert _abandoned((verdict,), (_defect("a", DefectSeverity.warning),)) is True
    # `edge:b` is in the same region in this fixture's geometry, and nothing aimed at it.
    assert _abandoned((verdict,), (_defect("b", DefectSeverity.warning),)) is False


def test_a_rolled_back_heal_abandons_nothing():
    """Floor-fix ruling 2, as `loop._abandoned` applies it: the heal aims at the analyzer's
    crossing and its partner faces, so a partner's unrelated warning must not read as abandoned
    because the heal could not re-trim the tangle. A crossing the status walk also reported is an
    error, and `_errors_remain` still catches that."""
    from meshpipeline.cad.repair.region_loop import _abandoned
    from meshpipeline.cad.repair.session import HEAL_OP

    healed = PackageVerdict(
        region_index=0, status="reverted", reason="cleared defects still present: edge:a",
        verdicts=(OpVerdict(op=HEAL_OP, targets=("edge:a",), defect_code="self_intersection",
                            status="rolled_back"),))

    assert _abandoned((healed,), (_defect("a", DefectSeverity.warning),)) is False


def test_a_package_too_large_for_what_the_run_has_left_is_not_started():
    """The run's allowance is the RUN'S. A package is executed whole or not at all, so a plan that
    cannot fit in what is left would spend the author's allowance rather than the loop's."""
    part = _Part(keys=("a",))
    run = _run(part, _author(part, plan_ops=3), max_ops=2)

    assert run.packages == ()
    assert part.ops["a"].tolerances == []          # nothing was applied
    assert run.stopped_because == "op_budget"
