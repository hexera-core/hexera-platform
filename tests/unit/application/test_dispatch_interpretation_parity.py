# Responsibility: Verify the interpretation survives the payload round trip and no launcher reshapes it.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests._geometry_support import interpretation_ref

from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"


def _payload_with_interpretation():
    from meshpipeline.application.pipeline_run import JobRequest

    ref = interpretation_ref(geometry_source_id="src-1", unit=LengthUnit.inch,
                             basis=ResolutionBasis.user_confirmed)
    return JobRequest(job_id="j-1", owner_id="o-1", geometry_interpretation=ref.to_payload())


def test_the_interpretation_survives_the_dispatch_payload_round_trip():
    req = _payload_with_interpretation()

    assert req.geometry_interpretation is not None
    payload = req.geometry_interpretation.to_payload()
    assert payload["unit"] == "in"
    assert payload["scale_to_metres"] == 0.0254
    assert payload["basis"] == "user_confirmed"


def test_the_interpretation_carries_no_location_or_provider_detail():
    payload = _payload_with_interpretation().geometry_interpretation.to_payload()

    blob = repr(payload).lower()
    for leak in ("/", "bucket", "minio", "gcs", "s3", "http", "secret", "key=", "credential",
                 "cfmesh", "snappy", "gmsh", "vmtk"):
        assert leak not in blob, f"the interpretation payload leaks {leak!r}: {payload}"
    assert set(payload) == {"interpretation_id", "geometry_source_id", "unit",
                            "scale_to_metres", "basis", "evidence"}


def test_no_launcher_reshapes_the_dispatch_payload():
    launchers = [m for m in sorted((SRC / "adapters" / "pipeline_execution").glob("*.py"))
                 if m.name not in ("__init__.py", "celery_app.py", "maintenance_tasks.py")]
    assert launchers, "no pipeline launcher modules found - the scan subject is empty"

    for mod in launchers:
        tree = ast.parse(mod.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # a launcher must not touch units, scaling or coordinate state at all
        for forbidden in ("scale_to_metres", "LengthUnit", "PreparedCoordinates",
                          "prepare_surface", "from_occ_transfer", "from_source_file"):
            assert forbidden not in names | attrs, (
                f"{mod.name} reaches for {forbidden}; conversion must not depend on the backend")


@pytest.mark.parametrize("module", [
    "cad/staging.py", "cad/normalise.py", "cad/analysis.py",
    "contracts/coordinate_state.py", "contracts/geometry_units.py",
])
def test_no_conversion_module_branches_on_the_execution_backend(module):
    text = (SRC / module).read_text()
    for backend in ("MESH_BACKEND", "cloud_run", "CloudRun", "celery", "Celery"):
        assert backend not in text, f"{module} branches on {backend}"
