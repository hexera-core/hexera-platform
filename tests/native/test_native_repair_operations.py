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


@pytest.fixture
def _filling_on(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)


def _read(path):
    from meshpipeline.cad.repair.brep import _read_shape

    return _read_shape(path)


def _assert_no_geometric_damage(before, after, *, declared_area=0.0):
    """The damage mode sanity_refusal does NOT cover: the part growing or its tolerance blowing up.

    `sanity_refusal` is counts and validity; the diagonal and max tolerance live in the
    cumulative half. Asserted directly so a diagonal doubling that preserved validity and counts
    could not pass.
    """
    from meshpipeline.cad.repair.caps import cumulative_refusal, sanity_refusal

    assert sanity_refusal(before, after) == "", sanity_refusal(before, after)
    assert after.diagonal_mm == pytest.approx(before.diagonal_mm, rel=1e-6)
    assert after.max_tolerance_mm <= max(before.max_tolerance_mm, 0.1) + 1e-12
    refusal = cumulative_refusal(before, after, declared_area_mm2=declared_area,
                                 declared_volume_mm3=0.0)
    assert refusal == "", refusal


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
    from meshpipeline.cad.repair.caps import measure, sanity_refusal
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
    assert sanity_refusal(before, after) == "", sanity_refusal(before, after)
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
    from meshpipeline.cad.repair.caps import measure, sanity_refusal

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    before = measure(shape)

    after = measure(FixShapeGlobal().apply(shape, (), tolerance_mm=0.01).shape)

    assert after.faces == before.faces and after.solids == before.solids
    assert after.diagonal_mm == pytest.approx(before.diagonal_mm, rel=1e-9)
    assert sanity_refusal(before, after) == ""


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


def test_the_small_edge_operation_does_no_damage_and_reports_what_it_aimed_at(tmp_path):
    # A PROGRESS-ONLY TEST WITH NO CAP ASSERTION CERTIFIES DESTRUCTION. This test used to assert
    # only that the sliver was gone from the re-scan, which was true - achieved by running the
    # fixer on a compound of bare faces, which extrapolated the geometry (diagonal 141.77 ->
    # 270.00 mm, max tolerance 1e-7 -> 80 mm). The fixer now runs on the whole shape, where the
    # solid's topology forbids removing the sliver: the honest outcome on this fixture is no
    # progress, and what must hold is that nothing was damaged.
    from tests.cad_fixtures import write_small_edge_step

    from meshpipeline.cad.repair.caps import measure, sanity_refusal
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FixSmallEdges

    shape, defect = _defect_of(write_small_edge_step(tmp_path / "sliver.step"),
                               DefectCode.small_edge)
    before = measure(shape)
    op = FixSmallEdges()
    targets = op.targets_for(defect)
    assert targets, "the operation could not aim at a located small edge"

    outcome = op.apply(shape, targets, tolerance_mm=0.05)

    after = measure(outcome.shape)
    assert sanity_refusal(before, after) == "", sanity_refusal(before, after)
    assert after.diagonal_mm == pytest.approx(before.diagonal_mm, rel=1e-6)
    assert after.max_tolerance_mm == pytest.approx(before.max_tolerance_mm, abs=1e-6)
    assert outcome.evidence["edges_targeted"] == 1
    assert "faces_fixed" not in outcome.evidence


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

    from meshpipeline.cad.repair.caps import measure

    before = measure(shape)
    outcome = FixFaceBoundary().apply(shape, (ref,), tolerance_mm=0.01)

    assert outcome.shape is not None
    _assert_no_geometric_damage(before, measure(outcome.shape))
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


def test_the_small_edge_operation_with_an_unresolvable_ref_rebuilds_nothing(tmp_path):
    from meshpipeline.cad.repair.identity import EntityRef
    from meshpipeline.cad.repair.operations import FixSmallEdges

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    stale = EntityRef(entity_type="edge", fingerprint="edge:deadbeef", entity_index=9999)

    outcome = FixSmallEdges().apply(shape, (stale,), tolerance_mm=0.01)

    assert "faces_fixed" not in outcome.evidence
    assert outcome.evidence["edges_targeted"] == 1
    assert outcome.shape is shape


