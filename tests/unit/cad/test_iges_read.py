"""The product's one IGES reader (cad/iges_read): B-splines are kept whole on import.

OpenCascade's default (`read.iges.bspline.continuity` 1) splits a B-spline at every place it is less
smooth than C1, so an exported spline part came in with ten times its own edges and stitching it took
minutes. The setting only takes once a reader has been built, so the reader sets it after building.
MESH-26: a boundary curve that turns a CORNER at one of its breaks is cut there, and only there; a break where
the curve runs on (tangent, or turning by under CORNER_TURN_DEG) stays inside one edge, and a face with no corner
is left exactly as read."""
from __future__ import annotations

import pytest

pytest.importorskip("OCP")


#: the top edge's poles: TANGENT runs straight on through its three C0 breaks, its parameter speed jumping
#: (no knot can be removed, so OpenCascade's default splits it); CORNER turns 33 degrees at each break
TANGENT = [(0, 0), (1, 0), (3, 0), (3.5, 0), (4, 0)]
CORNER = [(0, 0), (1, 0.3), (2, 0), (3, 0.3), (4, 0)]
#: one real corner (about 53 degrees, at the middle break) between two breaks that turn by under 0.01 degree
MIXED = [(0, 0), (1, 0.5), (2, 1.0001), (3, 0.5), (4, 0)]


def _spline(pts, dz: float = 0.0):
    """ONE degree-1 B-spline through `pts` (at height dz), with three C0 breaks at parameters 1, 2 and 3."""
    from OCP.Geom import Geom_BSplineCurve
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    poles = TColgp_Array1OfPnt(1, len(pts))
    for i, (x, y) in enumerate(pts, 1):
        poles.SetValue(i, gp_Pnt(float(x), float(y), dz))
    knots, mults = TColStd_Array1OfReal(1, 5), TColStd_Array1OfInteger(1, 5)
    for i in range(1, 6):
        knots.SetValue(i, float(i - 1))
        mults.SetValue(i, 2 if i in (1, 5) else 1)
    return Geom_BSplineCurve(poles, knots, mults, 1)


def _c0_face(pts=TANGENT, dz: float = 0.0):
    """A flat face whose top edge is ONE degree-1 B-spline through `pts` with three C0 breaks."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_Writer

    STEPControl_Writer()          # (test_declared_foot_scale: keeps "write.step.unit" registered for later tests)
    spline = _spline(pts, dz)
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(spline).Edge())
    for a, b in (((4, 0), (4, -2)), ((4, -2), (0, -2)), ((0, -2), (0, 0))):
        wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(a[0], a[1], dz), gp_Pnt(b[0], b[1], dz)).Edge())
    return BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()


def _write_iges(path, *shapes):
    """`shapes` written as IGES trimmed surfaces (entity 144, writer mode 0: how a pre-processor exports spline
    faces)."""
    from OCP.IGESControl import IGESControl_Writer

    w = IGESControl_Writer("MM", 0)
    for shape in shapes:
        w.AddShape(shape)
    w.ComputeModel()
    assert w.Write(str(path))
    return path


def _c0_face_iges(tmp_path, pts=TANGENT):
    return _write_iges(tmp_path / "c0_face.igs", _c0_face(pts))


def _count(shape, kind) -> int:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m.Extent()


def _read_with_continuity(path, value: int):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IGESControl import IGESControl_Reader
    from OCP.Interface import Interface_Static

    reader = IGESControl_Reader()
    assert Interface_Static.SetIVal_s("read.iges.bspline.continuity", value)
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    reader.TransferRoots()
    return reader.OneShape()


def test_the_reader_keeps_b_splines_whole():
    from meshpipeline.cad.iges_read import BSPLINE_CONTINUITY, continuity_in_force, iges_reader

    assert BSPLINE_CONTINUITY == 0
    iges_reader()
    assert continuity_in_force() == 0


def test_the_setting_is_made_after_the_reader_is_built():
    """Set before the first reader is built, OpenCascade overwrites it: the reader sets it after."""
    from OCP.IGESControl import IGESControl_Reader
    from OCP.Interface import Interface_Static

    from meshpipeline.cad.iges_read import continuity_in_force, iges_reader

    IGESControl_Reader()          # in a fresh process the key does not exist until a reader is built
    assert Interface_Static.SetIVal_s("read.iges.bspline.continuity", 1)
    assert continuity_in_force() == 1
    iges_reader()
    assert continuity_in_force() == 0


def test_a_tangent_c0_spline_edge_is_read_as_one_edge_not_split(tmp_path):
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.iges_read import read_iges

    path = _c0_face_iges(tmp_path)
    split = _read_with_continuity(path, 1)              # OpenCascade's own default
    whole = read_iges(path)
    assert _count(split, TopAbs_FACE) == _count(whole, TopAbs_FACE) == 1
    # the edge is 4 pieces by default (one per C0 span), one edge as the product reads it
    assert _count(split, TopAbs_EDGE) == 7
    assert _count(whole, TopAbs_EDGE) == 4


def test_a_spline_edge_that_turns_corners_is_split_as_opencascade_splits_it(tmp_path):
    """MESH-26: kept whole, a curve round a corner is one edge where the CAD has a corner (4 edges)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.iges_read import read_iges

    path = _c0_face_iges(tmp_path, CORNER)
    split = _read_with_continuity(path, 1)
    read = read_iges(path)
    assert _count(read, TopAbs_FACE) == 1
    assert _count(split, TopAbs_EDGE) == _count(read, TopAbs_EDGE) == 7


