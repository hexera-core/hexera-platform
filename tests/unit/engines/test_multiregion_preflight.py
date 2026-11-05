# Responsibility: Verify a multi-region case is refused before any native command when its assembly cannot support one.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.snappy_multiregion.multiregion_runner import (
    _run_snappy_multiregion_local,
    assembly_preflight,
)


def _workspace(tmp_path, *, solids, regions=None, surfaces=None):
    ws = tmp_path / "ws"
    (ws / "_assembly").mkdir(parents=True, exist_ok=True)
    (ws / "_assembly" / "solids.json").write_text(json.dumps(
        [{"index": i, "stl": f"s{i}.stl", "volume": 1.0 + i} for i in range(solids)]))
    if regions is not None:
        (ws / ".assembly_plan.json").write_text(json.dumps({"expected_regions": list(regions)}))
        tri = ws / "constant" / "triSurface"
        tri.mkdir(parents=True, exist_ok=True)
        for name in (regions if surfaces is None else surfaces):
            (tri / f"{name}.stl").write_text("solid x\nendsolid x\n")
    return ws


def test_a_single_solid_source_is_an_engine_owned_incompatibility(tmp_path):
    ws = _workspace(tmp_path, solids=1, regions=["fluid", "solid"])
    problem = assembly_preflight(ws)
    assert problem is not None
    assert problem["code"] == "multiregion_requires_multiple_solids"
    assert problem["solids_found"] == 1
    assert "at least two" in problem["error"]


def test_the_refusal_happens_before_any_native_command(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr("meshpipeline.engines.snappy_multiregion.native.run_guarded",
                        lambda *a, **k: launched.append(a) or pytest.fail("a native command ran"))
    ws = _workspace(tmp_path, solids=1, regions=["fluid", "solid"])
    result = _run_snappy_multiregion_local(ws, timeout=5)
    assert launched == []
    assert result["rc"] == -2
    assert result["code"] == "multiregion_requires_multiple_solids"
    assert "REJECTED before blockMesh" in result["log_tail"]


def test_a_region_naming_geometry_that_is_not_staged_is_refused(tmp_path):
    ws = _workspace(tmp_path, solids=2, regions=["fluid", "does_not_exist"],
                    surfaces=["fluid"])
    problem = assembly_preflight(ws)
    assert problem["code"] == "multiregion_region_surface_missing"
    assert problem["missing_regions"] == ["does_not_exist"]


def test_duplicate_region_names_are_ambiguous_and_refused(tmp_path):
    ws = _workspace(tmp_path, solids=2, regions=["fluid", "fluid"])
    problem = assembly_preflight(ws)
    assert problem["code"] == "multiregion_region_names_ambiguous"
    assert problem["duplicate_regions"] == ["fluid"]


def test_a_workspace_that_was_never_configured_is_refused(tmp_path):
    ws = _workspace(tmp_path, solids=2)                # inventory, but no validated plan
    problem = assembly_preflight(ws)
    assert problem["code"] == "multiregion_region_coverage_invalid"
    assert "configure_mesh must accept a region map" in problem["error"]


def test_a_workspace_that_was_never_staged_is_refused(tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    problem = assembly_preflight(ws)
    assert problem["code"] == "multiregion_not_staged"


def test_one_region_is_not_a_conjugate_case(tmp_path):
    ws = _workspace(tmp_path, solids=2, regions=["fluid"])
    problem = assembly_preflight(ws)
    assert problem["code"] == "multiregion_requires_multiple_solids"


def test_a_valid_two_solid_assembly_passes_the_preflight(tmp_path):
    ws = _workspace(tmp_path, solids=2, regions=["fluid", "solid"])
    assert assembly_preflight(ws) is None


def test_the_refusal_names_no_path_or_workspace(tmp_path):
    ws = _workspace(tmp_path, solids=1, regions=["fluid", "solid"])
    problem = assembly_preflight(ws)
    blob = json.dumps(problem)
    assert str(tmp_path) not in blob
    assert "/" not in problem["error"]
