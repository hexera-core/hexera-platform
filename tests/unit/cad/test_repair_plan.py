# Responsibility: Verify the rules planner's ordering, its refusal to repeat what did not work, and that it reports skipping a defect rather than skipping it silently.
from __future__ import annotations

import meshpipeline.cad.repair.plan as plan_module
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.plan import Candidate, RulesPlanner, RunHistory, paired_rim_fingerprints

# THE PLANNER IS THE SEAT A MODEL TAKES IN SLICE 8, so what is pinned here is the INTERFACE and
# the baseline behaviour a model will be scored against - not an implementation detail. No kernel:
# ordering and bookkeeping are pure.


def _defect(code, *, fingerprint, severity=DefectSeverity.error, kind="edge", index=1,
            measurements=None) -> EntityDefect:
    return EntityDefect(code=code, severity=severity, entity_type=kind, entity_index=index,
                        message="x", fingerprint=fingerprint,
                        measurements=measurements or {})


class _Op:
    def __init__(self, name, answers, scope):
        self.name = name
        self.answers = frozenset(answers)
        self.scope = scope

    def targets_for(self, defect):
        if defect.code not in self.answers or not defect.fingerprint:
            return ()
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)


_NARROW = _Op("narrow", {DefectCode.small_edge}, 1)
_WIDE = _Op("wide", {DefectCode.small_edge, DefectCode.open_shell}, 4)
_GLOBAL = _Op("fix_shape_global", {DefectCode.small_edge, DefectCode.invalid_brep}, 99)


# ORDERING


def test_the_worst_defect_is_addressed_first():
    planner = RulesPlanner(ops=(_NARROW, _WIDE, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a",
                       severity=DefectSeverity.warning),
               _defect(DefectCode.open_shell, fingerprint="edge:b",
                       severity=DefectSeverity.error, index=2))

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.defect_fingerprint == "edge:b"


def test_the_narrowest_operation_that_claims_a_defect_is_chosen():
    # A narrow fix that fails costs one entity's worth of rollback; a wide one costs the part's.
    planner = RulesPlanner(ops=(_WIDE, _NARROW, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, RunHistory(), shape=None).op.name == "narrow"


def test_the_whole_shape_fallback_is_only_proposed_when_nothing_scoped_is_left():
    planner = RulesPlanner(ops=(_NARROW, _GLOBAL))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, history, shape=None).op.name == "narrow"
    history.mark_ineffective("edge:a", "narrow")
    assert planner.propose(defects, history, shape=None).op.name == "fix_shape_global"


def test_a_defect_nothing_claims_produces_no_candidate_and_is_reported():
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    defects = (_defect(DefectCode.self_intersection, fingerprint="face:a", kind="face"),)

    assert planner.propose(defects, history, shape=None) is None
    assert any(r["reason"] == "no_operation_claims_it" for r in history.refusals)


def test_an_untargetable_defect_is_reported_rather_than_skipped_silently():
    # It is the case a model most needs to see: the system found something and could not aim at
    # it. A silent skip is how that stops being visible.
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint=""),)

    assert planner.propose(defects, history, shape=None) is None
    assert any(r["reason"] == "untargetable" for r in history.refusals)


# THE INEFFECTIVE-PAIR LEDGER


def test_a_pair_that_did_not_work_is_never_proposed_again_in_this_run():
    # THE PER-OPERATION LEARNING SIGNAL. In slice 8 this is exactly what the model is shown on its
    # next turn, which is why it is recorded per (defect, operation) rather than per operation.
    planner = RulesPlanner(ops=(_NARROW, _WIDE))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    first = planner.propose(defects, history, shape=None)
    history.mark_ineffective(first.defect_fingerprint, first.op.name)
    second = planner.propose(defects, history, shape=None)

    assert second is not None and second.op.name != first.op.name


