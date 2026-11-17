# Responsibility: Verify a target package executes as ONE reversible unit - admitted on the claim
#                 its author stated BEFORE execution, applied through the existing per-operation
#                 judgements, proved by a single region-scoped re-inspection, and reverted whole
#                 whenever that claim does not hold.
from __future__ import annotations

from meshpipeline.cad.repair import session as session_module
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import PackageVerdict, RepairSession

# NO KERNEL HERE. What is tested is the package's own logic: the admission gate, the order the
# judgements run in, the all-or-nothing reversal and what the verdict reports. The shape is a
# string, the measurer and the scanner are doubles, and the clock only moves when the work being
# timed says it did.

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)

#: The repaired shape measures the same in everything the caps read, and differs in ONE count the
#: caps ignore - so a rollback is observable through `current()` without tripping a cap.
_REPAIRED = Measure(**{**_SOUND.to_dict(), "vertices": 41})

_APPLY_S = 0.5
_SCAN_S = 1.0

_EDGE = "edge:a"
_FACE = "face:b"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Op:
    """Repairs `edge:a` by returning a different shape. Stateless, as a real operation is.

    `declared_area_mm2` is the AREA DECLARATION a real operation makes in its evidence - the
    surface it says it added. `caps.Accounting` accumulates those for every KEPT operation and the
    residual check judges the whole run against them, which is what makes a stale declaration
    poison every later operation.
    """

    name = "fix"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, clock: _Clock, *, name: str = "fix",
                 declared_area_mm2: float = 0.0) -> None:
        self.name = name
        self._clock = clock
        self._declared_area_mm2 = declared_area_mm2

    def apply(self, shape, targets, *, tolerance_mm):
        self._clock.now += _APPLY_S
        return OpOutcome(shape="repaired",
                         evidence={"area_delta_mm2": self._declared_area_mm2})


def _defect(fingerprint: str, *, code=DefectCode.small_edge, entity_type="edge",
            index: int = 1, severity=DefectSeverity.error, at=None) -> EntityDefect:
    """One located failure point. `at` is the corner of a 1 mm box, which is how a defect OUTSIDE
    a region is told from one inside it: a record with no box cannot be placed, and a scoped scan
    measures what it cannot place rather than concluding it is elsewhere."""
    location = {} if at is None else {"bbox_min": at,
                                      "bbox_max": tuple(v + 1.0 for v in at)}
    return EntityDefect(code=code, severity=severity, entity_type=entity_type,
                        entity_index=index, message="x", fingerprint=fingerprint,
                        location=location)


def _targets(fingerprint: str = _EDGE) -> tuple[EntityRef, ...]:
    return (EntityRef(entity_type="edge", fingerprint=fingerprint, entity_index=1),)


def _session(*, scan_complete: bool = True, repairs: bool = True, ops: int = 1,
             declared_area_mm2: float = 0.0):
    """A session whose first scan reports `edge:a` and `face:b`, and whose later scans report
    only `face:b` - i.e. the plan cleared the edge and left the face exactly where it was.

    `declared_area_mm2` makes every operation declare that much added surface AND makes the
    repaired shape measure exactly that much more, so the declaration is honest and the residual
    check is satisfied - which is the only way an operation carrying a declaration is KEPT.
    """
    clock = _Clock()
    first = [_defect(_EDGE), _defect(_FACE, code=DefectCode.small_face, entity_type="face",
                                    index=2)]
    later = first if not repairs else [
        _defect(_FACE, code=DefectCode.small_face, entity_type="face", index=2)]
    calls = {"n": 0}

    def scanner(_shape):
        clock.now += _SCAN_S
        calls["n"] += 1
        return (first if calls["n"] == 1 else later), scan_complete

    repaired = Measure(**{**_REPAIRED.to_dict(),
                          "surface_area_mm2": _SOUND.surface_area_mm2 + declared_area_mm2})

    def measurer(shape):
        return _SOUND if shape == "original" else repaired

    session = RepairSession("original", measurer=measurer, scanner=scanner, clock=clock)
    session.test_clock = clock                      # type: ignore[attr-defined]
    session.test_ops = tuple(_Op(clock, name=f"fix{n}", declared_area_mm2=declared_area_mm2)
                             for n in range(ops))
    return session