def test_only_the_faces_that_turn_a_corner_are_split(tmp_path):
    """One file, a cornered face and a tangent one: the first is split (4 edges -> 7), the second stays whole (4)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.iges_read import read_iges

    path = _write_iges(tmp_path / "two.igs", _c0_face(CORNER), _c0_face(TANGENT, dz=5.0))
    read = read_iges(path)
    assert _count(read, TopAbs_FACE) == 2
    assert _count(read, TopAbs_EDGE) == 7 + 4
    assert _count(_read_with_continuity(path, 1), TopAbs_EDGE) == 7 + 7


def _edges(shape) -> list:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, m)
    return [TopoDS.Edge_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _faces_of(shape, edge) -> int:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, m)
    return sum(1 for _ in m.FindFromKey(edge))


def _valid(shape) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return BRepCheck_Analyzer(shape).IsValid()


def test_a_curve_is_cut_at_its_corner_only_its_near_straight_breaks_stay_inside_one_edge(tmp_path):
    """MESH-26 cuts a boundary curve where it turns a corner and nowhere else: of MIXED's three breaks only the
    middle one turns by more than CORNER_TURN_DEG, so the top edge is two edges (OpenCascade's split: four), each
    the read curve itself over its span - the two near-straight breaks stay inside them."""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.iges_read import read_iges

    path = _c0_face_iges(tmp_path, MIXED)
    read = read_iges(path)
    assert _count(_read_with_continuity(path, 0), TopAbs_EDGE) == 4
    assert _count(_read_with_continuity(path, 1), TopAbs_EDGE) == 7
    assert _count(read, TopAbs_FACE) == 1 and _count(read, TopAbs_EDGE) == 5
    assert _valid(read)
    spans = []
    for edge in _edges(read):
        curve = BRep_Tool.Curve_s(edge, 0.0, 1.0)
        if isinstance(curve, Geom_BSplineCurve):
            assert (curve.NbPoles(), curve.NbKnots(), curve.Degree()) == (5, 5, 1)   # the whole read curve
            spans.append(tuple(round(t, 9) for t in BRep_Tool.Range_s(edge)))
    assert sorted(spans) == [(0.0, 2.0), (2.0, 4.0)]                              # cut at the corner only


def _vertex(x, y):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt

    return BRepBuilderAPI_MakeVertex(gp_Pnt(float(x), float(y), 0.0)).Vertex()


def _plane_face(*edges):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire

    wire = BRepBuilderAPI_MakeWire()
    for edge in edges:
        wire.Add(edge)
    return BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()


def _ends(edge):
    from OCP.TopExp import TopExp

    return TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge)


def _line(a, b):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge

    return BRepBuilderAPI_MakeEdge(a, b).Edge()


def _compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    builder, comp = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(comp)
    for shape in shapes:
        builder.Add(comp, shape)
    return comp


def _spline_edge(pts):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge

    return BRepBuilderAPI_MakeEdge(_spline(pts)).Edge()


def test_a_face_with_no_corner_is_left_exactly_as_read(tmp_path):
    """Face A's top curve turns a corner; face B shares A's bottom curve, which has breaks that do not turn. Only
    the corner is cut: B, and the curve it shares with A, are the very shapes read, to the byte."""
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.iges_read import split_corner_faces

    top = _spline_edge(MIXED)
    bottom = _spline_edge([(x, y - 2) for x, y in TANGENT])
    (t0, t1), (b0, b1) = _ends(top), _ends(bottom)
    face_a = _plane_face(top, _line(t1, b1), bottom, _line(b0, t0))
    c0, c1 = _vertex(0, -4), _vertex(4, -4)
    face_b = _plane_face(bottom, _line(b1, c1), _line(c1, c0), _line(c0, b0))
    shape = _compound(face_a, face_b)
    assert _valid(shape) and _count(shape, TopAbs_FACE) == 2 and len(_edges(shape)) == 7
    out = split_corner_faces(shape)
    assert _valid(out) and len(_edges(out)) == 8                      # the one corner: one more edge
    assert any(e.IsSame(bottom) for e in _edges(out))                 # the shared tangent curve: one edge, as read
    ex, kept = TopExp_Explorer(out, TopAbs_FACE), []
    while ex.More():
        kept += [ex.Current()] if ex.Current().IsSame(face_b) else []
        ex.Next()
    assert len(kept) == 1
    BRepTools.Write_s(face_b, str(tmp_path / "read.brep"))
    BRepTools.Write_s(kept[0], str(tmp_path / "after.brep"))
    assert (tmp_path / "read.brep").read_bytes() == (tmp_path / "after.brep").read_bytes()
    assert split_corner_faces(face_b) is face_b                       # no corner at all: the shape as given


def test_a_shared_edge_is_cut_once_for_both_its_faces():
    """The cornered curve bounds two faces: it is cut once, and both pieces bound both faces (the shape valid)."""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_BSplineCurve

    from meshpipeline.cad.iges_read import split_corner_faces

    edge = _spline_edge(MIXED)
    e0, e1 = _ends(edge)
    d0, d1, u0, u1 = _vertex(0, -2), _vertex(4, -2), _vertex(0, 3), _vertex(4, 3)
    below = _plane_face(edge, _line(e1, d1), _line(d1, d0), _line(d0, e0))
    above = _plane_face(edge, _line(e1, u1), _line(u1, u0), _line(u0, e0))
    shape = _compound(below, above)
    assert _valid(shape) and len(_edges(shape)) == 7 and _faces_of(shape, edge) == 2
    out = split_corner_faces(shape)
    assert _valid(out) and len(_edges(out)) == 8
    pieces = [e for e in _edges(out) if isinstance(BRep_Tool.Curve_s(e, 0.0, 1.0), Geom_BSplineCurve)]
    assert len(pieces) == 2 and all(_faces_of(out, e) == 2 for e in pieces)


def test_every_product_read_of_an_iges_goes_through_the_one_reader():
    """The read sites outside cad/repair build their IGES reader through cad/iges_read."""
    from pathlib import Path

    import meshpipeline

    root = Path(meshpipeline.__file__).parent
    stray = [str(p.relative_to(root)) for p in root.rglob("*.py")
             if "IGESControl_Reader" in p.read_text(encoding="utf-8")
             and p.relative_to(root).parts[:2] != ("cad", "repair") and p.name != "iges_read.py"]
    assert stray == []


def test_the_scout_reads_with_it(tmp_path):
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad.iges_read import continuity_in_force
    from meshpipeline.cad.scout import read_cad

    path = _c0_face_iges(tmp_path)
    _read_with_continuity(path, 1)
    assert _count(read_cad(path), TopAbs_EDGE) == 4
    assert continuity_in_force() == 0


# ----------------------------------------------------------------- the IGES gmsh stages (gmsh_import_path) ----
def _gmsh_import(path, scale: float = 1.0):
    """(faces, curves, box) of `path` imported by gmsh at OCCScaling `scale`, as the job's staging imports it."""
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", scale)
        gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()
        return (len(gmsh.model.getEntities(2)), len(gmsh.model.getEntities(1)),
                [round(v, 9) for v in gmsh.model.getBoundingBox(-1, -1)])
    finally:
        gmsh.finalize()


def test_gmsh_is_handed_the_iges_as_read_here_same_size_and_faces_edges_kept_whole(tmp_path):
    """gmsh's own OpenCascade splits the tangent C0 edge (7 curves) whatever this process sets; the BREP of this
    reading keeps it whole (4), at the same metre size."""
    from meshpipeline.cad.iges_read import gmsh_import_path

    path = _c0_face_iges(tmp_path)
    direct = _gmsh_import(path, 0.001)
    with gmsh_import_path(path) as handed:
        assert handed.suffix == ".brep" and handed.exists()
        via = _gmsh_import(handed, 0.001)
    assert not handed.exists()                            # the BREP lives only while gmsh imports it
    assert direct[1] == 7 and via[1] == 4
    assert direct[0] == via[0] == 1
    assert direct[2] == pytest.approx(via[2], abs=1e-9)


def test_any_other_file_is_handed_to_gmsh_as_it_is(tmp_path):
    from meshpipeline.cad.iges_read import gmsh_import_path

    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    with gmsh_import_path(step) as handed:
        assert handed == step


def test_an_iges_that_cannot_be_read_here_is_staged_as_before(tmp_path, monkeypatch, caplog):
    """Never a new failure: the IGES itself goes to gmsh, and it is logged."""
    import logging

    from meshpipeline.cad import iges_read

    path = _c0_face_iges(tmp_path)

    def _fail(p):
        raise iges_read.IgesReadError("unreadable here")
    monkeypatch.setattr(iges_read, "read_iges", _fail)
    with caplog.at_level(logging.WARNING, logger="meshpipeline.cad.iges_read"):
        with iges_read.gmsh_import_path(path) as handed:
            assert handed == path
    assert "handed to gmsh as it is" in caplog.text


def test_the_job_staging_stages_an_iges_with_its_edges_whole(tmp_path):
    """engines/gmsh/gmsh_runner._stage_here (the job's staging) writes the staged B-rep from this reading."""
    gmsh = pytest.importorskip("gmsh")
    from OCP.TopAbs import TopAbs_EDGE

    from meshpipeline.cad.scout import read_cad
    from meshpipeline.engines.gmsh.gmsh_runner import _stage_here

    path = _c0_face_iges(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _stage_here(gmsh, path, ws / "input.stl", 0.001, extras=True)
    assert _count(read_cad(ws / "geometry.brep"), TopAbs_EDGE) == 4
