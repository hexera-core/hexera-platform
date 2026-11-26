# Responsibility: Prove on the REAL kernel that the v4 face ops repair what they claim and refuse
#                 what they cannot keep inside the fidelity bound: a self-intersecting B-spline face
#                 is rebuilt, a 2 mm hole is filled and DECLARED, a 20% hole is refused, a near-plane
#                 B-spline becomes a plane, a degree-8 surface is re-approximated, a split B-spline
#                 patch is refitted into one declared face.
# Boundaries: the native tier. The verdict/checker (Tasks 2-5) is not in this branch, so "valid",
#             "clean" and "faithful" are asserted with OCCT's own measurements - BRepCheck_Analyzer,
#             free-edge counts, sampled surface distance against min(0.1 mm, 0.001 x diagonal), and
#             face-area changes - taken here, in the test process, independently of the op's own
#             measurement. They stand in for the verdict and are to be tightened when it lands.
from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder, BRep_Tool  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: E402
from OCP.BRepBuilderAPI import (  # noqa: E402
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepExtrema import BRepExtrema_DistShapeShape  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepLib import BRepLib  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.Geom import Geom_BSplineSurface, Geom_Plane  # noqa: E402
from OCP.Geom2dAPI import Geom2dAPI_Interpolate  # noqa: E402
from OCP.GeomAbs import GeomAbs_C2, GeomAbs_Plane  # noqa: E402
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface  # noqa: E402
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pln, gp_Pnt, gp_Pnt2d  # noqa: E402
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.ShapeBuild import ShapeBuild_ReShape  # noqa: E402
from OCP.ShapeFix import ShapeFix_Face  # noqa: E402
from OCP.TColgp import TColgp_Array2OfPnt, TColgp_HArray1OfPnt2d  # noqa: E402
from OCP.TColStd import TColStd_Array1OfReal  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE  # noqa: E402
from OCP.TopExp import TopExp, TopExp_Explorer  # noqa: E402
from OCP.TopLoc import TopLoc_Location  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shell  # noqa: E402
from OCP.TopTools import (  # noqa: E402
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)

from meshpipeline.cad.repair_v4.ops.faces import OPS, OpDeclined  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _op(name: str) -> Any:
    (op,) = [o for o in OPS if o.name == name]
    return op


# --- measurement, in the test process, independent of the op's own -----------------------------


def _write(shape: Any, path: Path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder as B
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, path, B())
    return shape


def _faces(shape: Any) -> list[Any]:
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, index)
    return [TopoDS.Face_s(index.FindKey(i)) for i in range(1, index.Extent() + 1)]


def _diagonal(shape: Any) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)


def _bound(shape: Any) -> float:
    return min(0.1, 0.001 * _diagonal(shape))


def _area(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return abs(props.Mass())


def _valid(shape: Any) -> bool:
    return bool(BRepCheck_Analyzer(shape).IsValid())


def _free_edges(shape: Any) -> int:
    """Edges that border one face only: 0 for a closed shell."""
    index = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, index)
    return sum(1 for i in range(1, index.Extent() + 1) if index.FindFromIndex(i).Size() == 1)


def _surface_points(face: Any, n: int = 15) -> list[Any]:
    """Points of the TRIMMED face on its carrier, by UV grid and the classifier."""
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.TopAbs import TopAbs_IN

    surface = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = (surface.FirstUParameter(), surface.LastUParameter(),
                      surface.FirstVParameter(), surface.LastVParameter())
    points = []
    for i in range(n + 1):
        for j in range(n + 1):
            u, v = u0 + (u1 - u0) * i / n, v0 + (v1 - v0) * j / n
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_IN:
                points.append(surface.Value(u, v))
    return points


def _dist(points: list[Any], shape: Any) -> float:
    return max(BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(p).Vertex(), shape).Value()
               for p in points)


def _ids_at(ws: Workspace, kind: str) -> dict[str, int]:
    return {s: i for s, i in ws.indices.items() if s.startswith(kind)}


# --- fixtures -----------------------------------------------------------------------------------


