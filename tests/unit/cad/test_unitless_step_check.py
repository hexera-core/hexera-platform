# Responsibility: Verify a STEP file whose unit is missing, spelled only by its exporter
# ('__CONSTANT UNIT inch', Inventor) or mixed across its parts never crashes the geometry check:
# the mesher's deflection follows the measured size with a floor OpenCascade accepts, the parser's
# applied unit is what OCC really applied, and the unit goes to the proposal.
# Boundaries: real OpenCASCADE files crafted from small boxes; no object store, no model.
from __future__ import annotations

import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.STEPControl")

from tests.cad_fixtures import write_step_of_units  # noqa: E402

from meshpipeline.cad import occ_box  # noqa: E402
from meshpipeline.cad.unit_evidence import (  # noqa: E402
    parser_applied_unit,
    read_declared_unit,
    stated_mostly,
)
from meshpipeline.contracts.geometry_units import LengthUnit  # noqa: E402

_UNITS = re.compile(r"GLOBAL_UNIT_ASSIGNED_CONTEXT\s*\(\s*\([^)]*\)\s*\)")
_REF = SimpleNamespace(owner_id="o", source_id="s")


def _unitless(tmp_path: Path, side: float = 10.0) -> Path:
    """A box `side` units across whose geometric context assigns no unit at all."""
    good = write_step_of_units(tmp_path / "with_unit.step", "MM", side)
    text = good.read_text(errors="replace")
    stripped = _UNITS.sub("", text)
    assert stripped != text and "GLOBAL_UNIT_ASSIGNED_CONTEXT" not in stripped
    out = tmp_path / "unitless.step"
    out.write_text(stripped)
    return out


def _inventor_inch(tmp_path: Path, side: float = 2.0) -> Path:
    """An inch box whose unit carries Inventor's own name for the inch."""
    good = write_step_of_units(tmp_path / "inch.step", "INCH", side)
    text = good.read_text(errors="replace")
    assert "CONVERSION_BASED_UNIT('INCH'" in text
    out = tmp_path / "inventor.stp"
    out.write_text(text.replace("CONVERSION_BASED_UNIT('INCH'", "CONVERSION_BASED_UNIT('__CONSTANT UNIT inch'"))
    return out


def _mixed(tmp_path: Path) -> Path:
    """The Inventor inch box plus two more contexts - one inch, one millimetre - as an assembly
    whose parts are drawn mostly in inches and once in millimetres."""
    src = _inventor_inch(tmp_path)
    text = src.read_text(errors="replace")
    extra = ("#900001=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
             "#900002=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNIT_ASSIGNED_CONTEXT((#900001))"
             "REPRESENTATION_CONTEXT('mm part','3D'));\n"
             "#900003=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#900001);\n"
             "#900004=(CONVERSION_BASED_UNIT('__CONSTANT UNIT inch',#900003)LENGTH_UNIT()NAMED_UNIT(*));\n"
             "#900005=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNIT_ASSIGNED_CONTEXT((#900004))"
             "REPRESENTATION_CONTEXT('inch part','3D'));\n")
    out = tmp_path / "mixed.stp"
    out.write_text(text.replace("ENDSEC;\nEND-ISO", extra + "ENDSEC;\nEND-ISO"))
    assert "#900005" in out.read_text()
    return out


def _box(side: float):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(side, side, side).Shape()


# ---------------------------------------------------------------- the mesher's parameters ----

@pytest.mark.parametrize("bad", [0.0, -1.0, 1e-12, 3e-8, float("nan"), float("inf"), None])
def test_a_deflection_that_is_no_length_never_reaches_the_mesher(bad):
    """3e-8 is what a 50 mm hub read as 50 um asked for: OpenCascade raised
    'BRepMesh_IncrementalMesh::initParameters : invalid parameter value' and the check failed."""
    shape = _box(5e-5)
    lin = occ_box.incremental_mesh(shape, bad, 0.3)
    assert math.isfinite(lin) and lin >= occ_box.MIN_LINEAR_DEFLECTION
    assert occ_box.surface_box(shape) is not None


@pytest.mark.parametrize("angle", [0.0, -0.5, float("nan"), 1e-15])
def test_an_angle_that_is_no_angle_never_reaches_the_mesher(angle):
    shape = _box(10.0)
    occ_box.incremental_mesh(shape, 0.01, angle)
    assert occ_box.surface_box(shape) is not None


def test_a_part_read_at_micrometres_is_still_measured():
    """mesh_to_size on a part 50 um across (the old reading of the unit-mixed Inventor hub)
    meshes at the floor and reads its real box."""
    box, lin = occ_box.mesh_to_size(_box(5e-5), 1.0 / 2500.0, 0.3)
    assert lin == occ_box.MIN_LINEAR_DEFLECTION
    assert box[3] - box[0] == pytest.approx(5e-5, rel=1e-3)


def test_an_explicit_deflection_that_is_no_length_falls_back_to_the_size():
    box, lin = occ_box.mesh_to_size(_box(10.0), 1.0 / 1000.0, 0.3, linear_deflection=0.0)
    assert lin == pytest.approx(math.sqrt(3) * 10.0 / 1000.0, rel=1e-6)
    assert box[3] - box[0] == pytest.approx(10.0, rel=1e-6)


