# A ported chamber's inner wall has a hole where each bore tunnel joins it. Those
# junction holes are the fluid's own passages - but the undeclared-opening probes
# cannot tell: a bore is void rather than material (so "nothing fills it" passes) and
# its exit ray leaves through the declared mouth itself (so "sees exterior" passes).
# Eleven mini_housing baseline episodes died with every chamber-to-bore junction sealed
# into the wall - chamber walled off from its own ports, zero-face port patches at the
# manifest gate. The passage test fixes it: a hole coaxial with a declared mouth and
# joined to it by pure void is the port's own bore, never sealable.
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
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


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


def _housing_shell(path):
    """A mini_housing in miniature: hollow sphere chamber (cavity r=50, wall 5) with
    two opposite tubes on the X axis (bore r=15, tube outer r=20, mouths at x=±100).
    Exported as the WALL SHELL - outer lump minus fluid - exactly the carve-case input."""
    outer = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), 55.0).Shape()
    for d in ((1, 0, 0), (-1, 0, 0)):
        outer = BRepAlgoAPI_Fuse(outer, _cyl((0, 0, 0), d, 20.0, 100.0)).Shape()
    fluid = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), 50.0).Shape()
    for d in ((1, 0, 0), (-1, 0, 0)):
        fluid = BRepAlgoAPI_Fuse(fluid, _cyl((0, 0, 0), d, 15.0, 100.0)).Shape()
    shell = BRepAlgoAPI_Cut(outer, fluid).Shape()
    w = STEPControl_Writer()
    w.Transfer(shell, STEPControl_AsIs)
    assert w.Write(str(path)) is not None
    return path


def test_chamber_to_bore_junctions_survive_the_undeclared_opening_seal(tmp_path):
    step = _housing_shell(tmp_path / "housing.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared())
    sealed = t["sealed"]["undeclared_openings"]
    assert sealed == [], (
        "chamber-to-bore junction holes were sealed as undeclared openings - the "
        f"chamber is walled off from its own ports: {sealed}")
    # both mouths still found and bound as ports
    assert len(t["openings"]) == 2, t["openings"]
