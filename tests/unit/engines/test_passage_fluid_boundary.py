# A FLUID DOMAIN'S WALL IS READ WHOLE. The passage field split the staged wall at its sharp edges and
# kept only the pieces touching a port cap - right for a hollow pipe's metal (its outer skin is not
# wetted), wrong for a fluid domain, whose every face bounds the flow: a heat exchanger's shell side
# kept its two nozzle pipes and lost the shell, the 19 tubes and the baffles, read 60 mm everywhere,
# and cfMesh delivered 7.9 cells across the tube bank (lab, 2026-10-04). The staging record now says
# which wall it is (`hollow_wall`), and a narrow reading on a fluid boundary is not vetoed by the
# ports - there is no wall thickness to misread there.
from __future__ import annotations

import math

import numpy as np
import pytest

import meshpipeline.engines.passage as P


def _open_tube(radius=0.05, length=1.0, n_around=48, n_along=80):
    th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
    xs = np.linspace(0, length, n_along)
    ring = np.stack([np.zeros_like(th), radius * np.cos(th), radius * np.sin(th)], axis=1)
    pts = np.concatenate([ring + [x, 0, 0] for x in xs])
    faces = []
    for i in range(n_along - 1):
        for j in range(n_around):
            a, b = i * n_around + j, i * n_around + (j + 1) % n_around
            faces += [[a, b, b + n_around], [a, b + n_around, a + n_around]]
    caps_pts = np.concatenate([pts, [[0, 0, 0], [length, 0, 0]]])
    c0, c1 = len(pts), len(pts) + 1
    caps = []
    base = (n_along - 1) * n_around
    for j in range(n_around):
        caps.append([c0, (j + 1) % n_around, j])
        caps.append([c1, base + j, base + (j + 1) % n_around])
    return pts, np.asarray(faces, np.int64), caps_pts, np.asarray(caps, np.int64)


def _poly(pts, faces):
    import pyvista as pv
    return pv.PolyData(np.asarray(pts, float), np.hstack([np.full((len(faces), 1), 3), faces]).ravel())


@pytest.fixture
def tube_with_an_obstacle(tmp_path):
    """A fluid domain: a 100 mm bore with a 60 mm closed rod across its middle (a tube-bank
    obstacle, 20 mm gap all round), the rod touching no port. Wall = bore skin + rod skin."""
    pv = pytest.importorskip("pyvista")
    bp, bf, cp, cf = _open_tube()
    rod = pv.Cylinder(center=(0.5, 0, 0), direction=(1, 0, 0), radius=0.03, height=0.4,
                      resolution=48, capping=True).triangulate().subdivide(2)
    rp = np.asarray(rod.points)
    rf = np.asarray(rod.faces).reshape(-1, 4)[:, 1:]
    wall = _poly(np.concatenate([bp, rp]), np.concatenate([bf, rf + len(bp)]))
    wall.save(str(tmp_path / "wall.stl"))
    _poly(cp, cf).save(str(tmp_path / "caps.stl"))
    return tmp_path


def _field(d, hollow):
    return P.passage_field_of_stls([d / "wall.stl"], cap_paths=[d / "caps.stl"],
                                   port_centroids=[[0, 0, 0], [1, 0, 0]], hollow_wall=hollow)


def test_a_fluid_boundary_keeps_the_obstacle_the_ports_do_not_touch(tube_with_an_obstacle):
    fluid = _field(tube_with_an_obstacle, False)
    assert fluid is not None
    pts, _faces, r = fluid
    on_rod = np.hypot(pts[:, 1], pts[:, 2]) < 0.035
    assert on_rod.any(), "the obstacle's wall must be read"
    assert np.median(r[on_rod]) < 0.015, "the 20 mm gap round the obstacle, not the 100 mm bore"
    assert np.percentile(r, 5) < 0.015


def test_a_hollow_walls_reading_keeps_every_wetted_piece(tube_with_an_obstacle):
    # read as a hollow wall's metal, the obstacle is wetted too (the cavity is on its outside)
    metal = _field(tube_with_an_obstacle, True)
    assert metal is not None
    pts, _faces, r = metal
    on_rod = np.hypot(pts[:, 1], pts[:, 2]) < 0.035
    assert on_rod.any()


def test_the_staged_record_says_which_wall_it_is(monkeypatch, tube_with_an_obstacle):
    seen = []

    def _spy(paths, **kw):
        seen.append(kw.get("hollow_wall"))
        return None
    monkeypatch.setattr(P, "passage_field_of_stls", _spy)
    srcs = {"wall": str(tube_with_an_obstacle / "wall.stl"),
            "inlet": str(tube_with_an_obstacle / "caps.stl")}
    for rec, want in (({"wall_bounds_fluid": True}, False), ({"wall_bounds_fluid": False}, True), ({}, True)):
        P.staged_passage_field({**rec, "openings": {}}, srcs, "wall", [])
        assert seen[-1] is want


def test_a_narrow_passage_on_a_fluid_boundary_is_not_vetoed_by_the_ports():
    ports = {"p05": 0.03, "median": 0.03}               # 60 mm nozzles
    tube_bank = {"p05": 0.0074, "median": 0.0093}       # the 15 mm gaps between tubes
    assert not P.plausible_radius(tube_bank, ports)     # on a hollow wall: a wall-thickness misread
    assert P.plausible_radius(tube_bank, ports, fluid_boundary=True)
    assert P.choose_passage_radius(tube_bank, ports, fluid_boundary=True)["source"] == "chord"
    assert P.choose_passage_radius(tube_bank, ports) == ports
    too_wide = {"p05": 0.2, "median": 0.3}               # the upper bound still applies
    assert not P.plausible_radius(too_wide, ports, fluid_boundary=True)
    failed_read = {"p05": 6e-6, "median": 6e-6}          # the field's clip floor, not a passage
    assert not P.plausible_radius(failed_read, ports, fluid_boundary=True)


def test_tessellation_records_whether_the_wall_is_metal(tmp_path):
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    prep = from_occ_transfer(GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t", unit=LengthUnit.millimetre,
        scale_to_metres=0.001, basis=ResolutionBasis.file_declared, evidence="mm"),
        LengthUnit.millimetre)

    def _step(shape, name):
        w = STEPControl_Writer()
        w.Transfer(shape, STEPControl_AsIs)
        w.Write(str(tmp_path / name))
        return tmp_path / name

    rod = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 25.0, 200.0).Shape()
    outer = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 30.0, 200.0).Shape()
    bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), 20.0, 202.0).Shape()
    tube = BRepAlgoAPI_Cut(outer, bore).Shape()
    fluid = tessellate_internal(_step(rod, "rod.step"), tmp_path / "a", prepared=prep,
                                fluid_solid=True)
    metal = tessellate_internal(_step(tube, "tube.step"), tmp_path / "b", prepared=prep,
                                fluid_solid=False)
    assert fluid["wall_bounds_fluid"] is True
    assert metal["wall_bounds_fluid"] is False
    assert math.isfinite(sum(metal["interior_point"]))
