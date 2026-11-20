# Responsibility: Prove the mesh probe's error classifier names the kind and the surfaces of a gmsh failure.
# Boundaries: pure string classification; nothing here runs gmsh (see tests/native for the probe itself).
from __future__ import annotations

import pytest

from meshpipeline.cad.repair.mesh_probe import (
    EDGE_THROUGH_FACE,
    OPEN_LOOP,
    OTHER,
    OVERLAPPING_FACETS,
    PERIODIC_SURFACE,
    classify,
)


@pytest.mark.parametrize("error, kind, surfaces", [
    ("Invalid boundary mesh (overlapping facets) on surface 33 surface 33", OVERLAPPING_FACETS, (33,)),
    ("Invalid boundary mesh (overlapping facets) on surface 320 surface 324", OVERLAPPING_FACETS, (320, 324)),
    ("Impossible to mesh periodic surface 6", PERIODIC_SURFACE, (6,)),
    ("PLC Error:  A segment and a facet intersect at point (1.5,2,-3)", EDGE_THROUGH_FACE, ()),
    ("The 1D mesh seems not to be forming a closed loop", OPEN_LOOP, ()),
    ("something gmsh never said before", OTHER, ()),
])
def test_classify_names_kind_and_surfaces(error, kind, surfaces):
    failure = classify(error, [])
    assert failure.kind == kind
    assert failure.surfaces == surfaces


def test_plc_point_is_parsed():
    failure = classify("PLC Error:  A segment and a facet intersect at point (1.5,2,-3)", [])
    assert failure.point == (1.5, 2.0, -3.0)


def test_open_loop_takes_surface_from_log():
    log = ["Meshing surface 3 (Plane)", "Meshing surface 12 (Plane, Frontal-Delaunay)"]
    failure = classify("The 1D mesh seems not to be forming a closed loop", log)
    assert failure.surfaces == (12,)


def test_open_loop_without_log_has_no_surface():
    assert classify("The 1D mesh seems not to be forming a closed loop", []).surfaces == ()


def test_plc_error_takes_surface_from_log_when_it_names_none():
    log = ["Meshing surface 3 (Plane)", "Meshing surface 7 (Plane, Frontal-Delaunay)"]
    failure = classify("PLC Error:  A segment and a facet intersect at point (1,2,3)", log)
    assert failure.kind == EDGE_THROUGH_FACE
    assert failure.surfaces == (7,)


# --- Mesh failures become located defects -------------------------------------------------------