def test_the_same_operation_may_still_be_proposed_for_a_different_defect():
    # The ban is on the PAIR. An operation that could not fix one edge may be exactly right for
    # another, and banning it outright would waste the rest of the catalogue.
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    history.mark_ineffective("edge:a", "narrow")
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),
               _defect(DefectCode.small_edge, fingerprint="edge:b", index=2))

    candidate = planner.propose(defects, history, shape=None)

    assert candidate is not None and candidate.defect_fingerprint == "edge:b"


def test_when_every_pair_has_been_tried_there_is_no_candidate_left():
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    history.mark_ineffective("edge:a", "narrow")
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:a"),)

    assert planner.propose(defects, history, shape=None) is None


def test_proposing_is_deterministic_for_the_same_inputs():
    # Two runs over identical bytes must do identical things, or nothing about a repair is
    # reproducible and the corpus cannot be compared across a change.
    planner = RulesPlanner(ops=(_NARROW, _WIDE, _GLOBAL))
    defects = (_defect(DefectCode.small_edge, fingerprint="edge:b", index=2),
               _defect(DefectCode.small_edge, fingerprint="edge:a", index=1))

    first = planner.propose(defects, RunHistory(), shape=None)
    again = planner.propose(defects, RunHistory(), shape=None)

    assert (first.op.name, first.defect_fingerprint) == (again.op.name, again.defect_fingerprint)


# THE HISTORY'S OWN BOOKKEEPING


def test_a_refusal_is_recorded_once_per_defect_and_reason():
    # The loop asks the planner repeatedly, and a refusal list that grew on every tick would bury
    # the report it exists to produce.
    history = RunHistory()
    history.refuse("edge:a", "untargetable")
    history.refuse("edge:a", "untargetable")

    assert len([r for r in history.refusals if r["defect"] == "edge:a"]) == 1


# THE RIM BAN MUST OUTLIVE THE PAIRING


_FILL = _Op("fill_planar_hole", {DefectCode.open_shell}, 5)


def _rim(fp, index):
    return _defect(DefectCode.open_shell, fingerprint=fp, index=index)


def test_the_rim_ban_survives_the_other_rim_being_closed(monkeypatch):
    # THE PART-RUINING SEQUENCE. The targeted fill has no rim check of its own, so once one rim is
    # closed by another operation the surviving rim has nothing to pair with. The ban must hold.
    planner = RulesPlanner(ops=(_FILL,))
    history = RunHistory()
    both = (_rim("edge:a", 1), _rim("edge:b", 2))
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"edge:a", "edge:b"}))
    assert planner.propose(both, history, shape=object()) is None

    monkeypatch.setattr(plan_module, "paired_rim_fingerprints", lambda shape, defects: frozenset())
    assert planner.propose((_rim("edge:a", 1),), history, shape=object()) is None


def test_the_fill_refusal_holds_for_an_unrelated_loop_with_a_fresh_fingerprint(monkeypatch):
    # The escape a per-fingerprint ban leaves: an operation closes one rim AND shifts the other's
    # bounding box, so the survivor reappears under a fingerprint that was never banned.
    planner = RulesPlanner(ops=(_FILL,))
    history = RunHistory()
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"edge:a", "edge:b"}))
    planner.propose((_rim("edge:a", 1), _rim("edge:b", 2)), history, shape=object())

    monkeypatch.setattr(plan_module, "paired_rim_fingerprints", lambda shape, defects: frozenset())
    assert planner.propose((_rim("edge:fresh", 7),), history, shape=object()) is None


def test_a_rim_refusal_is_not_recorded_as_a_failed_attempt(monkeypatch):
    planner = RulesPlanner(ops=(_FILL,))
    history = RunHistory()
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"edge:a", "edge:b"}))
    planner.propose((_rim("edge:a", 1), _rim("edge:b", 2)), history, shape=object())

    assert history.ineffective == set()
    assert history.fill_refused_because


def test_the_rim_refusal_is_recorded_on_the_first_proposal_even_if_another_op_wins(monkeypatch):
    planner = RulesPlanner(ops=(_Op("sew_faces", {DefectCode.open_shell}, 4), _FILL))
    history = RunHistory()
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"edge:a", "edge:b"}))

    candidate = planner.propose((_rim("edge:a", 1), _rim("edge:b", 2)), history, shape=object())

    assert candidate is not None and candidate.op.name == "sew_faces"
    assert {r["defect"] for r in history.refusals if r["reason"] == "paired_rim"} == {
        "edge:a", "edge:b"}


