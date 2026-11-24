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

from meshpipeline.cad.repair_v4.ops import OpDeclined  # noqa: E402
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


def _worst_deviation(shape, samples: int = 41, only_edge: int | None = None) -> float:
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
    for n, e in enumerate(_edges(shape), 1):
        if BRep_Tool.Degenerated_s(e) or (only_edge is not None and n != only_edge):
            continue
        c = BRepAdaptor_Curve(e)
        for f in anc.FindFromKey(e):
            face = TopoDS.Face_s(f)
            pc, s = BRepAdaptor_Curve2d(e, face), BRepAdaptor_Surface(face)
            for i in range(samples):
                t = c.FirstParameter() + (c.LastParameter() - c.FirstParameter()) * i / (samples - 1)
                uv = pc.Value(t)
                worst = max(worst, c.Value(t).Distance(s.Value(uv.X(), uv.Y())))
    return worst


def _same_parameter_everywhere(shape) -> bool:
    """The flag is only a claim, so it is not enough: every edge's independently measured
    pcurve/3D deviation (401 samples) must also be inside its own tolerance."""
    from OCP.BRep import BRep_Tool

    for n, e in enumerate(_edges(shape), 1):
        if BRep_Tool.Degenerated_s(e):
            continue
        if not BRep_Tool.SameParameter_s(e):
            return False
        if _worst_deviation(shape, 401, only_edge=n) > BRep_Tool.Tolerance_s(e) + 1e-12:
            return False
    return True


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
    assert d2.modified == {}   # the rebuild already settled the edge; tighten must change no entity
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


def _box_with_off_surface_edge(tmp_path: Path, shift: float, claimed: float = 0.02) -> tuple[str, int]:
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
        edge, Geom_Line(gp_Pnt(a.X() + shift, a.Y() + shift, a.Z() + shift), d), TopLoc_Location(), claimed)
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


