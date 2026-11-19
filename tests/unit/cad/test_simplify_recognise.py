# Responsibility: Verify CAD simplification inspection finds the features a part was built with - a through hole, edge fillets, the round ends of a slot - classifies each, and never offers a slot end for removal.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("OCP")


def _plate_with_features():
    """100 x 50 x 20 block: one through hole (r 5), its four vertical edges filleted (r 2), and a
    through slot 20 long with r 4 round ends - built, so what should be found is known."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    block = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(100, 50, 20)).Shape()
    fil = BRepFilletAPI_MakeFillet(block)
    e = TopExp_Explorer(block, TopAbs_EDGE)
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp
    while e.More():
        edge = TopoDS.Edge_s(e.Current())
        a, b = TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge)
        pa, pb = BRep_Tool.Pnt_s(a), BRep_Tool.Pnt_s(b)
        if abs(pa.X() - pb.X()) < 1e-9 and abs(pa.Y() - pb.Y()) < 1e-9:   # a vertical edge
            fil.Add(2.0, edge)
        e.Next()
    shape = fil.Shape()
    z = gp_Dir(0, 0, 1)
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(25, 25, -1), z), 5.0, 22.0).Shape()
    shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    slot = BRepPrimAPI_MakeBox(gp_Pnt(60, 21, -1), gp_Pnt(80, 29, 21)).Shape()
    for x in (60.0, 80.0):
        slot = BRepAlgoAPI_Fuse(slot, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 25, -1), z), 4.0, 22.0).Shape()).Shape()
    return BRepAlgoAPI_Cut(shape, slot).Shape()


def test_finds_the_hole_the_fillets_and_the_slot_ends():
    from meshpipeline.cad.simplify.contracts import FeatureKind
    from meshpipeline.cad.simplify.recognise import recognise

    found, diag = recognise(_plate_with_features())
    by_kind: dict = {}
    for c in found:
        by_kind.setdefault(c.kind, []).append(c)

    holes = by_kind.get(FeatureKind.hole, [])
    assert len(holes) == 1, [c.to_dict() for c in found]
    assert holes[0].radius == pytest.approx(5.0)
    assert holes[0].concave
    assert holes[0].centroid[0] == pytest.approx(25.0, abs=1e-6)

    blends = by_kind.get(FeatureKind.blend, [])
    assert len(blends) == 4                     # the four vertical edge fillets, none touching another
    assert all(not b.concave and b.radius == pytest.approx(2.0) for b in blends)

    ends = by_kind.get(FeatureKind.slot_end, [])
    assert len(ends) == 2
    # MEASURED ON THE NIST CORPUS: OpenCASCADE's defeaturing hands a slot end back unchanged
    # while reporting success, so offering one for removal would offer a no-op as a saving
    assert not any(e.removable for e in ends)
    assert diag == pytest.approx((100**2 + 50**2 + 20**2) ** 0.5, rel=1e-3)


def test_a_smaller_feature_is_priced_finer():
    from meshpipeline.cad.simplify.recognise import feature_size, surface_cells

    # PLAN-fea-4: 6 elements per quarter turn, so the edge scales with the radius
    assert feature_size(2.0) == pytest.approx(feature_size(4.0) / 2)
    assert surface_cells(100.0, feature_size(2.0)) > surface_cells(100.0, feature_size(4.0))
    assert surface_cells(100.0, 0.0) == 0


def test_inspect_reads_a_step_file_and_changes_nothing(tmp_path):
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.cad.simplify.contracts import SimplifyMode, SimplifyStatus
    from meshpipeline.cad.simplify.inspect import inspect_features
    from meshpipeline.contracts.geometry_source import MaterializedGeometry

    step = tmp_path / "plate.step"
    w = STEPControl_Writer()
    w.Transfer(_plate_with_features(), STEPControl_AsIs)
    w.Write(str(step))
    before = step.read_bytes()
    ref = source_ref(local_file=step, filename="plate.step")
    geometry = MaterializedGeometry(ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id),
                                    local_path=step)

    result = inspect_features(geometry)

    assert result.status is SimplifyStatus.candidates
    assert result.policy.mode is SimplifyMode.inspect
    assert result.output is None
    assert step.read_bytes() == before
    payload = result.to_dict()
    assert payload["report"]["operations"] == [{"name": "inspect", "mutated": False}]
    assert {c["kind"] for c in payload["report"]["candidates"]} == {"hole", "blend", "slot_end"}
    assert "Nothing was changed" in payload["report"]["summary"]


def test_a_triangle_surface_is_inconclusive_not_clean(tmp_path):
    from tests._geometry_support import materialized

    from meshpipeline.cad.simplify.contracts import SimplifyStatus
    from meshpipeline.cad.simplify.inspect import inspect_features

    geometry = materialized(tmp_path, filename="part.stl")
    assert Path(geometry.path).suffix == ".stl"

    result = inspect_features(geometry)

    assert result.status is SimplifyStatus.inconclusive
    assert result.report.diagnostics["not_inspected"] == "not_a_cad_solid"
