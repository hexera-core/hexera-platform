# Responsibility: Verify that the three WHOLE-SHAPE operations declare what they actually did -
#                 `OpOutcome.rebuilt` (every face their own history replaced) and
#                 `evidence["removed_fingerprints"]` (every face it removed) - so the
#                 held-for-review guard in `session.try_op` and `session._revealed` stop reading
#                 silence as "this operation touched nothing".
# Boundaries: REAL OpenCASCADE. The claim under test is about what the kernel's own
#             ShapeBuild_ReShape and BRepBuilderAPI_Sewing histories record, which a double
#             cannot answer. The fixtures are built in memory; the only disk use is pytest's
#             `tmp_path`, where the byte-identity check writes BREP text to compare.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for
from meshpipeline.cad.repair.operations import (
    WHOLE_SHAPE_OPS,
    FixShapeGlobal,
    FixSmallEdges,
    SewFaces,
    _face_fates,
    _named_faces,
    box_of,
    catalogue,
)

# THE FIXTURES, in memory. `tests.cad_fixtures.write_small_edge_step` builds the same plate and
# writes it as STEP; here the shape itself is wanted, and twice, so that one part carries two
# independent instances of the same defect.


def _sliver_plate(dx: float = 0.0):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec

    plate = BRepPrimAPI_MakeBox(100.0, 100.0, 10.0).Shape()
    polygon = BRepBuilderAPI_MakePolygon()
    for point in (gp_Pnt(0.0, -1.0, -1.0), gp_Pnt(20.0, -1.0, -1.0),
                  gp_Pnt(20.0, 0.002, -1.0), gp_Pnt(0.0, 0.0, -1.0)):
        polygon.Add(point)
    polygon.Close()
    wedge = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face(), gp_Vec(0.0, 0.0, 12.0)).Shape()
    cut = BRepAlgoAPI_Cut(plate, wedge).Shape()
    if not dx:
        return cut
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(dx, 0.0, 0.0))
    return BRepBuilderAPI_Transform(cut, move, True).Shape()


def _loose_faces(shape, *extra):
    """`shape`'s faces (and any `extra` faces) as a compound with no shell and no solid, each face
    an independent COPY - what `sew_faces` is built for.

    COPIED, NOT RE-WRAPPED. Faces lifted straight out of a solid still share their edges, so
    sewing them has nothing to join: measured, every face came back `IsSame` its input and the
    18 edges stayed 18. Copied one at a time they share nothing (36 edges in) and sewing really
    rebuilds them (18 edges out, no face `IsSame` its input).
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for face in (*_faces(shape), *extra):
        builder.Add(compound, BRepBuilderAPI_Copy(face).Shape())
    return compound


def _speck(*, side_mm: float = 5e-6, z_mm: float = 20.0):
    """A square face far below sewing's `MinTolerance` (1e-4 x its tolerance) - a face sewing's
    face analysis finds degenerate and DROPS, reporting it through `DeletedFace(i)`."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in ((50.0, 50.0), (50.0 + side_mm, 50.0), (50.0 + side_mm, 50.0 + side_mm),
                 (50.0, 50.0 + side_mm)):
        polygon.Add(gp_Pnt(x, y, z_mm))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    found = _indexed(shape, TopAbs_FACE)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _edges(shape) -> list:
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad.repair.localize import _indexed

    found = _indexed(shape, TopAbs_EDGE)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _diagonal(shape) -> float:
    from meshpipeline.cad.repair.localize import _diagonal as measured

    return float(measured(shape))


def _names_of(faces, shape) -> set:
    quantum = quantum_for(_diagonal(shape))
    return {fingerprint_of("face", box_of(f), quantum=quantum) for f in faces}


def _sliver_edge(shape):
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopoDS import TopoDS

    for edge in _edges(shape):
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(TopoDS.Edge_s(edge), props)
        if 0.0 < props.Mass() < 0.01:
            return edge
    raise AssertionError("the fixture carries no sliver edge")


