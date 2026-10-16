# Responsibility: Verify each repair operation does what it claims against the real kernel, and reports what became of the entities it was aimed at.
# Boundaries: the native tier - the only place OpenCASCADE exists. The catalogue's declarations
#             and the caps' arithmetic are unit-tested; what is proved here is that the kernel's
#             real output matches the mapping each operation reports about it.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_open_shell_step, write_step

from meshpipeline.cad.repair.identity import quantum_for
from meshpipeline.cad.repair.operations import FixShapeGlobal, box_of, mapping_from_context, splice

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def test_the_splice_rebuilds_only_the_ancestors_of_what_was_replaced(tmp_path):
    # WHAT MAKES A TARGETED FIX POSSIBLE. Replacing one face must leave every other face's
    # identity intact - otherwise a "targeted" operation has rewritten the whole part and the
    # mapping for everything else is a fiction.
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _indexed

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    faces = _indexed(shape, TopAbs_FACE)
    assert faces.Extent() == 6

    target = TopoDS.Face_s(faces.FindKey(1))
    # A replacement that is geometrically the same face, rebuilt: the substitution is what is
    # under test, not the geometry.
    # ONE ARGUMENT. `MakeFace(face, True)` has no overload in this OCP build - the two-argument
    # form takes a wire plus OnlyPlane, not a face.
    rebuilt = BRepBuilderAPI_MakeFace(target).Face()
    spliced, context = splice(shape, [(target, rebuilt)])

    assert context.IsRecorded(target)
    after = _indexed(spliced, TopAbs_FACE)
    assert after.Extent() == 6, "the splice changed the face count"
    # Every face except the replaced one is still the SAME sub-shape by identity.
    survivors = 0
    explorer = TopExp_Explorer(spliced, TopAbs_FACE)
    while explorer.More():
        if faces.Contains(explorer.Current()):
            survivors += 1
        explorer.Next()
    assert survivors == 5, f"expected 5 untouched faces, found {survivors}"


def test_the_whole_shape_fallback_still_repairs_what_it_always_did(tmp_path):
    # THE FLOOR UNDER THIS WHOLE SLICE. If the fallback stopped working, the loop could repair
    # less than the single pass it replaces, and no amount of targeting would make up for it.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    before = measure(shape)
    # NOT `assert not before.valid`. `BRepCheck_Analyzer.IsValid()` is TRUE for an open shell -
    # after the STEP round trip this fixture is a single 5-face shell with no solid, and the
    # kernel calls that valid B-rep. Validity is not a proxy for "broken", which is the whole
    # reason localize.py runs free-bound analysis alongside the status walk. The precondition
    # that matters is that the LOCALISER can point at the defect, because that is what the loop
    # acts on.
    found, _ = localize_scan(shape)
    assert [d for d in found if d.code is DefectCode.open_shell], (
        f"the fixture carries no located opening; scan reported {[d.code.value for d in found]}")

    outcome = FixShapeGlobal().apply(shape, (), tolerance_mm=0.01)

    after = measure(outcome.shape)
    assert after.faces >= before.faces, "the fallback lost a face"
    # WHAT THE FLOOR ACTUALLY DOES, observed: on this fixture ShapeFix_Shape does NOT close the
    # opening. The scan finds one open_shell at edge 10 before and the same one after. So the
    # fallback is a floor in the sense of "never makes things worse", not "repairs a hole" - patching
    # an opening is a scoped operation's job. The assertion pins that it did not get worse.
    after_found, _ = localize_scan(outcome.shape)
    opens_before = len([d for d in found if d.code is DefectCode.open_shell])
    opens_after = len([d for d in after_found if d.code is DefectCode.open_shell])
    assert opens_after <= opens_before, f"open_shell count rose from {opens_before} to {opens_after}"
    assert outcome.evidence["precision_mm"] == pytest.approx(0.01)


def test_a_sound_part_survives_the_fallback_with_its_geometry_intact(tmp_path):
    from meshpipeline.cad.repair.caps import measure

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    before = measure(shape)

    after = measure(FixShapeGlobal().apply(shape, (), tolerance_mm=0.01).shape)

    assert after.faces == before.faces and after.solids == before.solids
    assert after.diagonal_mm == pytest.approx(before.diagonal_mm, rel=1e-9)