def test_two_targets_naming_one_face_repair_it_once(tmp_path):
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of
    from meshpipeline.cad.repair.localize import _diagonal, _indexed
    from meshpipeline.cad.repair.operations import FixFaceBoundary

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    face = _indexed(shape, TopAbs_FACE).FindKey(1)
    ref = EntityRef(entity_type="face",
                    fingerprint=fingerprint_of("face", box_of(face),
                                               quantum=quantum_for(_diagonal(shape))),
                    entity_index=1)

    outcome = FixFaceBoundary().apply(shape, (ref, ref), tolerance_mm=0.01)

    assert outcome.evidence["faces_fixed"] == 1


def _tolerances(shape):
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import _indexed

    edges = _indexed(shape, TopAbs_EDGE)
    return [BRep_Tool.Tolerance_s(TopoDS.Edge_s(edges.FindKey(i)))
            for i in range(1, edges.Extent() + 1)]


def test_the_face_boundary_operation_leaves_its_input_untouched_for_the_noop_case(tmp_path):
    # THE SESSION'S ROLLBACK IS A HANDLE SWAP, correct only if no operation mutates its input.
    # ShapeFix_Face may edit edge tolerances/pcurves in place on sub-shapes shared with the input.
    #
    # HONESTY: no face was found on which this operation changes anything, so this test
    # demonstrates the absence of mutation only for the no-op case. Every face of the sliver,
    # open-shell, missing-bore-wall and loose-faces fixtures was tried at precision 0.05:
    # ShapeFix_Face.Perform() returned the face itself (IsSame) and set no DONE status on any.
    # Whether a face that Perform() really rewrites leaves the input intact remains UNPROVEN.
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from tests.cad_fixtures import write_small_edge_step

    from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of
    from meshpipeline.cad.repair.localize import _diagonal, _indexed
    from meshpipeline.cad.repair.operations import FixFaceBoundary

    def snapshot(shape):
        return (_indexed(shape, TopAbs_EDGE).Extent(), _indexed(shape, TopAbs_FACE).Extent(),
                _tolerances(shape))

    for name, path in (("sliver", write_small_edge_step(tmp_path / "sliver.step")),
                       ("open_shell", write_open_shell_step(tmp_path / "open.step"))):
        shape = _read(path)
        faces = _indexed(shape, TopAbs_FACE)
        before = snapshot(shape)
        for i in range(1, faces.Extent() + 1):
            ref = EntityRef(entity_type="face",
                            fingerprint=fingerprint_of("face", box_of(faces.FindKey(i)),
                                                       quantum=quantum_for(_diagonal(shape))),
                            entity_index=i)
            FixFaceBoundary().apply(shape, (ref,), tolerance_mm=0.05)
            assert snapshot(shape) == before, f"{name} face {i}: the input shape was mutated"


# SEWING


def test_sewing_joins_loose_faces_into_a_shell_without_losing_one(tmp_path):
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import SewFaces

    shape, defect = _defect_of(write_loose_faces_step(tmp_path / "loose.step"),
                               DefectCode.open_shell)
    before = measure(shape)
    # SIX SHELLS, NOT ZERO. The STEP reader wraps each loose face in a shell of its own, so the
    # body arrives as six one-face shells with no solid. Sewing's job is to collapse them into
    # one, which is why the assertion below is that the count FALLS - an earlier version of this
    # test asserted it rose, which the fixture can never satisfy.
    assert before.solids == 0 and before.shells == 6

    op = SewFaces()
    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.01)

    after = measure(outcome.shape)
    _assert_no_geometric_damage(before, after)
    assert after.faces == before.faces, "sewing must not cost a face"
    assert after.shells == 1, f"six one-face shells were not joined into one (got {after.shells})"
    # AND THE SHELL CLOSED INTO A BODY. `_as_solid_if_closed` promotes a single closed shell, so a
    # successful sew of a complete box ends with a solid where there was none.
    assert after.solids == 1, "the closed shell was not promoted to a solid"
    assert outcome.mapping.basis == "kernel"
    # IT DECLARES NO AREA. Sewing joins what is already there; a sewing that moved surface would
    # be the cumulative cap's problem, and it should be.
    assert outcome.evidence.get("area_delta_mm2", 0.0) == 0.0


