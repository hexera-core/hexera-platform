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
#: `z` sits 1 mm from `a`, INSIDE that 2 mm merge radius, so `a` and `z` are ONE region. That is
#: what the two-defects-in-one-region probes below need, and it is only evidence of anything while
#: `a`/`b` stay apart and `a`/`z` stay together. No key may be a prefix of another: the double part
#: below reads "has this operation been applied" out of the shape's name.
_BOXES = {"a": (0.0, 1.0), "b": (100.0, 101.0), "z": (1.5, 2.5)}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _defect(key: str, severity=DefectSeverity.error) -> EntityDefect:
    low, high = _BOXES[key]
    return EntityDefect(code=DefectCode.small_edge, severity=severity,
                        entity_type="edge", entity_index={"a": 1, "b": 2, "z": 3}[key],
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
                 severity=DefectSeverity.error, measure_s: float = 0.0) -> None:
        self.clock = _Clock()
        self.keys = keys
        self.scans = 0
        self.incomplete_from = incomplete_from
        self.severity = severity
        #: What one `caps.measure` costs. ZERO BY DEFAULT, so the budget fixtures above keep
        #: measuring only the scans and applies they were written around; a test that is about
        #: where the measure time is reported asks for a price.
        self.measure_s = measure_s
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
        self.clock.now += self.measure_s
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

    def author(region, defects, attempts):
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


def _run(part: _Part, author, *, attempts: int | None = None, **budget):
    return run_regions("original", author=author, tolerance_mm=_TOLERANCE_MM,
                       budget=_budget(**budget), session=part.session(), clock=part.clock,
                       attempts=attempts)


# THE AUTHORS THAT NEED, OR REFUSE, A SECOND PACKAGE. Every one of them is STATELESS and reads
# `attempts` instead: that is the seam this task exists to create, and a double that kept its own
# counter would pass while the loop told the author nothing.


def _author_needing_two_tries(part: _Part):
    """Wrong on a region's FIRST package and right on its second - the baseline's own sequence.

    The first package aims at an operation that clears nothing, so the claim fails and the
    package is reverted; told that, the author proposes the operation that works. This is the
    whole behaviour the operation-at-a-time loop got from a `RunHistory` that outlived one
    iteration, and the region driver had no way to express.
    """
    wrong, right = _author(part, aim_elsewhere=(0, 1)), _author(part)

    def author(region, defects, attempts):
        return (right if attempts else wrong)(region, defects, attempts)

    return author


def _author_that_is_always_wrong(part: _Part):
    """Every package aims at an operation that clears nothing, so every one is reverted."""
    return _author(part, aim_elsewhere=(0, 1))


def _author_that_always_escalates(part: _Part):
    """Every package asks for more operations than its own budget allows, so every one escalates
    unapplied - a region that really cannot be finished inside its allowance."""
    return _author(part, escalate=(0, 1))


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
    # ATTEMPTS PINNED TO ONE: this probe is about the walk carrying on past an escalation, and
    # region 0's package escalates unapplied, so with the real bound it would escalate three
    # times over. Retry has its own tests below.
    run = _run(part, _author(part, escalate=(0,)), attempts=1)

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


def test_two_executed_operations_in_one_package_cost_two_scoped_scans():
    """N operations inside ONE package cost N scoped scans, and no whole-shape walk at all.

    THE INSIDE-A-PACKAGE FORM OF THE WHOLE THESIS, which a one-operation plan cannot show: with
    a single operation per package, "one scoped scan per operation" and "one scoped scan per
    package" are the same number. Both operations here aim at a defect their operation does not
    clear, so each is really applied and really re-inspected - `target_remains` is the proof that
    the re-inspection happened - and each of those re-inspections is scoped.
    """
    part = _Part()
    run = _run(part, _author(part, aim_elsewhere=(0, 1), plan_ops=2), attempts=1)

    verdicts = [v for p in run.packages for v in p.verdicts]
    assert [v.reason for v in verdicts] == ["target_remains"] * 4
    assert [p.status for p in run.packages] == ["reverted", "reverted"]
    # Two operations and one proof per package, and nothing left over.
    assert run.timings["scoped_scans"] == 6
    assert run.timings["scoped_scans"] == (sum(len(p.verdicts) for p in run.packages)
                                           + len(run.packages))
    # ONE whole-shape scan for the run: the first. Nothing committed, so nothing to re-verify.
    assert run.timings["verification_scans"] == 1
    assert run.timings["scan_count"] == 7


