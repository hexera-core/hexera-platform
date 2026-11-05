# Responsibility: Verify CAD repair inspection dispatches from the materialized geometry contract.
from __future__ import annotations

import pytest
from tests._geometry_support import materialized
from tests.cad_fixtures import write_step, write_stl

from meshpipeline.cad.repair.contracts import RepairStatus, RepairTarget
from meshpipeline.cad.repair.inspect import inspect_geometry
from meshpipeline.contracts.geometry_units import LengthUnit


def test_inspect_geometry_reports_source_and_interpretation_identity_for_stl(tmp_path):
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.stl")
    write_stl(geom.local_path)

    result = inspect_geometry(geom, target=RepairTarget.meshing, engine="snappy")
    payload = result.to_dict()

    assert result.status is RepairStatus.clean
    assert payload["input"]["source_id"] == geom.ref.source_id
    assert payload["input"]["interpretation_id"] == geom.interpretation.interpretation_id
    assert payload["input"]["suffix"] == ".stl"
    assert payload["policy"]["target"] == "meshing"
    assert payload["policy"]["engine"] == "snappy"
    assert payload["output"] is None


def test_inspect_geometry_dispatches_step(tmp_path):
    pytest.importorskip("OCP.STEPControl")
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.step")
    write_step(geom.local_path, "MM")

    result = inspect_geometry(geom)

    assert result.status is RepairStatus.clean
    measurements = {m.name: m.value for m in result.report.measurements}
    assert measurements["format"] == "step"


def test_unsupported_suffix_is_unrepairable_report(tmp_path):
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.obj")
    geom.local_path.write_text("not supported")

    result = inspect_geometry(geom)

    assert result.status is RepairStatus.unrepairable
    assert result.report.defects[0].code.value == "engine_staging_failure"
