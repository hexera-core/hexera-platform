# Responsibility: Verify on the REAL kernel and the REAL gmsh probe that resolve_sub_mesh_gap turns
#                 a thin-walled tube the default probe refuses (its outer cylinder's triangles pass
#                 through the inner one, 0.02 mm away) into a STEP the default probe meshes, with
#                 one valid solid, unchanged surface, declarations the caps accept, and that it
#                 declines a part with no sub-mesh gap and a gap between flat faces.
# Boundaries: the native tier. The claim, the consent flag and the rules baseline are unit-tested
#             hermetically (tests/unit/cad/test_repair_resolve_sub_mesh_gap.py).
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")
pytest.importorskip("gmsh", reason="the mesh probe is a native-tier dependency")

import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair import caps, mesh_probe  # noqa: E402
from meshpipeline.cad.repair.gap_split import ResolveSubMeshGap  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for  # noqa: E402
from meshpipeline.cad.repair.localize import _diagonal  # noqa: E402
from meshpipeline.cad.repair.operations import (  # noqa: E402
    AREA_DELTA,
    VOLUME_DELTA,
    box_of,
    sub_shape,
)

#: A 50 mm tube, 20 mm tall, with a 0.02 mm wall. The probe's mesh size is diagonal / 40 = 3.57 mm,
#: whose chord sags 3.57^2 / (8 * 50) = 0.032 mm inside the outer cylinder - wider than the wall.
RADIUS_MM, WALL_MM, HEIGHT_MM = 50.0, 0.02, 20.0


@pytest.fixture
def gap_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", True)


def _tube(wall_mm: float = WALL_MM):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(RADIUS_MM, HEIGHT_MM).Shape(),
                           BRepPrimAPI_MakeCylinder(RADIUS_MM - wall_mm, HEIGHT_MM).Shape()).Shape()


def _ref(shape, index: int) -> EntityRef:
    quantum = quantum_for(_diagonal(shape))
    return EntityRef(entity_type="face", entity_index=index,
                     fingerprint=fingerprint_of("face", box_of(sub_shape(shape, "face", index)),
                                                quantum=quantum))


def _meshes(shape, tmp_path, diagonal: float) -> tuple[bool, str]:
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    path = tmp_path / "part.step"
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(path))
    result = mesh_probe.probe_file(path, diagonal_mm=diagonal, budget_s=120)
    return bool(result.meshed), str(getattr(result.failure, "kind", "") or "")


def test_the_thin_tube_is_refused_as_a_gap_and_meshes_after_the_split(gap_on, tmp_path):
    tube = _tube()
    diagonal = _diagonal(tube)
    assert _meshes(tube, tmp_path, diagonal) == (False, mesh_probe.EDGE_THROUGH_FACE)
    defects = mesh_probe.mesh_defects(tube, diagonal_mm=diagonal, budget_s=120)
    located = [d for d in defects if d.measurements.get("located_by") == mesh_probe.LOCATED_BY_CLASH]
    assert located, defects
    assert located[0].measurements["clash_gap_mm"] == pytest.approx(WALL_MM, rel=1e-6)

    outcome = ResolveSubMeshGap().apply(tube, (_ref(tube, located[0].entity_index),),
                                        tolerance_mm=1e-4)
    evidence = outcome.evidence
    assert "declined" not in evidence, evidence
    assert evidence["gap_mm"] == pytest.approx(WALL_MM, rel=1e-6)
    assert evidence["faces_split"] == 2 and evidence["faces_added"] > 0
    before, after = caps.measure(tube), caps.measure(outcome.shape)
    assert after.valid and after.solids == before.solids == 1
    assert after.faces == before.faces + evidence["faces_added"]
    # THE SURFACE DID NOT MOVE: what moved is integration noise, and it is declared.
    assert abs(after.surface_area_mm2 - before.surface_area_mm2) < 1e-6 * before.surface_area_mm2
    assert abs(after.volume_mm3 - before.volume_mm3) < 1e-6 * before.volume_mm3
    assert caps.cumulative_refusal(before, after, declared_area_mm2=evidence[AREA_DELTA],
                                   declared_volume_mm3=evidence[VOLUME_DELTA]) == ""
    assert caps.sanity_refusal(before, after) == ""
    assert outcome.added and outcome.rebuilt
    # THE OUTCOME THAT COUNTS: the delivered STEP meshes at the probe's default size.
    assert _meshes(outcome.shape, tmp_path, diagonal) == (True, "")


def test_a_part_with_no_sub_mesh_gap_is_declined(gap_on):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    outcome = ResolveSubMeshGap().apply(box, (_ref(box, 1),), tolerance_mm=1e-4)
    assert outcome.evidence["declined"].startswith("no_sub_mesh_gap")
    assert outcome.shape is box


def test_a_gap_between_flat_faces_is_declined_flat(gap_on):
    """A 0.01 mm slot in a block: its two walls are a sub-mesh gap, but a plane does not sag, so a
    split would add faces and resolve nothing."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    block = BRepPrimAPI_MakeBox(40.0, 40.0, 10.0).Shape()
    slot = BRepPrimAPI_MakeBox(gp_Pnt(20.0, -1.0, 2.0), gp_Pnt(20.01, 41.0, 11.0)).Shape()
    part = BRepAlgoAPI_Cut(block, slot).Shape()
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    faces = _indexed(part, TopAbs_FACE)
    wall = next(i for i in range(1, faces.Extent() + 1)
                if abs(box_of(faces.FindKey(i))["bbox_min"][0] - 20.0) < 1e-6
                and abs(box_of(faces.FindKey(i))["bbox_max"][0] - 20.0) < 1e-6)
    outcome = ResolveSubMeshGap().apply(part, (_ref(part, wall),), tolerance_mm=1e-4)
    assert outcome.evidence["declined"].startswith("flat"), outcome.evidence
    assert outcome.evidence["gap_mm"] == pytest.approx(0.01, rel=1e-6)


def test_the_flag_off_declines_without_touching_the_part(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    tube = _tube()
    outcome = ResolveSubMeshGap().apply(tube, (_ref(tube, 1),), tolerance_mm=1e-4)
    assert outcome.evidence["declined"].startswith("resolving_disabled")
    assert outcome.shape is tube
