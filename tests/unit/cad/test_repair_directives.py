# Responsibility: Verify directives are proposed before the rules planner, that an unplayable one is reported rather than dropped, and that a directive never bypasses the session's judgement.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.directives import DirectedPlanner
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.loop import LoopBudget, run
from meshpipeline.cad.repair.operations import OpOutcome
from meshpipeline.cad.repair.plan import Candidate
from meshpipeline.cad.repair.session import RepairSession

_SOUND = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                 diagonal_mm=200.0, max_tolerance_mm=0.01,
                 surface_area_mm2=1000.0, volume_mm3=8000.0)


def _defect(fingerprint="edge:a", index=1) -> EntityDefect:
    return EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.error,
                        entity_type="edge", entity_index=index, message="x",
                        fingerprint=fingerprint)


class _Op:
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def targets_for(self, defect):
        return (EntityRef(entity_type="edge", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        self.calls += 1
        return OpOutcome(shape=f"{self.name}-{self.calls}")


class _Fallback:
    label = "fallback"

    def __init__(self, op):
        self._op = op
        self.calls = 0

    def propose(self, defects, history, *, shape):
        self.calls += 1
        for d in defects:
            if not history.was_ineffective(d.fingerprint, self._op.name):
                return Candidate(op=self._op, targets=self._op.targets_for(d),
                                 defect_code=d.code, defect_fingerprint=d.fingerprint)
        return None


def _directive(op="directed_op", targets=("edge:a",), source="engineer", item_id="i1"):
    return {"op": op, "targets": list(targets), "defect_code": "small_edge",
            "defect_fingerprint": targets[0], "source": source, "item_id": item_id}


def _session(scans):
    queue = list(scans)

    def scanner(_shape, valid=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return RepairSession("original", measurer=lambda _s: _SOUND, scanner=scanner)


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_TIMEOUT_S", 120.0)


def test_directives_are_proposed_before_the_rules_planner():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    fallback = _Fallback(rules)
    planner = DirectedPlanner([_directive()], fallback, ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((_defect(),), True), ((), True)]),
                 tolerance_mm=0.01)

    assert [v.op for v in result.verdicts] == ["directed_op"]
    assert directed.calls == 1 and rules.calls == 0
    assert result.planner_label == "directed-v1"


def test_an_unplayable_directive_is_recorded_not_skipped_silently():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    gone = _directive(targets=("edge:vanished",), item_id="gone")
    unknown = _directive(op="no_such_op", item_id="odd")
    planner = DirectedPlanner([gone, unknown], _Fallback(rules), ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((_defect(),), True), ((), True)]),
                 tolerance_mm=0.01)

    unplayable = [r for r in result.refusals if r.get("refusal") == "directive_unplayable"]
    assert {(r["directive"]["item_id"], r["reason"]) for r in unplayable} == {
        ("gone", "target_absent"), ("odd", "unknown_op")}
    assert directed.calls == 0
    assert [v.op for v in result.verdicts] == ["rules_op"]


def test_exhausted_directives_fall_back_to_rules():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    fallback = _Fallback(rules)
    planner = DirectedPlanner([_directive()], fallback, ops=(directed, rules))

    result = run(None, planner=planner,
                 session=_session([((_defect(),), True), ((_defect("edge:b", 2),), True),
                                   ((), True)]),
                 tolerance_mm=0.01)

    assert [v.op for v in result.verdicts] == ["directed_op", "rules_op"]
    assert fallback.calls >= 1


def test_a_replay_directive_and_an_engineer_directive_keep_their_source():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    replay = _directive(targets=("edge:gone",), source="replay", item_id="")
    engineer = _directive(op="no_such_op", source="engineer", item_id="e1")
    planner = DirectedPlanner([replay, engineer], _Fallback(rules), ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((_defect(),), True), ((), True)]),
                 tolerance_mm=0.01)

    sources = {r["directive"]["item_id"]: r["directive"]["source"] for r in result.refusals
               if r.get("refusal") == "directive_unplayable"}
    assert sources == {"": "replay", "e1": "engineer"}


def test_a_directive_is_not_a_cap_waiver_a_worsening_directive_is_rolled_back():
    # The session judges a directed candidate exactly as it judges a rules candidate: the defect
    # persists, so the progress rule rolls the operation back.
    directed, rules = _Op("directed_op"), _Op("rules_op")
    planner = DirectedPlanner([_directive()], _Fallback(rules), ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((_defect(),), True)]),
                 tolerance_mm=0.01)

    assert result.verdicts[0].op == "directed_op" and not result.verdicts[0].kept


