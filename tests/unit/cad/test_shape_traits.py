# Responsibility: Verify the pre-mesh shape traits are MEASURED from the geometry - the same on a STEP solid and
# its STL, in millimetres or metres - and place round tubes, annuli, chambers, tube banks, pipe walls and
# external bodies where they belong.
# Boundaries: cad/shape_traits.py on solids built here with OpenCASCADE; nothing is named after a shape family.
from __future__ import annotations

import math

import numpy as np
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.StlAPI import StlAPI_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.shape_traits import (
    ShapeTraits,
    measure,
    measure_file,
    port_hydraulic_diameters,
    read_triangles,
)
from meshpipeline.engines import fitness


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


def _box(x, y, z, origin=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*origin), x, y, z).Shape()


def _step(shape, path):
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _stl(shape, path):
    BRepMesh_IncrementalMesh(shape, 0.2, False, 0.2, True)
    w = StlAPI_Writer()
    w.ASCIIMode = False
    w.Write(shape, str(path))
    return path


def _ports(*specs):
    out = []
    for i, spec in enumerate(specs):
        out.append({"name": f"p{i}", "type": "inlet" if i == 0 else "outlet", **spec})
    return out + [{"name": "wall", "type": "wall"}]


TUBE_PORTS = _ports({"diameter_mm": 20.0}, {"diameter_mm": 20.0})


def test_a_round_tube_reads_as_long_and_as_wide_as_its_openings(tmp_path):
    t = measure_file(_step(_cyl((0, 0, 0), (1, 0, 0), 10.0, 200.0), tmp_path / "tube.step"),
                     flow="internal", input_kind="fluid-domain", patches=TUBE_PORTS)
    assert t.form == "cad" and t.closed is True and t.genus == 0 and t.n_solids == 1
    assert t.ports == 2
    assert 0.85 < t.gap_vs_port < 1.15, t
    assert 7.0 < t.slenderness < 12.0, t                 # about L / D = 10
    assert t.neck > 0.6                                   # no throat
    assert t.passage == pytest.approx(0.020, rel=0.15)    # the chord, in metres
    assert fitness.shape_class(t) == fitness.TUBE


def test_the_same_tube_as_an_stl_and_in_metres_reads_the_same(tmp_path):
    shape = _cyl((0, 0, 0), (1, 0, 0), 10.0, 200.0)
    cad = measure_file(_step(shape, tmp_path / "tube.step"), flow="internal",
                       input_kind="fluid-domain", patches=TUBE_PORTS)
    stl = measure_file(_stl(shape, tmp_path / "tube.stl"), flow="internal",
                       input_kind="fluid-domain", patches=TUBE_PORTS, surface_unit_to_m=0.001)
    assert stl.form == "surface"
    for k in ("gap_vs_port", "slenderness", "neck"):
        assert getattr(stl, k) == pytest.approx(getattr(cad, k), rel=0.15), k
    # the same triangles in metres: every ratio is unchanged
    tris, _ = read_triangles(tmp_path / "tube.stl")
    metres = measure(tris / 1000.0, flow="internal", input_kind="fluid-domain",
                     port_diameters=port_hydraulic_diameters(TUBE_PORTS, scale=0.001))
    mm = measure(tris, flow="internal", input_kind="fluid-domain",
                 port_diameters=port_hydraulic_diameters(TUBE_PORTS))
    for k in ("gap_vs_port", "slenderness", "neck", "scale_ratio", "sharp_edges"):
        assert getattr(metres, k) == pytest.approx(getattr(mm, k), rel=1e-6), k


def test_an_annulus_spans_half_its_openings_hydraulic_diameter(tmp_path):
    shape = BRepAlgoAPI_Cut(_cyl((0, 0, 0), (1, 0, 0), 30.0, 300.0),
                            _cyl((-1, 0, 0), (1, 0, 0), 20.0, 302.0)).Shape()
    ports = _ports({"diameter_mm": 60.0, "inner_diameter_mm": 40.0},
                   {"diameter_mm": 60.0, "inner_diameter_mm": 40.0})
    t = measure_file(_step(shape, tmp_path / "annulus.step"), flow="internal",
                     input_kind="fluid-domain", patches=ports)
    assert t.genus == 1
    assert 0.4 < t.gap_vs_port < 0.6, t                   # the 10 mm gap over a 20 mm hydraulic diameter
    assert fitness.shape_class(t) == fitness.THIN_GAP


def test_a_box_much_wider_than_its_openings_is_a_chamber(tmp_path):
    t = measure_file(_step(_box(100.0, 100.0, 100.0), tmp_path / "box.step"), flow="internal",
                     input_kind="fluid-domain",
                     patches=_ports({"diameter_mm": 20.0}, {"diameter_mm": 20.0}))
    assert t.gap_vs_port > 1.4 and t.slenderness < 3.0, t
    assert fitness.shape_class(t) == fitness.CHAMBER