def _package(session, *, cleared=frozenset(), reclassified=frozenset(),
             unchanged=frozenset(), max_ops: int = 4, timeout_s: float = 1000.0,
             plan=None, extra_fingerprints=frozenset(), max_area_delta_mm2: float = 0.0,
             located: bool = True):
    predicate = Predicate(cleared=frozenset(cleared), reclassified=frozenset(reclassified),
                          unchanged=frozenset(unchanged),
                          max_area_delta_mm2=max_area_delta_mm2, max_volume_delta_mm3=0.0)
    named = predicate.cleared | predicate.reclassified | predicate.unchanged
    region = Region(index=3, fingerprints=named | {_EDGE, _FACE} | frozenset(extra_fingerprints),
                    bbox_min=(0.0, 0.0, 0.0) if located else None,
                    bbox_max=(1.0, 1.0, 1.0) if located else None,
                    face_indices=frozenset({2}), located=located)
    if plan is None:
        plan = tuple({"op": op, "targets": _targets(), "defect_code": DefectCode.small_edge,
                      "tolerance_mm": 0.01} for op in session.test_ops)
    return TargetPackage(region=region, intent="clear the short edge", plan=tuple(plan),
                         predicate=predicate,
                         budget=PackageBudget(max_ops=max_ops, timeout_s=timeout_s,
                                              model_calls=0, deviation_mm=0.0))


# THE BRIEF'S CASES


def test_package_commits_when_its_predicate_holds():
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status == "committed"
    assert verdict.reason == ""
    assert verdict.region_index == 3
    assert [v.status for v in verdict.verdicts] == ["kept"]
    assert session.shape == "repaired"


def test_package_reverts_whole_plan_when_predicate_fails():
    session = _session()
    before = session.current()
    verdict = session.try_package(_package(session, cleared={"edge:never-cleared"},
                                           extra_fingerprints={"edge:never-cleared"}))
    assert verdict.status == "reverted"
    assert session.current() == before          # every operation in the plan undone
    assert session.shape == "original"


def test_package_escalates_when_it_exhausts_its_budget():
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}, max_ops=0))
    assert verdict.status == "escalated"
    assert "budget" in verdict.reason
    assert verdict.verdicts == ()                # refused before anything was applied
    assert session.shape == "original"


def test_incomplete_region_scan_never_commits():
    # Review Focus: a partial list supports no claim of absence.
    session = _session(scan_complete=False)
    before = session.current()
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status != "committed"
    assert "scan" in verdict.reason
    assert session.current() == before
    assert session.shape == "original"
    # ESCALATED, NOT REVERTED: the claim was never judged, so it is a person's question rather
    # than a failed claim - and the package did run, so it carries no `refused:` prefix.
    assert verdict.status == "escalated"
    assert not verdict.reason.startswith("refused:")


def test_reclassified_entities_are_reported_on_the_verdict():
    # Review Focus: clean-by-relabelling must be visible, not silent.
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}, reclassified={_FACE}))
    assert verdict.status == "committed"
    assert verdict.reclassified == frozenset({_FACE})


# THE ADMISSION GATE - refused BEFORE any work, because the package was malformed


def test_a_budget_that_permits_unbounded_time_is_refused():
    # The per-region budget is this loop's cost control. A zero read as "no timeout" would hand an
    # author unlimited wall clock on a region, so it is malformed, not permissive.
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}, timeout_s=0.0))
    assert verdict.reason.startswith("refused:")
    assert "budget" in verdict.reason
    assert verdict.verdicts == ()                # no operation was attempted
    assert session.shape == "original"
    assert session.scan_count == 0


def test_a_negative_operation_budget_is_refused_but_a_zero_one_escalates():
    # Zero permits nothing, which is a budget working. Negative has no reading at all.
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}, max_ops=-1))
    assert verdict.reason.startswith("refused:")
    assert session.scan_count == 0
    assert _session().try_package(
        _package(_session(), cleared={_EDGE}, max_ops=0)).status == "escalated"


def test_a_refusal_is_distinguishable_from_a_revert():
    # Both carry the `reverted` status, so the reason is what tells "never ran" from "ran and the
    # claim did not hold" - and an operator reading one as the other has the wrong diagnosis.
    refused = _session()
    refused_verdict = refused.try_package(_package(refused, cleared={_EDGE}, timeout_s=0.0))

    ran = _session()
    ran_verdict = ran.try_package(_package(ran, cleared={"edge:never-cleared"},
                                           extra_fingerprints={"edge:never-cleared"}))

    assert refused_verdict.status == ran_verdict.status == "reverted"
    assert refused_verdict.reason.startswith("refused:")
    assert not ran_verdict.reason.startswith("refused:")
    assert ran_verdict.verdicts != ()            # it really did execute, then was undone
    assert refused_verdict.verdicts == ()


def test_a_predicate_naming_the_empty_fingerprint_is_refused():
    # `localize` names an entity with no measurable extent "", and `decompose` returns those as a
    # region whose fingerprints are {""}. A claim about "" is a claim about no entity in
    # particular: it would hold for doing anything at all, which is the refusal `try_op` already
    # makes for an untargetable target.
    session = _session()
    verdict = session.try_package(_package(session, cleared={""}))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "empty fingerprint" in verdict.reason
    assert verdict.verdicts == ()
    assert session.scan_count == 0


