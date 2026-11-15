# Responsibility: Verify the gmsh mesh probe against the real gmsh and OCC kernels.
# Boundaries: the native tier; the error classifier's string handling is unit-tested separately.
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")
pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402

from meshpipeline.cad.repair import mesh_probe  # noqa: E402
from meshpipeline.cad.repair.mesh_probe import (  # noqa: E402
    EDGE_THROUGH_FACE,
    OPEN_LOOP,
    OVERLAPPING_FACETS,
    PERIODIC_SURFACE,
    PROBE_ERROR,
    TIMEOUT,
    probe_file,
)


def _box(x=0.0, y=0.0, z=0.0, size=10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), size, size, size).Shape()


def _write(shape, path):
    assert BRepTools.Write_s(shape, str(path))
    return path


def test_box_meshes(tmp_path):
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is True
    assert result.volume_elements > 0
    assert result.failure is None


_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


@pytest.mark.parametrize("name, diagonal, kind", [
    ("abc/00500400_bce216c87d2d17025db6ab36_step_000.step", 132.3, OVERLAPPING_FACETS),
    ("occt/bug32596_abc_0_13.step", 59.5, PERIODIC_SURFACE),
    ("occt/trj10_ch1-id-chs.stp", 463.5, OPEN_LOOP),
    ("abc/00506550_b5806bb84542de8e2656bc88_step_114.step", 870.6, EDGE_THROUGH_FACE),
])
def test_corpus_failures_are_classified_and_located(name, diagonal, kind):
    result = probe_file(_corpus(name), diagonal_mm=diagonal)
    assert result.meshed is False
    assert result.failure is not None
    assert result.failure.kind == kind
    assert result.failure.surfaces
    assert result.tag_boxes


def test_given_surfaces_skip_the_volume(tmp_path):
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3, surfaces=(1,))
    assert result.failure is None
    assert result.volume_elements == 0
    assert len(result.tag_boxes) == 6


def test_unreadable_part_is_a_failure_not_an_exception(tmp_path):
    bad = tmp_path / "bad.brep"
    bad.write_text("this is not a BREP")
    result = probe_file(bad, diagonal_mm=10.0)
    assert result.meshed is False
    assert result.failure is not None and result.failure.message


def test_tag_boxes_cover_every_surface(tmp_path):
    shape = _box()
    faces = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        faces += 1
        explorer.Next()
    path = _write(shape, tmp_path / "box.brep")
    full = probe_file(path, diagonal_mm=17.3)
    only_boxes = probe_file(path, diagonal_mm=17.3, surfaces=())
    assert len(full.tag_boxes) == faces == 6
    assert len(only_boxes.tag_boxes) == faces
    assert only_boxes.volume_elements == 0


@pytest.mark.parametrize("body", ["kill -SEGV $$", "exit 3"])
def test_probe_survives_child_crash(monkeypatch, tmp_path, body):
    # A child that dies without an answer (by signal, or non-zero) is a PROBE_ERROR, not a TIMEOUT.
    fake = tmp_path / "python"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake))
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3)
    assert result.meshed is False
    assert result.failure is not None and result.failure.kind == PROBE_ERROR


def test_a_hung_child_group_is_killed_at_the_budget(monkeypatch, tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nsleep 30 &\nwait\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake))
    start = time.monotonic()
    result = probe_file(_write(_box(), tmp_path / "box.brep"), diagonal_mm=17.3, budget_s=1.0)
    assert time.monotonic() - start < 4.0
    assert result.failure is not None and result.failure.kind == TIMEOUT


