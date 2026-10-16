# Responsibility: Verify each repair operation does what it claims against the real kernel, and reports what became of the entities it was aimed at.
# Boundaries: the native tier - the only place OpenCASCADE exists. The catalogue's declarations
#             and the caps' arithmetic are unit-tested; what is proved here is that the kernel's
#             real output matches the mapping each operation reports about it.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_open_shell_step, write_step

from meshpipeline.cad.repair.identity import quantum_for
from meshpipeline.cad.repair.operations import FixShapeGlobal, box_of, splice

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
    rebuilt = BRepBuilderAPI_MakeFace(target, True).Face()
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

    shape = _read(write_open_shell_step(tmp_path / "holed.step"))
    before = measure(shape)
    assert not before.valid

    outcome = FixShapeGlobal().apply(shape, (), tolerance_mm=0.01)

    after = measure(outcome.shape)
    assert after.faces >= before.faces, "the fallback lost a face"
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