def test_the_rim_question_never_raises_for_a_shape_it_cannot_read(monkeypatch):
    # The defects carry boundary_edges so they pass the early gate and genuinely reach
    # free_wire_containing, whose kernel import / lookup on a non-shape is what must not escape.
    defects = tuple(_defect(DefectCode.open_shell, fingerprint=f"edge:{i}", index=i,
                            measurements={"boundary_edges": 4}) for i in (1, 2))

    def _no_kernel(shape, index):
        raise ImportError("no kernel on this host")

    # The real lookup swallows most failures itself; what is pinned here is that one escaping it
    # (as an ImportError does on a kernel-less host) cannot take the planner down.
    monkeypatch.setattr("meshpipeline.cad.repair.operations.free_wire_containing", _no_kernel)
    rims = paired_rim_fingerprints(object(), defects)
    # FAIL CLOSED: an unanswered question is not "no bore here". Every candidate rim is held.
    assert rims == frozenset({"edge:1", "edge:2"})
    assert getattr(rims, "unknown", False) is True


def test_a_rim_whose_free_wire_cannot_be_resolved_fails_closed(monkeypatch):
    # FAIL OPEN, AS IT WAS: the unresolved rim was skipped, its partner had nothing to pair with,
    # and the partner was filled - one end of a through-hole sealed, a blind hole delivered.
    defects = tuple(_defect(DefectCode.open_shell, fingerprint=f"edge:{i}", index=i,
                            measurements={"boundary_edges": 4}) for i in (1, 2))
    monkeypatch.setattr("meshpipeline.cad.repair.operations.free_wire_containing",
                        lambda shape, index: "wire" if index == 2 else None)
    monkeypatch.setattr("meshpipeline.cad.repair.conservative._paired_rims",
                        lambda loops, shape=None: set())

    rims = paired_rim_fingerprints(object(), defects)
    assert getattr(rims, "unknown", False) is True
    assert rims == frozenset({"edge:1", "edge:2"})

    planner, history = RulesPlanner(ops=(_FILL,)), RunHistory()
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints", lambda shape, d: rims)
    assert planner.propose(defects, history, shape=object()) is None
    assert history.fill_refused_because


def test_a_lone_unresolved_loop_does_not_bar_filling(monkeypatch):
    # Nothing to pair with: a single opening whose wire is unresolved cannot be one end of a pair.
    defects = (_defect(DefectCode.open_shell, fingerprint="edge:1", index=1,
                       measurements={"boundary_edges": 4}),)
    monkeypatch.setattr("meshpipeline.cad.repair.operations.free_wire_containing",
                        lambda shape, index: None)
    rims = paired_rim_fingerprints(object(), defects)
    assert rims == frozenset() and not getattr(rims, "unknown", False)


def test_a_rim_check_that_could_not_run_refuses_every_fill(monkeypatch):
    # The planner must not fill on a guess: the unknown answer bars filling for the run, and each
    # loop is reported as a possible rim rather than as a demonstrated pair.
    planner = RulesPlanner(ops=(_FILL,))
    history = RunHistory()
    monkeypatch.setattr(plan_module, "paired_rim_fingerprints",
                        lambda shape, defects: plan_module.RimsUnknown({"edge:a"}))

    assert planner.propose((_rim("edge:a", 1),), history, shape=object()) is None
    assert "could not run" in history.fill_refused_because
    refusal = next(r for r in history.refusals if r["reason"] == "paired_rim")
    assert refusal["defect"] == "edge:a" and "could not run" in refusal["detail"]


def test_two_different_unlocated_defects_are_two_refusals():
    planner = RulesPlanner(ops=(_NARROW,))
    history = RunHistory()
    defects = (_defect(DefectCode.small_edge, fingerprint="", kind="edge", index=1),
               _defect(DefectCode.small_face, fingerprint="", kind="face", index=2))

    assert planner.propose(defects, history, shape=None) is None
    assert len([r for r in history.refusals if r["reason"] == "untargetable"]) == 2