def test_a_package_targeting_the_unlocated_region_is_refused():
    # That region has no box, so a scan scoped to it is the whole-shape scan: there is nothing
    # scoped about the proof and nothing targetable in the region.
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}, located=False))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "unlocated" in verdict.reason
    assert session.scan_count == 0


def test_an_empty_plan_is_refused_rather_than_committed():
    # It would otherwise buy a commit with two scans and no work: a predicate naming only
    # `unchanged` entities holds trivially when nothing was applied.
    session = _session()
    verdict = session.try_package(_package(session, unchanged={_FACE}, plan=()))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "empty" in verdict.reason
    assert session.scan_count == 0


def test_a_predicate_that_names_no_entity_is_refused_before_anything_is_applied():
    session = _session()
    verdict = session.try_package(_package(session))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "names no entity" in verdict.reason
    assert verdict.verdicts == ()
    assert session.shape == "original"
    assert session.scan_count == 0               # the re-inspection was never paid for


def test_a_predicate_naming_geometry_outside_the_region_is_refused():
    session = _session()
    package = _package(session, cleared={_EDGE})
    outside = Predicate(cleared=frozenset({_EDGE, "edge:elsewhere"}),
                        reclassified=frozenset(), unchanged=frozenset(),
                        max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
    from dataclasses import replace

    verdict = session.try_package(replace(package, predicate=outside))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "edge:elsewhere" in verdict.reason
    assert verdict.verdicts == ()
    assert session.scan_count == 0


def test_a_malformed_plan_item_is_refused_before_anything_is_applied():
    session = _session()
    verdict = session.try_package(_package(
        session, cleared={_EDGE},
        plan=({"op": session.test_ops[0], "targets": (), "defect_code": DefectCode.small_edge},)))
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("refused:")
    assert "malformed" in verdict.reason
    assert session.scan_count == 0


# REVERSAL IS ALL OR NOTHING


def test_an_operation_rolled_back_by_its_own_caps_does_not_stop_the_package_being_judged():
    # The predicate is written about the whole plan, so one operation declining is not the verdict.
    session = _session(repairs=False)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert [v.status for v in verdict.verdicts] == ["rolled_back"]
    assert verdict.status == "reverted"
    # THE REASON IS THE PREDICATE'S, NOT THE OPERATION'S. An implementation that abandoned the
    # package the moment an operation rolled itself back would also be `reverted`, so without
    # this the test passes for the behaviour its own name denies.
    # It is `evaluate`'s KEPT-NOTHING guard that answers here, not its name comparison: the plan's
    # one operation rolled itself back, so there is no shape for a `cleared` claim to be true of.
    assert verdict.reason == ("no operation was kept, so nothing was repaired: the claim to have "
                              f"cleared {_EDGE} cannot hold over geometry the package did not "
                              "change")
    assert session.shape == "original"


def test_a_two_operation_package_reverts_to_the_boundary_not_to_the_last_operation():
    session = _session(ops=2)
    verdict = session.try_package(_package(session, cleared={"edge:never-cleared"},
                                           extra_fingerprints={"edge:never-cleared"}))
    assert verdict.status == "reverted"
    assert len(verdict.verdicts) == 2
    assert session.shape == "original"
    assert session.complete_scan is True
    assert [d.fingerprint for d in session.defects()] == [_EDGE, _FACE]


def test_a_reverted_package_leaves_no_declaration_behind_in_the_accounting():
    # `caps.Accounting` is "immutable, so a rolled-back operation cannot leave a trace", and
    # `try_op` upholds that by adding only for a verdict it accepted. A PACKAGE keeps its
    # operations individually and discards them together, so the boundary snapshot has to carry
    # the accounting too - otherwise the declarations of geometry that no longer exists survive,
    # and the next legitimate operation ANYWHERE in the run is refused for a residual nothing
    # accounted for.
    session = _session(declared_area_mm2=50.0)
    before = session.accounting
    assert before.declared_area_mm2 == 0.0

    verdict = session.try_package(_package(session, cleared={"edge:never-cleared"},
                                           extra_fingerprints={"edge:never-cleared"},
                                           max_area_delta_mm2=100.0))
    assert verdict.status == "reverted"
    assert [v.status for v in verdict.verdicts] == ["kept"]   # kept by try_op, then discarded
    assert session.accounting == before
    assert session.accounting.declared_area_mm2 == 0.0


def test_a_committed_package_keeps_its_operations_declarations():
    # The other half of the same invariant: restoring the accounting on a revert must not become
    # "never account for anything", or every kept package's declarations would be lost instead.
    session = _session(declared_area_mm2=50.0)
    verdict = session.try_package(_package(session, cleared={_EDGE}, max_area_delta_mm2=100.0))
    assert verdict.status == "committed"
    assert session.accounting.declared_area_mm2 == 50.0


def test_a_reverted_package_restores_the_accounting_it_found_not_an_empty_one():
    # The region loop runs package after package, so by the second one the accounting carries the
    # FIRST one's honest declarations. Restoring an empty accounting on a revert would pass every
    # single-package test and silently discard them.
    session = _session(ops=2, declared_area_mm2=50.0)
    first = session.try_package(_package(
        session, cleared={_EDGE}, max_area_delta_mm2=100.0,
        plan=({"op": session.test_ops[0], "targets": _targets(),
               "defect_code": DefectCode.small_edge, "tolerance_mm": 0.01},)))
    assert first.status == "committed"
    assert session.accounting.declared_area_mm2 == 50.0

    second = session.try_package(_package(
        session, cleared={"edge:never-cleared"}, extra_fingerprints={"edge:never-cleared"},
        max_area_delta_mm2=100.0,
        plan=({"op": session.test_ops[1], "targets": _targets(),
               "defect_code": DefectCode.small_edge, "tolerance_mm": 0.01},)))
    assert second.status == "reverted"
    assert session.accounting.declared_area_mm2 == 50.0


def test_an_escalated_package_leaves_no_declaration_behind_either():
    session = _session(scan_complete=False, declared_area_mm2=50.0)
    verdict = session.try_package(_package(session, cleared={_EDGE}, max_area_delta_mm2=100.0))
    assert verdict.status == "escalated"
    assert [v.status for v in verdict.verdicts] == ["kept"]
    assert session.accounting.declared_area_mm2 == 0.0


def test_a_plan_item_cannot_hand_itself_an_engineers_answer_to_a_hold():
    # `answered_held` is THE ENGINEER'S ANSWER in `try_op`. A plan is authored by a model, so
    # reading that key from a plan item would let the author exempt itself from a hold on
    # geometry a person is still being asked about. The key is not part of the item's shape, and
    # an item carrying one is ignored rather than obeyed.
    session = _session()
    package = _package(session, cleared={_EDGE})
    smuggled = tuple({**item, "answered_held": frozenset({_FACE})} for item in package.plan)

    from dataclasses import replace

    captured: list = []
    original_try_op = session.try_op

    def spy(op, targets, defect_code, **kwargs):
        captured.append(kwargs)
        return original_try_op(op, targets, defect_code, **kwargs)

    session.try_op = spy                            # type: ignore[method-assign]
    session.try_package(replace(package, plan=smuggled))
    assert captured and all("answered_held" not in kwargs for kwargs in captured)


def test_running_out_of_time_mid_plan_escalates_and_reverts_the_whole_package():
    session = _session(ops=3)
    verdict = session.try_package(_package(session, cleared={_EDGE}, timeout_s=2.0))
    assert verdict.status == "escalated"
    assert "budget" in verdict.reason
    assert len(verdict.verdicts) < 3
    assert session.shape == "original"


# WHAT THE VERDICT REPORTS


def test_package_aggregates_the_seconds_its_operations_already_timed():
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.apply_s == _APPLY_S
    # the first scan, the operation's re-inspection and the package's scoped scan
    assert verdict.scan_s == 3 * _SCAN_S
    assert verdict.to_dict()["apply_s"] == _APPLY_S
    assert verdict.to_dict()["scan_s"] == 3 * _SCAN_S


def test_package_verdicts_are_recorded_on_the_session():
    session = _session()
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert session.packages == [verdict]


def test_package_verdict_fields_default_so_existing_constructions_still_work():
    v = PackageVerdict(region_index=0, status="committed")
    assert (v.reason, v.verdicts, v.reclassified, v.scan_s, v.apply_s) == (
        "", (), frozenset(), 0.0, 0.0)


# THE PER-OPERATION PROOF IS SCOPED TOO (task 9)
#
# A package that scoped only its own boundary scan made the region loop MORE expensive than the
# operation-at-a-time baseline: `try_op` re-inspected the whole shape after every operation and
# the scoped proof was simply added on top. These cases pin the saving and the hazard that comes
# with it - that the two sides of the progress rule's comparison must be drawn from one scope.


class _ScanSpy:
    """Every `localize_scan` the session took, and whether it was scoped to a region.

    PATCHED ON THE MODULE RATHER THAN INJECTED. An injected scanner is deliberately called as it
    always was, `scanner(shape)`, so a constructor double cannot show which scans were scoped;
    the session looks `localize_scan` up on its own module at call time, which is what makes the
    production path observable without a kernel.

    The double stands in for a scoped scan by reporting only the region's own fingerprints, which
    is what a real scoped scan does to the findings of the part it was not allowed to walk.
    """

    def __init__(self, monkeypatch, scans, clock: _Clock) -> None:
        self.within: list = []
        self._scans = tuple(scans)
        self._clock = clock

        def spy(_shape, *, face_scan_s=0.0, within=None, scope_diagonal_mm=None):
            # `scope_diagonal_mm` IS ACCEPTED AND IGNORED: this double reports by fingerprint, so
            # it has no grown box to grow. What the real scan does with it is a native test's
            # (`test_native_repair_scoped_scan`).
            self.within.append(within)
            self._clock.now += _SCAN_S
            found = self._scans[min(len(self.within), len(self._scans)) - 1]
            if within is not None:
                found = [d for d in found if d.fingerprint in within.fingerprints]
            return tuple(found), True

        monkeypatch.setattr(session_module, "_localize_scan", spy)

    @property
    def scoped(self) -> list:
        return [region for region in self.within if region is not None]

    @property
    def whole_shape(self) -> list:
        return [region for region in self.within if region is None]


def _spied_session(monkeypatch, *scans):
    """A session on the REAL scan path, with `localize_scan` spied on. No scanner is injected."""
    clock = _Clock()
    spy = _ScanSpy(monkeypatch, scans, clock)

    def measurer(shape):
        return _SOUND if shape == "original" else _REPAIRED

    session = RepairSession("original", measurer=measurer, clock=clock)
    session.test_clock = clock                      # type: ignore[attr-defined]
    session.test_ops = (_Op(clock),)                # type: ignore[attr-defined]
    return session, spy


_CLEARED = [_defect(_EDGE)]
#: An error 1000 mm away from the package's region, which no operation in it ever touches. Its box
#: is what puts it outside: the region's own box is 1 mm across and grown by 1% of the 200 mm
#: diagonal, so this is well clear of it.
_FAR = _defect("face:far", code=DefectCode.small_face, entity_type="face", index=9,
               at=(1000.0, 1000.0, 1000.0))
_SURVIVOR = [_defect(_FACE, code=DefectCode.small_face, entity_type="face", index=2)]


def test_an_operation_inside_a_package_re_inspects_only_its_region(monkeypatch):
    session, spy = _spied_session(monkeypatch, [*_CLEARED, *_SURVIVOR], _SURVIVOR)
    verdict = session.try_package(_package(session, cleared={_EDGE}))

    assert verdict.status == "committed"
    # The operation's own re-inspection, then the package's proof - both scoped to the region.
    assert [region.index for region in spy.scoped] == [3, 3]
    # ONE whole-shape scan, and it is the boundary list the package was admitted against, not a
    # re-inspection: a package of N operations now costs N scoped scans plus one, not N+1
    # whole-shape walks plus one scoped.
    assert len(spy.whole_shape) == 1
    assert session.scan_count == 3


def test_the_progress_rule_still_rolls_back_an_operation_that_cleared_nothing(monkeypatch):
    # THE RESCAN IS SCOPED, NOT SKIPPED. Without it an operation is kept for doing nothing at all.
    unrepaired = [*_CLEARED, *_SURVIVOR]
    session, spy = _spied_session(monkeypatch, unrepaired, unrepaired)
    verdict = session.try_package(_package(session, cleared={_EDGE}))

    assert [v.reason for v in verdict.verdicts] == ["target_remains"]
    assert len(spy.scoped) == 2 and len(spy.whole_shape) == 1
    assert session.shape == "original"


def test_a_new_defect_in_the_region_is_not_excused_by_defects_outside_it(monkeypatch):
    """THE HAZARD. `previous` comes from the cached WHOLE-SHAPE list and `found` from a scoped
    scan, so scoping only one side makes every defect elsewhere read as having disappeared - and
    the progress rule's "the part got better overall" allowances then excuse a warning this
    operation introduced into the very region it was proving.

    Region {edge:a, face:b}; `face:far` is an error the operation never touched. Compared against
    the region's own previous list the finding count did NOT fall, so the new sliver is damage.
    """
    far = _FAR
    introduced = _defect(_FACE, code=DefectCode.small_face, entity_type="face", index=2,
                         severity=DefectSeverity.warning)
    session, _spy = _spied_session(monkeypatch, [*_CLEARED, far], [introduced, far])

    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert [(v.status, v.reason) for v in verdict.verdicts] == [("rolled_back", "new_defect")]


def test_a_defect_outside_the_region_is_not_credited_as_cleared(monkeypatch):
    # The scoped scan says nothing about the part outside the region, so what it does not report
    # there is unknown rather than gone. An implementation that let a scoped list become the
    # session's whole-shape list would drop `face:far` and claim the rest of the part was checked.
    session, _spy = _spied_session(monkeypatch, [*_CLEARED, _FAR], [_FAR])

    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status == "committed"
    assert "face:far" not in verdict.reason
    assert "face:far" in [d.fingerprint for d in session.defects()]
    assert session.scan_count == 3                  # no extra whole-shape scan was taken to learn it


def test_try_op_without_a_region_is_unchanged(monkeypatch):
    session, spy = _spied_session(monkeypatch, [*_CLEARED, *_SURVIVOR], _SURVIVOR)
    verdict = session.try_op(session.test_ops[0], _targets(), DefectCode.small_edge,
                             tolerance_mm=0.1)

    assert verdict.status == "kept"
    assert spy.scoped == []
    # `_carries`' first scan and the re-inspection - both whole-shape, exactly as before.
    assert len(spy.whole_shape) == 2
    assert [d.fingerprint for d in session.defects()] == [_FACE]


# THE PACKAGE'S PROOF IS PAID FOR OUT OF THE PACKAGE'S OWN BUDGET (P2 Task 3 fix round 1). The
# plan loop checks the clock only BEFORE each operation, so a last operation could spend the budget
# and the verification probe would then spend a full probe budget on top.


def _probed_session(monkeypatch, clock: _Clock, *, apply_s: float):
    """A session on the DEFAULT scan path with the mesh probe live: `_localize_scan` reports the
    edge first and nothing after; every probe call records the budget it was handed."""
    import meshpipeline.cad.repair.mesh_probe as mesh_probe
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    scans = {"n": 0}

    def scan(shape, **kw):
        scans["n"] += 1
        return ((_defect(_EDGE, at=(0.0, 0.0, 0.0)),) if scans["n"] == 1 else ()), True

    budgets: list[float] = []

    def probe(shape, **kw):
        budgets.append(float(kw["budget_s"]))
        return ()

    monkeypatch.setattr(session_module, "_localize_scan", scan)
    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)

    class _Slow(_Op):
        def apply(self, shape, targets, *, tolerance_mm):
            self._clock.now += apply_s
            return OpOutcome(shape="repaired")

    def measurer(shape):
        return _SOUND if shape == "original" else _REPAIRED

    session = RepairSession("original", measurer=measurer, clock=clock,
                            remaining_s=lambda: 1000.0)
    session.test_ops = (_Slow(clock),)
    return session, budgets