def test_a_package_that_kept_nothing_does_not_spend_the_runs_operation_allowance():
    """A region discovered to be sound must not cost a later region its package.

    Once every per-operation proof is scoped, a region an earlier package already cleared is only
    discovered by packaging it and having its operation decline. Charging those attempts against
    the RUN's allowance - which counts attempts, not keeps - would let a part whose regions were
    co-swept (the sliver sweep is a whole-shape walk) burn the whole budget on packages that
    changed nothing and stop with `op_budget` before reaching a region with real defects.

    Region 0's plan is aimed at a defect its operation does not clear, so the package keeps
    nothing and is reverted. With an allowance of ONE operation, region 1 must still get its own.
    """
    part = _Part()
    run = _run(part, _author(part, aim_elsewhere=(0,)), max_ops=1, attempts=1)

    assert [(p.region_index, p.status) for p in run.packages] == [(0, "reverted"),
                                                                   (1, "committed")]
    assert run.stopped_because != "op_budget"
    assert run.shape == "original+b"          # region 1's repair really did happen


def test_a_package_that_kept_something_still_spends_it():
    # The other half: restoring the allowance for a package that changed nothing must not become
    # "never charge anything", or a model author would have an unbounded run.
    part = _Part()
    run = _run(part, _author(part), max_ops=1)

    assert [p.status for p in run.packages] == ["committed"]
    assert run.stopped_because == "op_budget"


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
    run = _run(part, _author(part, escalate=(1,)), attempts=1)
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

    package = author(_region("edge:a", "face:z"), (_defect("a"),), ())

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
    package = author(_region("edge:a"), (_defect("a"),), ())

    assert package is not None
    assert package.predicate.max_area_delta_mm2 == (
        rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO * _SOUND.surface_area_mm2)
    assert package.predicate.max_volume_delta_mm3 == (
        rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO * _SOUND.volume_mm3)


def test_the_rules_author_proposes_nothing_for_an_unlocated_region():
    author, planner = _rules_author(_candidate("edge:a", _Op(_Clock(), "a")))

    assert author(_region("", index=1, located=False), (_defect("a"),), ()) is None
    # NOT EVEN ASKED: there is no locus to scope a proof to, so there is nothing to choose.
    assert planner.asked == []


def test_the_rules_author_proposes_nothing_when_no_operation_claims_the_region():
    author, _ = _rules_author(None)

    assert author(_region("edge:a"), (_defect("a"),), ()) is None


def test_the_rules_author_refuses_a_claim_the_region_s_own_scan_could_not_verify():
    author, _ = _rules_author(_candidate("edge:elsewhere", _Op(_Clock(), "a")))

    assert author(_region("edge:a"), (_defect("a"),), ()) is None


def test_a_region_whose_package_was_tried_and_reverted_is_not_called_resolved():
    """`resolved` MEANS THE RUN SUCCEEDED, not that no error-severity defect remains.

    The two differ exactly here: a warning the loop gave up on. Calling this resolved would be
    read downstream as "repaired", which is the same ruling `loop._abandoned` makes per operation.
    """
    part = _Part(keys=("a",), severity=DefectSeverity.warning)
    run = _run(part, _author(part, aim_elsewhere=(0,)), attempts=1)

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
    run = _run(part, _author(part, aim_elsewhere=(1,)), attempts=1)
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
    run = _run(part, _author(part, escalate=(1,)), attempts=1)
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


# `_abandoned` KEYS ON THE OPERATION, AS THE BASELINE DOES. The two probes below are run through
# the whole loop rather than against the helper, because what was wrong was not the predicate but
# which log the predicate was read off: a package-level key makes a failed operation inside a
# COMMITTED package invisible, and the resulting divergence from `loop.py` flatters this driver on
# exactly the fixtures a benchmark would score.