def test_the_planner_is_labelled_and_a_candidate_serialises():
    assert RulesPlanner.label == "rules-v1"
    defect = _defect(DefectCode.small_edge, fingerprint="edge:a")
    candidate = RulesPlanner(ops=(_NARROW,)).propose((defect,), RunHistory(), shape=None)
    assert isinstance(candidate, Candidate)
    assert candidate.to_dict() == {"op": "narrow", "defect": "edge:a", "defect_code": "small_edge",
                                   "targets": ["edge:a"]}


# THE WHOLE-SHAPE FALLBACK, AGAINST THE REAL CATALOGUE
#
# The stub-based ordering tests above cannot prove this: `_GLOBAL` is an `_Op`, whose `targets_for`
# returns a ref for anything it answers. The real `FixShapeGlobal` returned `()` unconditionally,
# so the planner's `if not targets: continue` skipped it and EVERY defect only it claims got zero
# attempts - the files that used to get `ShapeFix_Shape` got nothing at all. These tests use
# `OPERATIONS` itself for that reason.


def test_the_real_catalogue_proposes_the_whole_shape_fallback_for_a_defect_only_it_claims():
    from meshpipeline.cad.repair.operations import OPERATIONS

    planner = RulesPlanner(ops=OPERATIONS)
    defects = (_defect(DefectCode.invalid_brep, fingerprint="shell:a", kind="shell", index=3),)

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None, "invalid_brep got no attempt at all"
    assert candidate.op.name == "fix_shape_global"
    # IT STILL NAMES WHAT IT AIMED AT, even though it runs whole-shape: the session's
    # `_carries` check reads the targets, so an empty tuple would be `target_absent`.
    assert [t.fingerprint for t in candidate.targets] == ["shell:a"]
    assert [t.entity_type for t in candidate.targets] == ["shell"]


def test_an_edge_curve_inconsistency_reaches_the_whole_shape_fallback():
    # `FixFaceBoundary` takes only faces, and `InvalidCurveOnSurface`, `Invalid3DCurve`,
    # `NoCurveOnSurface` and `InvalidPointOnCurve` are all EDGE statuses. Before the fallback was
    # reachable, the commonest attributed status on a translated part got no attempt.
    from meshpipeline.cad.repair.operations import OPERATIONS

    planner = RulesPlanner(ops=OPERATIONS)
    defects = (_defect(DefectCode.curve_inconsistency, fingerprint="edge:a", kind="edge"),)

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.op.name == "fix_shape_global"


def test_a_wire_attributed_defect_reaches_the_whole_shape_fallback():
    # `FixFaceBoundary.targets_for` declines a wire because the localiser does not record which
    # face owns it. Its docstring promises the planner then falls through here.
    from meshpipeline.cad.repair.operations import OPERATIONS

    planner = RulesPlanner(ops=OPERATIONS)
    defects = (_defect(DefectCode.wire_gap, fingerprint="wire:a", kind="wire", index=2),)

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.op.name == "fix_shape_global"


def test_the_fallback_is_still_last_in_the_real_catalogue():
    # Reachable must not mean preferred: a scoped operation that claims the defect wins.
    from meshpipeline.cad.repair.operations import OPERATIONS

    planner = RulesPlanner(ops=OPERATIONS)
    defects = (_defect(DefectCode.curve_inconsistency, fingerprint="face:a", kind="face"),)

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.op.name == "fix_face_boundary"


def test_the_fallback_declines_a_defect_it_does_not_claim_or_cannot_name():
    from meshpipeline.cad.repair.operations import FixShapeGlobal

    op = FixShapeGlobal()
    assert op.targets_for(_defect(DefectCode.self_intersection, fingerprint="face:a")) == ()
    assert op.targets_for(_defect(DefectCode.invalid_brep, fingerprint="")) == ()