def test_a_package_exhausted_by_its_last_operation_pays_for_no_proof(monkeypatch):
    clock = _Clock()
    session, budgets = _probed_session(monkeypatch, clock, apply_s=5.0)
    session.defects()
    verdict = session.try_package(_package(session, cleared={_EDGE}, timeout_s=2.0))
    assert verdict.status == "escalated"
    assert "before the package's proof" in verdict.reason
    # The first scan's probe and the operation's own re-inspection - and NO verification probe.
    assert len(budgets) == 2, budgets
    assert session.shape == "original"


def test_a_packages_proof_probe_is_capped_by_what_the_package_has_left(monkeypatch):
    clock = _Clock()
    session, budgets = _probed_session(monkeypatch, clock, apply_s=7.0)
    session.defects()
    verdict = session.try_package(_package(session, cleared={_EDGE}, timeout_s=10.0))
    assert verdict.status == "committed", verdict.reason
    assert len(budgets) == 3, budgets
    # The run had 1000 s and the probe timeout is 120 s; the package had 3 s left.
    assert budgets[-1] == 3.0


# THE SEMANTIC REVIEW (Plan 3 Task 4). With CAD_REPAIR_PACKAGE_REVIEW live, a package that passed
# its predicate is read by a separate reviewer BEFORE its commit becomes durable; a reject undoes
# it exactly as a failed predicate does, and every verdict is recorded on the PackageVerdict.
# Hermetic: the model call is a fake `package_review._ask`.