def _mixed_package(part, *, reclassify: bool):
    """ONE package over ONE region: operation A clears `edge:a`, operation B fails on `edge:z`.

    The predicate claims only what A does, so the package COMMITS with a rolled-back operation
    inside it - the shape this file could not otherwise produce, and the only shape in which a
    package-level key and an operation-level key disagree.
    """

    def author(region, defects, attempts):
        cleared = frozenset({"edge:a"})
        predicate = Predicate(
            cleared=cleared,
            reclassified=frozenset({"edge:z"}) if reclassify else frozenset(),
            unchanged=frozenset(), max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
        plan = ({"op": part.ops["a"], "defect_code": DefectCode.small_edge,
                 "targets": (EntityRef(entity_type="edge", fingerprint="edge:a",
                                       entity_index=1),)},
                {"op": part.ops["nothing"], "defect_code": DefectCode.small_edge,
                 "targets": (EntityRef(entity_type="edge", fingerprint="edge:z",
                                       entity_index=3),)})
        return TargetPackage(region=region, intent="clear edge:a", plan=plan,
                             predicate=predicate,
                             budget=PackageBudget(max_ops=4, timeout_s=1000.0, model_calls=0,
                                                  deviation_mm=0.0))

    return author


def test_an_operation_that_failed_inside_a_committed_package_still_blocks_resolved():
    """THE CRITICAL. `loop._abandoned` reads `not v.kept` off the session log, so an attempt that
    failed on a warning marks that fingerprint whatever the run did next. A package-level key
    loses it: the package committed, so its rolled-back operation contributes nothing, and the
    customer is told "Repaired within the conservative caps" about geometry the run tried and
    failed on. The baseline, handed this identical operation log, reports `_abandoned` and
    summarises a partial repair - and a silent divergence in the direction that flatters this
    driver is what the retained `loop.py` exists to catch (spec anti-goal: losing attribution).
    """
    part = _Part(keys=("a", "z"), severity=DefectSeverity.warning)
    # ATTEMPTS PINNED TO ONE: the package commits while leaving `edge:z` unclaimed, so with the
    # real bound the region is legitimately offered another package for it. What this probe is
    # about is the ONE package's log, and `_abandoned`'s reading of it.
    run = _run(part, _mixed_package(part, reclassify=False), attempts=1)

    # The shape the probe needs: one region, one committed package, one failed operation in it.
    assert len(run.regions) == 1
    assert [p.status for p in run.packages] == ["committed"]
    assert [v.status for v in run.packages[0].verdicts] == ["kept", "rolled_back"]
    assert run.packages[0].verdicts[1].reason == "target_remains"
    assert [d.fingerprint for d in run.defects_after] == ["edge:z"]

    # And the ruling: a warning this run tried and failed on is not a repaired part.
    assert run.stopped_because == REGIONS_EXHAUSTED
    assert run.resolved is False


def test_a_reclassified_survivor_does_not_block_resolved():
    """THE CONVERSE, and the one legitimate discharge. Reclassification is spec decision 2: the
    defect remains in the report with its rationale, explained rather than hidden, so a survivor
    the committing package's predicate reclassified must not read as abandoned. `reclassified` on
    the verdict is already the intersection of the claim with what the scoped scan still found,
    so this credits relabelling that really discharged something.
    """
    part = _Part(keys=("a", "z"), severity=DefectSeverity.warning)
    run = _run(part, _mixed_package(part, reclassify=True))

    assert [p.status for p in run.packages] == ["committed"]
    assert run.packages[0].reclassified == frozenset({"edge:z"})
    assert [d.fingerprint for d in run.defects_after] == ["edge:z"]
    assert run.stopped_because == "resolved"
    assert run.resolved is True


def test_a_kept_operation_discarded_with_its_package_abandons_its_targets():
    """Every operation of a non-committed package was discarded whole, so a `kept` verdict inside
    one left nothing behind either - the same reading `as_repair_run` restamps the log with."""
    from meshpipeline.cad.repair.region_loop import _abandoned

    discarded = PackageVerdict(
        region_index=0, status="reverted", reason="volume moved",
        verdicts=(OpVerdict(op="fix_a", targets=("edge:a",), defect_code="small_edge",
                            status="kept"),))

    assert _abandoned((discarded,), (_defect("a", DefectSeverity.warning),)) is True


def test_a_packages_own_whole_shape_measures_are_timed_and_reported():
    """`caps.measure` IS THE SECOND COST CENTRE THE SPEC NAMES, beside the scan, and a package
    adds two more whole-shape calls on top of the two every operation takes. Both bypassed
    `_timed_measure`, so they appeared in no timing field at all and the instrumentation built to
    make that cost visible could not see the cost this driver introduced. Scoping `caps.measure`
    itself is a later plan's; being honest about what it costs is this one's.
    """
    import pytest

    price = 0.25
    part = _Part(keys=("a", "z"), severity=DefectSeverity.warning, measure_s=price)
    # ATTEMPTS PINNED TO ONE, so `run.timings` totals exactly the one package this probe prices;
    # the region's unclaimed `edge:z` would otherwise earn it a second, legitimately.
    run = _run(part, _mixed_package(part, reclassify=False), attempts=1)

    package = run.packages[0]
    operations_s = sum(v.measure_s for v in package.verdicts)
    assert operations_s > 0.0                       # the operations' own measures still counted
    # ITS OWN TWO, ON TOP OF ITS OPERATIONS'. The verdict is where a reader gets a region's whole
    # cost without re-deriving it from the op log, which cannot contain these two.
    assert package.measure_s == pytest.approx(operations_s + 2 * price)
    # AND THE RUN'S REPORT SHOWS THE TRUE TOTAL, which summing the operations alone would not.
    assert run.timings["measure_s"] == pytest.approx(package.measure_s)
    assert run.timings["measure_s"] > operations_s
    assert package.to_dict()["measure_s"] == package.measure_s


def test_a_reclassifying_package_reports_a_live_reclassified_key():
    """ANTI-GOAL 1'S OWN MITIGATION, which read empty whatever happened: `ledger` was never handed
    a reclassification, so the one number saying how much of a clean report came from relabelling
    was always `[]`. Clean-by-relabelling is reachable - one genuinely kept operation beside a
    predicate that relabels the rest delivers an artefact and reports `resolved` - so the rate
    has to be visible in the run's own report.
    """
    from dataclasses import replace

    from meshpipeline.cad.repair.conservative import _disposition

    part = _Part(keys=("a", "z"), severity=DefectSeverity.warning)
    run = _run(part, _mixed_package(part, reclassify=True))

    out = _disposition(run)
    assert out["reclassified"] == ["edge:z"]
    assert out["resolved"] == ["edge:a"]
    assert out["inherent"] == ["edge:z"]            # it survived, explained rather than hidden

    # A REVERTED PACKAGE IS NOT CREDITED: its geometry and its claim were both thrown away, so
    # nothing it named was discharged.
    reverted = replace(run, packages=(replace(run.packages[0], status="reverted"),))
    assert _disposition(reverted)["reclassified"] == []


# SEVERAL PACKAGES PER REGION, AND AN AUTHOR THAT SEES WHAT FAILED


def test_a_region_gets_a_second_package_when_its_first_is_reverted():
    """THE POINT OF THE TASK. One package per region was one guess per region.

    The attempts cannot be packed into one longer plan instead: a predicate is stated BEFORE the
    plan runs, so a plan of k speculative attempts has to claim all k and one failure reverts the
    operations that succeeded. Retry therefore happens ACROSS packages, which is this walk.
    """
    part = _Part(keys=("a",))
    run = _run(part, _author_needing_two_tries(part))

    assert [p.status for p in run.packages] == ["reverted", "committed"]
    assert [p.region_index for p in run.packages] == [0, 0]
    # The second package's repair really did happen, and the run is not called partial for the
    # first one: the defect its predecessor failed on is gone.
    assert run.shape == "original+a"
    assert run.defects_after == ()
    assert run.stopped_because == "resolved"


def test_the_author_is_told_what_already_failed_in_this_region():
    """THE SEAM, and the reason it is `(region, defects, attempts)` rather than a counter.

    A model author revises through exactly this channel - it is handed the verdict its last claim
    earned, in words - so the walk must pass the VERDICTS and not merely how many there were.
    """
    part = _Part(keys=("a",))
    seen: list[tuple] = []
    chosen = _author_needing_two_tries(part)

    def spy(region, defects, attempts):
        seen.append(tuple(v.reason for v in attempts))
        return chosen(region, defects, attempts)

    _run(part, spy)

    # NOTHING HAS BEEN TRIED YET on the first call, which is what makes the first package of a
    # region byte-for-byte what the one-package-per-region walk authored.
    assert seen[0] == ()
    assert len(seen) == 2
    # IN WORDS, and the words are the predicate's own diagnosis: the first package's operation
    # cleared nothing and rolled itself back, so what the author is told is that the package kept
    # nothing - which is the revision signal, not a count.
    assert "no operation was kept" in seen[1][0]
    assert "edge:a" in seen[1][0]


def test_the_attempt_bound_is_honoured():
    part = _Part()
    run = _run(part, _author_that_is_always_wrong(part), attempts=2)

    assert len([p for p in run.packages if p.region_index == 0]) == 2
    # AND THE WALK GOES ON. A region that spent its attempts does not end the run: the bound is
    # per region, and the next region gets its own.
    assert len([p for p in run.packages if p.region_index == 1]) == 2
    assert run.stopped_because == REGIONS_EXHAUSTED


def test_a_region_is_escalated_once_however_many_attempts_it_took():
    part = _Part(keys=("a",))
    run = _run(part, _author_that_always_escalates(part), attempts=3)

    assert len(run.packages) == 3
    assert [p.status for p in run.packages] == ["escalated"] * 3
    # ONE ENTRY. `escalated` IS THE QUEUE a person works through, and a region listed three times
    # is one region's worth of work read as three.
    assert run.escalated == (0,)
    assert run.escalated.count(0) == 1


def test_a_committed_region_is_not_offered_another_package():
    """A second package is offered because a defect of the region's OWN remains, never because an
    attempt is left. A region whose claim was proved has nothing left to author for."""
    part = _Part()
    run = _run(part, _author(part), attempts=3)

    assert len(run.packages) == len(run.regions) == 2
    assert [p.status for p in run.packages] == ["committed", "committed"]


def test_a_region_whose_author_runs_dry_stops_before_its_attempts_do():
    # `None` IS NOT A FAILURE: the author has nothing further to propose, so asking again would
    # buy the same answer at the price of another call - and for a model author, another call.
    part = _Part(keys=("a",))
    calls: list[int] = []

    def author(region, defects, attempts):
        calls.append(len(attempts))
        return _author(part, aim_elsewhere=(0,))(region, defects, attempts) if not attempts \
            else None

    run = _run(part, author, attempts=3)

    assert calls == [0, 1]
    assert [p.status for p in run.packages] == ["reverted"]


def test_the_attempts_a_region_spends_do_not_charge_the_run_for_work_it_did_not_keep():
    """Task 9's rule, which the inner loop must not regress: a package that kept nothing charges
    nothing against the run's operation allowance, however many of them a region authored."""
    part = _Part()
    run = _run(part, _author_that_is_always_wrong(part), attempts=3, max_ops=1)

    assert len(run.packages) == 6                 # three attempts in each of two regions
    assert run.stopped_because != "op_budget"


def test_the_run_s_time_budget_ends_a_region_s_attempts():
    # The first scan costs 1.0 s and each attempt 2.5 s more, so a 3.5 s run gets region 0's
    # first attempt and is out of time before its second. The bound inside a region is not
    # allowed to outlive the job's deadline.
    part = _Part()
    run = _run(part, _author_that_is_always_wrong(part), attempts=3, timeout_s=3.5)

    assert [p.region_index for p in run.packages] == [0]
    assert run.stopped_because == "time_budget"


# A DECLINE IS NOT A FAILED ATTEMPT, the baseline's own ruling at its `declined` branch. It became
# load-bearing when `fix_small_faces` learned to decline a sliver that is sliver-sized by area but
# too wide for the fixer to find: the whole corpus of such parts was reported `regions_exhausted`
# for defects nothing had attempted.


class _DecliningOp:
    """Judges its aim and runs nothing, handing the input shape straight back - `fix_small_faces`
    on a face that is a sliver by area and no strip by width, in miniature."""

    answers = frozenset({DefectCode.small_edge})
    scope = 1
    name = "fix_declining"

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape=shape,
                         evidence={"declined": "too_wide_to_find (4 mm across)"})