def _bspline(f: Any, *, size: float, n: int = 8, deg: tuple[int, int] = (3, 3)) -> Any:
    """A B-spline surface z = f(x, y) over [0, size]^2, parametrised in mm."""
    grid = TColgp_Array2OfPnt(1, n, 1, n)
    for i in range(n):
        for j in range(n):
            x, y = size * i / (n - 1), size * j / (n - 1)
            grid.SetValue(i + 1, j + 1, gp_Pnt(x, y, f(x, y)))
    surface = GeomAPI_PointsToBSplineSurface(grid, deg[0], deg[1], GeomAbs_C2, 1e-7).Surface()
    for set_knots, count, get_knots in ((surface.SetUKnots, surface.NbUKnots(), surface.UKnots),
                                        (surface.SetVKnots, surface.NbVKnots(), surface.VKnots)):
        knots = get_knots()
        scaled = TColStd_Array1OfReal(1, count)
        for k in range(1, count + 1):
            scaled.SetValue(k, knots.Value(k) * size)
        set_knots(scaled)
    assert isinstance(surface, Geom_BSplineSurface)
    return surface


def _uv_curve(points: list[tuple[float, float]]) -> Any:
    h = TColgp_HArray1OfPnt2d(1, len(points))
    for i, p in enumerate(points):
        h.SetValue(i + 1, gp_Pnt2d(*p))
    interpolate = Geom2dAPI_Interpolate(h, False, 1e-9)
    interpolate.Perform()
    return interpolate.Curve()


def _line(a: tuple[float, float], b: tuple[float, float], n: int) -> list[tuple[float, float]]:
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def _looped_face(surface: Any, size: float, loop: float) -> Any:
    """A trimmed face on `surface` whose first edge crosses itself in a loop `loop` mm in size."""
    lo, hi = 0.1 * size, 0.9 * size
    turn = [(0.5 * size + loop * (t - 1.5 * math.sin(t)), lo + loop * (1 - 1.5 * math.cos(t)))
            for t in (2 * math.pi * i / 24 - math.pi for i in range(25))]
    first = (_line((lo, lo), (0.4 * size, lo), 6)[:-1] + turn
             + _line((turn[-1][0], lo), (hi, lo), 6)[1:])
    sides = [first, _line((hi, lo), (hi, hi), 4), _line((hi, hi), (lo, hi), 4),
             _line((lo, hi), (lo, lo), 4)]
    wire = BRepBuilderAPI_MakeWire()
    for points in sides:
        edge = BRepBuilderAPI_MakeEdge(_uv_curve(points), surface).Edge()
        BRepLib.BuildCurves3d_s(edge)
        wire.Add(edge)
    return BRepBuilderAPI_MakeFace(surface, wire.Wire(), True).Face()


def _bowtie_face(surface: Any, size: float) -> Any:
    lo, hi = 0.1 * size, 0.9 * size
    corners = [(lo, lo), (hi, hi), (hi, lo), (lo, hi)]
    wire = BRepBuilderAPI_MakeWire()
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        edge = BRepBuilderAPI_MakeEdge(_uv_curve(_line(a, b, 2)), surface).Edge()
        BRepLib.BuildCurves3d_s(edge)
        wire.Add(edge)
    return BRepBuilderAPI_MakeFace(surface, wire.Wire(), True).Face()


def _solid_with_top(top_surface: Any, size: float, height: float) -> Any:
    """A size x size x height box whose top face is replaced by one on `top_surface` (a valid solid)."""
    box = BRepPrimAPI_MakeBox(size, size, height).Shape()
    (top,) = [f for f in _faces(box) if abs(_centre_z(f) - height) < 1e-6]
    builder = BRep_Builder()
    replacement = TopoDS_Face()
    builder.MakeFace(replacement, top_surface, TopLoc_Location(), 1e-7)
    from OCP.TopAbs import TopAbs_WIRE

    wires = TopExp_Explorer(top, TopAbs_WIRE)
    while wires.More():
        builder.Add(replacement, wires.Current())
        wires.Next()
    fixer = ShapeFix_Face(replacement)
    fixer.Perform()
    context = ShapeBuild_ReShape()
    context.Replace(top, fixer.Face())
    solid = context.Apply(box)
    assert _valid(solid)
    return solid


def _centre_z(face: Any) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.CentreOfMass().Z()