def test_a_directive_left_queued_by_an_exhausted_budget_is_reported_not_reached():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    planner = DirectedPlanner([_directive(item_id="a"), _directive(item_id="b")],
                              _Fallback(rules), ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((_defect(),), True), ((_defect(),), True)]),
                 budget=LoopBudget(max_ops=1, stall_limit=99, timeout_s=120.0), tolerance_mm=0.01)

    left = [r for r in result.refusals if r.get("reason") == "not_reached"]
    assert [r["directive"]["item_id"] for r in left] == ["b"]
    assert left[0]["refusal"] == "directive_unplayable"


def test_directives_are_reported_not_reached_when_the_first_scan_is_incomplete():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    planner = DirectedPlanner([_directive(item_id="a")], _Fallback(rules), ops=(directed, rules))

    result = run(None, planner=planner, session=_session([((), False)]), tolerance_mm=0.01)

    assert [r["directive"]["item_id"] for r in result.refusals
            if r.get("reason") == "not_reached"] == ["a"]


def test_a_malformed_defect_code_is_reported_malformed():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    bad = {**_directive(item_id="m"), "defect_code": "not_a_code"}
    planner = DirectedPlanner([bad], _Fallback(rules), ops=(directed, rules))

    planner.propose((_defect(),), __import__("meshpipeline").cad.repair.plan.RunHistory(),
                    shape=None)

    assert [u["reason"] for u in planner.unplayable] == ["malformed"]


def test_a_bare_string_target_is_one_fingerprint_not_its_characters():
    directed, rules = _Op("directed_op"), _Op("rules_op")
    d = {**_directive(), "targets": "edge:a"}
    candidate = DirectedPlanner([d], _Fallback(rules), ops=(directed, rules)).propose(
        (_defect(),), __import__("meshpipeline").cad.repair.plan.RunHistory(), shape=None)

    assert [t.fingerprint for t in candidate.targets] == ["edge:a"]


def _rim(fingerprint, index):
    return EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                        entity_type="edge", entity_index=index, message="rim",
                        fingerprint=fingerprint, measurements={"boundary_edges": 4})


def _fill_on(targets, source="engineer"):
    return {"op": "fill_planar_hole", "targets": list(targets), "defect_code": "open_shell",
            "defect_fingerprint": targets[0], "source": source, "item_id": "i-fill"}


def test_an_engineer_fill_on_both_rims_of_a_pair_is_refused_by_the_rim_guard(monkeypatch):
    # ONE decision naming both ends of a missing bore would seal it: the override is for one rim.
    import meshpipeline.cad.repair.directives as directives
    from meshpipeline.cad.repair.plan import RunHistory
    monkeypatch.setattr(directives, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"rim:a", "rim:b"}))
    rims = (_rim("rim:a", 1), _rim("rim:b", 2))

    class _Nothing:
        def propose(self, *a, **k): return None
    planner = DirectedPlanner([_fill_on(["rim:a", "rim:b"])], _Nothing())
    assert planner.propose(rims, RunHistory(), shape="shape") is None
    assert [u["reason"] for u in planner.unplayable] == ["fill_refused"]

    single = DirectedPlanner([_fill_on(["rim:a"])], _Nothing())
    candidate = single.propose(rims, RunHistory(), shape="shape")
    assert candidate is not None and [t.fingerprint for t in candidate.targets] == ["rim:a"]


def test_only_an_engineers_directive_may_aim_at_a_defect_held_for_review():
    """A reserved code is the engineer's question: a model's (or a replay's) directive aimed at it
    is recorded as unplayable, while the engineer's own answer on it is played."""
    from meshpipeline.cad.repair.plan import RunHistory

    directed = _Op("directed_op")
    reserved = frozenset({DefectCode.small_edge})
    model = _directive(source="llm-planner", item_id="")
    engineer = _directive(source="engineer", item_id="i1")
    planner = DirectedPlanner([model, engineer], _Fallback(directed), ops=(directed,),
                              reserved=reserved)

    candidate = planner.propose((_defect(),), RunHistory(), shape=None)

    assert candidate is not None and candidate.item_id == "i1"
    assert [(u["directive"]["source"], u["reason"]) for u in planner.unplayable] == [
        ("llm-planner", "reserved_for_review")]