# THE RIM GUARD IS COMPARED AGAINST THE OPERATION'S OWN NAME


def test_exactly_one_catalogued_operation_carries_the_fill_name():
    # The guard in `RulesPlanner.propose` keys off `FILL_OPS`. A rename that missed a second
    # holder of a name, or lost the operation from the catalogue, would leave the guard dead
    # with every test green.
    from meshpipeline.cad.repair.operations import FILL_OPS, OPERATIONS

    assert FILL_OPS
    for name in FILL_OPS:
        assert [op.name for op in OPERATIONS].count(name) == 1


def test_a_refused_run_proposes_neither_fill(monkeypatch):
    # The run-level fill refusal (a paired or undecidable rim) keys off FILL_OPS, so the curved
    # fill is barred with the flat one: the real catalogue then has nothing left for a free loop
    # but sewing.
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.operations import OPERATIONS

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    loop = _defect(DefectCode.open_shell, fingerprint="edge:rim",
                   measurements={"boundary_edges": 4})
    history = RunHistory()
    history.refuse_filling("paired_rim")
    planner = RulesPlanner(ops=OPERATIONS)
    proposed = []
    while (candidate := planner.propose((loop,), history, shape=None)) is not None:
        proposed.append(candidate.op.name)
        history.mark_ineffective(candidate.defect_fingerprint, candidate.op.name)
    assert "fill_planar_hole" not in proposed and "fill_nonplanar_hole" not in proposed
    assert proposed, "the refusal barred every operation, not just the fills"


# A DEFECT RESERVED FOR A PERSON


def test_a_reserved_code_is_never_proposed_even_when_an_operation_claims_it():
    """With per-defect review on, an invalid_brep defect is the engineer's question. The whole-shape
    fixer claims invalid_brep, so without the reservation the loop changed exactly the geometry the
    review was holding back - before anyone answered."""
    planner = RulesPlanner(ops=(_NARROW, _GLOBAL), reserved=frozenset({DefectCode.invalid_brep}))
    defects = (_defect(DefectCode.invalid_brep, fingerprint="face:j", kind="face"),
               _defect(DefectCode.small_edge, fingerprint="edge:r",
                       severity=DefectSeverity.warning, index=2))

    candidate = planner.propose(defects, RunHistory(), shape=None)

    assert candidate is not None and candidate.defect_fingerprint == "edge:r"
    only_reserved = planner.propose(defects[:1], RunHistory(), shape=None)
    assert only_reserved is None


def test_with_nothing_reserved_the_planner_still_claims_every_code():
    planner = RulesPlanner(ops=(_GLOBAL,))
    defects = (_defect(DefectCode.invalid_brep, fingerprint="face:j", kind="face"),)
    assert planner.propose(defects, RunHistory(), shape=None).op.name == "fix_shape_global"


def test_the_rules_planner_does_not_aim_another_codes_fix_at_an_entity_held_for_review():
    # The DirectedPlanner refuses a model's scoped op aimed at an entity carrying a held defect;
    # the rules planner must agree, not merely skip the held CODE. Here the face carries both a
    # held invalid_brep and a repairable small_face: the small_face fix would change the face the
    # engineer is being asked about.
    narrow = _Op("narrow", {DefectCode.small_face}, 1)
    planner = RulesPlanner(ops=(narrow,), reserved=frozenset({DefectCode.invalid_brep}))
    shared = (_defect(DefectCode.small_face, fingerprint="face:b", kind="face", index=2),
              _defect(DefectCode.invalid_brep, fingerprint="face:b", kind="face", index=2))
    assert planner.propose(shared, RunHistory(), shape=None) is None

    elsewhere = (_defect(DefectCode.small_face, fingerprint="face:c", kind="face", index=3),
                 _defect(DefectCode.invalid_brep, fingerprint="face:b", kind="face", index=2))
    candidate = planner.propose(elsewhere, RunHistory(), shape=None)
    assert candidate is not None and candidate.defect_fingerprint == "face:c"


