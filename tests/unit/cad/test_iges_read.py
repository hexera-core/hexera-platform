"""The product's one IGES reader (cad/iges_read): B-splines are kept whole on import.

OpenCascade's default (`read.iges.bspline.continuity` 1) splits a B-spline at every place it is less
smooth than C1, so an exported spline part came in with ten times its own edges and stitching it took
minutes. The setting only takes once a reader has been built, so the reader sets it after building."""
from __future__ import annotations

import pytest

pytest.importorskip("OCP")


def _c0_face_iges(tmp_path):
    """A flat face whose top edge is ONE degree-1 B-spline with three C0 kinks, written as an IGES trimmed
    surface (entity 144, writer mode 0: how a pre-processor exports spline faces)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.Geom import Geom_BSplineCurve
    from OCP.gp import gp_Pnt
    from OCP.IGESControl import IGESControl_Writer
    from OCP.STEPControl import STEPControl_Writer
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    STEPControl_Writer()          # (test_declared_foot_scale: keeps "write.step.unit" registered for later tests)
    pts = [(0, 0), (1, 0.3), (2, 0), (3, 0.3), (4, 0)]
    poles = TColgp_Array1OfPnt(1, len(pts))
    for i, (x, y) in enumerate(pts, 1):
        poles.SetValue(i, gp_Pnt(float(x), float(y), 0.0))
    knots, mults = TColStd_Array1OfReal(1, 5), TColStd_Array1OfInteger(1, 5)
    for i in range(1, 6):
        knots.SetValue(i, float(i - 1))
        mults.SetValue(i, 2 if i in (1, 5) else 1)
    spline = Geom_BSplineCurve(poles, knots, mults, 1)
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(spline).Edge())
    for a, b in (((4, 0), (4, -2)), ((4, -2), (0, -2)), ((0, -2), (0, 0))):
        wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(a[0], a[1], 0), gp_Pnt(b[0], b[1], 0)).Edge())
    face = BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()
    w = IGESControl_Writer("MM", 0)
    w.AddShape(face)
    w.ComputeModel()
    path = tmp_path / "c0_face.igs"
    assert w.Write(str(path))
    return path


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


def test_a_c0_spline_edge_is_read_as_one_edge_not_split(tmp_path):
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    from meshpipeline.cad.iges_read import read_iges

    path = _c0_face_iges(tmp_path)
    split = _read_with_continuity(path, 1)              # OpenCascade's own default
    whole = read_iges(path)
    assert _count(split, TopAbs_FACE) == _count(whole, TopAbs_FACE) == 1
    # the kinked edge is 4 pieces by default (one per C0 span), one edge as the product reads it
    assert _count(split, TopAbs_EDGE) == 7
    assert _count(whole, TopAbs_EDGE) == 4


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
    """gmsh's own OpenCascade splits the C0 edge (7 curves) whatever this process sets; the BREP of this reading
    keeps it whole (4), at the same metre size."""
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
