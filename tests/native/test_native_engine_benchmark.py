# Responsibility: Evaluate CAD repair and downstream meshing on the same curated native models.
# Boundaries: real OpenCASCADE + real gmsh, inside the mesh image; no model calls or service stack.
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from tests.cad_fixtures import (
    write_loose_faces_step,
    write_missing_bore_wall_step,
    write_step,
)

import meshpipeline.settings.cad_repair as rcfg

pytestmark = pytest.mark.native


@pytest.fixture(autouse=True)
def _opted_in_to_conservative_repair(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)


def _benchmark_module():
    path = Path("/srv/devtools/validation/mesh_benchmark.py")
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / "devtools" / "validation" / "mesh_benchmark.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("native_engine_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _geometry(source: Path):
    from meshpipeline.contracts.geometry_source import (
        GeometryInterpretationRef,
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )

    digest, size = sha256_of(source)
    source_id = f"benchmark-{source.stem}"
    return MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id=source_id, owner_id="native-benchmark",
            object_key=f"benchmarks/{source.name}", sha256=digest, size_bytes=size,
            original_filename=source.name, suffix_hint=source.suffix),
        interpretation=GeometryInterpretationRef(
            interpretation_id=f"{source_id}-mm", geometry_source_id=source_id,
            unit="mm", scale_to_metres=0.001, basis="user_confirmed"),
        local_path=source)


def _counts(path: Path) -> dict:
    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(path)
    measured = {item.name: item.value for item in report.measurements}
    return {key: measured.get(key) for key in ("solids", "shells", "faces", "edges", "vertices")}


def _repair(source: Path, destination: Path) -> dict:
    from meshpipeline.cad.repair.conservative import RepairRefused, repair_step_file

    before = _counts(source)
    try:
        report = repair_step_file(source, destination)
    except RepairRefused as exc:
        return {"status": "refused", "reason": str(exc), "counts_before": before}
    measured = {item.name: item.value for item in report.measurements}
    return {
        "status": "repaired",
        "artifact": destination,
        "counts_before": before,
        "counts_after": _counts(destination),
        "operations_kept": int(measured.get("operations_kept", 0) or 0),
    }


def _mesh(source: Path, engine: str, workspace: Path) -> dict:
    assert engine == "gmsh", f"this curated downstream case does not apply to {engine}"
    from meshpipeline.cad.staging import prepare_surface
    from meshpipeline.engines.dispatch import run_engine_local
    from meshpipeline.engines.gmsh.gmsh_runner import finalize

    prepare_surface(_geometry(source), workspace / "input.stl", engine="gmsh")
    (workspace / "dimensionality").write_text("3D\n")
    (workspace / "gmsh_spec.json").write_text(json.dumps({
        "dimensionality": "3D", "element_order": "1"}))
    native = run_engine_local(workspace, engine="gmsh", timeout=300)
    judged = finalize(str(workspace), intake_patches=[], engine="gmsh",
                      domain="curated CAD downstream")
    manifest_path = workspace / "mesh_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    quality = dict(manifest.get("quality") or {})
    quality["cells"] = manifest.get("cell_count")
    return {
        "status": "success" if judged.get("success") else
                  f"failed: {judged.get('output') or native.get('log_tail') or 'native refusal'}",
        "selected_engine": "gmsh",
        "mesh_statistics": quality,
        "native_rc": native.get("rc"),
        "deliverables": [str(manifest_path)] if manifest_path.exists() else [],
    }


def _write_evidence(rows: list[dict], name: str = "curated_cad_repair_to_mesh") -> None:
    directory = os.environ.get("BENCHMARK_EVIDENCE_DIR")
    if not directory:
        return
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps(rows, indent=2, default=str))


def test_curated_cad_repair_to_mesh_matrix(tmp_path):
    benchmark = _benchmark_module()
    loose = write_loose_faces_step(tmp_path / "loose-faces.step")
    clean = write_step(tmp_path / "clean-box.step", "MM")
    passage = write_missing_bore_wall_step(tmp_path / "intentional-passage.step")
    cases = [
        benchmark.EngineBenchmarkCase("repairable-loose-faces", loose, ("gmsh",)),
        benchmark.EngineBenchmarkCase("clean-control", clean, ("gmsh",)),
        benchmark.EngineBenchmarkCase("intentional-passage", passage, ("gmsh",)),
    ]

    rows = benchmark.run_engine_matrix(
        cases, tmp_path / "benchmark-runs", repair=_repair, mesh=_mesh)
    # Evidence before assertions: a run that fails an expectation is exactly the run whose numbers
    # someone wants to read. Absent the env var the benchmark stays a plain pass/fail test.
    _write_evidence(rows)
    by_case_variant = {(row["case"], row["variant"]): row for row in rows}

    original = by_case_variant[("repairable-loose-faces", "original")]
    repaired = by_case_variant[("repairable-loose-faces", "repaired")]
    assert original["status"].startswith("failed:")
    assert repaired["status"] == "success"
    assert repaired["mesh_statistics"]["cells"] > 0
    assert repaired["cad_repair"]["counts_before"]["solids"] == 0
    assert repaired["cad_repair"]["counts_after"]["solids"] == 1
    assert repaired["cad_repair"]["operations_kept"] >= 1

    assert by_case_variant[("clean-control", "original")]["status"] == "success"
    assert ("clean-control", "repaired") not in by_case_variant
    assert by_case_variant[("clean-control", "original")]["cad_repair"]["status"] == "refused"

    safety = by_case_variant[("intentional-passage", "original")]
    assert safety["cad_repair"]["status"] == "refused"
    assert ("intentional-passage", "repaired") not in by_case_variant