def test_probe_from_a_thread_with_gmsh_unimported_in_the_parent(tmp_path):
    """The production shape: repair runs under `asyncio.to_thread`, gmsh never imported here."""
    import subprocess

    brep = _write(_box(), tmp_path / "box.brep")
    code = (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from meshpipeline.cad.repair.mesh_probe import probe_file\n"
        "async def main():\n"
        f"    r = await asyncio.to_thread(probe_file, Path({str(brep)!r}), diagonal_mm=17.3)\n"
        "    assert 'gmsh' not in sys.modules\n"
        "    return r\n"
        "r = asyncio.run(main())\n"
        "print('MESHED', r.meshed, r.volume_elements)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert "MESHED True" in out.stdout, out.stdout + out.stderr


def test_budget_is_honoured():
    path = _corpus("occt/bug32239.STEP")   # takes ~12 s to fail unbounded
    start = time.monotonic()
    result = probe_file(path, diagonal_mm=153.3, budget_s=1.0)
    assert time.monotonic() - start < 3.0   # budget + 2 s
    assert result.meshed is False
    assert result.failure is not None and result.failure.kind == TIMEOUT


# --- Mesh failures become located defects -------------------------------------------------------

def _compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


def _faces(shape):
    from meshpipeline.cad.repair.localize import _indexed

    return _indexed(shape, TopAbs_FACE)


def test_overlap_fixture_defect_names_a_real_face():
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
    from meshpipeline.cad.repair.localize import (
        EntityDefect,
        _centroid_and_box,
        _diagonal,
        _stamped,
    )

    shape = _read_shape(_corpus("abc/00500400_bce216c87d2d17025db6ab36_step_000.step"))
    diagonal = _diagonal(shape)
    found = mesh_probe.mesh_defects(shape, diagonal_mm=diagonal, budget_s=120.0)
    assert [d.code for d in found] == [DefectCode.mesh_overlapping_facets]
    defect = found[0]
    faces = _faces(shape)
    assert defect.severity is DefectSeverity.error
    assert defect.entity_type == "face"
    assert 1 <= defect.entity_index <= faces.Extent()
    assert "overlapping facets" in defect.message
    face = faces.FindKey(defect.entity_index)
    assert defect.location == _centroid_and_box(face)
    # The SAME name localize gives that face, so a package can claim it cleared.
    (reference,) = _stamped((EntityDefect(
        code=DefectCode.self_intersection, severity=DefectSeverity.error, entity_type="face",
        entity_index=defect.entity_index, message="", location=_centroid_and_box(face)),),
        diagonal_mm=diagonal)
    # Stamping a mesh defect in a list of its own gives the scan's name because a fingerprint is
    # the face's box on a grid of diagonal * QUANTUM_FRACTION, and a list-level `#n` suffix appears
    # only when two DIFFERENT faces share a box - which `faces_for_tags` already refuses to map
    # (its tol, diagonal * TAG_MATCH_FRACTION, is that same grid). So it is never ambiguous.
    assert defect.fingerprint
    assert defect.fingerprint == reference.fingerprint
    assert defect.ambiguous is False


def test_mapping_agrees_with_occ_face_count_on_box(tmp_path):
    from meshpipeline.cad.repair.localize import _bounds_of

    shape = _box(1.0, 2.0, 3.0, size=7.0)
    faces = _faces(shape)
    face_boxes = {}
    for i in range(1, faces.Extent() + 1):
        lo, hi = _bounds_of(faces.FindKey(i))
        face_boxes[i] = (*lo, *hi)
    probed = probe_file(_write(shape, tmp_path / "box.brep"), diagonal_mm=12.1, surfaces=())
    mapping = mesh_probe.faces_for_tags(face_boxes, probed.tag_boxes, tol=12.1 * 1e-6)
    assert len(mapping) == faces.Extent() == 6
    assert sorted(mapping.values()) == [1, 2, 3, 4, 5, 6]


def test_within_region_probes_only_region_faces(monkeypatch):
    from meshpipeline.cad.repair.localize import _diagonal
    from meshpipeline.cad.repair.regions import Region

    shape = _compound(_box(), _box(x=100.0))
    diagonal = _diagonal(shape)
    asked = []
    real = mesh_probe.probe_file

    def spy(path, **kwargs):
        result = real(path, **kwargs)
        asked.append((kwargs.get("surfaces"), result.tag_boxes))
        return result

    monkeypatch.setattr(mesh_probe, "probe_file", spy)
    within = Region(index=0, fingerprints=frozenset(), bbox_min=(-1.0, -1.0, -1.0),
                    bbox_max=(11.0, 11.0, 11.0))
    found = mesh_probe.mesh_defects(shape, diagonal_mm=diagonal, budget_s=60.0, within=within)
    assert found == ()
    assert len(asked) == 2   # one boxes-only pass, one scoped mesh
    assert list(asked[0][0]) == []
    meshed = asked[1][0]
    boxes = asked[1][1]
    assert len(meshed) == 6
    assert all(boxes[tag][3] <= 10.0 + 1e-6 for tag in meshed)   # every one on the first box


def _fake_probe(monkeypatch, failure, *, delay_s=0.0):
    """Real tag boxes; `failure` from every probe that was asked to mesh something."""
    real = mesh_probe.probe_file

    def fake(path, **kwargs):
        if kwargs.get("surfaces") is not None and not kwargs["surfaces"]:
            return real(path, **kwargs)
        boxes = real(path, **{**kwargs, "surfaces": ()}).tag_boxes
        time.sleep(delay_s)
        return mesh_probe.ProbeResult(False, 0, failure, boxes)

    monkeypatch.setattr(mesh_probe, "probe_file", fake)


def test_plc_point_goes_to_the_nearest_face(monkeypatch):
    from meshpipeline.cad.repair.contracts import DefectCode

    shape = _box()
    _fake_probe(monkeypatch, mesh_probe.MeshFailure(
        EDGE_THROUGH_FACE, (6,), (5.0, 5.0, 10.0), "PLC Error: at point (5,5,10)"))
    (defect,) = mesh_probe.mesh_defects(shape, diagonal_mm=17.3, budget_s=60.0)
    assert defect.code is DefectCode.mesh_edge_through_face
    assert defect.location["bbox_min"][2] == defect.location["bbox_max"][2] == 10.0
    assert defect.fingerprint


@pytest.mark.parametrize("failure", [
    mesh_probe.MeshFailure("other", (999,), None, "gmsh said something"),
    mesh_probe.MeshFailure("no_volume", (1,), None, "gmsh produced no volume elements"),
    mesh_probe.MeshFailure("timeout", (), None, "ran past its budget"),
    # No point, and the surface came from the log's last 2D surface - a guess, never a target.
    mesh_probe.MeshFailure(EDGE_THROUGH_FACE, (6,), None, "PLC Error: at point"),
    # A surface read off the log while the VOLUME was meshing is the last 2D surface: a guess.
    mesh_probe.MeshFailure("other", (1,), None, "volume step failed", stage="3d",
                           surfaces_from_log=True),
])
def test_unmappable_failure_stays_unlocated(monkeypatch, failure):
    _fake_probe(monkeypatch, failure)
    (defect,) = mesh_probe.mesh_defects(_box(), diagonal_mm=17.3, budget_s=60.0)
    assert defect.code is mesh_probe.CODE_FOR_KIND[failure.kind]
    assert defect.entity_index == 0
    assert defect.location == {}
    assert defect.fingerprint == ""
    assert defect.message == failure.message


def test_a_box_that_meshes_has_no_mesh_defects():
    assert mesh_probe.mesh_defects(_box(), diagonal_mm=17.3, budget_s=60.0) == ()


def test_log_surface_of_a_2d_failure_is_located(monkeypatch):
    _fake_probe(monkeypatch, mesh_probe.MeshFailure(
        OPEN_LOOP, (1,), None, "not a closed loop", stage="2d", surfaces_from_log=True))
    (defect,) = mesh_probe.mesh_defects(_box(), diagonal_mm=17.3, budget_s=60.0)
    assert defect.entity_type == "face"
    assert defect.entity_index >= 1
    assert defect.fingerprint


def test_locating_past_the_deadline_leaves_the_failure_unlocated(monkeypatch):
    from meshpipeline.cad.repair.contracts import DefectCode

    _fake_probe(monkeypatch, mesh_probe.MeshFailure(
        EDGE_THROUGH_FACE, (), (5.0, 5.0, 10.0), "PLC Error"), delay_s=1.5)
    (defect,) = mesh_probe.mesh_defects(_box(), diagonal_mm=17.3, budget_s=1.0)
    assert defect.code is DefectCode.mesh_edge_through_face
    assert defect.entity_index == 0
    assert defect.fingerprint == ""


def test_nearest_face_search_measures_few_faces(monkeypatch):
    """The box-bounded search: a point on one box of a far-apart pair measures that box only."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    measured = []

    class Counting(BRepExtrema_DistShapeShape):
        def __init__(self, *args):
            measured.append(1)
            super().__init__(*args)

    import OCP.BRepExtrema
    monkeypatch.setattr(OCP.BRepExtrema, "BRepExtrema_DistShapeShape", Counting)
    shape = _compound(_box(), _box(x=100.0))
    faces = _faces(shape)
    near = mesh_probe._nearest_faces(faces, (5.0, 5.0, 10.0), tol=1e-4,
                                     deadline=time.monotonic() + 60.0)
    assert near is not None and len(near) == 1
    assert len(measured) < faces.Extent() == 12


def test_scoped_defect_outside_the_region_is_dropped_not_unlocated(monkeypatch):
    from meshpipeline.cad.repair.localize import _diagonal
    from meshpipeline.cad.repair.regions import Region

    shape = _compound(_box(), _box(x=100.0))
    _fake_probe(monkeypatch, mesh_probe.MeshFailure(
        EDGE_THROUGH_FACE, (), (105.0, 5.0, 10.0), "PLC Error", stage="3d"))
    within = Region(index=0, fingerprints=frozenset(), bbox_min=(-1.0, -1.0, -1.0),
                    bbox_max=(11.0, 11.0, 11.0))
    assert mesh_probe.mesh_defects(shape, diagonal_mm=_diagonal(shape), budget_s=60.0,
                                   within=within) == ()


def test_nearest_face_search_stops_at_the_deadline():
    faces = _faces(_box())
    assert mesh_probe._nearest_faces(faces, (5.0, 5.0, 10.0), tol=1e-4,
                                     deadline=time.monotonic() - 1.0) is None