def test_faces_for_tags_matches_by_box():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1), 2: (5, 5, 5, 6, 6, 6), 3: (-3, 0, 0, -2, 1, 1)}
    tags = {10: (5, 5, 5, 6, 6, 6), 11: (-3, 0, 0, -2, 1, 1), 12: (0, 0, 0, 1, 1, 1 + 1e-9)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {10: 2, 11: 3, 12: 1}


def test_ambiguous_tag_stays_unlocated():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1), 2: (0, 0, 0, 1, 1, 1), 3: (5, 5, 5, 6, 6, 6)}
    tags = {7: (0, 0, 0, 1, 1, 1), 8: (5, 5, 5, 6, 6, 6)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {8: 3}


def test_tag_outside_tolerance_is_unmapped():
    from meshpipeline.cad.repair.mesh_probe import faces_for_tags

    faces = {1: (0, 0, 0, 1, 1, 1)}
    tags = {4: (0, 0, 0, 1, 1, 1.01), 5: (0, 0, 0, 1, 1, 1 + 5e-7)}
    assert faces_for_tags(faces, tags, tol=1e-6) == {5: 1}


def test_every_kind_has_a_code():
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.mesh_probe import (
        CODE_FOR_KIND,
        EDGE_THROUGH_FACE,
        NO_VOLUME,
        OPEN_LOOP,
        OTHER,
        OVERLAPPING_FACETS,
        PERIODIC_SURFACE,
        PROBE_ERROR,
        TIMEOUT,
    )

    kinds = {OVERLAPPING_FACETS, EDGE_THROUGH_FACE, OPEN_LOOP, PERIODIC_SURFACE, NO_VOLUME,
             TIMEOUT, PROBE_ERROR, OTHER}
    assert set(CODE_FOR_KIND) == kinds
    assert len(set(CODE_FOR_KIND.values())) == len(kinds)   # one-to-one
    assert all(isinstance(code, DefectCode) for code in CODE_FOR_KIND.values())
    assert CODE_FOR_KIND[OVERLAPPING_FACETS] is DefectCode.mesh_overlapping_facets
    assert CODE_FOR_KIND[TIMEOUT] is DefectCode.mesh_timeout


def test_mesh_codes_are_repairable_not_judgement():
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.triage import _NEEDS_JUDGEMENT, _REPAIRABLE

    mesh = {c.value for c in DefectCode if c.value.startswith("mesh_")}
    assert mesh == {"mesh_overlapping_facets", "mesh_edge_through_face", "mesh_open_loop",
                    "mesh_periodic_surface", "mesh_no_volume", "mesh_timeout", "mesh_probe_error",
                    "mesh_other"}
    assert mesh <= _REPAIRABLE
    assert not mesh & _NEEDS_JUDGEMENT


# --- Which surfaces a failure may be located on -------------------------------------------------

def test_log_fallback_is_marked_and_staged():
    from meshpipeline.cad.repair.mesh_probe import STAGE_3D

    failure = classify("something gmsh never said before", ["Meshing surface 4 (Plane)"],
                       stage=STAGE_3D)
    assert failure.surfaces == (4,)
    assert failure.surfaces_from_log is True
    assert failure.stage == STAGE_3D


def test_error_text_surfaces_are_not_log_derived():
    from meshpipeline.cad.repair.mesh_probe import STAGE_3D

    failure = classify("Invalid boundary mesh (overlapping facets) on surface 33 surface 33",
                       ["Meshing surface 40"], stage=STAGE_3D)
    assert failure.surfaces == (33,)
    assert failure.surfaces_from_log is False


@pytest.mark.parametrize("error, stage, expected", [
    # A log surface is the face being meshed only when the 2D stage is what failed.
    ("The 1D mesh seems not to be forming a closed loop", "2d", (12,)),
    ("something gmsh never said before", "2d", (12,)),
    # In the volume stage the last 2D surface is a guess, whatever the kind.
    ("something gmsh never said before", "3d", ()),
    ("The 1D mesh seems not to be forming a closed loop", "3d", ()),
    # An unknown stage is not trusted either.
    ("something gmsh never said before", "", ()),
    # Surfaces gmsh named in its own error are trusted in any stage.
    ("Invalid boundary mesh (overlapping facets) on surface 33 surface 34", "3d", (33, 34)),
    # An edge through a face is located only by its point (R7).
    ("PLC Error:  A segment and a facet intersect at point", "2d", ()),
])
def test_locating_surfaces_refuses_guesses(error, stage, expected):
    from meshpipeline.cad.repair.mesh_probe import locating_surfaces

    log = ["Meshing surface 3 (Plane)", "Meshing surface 12 (Plane)"]
    assert locating_surfaces(classify(error, log, stage=stage)) == expected


def test_box_distance_is_zero_inside_and_euclidean_outside():
    from meshpipeline.cad.repair.mesh_probe import _box_distance

    box = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
    assert _box_distance((0.5, 0.5, 0.5), box) == 0.0
    assert _box_distance((4.0, 5.0, 0.5), box) == pytest.approx(5.0)


def test_a_mesh_defect_records_the_stage_that_failed():
    # R11a: the session needs the stage to know a scoped (2D-only) probe could not have seen it.
    from meshpipeline.cad.repair.mesh_probe import STAGE_3D, MeshFailure, _unlocated, volume_stage

    defect = _unlocated(MeshFailure(OVERLAPPING_FACETS, (4,), None, "overlap", stage=STAGE_3D))
    assert defect.measurements["stage"] == STAGE_3D
    assert volume_stage(defect)


@pytest.mark.parametrize("code, stage, expected", [
    ("mesh_edge_through_face", "", True),
    ("mesh_edge_through_face", "2d", True),
    ("mesh_no_volume", "", True),
    ("mesh_overlapping_facets", "3d", True),
    ("mesh_overlapping_facets", "2d", False),
    ("mesh_overlapping_facets", "", False),
    ("mesh_open_loop", "2d", False),
    ("open_shell", "3d", False),
])
def test_volume_stage_is_the_code_or_the_recorded_stage(code, stage, expected):
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import EntityDefect
    from meshpipeline.cad.repair.mesh_probe import volume_stage

    defect = EntityDefect(code=DefectCode(code), severity=DefectSeverity.error,
                          entity_type="face", entity_index=1, message="",
                          measurements={"stage": stage} if stage else {})
    assert volume_stage(defect) is expected


# --- Task 3b: a no-coordinates PLC failure is located by contact measurement ----------------------

_PLC_BARE = "PLC Error:  A segment and a facet intersect"


def _two_boxes_and_a_fake_probe(monkeypatch, *, supports, calls=None):
    """mesh_defects over a real 2-solid compound, a faked gmsh answer and a faked pair finder."""
    pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.mesh_probe import MeshFailure, ProbeResult

    builder, shape = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(shape)
    for x in (0.0, 10.0):
        builder.Add(shape, BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 10, 10, 10).Shape())
    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    monkeypatch.setattr(mesh_probe, "probe_file",
                        lambda path, **kw: ProbeResult(False, 0, failure, {}))

    def fake(shape_, faces, *, tol, deadline):
        if calls is not None:
            calls.append(deadline)
        if not supports:
            return None
        return mesh_probe.Contact(faces=tuple(supports), pair="solids:1+2", pairs_total=3,
                                  pairs_considered=3)

    monkeypatch.setattr(mesh_probe, "_contact_support_faces", fake)
    return mesh_probe, shape