def _declining_author(part: _Part):
    """One package per region, planning the operation that declines. The package's claim therefore
    fails and it is reverted - but nothing in it was attempted."""

    def author(region, defects, attempts):
        defect = defects[0]
        predicate = Predicate(cleared=frozenset({defect.fingerprint}),
                              reclassified=frozenset(), unchanged=frozenset(),
                              max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
        plan = ({"op": _DecliningOp(), "defect_code": DefectCode.small_edge,
                 "targets": (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                                       entity_index=defect.entity_index),)},)
        return TargetPackage(region=region, intent=f"look at {defect.fingerprint}", plan=plan,
                             predicate=predicate,
                             budget=PackageBudget(max_ops=4, timeout_s=1000.0, model_calls=0,
                                                  deviation_mm=0.0))

    return author


def test_a_declined_operation_abandons_nothing_while_a_rolled_back_one_still_does():
    """The decline says nothing about whether progress was possible, so its target was never
    claimed; the operation beside it in the same discarded package really was tried, and its
    target is still abandoned. Per operation, as everywhere else in this predicate."""
    from meshpipeline.cad.repair.region_loop import _abandoned

    verdict = PackageVerdict(
        region_index=0, status="reverted", reason="cleared defects still present: edge:a",
        verdicts=(OpVerdict(op="fix_a", targets=("edge:a",), defect_code="small_edge",
                            status="rolled_back",
                            reason="declined:too_wide_to_find (4 mm across)"),
                  OpVerdict(op="fix_z", targets=("edge:z",), defect_code="small_edge",
                            status="rolled_back", reason="target_remains")))

    assert _abandoned((verdict,), (_defect("a", DefectSeverity.warning),)) is False
    assert _abandoned((verdict,), (_defect("z", DefectSeverity.warning),)) is True