class _ReviewAsk:
    """The reviewer's model call: queued replies, every call recorded; `spend_s` moves the run's
    clock as a real call would spend it."""

    def __init__(self, *replies, clock: _Clock | None = None, spend_s: float = 0.0):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._clock = clock
        self._spend_s = spend_s

    async def __call__(self, system, user, *, job_id, timeout_s):
        self.calls.append({"system": system, "user": user, "timeout_s": timeout_s})
        if self._clock is not None:
            self._clock.now += self._spend_s
        return self.replies.pop(0) if self.replies else None


def _review(monkeypatch, *replies, posture: str = "live", **kw) -> _ReviewAsk:
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair import package_review

    fake = _ReviewAsk(*replies, **kw)
    monkeypatch.setattr(package_review, "_ask", fake)
    monkeypatch.setattr(package_review, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_PACKAGE_REVIEW", posture)
    return fake


def _state(session) -> tuple:
    return (session.shape, session._defects, session.complete_scan, session.accounting,
            session.current())


def test_accept_commits_with_verdict_recorded(monkeypatch):
    session = _session()
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "only the edge moved"},
                   clock=session.test_clock, spend_s=2.0)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status == "committed", verdict.reason
    assert session.shape == "repaired"
    assert (verdict.reviewed, verdict.review_reason) == ("accept", "only the edge moved")
    # `review_s` is the reviewer's seconds on the run's clock, beside the other timings.
    assert verdict.review_s == 2.0
    out = verdict.to_dict()
    assert (out["reviewed"], out["review_reason"], out["review_s"]) == (
        "accept", "only the edge moved", 2.0)
    assert len(fake.calls) == 1
    # THE AUTHOR'S INTENT TRAVELS ONLY AS A CLAIM.
    import json
    evidence = json.loads(fake.calls[0]["user"])
    assert evidence["author_claim"] == "clear the short edge"
    assert evidence["defects_cleared"] == ["small_edge"]
    # Probe off: the evidence simply lacks the mesh and deviation fields.
    assert not {"mesh_before", "mesh_after", "deviation_mm"} & set(evidence)