def test_one_hole_of_a_group_fill_still_meets_the_rim_guard(monkeypatch):
    """A group fill is split into one single-target fill per hole, but it is still one decision
    over many - so the single-rim override never applies to a `group` fill."""
    import meshpipeline.cad.repair.directives as directives
    from meshpipeline.cad.repair.plan import RunHistory
    monkeypatch.setattr(directives, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"rim:a", "rim:b"}))
    rims = (_rim("rim:a", 1), _rim("rim:b", 2))

    class _Nothing:
        def propose(self, *a, **k): return None
    grouped = DirectedPlanner([{**_fill_on(["rim:a"]), "group": True}], _Nothing())
    assert grouped.propose(rims, RunHistory(), shape="shape") is None
    assert [u["reason"] for u in grouped.unplayable] == ["fill_refused"]


def test_the_nonplanar_fill_meets_the_same_rim_guard_as_the_planar_one(monkeypatch):
    """A curved patch over one rim of a missing passage seals it exactly as a flat one would, so
    a model's or a group's `fill_nonplanar_hole` on a paired rim is refused; an engineer's
    individual look at that one rim still plays."""
    import meshpipeline.cad.repair.directives as directives
    from meshpipeline.cad.repair.plan import RunHistory
    monkeypatch.setattr(directives, "paired_rim_fingerprints",
                        lambda shape, defects: frozenset({"rim:a", "rim:b"}))
    rims = (_rim("rim:a", 1), _rim("rim:b", 2))

    class _Nothing:
        def propose(self, *a, **k): return None

    def nonplanar(**extra):
        return {**_fill_on(["rim:a"]), "op": "fill_nonplanar_hole", **extra}

    for refused in (nonplanar(source="llm-planner", item_id=""), nonplanar(group=True)):
        planner = DirectedPlanner([refused], _Nothing())
        assert planner.propose(rims, RunHistory(), shape="shape") is None
        assert [u["reason"] for u in planner.unplayable] == ["fill_refused"]

    individual = DirectedPlanner([nonplanar()], _Nothing())
    candidate = individual.propose(rims, RunHistory(), shape="shape")
    assert candidate is not None and candidate.op.name == "fill_nonplanar_hole"
    assert [t.fingerprint for t in candidate.targets] == ["rim:a"]


def test_an_automatic_fill_is_refused_when_the_rim_check_could_not_run(monkeypatch):
    """FAIL CLOSED. An unanswered pairing question is not "this rim has no partner", so a model's
    or a replay's fill is refused rather than played on a guess - even when the unknown answer
    names no rim at all. (An engineer's single-rim fill is a person's look at that hole and keeps
    its override.)"""
    import meshpipeline.cad.repair.directives as directives
    from meshpipeline.cad.repair.plan import RimsUnknown, RunHistory
    monkeypatch.setattr(directives, "paired_rim_fingerprints",
                        lambda shape, defects: RimsUnknown())

    class _Nothing:
        def propose(self, *a, **k): return None
    for source in ("llm-planner", "replay"):
        planner = DirectedPlanner([_fill_on(["rim:a"], source=source)], _Nothing())
        assert planner.propose((_rim("rim:a", 1),), RunHistory(), shape="shape") is None
        assert [u["reason"] for u in planner.unplayable] == ["fill_refused"]


class _Captured(Exception):
    pass


@pytest.mark.parametrize("review, expected", [
    (True, {DefectCode.invalid_brep, DefectCode.self_intersection,
            DefectCode.non_manifold_surface, DefectCode.small_face}),
    (False, set())])
def test_repair_step_file_reserves_the_judgement_codes_only_when_review_is_on(
        monkeypatch, tmp_path, review, expected):
    """The hold-back is review's: with review off the loop aims at every code as it always did."""
    pytest.importorskip("OCP.BRep", reason="repair_step_file checks the kernel is present")
    import meshpipeline.cad.repair.brep as brep
    import meshpipeline.cad.repair.caps as caps
    import meshpipeline.cad.repair.loop as loop
    from meshpipeline.cad.repair.conservative import repair_step_file
    from meshpipeline.cad.repair.plan import RulesPlanner

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", review)
    monkeypatch.setattr(brep, "_read_shape", lambda path: "shape")
    monkeypatch.setattr(caps, "measure", lambda shape: _SOUND)
    seen = []

    def _run(shape, *, planner=None, **kw):
        seen.append(planner)
        raise _Captured

    monkeypatch.setattr(loop, "run", _run)
    for directives in ((), (_directive(source="llm-planner", item_id=""),)):
        with pytest.raises(_Captured):
            repair_step_file(tmp_path / "in.step", tmp_path / "out.step", directives=directives)
    rules, directed = seen
    assert isinstance(rules, RulesPlanner) and rules._reserved == expected
    assert isinstance(directed, DirectedPlanner) and directed._reserved == expected
    assert directed._fallback._reserved == expected