def _ref(shape, sub, entity_type: str) -> EntityRef:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    kind = {"edge": TopAbs_EDGE, "face": TopAbs_FACE}[entity_type]
    quantum = quantum_for(_diagonal(shape))
    return EntityRef(entity_type=entity_type,
                     fingerprint=fingerprint_of(entity_type, box_of(sub), quantum=quantum),
                     entity_index=_indexed(shape, kind).FindIndex(sub))


# THE SHARED READER: WHICH FACES WENT, AND WHICH WERE REBUILT


def _wireframe(shape, *, tolerance_mm: float, drop: bool):
    from OCP.ShapeFix import ShapeFix_Wireframe

    fixer = ShapeFix_Wireframe(shape)
    fixer.SetPrecision(tolerance_mm)
    fixer.SetMaxTolerance(tolerance_mm)
    fixer.ModeDropSmallEdges = drop
    fixer.FixSmallEdges()
    fixer.FixWireGaps()
    return fixer


def test_the_history_reader_separates_a_rebuilt_face_from_a_removed_one():
    """`_face_fates` is one walk of the replacement chain answering both questions. With the
    merge ON, this fixer removes nothing and rebuilds five faces of the plate; what it must NOT
    report is silence, which is what both declarations said before."""
    shape = _sliver_plate()
    fixer = _wireframe(shape, tolerance_mm=1.0, drop=True)
    context = fixer.Context()
    result = context.Apply(shape)

    gone, rebuilt = _face_fates(context, shape, result)
    assert gone == [], "this fixer merges edges, not faces"
    assert len(rebuilt) == 5, len(rebuilt)
    # Every rebuilt face is a face of the RESULT, never of the input: the declaration names the
    # geometry that now exists, which is what the next scan will fingerprint.
    for face in rebuilt:
        assert any(face.IsSame(f) for f in _faces(result))
    # And it is a strict subset: three of the plate's eight faces were not touched at all.
    assert len(rebuilt) < len(_faces(shape))


def test_the_history_reader_reports_a_face_the_pass_really_removed():
    """A hand-built history that deletes a face: the reader must call it gone, not rebuilt."""
    from OCP.ShapeBuild import ShapeBuild_ReShape

    shape = _sliver_plate()
    victim = _faces(shape)[0]
    context = ShapeBuild_ReShape()
    context.Remove(victim)
    result = context.Apply(shape)

    gone, rebuilt = _face_fates(context, shape, result)
    assert len(gone) == 1 and gone[0].IsSame(victim)
    assert not any(f.IsSame(victim) for f in rebuilt)


def test_named_faces_deduplicates_and_drops_the_unmeasurable():
    shape = _sliver_plate()
    faces = _faces(shape)
    named = _named_faces([*faces, *faces], diagonal_mm=_diagonal(shape))
    assert len(named) == len(set(named))
    assert set(named) == _names_of(faces, shape)
    assert _named_faces([], diagonal_mm=_diagonal(shape)) == ()


# WHAT EACH OPERATION NOW DECLARES


def test_fix_small_edges_declares_the_faces_its_own_context_rebuilt():
    """The headline for `fix_small_edges`: `rebuilt` was EMPTY for a pass that rewrites faces all
    over the part, so `session._revealed` read its damage as geometry it merely exposed."""
    shape = _sliver_plate()
    ref = _ref(shape, _sliver_edge(shape), "edge")
    outcome = FixSmallEdges().apply(shape, (ref,), tolerance_mm=0.014)

    assert outcome.rebuilt, "a pass that replaced faces and declares none declares a falsehood"
    # Exactly the faces its own history replaced - no more, and none invented.
    fixer = _wireframe(shape, tolerance_mm=0.014, drop=False)
    context = fixer.Context()
    _gone, rebuilt = _face_fates(context, shape, context.Apply(shape))
    assert set(outcome.rebuilt) == set(_named_faces(rebuilt, diagonal_mm=_diagonal(shape)))
    assert set(outcome.rebuilt) < _names_of(_faces(shape), shape), (
        "a whole-shape pass that rebuilt EVERY face would make the declaration useless")
    assert outcome.evidence["faces_rebuilt"] == len(outcome.rebuilt)
    # It removed nothing, and says so explicitly rather than by omission.
    assert outcome.evidence["removed_fingerprints"] == []