def test_reject_reverts_the_package(monkeypatch):
    # THE SAME RESTORED STATE AS A FAILED PREDICATE, accounting included.
    failed = _session(declared_area_mm2=2.0)
    failed.defects()
    failed_before = _state(failed)
    verdict = failed.try_package(_package(failed, cleared={"edge:never-cleared"},
                                          extra_fingerprints={"edge:never-cleared"},
                                          max_area_delta_mm2=2.0))
    assert verdict.status == "reverted"
    assert _state(failed) == failed_before

    rejected = _session(declared_area_mm2=2.0)
    rejected.defects()
    rejected_before = _state(rejected)
    fake = _review(monkeypatch, {"verdict": "reject", "reason": "a boss face was removed"})
    verdict = rejected.try_package(_package(rejected, cleared={_EDGE}, max_area_delta_mm2=2.0))
    assert len(fake.calls) == 1
    assert verdict.status == "reverted"
    assert verdict.reason.startswith("review:")
    assert (verdict.reviewed, verdict.review_reason) == ("reject", "a boss face was removed")
    assert verdict.to_dict()["reviewed"] == "reject"
    assert _state(rejected) == rejected_before
    assert _state(rejected) == _state(failed)


def test_a_review_no_answer_reverts_the_package(monkeypatch):
    session = _session()
    _review(monkeypatch, None)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.status == "reverted"
    assert verdict.reviewed == "reject" and "no reply" in verdict.review_reason
    assert session.shape == "original"