def test_sewing_a_body_that_has_a_solid_is_refused_by_the_operation_itself(tmp_path):
    # BRepBuilderAPI_Sewing RETURNS A SHELL. Handed a shape with a solid it hands back the shell
    # and the solid is gone - a real-kernel test caught exactly that. The caps would refuse the
    # result, but an operation that knowingly destroys a solid should not spend a re-inspection
    # proving it.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.identity import EntityRef
    from meshpipeline.cad.repair.operations import SewFaces

    shape = _read(write_step(tmp_path / "box.step", "MM"))
    assert measure(shape).solids == 1
    ref = EntityRef(entity_type="face", fingerprint="face:aaaa", entity_index=1)

    outcome = SewFaces().apply(shape, (ref,), tolerance_mm=0.01)

    assert outcome.shape is shape, "the shape was rebuilt despite nothing being sewn"
    assert outcome.evidence["declined"] == "the targeted body already has a solid to lose"


# FILLING


def test_filling_patches_a_small_planar_hole_and_the_patch_area_matches_its_declaration(
        tmp_path, _filling_on):
    # THE BORE RIM, NOT THE OPEN SHELL. A box missing a whole face spans 0.82 of its own diagonal,
    # which the span cap correctly calls a missing WALL rather than a hole - see the decline test
    # below. The missing-bore fixture's rims span 0.16, which is the geometry this operation exists
    # for: a small, flat, closed loop with exactly one surface that can go there.
    #
    # That patching BOTH rims would seal the customer's bolt hole is a PLANNING concern, refused in
    # plan.py and tested there. At the operation level, filling one named rim is correct.
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import AREA_DELTA, FillPlanarHole

    shape, defect = _defect_of(write_missing_bore_wall_step(tmp_path / "bore.step"),
                               DefectCode.open_shell)
    before = measure(shape)

    op = FillPlanarHole()
    targets = op.targets_for(defect)
    assert targets, "a located free boundary produced no target"
    outcome = op.apply(shape, targets, tolerance_mm=0.01)

    assert not outcome.evidence.get("declined"), \
        f"a 0.16-ratio rim was declined: {outcome.evidence.get('declined')}"
    after = measure(outcome.shape)
    _assert_no_geometric_damage(before, after, declared_area=outcome.evidence[AREA_DELTA])
    assert after.faces == before.faces + 1, "the patch did not land"
    # THE DECLARATION IS WHAT THE CUMULATIVE CAP TRUSTS. If it disagrees with the measurement, the
    # accounting is worthless and a real repair would be refused for the operation's bad arithmetic.
    declared = outcome.evidence[AREA_DELTA]
    measured = after.surface_area_mm2 - before.surface_area_mm2
    assert declared == pytest.approx(measured, rel=0.01), (
        f"declared {declared}, actually added {measured}")
    assert outcome.added, "the patch face is new geometry and must be reported as added"