def test_a_boxs_face_fingerprints_the_same_before_and_after_a_no_op_splice(tmp_path):
    # The fingerprint has to be computed from the same numbers on both sides of an operation, or
    # every mapping is noise. This pins that `box_of` and the localiser agree.
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.identity import fingerprint_of
    from meshpipeline.cad.repair.localize import _diagonal, _indexed

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    quantum = quantum_for(_diagonal(shape))
    face = _indexed(shape, TopAbs_FACE).FindKey(1)
    first = fingerprint_of("face", box_of(face), quantum=quantum)

    spliced, _ = splice(shape, [])
    again = fingerprint_of("face", box_of(_indexed(spliced, TopAbs_FACE).FindKey(1)),
                           quantum=quantum)

    assert first and first == again


# mapping_from_context, DIRECTLY - FixShapeGlobal takes no targets so it can never reach the loop body.


def _face_refs(shape, boxes=None):
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.identity import refs_for
    from meshpipeline.cad.repair.localize import _diagonal, _indexed

    faces = _indexed(shape, TopAbs_FACE)
    entries = boxes or [("face", i, box_of(faces.FindKey(i))) for i in range(1, faces.Extent() + 1)]
    return refs_for(entries, diagonal_mm=_diagonal(shape))


def _box_shape(tmp_path):
    return _read(write_step(tmp_path / "box.step", "MM"))


def test_a_target_replaced_by_different_geometry_is_reported_moved_on_a_kernel_basis(tmp_path):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _diagonal, _indexed

    shape = _box_shape(tmp_path)
    refs = _face_refs(shape)
    old = TopoDS.Face_s(_indexed(shape, TopAbs_FACE).FindKey(1))
    elsewhere = BRepBuilderAPI_MakeFace(gp_Pln(), 100.0, 101.0, 100.0, 101.0).Face()
    context = ShapeBuild_ReShape()
    context.Replace(old, elsewhere)

    mapping = mapping_from_context(context, refs[:1], shape_before=shape, diagonal_mm=_diagonal(shape))

    assert mapping.basis == "kernel"
    kind, new = mapping.fate_of(refs[0].fingerprint)
    assert kind == "moved" and new and new != refs[0].fingerprint


def test_a_target_replaced_by_a_null_shape_is_reported_gone(tmp_path):
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS, TopoDS_Shape

    from meshpipeline.cad.repair.identity import GONE
    from meshpipeline.cad.repair.localize import _diagonal, _indexed

    shape = _box_shape(tmp_path)
    refs = _face_refs(shape)
    context = ShapeBuild_ReShape()
    context.Replace(TopoDS.Face_s(_indexed(shape, TopAbs_FACE).FindKey(1)), TopoDS_Shape())

    mapping = mapping_from_context(context, refs[:1], shape_before=shape, diagonal_mm=_diagonal(shape))

    assert mapping.basis == "kernel"
    assert mapping.fate_of(refs[0].fingerprint) == (GONE, "")


def test_a_target_the_context_never_recorded_is_unknown_not_moved_or_gone(tmp_path):
    from OCP.ShapeBuild import ShapeBuild_ReShape

    from meshpipeline.cad.repair.localize import _diagonal

    shape = _box_shape(tmp_path)
    refs = _face_refs(shape)

    mapping = mapping_from_context(ShapeBuild_ReShape(), refs[:1], shape_before=shape,
                                   diagonal_mm=_diagonal(shape))

    assert mapping.fate_of(refs[0].fingerprint) == ("unknown", refs[0].fingerprint)
    assert not mapping.moved and not mapping.gone


def test_an_untargetable_ref_is_skipped_and_alone_does_not_earn_a_kernel_basis(tmp_path):
    from OCP.ShapeBuild import ShapeBuild_ReShape

    from meshpipeline.cad.repair.identity import EntityMapping, EntityRef
    from meshpipeline.cad.repair.localize import _diagonal

    shape = _box_shape(tmp_path)
    nameless = EntityRef(entity_type="face", fingerprint="", entity_index=1)

    mapping = mapping_from_context(ShapeBuild_ReShape(), (nameless,), shape_before=shape,
                                   diagonal_mm=_diagonal(shape))
    none_at_all = mapping_from_context(ShapeBuild_ReShape(), (), shape_before=shape,
                                       diagonal_mm=_diagonal(shape))

    assert mapping == EntityMapping.empty() and mapping.basis == "geometric"
    assert none_at_all.basis == "geometric"


