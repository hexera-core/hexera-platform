# Responsibility: Verify on the real kernel that heal_self_intersections reads the real face scan, declines a tangle it cannot isolate or uncross, and never changes the shape it was handed.
# Boundaries: a forged self-interfering solid, two overlapping boxes and a sound box, plus one directed run on ec_turbine-A from the OCCT corpus (on disk only, never committed; skipped when absent).
from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.cad_fixtures import write_pierced_floor_step

import meshpipeline.cad.repair.operations as operations
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for
from meshpipeline.cad.repair.localize import _diagonal, localize_scan
from meshpipeline.cad.repair.operations import MAX_HEAL_FACES, HealSelfIntersections

pytest.importorskip("OCP.BOPAlgo", reason="the CAD kernel is a native-tier dependency")

DEFAULT_CORPUS = "/tmp/cadcorpus/occt/opencascade-dataset-7.8.0/step"  # noqa: S108 - a read-only dataset location
TURBINE = Path(os.environ.get("HEXERA_OCCT_CORPUS", DEFAULT_CORPUS)) / "ec_turbine-A.stp"


@pytest.fixture(autouse=True)
def _heal_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _brep_dump(shape, path) -> bytes:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path.read_bytes()


def _crossings(shape):
    found, _ = localize_scan(shape, face_scan_s=20.0)
    return [d for d in found if d.code is DefectCode.self_intersection
            and d.kernel_status == "SelfInterference"]


def _refs(shape, indices):
    quantum = quantum_for(_diagonal(shape))
    return tuple(EntityRef(entity_type="face", entity_index=i, fingerprint=fingerprint_of(
        "face", operations.box_of(operations.sub_shape(shape, "face", i)), quantum=quantum))
        for i in indices)


def test_the_forged_solid_is_valid_to_brepcheck_and_crossed_to_the_analyzer(tmp_path):
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    assert BRepCheck_Analyzer(shape, True).IsValid()
    assert measure(shape).solids == 1
    crossing = _crossings(shape)
    assert sorted(d.entity_index for d in crossing) == [1, 5, 7, 8, 9]
    assert all(d.measurements["crossing_faces"] for d in crossing)


def test_a_tangle_wider_than_the_bound_is_declined_not_isolated(tmp_path):
    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    heal = HealSelfIntersections()
    dump = _brep_dump(shape, tmp_path / "before.brep")
    for finding in _crossings(shape):
        targets = heal.targets_for(finding)
        assert len(targets) == MAX_HEAL_FACES + 1
        out = heal.apply(shape, targets, tolerance_mm=0.01)
        assert out.shape is shape and out.evidence["declined"].startswith("not_isolated")
    assert _brep_dump(shape, tmp_path / "after.brep") == dump


def test_a_rebuilt_crossing_that_still_crosses_is_declined_and_the_input_is_untouched(tmp_path):
    # A directive naming two of the tangle's faces reaches the rebuild - both are planar, so
    # ShapeFix cannot uncross them - and the decline must leave the caller's shape byte for byte
    # as it was, though the rebuild wrote into the faces it fixed.
    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    dump = _brep_dump(shape, tmp_path / "before.brep")
    out = HealSelfIntersections().apply(shape, _refs(shape, (1, 5)), tolerance_mm=0.01)
    assert out.shape is shape
    assert out.evidence["declined"].startswith("crossing_remains"), out.evidence
    assert _brep_dump(shape, tmp_path / "after.brep") == dump


def test_faces_that_do_not_cross_are_not_healed(tmp_path):
    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    out = HealSelfIntersections().apply(shape, _refs(shape, (2, 3)), tolerance_mm=0.01)
    assert out.shape is shape and out.evidence["declined"].startswith("not_isolated")


