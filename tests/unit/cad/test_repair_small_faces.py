# Responsibility: Verify the small-face sweep's catalogue declarations and consent flag, the
#                  face-removal ACCOUNTING in the caps (a removal is accepted only when an
#                  operation declared exactly it), the session rollback an over-removing sweep
#                  earns, and triage's reroute of a sliver-only file to the conservative repair.
# Boundaries: no kernel - the operation's real geometry work is proved in
#             tests/native/test_native_repair_small_faces.py. What is pinned here is pure logic:
#             declarations, arithmetic and routing.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure, sanity_refusal
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityMapping
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.operations import (
    MAX_SMALL_FACE_REMOVALS,
    OPERATIONS,
    FixSmallFaces,
    OpOutcome,
)
from meshpipeline.cad.repair.session import KEPT, ROLLED_BACK, RepairSession
from meshpipeline.cad.repair.triage import (
    ROUTE_CONSERVATIVE_REPAIR,
    ROUTE_MANUAL_CLEANUP,
    recommend,
)


@pytest.fixture
def sweep_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)


def _small_face_defect(index=6, fingerprint="face:aaaa") -> EntityDefect:
    return EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.warning,
                        entity_type="face", entity_index=index, message="sliver",
                        fingerprint=fingerprint)


# THE CATALOGUE DECLARATIONS


def test_the_sweep_is_in_the_catalogue_and_claims_exactly_the_sliver_defects():
    op = next(o for o in OPERATIONS if o.name == "fix_small_faces")
    # small_face, and small_edge too: a sliver face OWNS sliver edges, and removing the face is
    # what answers them - without the claim, a file whose sliver was found through its edges
    # spends the stall budget on edge fixes and the sweep is never proposed. Nothing else: a wider
    # claim would let the planner reach a face-removing tool for a defect nobody consented to
    # defeature over.
    assert op.answers == frozenset({DefectCode.small_face, DefectCode.small_edge})


def test_with_consent_the_sweep_also_aims_at_a_located_sliver_edge(sweep_on):
    edge = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                        entity_type="edge", entity_index=9, message="sliver edge",
                        fingerprint="edge:bbbb")
    targets = FixSmallFaces().targets_for(edge)
    assert [(t.entity_type, t.fingerprint, t.entity_index) for t in targets] == \
        [("edge", "edge:bbbb", 9)]


def test_without_consent_the_sweep_offers_no_targets():
    # The flag is the consent to REMOVE geometry; with it absent the planner must never even see
    # a candidate, exactly as the fills and the heal behave behind their own flags.
    assert rcfg.CAD_REPAIR_FIX_SMALL_FACES is False
    assert FixSmallFaces().targets_for(_small_face_defect()) == ()


def test_with_consent_the_sweep_aims_at_the_located_face(sweep_on):
    targets = FixSmallFaces().targets_for(_small_face_defect())
    assert [(t.entity_type, t.fingerprint, t.entity_index) for t in targets] == \
        [("face", "face:aaaa", 6)]


def test_the_sweep_offers_no_target_for_an_unlocated_small_face(sweep_on):
    # No fingerprint means nothing to verify progress against, so nothing to aim at.
    unlocated = EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.warning,
                             entity_type="face", entity_index=0, message="x")
    assert FixSmallFaces().targets_for(unlocated) == ()


def test_the_sweep_declines_rather_than_running_without_consent():
    # Defence in depth, the fills' pattern: even handed targets directly (a model could), the
    # operation itself refuses while the flag is off.
    outcome = FixSmallFaces().apply(object(), (), tolerance_mm=0.1)
    assert "CAD_REPAIR_FIX_SMALL_FACES" in str(outcome.evidence.get("declined"))


# THE FACE-REMOVAL ACCOUNTING IN THE CAPS


_BEFORE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)


def _after(faces: int) -> Measure:
    return Measure(**{**_BEFORE.to_dict(), "faces": faces})


def test_a_face_removal_nobody_declared_is_still_refused(monkeypatch):
    # The prior behaviour, pinned: deleting geometry silently is defeaturing, and the declaration
    # path below must not have widened this for every other operation.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    assert "removed" in sanity_refusal(_BEFORE, _after(19))


def test_a_removal_the_operation_declared_is_accepted(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    assert sanity_refusal(_BEFORE, _after(19), declared_faces_removed=1) == ""


def test_a_removal_beyond_the_declaration_is_refused(monkeypatch):
    # Accounted, not allowed: a declaration of one face excuses one face, never a second.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    assert "removed" in sanity_refusal(_BEFORE, _after(18), declared_faces_removed=1)


# THE SESSION'S JUDGEMENT OF A SWEEP


class _SweepDouble:
    """A small-face sweep double: hands back whatever outcome the test scripted."""

    name = "fix_small_faces"
    answers = frozenset({DefectCode.small_face})
    scope = 3

    def __init__(self, outcome):
        self._outcome = outcome

    def targets_for(self, defect):  # pragma: no cover - not used by these tests
        return ()

    def apply(self, shape, targets, *, tolerance_mm):
        return self._outcome


def _session(*, scans, measures):
    scan_queue = list(scans)
    measure_queue = list(measures)

    def scanner(_shape, valid=None):
        return scan_queue.pop(0) if len(scan_queue) > 1 else scan_queue[0]

    def measurer(_shape):
        return measure_queue.pop(0) if len(measure_queue) > 1 else measure_queue[0]

    return RepairSession("original", measurer=measurer, scanner=scanner)


def _targets_of(defect):
    from meshpipeline.cad.repair.identity import EntityRef

    return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                      entity_index=defect.entity_index),)


