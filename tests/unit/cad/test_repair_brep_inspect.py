# Responsibility: Verify STEP/IGES repair inspection reports B-rep validity without mutation.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_iges, write_step

pytest.importorskip("OCP.STEPControl")

from meshpipeline.cad.repair.brep import inspect_brep_file  # noqa: E402
from meshpipeline.cad.repair.contracts import DefectCode  # noqa: E402


def _measurements(report):
    return {m.name: m.value for m in report.measurements}


def _codes(report):
    return {d.code for d in report.defects}


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


def test_valid_iges_reports_shape_counts(tmp_path):
    path = write_iges(tmp_path / "box.iges", "MM")

    report = inspect_brep_file(path)

    measurements = _measurements(report)
    assert measurements["format"] == "iges"
    assert measurements["is_valid"] is True
    assert measurements["faces"] >= 6


def test_unreadable_cad_is_reported_as_invalid_brep(tmp_path):
    path = tmp_path / "broken.step"
    path.write_text("not a STEP file")

    report = inspect_brep_file(path)

    assert DefectCode.invalid_brep in _codes(report)
    assert _measurements(report)["is_valid"] is False
