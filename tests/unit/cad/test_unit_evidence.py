# Responsibility: Verify a declared unit is resolved from real file evidence, and an ambiguous one asks the user.
from __future__ import annotations

import pytest
from tests.cad_fixtures import corrupt_step_unit, write_iges, write_step

from meshpipeline.cad.unit_evidence import OCC_OUTPUT_UNIT, read_declared_unit
from meshpipeline.contracts.geometry_units import LengthUnit


@pytest.mark.parametrize("declared,expected", [
    ("MM", LengthUnit.millimetre),
    ("CM", LengthUnit.centimetre),
    ("INCH", LengthUnit.inch),
])
def test_a_positively_declared_step_unit_is_resolved(tmp_path, declared, expected):
    ev = read_declared_unit(write_step(tmp_path / f"box_{declared}.step", declared))
    assert ev.resolved and ev.unit is expected
    assert "declared in the file" in ev.detail


def test_a_step_declaring_metres_is_not_trusted(tmp_path):
    ev = read_declared_unit(write_step(tmp_path / "box_m.step", "M"))
    assert not ev.resolved
    assert ev.unit is None


@pytest.mark.parametrize("how", ["malformed", "missing"])
def test_a_corrupted_step_unit_context_is_refused(tmp_path, how):
    good = write_step(tmp_path / "good.step", "MM")
    bad = corrupt_step_unit(good, tmp_path / f"{how}.step", how)
    assert not read_declared_unit(bad).resolved


def test_a_corrupted_file_still_reads_which_is_why_names_are_not_enough(tmp_path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    good = write_step(tmp_path / "good.step", "MM")
    bad = corrupt_step_unit(good, tmp_path / "bad.step", "malformed")
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(bad)) == IFSelect_RetDone, "the corrupt file was rejected"


@pytest.mark.parametrize("declared,expected", [
    ("MM", LengthUnit.millimetre), ("M", LengthUnit.metre), ("IN", LengthUnit.inch),
])
def test_iges_global_section_units_are_resolved(tmp_path, declared, expected):
    ev = read_declared_unit(write_iges(tmp_path / f"box_{declared}.iges", declared))
    assert ev.resolved and ev.unit is expected


def test_unitless_formats_declare_nothing(tmp_path):
    for name in ("shape.stl", "shape.vtp"):
        p = tmp_path / name
        p.write_text("not really geometry")
        ev = read_declared_unit(p)
        assert not ev.resolved and "does not record a unit" in ev.detail


def test_the_reader_output_unit_is_stated_not_assumed():
    assert OCC_OUTPUT_UNIT is LengthUnit.millimetre


def test_evidence_detail_carries_nothing_private(tmp_path):
    ev = read_declared_unit(write_step(tmp_path / "secret-name.step", "MM"))
    assert str(tmp_path) not in ev.detail and "secret-name" not in ev.detail


def test_an_iges_with_a_model_scale_requires_clarification(tmp_path):
    from tests.cad_fixtures import write_iges_with_scale

    plain = write_iges(tmp_path / "plain.iges", "MM")
    scaled = write_iges_with_scale(plain, tmp_path / "scaled.iges", "2.0")
    ev = read_declared_unit(scaled)
    assert not ev.resolved
    assert "model scale" in ev.detail


def test_more_than_one_representation_unit_cannot_auto_resolve(monkeypatch, tmp_path):
    import meshpipeline.cad.unit_evidence as ue

    good = write_step(tmp_path / "mm.step", "MM")

    class _TwoUnits:
        def ReadFile(self, _p):
            from OCP.IFSelect import IFSelect_RetDone
            return IFSelect_RetDone

        def FileUnits(self, lengths, _angles, _solids):
            from OCP.TCollection import TCollection_AsciiString
            lengths.Append(TCollection_AsciiString("millimetre"))
            lengths.Append(TCollection_AsciiString("centimetre"))

    monkeypatch.setattr(ue, "_step_reader", lambda: _TwoUnits(), raising=False)
    ev = ue._step_evidence_with_reader(_TwoUnits(), good)
    assert not ev.resolved
    assert "different length units" in ev.detail