def test_plc_without_coordinates_is_located_by_contact(monkeypatch):
    mesh_probe, shape = _two_boxes_and_a_fake_probe(monkeypatch, supports=[3, 7])
    found = mesh_probe.mesh_defects(shape, diagonal_mm=17.3, budget_s=60.0)
    assert [d.entity_index for d in found] == [3, 7]
    assert all(d.code.value == "mesh_edge_through_face" and d.entity_type == "face"
               and d.measurements["stage"] == "3d" and d.fingerprint for d in found)
    assert all(d.measurements["located_by"] == "contact-pair" for d in found)
    assert all(d.measurements["pair"] == "solids:1+2" and d.measurements["pairs_total"] == 3
               and d.measurements["pairs_considered"] == 3 for d in found)


def test_located_by_marker_present(monkeypatch):
    mesh_probe, shape = _two_boxes_and_a_fake_probe(monkeypatch, supports=[2])
    (defect,) = mesh_probe.mesh_defects(shape, diagonal_mm=17.3, budget_s=60.0)
    assert defect.measurements["located_by"] == mesh_probe.LOCATED_BY_CONTACT == "contact-pair"


def test_no_contact_pair_stays_unlocated(monkeypatch):
    mesh_probe, shape = _two_boxes_and_a_fake_probe(monkeypatch, supports=[])
    (defect,) = mesh_probe.mesh_defects(shape, diagonal_mm=17.3, budget_s=60.0)
    assert defect.entity_index == 0 and defect.fingerprint == ""
    assert "located_by" not in defect.measurements


def test_single_solid_plc_stays_unlocated(monkeypatch):
    # The REAL seam: one solid has no pair, so nothing is inferred.
    mesh_probe, _shape = _two_boxes_and_a_fake_probe(monkeypatch, supports=[3])
    monkeypatch.undo()
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from meshpipeline.cad.repair import mesh_probe as mp
    from meshpipeline.cad.repair.mesh_probe import MeshFailure, ProbeResult

    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    monkeypatch.setattr(mp, "probe_file", lambda path, **kw: ProbeResult(False, 0, failure, {}))
    (defect,) = mp.mesh_defects(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), diagonal_mm=17.3,
                                budget_s=60.0)
    assert defect.entity_index == 0 and defect.fingerprint == ""
    assert "located_by" not in defect.measurements


