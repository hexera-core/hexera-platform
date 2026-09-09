# Responsibility: Verify the internal carve seeds an annular FLUID solid inside the fluid, never in
# the hole it surrounds, and refuses a declared fluid domain it cannot seed.
# Blade-row passages 001/003/005 (jobs 9bf37dd8, f69eb843, db9e64e1) were "delivered" as a mesh of
# the hub bore: the volume centroid and both ring-port centroids sit on the axis, the port caps
# sealed the bore at both ends, the hollow-wall fallback accepted that void point, and
# snappyHexMesh kept the bore. Passages 002/004 kept the exterior the same way.
import math

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
    from OCP.TopAbs import TopAbs_IN
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.cad.cad_tessellate import tessellate_internal
from meshpipeline.contracts.coordinate_state import from_occ_transfer
from meshpipeline.contracts.geometry_units import (
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)

R_OUT, R_IN, L = 226.65, 134.44, 232.0        # mm: blade_row_passage_001's annulus, no blades


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _cyl(base, direction, r, length):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*direction)), r, length).Shape()


def _annular_fluid(path):
    """The FLUID of an annular passage: outer cylinder minus the hub. Both end faces are rings."""
    shape = BRepAlgoAPI_Cut(_cyl((0, 0, 0), (1, 0, 0), R_OUT, L),
                            _cyl((-1, 0, 0), (1, 0, 0), R_IN, L + 2)).Shape()
    w = STEPControl_Writer(); w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(str(path)) is not None
    return path


def _declared():
    area = math.pi * ((R_OUT / 1000.0) ** 2 - (R_IN / 1000.0) ** 2)
    return [{"name": "inlet", "area_m2": area, "near_m": (0.0, 0.0, 0.0), "d_m": 2 * R_OUT / 1000.0},
            {"name": "outlet", "area_m2": area, "near_m": (L / 1000.0, 0.0, 0.0),
             "d_m": 2 * R_OUT / 1000.0}]


def _inside_solid(step, p_m):
    r = STEPControl_Reader(); r.ReadFile(str(step)); r.TransferRoots()
    c = BRepClass3d_SolidClassifier(r.OneShape())
    c.Perform(gp_Pnt(p_m[0] * 1000.0, p_m[1] * 1000.0, p_m[2] * 1000.0), 1e-7)
    return c.State() == TopAbs_IN


def test_an_annular_fluid_solid_is_seeded_on_the_ring_not_in_the_hub_bore(tmp_path):
    step = _annular_fluid(tmp_path / "annulus.step")
    t = tessellate_internal(step, tmp_path / "stls", prepared=_prepared(),
                            declared_ports=_declared(), fluid_solid=True)
    x, y, z = t["interior_point"]
    r_mm = math.hypot(y, z) * 1000.0
    assert R_IN < r_mm < R_OUT, f"seed at radius {r_mm:.1f} mm is not between hub and shroud"
    assert 0.0 < x * 1000.0 < L
    assert _inside_solid(step, (x, y, z)), "the seed must be inside the fluid solid"


def test_without_the_declaration_the_ring_candidates_still_win(tmp_path):
    # the ring candidates are tried before the hollow-wall fallback, so even an undeclared kind
    # no longer seeds the bore of a fluid annulus
    step = _annular_fluid(tmp_path / "annulus2.step")
    t = tessellate_internal(step, tmp_path / "stls2", prepared=_prepared(),
                            declared_ports=_declared())
    x, y, z = t["interior_point"]
    assert R_IN < math.hypot(y, z) * 1000.0 < R_OUT


def test_a_declared_fluid_domain_that_cannot_be_seeded_is_refused(tmp_path, monkeypatch):
    # force every candidate to fail the inside test: a fluid domain must never fall back to void
    from OCP import BRepClass3d as _B

    import meshpipeline.cad.cad_tessellate as T

    class _Never:
        def __init__(self, *a, **k): pass
        def Perform(self, *a, **k): pass
        def State(self): return None
    monkeypatch.setattr(_B, "BRepClass3d_SolidClassifier", _Never)
    step = _annular_fluid(tmp_path / "annulus3.step")
    with pytest.raises(RuntimeError, match="declared fluid domain"):
        T.tessellate_internal(step, tmp_path / "stls3", prepared=_prepared(),
                              declared_ports=_declared(), fluid_solid=True)