# --- cross-engine: cfMesh vs snappy on ONE shared external-flow model ------------------------
# Both engines already have a native case built on the same unit sphere, but each picks its own
# cell budget, so their numbers were never comparable. Here the geometry, the domain box, the
# patch contract AND the budget are identical, which is the only arrangement in which "cfMesh
# produced N cells and snappy produced M" says anything about the engines rather than the inputs.
_SHARED_BUDGET = 40000
_SHARED_DOMAIN = {"domain_min": [-1.0, -1.0, -1.0], "domain_max": [1.5, 1.0, 1.0]}
_SHARED_CONTRACT = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]


def _shared_external_case(workspace: Path) -> None:
    import pyvista as pv

    workspace.mkdir(parents=True, exist_ok=True)
    sphere = pv.Sphere(radius=0.5, theta_resolution=24, phi_resolution=24).triangulate()
    sphere.save(str(workspace / "input.stl"))
    (workspace / "flow_topology").write_text("external\n")
    (workspace / "dimensionality").write_text("3D\n")


def _mesh_external(engine: str, workspace: Path) -> dict:
    from tests.native._native_geometry import prepared_surface_for

    from meshpipeline.engines.dispatch import run_engine_local

    _shared_external_case(workspace)
    if engine == "cfmesh":
        import meshpipeline.engines.cfmesh.cfmesh_runner as runner
        from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
        from meshpipeline.engines.cfmesh.finalize import finalize
        write_case_skeleton(workspace)
        timeout = 600
    else:
        import meshpipeline.engines.snappy.snappy_runner as runner
        from meshpipeline.engines.snappy.finalize import finalize
        runner._write_case_skeleton(workspace)
        timeout = 900

    configured = runner.configure_mesh(
        workspace, geometry_file="input.stl",
        surface=prepared_surface_for(workspace / "input.stl"),
        strategy={"max_cells": _SHARED_BUDGET}, wall_patch="body",
        contract_patches=_SHARED_CONTRACT, args=dict(_SHARED_DOMAIN),
        cell_budget=_SHARED_BUDGET)
    if not configured.get("success"):
        return {"engine": engine, "status": f"configure failed: {configured}"}

    native = run_engine_local(workspace, engine=engine, timeout=timeout)
    judged = finalize(str(workspace), intake_patches=_SHARED_CONTRACT, engine=engine,
                      domain="unit sphere ext", flow_topology="external")
    manifest_path = workspace / "mesh_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    quality = dict(manifest.get("quality") or {})
    return {
        "engine": engine,
        "status": "success" if judged.get("success") else f"failed: {judged.get('output')}",
        "native_rc": native.get("rc"),
        "cells": manifest.get("cell_count"),
        "cell_budget": _SHARED_BUDGET,
        "max_non_ortho": quality.get("max_non_ortho"),
        "max_skewness": quality.get("max_skewness"),
        "surface_deviation": quality.get("surface_deviation"),
    }


def test_cross_engine_external_flow_cfmesh_vs_snappy(tmp_path, canonical_provenance):
    rows = [_mesh_external(engine, tmp_path / engine) for engine in ("cfmesh", "snappy")]
    by_engine = {row["engine"]: row for row in rows}
    # Surface deviation is the fidelity number worth publishing, and it only exists for an engine
    # that declares a surface-capture reference. Recording WHICH engines can be compared on it is
    # part of the evidence: a side-by-side that quietly omits an engine reads as if it tied.
    comparable = sorted(e for e, row in by_engine.items() if row.get("surface_deviation"))
    # Measured, not asserted: at an identical cell_budget cfMesh lands near it while snappy
    # overshoots it several-fold, because the budget steers castellation refinement rather than
    # capping the snapped result. So cell COUNT is not a cross-engine axis - two engines handed
    # the same budget are not attempting the same size of mesh - and a comparison that ranked
    # them by cells would be reporting that difference in meaning as if it were engine quality.
    overshoot = {e: (row["cells"] / _SHARED_BUDGET if row.get("cells") else None)
                 for e, row in by_engine.items()}
    _write_evidence({"cell_budget": _SHARED_BUDGET, "domain": _SHARED_DOMAIN,
                     "surface_fidelity_comparable_engines": comparable,
                     "cells_over_budget_ratio": overshoot,
                     "comparable_quality_metrics": ["max_non_ortho", "max_skewness"],
                     "results": rows}, name="cross_engine_external_flow")

    for engine, row in by_engine.items():
        assert row["status"] == "success", f"{engine}: {row['status']}"
        assert row["cells"] and row["cells"] > 0, f"{engine} reported no cells: {row}"
        # Both engines run the same OpenFOAM checkMesh, so these two are the common floor on
        # which a cross-engine quality claim can actually rest.
        assert row["max_non_ortho"] is not None, f"{engine} reported no non-orthogonality"
        assert row["max_skewness"] is not None, f"{engine} reported no skewness"

    assert "snappy" in comparable, \
        "snappy declares a surface-capture reference, so its surface deviation must be measured"