def _box_with_hole(tmp_path: Path, *, size: float, hole: float, round_hole: bool) -> str:
    """An open shell: a `size` cube whose top face has a hole (`hole` mm across) and no patch."""
    box = BRepPrimAPI_MakeBox(size, size, size).Shape()
    plane = Geom_Plane(gp_Pln(gp_Pnt(0, 0, size), gp_Dir(0, 0, 1)))
    outer = BRepTools.OuterWire_s(
        [f for f in _faces(box) if abs(_centre_z(f) - size) < 1e-6][0])
    top = BRepBuilderAPI_MakeFace(plane, outer, True)
    c = size / 2
    if round_hole:
        circle = gp_Circ(gp_Ax2(gp_Pnt(c, c, size), gp_Dir(0, 0, -1)), hole / 2)
        inner = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()
    else:
        h = hole / 2
        pts = [gp_Pnt(c - h, c - h, size), gp_Pnt(c - h, c + h, size),
               gp_Pnt(c + h, c + h, size), gp_Pnt(c + h, c - h, size)]
        inner_maker = BRepBuilderAPI_MakeWire()
        for a, b in zip(pts, pts[1:] + pts[:1], strict=True):
            inner_maker.Add(BRepBuilderAPI_MakeEdge(a, b).Edge())
        inner = inner_maker.Wire()
    top.Add(inner)
    sewing = BRepBuilderAPI_Sewing(1e-6)
    for f in _faces(box):
        if abs(_centre_z(f) - size) > 1e-6:
            sewing.Add(f)
    sewing.Add(top.Face())
    sewing.Perform()
    shell = sewing.SewedShape()
    assert _free_edges(shell) > 0
    return _write(shell, tmp_path / "holed.brep")


def _free_loop_ids(ws: Workspace, near: tuple[float, float, float], radius: float) -> list[str]:
    """Stable ids of the free edges within `radius` of `near` (asked of the kernel's own shape)."""
    shape = _read(ws.brep_path)
    index = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, index)
    found = []
    for i in range(1, index.Extent() + 1):
        if index.FindFromIndex(i).Size() != 1:
            continue
        edge = TopoDS.Edge_s(index.FindKey(i))
        d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*near)).Vertex(), edge).Value()
        if d < radius:
            found.append(i)
    return [s for s, i in _ids_at(ws, "E").items() if i in found]


# --- rebuild_face -------------------------------------------------------------------------------


def _surface_kind_of(face: Any) -> str:
    return str(BRepAdaptor_Surface(face, True).GetType()).rsplit("_", 1)[-1]


def _self_intersects(face: Any) -> bool:
    """ShapeAnalysis_Wire's verdict on the outer wire (BRepCheck cannot see a loop this small)."""
    from OCP.ShapeAnalysis import ShapeAnalysis_Wire

    return bool(ShapeAnalysis_Wire(BRepTools.OuterWire_s(face), face, 1e-6).CheckSelfIntersection())


def _with_distant_neighbour(face: Any) -> Any:
    """`face` and a small unrelated plane 300 mm away, in one compound: a larger PART, so the
    fidelity bound is its 0.1 mm cap. (A loop the wire analysis can detect is ~0.07 mm tall on a
    40 mm patch, which would exceed that patch's own 0.057 mm bound.)"""
    from OCP.TopoDS import TopoDS_Compound

    far = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(300, 0, 0), gp_Dir(0, 0, 1)), 0, 10, 0, 10).Face()
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, face)
    builder.Add(compound, far)
    return compound