def test_contact_search_respects_budget(monkeypatch):
    import time

    mesh_probe, shape = _two_boxes_and_a_fake_probe(monkeypatch, supports=[3, 7])
    monkeypatch.undo()
    from meshpipeline.cad.repair import mesh_probe as mp
    from meshpipeline.cad.repair.mesh_probe import MeshFailure, ProbeResult

    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    monkeypatch.setattr(mp, "probe_file", lambda path, **kw: ProbeResult(False, 0, failure, {}))
    # An expired deadline: the real seam answers None (out of budget), never a guess.
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed
    assert mp._contact_support_faces(shape, _indexed(shape, TopAbs_FACE), tol=1e-6,
                                     deadline=time.monotonic() - 1.0) is None
    # And through mesh_defects the whole call stays unlocated at a spent budget.
    (defect,) = mp.mesh_defects(shape, diagonal_mm=17.3, budget_s=0.0)
    assert defect.entity_index == 0 and "located_by" not in defect.measurements


# --- surface-mesh clash: gmsh's PLC names nothing, the clashing triangles do -------------------

def _clash_probe(monkeypatch, *, failure, clashes, contact_calls):
    """mesh_defects over a real 2-box compound; gmsh's answer faked WITH a clash report and tag
    boxes equal to the OCC faces' own, so tag N is face N."""
    pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.mesh_probe import ProbeResult

    builder, shape = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(shape)
    for x in (0.0, 5.0):          # overlapping boxes: a real interference
        builder.Add(shape, BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 10, 10, 10).Shape())
    boxes = mesh_probe._face_boxes(_indexed(shape, TopAbs_FACE))
    monkeypatch.setattr(mesh_probe, "probe_file",
                        lambda path, **kw: ProbeResult(False, 0, failure, boxes, clashes))

    def no_contact(*a, **kw):
        contact_calls.append(1)
        return None

    monkeypatch.setattr(mesh_probe, "_contact_support_faces", no_contact)
    return mesh_probe, shape


def test_plc_with_no_point_is_located_by_the_clashing_triangles(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    calls: list = []
    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=calls,
                                     clashes=(Clash((2, 7), 12, (5.0, 5.0, 5.0), "crossing"),))

    found = mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)

    assert [d.entity_index for d in found] == [2, 7]
    assert all(d.code.value == "mesh_edge_through_face" and d.fingerprint
               and d.measurements["stage"] == "3d" for d in found)
    assert all(d.measurements["located_by"] == mesh_probe.LOCATED_BY_CLASH
               == "surface-mesh-clash" for d in found)
    assert found[0].measurements["clash_pairs"] == [[2, 7]]
    assert found[0].measurements["clash_kind"] == "crossing"
    assert calls == []            # evidence first: the contact inference was never needed


def test_overlap_tags_gmsh_could_not_map_fall_back_to_the_clash(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    failure = MeshFailure(OVERLAPPING_FACETS, (999,), None,
                          "Invalid boundary mesh (overlapping facets) on surface 999",
                          stage="3d")
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=[],
                                     clashes=(Clash((3, 9), 4, (1.0, 1.0, 1.0), "coplanar"),))

    found = mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)

    assert [d.entity_index for d in found] == [3, 9]
    assert all(d.code.value == "mesh_overlapping_facets" for d in found)


def test_no_clash_still_falls_back_to_contact(monkeypatch):
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    calls: list = []
    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, clashes=(),
                                     contact_calls=calls)
    (defect,) = mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)
    assert defect.fingerprint == "" and calls == [1]


def test_a_clash_records_the_kernel_gap_between_its_faces(monkeypatch):
    # A clash between faces that do not meet in the B-rep is the coarse surface mesh's chord
    # cutting across a thin gap: a mesher-side cause. The gap is evidence the author can read.
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    # Box 1 spans x 0..10, box 2 x 5..15; face 1 is box 1's x=0 wall, face 8 box 2's x=15 wall.
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=[],
                                     clashes=(Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing"),))
    found = mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)

    gaps = {d.measurements["clash_gap_mm"] for d in found}
    assert len(gaps) == 1
    (gap,) = gaps
    assert gap is not None and abs(gap - 15.0) < 1e-6