def test_a_model_directive_on_a_code_nobody_holds_back_still_plays_under_reservation():
    """The reservation is per code, not a ban on the model: an llm-planner directive aimed at a
    repairable defect plays first while the judgement codes are held."""
    from meshpipeline.cad.repair.plan import RunHistory

    directed = _Op("directed_op")
    planner = DirectedPlanner([_directive(source="llm-planner", item_id="")],
                              _Fallback(directed), ops=(directed,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    candidate = planner.propose((_defect(),), RunHistory(), shape=None)
    assert candidate is not None and candidate.source == "llm-planner"
    assert planner.unplayable == []


class _WholeShapeOp(_Op):
    """Stands in for fix_shape_global: runs over the whole shape, names targets for bookkeeping."""
    answers = frozenset({DefectCode.invalid_brep, DefectCode.small_edge})
    scope = 99


def _brep_defect(fingerprint="face:b", index=2) -> EntityDefect:
    return EntityDefect(code=DefectCode.invalid_brep, severity=DefectSeverity.error,
                        entity_type="face", entity_index=index, message="x",
                        fingerprint=fingerprint)


def _model_whole_shape(targets):
    return {"op": "whole_op", "targets": list(targets), "defect_code": "invalid_brep",
            "defect_fingerprint": targets[0] if targets else "", "source": "llm-planner",
            "item_id": ""}


class _Nothing:
    label = "nothing"

    def propose(self, *a, **k):
        return None


@pytest.mark.parametrize("targets", [(), ("face:b",)])
def test_a_model_whole_shape_heal_on_a_held_code_is_refused(targets):
    """A whole-shape heal on a held code would answer the engineer's question before they did:
    the session keeps it exactly when it removes the held defect. Refused, named or target-less."""
    from meshpipeline.cad.repair.plan import RunHistory

    whole = _WholeShapeOp("whole_op")
    planner = DirectedPlanner([_model_whole_shape(targets)], _Nothing(), ops=(whole,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    assert planner.propose((_brep_defect(),), RunHistory(), shape=None) is None
    assert [u["reason"] for u in planner.unplayable] == ["reserved_for_review"]


def test_a_model_whole_shape_heal_on_a_code_nobody_holds_plays():
    from meshpipeline.cad.repair.plan import RunHistory

    whole = _WholeShapeOp("whole_op")
    directive = {**_model_whole_shape(()), "defect_code": "small_edge"}
    planner = DirectedPlanner([directive], _Nothing(), ops=(whole,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    candidate = planner.propose((_defect("edge:a"), _brep_defect("face:b")), RunHistory(),
                                shape=None)
    assert candidate is not None and candidate.op is whole and candidate.source == "llm-planner"
    # target-less: named by the defects carrying its own code, for the progress rule
    assert [t.fingerprint for t in candidate.targets] == ["edge:a"]
    assert planner.unplayable == []


def test_a_model_whole_shape_heal_whose_target_carries_a_held_defect_is_refused():
    from meshpipeline.cad.repair.plan import RunHistory

    whole = _WholeShapeOp("whole_op")
    directive = {**_model_whole_shape(("face:b",)), "defect_code": "small_edge"}
    shared = (_defect("face:b"), _brep_defect("face:b"))
    planner = DirectedPlanner([directive], _Nothing(), ops=(whole,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    assert planner.propose(shared, RunHistory(), shape=None) is None
    assert [u["reason"] for u in planner.unplayable] == ["reserved_for_review"]


@pytest.mark.parametrize("source", ["engineer", "replay"])
@pytest.mark.parametrize("whole_shape", [False, True])
def test_an_engineer_or_replay_directive_at_a_held_entity_plays(source, whole_shape):
    """The engineer is the person being asked; a replay is an operation this upload already kept,
    replayed so the re-run reproduces the same file. Neither is refused reserved_for_review."""
    from meshpipeline.cad.repair.plan import RunHistory

    op = _WholeShapeOp("whole_op") if whole_shape else _Op("directed_op")
    held_code = {**_directive(op=op.name, targets=("face:b",), source=source,
                              item_id="i1" if source == "engineer" else ""),
                 "defect_code": "invalid_brep" if whole_shape else "small_edge"}
    planner = DirectedPlanner([held_code], _Nothing(), ops=(op,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    candidate = planner.propose((_defect("face:b"), _brep_defect("face:b")), RunHistory(),
                                shape=None)
    assert candidate is not None and candidate.source == source
    assert planner.unplayable == []


def test_a_target_less_whole_shape_heal_with_nothing_carrying_its_code_is_target_absent():
    from meshpipeline.cad.repair.plan import RunHistory

    # a code nobody holds (a held code is refused before its targets are looked at)
    directive = {**_model_whole_shape(()), "defect_code": "small_edge"}
    planner = DirectedPlanner([directive], _Nothing(), ops=(_WholeShapeOp("whole_op"),),
                              reserved=frozenset({DefectCode.invalid_brep}))
    assert planner.propose((_brep_defect(),), RunHistory(), shape=None) is None
    assert [u["reason"] for u in planner.unplayable] == ["target_absent"]


def test_a_scoped_model_directive_at_an_entity_held_for_review_stays_refused():
    """The hold-back is about aim: a scoped operation whose target carries a held defect is refused
    for a model even when the directive's own code is not held."""
    from meshpipeline.cad.repair.plan import RunHistory

    class _Nothing:
        def propose(self, *a, **k): return None
    directed = _Op("directed_op")
    shared = (_defect("face:b"), _brep_defect("face:b"))
    planner = DirectedPlanner([_directive(targets=("face:b",), source="llm-planner", item_id="")],
                              _Nothing(), ops=(directed,),
                              reserved=frozenset({DefectCode.invalid_brep}))
    assert planner.propose(shared, RunHistory(), shape=None) is None
    assert [u["reason"] for u in planner.unplayable] == ["reserved_for_review"]

    engineer = DirectedPlanner([_directive(targets=("face:b",))], _Nothing(), ops=(directed,),
                               reserved=frozenset({DefectCode.invalid_brep}))
    assert engineer.propose(shared, RunHistory(), shape=None) is not None


# THE SELF-INTERSECTION HEAL UNDER THE JUDGEMENT HOLD
#
# `self_intersection` is in REVIEW_RESERVED_CODES. With review on, the heal plays only through
# the engineer's own directive; a model's is refused before its targets are looked at. With review
# off nothing is held and a model's heal plays like any other directive.

def _crossing_face(fingerprint="face:f3", index=3, partners=((7, "face:f7"),)):
    return EntityDefect(
        code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
        entity_index=index, message="crosses", fingerprint=fingerprint,
        kernel_status="SelfInterference",
        measurements={"crossing_face_indices": [i for i, _ in partners],
                      "crossing_faces": [f for _, f in partners]})


def _heal_on(targets, source):
    return {"op": "heal_self_intersections", "targets": list(targets),
            "defect_code": "self_intersection", "defect_fingerprint": targets[0],
            "source": source, "item_id": "i-heal" if source == "engineer" else ""}


_CROSSING = (_crossing_face(), _crossing_face("face:f7", 7, ((3, "face:f3"),)))


def test_an_engineer_heal_directive_on_a_held_face_plays():
    from meshpipeline.cad.repair.plan import RunHistory
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    planner = DirectedPlanner([_heal_on(["face:f3", "face:f7"], "engineer")], _Nothing(),
                              reserved=REVIEW_RESERVED_CODES)
    candidate = planner.propose(_CROSSING, RunHistory(), shape=None)
    assert candidate is not None and candidate.op.name == "heal_self_intersections"
    assert candidate.source == "engineer"
    assert [(t.fingerprint, t.entity_index) for t in candidate.targets] == [
        ("face:f3", 3), ("face:f7", 7)]
    assert planner.unplayable == []


@pytest.mark.parametrize("review_on, played", [(True, False), (False, True)])
def test_a_model_heal_directive_is_held_only_while_review_is_on(review_on, played):
    from meshpipeline.cad.repair.plan import RunHistory
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    reserved = REVIEW_RESERVED_CODES if review_on else frozenset()
    planner = DirectedPlanner([_heal_on(["face:f3", "face:f7"], "llm-planner")], _Nothing(),
                              reserved=reserved)
    candidate = planner.propose(_CROSSING, RunHistory(), shape=None)
    if played:
        assert candidate is not None and candidate.op.name == "heal_self_intersections"
        assert candidate.source == "llm-planner" and planner.unplayable == []
    else:
        assert candidate is None
        assert [u["reason"] for u in planner.unplayable] == ["reserved_for_review"]


# THE ACCEPT PATH NAMES ONE FACE. The review proposer writes one review item per crossing face and
# its proposal names that face alone; accepting it hands the planner `targets: [that face]`. Every
# analyzer crossing involves two faces at least, so the raw list made the heal's pre-analysis find
# "no crossing among the targets" while the scan's own `crossing_faces` listed the partners. The
# directive is widened to the whole component the scan measured, as the rules planner aims it.

_TRIANGLE = (_crossing_face("face:f3", 3, ((7, "face:f7"), (9, "face:f9"))),
             _crossing_face("face:f7", 7, ((3, "face:f3"), (9, "face:f9"))))


@pytest.fixture
def _heal_flag(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)


@pytest.mark.usefixtures("_heal_flag")
def test_a_one_face_engineer_heal_is_aimed_at_the_whole_crossing_component():
    from meshpipeline.cad.repair.plan import RunHistory
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    planner = DirectedPlanner([_heal_on(["face:f3"], "engineer")], _Nothing(),
                              reserved=REVIEW_RESERVED_CODES)
    candidate = planner.propose(_TRIANGLE, RunHistory(), shape=None)
    assert candidate is not None and candidate.op.name == "heal_self_intersections"
    assert [(t.fingerprint, t.entity_index) for t in candidate.targets] == [
        ("face:f3", 3), ("face:f7", 7), ("face:f9", 9)]
    # the item it answers and the defect it is judged on are still the face the engineer accepted
    assert candidate.defect_fingerprint == "face:f3" and candidate.item_id == "i-heal"
    assert planner.unplayable == []


@pytest.mark.usefixtures("_heal_flag")
def test_a_heal_already_naming_its_component_is_not_doubled():
    from meshpipeline.cad.repair.plan import RunHistory

    planner = DirectedPlanner([_heal_on(["face:f3", "face:f7"], "engineer")], _Nothing())
    candidate = planner.propose(_CROSSING, RunHistory(), shape=None)
    assert [(t.fingerprint, t.entity_index) for t in candidate.targets] == [
        ("face:f3", 3), ("face:f7", 7)]


@pytest.mark.usefixtures("_heal_flag")
def test_a_widened_one_face_model_heal_is_still_held_while_review_is_on():
    from meshpipeline.cad.repair.plan import RunHistory
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    planner = DirectedPlanner([_heal_on(["face:f3"], "llm-planner")], _Nothing(),
                              reserved=REVIEW_RESERVED_CODES)
    assert planner.propose(_TRIANGLE, RunHistory(), shape=None) is None
    assert [u["reason"] for u in planner.unplayable] == ["reserved_for_review"]


@pytest.mark.usefixtures("_heal_flag")
def test_a_heal_at_a_face_without_crossing_evidence_keeps_the_face_it_named():
    # No `crossing_faces` (a wire-level finding): nothing to widen to, so the heal meets its own
    # not_isolated decline rather than a guess at partners.
    from meshpipeline.cad.repair.plan import RunHistory

    bare = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                        entity_type="face", entity_index=4, message="wire crosses",
                        fingerprint="face:f4")
    planner = DirectedPlanner([_heal_on(["face:f4"], "engineer")], _Nothing())
    candidate = planner.propose((bare,), RunHistory(), shape=None)
    assert [(t.fingerprint, t.entity_index) for t in candidate.targets] == [("face:f4", 4)]


@pytest.mark.usefixtures("_heal_flag")
def test_another_ops_one_target_directive_at_a_crossing_face_is_not_widened():
    from meshpipeline.cad.repair.plan import RunHistory

    directive = {**_heal_on(["face:f3"], "engineer"), "op": "fix_face_boundary"}
    planner = DirectedPlanner([directive], _Nothing())
    candidate = planner.propose(_TRIANGLE, RunHistory(), shape=None)
    assert candidate is not None and candidate.op.name == "fix_face_boundary"
    assert [t.fingerprint for t in candidate.targets] == ["face:f3"]