def test_review_off_is_byte_identical(monkeypatch):
    control = _session()
    expected = control.try_package(_package(control, cleared={_EDGE})).to_dict()
    session = _session()
    fake = _review(monkeypatch, {"verdict": "reject", "reason": "x"}, posture="off")
    out = session.try_package(_package(session, cleared={_EDGE})).to_dict()
    assert fake.calls == []
    assert out == expected
    assert not {"reviewed", "review_reason", "review_s"} & set(out)


def test_no_time_left_rejects_not_skips(monkeypatch):
    # AN UNREVIEWED COMMIT IS EXACTLY WHAT THE POSTURE FORBIDS, so no time left is a reject.
    session = _session()
    session.remaining_s = lambda: 0.0
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert fake.calls == []
    assert verdict.status == "reverted"
    assert (verdict.reviewed, verdict.review_reason) == ("reject", "no time left to review")
    assert session.shape == "original"


def test_the_review_timeout_is_cut_to_the_runs_remaining_time_less_a_margin(monkeypatch):
    # 5 s are kept back before the run's deadline, as the model author keeps them.
    session = _session()
    session.remaining_s = lambda: 8.0
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})
    session.try_package(_package(session, cleared={_EDGE}))
    assert fake.calls[0]["timeout_s"] == 3.0
    inside_margin = _session()
    inside_margin.remaining_s = lambda: 4.0
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})
    verdict = inside_margin.try_package(_package(inside_margin, cleared={_EDGE}))
    assert fake.calls == []
    assert verdict.review_reason == "no time left to review"
    assert inside_margin.shape == "original"


def test_the_verdict_records_the_evidence_it_was_judged_on(monkeypatch):
    import json
    session = _session()
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.review_evidence == json.loads(fake.calls[0]["user"])
    assert verdict.to_dict()["review_evidence"]["author_claim"] == "clear the short edge"
    # The part's scale, from the session's measure of the upload.
    assert verdict.review_evidence["diagonal_mm"] == _SOUND.diagonal_mm
    assert verdict.review_evidence["deviation_status"] == "not measured: probe off"