def test_each_face_carries_its_own_clash_measurements(monkeypatch):
    # Greptile #495: every face took the FIRST pair's gap, kind and point, so a face whose pair
    # really meets could read as another pair's sizing gap.
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    # Pair (1, 8): 15 mm apart. Pair (2, 9): box 1's x=10 wall crosses box 2's y=0 floor, gap 0.
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=[],
                                     clashes=(Clash((1, 8), 5, (5.0, 5.0, 5.0), "crossing"),
                                              Clash((2, 9), 2, (10.0, 0.0, 5.0), "coplanar")))
    by_face = {d.entity_index: d.measurements
               for d in mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)}

    assert set(by_face) == {1, 2, 8, 9}
    for face in (1, 8):
        assert by_face[face]["clash_gap_mm"] == pytest.approx(15.0)
        assert by_face[face]["clash_kind"] == "crossing"
        assert by_face[face]["clash_pair"] == [1, 8]
    for face in (2, 9):
        assert by_face[face]["clash_gap_mm"] == pytest.approx(0.0, abs=1e-9)
        assert by_face[face]["clash_kind"] == "coplanar"
        assert by_face[face]["clash_point"] == [10.0, 0.0, 5.0]
        assert by_face[face]["clash_pair"] == [2, 9]


# --- face sizing: a DIAGNOSTIC for gap-only clashes, never a repair ------------------------------

def _sizing_probe(monkeypatch, *, clashes, sized_meshes):
    """face_sizing over a real 2-box compound (box 1 x 0..10, box 2 x 5..15), gmsh faked: the
    unsized probe fails with `clashes`; a sized one meshes per `sized_meshes` (one bool per call)."""
    pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.mesh_probe import MeshFailure, ProbeResult

    builder, shape = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(shape)
    for x in (0.0, 5.0):
        builder.Add(shape, BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 10, 10, 10).Shape())
    boxes = mesh_probe._face_boxes(_indexed(shape, TopAbs_FACE))
    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    calls: list = []
    answers = iter(sized_meshes)

    def fake(path, *, diagonal_mm, sizing=None, **kw):
        calls.append(sizing)
        if sizing is None:
            return ProbeResult(False, 0, failure, boxes, clashes)
        if next(answers):
            return ProbeResult(True, 100, None, boxes)
        return ProbeResult(False, 0, failure, boxes, clashes)

    monkeypatch.setattr(mesh_probe, "probe_file", fake)
    return mesh_probe, shape, calls