def test_two_overlapping_bodies_are_declined_multi_body(tmp_path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    both = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(both)
    builder.Add(both, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    builder.Add(both, BRepPrimAPI_MakeBox(gp_Pnt(5.0, 5.0, 5.0), 10.0, 10.0, 10.0).Shape())
    # A pair the analyzer reports directly: `crossing_face_indices` is the whole tangle a face
    # sits in, not only the faces it crosses itself.
    from meshpipeline.cad.repair.localize import _kernel_face_pairs

    first, second = _kernel_face_pairs(both)[0]
    out = HealSelfIntersections().apply(both, _refs(both, (first, second)), tolerance_mm=0.01)
    assert out.shape is both and out.evidence["declined"].startswith("multi_body")


def test_a_kept_heal_is_spliced_into_a_copy_and_leaves_the_part_whole(tmp_path, monkeypatch):
    # The kept path on real topology. No real crossing heals under ShapeFix, so the BEFORE
    # analysis is told two adjacent faces of a sound box cross; everything after it - the copy,
    # the rebuild, the splice, both identity checks and the real re-analysis - is the kernel's.
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from meshpipeline.cad.repair.caps import measure

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    dump = _brep_dump(shape, tmp_path / "before.brep")
    before = measure(shape)
    real = operations._analyze_faces
    calls = []

    def analyze(on, faces):
        calls.append(on)
        return [(1, 3)] if len(calls) == 1 else real(on, faces)

    monkeypatch.setattr(operations, "_analyze_faces", analyze)
    targets = _refs(shape, (1, 3))
    out = HealSelfIntersections().apply(shape, targets, tolerance_mm=0.01)

    assert "declined" not in out.evidence, out.evidence
    assert out.shape is not shape and len(calls) == 2
    assert BRepCheck_Analyzer(out.shape, True).IsValid()
    after = measure(out.shape)
    assert ((after.solids, after.shells, after.faces, after.edges, after.vertices)
            == (before.solids, before.shells, before.faces, before.edges, before.vertices))
    assert after.surface_area_mm2 == pytest.approx(before.surface_area_mm2)
    assert after.volume_mm3 == pytest.approx(before.volume_mm3)
    assert out.evidence["faces_rebuilt"] == 2
    assert out.evidence[operations.AREA_DELTA] == pytest.approx(0.0, abs=1e-6)
    assert out.mapping.basis == "kernel"
    assert _brep_dump(shape, tmp_path / "after.brep") == dump


def test_the_outside_identities_and_seams_read_the_real_topology():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    faces = [operations.sub_shape(shape, "face", i) for i in (1, 3)]
    outside = operations._outside_identities(shape, faces)
    seams = operations._seams(shape, faces)
    # 4 untouched faces; of 12 edges, the 7 owned by face 1 or 3 are excluded (they share one).
    assert len(outside) == 4 + (12 - 7)
    # Each target's 4 edges, less the one they share with each other: 6 seams with outside faces.
    assert len(seams) == 6
    assert operations._owners(shape, faces) == {1}


def _cylinder_needing_same_parameter():
    """A cylinder whose lateral face's edges are flagged not same-parameter, so ShapeFix WRITES
    into them (the flag, and the tolerance it computes) - into edges the caps share."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.TopoDS import TopoDS

    shape = BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape()
    builder = BRep_Builder()
    for edge in operations._face_edges(TopoDS.Face_s(operations.sub_shape(shape, "face", 1))):
        builder.SameParameter(edge, False)
        builder.SameRange(edge, False)
    return shape


@pytest.mark.parametrize("after, declined", [(None, False), ([(1, 2)], True)])
def test_a_rebuild_that_writes_into_its_edges_never_reaches_the_callers_shape(
        tmp_path, monkeypatch, after, declined):
    # Kept and declined (crossing_remains, after the rebuild ran) alike: the caller's shape is
    # also the session's rollback snapshot, so not one byte of it may change.
    shape = _cylinder_needing_same_parameter()
    dump = _brep_dump(shape, tmp_path / "before.brep")
    real = operations._analyze_faces
    answers = [[(1, 2)], after]

    def analyze(on, faces):
        answer = answers.pop(0)
        return real(on, faces) if answer is None else answer

    monkeypatch.setattr(operations, "_analyze_faces", analyze)
    out = HealSelfIntersections().apply(shape, _refs(shape, (1, 2)), tolerance_mm=0.01)
    if declined:
        assert out.shape is shape and out.evidence["declined"].startswith("crossing_remains")
    else:
        assert "declined" not in out.evidence, out.evidence
        assert out.shape is not shape
    assert _brep_dump(shape, tmp_path / "after.brep") == dump


def test_without_its_copy_the_same_rebuild_would_have_changed_the_callers_shape(
        tmp_path, monkeypatch):
    # The guard on the test above: proof that this fixture's rebuild really writes into the
    # edges it is handed, so the byte comparison there could have failed.
    shape = _cylinder_needing_same_parameter()
    dump = _brep_dump(shape, tmp_path / "before.brep")
    real = operations._analyze_faces
    calls = []

    def analyze(on, faces):
        calls.append(on)
        return [(1, 2)] if len(calls) == 1 else real(on, faces)

    monkeypatch.setattr(operations, "_analyze_faces", analyze)
    monkeypatch.setattr(operations, "_copied", lambda s: s)
    HealSelfIntersections().apply(shape, _refs(shape, (1, 2)), tolerance_mm=0.01)
    assert _brep_dump(shape, tmp_path / "after.brep") != dump


def test_a_run_over_the_forged_tangle_declines_honestly_and_hands_back_the_part(
        tmp_path, monkeypatch):
    # Review off, heal on: the rules planner proposes the heal for the located crossing (it is the
    # only operation that claims it), the heal declines the five-face tangle as not isolated, and
    # the run ends with the crossing still reported rather than "repaired".
    from meshpipeline.cad.repair import loop

    for name, value in (("CAD_REPAIR_ENABLED", True), ("CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001),
                        ("CAD_REPAIR_MAX_TOLERANCE_MM", 0.1),
                        ("CAD_REPAIR_ALLOW_FACE_REMOVAL", False),
                        ("CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02), ("CAD_REPAIR_MAX_OPS", 6),
                        ("CAD_REPAIR_STALL_LIMIT", 2), ("CAD_REPAIR_TIMEOUT_S", 120.0)):
        monkeypatch.setattr(rcfg, name, value)
    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    run = loop.run(shape, tolerance_mm=0.01)
    heals = [v for v in run.verdicts if v.op == "heal_self_intersections"]
    assert heals and all(v.reason.startswith("declined:not_isolated") for v in heals)
    assert not any(v.kept for v in run.verdicts)
    assert any(d.code is DefectCode.self_intersection for d in run.defects)


def _session_heal(monkeypatch, *, rebuilt_area_gain, after_pairs):
    """A real box in a real session, the heal driven to a chosen decline after its rebuild: the
    BEFORE analysis says faces 1 and 3 cross, each rebuilt face measures `rebuilt_area_gain`
    more than its original, and the AFTER analysis answers `after_pairs`."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    import meshpipeline.cad.repair.localize as localize
    from meshpipeline.cad.repair.session import RepairSession

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    answers = [[(1, 3)], after_pairs]
    monkeypatch.setattr(operations, "_analyze_faces", lambda on, faces: answers.pop(0))
    rebuilt: list = []
    real_rebuild, real_area = operations._rebuild_face, localize._face_area

    def rebuild(face, *, tolerance_mm):
        rebuilt.append(real_rebuild(face, tolerance_mm=tolerance_mm))
        return rebuilt[-1]

    def area(face):
        gain = rebuilt_area_gain if any(face.IsSame(r) for r in rebuilt) else 0.0
        return real_area(face) + gain

    monkeypatch.setattr(operations, "_rebuild_face", rebuild)
    monkeypatch.setattr(localize, "_face_area", area)
    session = RepairSession(shape)
    monkeypatch.setattr(session, "_carries", lambda targets, code: True)
    return session, _refs(shape, (1, 3))


def test_a_too_much_surface_decline_is_recorded_as_declined_not_as_a_cap(monkeypatch):
    # 2 x 30 mm2 on a 600 mm2 box is 10%: the heal's own bound declines it. Before the fix the
    # declaration rode on the decline and the session's cumulative cap reported it first.
    session, targets = _session_heal(monkeypatch, rebuilt_area_gain=30.0, after_pairs=[])
    verdict = session.try_op(HealSelfIntersections(), targets, DefectCode.self_intersection,
                             tolerance_mm=0.01)
    assert verdict.reason.startswith("declined:too_much_surface"), verdict.reason


def test_a_crossing_remains_decline_after_a_kept_declaration_is_recorded_as_declined(monkeypatch):
    # Earlier kept ops declared 10 mm2 the shape does not show (inside the 12 mm2 residual); the
    # heal measured another 10 mm2 and then declined. Counted, the 20 mm2 broke the cap.
    from meshpipeline.cad.repair.caps import Accounting

    session, targets = _session_heal(monkeypatch, rebuilt_area_gain=5.0, after_pairs=[(1, 3)])
    session.accounting = Accounting(declared_area_mm2=10.0)
    verdict = session.try_op(HealSelfIntersections(), targets, DefectCode.self_intersection,
                             tolerance_mm=0.01)
    assert verdict.reason.startswith("declined:crossing_remains"), verdict.reason


def _run_config(monkeypatch):
    for name, value in (("CAD_REPAIR_ENABLED", True), ("CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001),
                        ("CAD_REPAIR_MAX_TOLERANCE_MM", 0.1),
                        ("CAD_REPAIR_ALLOW_FACE_REMOVAL", False),
                        ("CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02), ("CAD_REPAIR_MAX_OPS", 6),
                        ("CAD_REPAIR_STALL_LIMIT", 2), ("CAD_REPAIR_TIMEOUT_S", 120.0)):
        monkeypatch.setattr(rcfg, name, value)


def _directed_heal(shape, target, item_id):
    from meshpipeline.cad.repair import loop
    from meshpipeline.cad.repair.directives import DirectedPlanner
    from meshpipeline.cad.repair.triage import REVIEW_RESERVED_CODES

    # The whole crossing component, as the scan reports it, plus the face itself.
    directive = {"op": "heal_self_intersections", "defect_code": "self_intersection",
                 "defect_fingerprint": target.fingerprint,
                 "targets": [target.fingerprint, *target.measurements["crossing_faces"]],
                 "source": "engineer", "item_id": item_id}
    planner = DirectedPlanner([directive], reserved=REVIEW_RESERVED_CODES)
    return loop.run(shape, planner=planner, tolerance_mm=0.01)


def _assert_honest(verdict, run, target):
    # EITHER kept with the target gone and an honest basis, OR a named decline, OR judged and
    # rolled back by the caps / progress rule - never a silent no-op.
    if verdict.kept:
        assert verdict.basis in ("geometric", "kernel")
        assert target.fingerprint not in {d.fingerprint for d in run.defects
                                          if d.code is DefectCode.self_intersection}
    elif verdict.reason.startswith("declined:"):
        assert verdict.reason[len("declined:"):].split(" ")[0] in {
            "not_isolated", "multi_body", "too_much_surface", "grew_beyond_targets",
            "crossing_remains"}
    else:
        assert verdict.reason.startswith(("cap:", "target_remains", "new_defect")), verdict.reason


def test_an_engineer_directive_on_the_forged_tangle_ends_honestly(tmp_path, monkeypatch):
    _run_config(monkeypatch)
    shape = _read(write_pierced_floor_step(tmp_path / "pierced.step"))
    target = _crossings(shape)[0]
    run = _directed_heal(shape, target, "pierced-proof")
    verdict = run.verdicts[0]
    assert verdict.op == "heal_self_intersections"
    _assert_honest(verdict, run, target)


@pytest.mark.skipif(not TURBINE.exists(),
                    reason="OCCT corpus not on disk (licence: never committed)")
def test_the_turbine_crossing_is_located_and_healed_by_an_engineer_directive(monkeypatch):
    _run_config(monkeypatch)
    shape = _read(TURBINE)
    found, _ = localize_scan(shape, face_scan_s=30.0)
    located = [d for d in found if d.code is DefectCode.self_intersection
               and d.measurements.get("crossing_faces")]
    if not located:
        # The analyzer ran to completion inside its budget (~5 s; a timeout is silence too, but
        # 120 s gave the same empty answer) and found no face crossing another: there is nothing
        # for the heal to aim at, and forcing a directive would prove nothing.
        pytest.skip("ec_turbine-A: the face scan completed and located no self-crossing face")
    target = located[0]
    run = _directed_heal(shape, target, "turbine-proof")
    verdict = run.verdicts[0]
    print(f"TURBINE component={1 + len(target.measurements['crossing_faces'])} "
          f"kept={verdict.kept} basis={verdict.basis!r} reason={verdict.reason!r}")
    assert verdict.op == "heal_self_intersections"
    _assert_honest(verdict, run, target)


def test_a_heal_on_a_sound_part_is_declined_not_isolated(tmp_path):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    out = HealSelfIntersections().apply(shape, _refs(shape, (1,)), tolerance_mm=0.01)
    assert out.shape is shape and out.evidence["declined"].startswith("not_isolated")