def test_a_region_whose_only_operation_declined_does_not_block_resolved():
    """THE CORPUS CASE, through the whole loop: every package is reverted because its claim could
    not hold, and every operation in them declined. No error-severity defect remains and nothing
    was attempted, so what the final scan reports is a defect nobody claimed - and the run is
    `resolved`, as the baseline calls the same log."""
    part = _Part(keys=("a", "b"), severity=DefectSeverity.warning)
    run = _run(part, _declining_author(part), attempts=1)

    assert [p.status for p in run.packages] == ["reverted", "reverted"]
    assert all(v.reason.startswith("declined:")
               for p in run.packages for v in p.verdicts), [
        v.reason for p in run.packages for v in p.verdicts]
    assert [d.fingerprint for d in run.defects_after] == ["edge:a", "edge:b"]
    assert run.stopped_because == "resolved"
    assert run.resolved is True


# THE WALK ORDER. `decompose` sorts regions spatially, which is the wrong order to spend a budget
# in: the run's clock and its operation allowance go to whatever sits at the lowest corner, and a
# part with many scattered warnings and one error can run out before the error is reached at all.


class _MixedPart(_Part):
    """A part whose defects do NOT all share one severity, reported in a chosen input order.

    `order` is the order the SCANNER lists its findings in, which is the only thing a different
    input order can mean here - the walk must not depend on it.
    """

    def __init__(self, severities: dict, *, order: tuple[str, ...] | None = None) -> None:
        super().__init__(keys=tuple(severities))
        self.severities = dict(severities)
        self.order = tuple(order) if order is not None else tuple(severities)
        self.ops["z"] = _Op(self.clock, "z")

    def scanner(self, shape):
        self.clock.now += _SCAN_S
        self.scans += 1
        found = [_defect(key, self.severities[key]) for key in self.order
                 if f"+{key}" not in shape]
        return found, True