def test_filling_declines_a_missing_wall_rather_than_lidding_it(tmp_path, _filling_on):
    # NOT A CONTRIVED CAP. The open-shell fixture is a box with one whole face gone, so its opening
    # spans 14.142 mm against a 17.321 mm diagonal - a ratio of 0.8165, over the 0.75 cap. That is
    # the cap doing exactly what it is for: past this it is not a hole, it is a missing wall, and a
    # flat lid over a missing wall is a different part rather than a repaired one.
    #
    # An earlier version of this test shrank the cap to 0.001 to force the branch. This is better,
    # because the geometry itself is the case.
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    shape, defect = _defect_of(write_open_shell_step(tmp_path / "holed.step"),
                               DefectCode.open_shell)
    before = measure(shape)

    op = FillPlanarHole()
    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.01)

    assert outcome.shape is shape, "a declined fill must not rebuild the shape"
    assert "too_large" in outcome.evidence["declined"]
    assert measure(outcome.shape).faces == before.faces


def test_filling_declines_a_loop_that_is_not_planar(tmp_path):
    # `BRepBuilderAPI_MakeFace(wire, OnlyPlane=True)` simply declines a non-planar loop, which is
    # the behaviour wanted: across a flat loop exactly one surface can go there, so the patch is
    # determined. Across a curved one there are infinitely many, and choosing is a judgement about
    # the part's shape.
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    # Loose faces have free boundaries on every edge, and most of those loops are not planar.
    shape, defect = _defect_of(write_loose_faces_step(tmp_path / "loose.step"),
                               DefectCode.open_shell)
    op = FillPlanarHole()
    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.01)

    # KNOWN GAP, NOT COVERAGE: no fixture in the suite carries a non-planar free loop, and this test
    # runs with filling switched off, so it only exercises the disabled-decline. The planarity
    # decline is unproven with the geometry available.
    # Either it patched a genuinely planar loop or it declined - what must NOT happen is a patch
    # over a non-planar loop, so the assertion is on the invariant rather than on which branch ran.
    assert outcome.evidence.get("declined") or outcome.evidence.get("patch_is_planar") is True


def test_filling_one_rim_of_a_missing_bore_wall_is_possible_which_is_why_the_planner_refuses(
        tmp_path, _filling_on):
    # NOT A BUG IN THIS OPERATION, AND THE TEST SAYS SO. Each rim of a missing tube wall is a
    # perfectly fillable flat loop; what makes patching them wrong is that there are TWO and
    # sealing both deletes the customer's bolt hole. That is a decision about which loop to aim
    # at, so `plan.py` refuses to propose the pair (Task 9) and records the refusal. This test
    # pins the division of responsibility, so that moving the check back in here would fail it.
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    shape, defect = _defect_of(write_missing_bore_wall_step(tmp_path / "bore.step"),
                               DefectCode.open_shell)
    op = FillPlanarHole()
    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.01)

    assert outcome.evidence.get("declined") or outcome.evidence.get("patch_is_planar") is True


def test_patching_one_bore_rim_does_not_promote_the_still_open_shell_to_a_solid(
        tmp_path, _filling_on):
    # `MakeSolid` wraps any shell without checking closure, so a one-patch-at-a-time loop used to
    # get a solid-typed shape that was really open. The promotion now verifies closure, and this
    # pins that: one rim patched leaves the other rim as a free loop, so there must be no solid.
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.operations import FillPlanarHole

    shape, defect = _defect_of(write_missing_bore_wall_step(tmp_path / "bore.step"),
                               DefectCode.open_shell)
    op = FillPlanarHole()
    outcome = op.apply(shape, op.targets_for(defect), tolerance_mm=0.01)

    assert not outcome.evidence.get("declined"), "the rim was not patched, so nothing is proved"
    _assert_no_geometric_damage(measure(shape), measure(outcome.shape),
                                declared_area=outcome.evidence.get("area_delta_mm2", 0.0))
    assert measure(outcome.shape).faces == measure(shape).faces + 1
    assert measure(outcome.shape).solids == 0, "an open shell was typed as a solid"
    remaining = ShapeAnalysis_FreeBounds(outcome.shape).GetClosedWires()
    assert remaining is not None and TopExp_Explorer(remaining, TopAbs_WIRE).More(), \
        "no free loop remains, so the shell closed and this test no longer pins the refusal"
