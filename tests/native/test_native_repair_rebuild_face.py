# Responsibility: Verify on the REAL kernel that rebuild_face re-trims a face into a kept-equivalent
#                 part (same counts, deviation ~0, valid, volume unchanged), keeps a seamed and a
#                 located face equivalent too, and report what the region loop does with it on the
#                 known single-solid overlapping-facets corpus file.
# Boundaries: the native tier; the claim, declines, flag gating and the rules baseline's fallback
#             chain are unit-tested hermetically (tests/unit/cad/test_repair_rebuild_face.py).
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepBuilderAPI", reason="the CAD kernel is a native-tier dependency")

import meshpipeline.cad.repair.rules_author as rules_author_module  # noqa: E402
import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair.caps import measure  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for  # noqa: E402
from meshpipeline.cad.repair.operations import (  # noqa: E402
    AREA_DELTA,
    RebuildFace,
    box_of,
    catalogue,
    sub_shape,
)
from meshpipeline.cad.repair.rules_author import mesh_fallbacks, mesh_ops  # noqa: E402


@pytest.fixture
def rebuild_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", True)


def _ref(shape, index: int) -> EntityRef:
    diagonal = measure(shape).diagonal_mm
    return EntityRef(entity_type="face", entity_index=index,
                     fingerprint=fingerprint_of("face", box_of(sub_shape(shape, "face", index)),
                                                quantum=quantum_for(diagonal)))


def _assert_kept_equivalent(shape, outcome):
    before, after = measure(shape), measure(outcome.shape)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.shape is not shape
    assert (after.solids, after.shells, after.faces) == (before.solids, before.shells,
                                                         before.faces)
    assert outcome.evidence["faces_removed"] == outcome.evidence["faces_added"] == 1
    assert after.valid
    assert abs(after.volume_mm3 - before.volume_mm3) <= 1e-6 * before.volume_mm3
    assert outcome.evidence[AREA_DELTA] == pytest.approx(0.0, abs=1e-6)
    assert outcome.evidence["deviation_mm"] == pytest.approx(0.0, abs=1e-6)
    assert outcome.evidence["deviation_samples"] > 0
    assert outcome.rebuilt


def test_a_plain_box_face_rebuilt_is_kept_equivalent(rebuild_on):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    outcome = RebuildFace().apply(shape, (_ref(shape, 1),), tolerance_mm=0.01)
    _assert_kept_equivalent(shape, outcome)
    # Same box, same name: nothing ADDED, the face still the op's own.
    assert outcome.added == ()
    assert outcome.rebuilt == (_ref(shape, 1).fingerprint,)


def test_the_input_is_never_written_into(rebuild_on):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    faces = [sub_shape(shape, "face", i).TShape() for i in range(1, 7)]
    RebuildFace().apply(shape, (_ref(shape, 1),), tolerance_mm=0.01)
    assert [sub_shape(shape, "face", i).TShape() for i in range(1, 7)] == faces


def test_a_seamed_face_on_a_located_body_is_kept_equivalent(rebuild_on):
    # A cylinder's side face carries a seam (one edge used twice), and a body moved by a location
    # gives every face a non-identity location: the re-trim must find the existing pcurves in both.
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    from OCP.TopLoc import TopLoc_Location

    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 1, 0)), 0.4)
    shape = BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape().Moved(TopLoc_Location(turn))
    for index in range(1, measure(shape).faces + 1):
        _assert_kept_equivalent(
            shape, RebuildFace().apply(shape, (_ref(shape, index),), tolerance_mm=0.01))


# THE SPLICE AND THE HOLE, PROVED: every case above is a geometric no-op, so a skipped splice or a
# dropped inner wire with unchanged area would pass them. A plate with a through-hole has a face
# with TWO wires, and spying on the re-trim seam names the exact face that must land in the output.


def _plate_with_hole():
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    plate = BRepPrimAPI_MakeBox(40.0, 40.0, 5.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, -1), gp_Dir(0, 0, 1)), 5.0, 7.0).Shape()
    return BRepAlgoAPI_Cut(plate, hole).Shape()


def _located(shape):
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    turn, shift = gp_Trsf(), gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 2, 3)), 0.7)
    shift.SetTranslation(gp_Vec(13.0, -7.0, 21.0))
    return shape.Moved(TopLoc_Location(shift.Multiplied(turn)))


def _wire_count(face) -> int:
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    wires = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_WIRE, wires)
    return wires.Extent()


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    faces = _indexed(shape, TopAbs_FACE)
    return [faces.FindKey(i) for i in range(1, faces.Extent() + 1)]


