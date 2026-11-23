# Responsibility: Prove on the REAL kernel that the v4 edge/tolerance ops lower tolerances to what the
#                 geometry earns (never relabel), keep ids, stay inside the fidelity bound, and that
#                 fix_poles restores a sphere's missing degenerate edges.
# Boundaries: native tier. The brief's "cheap verdict improves" is NOT asserted here because
#             `cheap_verdict` (Tasks 2-5) is not in this branch; the stand-ins are the measured max
#             edge tolerance, BRepCheck_Analyzer validity, SameParameter and a pcurve/3D deviation
#             measured independently below. Tighten these to the verdict once it lands.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops.edges import (  # noqa: E402
    OPS,
    FixPoles,
    RebuildEdgeAsIntersection,
    TightenTolerances,
)
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

INFLATED = 1e-2
BOX = 100.0


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerPool]:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _read(path: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _write(shape, path: Path) -> str:
    from OCP.BRepTools import BRepTools

    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _edges(shape) -> list:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, m)
    return [TopoDS.Edge_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _diag(shape) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def _max_edge_tol(shape) -> float:
    from OCP.BRep import BRep_Tool

    return max(BRep_Tool.Tolerance_s(e) for e in _edges(shape))


def _valid(shape) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def _worst_deviation(shape) -> float:
    """Independent of the op's own measurement: 3D curve vs each pcurve lifted onto its face."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, anc)
    worst = 0.0
    for e in _edges(shape):
        if BRep_Tool.Degenerated_s(e):
            continue
        c = BRepAdaptor_Curve(e)
        for f in anc.FindFromKey(e):
            face = TopoDS.Face_s(f)
            pc, s = BRepAdaptor_Curve2d(e, face), BRepAdaptor_Surface(face)
            for i in range(41):
                t = c.FirstParameter() + (c.LastParameter() - c.FirstParameter()) * i / 40
                uv = pc.Value(t)
                worst = max(worst, c.Value(t).Distance(s.Value(uv.X(), uv.Y())))
    return worst


def _same_parameter_everywhere(shape) -> bool:
    from OCP.BRep import BRep_Tool

    return all(BRep_Tool.SameParameter_s(e) for e in _edges(shape) if not BRep_Tool.Degenerated_s(e))


def _inflated_box(tmp_path: Path, which: int = 0) -> tuple[str, int]:
    """A 100 mm box with ONE edge (and its vertices) declared 1e-2 fuzzy. Built with
    ShapeFix_ShapeTolerance::SetTolerance because Task 7's `tolerance_inflate` injector is not in
    this branch. Returns (brep path, 1-based kernel index of the inflated edge)."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.ShapeFix import ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_SHAPE

    box = BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape()
    edge = _edges(box)[which]
    ShapeFix_ShapeTolerance().SetTolerance(edge, INFLATED, TopAbs_SHAPE)
    assert _max_edge_tol(box) == pytest.approx(INFLATED)
    return _write(box, tmp_path / "inflated.brep"), which + 1


def _id_of(ws: Workspace, kind: str, index: int) -> str:
    (sid,) = [s for s, i in ws.indices.items() if i == index and s.startswith(kind)]
    return sid


def test_both_ops_bring_an_inflated_box_edge_inside_1e5_diag(pool: WorkerPool, tmp_path: Path) -> None:
    path, eindex = _inflated_box(tmp_path)
    ws0 = Workspace.from_brep(pool, path)
    edge_id = _id_of(ws0, "E", eindex)
    before = _read(path)
    diag = _diag(before)
    assert _max_edge_tol(before) == pytest.approx(INFLATED) and INFLATED > 1e-5 * diag

    ws1, d1 = RebuildEdgeAsIntersection().apply(pool, ws0, [edge_id], {}, timeout_s=300)
    ws2, d2 = TightenTolerances().apply(pool, ws1, [], {"target_rel": 1e-5}, timeout_s=300)

    after = _read(ws2.brep_path)
    assert _max_edge_tol(after) <= 1e-5 * diag
    assert _valid(after) and _same_parameter_everywhere(after)
    assert _worst_deviation(after) <= _max_edge_tol(after) + 1e-9
    # geometry stayed put: bounding diagonal and volume equal to well inside the fidelity bound
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    vol = GProp_GProps()
    BRepGProp.VolumeProperties_s(after, vol)
    assert vol.Mass() == pytest.approx(BOX ** 3, rel=1e-9)
    assert abs(_diag(after) - diag) <= min(0.1, 1e-3 * diag)
    # identity: nothing was retired; only the inflated edge counts as modified by the rebuild
    assert set(ws2.ids) == set(ws0.ids)
    assert list(d1.modified) == [edge_id] and d1.removed == []
    assert set(d2.modified) <= {edge_id}   # tighten has nothing else to lower
    assert not any(s.startswith("F") for s in d1.modified)


def test_tighten_alone_lowers_tolerance_and_leaves_satisfied_geometry_alone(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    path, eindex = _inflated_box(tmp_path, which=3)
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws1.brep_path)
    assert _max_edge_tol(after) <= 1e-5 * _diag(after)
    assert _valid(after) and _same_parameter_everywhere(after)
    assert list(diff.modified) == [_id_of(ws0, "E", eindex)]
    assert set(ws1.ids) == set(ws0.ids)


def _box_with_off_surface_edge(tmp_path: Path, shift: float) -> tuple[str, int]:
    """A box whose first edge's 3D curve is moved `shift` along (1,1,1) off its faces (tolerance
    claimed 0.02), so the pcurve/3D mismatch is REAL. Returns (path, kernel index of that edge)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Geom import Geom_Line
    from OCP.gp import gp_Dir, gp_Pnt
    from OCP.TopLoc import TopLoc_Location

    box = BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape()
    edge = _edges(box)[0]
    assert not BRep_Tool.Degenerated_s(edge)
    c = BRepAdaptor_Curve(edge)
    a, b = c.Value(c.FirstParameter()), c.Value(c.LastParameter())
    d = gp_Dir(b.X() - a.X(), b.Y() - a.Y(), b.Z() - a.Z())
    BRep_Builder().UpdateEdge(
        edge, Geom_Line(gp_Pnt(a.X() + shift, a.Y() + shift, a.Z() + shift), d), TopLoc_Location(), 0.02)
    return _write(box, tmp_path / "off.brep"), 1


def test_tighten_does_not_clamp_a_deviation_it_cannot_remove(pool: WorkerPool, tmp_path: Path) -> None:
    """An edge whose 3D curve really sits off its faces must keep a tolerance that covers that, and
    be REPORTED as over target, not be relabelled to the target."""
    path, _ = _box_with_off_surface_edge(tmp_path, 0.007)
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws1.brep_path)
    assert _max_edge_tol(after) > 1e-5 * _diag(after)                # not clamped
    assert _worst_deviation(after) <= _max_edge_tol(after) + 1e-9    # honest about the real gap
    assert diff.params["over_target"] == [1]


def test_rebuild_edge_removes_a_real_pcurve_3d_mismatch_so_tightening_can_succeed(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    path, eindex = _box_with_off_surface_edge(tmp_path, 0.007)
    ws0 = Workspace.from_brep(pool, path)
    edge_id = _id_of(ws0, "E", eindex)
    diag = _diag(_read(path))
    assert _worst_deviation(_read(path)) > 5e-3                      # the defect is real before

    ws1, d1 = RebuildEdgeAsIntersection().apply(pool, ws0, [edge_id], {}, timeout_s=300)
    ws2, d2 = TightenTolerances().apply(pool, ws1, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws2.brep_path)
    assert d1.params["max_move"] == pytest.approx(0.007 * 2 ** 0.5, rel=1e-3)   # measured, within bound
    assert _worst_deviation(after) < 1e-6
    assert _max_edge_tol(after) <= 1e-5 * diag and d2.params["over_target"] == []
    assert _valid(after) and _same_parameter_everywhere(after)
    assert set(ws2.ids) == set(ws0.ids) and edge_id in d1.modified


def test_rebuild_edge_refuses_to_move_geometry_beyond_the_fidelity_bound(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    path, eindex = _box_with_off_surface_edge(tmp_path, 0.5)         # bound is 0.001 x 173 = 0.173
    ws0 = Workspace.from_brep(pool, path)
    with pytest.raises(RuntimeError, match="fidelity bound"):
        RebuildEdgeAsIntersection().apply(pool, ws0, [_id_of(ws0, "E", eindex)], {}, timeout_s=300)


def test_target_rel_outside_its_bounds_is_refused(pool: WorkerPool, tmp_path: Path) -> None:
    path, _ = _inflated_box(tmp_path)
    ws = Workspace.from_brep(pool, path)
    with pytest.raises(RuntimeError, match="target_rel"):
        TightenTolerances().apply(pool, ws, [], {"target_rel": 1e-2}, timeout_s=300)


def _sphere_missing_poles(tmp_path: Path) -> str:
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere
    from OCP.TopAbs import TopAbs_FACE, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Iterator

    sphere = BRepBuilderAPI_Copy(BRepPrimAPI_MakeSphere(50.0).Shape(), True).Shape()
    wire = TopoDS.Wire_s(TopExp_Explorer(TopExp_Explorer(sphere, TopAbs_FACE).Current(), TopAbs_WIRE).Current())
    wire.Free(True)
    degenerate = []
    it = TopoDS_Iterator(wire)
    while it.More():
        if BRep_Tool.Degenerated_s(TopoDS.Edge_s(it.Value())):
            degenerate.append(TopoDS.Edge_s(it.Value()))
        it.Next()
    assert len(degenerate) == 2
    for e in degenerate:
        BRep_Builder().Remove(wire, e)
    assert not _valid(sphere), "fixture must actually be broken"
    return _write(sphere, tmp_path / "sphere.brep")


def test_fix_poles_restores_a_sphere_with_missing_degenerate_edges(pool: WorkerPool, tmp_path: Path) -> None:
    ws0 = Workspace.from_brep(pool, _sphere_missing_poles(tmp_path))
    (face_id,) = [s for s in ws0.ids if s.startswith("F")]
    solid_id = next(s for s in ws0.ids if s.startswith("B"))
    ws1, diff = FixPoles().apply(pool, ws0, [face_id], {}, timeout_s=300)

    after = _read(ws1.brep_path)
    assert _valid(after) and _same_parameter_everywhere(after)
    assert len(_edges(after)) == len(_edges(_read(ws0.brep_path))) + 2   # the two poles are back
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    area = GProp_GProps()
    BRepGProp.SurfaceProperties_s(after, area)
    assert area.Mass() == pytest.approx(4 * 3.141592653589793 * 50.0 ** 2, rel=1e-6)
    assert face_id in ws1.ids and solid_id in ws1.ids           # the face keeps its id
    assert "unattributed" not in diff.params
    assert len(diff.generated.get(face_id, [])) == 2            # the new pole edges came from the face


def test_ops_publish_the_protocol_surface() -> None:
    assert [op.name for op in OPS] == ["rebuild_edge_as_intersection", "tighten_tolerances", "fix_poles"]
    schema = TightenTolerances.params_schema["properties"]["target_rel"]
    assert (schema["minimum"], schema["maximum"]) == (1e-7, 1e-4)