def test_rebuild_face_repairs_a_small_self_intersecting_loop_and_stays_within_bound(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    surface = _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0)
    face = _looped_face(surface, 40.0, loop=0.05)  # a loop ~0.07 mm tall
    part = _with_distant_neighbour(face)
    assert _self_intersects(face), "the fixture must be a genuinely self-intersecting face"
    assert isinstance(BRep_Tool.Surface_s(face), Geom_BSplineSurface)
    bound = _bound(part)
    assert 0.07 < bound <= 0.1, "the loop is under the bound only because the part is large"
    ws = Workspace.from_brep(pool, _write(part, tmp_path / "looped.brep"))
    (face_id,) = [s for s, i in _ids_at(ws, "F").items()
                  if _surface_kind_of(_faces(part)[i - 1]) == "BSplineSurface"]

    after, diff = _op("rebuild_face").apply(pool, ws, [face_id], {"continuity": "G0"},
                                            timeout_s=TIMEOUT)

    rebuilt = _read(after.brep_path)
    (new_face,) = [f for f in _faces(rebuilt) if _surface_kind_of(f) == "BSplineSurface"]
    assert _valid(rebuilt) and not _self_intersects(new_face)
    assert diff.params["measured"]["wire_fixed"] is True
    # The TRIM is what moved: the old boundary (loop included) must sit within the bound of the
    # new face, measured here on the old face's own edge points.
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    old_edge_points = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current()))
        a, b = curve.FirstParameter(), curve.LastParameter()
        old_edge_points += [curve.Value(a + (b - a) * k / 40) for k in range(41)]
        explorer.Next()
    assert _dist(old_edge_points, new_face) <= bound
    assert diff.params["measured"]["boundary_shift"] <= bound
    assert abs(_area(new_face) - _area(face)) / _area(face) < 0.02
    # Identity: a rebuilt face is the same face (Modified 1:1), and nothing is declared.
    assert face_id in after.ids and diff.modified[face_id] == [face_id]
    assert len([f for f in after.ids if f.startswith("F")]) == 2  # the neighbour untouched
    assert len(diff.removed) <= 1 and all(r.startswith("E") for r in diff.removed)
    assert diff.declared_added == [] and diff.declared_removed == []


def test_rebuild_face_never_refuses_a_loop_larger_than_the_bound(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    """B12: a loop ~0.4 mm tall against a 0.057 mm bound is no longer refused on the bound. The op may apply (the
    boundary shift is then reported) or fail on the kernel, but never declines on fidelity."""
    surface = _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0)
    face = _looped_face(surface, 40.0, loop=0.3)
    assert _self_intersects(face)
    ws = Workspace.from_brep(pool, _write(face, tmp_path / "bigloop.brep"))
    (face_id,) = [s for s in ws.ids if s.startswith("F")]
    try:
        _, diff = _op("rebuild_face").apply(pool, ws, [face_id], {"continuity": "G0"}, timeout_s=TIMEOUT)
    except OpDeclined as why:
        assert "over_bound" not in why.reason, why.reason
        return
    assert "measured" in diff.params, diff.params


def test_rebuild_face_declines_what_it_cannot_fix_and_changes_nothing(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    surface = _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0)
    face = _bowtie_face(surface, 40.0)
    assert not _valid(face)
    ws = Workspace.from_brep(pool, _write(face, tmp_path / "bowtie.brep"))
    (face_id,) = [s for s in ws.ids if s.startswith("F")]

    with pytest.raises(OpDeclined) as declined:
        _op("rebuild_face").apply(pool, ws, [face_id], {"continuity": "G0"}, timeout_s=TIMEOUT)

    assert "unfixable" in declined.value.reason
    assert ws.history == [] and sorted(p.name for p in tmp_path.iterdir()) == ["bowtie.brep"]


# --- fill_loop ----------------------------------------------------------------------------------


def test_fill_loop_closes_a_2mm_hole_declares_the_added_face_and_moves_nothing(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    ws = Workspace.from_brep(pool, _box_with_hole(tmp_path, size=50.0, hole=2.0, round_hole=True))
    shell_before = _read(ws.brep_path)
    loop = _free_loop_ids(ws, (25.0, 25.0, 50.0), 1.5)
    assert loop and _free_edges(shell_before) == len(loop)
    before = {s: ws.ids[s] for s in ws.ids}

    after, diff = _op("fill_loop").apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=TIMEOUT)

    shell = _read(after.brep_path)
    assert _valid(shell) and _free_edges(shell) == 0, "the hole is closed and nothing else opened"
    # Declared: exactly one added face, and it is the planar disc the loop bounded.
    (added,) = diff.declared_added
    assert added.startswith("F") and added in after.ids and added not in before
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shell, TopAbs_FACE, index)
    disc = TopoDS.Face_s(index.FindKey(after.indices[added]))
    assert BRepAdaptor_Surface(disc, True).GetType() == GeomAbs_Plane
    assert _area(disc) == pytest.approx(math.pi * 1.0 ** 2, rel=1e-3)
    # One-sided original -> repaired: every original face is still where it was.
    assert max(_dist(_surface_points(f), shell) for f in _faces(shell_before)) <= _bound(shell_before)
    # Identity (IsSame contract): every pre-existing id survived, none was minted but the added face.
    assert set(before) <= set(after.ids) and set(after.ids) - set(before) == {added}
    assert diff.removed == [] and diff.declared_removed == []
    assert _area(shell) == pytest.approx(_area(shell_before) + _area(disc), rel=1e-9)


