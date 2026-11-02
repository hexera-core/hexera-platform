# Responsibility: Prove the client-demo fixture selects geometry by measurements and that the
# real Formula profile carries exactly the three localized errors advertised in the recording.
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.cad_fixtures import write_step

from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity

pytest.importorskip("OCP.STEPControl", reason="the CAD kernel is a native-tier dependency")


def _scan(path: Path):
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan

    return localize_scan(_read_shape(path), face_scan_s=0.0)


def test_builder_removes_the_face_selected_by_geometry_after_step_round_trip(tmp_path):
    from tools.demo.build_crs_formula_fixture import FaceSelector, build_fixture

    source = write_step(tmp_path / "box.step", "MM")
    output = tmp_path / "broken.step"
    manifest = tmp_path / "broken.json"
    selector = FaceSelector(
        name="left_panel",
        centroid_mm=(0.0, 5.0, 5.0),
        area_mm2=100.0,
        centroid_tolerance_mm=0.01,
        area_tolerance_mm2=0.01,
    )

    build_fixture(source, output, manifest, selectors=(selector,))

    defects, complete = _scan(output)
    openings = [d for d in defects if d.code is DefectCode.open_shell]
    assert complete and len(openings) == 1
    written = json.loads(manifest.read_text())
    assert len(written["defects"]) == 1
    recorded = written["defects"][0]
    assert {key: recorded[key] for key in ("name", "code", "severity")} == {
        "name": "left_panel", "code": "open_shell", "severity": "error",
    }
    assert recorded["area_mm2"] == pytest.approx(100.0)
    assert recorded["centroid_mm"] == pytest.approx([0.0, 5.0, 5.0])


def test_formula_profile_creates_three_localized_open_shell_errors(tmp_path):
    baseline = os.getenv("CRS_FORMULA_BASELINE", "")
    if not baseline:
        pytest.skip("set CRS_FORMULA_BASELINE to exercise the downloaded Formula source")

    from tools.demo.build_crs_formula_fixture import build_formula_fixture

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure

    output = tmp_path / "formula-broken.step"
    manifest = tmp_path / "formula-broken.json"
    build_formula_fixture(Path(baseline), output, manifest)

    defects, complete = _scan(output)
    openings = [d for d in defects
                if d.code is DefectCode.open_shell and d.severity is DefectSeverity.error]
    assert complete and len(openings) == 3
    assert measure(_read_shape(output)).diagonal_mm > 4_000.0
    written = json.loads(manifest.read_text())
    assert written["vehicle"] == "Formula 1000 CFD body"
    assert written["removed_prior_demo_solid"] is True
    assert [d["name"] for d in written["defects"]] == [
        "floor_panel_starboard", "floor_panel_port", "diffuser_floor_tab",
    ]


def test_formula_profile_repairs_all_three_errors_inside_the_shipped_operation_budget(
        tmp_path, monkeypatch):
    baseline = os.getenv("CRS_FORMULA_BASELINE", "")
    if not baseline:
        pytest.skip("set CRS_FORMULA_BASELINE to exercise the downloaded Formula source")

    from tools.demo.build_crs_formula_fixture import build_formula_fixture

    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import cumulative_refusal, measure, sanity_refusal
    from meshpipeline.cad.repair.conservative import repair_step_file

    for name, value in (
        ("CAD_REPAIR_ENABLED", True),
        ("CAD_REPAIR_FILL_PLANAR_HOLES", True),
        ("CAD_REPAIR_FIX_SMALL_FACES", False),
        ("CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False),
        ("CAD_REPAIR_REVIEW_ENABLED", False),
        ("CAD_REPAIR_MAX_OPS", 6),
        ("CAD_REPAIR_STALL_LIMIT", 2),
        ("CAD_REPAIR_TIMEOUT_S", 120.0),
    ):
        monkeypatch.setattr(rcfg, name, value)

    source = tmp_path / "formula-broken.step"
    repaired = tmp_path / "formula-repaired.step"
    build_formula_fixture(Path(baseline), source, tmp_path / "formula-broken.json")
    before = measure(_read_shape(source))

    report = repair_step_file(source, repaired)

    kept = [operation for operation in report.operations if operation["status"] == "kept"]
    assert [operation["name"] for operation in kept] == ["fill_planar_hole"] * 3
    assert len(report.operations) <= 6
    defects, complete = _scan(repaired)
    assert complete
    assert not [d for d in defects if d.severity is DefectSeverity.error]
    after = measure(_read_shape(repaired))
    declared_area = sum(operation["evidence"]["area_delta_mm2"] for operation in kept)
    assert sanity_refusal(before, after) == ""
    assert cumulative_refusal(before, after, declared_area_mm2=declared_area,
                              declared_volume_mm3=0.0) == ""