def test_a_region_holding_an_error_is_walked_before_a_region_of_warnings():
    """Region 0 is spatially first and holds only a warning; region 1 holds the error. The error
    is what makes the part unusable, so it is offered a package first - whatever `decompose`'s
    spatial order says, and whatever the budget then does."""
    part = _MixedPart({"a": DefectSeverity.warning, "b": DefectSeverity.error})
    run = _run(part, _author(part))

    assert [p.region_index for p in run.packages] == [1, 0]
    # THE REPORT STILL CARRIES THE DECOMPOSITION, in its own spatial order.
    assert [r.index for r in run.regions] == [0, 1]


def test_only_the_worst_region_is_reached_when_the_budget_ends_the_walk():
    """The regression this ordering exists for: one package's worth of allowance buys the ERROR's
    region, not the warning's. A spatial walk spent it on the warning and committed nothing that
    mattered."""
    part = _MixedPart({"a": DefectSeverity.warning, "b": DefectSeverity.error})
    run = _run(part, _author(part), max_ops=1)

    assert [p.region_index for p in run.packages] == [1]
    # The error is gone and the warning was never attempted, so what is left is a defect nobody
    # claimed - which is the trade the ordering makes, and the right way round.
    assert [d.fingerprint for d in run.defects_after] == ["edge:a"]


