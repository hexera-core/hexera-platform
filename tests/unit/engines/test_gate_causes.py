# Responsibility: Verify every gate names the cause it rejects for, with the facts the user's sentence needs.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts.failure_cause import FailureCause
from meshpipeline.engines.gates import GateCtx, GateFeedback, cause_of, facts_of
from meshpipeline.engines.registry import engine_names, get_spec

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"


@pytest.fixture
def _execution_publisher(monkeypatch):
    # the executor publishes through the ownership-checked port; these tests exercise what it
    # records, not Redis or PostgreSQL, so the port is stood in for
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as _ex
    install(monkeypatch, _ep)
    monkeypatch.setattr(_ex, "execution_publisher", _ep.execution_publisher)


@pytest.mark.parametrize("engine", engine_names())
def test_every_declared_gate_names_a_cause_from_the_vocabulary(engine):
    for g in get_spec(engine).gates:
        assert g.cause, f"{engine}:{g.key} declares no cause - its failure would be told vaguely"
        FailureCause(g.cause)


def test_gate_feedback_is_still_the_text_every_consumer_reads():
    fb = GateFeedback("[X] something", cause="mesh_quality", facts={"a": 1})
    assert fb == "[X] something" and "something" in fb and fb.splitlines()[0] == "[X] something"
    wrapped = fb.prefixed("[MANIFEST] ")
    assert wrapped == "[MANIFEST] [X] something" and wrapped.cause == "mesh_quality"
    assert cause_of("plain text") == "" and facts_of("plain text") == {}


def _manifest(ws: Path, **over) -> Path:
    m = {"schema_version": "2.1", "geometry": {}, "patches": {"car_wall": [], "farfield": []},
         "validation": {"has_wall": True, "has_inflow": True, "has_outflow": True,
                        "patch_validation": {"car_wall": True, "farfield": True}},
         "cell_count": 1000, "mesh_written": True}
    m.update(over)
    (ws / "mesh_manifest.json").write_text(json.dumps(m))
    return ws


@pytest.mark.parametrize("engine", ["snappy", "cfmesh"])
class TestTheFlowManifestGate:
    def _gate(self, engine):
        import importlib
        return importlib.import_module(f"meshpipeline.engines.{engine}.flow_gates")

    def test_over_budget_is_named_with_both_counts(self, engine, tmp_path):
        fg = self._gate(engine)
        _manifest(tmp_path, cell_count=polcfg.CELL_HARD_LIMIT + 1)
        ok, fb = fg._gate_manifest_valid(GateCtx(workspace=tmp_path))
        assert ok is False and fb.startswith("[MANIFEST_VALIDATION_FAILED]")
        assert cause_of(fb) == FailureCause.CELL_BUDGET
        assert facts_of(fb) == {"cells": polcfg.CELL_HARD_LIMIT + 1,
                                "limit": polcfg.CELL_HARD_LIMIT}

    def test_no_wall_type_is_a_contract_mismatch(self, engine, tmp_path):
        fg = self._gate(engine)
        _manifest(tmp_path, validation={"has_wall": False, "has_inflow": True,
                                        "has_outflow": True})
        ok, fb = fg._gate_manifest_valid(GateCtx(workspace=tmp_path))
        assert ok is False and cause_of(fb) == FailureCause.CONTRACT_MISMATCH
        assert facts_of(fb)["missing_roles"] == ["wall"]

    def test_a_declared_patch_the_mesh_never_carried_is_a_contract_mismatch(self, engine,
                                                                           tmp_path):
        fg = self._gate(engine)
        _manifest(tmp_path, validation={
            "has_wall": True, "has_inflow": True, "has_outflow": True,
            "patch_validation": {"car wall": False, "farfield": True}})
        pm = tmp_path / "constant" / "polyMesh"
        pm.mkdir(parents=True)
        (pm / "boundary").write_text("2\n(\n car_wall\n {\n type wall;\n nFaces 50;\n }\n"
                                     " farfield\n {\n type patch;\n nFaces 9;\n }\n)\n")
        ok, fb = fg._gate_manifest_valid(GateCtx(workspace=tmp_path))
        assert ok is False and "zero faces" in fb
        assert cause_of(fb) == FailureCause.CONTRACT_MISMATCH
        assert facts_of(fb)["renamed"] == {"car wall": "car_wall"}

    def test_a_manifest_that_was_never_written_is_the_mesher_stopping(self, engine, tmp_path):
        fg = self._gate(engine)
        ok, fb = fg._gate_manifest_valid(GateCtx(workspace=tmp_path))
        assert ok is False and cause_of(fb) == ""        # plain text: the GateSpec's cause
        spec_cause = next(g.cause for g in get_spec(engine).gates if g.key == "manifest_valid")
        assert spec_cause == FailureCause.ENGINE_CRASHED


def test_the_contract_check_carries_its_diff():
    from meshpipeline.engines.contract import check_contract
    ok, diag = check_contract(
        intake_patches=[{"name": "car wall", "type": "wall"}, {"name": "sym", "type": "symmetry"},
                        {"name": "farfield", "type": "farfield"}],
        manifest_patches=["car_wall", "sym", "farfield"],
        manifest_patch_types={"sym": "wall", "farfield": "farfield", "car_wall": "wall"})
    assert ok is False and diag.startswith("[CONTRACT_MISMATCH]")
    f = facts_of(diag)
    assert cause_of(diag) == FailureCause.CONTRACT_MISMATCH
    assert f["missing"] == ["car wall"] and f["renamed"] == {"car wall": "car_wall"}
    assert f["extra"] == ["car_wall"]
    assert f["mistyped"] == [{"name": "sym", "declared": "symmetry", "got": "wall"}]


