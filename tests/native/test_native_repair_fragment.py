# Responsibility: Verify on the REAL kernel that fragment_bodies makes two touching bodies share
#                 their contact (both bodies kept, interface faces added, declared counts equal to
#                 measured ones), declines two interpenetrating bodies, and report what the region
#                 loop does with it on a real touch-only corpus file.
# Boundaries: the native tier; the claim, declines, flag gating and whole-shape judgement are
#             unit-tested hermetically (tests/unit/cad/test_repair_fragment.py).
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("OCP.BOPAlgo", reason="the CAD kernel is a native-tier dependency")

import meshpipeline.cad.repair.rules_author as rules_author_module  # noqa: E402
import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair.caps import measure  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for  # noqa: E402
from meshpipeline.cad.repair.operations import (  # noqa: E402
    AREA_DELTA,
    VOLUME_DELTA,
    FragmentBodies,
    box_of,
    catalogue,
    sub_shape,
)
from meshpipeline.cad.repair.rules_author import mesh_ops  # noqa: E402


@pytest.fixture
def fragment_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)


# SYNTHETIC IS DELIBERATE HERE: what is under test is the operation's INPUT CONDITION - two bodies
# that touch, or that interpenetrate - not a gmsh failure. Two boxes state that condition exactly.

def _box(x, y, z, dx, dy, dz):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _assembly(*bodies):
    """A compound of the bodies, through a BREP round trip as an upload would arrive."""
    import tempfile

    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape

    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    for body in bodies:
        builder.Add(compound, body)
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "assembly.brep")
        assert BRepTools.Write_s(compound, path)
        shape = TopoDS_Shape()
        assert BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _ref(shape, index: int) -> EntityRef:
    diagonal = measure(shape).diagonal_mm
    return EntityRef(entity_type="face", entity_index=index,
                     fingerprint=fingerprint_of("face", box_of(sub_shape(shape, "face", index)),
                                                quantum=quantum_for(diagonal)))


def _contact_face(shape, x: float) -> int:
    """The index of the big box's face lying in the plane `x` (the side the small box touches)."""
    count = measure(shape).faces
    for index in range(1, count + 1):
        box = box_of(sub_shape(shape, "face", index))
        low, high = box["bbox_min"], box["bbox_max"]
        if abs(low[0] - x) < 1e-6 and abs(high[0] - x) < 1e-6 and high[1] - low[1] > 9.0:
            return index
    raise AssertionError(f"no face in the plane x={x}")