def test_gap_only_clashes_are_retried_with_face_sizing(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash

    # Face 1 (x=0) and face 8 (x=15) are 15 mm apart: a gap, not an intersection.
    mesh_probe, shape, calls = _sizing_probe(
        monkeypatch, clashes=(Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing"),),
        sized_meshes=[True])

    found = mesh_probe.face_sizing(shape, diagonal_mm=20.0, budget_s=60.0)

    assert found["applicable"] is True and found["meshes"] is True
    assert found["surfaces"] == [1, 8]
    assert found["pairs"] == [{"surfaces": [1, 8], "gap_mm": pytest.approx(15.0),
                               "adjacent": False}]
    # The first size is the coarse one: diagonal / 200.
    assert found["size_mm"] == pytest.approx(0.1)
    assert calls[1] == {"surfaces": [1, 8], "size": pytest.approx(0.1)}


def test_a_coarse_sizing_that_fails_falls_through_to_the_finer_one(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash

    mesh_probe, shape, calls = _sizing_probe(
        monkeypatch, clashes=(Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing"),),
        sized_meshes=[False, True])

    found = mesh_probe.face_sizing(shape, diagonal_mm=20.0, budget_s=60.0)

    assert found["meshes"] is True
    assert [a["meshes"] for a in found["attempts"]] == [False, True]
    assert found["size_mm"] == pytest.approx(20.0 / 1000.0)


def test_a_real_intersection_is_not_a_sizing_question(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash

    # Face 2 (box 1's x=10 wall) crosses face 9 (box 2's y=0 floor): they meet, gap 0.
    mesh_probe, shape, calls = _sizing_probe(
        monkeypatch, clashes=(Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing"),
                              Clash((2, 9), 2, (10.0, 0.0, 5.0), "crossing")),
        sized_meshes=[])

    found = mesh_probe.face_sizing(shape, diagonal_mm=20.0, budget_s=60.0)

    assert found["applicable"] is False and found["reason"] == "real_intersection"
    assert found["meshes"] is None
    assert calls == [None]          # never re-meshed with sizing


def test_no_clash_is_not_applicable(monkeypatch):
    mesh_probe, shape, calls = _sizing_probe(monkeypatch, clashes=(), sized_meshes=[])
    found = mesh_probe.face_sizing(shape, diagonal_mm=20.0, budget_s=60.0)
    assert found["applicable"] is False and found["reason"] == "no_clash"
    assert calls == [None]


def test_faces_that_share_an_edge_are_adjacent_not_intersecting(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash

    # Face 1 (x=0 wall) and face 3 (y=0 floor) of box 1 share an edge: distance 0, but that is
    # their common boundary, not a crossing. Triangles of two adjacent faces crossing is a
    # sliver-angle fold the mesher's sizing answers, so the diagnostic still applies.
    mesh_probe, shape, calls = _sizing_probe(
        monkeypatch, clashes=(Clash((1, 3), 2, (0.0, 0.0, 5.0), "crossing"),
                              Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing")),
        sized_meshes=[True])

    found = mesh_probe.face_sizing(shape, diagonal_mm=20.0, budget_s=60.0)

    assert found["applicable"] is True and found["meshes"] is True
    assert found["pairs"][0] == {"surfaces": [1, 3], "gap_mm": pytest.approx(0.0, abs=1e-9),
                                 "adjacent": True}


def test_the_first_sizing_is_coarse_and_the_second_is_set_by_the_smallest_gap(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash

    # MEASURED, abc/0051_00519054 (gaps 0.01 mm): diagonal / 200 meshed it, and a gap-driven size
    # hit the floor and spent the budget. So the coarse size is tried first, the gap's second.
    # Face 2 (x=10) / face 7 (x=5) are 5 mm apart, face 1/8 15 mm: a fifth of the smallest is 1.0,
    # finer than diagonal / 1000 = 2 on a 2000 mm part, so the gap sets the second size.
    mesh_probe, shape, calls = _sizing_probe(
        monkeypatch, clashes=(Clash((1, 8), 3, (5.0, 5.0, 5.0), "crossing"),
                              Clash((2, 7), 3, (7.0, 5.0, 5.0), "crossing")),
        sized_meshes=[False, True])

    found = mesh_probe.face_sizing(shape, diagonal_mm=2000.0, budget_s=60.0)

    assert [a["size_mm"] for a in found["attempts"]] == [pytest.approx(10.0), pytest.approx(1.0)]
    assert found["size_mm"] == pytest.approx(1.0)


def test_a_clash_defect_says_whether_its_faces_are_adjacent(monkeypatch):
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    failure = MeshFailure(EDGE_THROUGH_FACE, (), None, _PLC_BARE, stage="3d")
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=[],
                                     clashes=(Clash((1, 3), 2, (0.0, 0.0, 5.0), "crossing"),))
    found = mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)
    assert {d.measurements["clash_adjacent"] for d in found} == {True}


def test_a_face_gmsh_named_still_carries_its_clash_evidence(monkeypatch):
    # Lane F's "clash not located" bucket: gmsh names the overlapping surfaces itself, so the face
    # was located - but without the clash evidence (kind, gap, adjacency) that says what kind of
    # failure it is. A gmsh-named face that is in a clash pair carries that pair's evidence.
    from meshpipeline.cad.repair.mesh_clash import Clash
    from meshpipeline.cad.repair.mesh_probe import MeshFailure

    failure = MeshFailure(OVERLAPPING_FACETS, (1, 3), None,
                          "Invalid boundary mesh (overlapping facets) on surface 1 surface 3",
                          stage="3d")
    mesh_probe, shape = _clash_probe(monkeypatch, failure=failure, contact_calls=[],
                                     clashes=(Clash((1, 3), 3, (0.0, 0.0, 5.0), "fold"),))
    found = {d.entity_index: d.measurements
             for d in mesh_probe.mesh_defects(shape, diagonal_mm=20.6, budget_s=60.0)}

    assert set(found) == {1, 3}
    for face in (1, 3):
        assert "located_by" not in found[face]          # gmsh named it; the clash only explains
        assert found[face]["clash_kind"] == "fold"
        assert found[face]["clash_pair"] == [1, 3]
        assert found[face]["clash_adjacent"] is True
        assert found[face]["clash_gap_mm"] == pytest.approx(0.0, abs=1e-9)