# THE SELF-INTERSECTION HEAL'S CLAIM (Plan ruling 5)
#
# `heal_self_intersections` answers `self_intersection`, but it aims only at a FACE finding that
# carries the analyzer's partner evidence. Counting every op that merely answers a code as a claim
# would silence the refusal a wire-level finding has always produced, so a claim is an operation
# that offered targets - or one the run's history skipped (tried and rolled back, or a fill the
# run has refused).

WIRE_LEVEL_SELF_INTERSECTION = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="wire",
    entity_index=4, message="SelfIntersectingWire", fingerprint="wire:w4",
    kernel_status="SelfIntersectingWire")
FACE_FINDING_WITH_PARTNERS = EntityDefect(
    code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
    entity_index=3, message="crosses 2", fingerprint="face:f3", kernel_status="SelfInterference",
    measurements={"crossing_face_indices": [7, 9], "crossing_faces": ["face:f7", "face:f9"]})


def test_a_wire_level_self_intersection_is_still_reported_unclaimed(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    planner = RulesPlanner()                 # real catalogue
    history = RunHistory()
    assert planner.propose([WIRE_LEVEL_SELF_INTERSECTION], history, shape=None) is None
    assert [r["reason"] for r in history.refusals] == ["no_operation_claims_it"]


def test_a_held_self_intersection_is_not_proposed_by_the_rules_planner():
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    planner = RulesPlanner(reserved=REVIEW_RESERVED_CODES)
    assert planner.propose([FACE_FINDING_WITH_PARTNERS], RunHistory(), shape=None) is None


def test_a_held_self_intersection_stays_held_with_the_heal_enabled(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    planner = RulesPlanner(reserved=REVIEW_RESERVED_CODES)
    history = RunHistory()
    assert planner.propose([FACE_FINDING_WITH_PARTNERS], history, shape=None) is None
    assert history.refusals == []


def test_with_review_off_the_rules_planner_proposes_the_heal_for_a_crossing_face(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    candidate = RulesPlanner().propose([FACE_FINDING_WITH_PARTNERS], RunHistory(), shape=None)
    assert candidate is not None and candidate.op.name == "heal_self_intersections"
    assert [t.fingerprint for t in candidate.targets] == ["face:f3", "face:f7", "face:f9"]


def test_with_the_heal_disabled_a_crossing_face_is_unclaimed_as_before(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    history = RunHistory()
    assert RulesPlanner().propose([FACE_FINDING_WITH_PARTNERS], history, shape=None) is None
    assert [r["reason"] for r in history.refusals] == ["no_operation_claims_it"]


def test_a_heal_that_was_rolled_back_still_counts_as_a_claim(monkeypatch):
    # Tried and failed is not "nothing claims it": the refusal would tell a reader no operation
    # exists for a defect one just attempted.
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    history = RunHistory()
    history.mark_ineffective("face:f3", "heal_self_intersections")
    assert RulesPlanner().propose([FACE_FINDING_WITH_PARTNERS], history, shape=None) is None
    assert history.refusals == []


def test_a_refused_fill_still_counts_as_a_claim():
    # The run-level fill refusal skips a fill before its targets are asked for; that defect is
    # claimed (and refused for a reason already recorded), not unclaimed.
    fill = _Op("fill_planar_hole", {DefectCode.open_shell}, 5)
    history = RunHistory()
    history.refuse_filling("paired_rim")
    loop = _defect(DefectCode.open_shell, fingerprint="edge:rim")
    assert RulesPlanner(ops=(fill,)).propose((loop,), history, shape=None) is None
    assert history.refusals == []


def test_an_operation_that_answers_but_offers_nothing_is_not_a_claim():
    class _Silent(_Op):
        def targets_for(self, defect):
            return ()

    history = RunHistory()
    silent = _Silent("silent", {DefectCode.small_edge}, 1)
    assert RulesPlanner(ops=(silent,)).propose(
        (_defect(DefectCode.small_edge, fingerprint="edge:a"),), history, shape=None) is None
    assert [r["reason"] for r in history.refusals] == ["no_operation_claims_it"]