def _shared_faces(shape) -> int:
    """How many faces are owned by two solids - the conformal interface."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, ancestors)
    shared = 0
    for i in range(1, ancestors.Extent() + 1):
        owners: list = []
        for owner in ancestors.FindFromIndex(i):
            if not any(owner.IsSame(o) for o in owners):
                owners.append(owner)
        shared += len(owners) > 1
    return shared


def test_two_touching_boxes_come_back_sharing_their_contact(fragment_on):
    # A 5 mm box pressed against the middle of a 10 mm box's x=10 side: they touch, share no volume.
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), _box(10, 2, 2, 5, 5, 5))
    before = measure(shape)
    assert _shared_faces(shape) == 0

    outcome = FragmentBodies().apply(shape, (_ref(shape, _contact_face(shape, 10.0)),),
                                     tolerance_mm=0.01)

    assert "declined" not in outcome.evidence, outcome.evidence
    after = measure(outcome.shape)
    assert after.solids == before.solids == 2
    assert outcome.evidence["solids_delta"] == 0
    # THE CONTACT IS NOW ONE FACE BOTH BODIES OWN, cut out of the big box's side.
    assert _shared_faces(outcome.shape) == 1
    # Declared counts equal measured ones, with the counters the caps use.
    assert outcome.evidence["faces_added"] == after.faces - before.faces == 1
    assert outcome.evidence[AREA_DELTA] == pytest.approx(
        after.surface_area_mm2 - before.surface_area_mm2, abs=1e-6)
    assert outcome.evidence[VOLUME_DELTA] == 0.0
    assert outcome.evidence["common_volume_mm3"] <= 1e-6
    assert abs(after.volume_mm3 - before.volume_mm3) <= 1e-6 * before.volume_mm3
    assert after.valid
    # NO NEW NAME, measured: the shared patch has the small box's contact face's box, and the
    # holed remainder keeps the big side's box, so every face of the result is named as before.
    assert outcome.added == ()
    assert outcome.mapping.basis == "kernel" and outcome.mapping.moved == {}


def test_two_interpenetrating_boxes_are_declined(fragment_on):
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), _box(8, 2, 2, 5, 5, 5))
    # The x=10 side of the big box now cuts through the small one.
    outcome = FragmentBodies().apply(shape, (_ref(shape, _contact_face(shape, 10.0)),),
                                     tolerance_mm=0.01)
    assert outcome.shape is shape
    reason = outcome.evidence["declined"]
    assert reason.startswith("interpenetrating"), reason
    assert "which body owns the shared volume is a judgement" in reason
    assert outcome.evidence["common_volume_mm3"] == pytest.approx(2 * 5 * 5, rel=1e-6)


def test_a_single_body_is_declined(fragment_on):
    shape = _assembly(_box(0, 0, 0, 10, 10, 10))
    outcome = FragmentBodies().apply(shape, (_ref(shape, 1),), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("single_body")


def test_two_boxes_apart_have_no_partner(fragment_on):
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), _box(20, 0, 0, 5, 5, 5))
    outcome = FragmentBodies().apply(shape, (_ref(shape, _contact_face(shape, 10.0)),),
                                     tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("no_partner")


def test_of_two_bodies_touching_the_face_the_one_nearest_its_centre_is_fused(fragment_on):
    # Both small boxes touch the big box's x=10 side (distance 0): the tie goes to the one whose
    # box centre is nearest the face's - the one at its middle, not the one in its corner.
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), _box(10, 0, 0, 2, 2, 2),
                      _box(10, 4, 4, 2, 2, 2))
    outcome = FragmentBodies().apply(shape, (_ref(shape, _contact_face(shape, 10.0)),),
                                     tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert _shared_faces(outcome.shape) == 1
    shared = _shared_face_boxes(outcome.shape)
    assert len(shared) == 1
    low, high = shared[0]["bbox_min"], shared[0]["bbox_max"]
    assert low[1] == pytest.approx(4.0) and high[1] == pytest.approx(6.0), shared


def test_a_partner_already_conformal_with_a_third_body_is_declined(fragment_on):
    from OCP.BOPAlgo import BOPAlgo_Builder

    # B and C already share a face (fused beforehand); A touches B.
    middle, far = _box(10, 2, 2, 5, 5, 5), _box(15, 2, 2, 5, 5, 5)
    builder = BOPAlgo_Builder()
    builder.AddArgument(middle)
    builder.AddArgument(far)
    builder.SetRunParallel(False)
    builder.Perform()
    assert not builder.HasErrors()
    middle, far = builder.Modified(middle).First(), builder.Modified(far).First()
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), middle, far)
    assert _shared_faces(shape) == 1
    outcome = FragmentBodies().apply(shape, (_ref(shape, _contact_face(shape, 10.0)),),
                                     tolerance_mm=0.01)
    assert outcome.shape is shape
    assert outcome.evidence["declined"].startswith("shared_with_third_body"), outcome.evidence


def _shared_face_boxes(shape) -> list[dict]:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, ancestors)
    boxes = []
    for i in range(1, ancestors.Extent() + 1):
        owners: list = []
        for owner in ancestors.FindFromIndex(i):
            if not any(owner.IsSame(o) for o in owners):
                owners.append(owner)
        if len(owners) > 1:
            boxes.append(box_of(ancestors.FindKey(i)))
    return boxes


# THE REAL MESH FAILURE

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


#: A 2-solid, touch-only file from the body-pair census
#: (devtools/validation/evidence/2026-11-15-body-pairs/run.json: overlapping 0, touching 1).
_TOUCH_ONLY = "abc/00506275_541a2ce632275fed8964367a_step_000.step"
#: The single-solid edge-through-face file: there is no pair, so the op must decline.
_SINGLE_SOLID = "abc/00506550_b5806bb84542de8e2656bc88_step_114.step"


def _contact_ref(shape) -> EntityRef:
    """A face of solid 1 touching solid 2 (distance 0) - where a located failure would point."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID

    from meshpipeline.cad.repair.localize import _indexed

    solids = _indexed(shape, TopAbs_SOLID)
    first, second = solids.FindKey(1), solids.FindKey(2)
    own = _indexed(first, TopAbs_FACE)
    faces = _indexed(shape, TopAbs_FACE)
    for index in range(1, faces.Extent() + 1):
        face = faces.FindKey(index)
        if own.Contains(face) and BRepExtrema_DistShapeShape(face, second).Value() <= 1e-9:
            return _ref(shape, index)
    raise AssertionError("the census says these bodies touch; no touching face was found")