def test_an_ambiguous_ref_that_did_not_move_is_not_reported_moved(tmp_path):
    # `refs_for` suffixes a collided key ("face:abc#0"); a box recomputed from geometry never
    # carries the suffix. Comparing the two directly reported every unmoved duplicate as moved.
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _diagonal, _indexed

    shape = _box_shape(tmp_path)
    face = TopoDS.Face_s(_indexed(shape, TopAbs_FACE).FindKey(1))
    box = box_of(face)
    refs = _face_refs(shape, boxes=[("face", 1, box), ("face", 1, box)])
    assert all(r.ambiguous and "#" in r.fingerprint for r in refs)
    context = ShapeBuild_ReShape()
    context.Replace(face, BRepBuilderAPI_MakeFace(face).Face())     # recorded, same place

    mapping = mapping_from_context(context, refs, shape_before=shape, diagonal_mm=_diagonal(shape))

    assert mapping.basis == "kernel"
    assert not mapping.moved, f"an unmoved duplicate was reported moved: {mapping.moved}"


# THE SCOPED SHAPEFIX OPERATIONS


def _defect_of(path, code):
    from meshpipeline.cad.repair.localize import localize_scan

    shape = _read(path)
    found, _ = localize_scan(shape)
    matching = [d for d in found if d.code is code]
    assert matching, f"the fixture carries no {code.value}; found {[d.code.value for d in found]}"
    return shape, matching[0]


def test_the_small_edge_operation_removes_the_edge_it_was_aimed_at(tmp_path):
    from tests.cad_fixtures import write_small_edge_step

    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.identity import GONE
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.operations import FixSmallEdges

    shape, defect = _defect_of(write_small_edge_step(tmp_path / "sliver.step"),
                               DefectCode.small_edge)
    op = FixSmallEdges()
    targets = op.targets_for(defect)
    assert targets, "the operation could not aim at a located small edge"

    outcome = op.apply(shape, targets, tolerance_mm=0.05)

    # THE DEFECT, NOT THE COUNT. What is asserted is that the specific sliver's defect is gone
    # from the new scan - which is the whole question the old code could not answer.
    after, _ = localize_scan(outcome.shape)
    fate, successor = outcome.mapping.fate_of(targets[0].fingerprint)
    still_there = [d for d in after
                   if d.code is DefectCode.small_edge
                   and d.fingerprint in {targets[0].fingerprint, successor}]
    assert not still_there, f"the targeted sliver survived (fate was {fate})"
    # AND THE KERNEL SAID SO, rather than us inferring it from an absence.
    assert outcome.mapping.basis == "kernel"
    assert fate in (GONE, "moved", "unknown")


def test_the_face_boundary_operation_reports_what_it_did_to_its_face(tmp_path):
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for
    from meshpipeline.cad.repair.localize import _diagonal, _indexed
    from meshpipeline.cad.repair.operations import FixFaceBoundary

    # A sound face: ShapeFix_Face has nothing to fix, and the operation must still come back with
    # a shape and a mapping rather than raising. An operation that only works on broken input
    # cannot be proposed speculatively, and the planner does exactly that.
    shape = _read(write_step(tmp_path / "box.step", "MM"))
    face = _indexed(shape, TopAbs_FACE).FindKey(1)
    ref = EntityRef(entity_type="face",
                    fingerprint=fingerprint_of("face", box_of(face),
                                               quantum=quantum_for(_diagonal(shape))),
                    entity_index=1)

    outcome = FixFaceBoundary().apply(shape, (ref,), tolerance_mm=0.01)

    assert outcome.shape is not None
    assert outcome.mapping.basis == "kernel"
    assert outcome.evidence["faces_fixed"] == 1


def test_an_operation_handed_a_ref_whose_index_does_not_resolve_does_not_crash(tmp_path):
    # A ref carries an index into the shape it was built from. The session rebuilds refs after
    # every kept operation, but a planner bug - or a resumed run - can still hand over a stale
    # one. The operation reports fixing nothing; it does not raise and does not fix an arbitrary
    # entity that happens to sit at that index.
    from meshpipeline.cad.repair.identity import EntityRef
    from meshpipeline.cad.repair.operations import FixFaceBoundary

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    stale = EntityRef(entity_type="face", fingerprint="face:deadbeef", entity_index=9999)

    outcome = FixFaceBoundary().apply(shape, (stale,), tolerance_mm=0.01)

    assert outcome.evidence["faces_fixed"] == 0
    assert outcome.shape is shape, "nothing was fixed, so nothing should have been rebuilt"