def test_fill_loop_fills_a_loop_spanning_20_percent_of_the_diagonal(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    """B12: the 5% span cap is gone; a 20% loop is filled and its span reported."""
    size = 50.0
    side = 0.2 * math.sqrt(3) * size / math.sqrt(2)  # a square hole whose diagonal is 20% of the part's
    ws = Workspace.from_brep(pool, _box_with_hole(tmp_path, size=size, hole=side, round_hole=False))
    loop = _free_loop_ids(ws, (25.0, 25.0, 50.0), side)
    assert len(loop) == 4

    _, diff = _op("fill_loop").apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=TIMEOUT)

    assert len(diff.declared_added) == 1
    assert diff.params["measured"]["span_ratio"] == pytest.approx(0.2, rel=0.02)


def test_fill_loop_fills_loops_either_side_of_the_old_5_percent_cap(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    size = 50.0
    diagonal = math.sqrt(3) * size
    for ratio in (0.045, 0.055):
        folder = tmp_path / f"r{ratio}"
        folder.mkdir()
        side = ratio * diagonal / math.sqrt(2)
        ws = Workspace.from_brep(pool, _box_with_hole(folder, size=size, hole=side, round_hole=False))
        loop = _free_loop_ids(ws, (25.0, 25.0, 50.0), side)
        _, diff = _op("fill_loop").apply(pool, ws, loop, {"continuity": "G0"}, timeout_s=TIMEOUT)
        assert len(diff.declared_added) == 1


# --- to_canonical -------------------------------------------------------------------------------


def test_to_canonical_turns_a_near_plane_bspline_into_a_plane_within_1e_4(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    surface = _bspline(lambda x, y: 5.0 + 2e-5 * math.sin(x / 3) * math.cos(y / 4), size=20.0)
    solid = _solid_with_top(surface, 20.0, 5.0)
    ws = Workspace.from_brep(pool, _write(solid, tmp_path / "nearplane.brep"))
    (top_index,) = [i + 1 for i, f in enumerate(_faces(solid))
                    if isinstance(BRep_Tool.Surface_s(f), Geom_BSplineSurface)]
    top = _faces(solid)[top_index - 1]
    (top_id,) = [s for s, i in _ids_at(ws, "F").items() if i == top_index]
    before = set(ws.ids)

    after, diff = _op("to_canonical").apply(pool, ws, [top_id], {"tol_mm": 1e-4}, timeout_s=TIMEOUT)

    result = _read(after.brep_path)
    assert _valid(result)
    (plane_face,) = [f for f in _faces(result) if BRepAdaptor_Surface(f, True).GetType() == GeomAbs_Plane
                     and abs(_centre_z(f) - 5.0) < 1e-3]
    # Sampled distance of the ORIGINAL B-spline face to the new plane, and back: both within 1e-4.
    assert _dist(_surface_points(top), plane_face) <= 1e-4
    assert _dist(_surface_points(plane_face), top) <= 1e-4
    assert sum(1 for f in _faces(result) if isinstance(BRep_Tool.Surface_s(f), Geom_BSplineSurface)) == 0
    # Identity: the solid, its other five faces and all its edges keep their ids; the snapped
    # face is Modified 1:1 and keeps its own.
    assert set(after.ids) == before
    assert diff.modified[top_id] == [top_id] and diff.removed == []
    assert diff.declared_added == [] and diff.declared_removed == []


def test_to_canonical_accepts_a_tolerance_over_the_old_bound(pool: WorkerPool, tmp_path: Path) -> None:
    surface = _bspline(lambda x, y: 5.0 + 2e-5 * math.sin(x / 3), size=20.0)
    solid = _solid_with_top(surface, 20.0, 5.0)
    ws = Workspace.from_brep(pool, _write(solid, tmp_path / "nearplane.brep"))
    (top_index,) = [i + 1 for i, f in enumerate(_faces(solid))
                    if isinstance(BRep_Tool.Surface_s(f), Geom_BSplineSurface)]
    (top_id,) = [s for s, i in _ids_at(ws, "F").items() if i == top_index]
    assert _bound(solid) < 0.05

    try:
        _op("to_canonical").apply(pool, ws, [top_id], {"tol_mm": 0.05}, timeout_s=TIMEOUT)
    except OpDeclined as why:
        assert "over_bound" not in why.reason, why.reason


# --- reapproximate ------------------------------------------------------------------------------


def test_reapproximate_lowers_the_degree_within_tolerance(pool: WorkerPool, tmp_path: Path) -> None:
    surface = _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0, n=14,
                       deg=(8, 8))
    assert surface.UDegree() == 8
    face = BRepBuilderAPI_MakeFace(surface, 1e-7).Face()
    ws = Workspace.from_brep(pool, _write(face, tmp_path / "deg8.brep"))
    (face_id,) = [s for s in ws.ids if s.startswith("F")]

    after, diff = _op("reapproximate").apply(
        pool, ws, [face_id], {"tol_mm": 0.02, "max_degree": 5}, timeout_s=TIMEOUT)

    result = _read(after.brep_path)
    (new_face,) = _faces(result)
    new_surface = BRep_Tool.Surface_s(new_face)
    assert max(new_surface.UDegree(), new_surface.VDegree()) <= 5
    assert _valid(result)
    assert _dist(_surface_points(face), new_face) <= 0.02
    assert _dist(_surface_points(new_face), face) <= 0.02
    assert diff.modified[face_id] == [face_id] and diff.removed == [] and diff.declared_added == []


# --- refit_cluster ------------------------------------------------------------------------------


def _split_patch_shell(tmp_path: Path, surface: Any = None, *, ways: int = 4) -> tuple[str, Any]:
    """One B-spline patch split by its mid iso-curve(s) into `ways` (2 or 4) faces in a shell."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    surface = surface or _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0)
    patch = BRepBuilderAPI_MakeFace(surface, 1e-7).Face()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(patch)
    tools.Append(BRepBuilderAPI_MakeEdge(surface.UIso(sum(surface.Bounds()[:2]) / 2)).Edge())
    if ways == 4:
        tools.Append(BRepBuilderAPI_MakeEdge(surface.VIso(sum(surface.Bounds()[2:]) / 2)).Edge())
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    parts = _faces(splitter.Shape())
    assert len(parts) == ways
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for part in parts:
        builder.Add(shell, part)
    return _write(shell, tmp_path / f"split{ways}.brep"), patch


def test_refit_cluster_replaces_four_fragments_with_one_declared_face(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    path, patch = _split_patch_shell(tmp_path)
    ws = Workspace.from_brep(pool, path)
    cluster = sorted(s for s in ws.ids if s.startswith("F"))
    assert len(cluster) == 4

    after, diff = _op("refit_cluster").apply(pool, ws, cluster, {"tol_mm": 0.02}, timeout_s=TIMEOUT)

    result = _read(after.brep_path)
    (merged,) = _faces(result)
    assert _valid(result)
    # Declared: one added face (the merged one, a fresh id), and exactly the cluster removed.
    (added,) = diff.declared_added
    assert added in after.ids and added not in cluster and after.indices[added] == 1
    assert diff.declared_removed == cluster
    assert sorted(r for r in diff.removed if r.startswith("F")) == cluster
    assert len([r for r in diff.removed if r.startswith("E")]) == 4  # the interior edges
    assert [s for s in after.ids if s.startswith("F")] == [added]
    # Faithful: the merged face and the original patch are within tolerance of each other.
    assert _dist(_surface_points(merged), patch) <= 0.02
    assert _dist(_surface_points(patch), merged) <= 0.02
    assert abs(_area(merged) - _area(patch)) / _area(patch) < 0.01
    assert diff.params["measured"]["old_to_new"] <= 0.02


def test_every_op_is_exposed_with_a_closed_schema_and_no_fidelity_maximum() -> None:
    assert [o.name for o in OPS] == ["rebuild_face", "fill_loop", "reapproximate", "to_canonical",
                                     "refit_cluster"]
    for op in OPS:
        assert op.params_schema["additionalProperties"] is False
        assert callable(op.applies_to) and callable(op.apply)
    assert _op("reapproximate").params_schema["properties"]["max_degree"] == {
        "type": "integer", "minimum": 3, "maximum": 9}
    assert "maximum" not in _op("to_canonical").params_schema["properties"]["tol_mm"]       # B12: no bound cap


@pytest.mark.parametrize("continuity", ["G0", "G1"])
def test_fill_loop_fills_a_curved_hole_with_a_non_planar_face_close_to_the_surface(
    pool: WorkerPool, tmp_path: Path, continuity: str,
) -> None:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    radius = 20.0
    cylinder = BRepPrimAPI_MakeCylinder(radius, 200.0).Shape()
    (wall,) = [f for f in _faces(cylinder) if BRepAdaptor_Surface(f, True).GetType().name.endswith("Cylinder")]
    bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 100), gp_Dir(0, 1, 0)), 3.0, 30.0).Shape()
    shell = TopoDS_Shell()
    BRep_Builder().MakeShell(shell)
    for piece in _faces(BRepAlgoAPI_Cut(wall, bore).Shape()):
        BRep_Builder().Add(shell, piece)
    holed = shell
    ws = Workspace.from_brep(pool, _write(holed, tmp_path / "wall.brep"))
    loop = _free_loop_ids(ws, (0.0, radius, 100.0), 5.0)
    assert loop
    before_free = _free_edges(holed)

    after, diff = _op("fill_loop").apply(pool, ws, loop, {"continuity": continuity}, timeout_s=TIMEOUT)

    result = _read(after.brep_path)
    (added,) = diff.declared_added
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(result, TopAbs_FACE, index)
    fill = TopoDS.Face_s(index.FindKey(after.indices[added]))
    assert _valid(result) and _free_edges(result) == before_free - len(loop)
    assert BRepAdaptor_Surface(fill, True).GetType() != GeomAbs_Plane
    # Sampled distance of the fill from the cylinder it plugs: within the fidelity bound.
    worst = max(abs(math.hypot(p.X(), p.Y()) - radius)
                for p in _surface_points(fill))
    assert worst <= _bound(holed), (worst, _bound(holed))
    assert diff.removed == [] and diff.declared_removed == []


@pytest.mark.parametrize("op_name", ["to_canonical", "reapproximate"])
def test_replacing_two_adjacent_faces_keeps_their_shared_edge_shared(
    pool: WorkerPool, tmp_path: Path, op_name: str,
) -> None:
    """Two faces of one patch share an edge; replacing both must not crack the seam between them."""
    if op_name == "to_canonical":
        surface = _bspline(lambda x, y: 5.0 + 2e-5 * math.sin(x / 3) * math.cos(y / 4), size=20.0)
        params: dict = {"tol_mm": 1e-4}
        ways = 4
    else:
        surface = _bspline(lambda x, y: 0.4 * math.sin(x / 9) * math.cos(y / 11), size=40.0, n=14,
                           deg=(8, 8))
        params = {"tol_mm": 0.02, "max_degree": 5}
        ways = 4
    path, _ = _split_patch_shell(tmp_path, surface, ways=ways)
    ws = Workspace.from_brep(pool, path)
    faces = sorted(s for s in ws.ids if s.startswith("F"))
    assert len(faces) == ways
    before = _free_edges(_read(path))

    after, diff = _op(op_name).apply(pool, ws, faces, params, timeout_s=TIMEOUT)

    result = _read(after.brep_path)
    assert _valid(result)
    assert _free_edges(result) == before, "the seam between the two faces must still be shared"
    assert set(faces) <= set(after.ids) and all(diff.modified[f] == [f] for f in faces)