def test_fix_small_edges_declares_only_the_plate_it_actually_rewrote():
    """Two plates, one part. The pass is whole-shape and the declaration must say which faces it
    really reached - this is the measurement that a declaration is now TRUE rather than silent."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    left, right = _sliver_plate(), _sliver_plate(dx=500.0)
    builder.Add(compound, left)
    builder.Add(compound, right)

    ref = _ref(compound, _sliver_edge(left), "edge")
    outcome = FixSmallEdges().apply(compound, (ref,), tolerance_mm=0.014)

    quantum = quantum_for(_diagonal(compound))
    declared = set(outcome.rebuilt)
    assert declared, "nothing declared is the bug this test exists for"
    # THE OPERATION REACHES BOTH PLATES - it is whole-shape, and the declaration says so instead
    # of naming only the target's. That is the honest reading of what it did.
    left_names = {fingerprint_of("face", box_of(f), quantum=quantum) for f in _faces(left)}
    right_names = {fingerprint_of("face", box_of(f), quantum=quantum) for f in _faces(right)}
    assert declared & left_names, "the target's own plate must be in the declaration"
    assert declared & right_names, (
        "this pass does reach the other plate; a declaration that hid that would be the old lie "
        "in a new place")
    assert declared <= (left_names | right_names)


def test_fix_shape_global_declares_what_its_context_did_and_nothing_more():
    """Measured no-op on these parts (the diagnosis: `IsSame` TRUE, zero recorded replacements).
    The declaration must therefore be EMPTY here - and empty because it was read, not assumed."""
    shape = _sliver_plate()
    ref = _ref(shape, _faces(shape)[0], "face")
    outcome = FixShapeGlobal().apply(shape, (ref,), tolerance_mm=0.014)
    assert outcome.rebuilt == ()
    assert outcome.evidence["removed_fingerprints"] == []
    assert outcome.evidence["faces_rebuilt"] == 0


def test_fix_shape_global_declares_a_rebuild_when_its_context_records_one(monkeypatch):
    """The paired test, so the empty declaration above cannot pass by never looking. A pass whose
    history DOES replace a face must name it.

    The fixer is the only thing replaced - a real `ShapeBuild_ReShape` carrying one real face
    substitution, so the reading under test is the real one.
    """
    import OCP.ShapeFix as shapefix
    from OCP.ShapeBuild import ShapeBuild_ReShape

    shape = _sliver_plate()
    elsewhere = _sliver_plate(dx=0.5)           # the same topology, half a millimetre away
    old, new = _faces(shape)[0], _faces(elsewhere)[0]
    context = ShapeBuild_ReShape()
    context.Replace(old, new)

    class _Fixer:
        def __init__(self, _shape):
            self.FixSolidMode = 0
            self.FixFreeShellMode = 0
            self.FixFreeFaceMode = 0
            self.FixSameParameterMode = 0
            self.FixVertexPositionMode = 0

        def SetPrecision(self, _value) -> None:
            pass

        def SetMaxTolerance(self, _value) -> None:
            pass

        def Perform(self) -> None:
            pass

        def Shape(self):
            return context.Apply(shape)

        def Context(self):
            return context

    monkeypatch.setattr(shapefix, "ShapeFix_Shape", _Fixer)
    outcome = FixShapeGlobal().apply(shape, (_ref(shape, old, "face"),), tolerance_mm=0.014)

    assert outcome.evidence["faces_rebuilt"] == 1
    assert outcome.rebuilt == _named_faces([new], diagonal_mm=_diagonal(shape))
    assert outcome.evidence["removed_fingerprints"] == []


def test_sew_faces_declares_the_faces_it_rebuilt_and_the_ones_it_deleted():
    """Sewing has no `ShapeBuild_ReShape`, so its declaration is read from its OWN history -
    `DeletedFace` for what went, identity against the input for what it rebuilt."""
    loose = _loose_faces(_sliver_plate())
    ref = _ref(loose, _faces(loose)[0], "face")
    outcome = SewFaces().apply(loose, (ref,), tolerance_mm=0.1)

    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.evidence["bodies_sewn"] == 1
    # Sewing rebuilds every face whose edges it replaced, which on a closed box is all of them.
    assert outcome.rebuilt, "sewing that rebuilt no face would not have sewn anything"
    assert set(outcome.rebuilt) <= _names_of(_faces(outcome.shape), outcome.shape)
    assert outcome.evidence["faces_rebuilt"] == len(outcome.rebuilt)
    # It deleted nothing here, and the key is present so the guard reads a fact, not a default.
    assert outcome.evidence["removed_fingerprints"] == []


def test_sew_faces_that_changes_nothing_declares_nothing():
    """The decline path keeps today's behaviour: a body with a solid to lose is not sewn, and an
    operation that did nothing may not look like one that rebuilt the assembly."""
    outcome = SewFaces().apply(_sliver_plate(), (), tolerance_mm=0.1)
    assert outcome.evidence["declined"]
    assert outcome.rebuilt == ()
    assert "removed_fingerprints" not in outcome.evidence


def test_sew_faces_names_the_face_it_deleted():
    """The removal path, for real. A loose body carrying a speck of a face: sewing's face
    analysis drops it, counts it in `NbDeletedFaces()`, and hands it back through
    `DeletedFace(i)` - and that name is what `session.try_op` checks against the entities
    per-defect review is holding, the whole reason the key has to be populated.

    THIS IS ALSO THE REGRESSION for the reader it replaced, which called a zero-argument
    `DeletedFace()` the binding does not have and swallowed the TypeError: it read every sewing
    as "dropped nothing", this one included."""
    speck = _speck()
    loose = _loose_faces(_sliver_plate(), speck)
    ref = _ref(loose, _faces(loose)[0], "face")
    outcome = SewFaces().apply(loose, (ref,), tolerance_mm=0.1)

    assert "declined" not in outcome.evidence, outcome.evidence
    assert len(_faces(outcome.shape)) == len(_faces(loose)) - 1, "sewing must drop the speck"
    removed = outcome.evidence["removed_fingerprints"]
    assert removed == sorted(_named_faces([speck], diagonal_mm=_diagonal(loose))), removed
    assert removed, "the speck must be nameable at the part's quantum, or this proves nothing"


# THE DECLARATION IS WHAT THE GUARDS READ


def test_the_face_count_cap_is_net_which_is_why_the_names_are_needed():
    """Why the names and not just a count. `session.try_op` rolls an operation back when it
    removed a face an engineer is being asked about, and it reads
    `evidence["removed_fingerprints"]` to find out - a key only `fix_small_faces` set before, so
    the whole-shape passes were invisible to that guard. `sanity_refusal` does not cover the gap:
    its face check is a NET comparison, so a pass that removes one face and splits another into
    two passes it while a held face is gone."""
    from meshpipeline.cad.repair.caps import Measure, sanity_refusal

    def measured(faces: int) -> Measure:
        return Measure(valid=True, solids=1, shells=1, faces=faces, edges=18, vertices=12,
                       diagonal_mm=141.8, max_tolerance_mm=1e-7,
                       surface_area_mm2=24400.0, volume_mm3=100000.0)

    assert sanity_refusal(measured(8), measured(8)) == ""
    # One face fewer IS refused - so the hole is specifically the net-zero case above, and
    # `removed_fingerprints` is the only thing that can see into it.
    assert sanity_refusal(measured(8), measured(7)) != ""


def test_only_sew_faces_is_declared_whole_shape_without_a_kernel_history():
    """`sew_faces` stays whole-shape and SAYS so. `BRepBuilderAPI_Sewing` has no
    `ShapeBuild_ReShape` and no applicable record set: "sew this rim and not that one" is not a
    request the API can be given, so its reach is bounded the only way sewing's can be, per body.
    Declaring it is what stops the accounting from pretending otherwise."""
    assert WHOLE_SHAPE_OPS == frozenset({"fix_small_edges", "fix_shape_global", "sew_faces"})
    names = {op.name for op in catalogue(split_closed_faces=True, fragment_bodies=True,
                                         rebuild_face=True, close_wire_gaps=True)}
    assert WHOLE_SHAPE_OPS <= names


@pytest.mark.parametrize("name", sorted(WHOLE_SHAPE_OPS))
def test_every_whole_shape_operation_reads_its_own_history(name):
    """The contract: an operation declared whole-shape must populate both declarations off its own
    history. A new whole-shape wrapper added without them fails here."""
    import inspect

    import meshpipeline.cad.repair.operations as module

    op = next(o for o in catalogue(split_closed_faces=True, fragment_bodies=True,
                                   rebuild_face=True, close_wire_gaps=True) if o.name == name)
    source = inspect.getsource(type(op))
    assert "removed_fingerprints" in source, f"{name} does not declare what it removed"
    assert "rebuilt=" in source, f"{name} does not declare what it rebuilt"
    assert module is not None


# THE DECLARATION IS BOOKKEEPING: THE SHAPE IS UNCHANGED
#
# The hard invariant. Each operation's output is compared, as BREP TEXT, with the bare kernel
# call it wraps run on a freshly built copy of the same input - so reading the history can never
# have altered what the operation produces. Fresh copies, because ShapeFix edits tolerances and
# pcurves IN PLACE: a second run on the same input object would start from different bytes.
# (The commit that added the declarations was also checked byte-for-byte against the previous
# implementation's output on these fixtures; this test keeps that true from here on.)


def _brep(shape, path) -> bytes:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path.read_bytes()


def _bare_wireframe(shape, tolerance_mm: float):
    fixer = _wireframe(shape, tolerance_mm=tolerance_mm, drop=False)
    return fixer.Context().Apply(shape)


def _bare_shapefix(shape, tolerance_mm: float):
    from OCP.ShapeFix import ShapeFix_Shape

    fixer = ShapeFix_Shape(shape)
    fixer.SetPrecision(tolerance_mm)
    fixer.SetMaxTolerance(tolerance_mm)
    fixer.FixSolidMode = 1
    fixer.FixFreeShellMode = 1
    fixer.FixFreeFaceMode = 1
    fixer.FixSameParameterMode = 1
    fixer.FixVertexPositionMode = 0
    fixer.Perform()
    return fixer.Shape()


def _bare_sewing(shape, tolerance_mm: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    from meshpipeline.cad.repair.conservative import _as_solid_if_closed

    sewing = BRepBuilderAPI_Sewing(tolerance_mm)
    sewing.SetNonManifoldMode(False)
    sewing.Add(shape)
    sewing.Perform()
    return _as_solid_if_closed(sewing.SewedShape())


@pytest.mark.parametrize(("op", "bare", "make", "tolerance_mm"), [
    (FixSmallEdges, _bare_wireframe, _sliver_plate, 0.014),
    (FixSmallEdges, _bare_wireframe, lambda: _loose_faces(_sliver_plate(), _speck()), 0.014),
    (FixShapeGlobal, _bare_shapefix, _sliver_plate, 0.014),
    (FixShapeGlobal, _bare_shapefix, lambda: _loose_faces(_sliver_plate(), _speck()), 0.014),
    (SewFaces, _bare_sewing, lambda: _loose_faces(_sliver_plate()), 0.1),
    (SewFaces, _bare_sewing, lambda: _loose_faces(_sliver_plate(), _speck()), 0.1),
], ids=["edges-plate", "edges-loose", "global-plate", "global-loose", "sew-torn", "sew-speck"])
def test_declaring_does_not_change_the_shape_produced(op, bare, make, tolerance_mm, tmp_path):
    first, second = make(), make()
    assert _brep(first, tmp_path / "a.brep") == _brep(second, tmp_path / "b.brep")
    target = _ref(first, _faces(first)[0], "face")
    if op is FixSmallEdges:
        target = _ref(first, _sliver_edge(first), "edge")
    outcome = op().apply(first, (target,), tolerance_mm=tolerance_mm)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert (_brep(outcome.shape, tmp_path / "op.brep")
            == _brep(bare(second, tolerance_mm), tmp_path / "bare.brep"))