def test_the_walk_does_not_depend_on_the_order_the_defects_were_reported_in():
    """Two inspections of the same bytes must produce the same report, so the walk is keyed on
    severity with `decompose`'s spatial index as the only tie-break - never on the order the scan
    happened to list its findings in."""
    severities = {"a": DefectSeverity.warning, "z": DefectSeverity.warning,
                  "b": DefectSeverity.error}
    forward = _run(_MixedPart(severities, order=("a", "z", "b")),
                   _author(_MixedPart(severities, order=("a", "z", "b"))))
    reversed_ = _run(_MixedPart(severities, order=("b", "z", "a")),
                     _author(_MixedPart(severities, order=("b", "z", "a"))))

    assert [p.region_index for p in forward.packages] == [1, 0, 0]
    assert ([p.region_index for p in forward.packages]
            == [p.region_index for p in reversed_.packages])
    assert [r.index for r in forward.regions] == [r.index for r in reversed_.regions]
    assert ([sorted(r.fingerprints) for r in forward.regions]
            == [sorted(r.fingerprints) for r in reversed_.regions])


# WHAT WAS DELIBERATELY NOT AIMED AT. The deterministic author rebuilds the choosing rules'
# ledger on every call, so a refusal the rules made - the paired rim of a missing bore wall above
# all - was authored and thrown away: the region path reported NO refusals at all while the
# baseline reported 22 `paired_rim` on the same corpus.


class _RefusingPlanner:
    """Refuses to aim at a defect, as the real rules refuse a paired rim, and proposes nothing."""

    label = "stub"

    def __init__(self) -> None:
        self.asked = 0

    def propose(self, defects, history, *, shape):
        self.asked += 1
        history.refuse("edge:a", "paired_rim", "this loop pairs with another as the two ends of "
                                               "one passage")
        return None


def test_a_refusal_the_planner_made_reaches_the_run_s_report():
    """In the baseline's own records - `{"defect", "reason", "detail"}`, which is what the
    receipt and the corpus's `refusals_by_reason` already read - and ONCE, though the author is
    asked once per region and recomputes the refusal every time."""
    from meshpipeline.cad.repair.region_loop import as_repair_run
    from meshpipeline.cad.repair.rules_author import rules_author

    part = _Part(keys=("a", "b"), severity=DefectSeverity.warning)
    planner = _RefusingPlanner()
    author = rules_author("original", original=_SOUND, planner=planner)
    run = _run(part, author)

    assert planner.asked == 2
    assert run.packages == ()
    assert run.refusals == ({"defect": "edge:a", "reason": "paired_rim",
                             "detail": "this loop pairs with another as the two ends of one "
                                       "passage"},)
    assert run.to_dict()["refusals"] == [dict(run.refusals[0])]
    # AND REACHES THE SHAPE EVERY STAGE AFTER THE LOOP READS, unchanged.
    sealed = as_repair_run(run, original=_SOUND.to_dict(), final=_SOUND.to_dict())
    assert sealed.refusals == run.refusals


def test_an_author_with_nothing_to_say_about_refusals_reports_none():
    """The `author` seam is `(region, defects, attempts) -> package`; reporting refusals is an
    extra an author may not have, and a plain callable must not break the walk."""
    part = _Part()
    run = _run(part, _author(part))

    assert run.refusals == ()


# --- A run whose only errors are mesh failures ends unmeshable -----------------------------------

def _mesh_defect(code=DefectCode.mesh_overlapping_facets) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                        entity_index=1, message="gmsh will not mesh this", fingerprint=f"{code}:1",
                        location=None)


def _terminals(defects, *, complete=True, escalated=False, fallback="no_candidate"):
    from types import SimpleNamespace

    from meshpipeline.cad.repair import loop as loop_module
    from meshpipeline.cad.repair import region_loop as region_module
    session = SimpleNamespace(complete_scan=complete, defects=lambda: tuple(defects), log=[])
    region = region_module._terminal(session, tuple(defects), escalated=escalated,
                                     packages=(), fallback=fallback)
    baseline = loop_module._terminal(session, fallback) if not escalated else None
    return region, baseline