@pytest.mark.parametrize("located", [False, True], ids=["plain", "rotated_translated"])
def test_a_holed_face_is_spliced_in_with_its_hole(monkeypatch, rebuild_on, located):
    import meshpipeline.cad.repair.operations as operations

    shape = _plate_with_hole()
    if located:
        shape = _located(shape)
    holed = [i for i, face in enumerate(_faces(shape), start=1) if _wire_count(face) == 2]
    assert len(holed) == 2                       # the top and bottom of the plate
    index = holed[0]
    if located:
        assert not sub_shape(shape, "face", index).Location().IsIdentity()

    seen: list = []
    real = operations._retrim_face

    def spy(face, **kw):
        result = real(face, **kw)
        seen.append(result)
        return result

    monkeypatch.setattr(operations, "_retrim_face", spy)
    outcome = RebuildFace().apply(shape, (_ref(shape, index),), tolerance_mm=0.01)
    _assert_kept_equivalent(shape, outcome)

    (old, new), = seen
    out_faces = _faces(outcome.shape)
    # (a) THE REBUILT FACE KEEPS ITS WIRES - the hole's inner wire was added back.
    assert _wire_count(new) == _wire_count(old) == 2
    # (b) THE OUTPUT HOLDS THE REBUILT FACE, NOT THE OLD ONE: the splice really happened.
    assert sum(face.IsSame(new) for face in out_faces) == 1
    assert not any(face.IsSame(old) for face in out_faces)
    # ...and still has exactly two holed faces, the hole's area still absent from it.
    assert sum(_wire_count(face) == 2 for face in out_faces) == 2
    assert measure(outcome.shape).surface_area_mm2 == pytest.approx(
        measure(shape).surface_area_mm2, rel=1e-9)


# THE REAL MESH FAILURE

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


#: The known single-solid overlapping-facets file: 8 faces, the failure located on face 1 (a
#: closed cone).
_OVERLAP = "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"


def test_corpus_overlap_file_under_the_region_loop(monkeypatch, rebuild_on, capsys):
    """THE END-TO-END OUTCOME, measured 2026-11-14 (see the task report for the numbers).

    gmsh records this file's overlapping facets in the 3D STAGE (the volume mesher reading the
    surface mesh), not the 2D stage. rebuild_face aims only at a 2D-stage overlap, and the rules
    baseline gives a volume-stage overlap no package at all - so the operation is NOT PROPOSED,
    and the file does not mesh after. Asserted as measured, so a change in either direction
    (the probe staging it differently, or the chain aiming at it) is seen here.
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.loop import LoopBudget
    from meshpipeline.cad.repair.region_loop import run_regions
    from meshpipeline.cad.repair.rules_author import rules_author

    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 300.0)
    # THE IMPORT-TIME MAPPING WAS BUILT WITH THE FLAG OFF; the run gets the flag-on one.
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(rebuild_face=True))
    monkeypatch.setattr(rules_author_module, "MESH_FALLBACKS", mesh_fallbacks(rebuild_face=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(rebuild_face=True)})

    shape = _read_shape(_corpus(_OVERLAP))
    started = time.monotonic()
    author = rules_author(shape, original=measure(shape))
    run = run_regions(shape, author=author, tolerance_mm=0.1,
                      budget=LoopBudget(max_ops=6, stall_limit=2, timeout_s=300.0))
    elapsed = time.monotonic() - started
    verdicts = [(v.op, v.status, v.reason) for p in run.packages for v in p.verdicts]
    overlaps = [d for d in run.defects_after if d.code is DefectCode.mesh_overlapping_facets]
    with capsys.disabled():
        print(f"\n[{_OVERLAP}] run {elapsed:.1f}s stopped={run.stopped_because} "
              f"verdicts={verdicts} refusals={[r['reason'] for r in author.refusals]} "
              f"overlaps_after={[(d.entity_index, d.measurements.get('stage')) for d in overlaps]}")

    assert elapsed < 300.0
    assert not any(op == "rebuild_face" for op, _, _ in verdicts), verdicts
    assert overlaps and all(d.measurements.get("stage") == "3d" for d in overlaps), overlaps
    assert "no_operation_claims_it" in {r["reason"] for r in author.refusals}


def test_rebuild_aimed_by_hand_at_the_corpus_face_leaves_the_overlap(rebuild_on):
    """WHAT THE OPERATION WOULD DO IF IT WERE AIMED, on the same file and face: it applies (the
    rebuilt cone is valid, the part kept-equivalent), and the whole-shape probe afterwards still
    reports the same 3D-stage overlapping facets on face 1. The re-trim is a geometric no-op here;
    whatever gmsh's volume stage objects to is not this face's trim."""
    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode

    shape = _read_shape(_corpus(_OVERLAP))
    before = measure(shape)
    found = mesh_probe.mesh_defects(shape, diagonal_mm=before.diagonal_mm, budget_s=300.0)
    assert [(d.code, d.entity_index) for d in found] == [(DefectCode.mesh_overlapping_facets, 1)]

    outcome = RebuildFace().apply(shape, (_ref(shape, 1),), tolerance_mm=0.1)
    _assert_kept_equivalent(shape, outcome)
    after = mesh_probe.mesh_defects(outcome.shape, diagonal_mm=before.diagonal_mm, budget_s=300.0)
    assert [(d.code, d.entity_index) for d in after] == [(DefectCode.mesh_overlapping_facets, 1)]