def test_a_review_that_raises_reverts_the_package(monkeypatch):
    from meshpipeline.cad.repair import package_review

    session = _session()
    session.defects()
    before = _state(session)
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(package_review, "evidence_of", broken)
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert fake.calls == []
    assert verdict.status == "reverted"
    assert (verdict.reviewed, verdict.review_reason) == ("reject", "review failed: RuntimeError")
    assert _state(session) == before


# NO RETRY-UNTIL-ACCEPT, AND THE GRADER STAYS OUT OF THE AUTHOR'S REACH (fix round 1, C1).


def _reworded(session, intent: str):
    package = _package(session, cleared={_EDGE})
    from dataclasses import replace
    return replace(package, intent=intent)


def test_the_author_sees_only_a_generic_reason_for_a_review_reject(monkeypatch):
    from meshpipeline.cad.repair.observe import _attempt_of

    session = _session()
    _review(monkeypatch, {"verdict": "reject", "reason": "SECRET-GRADER-REASONING"})
    verdict = session.try_package(_package(session, cleared={_EDGE}))
    assert verdict.reason == "review: rejected by review"
    assert verdict.review_reason == "SECRET-GRADER-REASONING"
    import json
    assert "SECRET-GRADER-REASONING" not in json.dumps(_attempt_of(verdict))


def test_a_reworded_retry_is_judged_with_the_prior_reject_in_evidence(monkeypatch):
    import json
    session = _session()
    fake = _review(monkeypatch, {"verdict": "reject", "reason": "the boss was flattened"},
                   {"verdict": "accept", "reason": "ok"})
    first = session.try_package(_reworded(session, "clear the short edge"))
    second = session.try_package(_reworded(session, "tidy the edge, nothing else"))
    assert first.status == "reverted" and second.status == "committed"
    assert json.loads(fake.calls[0]["user"])["review_rejects"] == {"count": 0, "reasons": []}
    assert json.loads(fake.calls[1]["user"])["review_rejects"] == {
        "count": 1, "reasons": ["the boss was flattened"]}


def test_a_second_review_reject_escalates_the_region(monkeypatch):
    session = _session()
    fake = _review(monkeypatch, {"verdict": "reject", "reason": "r1"},
                   {"verdict": "reject", "reason": "r2"},
                   {"verdict": "accept", "reason": "ok"})
    first = session.try_package(_reworded(session, "one"))
    second = session.try_package(_reworded(session, "two"))
    third = session.try_package(_reworded(session, "three"))
    assert first.status == "reverted"
    assert (second.status, second.reason) == ("escalated", "review: rejected by review twice")
    assert (second.reviewed, second.review_reason) == ("reject", "r2")
    assert session.shape == "original"
    # PAST THE CAP NOTHING RUNS AND NOTHING IS ASKED.
    assert (third.status, third.reason) == ("escalated", "review: rejected by review twice")
    assert third.verdicts == () and len(fake.calls) == 2


def test_one_review_reject_allows_exactly_one_more_attempt(monkeypatch):
    session = _session()
    fake = _review(monkeypatch, {"verdict": "reject", "reason": "r1"},
                   {"verdict": "accept", "reason": "ok"})
    session.try_package(_reworded(session, "one"))
    # The one extra attempt fails its predicate rather than its review...
    failed = session.try_package(_package(session, cleared={"edge:never-cleared"},
                                          extra_fingerprints={"edge:never-cleared"}))
    assert failed.status == "reverted" and not failed.reason.startswith("review:")
    # ...and the attempt after it is escalated unrun.
    spent = session.try_package(_reworded(session, "three"))
    assert spent.status == "escalated" and spent.reason.startswith("review:")
    assert spent.verdicts == () and len(fake.calls) == 1


def test_the_reviewer_is_never_consulted_for_a_package_that_did_not_pass(monkeypatch):
    fake = _review(monkeypatch, {"verdict": "accept", "reason": "ok"})
    reverted = _session()
    v = reverted.try_package(_package(reverted, cleared={"edge:never-cleared"},
                                      extra_fingerprints={"edge:never-cleared"}))
    escalated = _session()
    e = escalated.try_package(_package(escalated, cleared={_EDGE}, max_ops=0))
    refused = _session()
    r = refused.try_package(_package(refused, cleared={_EDGE}, timeout_s=0.0))
    assert (v.status, e.status, r.status) == ("reverted", "escalated", "reverted")
    assert fake.calls == []
    for verdict in (v, e, r):
        assert verdict.reviewed is None
        assert "reviewed" not in verdict.to_dict()
