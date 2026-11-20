# Responsibility: Verify every scan in one repair run names entities on ONE grid - the upload's -
#                 so an entity's name is a fact about the entity and not about what else happened
#                 to the part.
from __future__ import annotations

import meshpipeline.cad.repair.session as session_module
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import _diagonal, localize_scan
from meshpipeline.cad.repair.session import RepairSession

# WHY THIS FILE EXISTS (docs/superpowers/specs/2026-11-19-target-remains-diagnosis.md).
#
# `identity.quantum_for` snaps the part's diagonal UP to a binary octave, so two scans of one part
# agree unless a repair changed its size by nearly a factor of two. That removed the common case
# of the moving grid; it did not remove the grid's dependence on the shape being scanned. A part
# whose extent crosses an octave - and the honest residual the octave fix already names - still
# gets every entity renamed, including entities nobody touched, and the progress rule then cannot
# find the defect it just named and reads that as the defect being gone. A FALSE KEEP.
#
# The fix is that a RUN names at the UPLOAD's diagonal, which never moves, so the grid is a
# property of the run rather than of the scan. The tests below pin both halves: that the parameter
# holds a name still, and that a caller which cannot supply it keeps exactly today's behaviour.

#: A square face in the z = 0 plane. ONE FACE IS ALREADY A DEFECT - its boundary is free - so
#: `localize_scan` has something to name without needing a broken fixture on disk.
def _face(x0: float, y0: float, x1: float, y1: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    plane = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    return BRepBuilderAPI_MakeFace(plane, x0, x1, y0, y1).Face()


def _grown(part):
    """`part`, plus a speck far enough away to push the part's diagonal over an OCTAVE.

    The speck is what a repair's extent change stands in for here, and it is deliberately
    extravagant: the octave snap absorbs anything smaller, so a test that moved the diagonal by a
    micrometre would pass with the parameter ignored. The original face's own entities are
    untouched and keep their bounding boxes to the bit.
    """
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, part)
    builder.Add(compound, _face(7.0, 7.0, 7.1, 7.1))
    return compound


def _named(found) -> dict[tuple, str]:
    """Each defect's fingerprint, keyed by WHERE IT IS - the thing a name is supposed to track."""
    return {(d.entity_type, d.code.value,
             tuple(d.location.get("bbox_min") or ()),
             tuple(d.location.get("bbox_max") or ())): d.fingerprint
            for d in found}


def test_two_scans_of_one_part_at_different_extents_name_the_same_entity_the_same():
    part = _face(0.0, 0.0, 1.0, 1.0)
    upload_diagonal = _diagonal(part)

    before, _ = localize_scan(part, quantum_diagonal_mm=upload_diagonal)
    after, _ = localize_scan(_grown(part), quantum_diagonal_mm=upload_diagonal)

    survivor = next(iter(_named(before)))
    assert _named(before)[survivor] == _named(after)[survivor], (
        "the same entity, at the same box to the bit, was renamed because the PART's extent moved")


def test_without_the_runs_grid_the_same_entity_is_renamed_which_is_why_it_exists():
    # THE RED THIS FIX IS FOR, kept as a test so the one above cannot pass vacuously: scanned at
    # its own diagonal each time, the untouched entity gets two different names.
    part = _face(0.0, 0.0, 1.0, 1.0)
    grown = _grown(part)
    assert _diagonal(part) < 2.0 <= 8.0 < _diagonal(grown), "the extents must cross an octave"

    before, _ = localize_scan(part)
    after, _ = localize_scan(grown)

    survivor = next(iter(_named(before)))
    assert _named(before)[survivor] != _named(after)[survivor]


def test_a_caller_with_no_upload_diagonal_gets_byte_for_byte_the_scan_it_always_got():
    # `brep.inspect_brep_file` scans ONE shape once and has no run to belong to. A caller that
    # cannot supply the number must keep today's behaviour rather than be handed a guess.
    part = _grown(_face(0.0, 0.0, 1.0, 1.0))

    assert localize_scan(part) == localize_scan(part, quantum_diagonal_mm=None)
    assert localize_scan(part) == localize_scan(part, quantum_diagonal_mm=_diagonal(part))


def test_the_session_names_every_scan_in_the_run_at_the_uploads_diagonal(monkeypatch):
    """The session HAS the number (`self.original.diagonal_mm`); the point is that it passes it.

    Watched on the module attribute because that is how the session looks `localize_scan` up -
    at call time - and the point of this test is the ARGUMENT, so no kernel is needed.
    """
    seen: list[dict] = []
    monkeypatch.setattr(session_module, "_localize_scan",
                        lambda _shape, **kw: seen.append(kw) or ((), True))
    upload = Measure(valid=True, solids=1, shells=1, faces=6, edges=12, vertices=8,
                     diagonal_mm=1822.8446848590597, max_tolerance_mm=1e-5,
                     surface_area_mm2=10.0, volume_mm3=1.0)
    work = RepairSession("part", measurer=lambda _s: upload)

    work.defects()
    # The part has been repaired and its extent has moved, exactly as on
    # 0059_00592781_58be5d61afe56e0fcdda08e3_step_000. The grid must not move with it.
    work.shape = "repaired"
    work._defects = None
    work.defects()
    work._rescan(face_scan=False)

    assert seen, "the session never scanned"
    assert [kw.get("quantum_diagonal_mm") for kw in seen] == [upload.diagonal_mm] * len(seen)


def test_an_operation_inside_the_session_names_on_the_runs_grid_not_its_own_inputs():
    """OPERATIONS NAME ON THE RUN'S GRID TOO, not only scans (PR #494 review, session.py:611).

    Every rebuilding operation names what it made at `_diagonal_of(its input shape)` - which after
    a kept repair is the CURRENT shape's, not the upload's. Across an octave those are two grids,
    so a patch's `outcome.added` name and the next scan's name for that patch diverge and the
    scan's allowed warning on it reads as `new_defect`. Here the op runs on a shape whose extent
    already crossed an octave (as after an earlier kept repair) and names its own faces exactly as
    the real operations do; inside the session those names must be the scan's names.
    """
    from meshpipeline.cad.repair.operations import OpOutcome, _diagonal_of, _face_names

    part = _face(0.0, 0.0, 1.0, 1.0)
    grown = _grown(part)
    upload_diagonal = _diagonal(part)
    assert _diagonal(part) < 2.0 <= 8.0 < _diagonal(grown), "the extents must cross an octave"
    seen: list[tuple[str, ...]] = []

    class NamesItsOwnFaces:
        name = "names_its_own_faces"
        answers = ()
        whole_shape = False

        def apply(self, shape, targets, *, tolerance_mm):
            names = _face_names(shape, diagonal_mm=_diagonal_of(shape))
            seen.append(names)
            return OpOutcome(shape=shape, added=names)

    upload = Measure(valid=True, solids=0, shells=0, faces=1, edges=4, vertices=4,
                     diagonal_mm=upload_diagonal, max_tolerance_mm=1e-7,
                     surface_area_mm2=1.0, volume_mm3=0.0)
    work = RepairSession(grown, measurer=lambda _s: upload)
    defect = next(d for d in work.defects())
    ref = EntityRef(defect.entity_type, defect.fingerprint)
    work.try_op(NamesItsOwnFaces(), (ref,), defect.code, tolerance_mm=1e-3)

    assert seen, "the operation never ran"
    assert seen[0] == _face_names(grown, diagonal_mm=upload_diagonal), (
        "an operation named its faces on its input shape's grid, not the run's")