def test_rebuild_edge_never_refuses_on_how_far_it_moves_geometry(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    """B12: moving an edge 0.5 mm (bound 0.173 mm) is no longer refused; the move is measured and reported."""
    path, eindex = _box_with_off_surface_edge(tmp_path, 0.5)         # bound is 0.001 x 173 = 0.173
    ws0 = Workspace.from_brep(pool, path)
    try:
        _, diff = RebuildEdgeAsIntersection().apply(pool, ws0, [_id_of(ws0, "E", eindex)], {}, timeout_s=300)
    except OpDeclined as why:
        assert "over_bound" not in why.reason and "fidelity" not in why.reason, why.reason
        return
    assert "max_move" in str(diff.params) and "fidelity_bound" in str(diff.params), diff.params


def test_target_rel_outside_its_bounds_is_refused(pool: WorkerPool, tmp_path: Path) -> None:
    path, _ = _inflated_box(tmp_path)
    ws = Workspace.from_brep(pool, path)
    with pytest.raises(OpDeclined, match="^invalid_params: target_rel"):
        TightenTolerances().apply(pool, ws, [], {"target_rel": 1e-2}, timeout_s=300)


def _broken_sphere(radius: float = 50.0):
    """A sphere solid whose wire lost both degenerate pole edges (asserted invalid)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere
    from OCP.TopAbs import TopAbs_FACE, TopAbs_WIRE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Iterator

    sphere = BRepBuilderAPI_Copy(BRepPrimAPI_MakeSphere(radius).Shape(), True).Shape()
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
    return sphere


def _compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for sh in shapes:
        builder.Add(comp, sh)
    return comp


def _sphere_missing_poles(tmp_path: Path) -> str:
    return _write(_broken_sphere(), tmp_path / "sphere.brep")


def _invalid_faces(shape) -> int:
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    an, n, ex = BRepCheck_Analyzer(shape), 0, TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        n += 0 if an.IsValid(ex.Current()) else 1
        ex.Next()
    return n


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


def _half_cylinder_with_arc_off_surface(tmp_path: Path, off: float) -> tuple[str, int]:
    """A half cylinder (r=20, h=50). Its first circular edge (plane x cylinder) gets a concentric
    circle `off` larger as 3D curve - a REAL mismatch on a CURVED edge. Returns (path, edge index)."""
    import math

    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.Geom import Geom_Circle
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt
    from OCP.TopLoc import TopLoc_Location

    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 20.0, 50.0, math.pi).Shape()
    for n, e in enumerate(_edges(shape), 1):
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_Circle:
            circ = c.Circle()
            BRep_Builder().UpdateEdge(
                e, Geom_Circle(gp_Circ(circ.Position(), circ.Radius() + off)), TopLoc_Location(), 0.02)
            return _write(shape, tmp_path / "arc.brep"), n
    raise AssertionError("no circular edge")


def test_curved_edge_tolerance_covers_the_dense_independent_deviation(pool: WorkerPool, tmp_path: Path) -> None:
    """On a circle the 513-sample + refinement measure must not sit below the true gap: check
    against a 20001-sample independent measurement, and that BRepCheck accepts the result."""
    path, eindex = _half_cylinder_with_arc_off_surface(tmp_path, 0.007)
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws1.brep_path)
    from OCP.BRep import BRep_Tool

    tol = BRep_Tool.Tolerance_s(_edges(after)[eindex - 1])
    truth = _worst_deviation(after, 20001, only_edge=eindex)
    assert truth > 5e-3 and tol >= truth - 1e-12        # not below the real gap
    assert eindex in diff.params["over_target"]         # and reported, not clamped
    assert _valid(after)


def test_rebuild_restores_a_curved_edge_from_the_plane_cylinder_intersection(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    path, eindex = _half_cylinder_with_arc_off_surface(tmp_path, 0.007)
    ws0 = Workspace.from_brep(pool, path)
    edge_id = _id_of(ws0, "E", eindex)
    ws1, d1 = RebuildEdgeAsIntersection().apply(pool, ws0, [edge_id], {}, timeout_s=300)
    ws2, d2 = TightenTolerances().apply(pool, ws1, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws2.brep_path)
    assert d1.params["max_move"] == pytest.approx(0.007, rel=1e-3)
    assert _worst_deviation(after, 20001, only_edge=eindex) < 1e-6
    assert _max_edge_tolerance_ok(after) and _valid(after) and _same_parameter_everywhere(after)
    assert d2.params["over_target"] == [] and set(ws2.ids) == set(ws0.ids) and edge_id in d1.modified


def _max_edge_tolerance_ok(shape) -> bool:
    return _max_edge_tol(shape) <= 1e-5 * _diag(shape)


def test_rebuild_replaces_a_wrong_pcurve(pool: WorkerPool, tmp_path: Path) -> None:
    """The 3D curve is right but one pcurve is 0.3 off in UV: the stale pcurve must be dropped and
    re-derived, not kept."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Geom2d import Geom2d_Line
    from OCP.gp import gp_Dir2d, gp_Pnt2d
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    box = BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape()
    edge = _edges(box)[0]
    anc = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(box, TopAbs_EDGE, TopAbs_FACE, anc)
    face = TopoDS.Face_s(next(iter(anc.FindFromKey(edge))))
    pc = BRepAdaptor_Curve2d(edge, face)
    a, b = pc.Value(pc.FirstParameter()), pc.Value(pc.LastParameter())
    dx, dy = b.X() - a.X(), b.Y() - a.Y()
    n = (dx * dx + dy * dy) ** 0.5
    wrong = Geom2d_Line(gp_Pnt2d(a.X() - 0.3 * dy / n, a.Y() + 0.3 * dx / n), gp_Dir2d(dx, dy))
    BRep_Builder().UpdateEdge(edge, wrong, face, 0.5)
    path = _write(box, tmp_path / "wrongpc.brep")
    assert _worst_deviation(_read(path)) > 0.25 and BRep_Tool.Tolerance_s(_edges(_read(path))[0]) == 0.5

    ws0 = Workspace.from_brep(pool, path)
    ws1, _ = RebuildEdgeAsIntersection().apply(pool, ws0, [_id_of(ws0, "E", 1)], {}, timeout_s=300)
    after = _read(ws1.brep_path)
    assert _worst_deviation(after, 401) < 1e-6
    ws2, d2 = TightenTolerances().apply(pool, ws1, [], {"target_rel": 1e-5}, timeout_s=300)
    after = _read(ws2.brep_path)
    assert _max_edge_tolerance_ok(after) and _valid(after) and _same_parameter_everywhere(after)


def test_tighten_lowers_face_tolerances_and_reports_them(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.ShapeFix import ShapeFix_ShapeTolerance
    from OCP.TopAbs import TopAbs_FACE

    box = BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape()
    ShapeFix_ShapeTolerance().SetTolerance(box, INFLATED, TopAbs_FACE)
    path = _write(box, tmp_path / "facetol.brep")
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    ex, tols = TopExp_Explorer(_read(ws1.brep_path), TopAbs_FACE), []
    while ex.More():
        tols.append(BRep_Tool.Tolerance_s(TopoDS.Face_s(ex.Current())))
        ex.Next()
    assert len(tols) == 6 and max(tols) <= 1e-5 * _diag(box)
    assert len([s for s in diff.modified if s.startswith("F")]) == 6 and diff.params["faces_over_target"] == []


def test_tighten_does_not_fail_on_an_untouched_edge_that_under_reports(pool: WorkerPool, tmp_path: Path) -> None:
    """Edge claims 1e-7 but is really 0.01 off: it is already 'tighter than target', so this op does
    not touch or judge it (and must not fail on it)."""
    path, _ = _box_with_off_surface_edge(tmp_path, 0.007, claimed=1e-7)
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    assert diff.modified == {} and ws1.ids == ws0.ids
    ((index, dev),) = diff.params["under_reported"]      # a blind spot is not silent: it is listed
    assert index == 1 and dev > 5e-3


def test_a_raised_tolerance_is_reported_as_a_modification(pool: WorkerPool, tmp_path: Path) -> None:
    path, eindex = _box_with_off_surface_edge(tmp_path, 0.05, claimed=0.02)   # real gap ~0.07 > claim
    ws0 = Workspace.from_brep(pool, path)
    ws1, diff = TightenTolerances().apply(pool, ws0, [], {"target_rel": 1e-5}, timeout_s=300)
    from OCP.BRep import BRep_Tool

    tol = BRep_Tool.Tolerance_s(_edges(_read(ws1.brep_path))[eindex - 1])
    assert tol > 0.02 and tol >= _worst_deviation(_read(ws1.brep_path), 401, only_edge=eindex) - 1e-12
    assert _id_of(ws0, "E", eindex) in diff.modified


def test_fix_poles_fails_loudly_when_it_changes_nothing(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    path = _write(BRepPrimAPI_MakeSphere(50.0).Shape(), tmp_path / "good_sphere.brep")
    ws0 = Workspace.from_brep(pool, path)
    (face_id,) = [s for s in ws0.ids if s.startswith("F")]
    with pytest.raises(OpDeclined, match="^no_change: ShapeFix_Face.Perform changed nothing"):
        FixPoles().apply(pool, ws0, [face_id], {}, timeout_s=300)


def test_fix_poles_works_when_the_rest_of_the_shape_is_still_invalid(pool: WorkerPool, tmp_path: Path) -> None:
    """The usual repair input is invalid somewhere else: fixing one face must not demand that the
    whole shape be valid."""
    path = _write(_compound(_broken_sphere(50.0), _translated(_broken_sphere(20.0), 200.0)), tmp_path / "two.brep")
    ws0 = Workspace.from_brep(pool, path)
    first = sorted((s for s in ws0.ids if s.startswith("F")), key=lambda s: int(s[1:]))[0]
    assert _invalid_faces(_read(path)) == 2
    ws1, _ = FixPoles().apply(pool, ws0, [first], {}, timeout_s=300)
    after = _read(ws1.brep_path)
    assert not _valid(after) and _invalid_faces(after) == 1   # one fixed, the other left alone


def _translated(shape, dx: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    t = gp_Trsf()
    t.SetTranslation(gp_Vec(dx, 0, 0))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def test_fix_poles_refuses_when_only_some_targeted_faces_changed(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    good = _translated(BRepPrimAPI_MakeSphere(20.0).Shape(), 200.0)
    path = _write(_compound(_broken_sphere(50.0), good), tmp_path / "mixed.brep")
    ws0 = Workspace.from_brep(pool, path)
    faces = [s for s in ws0.ids if s.startswith("F")]
    assert len(faces) == 2
    with pytest.raises(OpDeclined, match="^no_change: .*changed nothing on face"):
        FixPoles().apply(pool, ws0, faces, {}, timeout_s=300)


def test_the_no_new_invalid_gate_can_reject_and_works_on_invalid_inputs() -> None:
    """The in-context gate must be able to fail: a valid sphere turned into the broken one is
    rejected, while the same broken shape in and out (invalid input) is not."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    from meshpipeline.cad.repair_v4.ops import edges as ops_edges

    good, broken = BRepPrimAPI_MakeSphere(50.0).Shape(), _broken_sphere()
    with pytest.raises(ops_edges.KernelDeclined, match="^invalidated: .*accepted before"):
        ops_edges._assert_no_new_invalid("x", good, broken, same_indices=False)
    ops_edges._assert_no_new_invalid("x", broken, _broken_sphere(), same_indices=False)   # no raise
    ops_edges._assert_no_new_invalid("x", broken, broken, same_indices=True)              # no raise
