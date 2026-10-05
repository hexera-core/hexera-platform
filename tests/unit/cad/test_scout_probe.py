# Responsibility: Verify the scout's fast probe gives the exact B-rep's answers - every point the
# classifier calls in material, every line cut the whole-shape intersector finds - on solids of
# every kind the probe treats differently: flat, curved, B-spline, hollow, overlapping, inside out,
# and with a face the mesher left bare; and that the scout reads the same part either way.
# Boundaries: real OpenCASCADE solids; no files except the end-to-end scout check.
from __future__ import annotations

import math
import random

import numpy as np
import pytest

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.BRepPrimAPI import (
        BRepPrimAPI_MakeBox,
        BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakeSphere,
        BRepPrimAPI_MakeTorus,
    )
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.scout_probe import PartProbe


def _solids(shape) -> list:
    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(TopoDS.Solid_s(exp.Current()))
        exp.Next()
    return out


def _box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _cyl(r, h, origin=(0, 0, 0), axis=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*origin), gp_Dir(*axis)), r, h).Shape()


def _compound(*shapes):
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for s in shapes:
        b.Add(comp, s)
    return comp


def _loft():
    """A bulging B-spline vase, closed by flat ends."""
    loft = BRepOffsetAPI_ThruSections(True, False)
    for z, r in ((0.0, 20.0), (30.0, 34.0), (60.0, 14.0), (80.0, 25.0)):
        circ = gp_Circ(gp_Ax2(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)), r)
        loft.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire())
    loft.Build()
    return loft.Shape()


SHAPES = {
    "box": lambda: _box(0, 0, 0, 100, 60, 40),
    "tube": lambda: BRepAlgoAPI_Cut(_cyl(50, 200), _cyl(40, 200)).Shape(),
    "torus": lambda: BRepPrimAPI_MakeTorus(60.0, 15.0).Shape(),
    "hollow_sphere": lambda: BRepAlgoAPI_Cut(BRepPrimAPI_MakeSphere(50.0).Shape(), BRepPrimAPI_MakeSphere(40.0).Shape()).Shape(),
    "overlapping": lambda: _compound(_box(0, 0, 0, 80, 40, 40), _cyl(25, 120, (60, 20, -40))),
    "tee": lambda: BRepAlgoAPI_Cut(BRepAlgoAPI_Fuse(_cyl(50, 300, axis=(1, 0, 0)), _cyl(30, 150, (150, 0, 0))).Shape(),
                                   BRepAlgoAPI_Fuse(_cyl(40, 300, axis=(1, 0, 0)), _cyl(22, 150, (150, 0, 0))).Shape()).Shape(),
    "loft": _loft,
}


def _probe(shape, *, bare_face: int | None = None, deflection_fraction: float = 1.0 / 2500.0):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    lin = math.dist((x0, y0, z0), (x1, y1, z1)) * deflection_fraction
    BRepMesh_IncrementalMesh(shape, lin, False, 0.3, True)
    if bare_face is not None:
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        for _ in range(bare_face):
            exp.Next()
        BRepTools.Clean_s(exp.Current())                  # as if the mesher had left it bare
    return PartProbe(shape, _solids(shape), deflection=lin), (x0, y0, z0, x1, y1, z1), lin


def _points(probe, box, lin, n_free=150, n_near=150, seed=7):
    """Points anywhere in (and around) the box, and points just off the mesh's own nodes at a
    ladder of distances from far inside the margin to well outside it, on both sides."""
    rnd = random.Random(seed)
    x0, y0, z0, x1, y1, z1 = box
    pad = 0.1 * max(x1 - x0, y1 - y0, z1 - z0)
    pts = [(rnd.uniform(x0 - pad, x1 + pad), rnd.uniform(y0 - pad, y1 + pad), rnd.uniform(z0 - pad, z1 + pad))
           for _ in range(n_free)]
    tris = probe._tris
    for _ in range(n_near):
        t = tris[rnd.randrange(len(tris))]
        a, b = rnd.random(), rnd.random()
        if a + b > 1.0:
            a, b = 1.0 - a, 1.0 - b
        q = t[0] + a * (t[1] - t[0]) + b * (t[2] - t[0])
        nrm = np.cross(t[1] - t[0], t[2] - t[0])
        nn = float(np.linalg.norm(nrm)) or 1.0
        off = rnd.choice((0.01, 0.3, 1.0, 3.0, 10.0)) * lin * rnd.choice((1.0, -1.0))
        pts.append(tuple(float(v) for v in q + nrm / nn * off))
    return pts


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_every_point_is_answered_as_the_classifier_answers_it(name):
    probe, box, lin = _probe(SHAPES[name]())
    pts = _points(probe, box, lin)
    wrong = [p for p in pts if probe.inside_any(p) != probe.inside_any_exact(p)]
    assert not wrong, f"{name}: {len(wrong)} of {len(pts)} points read differently, first {wrong[:3]}"
    # the point of it: most points never reach the classifier
    assert probe.n_mesh > len(pts) // 3, (probe.n_mesh, probe.n_ray, probe.n_classifier)


