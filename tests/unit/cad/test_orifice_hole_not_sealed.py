# An orifice plate is a wall face with a hole in it - the hole the fluid is squeezed through.
# The undeclared-opening sealer looked through that hole, saw the outside via the open pipe
# end, and capped it: every orifice shape in the corpus meshed a 7 mm slab between two caps
# and had its real ports "sealed over" (jobs 848d9dba, 6edbafb2, 1b20782b). A hole that sees
# the exterior only THROUGH a declared port's mouth is the flow passage, never sealable.
# A hole that sees the exterior directly - a bore drilled through the pipe wall - still is.
import math

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)

# millimetres, like the corpus generator: a 100 mm bore, 10 mm wall, 400 mm run, and an
# 8 mm plate at mid-length with a 50 mm hole, integral to the wall (the plate ring reaches
# half a wall thickness into the metal, exactly as families.py builds it)
D, DO, TP, L, W = 100.0, 50.0, 8.0, 400.0, 10.0


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


def _orifice_shell(path, *, side_hole: bool = False):
    """The corpus orifice as the WALL SHELL (outer minus fluid): fluid = run minus plate
    ring, plate ring = annulus (r = D/2 + W/2, thickness TP) minus the orifice hole."""
    run = _cyl((0, 0, 0), (1, 0, 0), D / 2, L)
    x0 = L / 2 - TP / 2
    ring = _cyl((x0, 0, 0), (1, 0, 0), D / 2 + W / 2, TP)
    hole = _cyl((x0, 0, 0), (1, 0, 0), DO / 2, TP)
    ring = BRepAlgoAPI_Cut(ring, hole).Shape()
    fluid = BRepAlgoAPI_Cut(run, ring).Shape()
    outer = _cyl((0, 0, 0), (1, 0, 0), D / 2 + W, L)
    shell = BRepAlgoAPI_Cut(outer, fluid).Shape()
    if side_hole:
        # an UNDECLARED opening: a 12 mm bore drilled radially through the wall at L/4
        drill = _cyl((L / 4, 0, 0), (0, 1, 0), 6.0, D / 2 + W + 1.0)
        shell = BRepAlgoAPI_Cut(shell, drill).Shape()
    w = STEPControl_Writer()
    w.Transfer(shell, STEPControl_AsIs)
    assert w.Write(str(path)) is not None
    return path


def _declared():
    # what the pipeline hands over from the intake: name, bore, and a rough location
    area = math.pi * (D / 2000.0) ** 2
    return [{"name": "inlet", "area_m2": area, "near_m": (0.0, 0.0, 0.0), "d_m": D / 1000.0},
            {"name": "outlet", "area_m2": area, "near_m": (L / 1000.0, 0.0, 0.0),
             "d_m": D / 1000.0}]


def test_the_orifice_bore_is_the_passage_not_an_undeclared_opening(tmp_path):
    step = _orifice_shell(tmp_path / "orifice.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(),
                            declared_ports=_declared())
    sealed = t["sealed"]["undeclared_openings"]
    assert sealed == [], (
        "the orifice hole was capped as an undeclared opening - the bore is shut and the "
        f"fluid cannot reach the far port: {sealed}")
    assert sorted(t["openings"]) == ["inlet", "outlet"], t["openings"]
    # the two ports are the pipe ENDS, a full run apart - not the plate faces 8 mm apart
    xs = sorted(o["centroid"][0] for o in t["openings"].values())
    assert xs[1] - xs[0] == pytest.approx(L / 1000.0, abs=1e-3)


def test_a_hole_that_sees_the_exterior_directly_is_still_sealed(tmp_path):
    step = _orifice_shell(tmp_path / "orifice_drilled.step", side_hole=True)
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(),
                            declared_ports=_declared())
    sealed = t["sealed"]["undeclared_openings"]
    # the radial drill through the wall: two rims (outer skin and bore), both sealed; the
    # orifice bore itself is still left alone
    assert sealed, "a bore drilled through the wall was not sealed - the carve would leak"
    for s in sealed:
        assert abs(s["centroid"][0] - L / 4000.0) < 2e-3, f"sealed the wrong hole: {s}"
        assert s["area"] < 2.0 * math.pi * 0.006 ** 2, f"a sealed area is not the drill: {s}"