def _flag_on_baseline(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 300.0)
    # THE IMPORT-TIME CATALOGUE WAS BUILT WITH THE FLAG OFF; the run gets the flag-on mapping.
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(fragment_bodies=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(fragment_bodies=True)})


def test_corpus_touch_only_file_under_the_region_loop(monkeypatch, fragment_on, capsys):
    """THE END-TO-END OUTCOME (Task 3b + P2-R8): the failure is LOCATED by contact measurement
    (`located_by: contact-pair`) and, because the author aims at error-severity mesh failures
    before kernel warnings, `fragment_bodies` is PROPOSED. Outcome numbers are in the printed
    line and the task report.
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.loop import LoopBudget
    from meshpipeline.cad.repair.region_loop import run_regions
    from meshpipeline.cad.repair.rules_author import rules_author

    path = _corpus(_TOUCH_ONLY)
    _flag_on_baseline(monkeypatch)
    shape = _read_shape(path)
    started = time.monotonic()
    run = run_regions(shape, author=rules_author(shape, original=measure(shape)),
                      tolerance_mm=0.1, budget=LoopBudget(max_ops=6, stall_limit=2,
                                                          timeout_s=1800.0))
    elapsed = time.monotonic() - started
    verdicts = [(v.op, v.status, v.reason) for p in run.packages for v in p.verdicts]
    through = [d for d in run.defects_after if d.code is DefectCode.mesh_edge_through_face]
    with capsys.disabled():
        print(f"\n[{_TOUCH_ONLY}] run {elapsed:.1f}s stopped={run.stopped_because} "
              f"verdicts={[(op, status) for op, status, _ in verdicts]} "
              f"edge_through_face_after={[(d.entity_index, d.fingerprint) for d in through]}")

    assert any(op == "fragment_bodies" for op, _, _ in verdicts), verdicts
    assert any(op == "fragment_bodies" and status == "kept" for op, status, _ in verdicts), verdicts
    revealed = [r for p in run.packages for v in p.verdicts if v.op == "fragment_bodies"
                for r in (v.delta.get("revealed") or [])]
    with capsys.disabled():
        print(f"revealed={[(r['code'], r['fingerprint']) for r in revealed]}")
    assert {r["code"] for r in revealed} == {"mesh_overlapping_facets"}, revealed
    assert DefectCode.mesh_edge_through_face not in {d.code for d in run.defects_after}


def test_fragment_aimed_at_the_contact_clears_the_corpus_edge_through_face(fragment_on):
    """WHAT THE OPERATION WOULD DO IF IT COULD BE AIMED, measured on the same file: given the
    contact face by hand, it is applied (both solids kept, 8 faces added, no area or volume
    moved), and the whole-shape probe afterwards no longer reports an edge through a face - it
    fails instead on overlapping facets in the 3D stage, located on a face. Progress, not a mesh.
    """
    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode

    shape = _read_shape(_corpus(_TOUCH_ONLY))
    before = measure(shape)
    found = mesh_probe.mesh_defects(shape, diagonal_mm=before.diagonal_mm, budget_s=300.0)
    assert found and {d.code for d in found} == {DefectCode.mesh_edge_through_face}, found

    outcome = FragmentBodies().apply(shape, (_contact_ref(shape),), tolerance_mm=0.1)

    assert "declined" not in outcome.evidence, outcome.evidence
    after = measure(outcome.shape)
    assert after.solids == before.solids == 2 and outcome.evidence["solids_delta"] == 0
    assert outcome.evidence["faces_added"] == after.faces - before.faces > 0
    assert outcome.evidence["common_volume_mm3"] <= 1e-6
    remaining = mesh_probe.mesh_defects(outcome.shape, diagonal_mm=before.diagonal_mm,
                                        budget_s=300.0)
    assert DefectCode.mesh_edge_through_face not in {d.code for d in remaining}, remaining


def test_the_single_solid_corpus_file_is_declined(fragment_on):
    from meshpipeline.cad.repair.brep import _read_shape

    shape = _read_shape(_corpus(_SINGLE_SOLID))
    outcome = FragmentBodies().apply(shape, (_ref(shape, 1),), tolerance_mm=0.1)
    assert outcome.shape is shape
    assert outcome.evidence["declined"].startswith("single_body (the part has 1 solid(s)")


def test_the_session_keeps_fragment_and_lists_the_revealed_overlap(monkeypatch, fragment_on):
    """THE SESSION-LEVEL PROOF of the revealed rule, on the real file and the real probe.

    Everything is real: gmsh's PLC error carries no coordinates, so the probe locates the
    edge-through-face by contact measurement (Task 3b) and the fragment is aimed at the first
    face it names.

    Measured 2026-11-14: after the fragment the edge through a face is gone and the real probe
    reports overlapping facets in the 3D stage on face 943. The file has two bodies, so `rebuilt`
    names every face of the part - but face 943's fingerprint PREDATES the op (it crossed the
    rebuild unchanged), so by ruling P2-R7 it is REVEALED: the op is KEPT and the overlap stays in
    the session's defects. (Round 2's blunter rule rolled it back here.)
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.session import KEPT, RepairSession

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 300.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    shape = _read_shape(_corpus(_TOUCH_ONLY))
    session = RepairSession(shape)
    through = [d for d in session.defects() if d.code is DefectCode.mesh_edge_through_face]
    # Task 3b: the REAL probe now locates the failure itself, by contact measurement.
    assert through and all(d.fingerprint for d in through), through
    assert all(d.measurements["located_by"] == "contact-pair" for d in through), through
    contact = _ref(shape, through[0].entity_index)

    verdict = session.try_op(FragmentBodies(), (contact,), DefectCode.mesh_edge_through_face,
                             tolerance_mm=0.1)

    assert verdict.status == KEPT, (verdict.reason, verdict.delta.get("revealed"))
    revealed = verdict.delta.get("revealed") or []
    assert {r["code"] for r in revealed} == {"mesh_overlapping_facets"}, revealed
    remaining = {(d.fingerprint, d.code.value) for d in session.defects()}
    assert all((r["fingerprint"], r["code"]) in remaining for r in revealed)
    assert DefectCode.mesh_edge_through_face not in {d.code for d in session.defects()}