@pytest.mark.parametrize("name", ["tube", "loft", "overlapping"])
def test_a_face_the_mesher_left_bare_still_reads_exactly(name):
    probe, box, lin = _probe(SHAPES[name](), bare_face=1)
    assert probe._bare.any()
    pts = _points(probe, box, lin, seed=11)
    wrong = [p for p in pts if probe.inside_any(p) != probe.inside_any_exact(p)]
    assert not wrong, f"{name}: {len(wrong)} points read differently with a bare face"


def test_an_inside_out_solid_is_left_to_the_classifier():
    shape = _box(0, 0, 0, 50, 50, 50).Reversed()
    probe, box, lin = _probe(shape)
    assert probe._trusted == [False]
    pts = _points(probe, box, lin, n_free=40, n_near=40)
    assert all(probe.inside_any(p) == probe.inside_any_exact(p) for p in pts)


def test_a_point_on_the_surface_is_never_read_as_material_by_the_mesh():
    probe, _box_, _lin = _probe(SHAPES["box"]())
    for p in ((0.0, 30.0, 20.0), (50.0, 0.0, 20.0), (100.0, 60.0, 40.0), (50.0, 30.0, 40.0)):
        assert probe.inside_any(p) == probe.inside_any_exact(p)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_every_line_meets_the_part_where_the_whole_shape_cut_says(name):
    probe, box, _lin = _probe(SHAPES[name]())
    rnd = random.Random(3)
    x0, y0, z0, x1, y1, z1 = box
    for _ in range(40):
        o = (rnd.uniform(x0, x1), rnd.uniform(y0, y1), rnd.uniform(z0, z1))
        d = [rnd.gauss(0, 1) for _ in range(3)]
        n = math.sqrt(sum(v * v for v in d))
        d = tuple(v / n for v in d)
        for skip in (0.0, 0.01 * (x1 - x0)):
            assert probe.meets_beyond(o, d, skip) == probe.meets_beyond_exact(o, d, skip)
            assert probe.first_beyond(o, d, skip) == pytest.approx(probe.first_beyond_exact(o, d, skip), abs=1e-12)


def test_the_scout_reads_the_same_part_with_the_fast_probe_as_with_the_exact_one(tmp_path, monkeypatch):
    """End to end: the scout's whole answer - kinds, openings, faces, seed - is the same."""
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.cad import scout_probe
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import GeometryInterpretation, LengthUnit, ResolutionBasis

    interp = GeometryInterpretation(interpretation_id="t", owner_id="t", geometry_source_id="t",
                                    unit=LengthUnit.millimetre, scale_to_metres=1e-3,
                                    basis=ResolutionBasis.file_declared, evidence="test")
    for name in ("tube", "tee", "box", "overlapping", "loft"):
        path = tmp_path / f"{name}.step"
        w = STEPControl_Writer()
        assert Interface_Static.SetCVal_s("write.step.unit", "MM")
        w.Transfer(SHAPES[name](), STEPControl_AsIs)
        w.Write(str(path))
        prepared = from_occ_transfer(interp, parser_applied_unit(path))
        fast = scout_cad(path, prepared=prepared).as_dict()
        with monkeypatch.context() as m:
            m.setattr(scout_probe.PartProbe, "inside_any", scout_probe.PartProbe.inside_any_exact)
            m.setattr(scout_probe.PartProbe, "meets_beyond", scout_probe.PartProbe.meets_beyond_exact)
            m.setattr(scout_probe.PartProbe, "first_beyond", scout_probe.PartProbe.first_beyond_exact)
            exact = scout_cad(path, prepared=prepared).as_dict()
        assert fast == exact, name


def test_a_mesh_index_that_cannot_be_built_leaves_the_exact_answers(monkeypatch):
    from meshpipeline.cad import scout_probe

    def broken(self, *a, **k):
        raise RuntimeError("no index today")

    monkeypatch.setattr(scout_probe.PartProbe, "__init__", broken)
    shape = SHAPES["tube"]()
    BRepMesh_IncrementalMesh(shape, 0.5, False, 0.3, True)
    probe = scout_probe.make_probe(shape, _solids(shape), deflection=0.5)
    assert type(probe) is scout_probe.ExactProbe
    assert probe.inside_any((45.0, 0.0, 100.0)) is True             # in the wall
    assert probe.inside_any((0.0, 0.0, 100.0)) is False             # in the bore
    assert probe.meets_beyond((0.0, 0.0, 100.0), (1.0, 0.0, 0.0), 0.0) is True
    assert probe.first_beyond((0.0, 0.0, 100.0), (1.0, 0.0, 0.0), 0.0) == pytest.approx(40.0)
