# Responsibility: Verify STEP/IGES repair inspection reports B-rep validity without mutation.
from __future__ import annotations

import importlib
import sys
import types

import pytest
from tests.cad_fixtures import write_iges, write_step

from meshpipeline.cad.repair import brep
from meshpipeline.cad.repair.brep import inspect_brep_file
from meshpipeline.cad.repair.contracts import DefectCode


def _has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


OCP_AVAILABLE = _has_module("OCP.STEPControl")
requires_ocp = pytest.mark.skipif(not OCP_AVAILABLE, reason="OCP.STEPControl unavailable")


def _measurements(report):
    return {m.name: m.value for m in report.measurements}


def _codes(report):
    return {d.code for d in report.defects}


def test_brep_module_imports_without_ocp():
    assert callable(inspect_brep_file)


def test_unexpected_inspection_error_propagates(monkeypatch, tmp_path):
    path = tmp_path / "box.step"
    path.write_text("fixture")
    monkeypatch.setattr(brep, "_read_shape", lambda _path: object())
    monkeypatch.setattr(
        brep,
        "_count_subshapes",
        lambda _shape: (_ for _ in ()).throw(RuntimeError("programmer error")),
    )

    with pytest.raises(RuntimeError, match="programmer error"):
        inspect_brep_file(path)


def test_transfer_failure_is_reported_as_invalid_brep(monkeypatch, tmp_path):
    class Reader:
        def ReadFile(self, _path):
            return 1

        def TransferRoots(self):
            return 0

        def OneShape(self):  # pragma: no cover - transfer failure should stop earlier
            raise AssertionError("OneShape should not be called")

    _install_fake_ocp(monkeypatch, reader=Reader())
    path = tmp_path / "empty.step"
    path.write_text("fixture")

    with pytest.raises(brep._CadReadError, match="transfer roots"):
        brep._read_shape(path)

    report = inspect_brep_file(path)

    assert DefectCode.invalid_brep in _codes(report)
    assert _measurements(report)["is_valid"] is False
    assert report.summary == "Automatic repair was not safe for this geometry."


def test_null_shape_is_reported_as_invalid_brep(monkeypatch, tmp_path):
    class NullShape:
        def IsNull(self):
            return True

    class Reader:
        def ReadFile(self, _path):
            return 1

        def TransferRoots(self):
            return 1

        def OneShape(self):
            return NullShape()

    _install_fake_ocp(monkeypatch, reader=Reader())
    path = tmp_path / "empty.step"
    path.write_text("fixture")

    with pytest.raises(brep._CadReadError, match="empty shape"):
        brep._read_shape(path)

    report = inspect_brep_file(path)

    assert DefectCode.invalid_brep in _codes(report)
    assert _measurements(report)["is_valid"] is False


def _install_fake_ocp(monkeypatch, reader):
    step = types.ModuleType("OCP.STEPControl")
    step.STEPControl_Reader = lambda: reader
    iges = types.ModuleType("OCP.IGESControl")
    iges.IGESControl_Reader = lambda: reader
    ifselect = types.ModuleType("OCP.IFSelect")
    ifselect.IFSelect_RetDone = 1
    monkeypatch.setitem(sys.modules, "OCP.STEPControl", step)
    monkeypatch.setitem(sys.modules, "OCP.IGESControl", iges)
    monkeypatch.setitem(sys.modules, "OCP.IFSelect", ifselect)


@requires_ocp
def test_valid_step_reports_shape_counts_and_no_mutation(tmp_path):
    path = write_step(tmp_path / "box.step", "MM")
    before = path.read_bytes()

    report = inspect_brep_file(path)

    assert path.read_bytes() == before
    assert report.summary == "No repair needed."
    assert _codes(report) == set()
    measurements = _measurements(report)
    assert measurements["format"] == "step"
    assert measurements["is_valid"] is True
    assert measurements["faces"] >= 6
    assert measurements["edges"] >= 12


@requires_ocp
def test_valid_iges_reports_shape_counts(tmp_path):
    path = write_iges(tmp_path / "box.iges", "MM")

    report = inspect_brep_file(path)

    measurements = _measurements(report)
    assert measurements["format"] == "iges"
    assert measurements["is_valid"] is True
    assert measurements["faces"] >= 6


@requires_ocp
def test_unreadable_cad_is_reported_as_invalid_brep(tmp_path):
    path = tmp_path / "broken.step"
    path.write_text("not a STEP file")

    report = inspect_brep_file(path)

    assert DefectCode.invalid_brep in _codes(report)
    assert _measurements(report)["is_valid"] is False


def test_missing_cad_file_raises_rather_than_reporting_a_defect(tmp_path):
    with pytest.raises(FileNotFoundError):
        inspect_brep_file(tmp_path / "absent.step")


def test_kernel_exception_is_reported_not_raised(monkeypatch, tmp_path):
    class Standard_Failure(Exception):  # noqa: N801 - mirrors the OCC class name
        pass

    standard = types.ModuleType("OCP.Standard")
    standard.Standard_Failure = Standard_Failure
    monkeypatch.setitem(sys.modules, "OCP.Standard", standard)
    monkeypatch.setattr(
        brep,
        "_read_shape",
        lambda _path: (_ for _ in ()).throw(Standard_Failure("corrupt entity")),
    )
    path = tmp_path / "raises.step"
    path.write_text("fixture")

    report = inspect_brep_file(path)

    assert _codes(report) == {DefectCode.invalid_brep}
    assert report.defects[0].details["error"] == "Standard_Failure"
    assert report.summary == "Automatic repair was not safe for this geometry."


def test_shape_without_faces_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(brep, "_read_shape", lambda _path: object())
    monkeypatch.setattr(
        brep,
        "_count_subshapes",
        lambda _shape: {
            "solids": 0,
            "shells": 0,
            "faces": 0,
            "edges": 12,
            "vertices": 8,
        },
    )
    monkeypatch.setattr(brep, "_is_valid", lambda _shape: True)
    path = tmp_path / "wireframe.step"
    path.write_text("fixture")

    report = inspect_brep_file(path)

    assert _codes(report) == {DefectCode.invalid_brep}
    assert report.defects[0].details["reason"] == "no_faces"
    assert report.summary == "Automatic repair was not safe for this geometry."
    assert _measurements(report)["edges"] == 12