def test_the_quality_floor_hands_over_the_number_and_the_bar():
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    manifest = {"quality": {"fatal": [], "skew_fraction": 0.004, "cells": 10},
                "patch_face_counts": {"body": 10}, "patch_types": {"body": "wall"}}
    ok, fb = gate_declared_criteria("snappy", manifest)
    assert ok is False and cause_of(fb) == FailureCause.MESH_QUALITY
    (check,) = facts_of(fb)["checks"]
    assert (check["key"], check["measured"], check["op"], check["threshold"]) == \
        ("skew_fraction", 0.004, "<=", 5e-4)


def test_an_unmeasured_bar_is_named_as_unmeasured():
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    ok, fb = gate_declared_criteria("snappy", {"quality": {"skew_fraction": 0.0, "cells": 1},
                                               "patch_face_counts": {"body": 1},
                                               "patch_types": {"body": "wall"}})
    assert ok is False and facts_of(fb)["unmeasured"]


def test_broken_cells_are_named_by_kind():
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    ok, fb = gate_declared_criteria("snappy", {
        "quality": {"fatal": ["negative-volume cells"], "skew_fraction": 0.0, "cells": 1},
        "patch_face_counts": {"body": 1}, "patch_types": {"body": "wall"}})
    assert ok is False and facts_of(fb) == {"fatal": ["negative-volume cells"]}


def test_gmsh_element_quality_and_its_own_group_names():
    from meshpipeline.engines.gmsh import gates as gg
    from meshpipeline.engines.gmsh.gmsh_runner import SICN_FLOOR
    ctx = GateCtx(workspace=Path("."), manifest={"quality": {"min_sicn": SICN_FLOOR / 2}})
    ok, fb = gg._gate_sicn_floor(ctx)
    assert ok is False and cause_of(fb) == FailureCause.MESH_QUALITY
    assert facts_of(fb)["checks"][0]["threshold"] == SICN_FLOOR
    ctx = GateCtx(workspace=Path("."), intake_patches=[{"name": "inlet", "type": "inlet"}],
                  manifest={"patch_types": {"outlet": "outlet"}, "patches": {"outlet": [1]}})
    ok, fb = gg._gate_gmsh_region_contract(ctx)
    assert ok is False and cause_of(fb) == FailureCause.CONTRACT_MISMATCH
    assert facts_of(fb)["retry_may_fix"] is True


def test_a_wrong_boundary_type_is_named(tmp_path):
    from meshpipeline.engines.snappy.finalize import reconcile_boundary_types
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "boundary").write_text("1\n(\n    sym\n    {\n        type patch;\n        nFaces 4;\n"
                                 "    }\n)\n")
    fb = reconcile_boundary_types(tmp_path, [{"name": "sym", "type": "symmetry"}])
    assert cause_of(fb) == FailureCause.BOUNDARY_TYPE
    assert facts_of(fb)["mistyped"] == [{"name": "sym", "declared": "symmetry",
                                        "want": "symmetryPlane", "got": "patch"}]


def test_the_executor_records_the_cause_the_gate_named(monkeypatch, tmp_path, _execution_publisher):
    import meshpipeline.engines.cfmesh.flow_gates as fg
    import meshpipeline.pipeline.executor as ex

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass

    class _Engine:
        name = "cfmesh"

        def finalize(self, *a, **k):
            return {"success": True, "output": "[CFMESH] ok"}

    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    monkeypatch.setattr(ex, "get_engine", lambda n="": _Engine())
    monkeypatch.setattr(fg, "_validate_manifest", lambda *a, **k: (
        False, GateFeedback("mesh_manifest.json: cell_count=9 exceeds",
                            cause=FailureCause.CELL_BUDGET, facts={"cells": 9, "limit": 5})))
    _manifest(tmp_path)
    out = asyncio.run(ex.node_executor({"openfoam_workspace": str(tmp_path), "job_id": "t",
                                        "engine": "cfmesh", "intake_patches": []}))
    assert out["executor_failed_gate"] == "manifest_valid"
    assert out["executor_failure_cause"] == "cell_budget"
    assert out["executor_failure_facts"] == {"cells": 9, "limit": 5}


def test_a_passing_run_records_no_cause(monkeypatch, tmp_path, _execution_publisher):
    import meshpipeline.pipeline.executor as ex
    monkeypatch.setattr(polcfg, "DOMAIN_EXTENT_GATE_ENABLED", False)
    monkeypatch.setattr(polcfg, "SOLVABILITY_GATE_ENABLED", False)

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass

    class _Engine:
        name = "cfmesh"

        def finalize(self, *a, **k):
            return {"success": True, "output": "ok"}

    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    monkeypatch.setattr(ex, "get_engine", lambda n="": _Engine())
    monkeypatch.setattr("meshpipeline.engines.registry.get_spec",
                        lambda n: type("S", (), {"gates": (), "deliverable": None})())
    out = asyncio.run(ex.node_executor({"openfoam_workspace": str(tmp_path), "job_id": "t",
                                        "engine": "cfmesh", "intake_patches": []}))
    assert out["executor_success"] is True
    assert out["executor_failure_cause"] == "" and out["executor_failure_facts"] == {}


def test_no_snappy_module_calls_its_mesh_a_cfmesh_mesh():
    # the snappy finalize summary is the first line a retry's planner reads: it said "[CFMESH]",
    # and a snappy run's reasoning talked about "CFMESH quality" (job ac1daa3e)
    for name in ("finalize.py", "solvability.py"):
        text = (SRC / "engines" / "snappy" / name).read_text(encoding="utf-8")
        assert "[CFMESH]" not in text and "valid cfMesh volume mesh" not in text