def test_a_final_list_of_only_mesh_errors_is_unmeshable():
    from meshpipeline.cad.repair.loop import UNMESHABLE
    for codes in ([DefectCode.mesh_open_loop],
                  [DefectCode.mesh_open_loop, DefectCode.mesh_other],
                  [DefectCode.mesh_timeout, DefectCode.mesh_overlapping_facets]):
        region, baseline = _terminals([_mesh_defect(c) for c in codes])
        assert region == baseline == UNMESHABLE == "unmeshable"


def test_a_list_without_mesh_codes_gives_the_old_results():
    assert _terminals([]) == ("resolved", "resolved")
    assert _terminals([_defect("a")]) == ("no_candidate", "no_candidate")
    assert _terminals([_defect("a", DefectSeverity.warning)]) == ("resolved", "resolved")
    assert _terminals([_defect("a")], fallback="stalled") == ("stalled", "stalled")


def test_mixed_mesh_and_other_errors_keep_the_old_fallback():
    region, baseline = _terminals([_mesh_defect(), _defect("a")])
    assert region == baseline == "no_candidate"


def test_incomplete_scan_and_escalation_still_win_over_unmeshable():
    only_mesh = [_mesh_defect()]
    assert _terminals(only_mesh, complete=False)[0] == "scan_incomplete"
    assert _terminals(only_mesh, complete=False)[1] == "scan_incomplete"
    assert _terminals(only_mesh, escalated=True)[0] == "escalated"


def test_receipt_has_words_for_unmeshable():
    from meshpipeline.pipeline.repair_receipt import _DEFECT_WORDS, _STOPPED
    assert _STOPPED["unmeshable"].strip()
    for code in DefectCode:
        if code.value.startswith("mesh_"):
            assert _DEFECT_WORDS[code.value].strip()


def test_only_no_answer_mesh_errors_keep_the_fallback():
    # A timeout or a dead probe is the mesher not being heard from, not evidence it refused.
    for codes in ([DefectCode.mesh_timeout], [DefectCode.mesh_probe_error],
                  [DefectCode.mesh_timeout, DefectCode.mesh_probe_error]):
        for fallback in ("time_budget", "stalled"):
            assert _terminals([_mesh_defect(c) for c in codes], fallback=fallback) == (
                fallback, fallback)


def test_mesh_errors_plus_an_abandoned_fingerprint_is_unmeshable():
    from types import SimpleNamespace

    from meshpipeline.cad.repair.region_loop import _terminal
    # Intended: an abandoned warning is not an error, and the mesher's refusal is the stronger
    # evidence, so it is said rather than hidden behind the generic fallback.
    mesh = _mesh_defect()
    warning = _defect("a", DefectSeverity.warning)
    op = SimpleNamespace(op="x", kept=False, reason="rolled_back", targets=(warning.fingerprint,))
    verdict = SimpleNamespace(verdicts=(op,), committed=False, reclassified=())
    session = SimpleNamespace(complete_scan=True)
    assert _terminal(session, (mesh, warning), escalated=False, packages=(verdict,),
                     fallback="no_candidate") == "unmeshable"


def test_mesher_problem_discharges_nothing():
    """A verdict ends the region's attempts and clears NOTHING: the run stops for the same reason,
    with the same defects left, as an author that simply had nothing to propose."""
    def verdict_author(part):
        inner = _author(part)
        calls: list[int] = []

        def author(region, defects, attempts):
            if region.index == 1:
                calls.append(len(attempts))
                return None
            return inner(region, defects, attempts)

        author.calls = calls
        author.verdicts = [{"region": 1, "mesher_problem": ["edge:b"], "reason": "sound",
                            "author": "regions-model-v1"}]
        return author

    plain_part, verdict_part = _Part(), _Part()
    plain = _run(plain_part, _author(plain_part, decline=(1,)))
    author = verdict_author(verdict_part)
    run = _run(verdict_part, author, attempts=3)

    assert author.calls == [0], "the region stops authoring after the verdict"
    assert run.stopped_because == plain.stopped_because == REGIONS_EXHAUSTED
    assert [d.fingerprint for d in run.defects_after] == ["edge:b"]
    assert run.escalated == plain.escalated
