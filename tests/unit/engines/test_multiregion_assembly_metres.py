# Responsibility: Verify a multi-solid assembly lands at its physical size with region identity and interfaces intact.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.STEPControl")

from meshpipeline.contracts.coordinate_state import from_source_file  # noqa: E402
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
    scale_to_metres,
)
from meshpipeline.engines.snappy_multiregion import multiregion_runner as MR  # noqa: E402

#: Each box is EDGE units on a side; they touch on x = EDGE, sharing that whole face.
EDGE = 10.0

CASES = [
    (LengthUnit.metre, EDGE * 1.0),
    (LengthUnit.millimetre, EDGE * 1e-3),
    (LengthUnit.centimetre, EDGE * 1e-2),
    (LengthUnit.inch, EDGE * 0.0254),
]


def _assembly_step(path, declared: str):
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), EDGE, EDGE, EDGE).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(EDGE, 0, 0), EDGE, EDGE, EDGE).Shape()
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, a)
    builder.Add(comp, b)

    writer = STEPControl_Writer()          # constructed first: the unit static needs the controller
    assert Interface_Static.SetCVal_s("write.step.unit", declared)
    writer.Transfer(comp, STEPControl_AsIs)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer.Write(str(path))
    return path


def _prepared(unit: LengthUnit):
    return from_source_file(GeometryInterpretation(
        interpretation_id="i-1", owner_id="o-1", geometry_source_id="s-1",
        unit=unit, scale_to_metres=scale_to_metres(unit),
        basis=ResolutionBasis.user_confirmed))


_DECLARED = {LengthUnit.metre: "M", LengthUnit.millimetre: "MM",
             LengthUnit.centimetre: "CM", LengthUnit.inch: "INCH"}


def _solids(tmp_path, unit: LengthUnit):
    step = _assembly_step(tmp_path / f"asm-{unit.value}.step", _DECLARED[unit])
    return MR.read_assembly_solids(step, tmp_path / f"out-{unit.value}",
                                   prepared=_prepared(unit))


def _span(solids) -> float:
    lo = min(s["bbox_min"][0] for s in solids)
    hi = max(s["bbox_max"][0] for s in solids)
    return hi - lo


# physical size

@pytest.mark.parametrize("unit,edge_m", CASES)
def test_the_assembly_lands_at_its_true_physical_size(unit, edge_m, tmp_path):
    solids = _solids(tmp_path, unit)

    assert len(solids) == 2, f"expected 2 solids, got {len(solids)}"
    assert _span(solids) == pytest.approx(2 * edge_m, rel=1e-4)


@pytest.mark.parametrize("unit,edge_m", CASES)
def test_each_solid_volume_is_physical(unit, edge_m, tmp_path):
    solids = _solids(tmp_path, unit)

    for s in solids:
        assert s["volume"] == pytest.approx(edge_m ** 3, rel=1e-4), (
            f"a {EDGE}-{unit.value} box should be {edge_m ** 3} m^3")


def test_equivalent_assemblies_in_different_units_agree_physically(tmp_path):
    spans = {}
    for unit, edge_m in CASES:
        spans[unit] = _span(_solids(tmp_path, unit)) / edge_m      # normalised by its own edge
    for unit, ratio in spans.items():
        assert ratio == pytest.approx(2.0, rel=1e-4), unit


# topology survives

@pytest.mark.parametrize("unit,edge_m", CASES)
def test_solid_count_and_region_identity_survive(unit, edge_m, tmp_path):
    solids = _solids(tmp_path, unit)

    assert [s["index"] for s in solids] == [0, 1]
    for s in solids:
        assert s["stl"].endswith(".stl")
        assert s["bbox_max"][0] - s["bbox_min"][0] == pytest.approx(edge_m, rel=1e-4)


@pytest.mark.parametrize("unit,edge_m", CASES)
def test_the_shared_interface_stays_conformal_and_lands_in_metres(unit, edge_m, tmp_path):
    solids = sorted(_solids(tmp_path, unit), key=lambda s: s["bbox_min"][0])
    left, right = solids

    assert left["bbox_max"][0] == pytest.approx(edge_m, rel=1e-4)
    assert right["bbox_min"][0] == pytest.approx(edge_m, rel=1e-4)
    # the interface is shared: the left box ends exactly where the right one begins
    assert left["bbox_max"][0] == pytest.approx(right["bbox_min"][0], rel=1e-6)
    # and they still overlap fully in the other two axes - the face is whole, not a corner touch
    for axis in (1, 2):
        assert left["bbox_min"][axis] == pytest.approx(right["bbox_min"][axis], abs=edge_m * 1e-4)
        assert left["bbox_max"][axis] == pytest.approx(right["bbox_max"][axis], abs=edge_m * 1e-4)


# what it refuses

def test_reading_an_assembly_without_coordinate_state_is_refused(tmp_path):
    step = _assembly_step(tmp_path / "asm.step", "MM")

    with pytest.raises(ValueError, match="coordinate state"):
        MR.read_assembly_solids(step, tmp_path / "out", prepared=None)


def test_the_bundle_takes_its_state_from_the_tool_context_only(tmp_path):
    with pytest.raises(ValueError, match="execution geometry"):
        MR._prepared_from(None)

    class _Ctx:
        geometry = None
    with pytest.raises(ValueError, match="execution geometry"):
        MR._prepared_from(_Ctx())