def test_the_floor_never_lowers_a_sound_deflection():
    assert occ_box.deflection_for(100.0, 1e-3) == pytest.approx(0.1)
    assert occ_box.deflection_for(0.0, 1e-3) == occ_box.MIN_LINEAR_DEFLECTION
    assert occ_box.deflection_for(float("nan"), 1e-3) == occ_box.MIN_LINEAR_DEFLECTION


def test_every_mesh_call_goes_through_the_guarded_helper():
    """No intake, check or stage module calls BRepMesh_IncrementalMesh itself."""
    src = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
    offenders = [str(p.relative_to(src)) for p in src.rglob("*.py")
                 if p.name != "occ_box.py" and "BRepMesh_IncrementalMesh(" in p.read_text(errors="replace")]
    assert offenders == []


# ------------------------------------------------------------------- what the parser applied ----

def test_a_step_with_no_unit_keeps_its_numbers_as_millimetres(tmp_path):
    """OpenCASCADE applies no conversion to a file that names no length unit; reporting its metre
    fallback read a 10-unit box as 10 um."""
    path = _unitless(tmp_path)
    assert not read_declared_unit(path).resolved
    assert parser_applied_unit(path) is LengthUnit.millimetre


def test_an_inch_spelled_by_its_exporter_is_read_as_inches(tmp_path):
    path = _inventor_inch(tmp_path)
    ev = read_declared_unit(path)
    assert ev.resolved and ev.unit is LengthUnit.inch
    assert parser_applied_unit(path) is LengthUnit.inch


def test_a_file_mixing_units_is_read_in_the_unit_most_parts_state(tmp_path):
    path = _mixed(tmp_path)
    assert not read_declared_unit(path).resolved            # still asked: the user decides
    mostly = stated_mostly(path)
    assert mostly is not None and mostly.unit is LengthUnit.inch
    assert "inches for most of its parts" in mostly.detail
    assert parser_applied_unit(path) is LengthUnit.inch


def test_a_tie_between_units_names_no_majority(tmp_path):
    good = write_step_of_units(tmp_path / "mm.step", "MM", 10.0)
    text = good.read_text(errors="replace")
    extra = ("#900001=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.CENTI.,.METRE.));\n"
             "#900002=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNIT_ASSIGNED_CONTEXT((#900001))"
             "REPRESENTATION_CONTEXT('two','3D'));\n")
    two = tmp_path / "two.step"
    two.write_text(text.replace("ENDSEC;\nEND-ISO", extra + "ENDSEC;\nEND-ISO"))
    assert stated_mostly(two) is None


# --------------------------------------------------------------------------- the check itself ----

def test_the_check_reads_a_small_unitless_part_and_proposes_a_unit(tmp_path):
    """The crash: a unit-less part read at a scale a millionth of its own, meshed at a deflection
    OpenCascade refuses. Now: measured at its size, the unit assumed and said, and proposed."""
    from meshpipeline.application import geometry_check as gc

    path = _unitless(tmp_path, side=10.0)
    facts, _skin = gc._scout_exact(path, tmp_path, None, _REF)
    assert facts["unit_assumed"] is True
    assert facts["size_mm"] == pytest.approx([10.0, 10.0, 10.0], rel=1e-3)
    assert any("does not say its unit" in n for n in facts["notes"])
    proposal = {"size_mm": facts["size_mm"], "scale_to_m": facts["scale_to_m"], "flow": ""}
    inferred = gc.unit_inference(proposal, facts)
    assert inferred is not None and inferred["unit"] in {u.value for u in LengthUnit}


def test_the_check_reads_a_mixed_inventor_assembly_at_its_real_size(tmp_path):
    """The pod hubs: inch parts and a millimetre one, the inch spelled '__CONSTANT UNIT inch'.
    The check reads the part at its real size and proposes inches, surely."""
    from meshpipeline.application import geometry_check as gc

    path = _mixed(tmp_path)
    facts, _skin = gc._scout_exact(path, tmp_path, None, _REF)
    assert facts["unit_assumed"] is True
    assert facts["unit_stated_mostly"] == "in"
    assert facts["size_mm"] == pytest.approx([50.8, 50.8, 50.8], rel=1e-3)   # 2 in
    proposal = {"size_mm": facts["size_mm"], "scale_to_m": facts["scale_to_m"], "flow": ""}
    inferred = gc.unit_inference(proposal, facts)
    assert inferred["unit"] == "in" and inferred["confidence"] == "high"
    # the FEA path reads it as a part, never as a body in a flow
    fea = gc.unit_inference({**proposal, "flow": "external"}, {**facts, "fea_measure": {"measured": True}})
    assert fea["what"] == "a part"


def test_the_cfd_checks_tessellate_a_tiny_part(tmp_path):
    """The same guard on the CFD side: the touching-parts reading and the clean check's crossings
    search, given a part whose measured size is zero or microscopic, never raise."""
    from meshpipeline.cad.fluid_volume import touching_parts
    from meshpipeline.cad.validity_brep import _crossings

    a, b = _box(5e-5), _box(5e-5)
    assert touching_parts([a, b], 0.0) in (True, False)
    findings: list = []
    _crossings(_box(5e-5), 0.0, findings, {})
    assert not any(f.code == "not_verified" for f in findings)
