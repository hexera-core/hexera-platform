# Responsibility: Verify the executor's gate order, short circuits, and that a crashing gate is a system failure.
from __future__ import annotations

import json
from pathlib import Path

import pytest

import meshpipeline.engines.cfmesh.flow_gates as fg  # noqa: E402
import meshpipeline.engines.contract as contract_mod  # noqa: E402
import meshpipeline.pipeline.executor as ex
import meshpipeline.settings.policy as polcfg
from meshpipeline.errors import SystemFailure  # noqa: E402

MANIFEST = {
    "schema_version": 1,
    "geometry": {"domain_box": [[0, 0, 0], [1, 1, 1]]},
    "patches": ["body", "farfield"],
    "patch_types": {"body": "wall", "farfield": "patch"},
    "patch_face_counts": {"body": 500, "farfield": 120},
    "validation": {"has_wall": True, "has_inflow": True, "has_outflow": True},
    # A healthy cfMesh mesh REPORTS its quality bars. `fatal: []` is a measured clean bill of
    # health, not an absent measurement - the quality_floor gate refuses the latter, because a
    # mesh nobody checked has not cleared a floor.
    "quality": {"fatal": [], "cells": 1000},
}


@pytest.fixture
def quiet(monkeypatch):
    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    monkeypatch.setattr(polcfg, "DOMAIN_EXTENT_GATE_ENABLED", False)
    monkeypatch.setattr(polcfg, "SOLVABILITY_GATE_ENABLED", True)


def _state(ws: Path) -> dict:
    return {"openfoam_workspace": str(ws), "job_id": "t", "domain": "external aero",
            "intake_patches": [{"name": "body", "type": "wall"}], "engine": "cfmesh"}


def _arm(monkeypatch, ws: Path, *, finalize_ok=True, manifest_ok=True,
         contract_ok=True, solvable=True, on_solvability=None):
    (ws / "mesh_manifest.json").write_text(json.dumps(MANIFEST))
    # solvability is engine-owned now: the fake engine declares its own check,
    # exactly as the real cfMesh adapter forwards to engines.cfmesh.solvability.
    # `on_solvability` injects a custom check (e.g. one that raises, or asserts
    # it must not be called); otherwise it returns pass/fail per `solvable`.
    class _FakeEngine:
        name = "cfmesh"
        def finalize(self, *a, **k):
            return {"success": finalize_ok,
                    "output": "finalize-out" if finalize_ok else "[FINALIZE_FAILED]"}
        def check_solvability(self, w, metrics_out=None):
            if on_solvability is not None:
                return on_solvability(w, metrics_out)
            return (True, "") if solvable else (False, "[UNSOLVABLE] singular operator")
    monkeypatch.setattr(ex, "get_engine", lambda n="": _FakeEngine())
    monkeypatch.setattr(fg, "_validate_manifest",
                        lambda *a, **k: (manifest_ok, "" if manifest_ok else "bad manifest"))
    monkeypatch.setattr(contract_mod, "check_contract",
                        lambda **k: (contract_ok, "" if contract_ok else "[CONTRACT] patch mismatch"))