def test_a_mesh_failure_on_the_new_interface_face_rolls_the_fragment_back(
        monkeypatch, fragment_on):
    """THE OTHER DIRECTION, on the real kernel: a mesh failure on a face whose fingerprint the
    rebuild CHANGED is the operation's own. A box overhanging the big box's side touches it on a
    patch with a NEW box - the shared interface face, a name that did not exist before. The probe
    is a double (synthetic is the point: the input condition is under test), reporting the edge
    through the contact face first and then overlapping facets on that interface face."""
    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect
    from meshpipeline.cad.repair.session import ROLLED_BACK, RepairSession

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    shape = _assembly(_box(0, 0, 0, 10, 10, 10), _box(10, 5, 5, 10, 10, 10))
    contact = _ref(shape, _contact_face(shape, 10.0))
    rehearsal = FragmentBodies().apply(shape, (contact,), tolerance_mm=0.01)
    interface = _shared_face_boxes(rehearsal.shape)
    assert len(interface) == 1
    diagonal = measure(shape).diagonal_mm
    interface_name = fingerprint_of("face", interface[0], quantum=quantum_for(diagonal))
    assert interface_name in rehearsal.rebuilt

    def defect(code, fingerprint, index, location, stage):
        return EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                            entity_index=index, message=code.value, fingerprint=fingerprint,
                            location=dict(location),
                            measurements={"mesh_kind": code.value, "stage": stage})

    first = defect(DefectCode.mesh_edge_through_face, contact.fingerprint, contact.entity_index,
                   box_of(sub_shape(shape, "face", contact.entity_index)), "3d")
    after = defect(DefectCode.mesh_overlapping_facets, interface_name, 1, interface[0], "3d")
    calls: list = []

    def probe(target, **kw):
        calls.append(target)
        return (first,) if len(calls) == 1 else (after,)

    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)
    session = RepairSession(shape)
    session.defects()
    verdict = session.try_op(FragmentBodies(), (contact,), DefectCode.mesh_edge_through_face,
                             tolerance_mm=0.01)
    assert verdict.status == ROLLED_BACK and verdict.reason == "new_defect", verdict.reason
    assert "revealed" not in verdict.delta
    assert session.shape is shape