def test_tubes_crossing_the_fluid_read_as_handles_a_tube_bank(tmp_path):
    shell = _box(200.0, 60.0, 60.0)
    for i in range(5):
        shell = BRepAlgoAPI_Cut(shell, _cyl((30.0 + 35.0 * i, 30.0, -1.0), (0, 0, 1), 6.0, 62.0)).Shape()
    t = measure_file(_step(shell, tmp_path / "bank.step"), flow="internal", input_kind="fluid-domain",
                     patches=_ports({"width_mm": 60.0, "height_mm": 60.0},
                                    {"width_mm": 60.0, "height_mm": 60.0}))
    assert t.genus == 5, t
    assert fitness.handles_through_fluid(t) == 5
    assert fitness.shape_class(t) == fitness.OBSTACLES


def test_a_pipe_wall_reads_its_bore_not_its_metal(tmp_path):
    wall = BRepAlgoAPI_Cut(_cyl((0, 0, 0), (1, 0, 0), 12.0, 200.0),
                           _cyl((-1, 0, 0), (1, 0, 0), 10.0, 202.0)).Shape()
    t = measure_file(_step(wall, tmp_path / "pipe.step"), flow="internal", input_kind="body-surface",
                     patches=TUBE_PORTS)
    assert t.genus == 1
    assert fitness.handles_through_fluid(t) == 0          # its one handle is the bore its ports open
    assert 0.85 < t.gap_vs_port < 1.15, t
    assert t.thin_wall_fraction is not None and t.thin_wall_fraction > 0.5
    assert fitness.shape_class(t) == fitness.TUBE


def test_external_bodies_are_thin_or_thick_by_their_measured_proportions(tmp_path):
    plate = measure_file(_step(_box(200.0, 600.0, 20.0), tmp_path / "plate.step"), flow="external",
                         input_kind="body-surface", flow_axis="+x")
    cube = measure_file(_step(_box(100.0, 100.0, 100.0), tmp_path / "cube.step"), flow="external",
                        input_kind="body-surface", flow_axis="+x")
    assert plate.thickness_ratio == pytest.approx(0.1, rel=0.05)
    assert cube.thickness_ratio == pytest.approx(1.0, rel=0.05)
    assert fitness.shape_class(plate) == fitness.SLENDER
    assert fitness.shape_class(cube) == fitness.BLUNT
    # a box's twelve edges are all sharp; nothing is a knife edge
    assert cube.sharp_edges > 0 and cube.knife_edges == 0


def test_the_flow_axis_decides_what_streamwise_means(tmp_path):
    path = _step(_box(200.0, 600.0, 20.0), tmp_path / "plate.step")
    along_x = measure_file(path, flow="external", flow_axis="+x")
    along_z = measure_file(path, flow="external", flow_axis="-z")
    assert along_x.thickness_ratio == pytest.approx(0.1, rel=0.05)
    assert along_z.thickness_ratio == pytest.approx(10.0, rel=0.05)


def test_hydraulic_diameters_of_declared_openings():
    got = port_hydraulic_diameters([
        {"type": "inlet", "diameter_mm": 50.0},
        {"type": "outlet", "diameter_mm": 60.0, "inner_diameter_mm": 40.0},
        {"type": "outlet", "width_mm": 30.0, "height_mm": 10.0},
        {"type": "outlet", "area_mm2": math.pi * 25.0},
        {"type": "wall", "diameter_mm": 999.0},
        {"type": "farfield"},
    ])
    assert got == pytest.approx([50.0, 20.0, 15.0, 10.0])


def test_an_open_sheet_reads_its_fluid_from_the_side_its_chords_land(tmp_path):
    # a lumen exported as its wall alone: an open cylinder, no caps, winding unknown
    n, L, r = 48, 200.0, 10.0
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    ring = lambda x: np.stack([np.full(n, x), r * np.cos(th), r * np.sin(th)], axis=1)  # noqa: E731
    a, b = ring(0.0), ring(L)
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris += [[a[i], b[i], b[j]], [a[i], b[j], a[j]]]
    t = measure(np.asarray(tris), flow="internal", input_kind="body-surface",
                port_diameters=[20.0, 20.0])
    assert t.closed is False and t.genus is None
    assert 0.85 < t.gap_vs_port < 1.15, t


def test_a_file_that_cannot_be_read_gives_empty_traits_never_an_error(tmp_path):
    bad = tmp_path / "broken.step"
    bad.write_text("ISO-10303-21; this is not a solid")
    t = measure_file(bad, flow="internal", input_kind="fluid-domain")
    assert not t.measured and t.notes
    assert fitness.shape_class(t) == ""
    missing = measure_file(tmp_path / "gone.stl", flow="external")
    assert not missing.measured


def test_traits_round_trip_through_their_dict():
    t = ShapeTraits(flow="internal", form="cad", n_triangles=10, genus=2, gap_vs_port=0.9,
                    notes=("a note",))
    assert ShapeTraits.from_dict(t.as_dict()) == t
    assert ShapeTraits.from_dict({"flow": "external", "unknown_key": 1}).flow == "external"
    assert ShapeTraits.from_dict(None) == ShapeTraits()