async def test_all_gates_pass(quiet, monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is True
    assert out["solvability_failed"] is False
    assert out["mesh_manifest"]["patches"] == ["body", "farfield"]


async def test_finalize_failure_short_circuits(quiet, monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, finalize_ok=False)
    def _never(*a, **k):
        raise AssertionError("manifest gate must not run when finalize failed")
    monkeypatch.setattr(fg, "_validate_manifest", _never)
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is False
    assert out["mesh_manifest"] == {}


async def test_geometry_unsuitable_reason_short_circuits_before_finalize(quiet, monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise AssertionError("finalize must not run when the input was already rejected up front")
    monkeypatch.setattr(ex, "get_engine", _boom)
    reason = ("[GEOMETRY_UNSUITABLE] the input surface self-intersects - no tetrahedral fill "
              "is possible.")
    out = await ex.node_executor(dict(_state(tmp_path), engine="vmtk",
                                      geometry_unsuitable_reason=reason))
    assert out["executor_success"] is False
    assert out["executor_failed_gate"] == "geometry"
    assert out["executor_output"] == reason


async def test_manifest_rejection_flips_success(quiet, monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, manifest_ok=False)
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is False
    assert "[MANIFEST_VALIDATION_FAILED]" in out["executor_output"]


async def test_contract_rejection_skips_solvability(quiet, monkeypatch, tmp_path):
    def _never(w, metrics):
        raise AssertionError("solvability must not run after a contract rejection")
    _arm(monkeypatch, tmp_path, contract_ok=False, on_solvability=_never)
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is False
    assert "[CONTRACT]" in out["executor_output"]
    assert out["solvability_failed"] is False


async def test_solvability_rejection_sets_flag(quiet, monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, solvable=False)
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is False
    assert out["solvability_failed"] is True
    assert "[UNSOLVABLE]" in out["executor_output"]


async def test_contract_gate_crash_raises_system_failure(quiet, monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    def _crash(**k):
        raise RuntimeError("gate bug")
    monkeypatch.setattr(contract_mod, "check_contract", _crash)
    with pytest.raises(SystemFailure):
        await ex.node_executor(_state(tmp_path))


async def test_solvability_gate_crash_raises_system_failure(quiet, monkeypatch, tmp_path):
    def _crash(w, metrics):
        raise MemoryError("AMG blew up")
    _arm(monkeypatch, tmp_path, on_solvability=_crash)
    with pytest.raises(SystemFailure):
        await ex.node_executor(_state(tmp_path))


async def test_missing_workspace_is_executor_error(quiet):
    out = await ex.node_executor({"job_id": "t"})
    assert out["executor_success"] is False
    assert "[EXECUTOR_ERROR]" in out["executor_output"]


def _with_gates(monkeypatch, gates):
    """The cfMesh spec with these gates in place of its own."""
    import meshpipeline.engines.registry as reg
    real = reg.get_spec("cfmesh")

    class _Spec:
        def __init__(self, gates):
            self.gates = gates

        def __getattr__(self, name):
            return getattr(real, name)
    spec = _Spec(gates)
    monkeypatch.setattr(reg, "get_spec", lambda name="": spec)


async def test_a_note_gate_on_a_sound_mesh_is_a_caveat_not_a_failure(quiet, monkeypatch, tmp_path):
    """LAD-cfd-3: the resolution floor is a non-blocking gate - the trial solve still runs, the
    mesh is valid, and the shortfall travels as the mesh's note, kept on the run's sound list."""
    from meshpipeline.engines.gates import GateSpec, refuse
    solved = []
    _arm(monkeypatch, tmp_path, on_solvability=lambda w, m: solved.append(1) or (True, ""))
    _with_gates(monkeypatch, (
        GateSpec(key="resolution_floor", blocking=False, check=lambda ctx: (False, refuse(
            "[RESOLUTION] undermeshed: 8", "under_resolved", cells_across=8.0, needed=12.0))),))
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is True and out["executor_failed_gate"] == ""
    (note,) = out["requirement_caveats"]
    assert note["kind"] == "resolution_short" and note["cells_across"] == 8.0
    assert out["sound_attempts"][0]["workspace"] == str(tmp_path)
    assert solved, "the trial solve still runs on a mesh short of the floor"


async def test_over_the_cell_limit_the_trial_solve_is_not_run(quiet, monkeypatch, tmp_path):
    """LAD-cfd-4: a sound mesh over one job's cell limit is delivered with the note, and the
    worker's trial solve - whose matrix outgrows the worker - is not run on it."""
    from meshpipeline.engines.gates import GateSpec
    from meshpipeline.engines.resolution_policy import gate_cell_limit

    def _never(w, m):
        raise AssertionError("no trial solve over the cell limit")
    _arm(monkeypatch, tmp_path, on_solvability=_never)
    m = json.loads((tmp_path / "mesh_manifest.json").read_text())
    (tmp_path / "mesh_manifest.json").write_text(json.dumps(
        {**m, "cell_count": polcfg.CELL_HARD_LIMIT + 1}))
    _with_gates(monkeypatch, (GateSpec(key="cell_limit", blocking=False, check=gate_cell_limit),))
    out = await ex.node_executor(_state(tmp_path))
    assert out["executor_success"] is True
    assert [c["kind"] for c in out["requirement_caveats"]] == ["over_cell_limit"]


# node_executor publishes through the ownership-checked port. These tests exercise the gate
# sequence, not Redis or PostgreSQL, so the port is stood in for.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as _ex
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_ex, "execution_publisher", _ep.execution_publisher)
    return made