def test_a_sweep_that_removed_more_than_it_declared_is_rolled_back():
    # THE DAMAGE CASE. The kernel's strip fix took a neighbour with it: two faces gone, one
    # declared. The cap refuses, the snapshot comes back, and the log says why.
    defect = _small_face_defect()
    outcome = OpOutcome(shape="damaged", mapping=EntityMapping.empty(),
                        evidence={"faces_removed": 1})
    session = _session(scans=[((defect,), True)],
                       measures=[_BEFORE, _BEFORE, _after(18)])

    verdict = session.try_op(_SweepDouble(outcome), _targets_of(defect), defect.code,
                             tolerance_mm=0.1)

    assert verdict.status == ROLLED_BACK
    assert verdict.reason.startswith("cap:")
    assert session.shape == "original", "the snapshot was not restored"


def test_a_sweep_that_removed_exactly_what_it_declared_and_cleared_its_defect_is_kept():
    defect = _small_face_defect()
    outcome = OpOutcome(shape="swept",
                        mapping=EntityMapping(moved={}, gone=frozenset({defect.fingerprint}),
                                              basis="kernel"),
                        evidence={"faces_removed": 1})
    session = _session(scans=[((defect,), True), ((), True)],
                       measures=[_BEFORE, _BEFORE, _after(19)])

    verdict = session.try_op(_SweepDouble(outcome), _targets_of(defect), defect.code,
                             tolerance_mm=0.1)

    assert verdict.status == KEPT
    assert session.shape == "swept"


# TRIAGE'S REROUTE


def _located(*entities) -> dict:
    return {"report": {"summary": "inspected",
                       "defects": [{"code": e["code"], "severity": e["severity"]}
                                   for e in entities],
                       "entities": list(entities)}}


def _entity(code, *, entity="face:7", severity="warning") -> dict:
    return {"code": code, "severity": severity, "entity": entity, "region": "unknown",
            "message": f"{code} here", "measurements": {},
            "location": {"centroid": [1.0, 1.0, 1.0]}}


def test_without_the_sweep_a_sliver_only_file_still_goes_to_a_person():
    # The flag-off world, byte for byte: advising a repair no operation exists for would send the
    # loop out to attempt nothing and call that a service.
    advice = recommend(repair_status="clean", report=_located(_entity("small_face")))
    assert advice.route == ROUTE_MANUAL_CLEANUP


def test_a_sliver_only_file_is_rerouted_to_the_conservative_repair(sweep_on):
    advice = recommend(repair_status="clean", report=_located(_entity("small_face")))
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert advice.profile == "conservative"
    assert [t["entity"] for t in advice.targets] == ["face:7"]


def test_slivers_beside_repairable_defects_still_reroute_and_both_are_aimed_at(sweep_on):
    advice = recommend(
        repair_status="repairable",
        report=_located(_entity("small_face"),
                        _entity("small_edge", entity="edge:3")))
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert {t["entity"] for t in advice.targets} == {"face:7", "edge:3"}


def test_a_sliver_beside_a_real_judgement_defect_still_goes_to_a_person(sweep_on):
    # THE CAUTION PRESERVED: the sweep answers small_face and nothing else. A self-intersection
    # on the same part is still a question about intent, and repairing the easy half would not
    # settle the hard one.
    advice = recommend(
        repair_status="repairable",
        report=_located(_entity("small_face"),
                        _entity("self_intersection", entity="face:4", severity="error")))
    assert advice.route == ROUTE_MANUAL_CLEANUP


def test_more_slivers_than_one_sweep_may_remove_still_go_to_a_person(sweep_on):
    # A part carrying that many slivers is a modelling style, not writing debris, and the
    # operation itself would decline the sweep - so the advice says person, not an attempt that
    # is known in advance to refuse.
    many = [_entity("small_face", entity=f"face:{i}")
            for i in range(MAX_SMALL_FACE_REMOVALS + 1)]
    advice = recommend(repair_status="clean", report=_located(*many))
    assert advice.route == ROUTE_MANUAL_CLEANUP


def test_a_small_face_code_with_no_located_entity_still_goes_to_a_person(sweep_on):
    # Nothing to aim at means nothing to verify against; rerouting it would start a loop whose
    # planner refuses every defect as untargetable.
    report = {"report": {"defects": [{"code": "small_face", "severity": "warning"}],
                         "entities": []}}
    advice = recommend(repair_status="clean", report=report)
    assert advice.route == ROUTE_MANUAL_CLEANUP
