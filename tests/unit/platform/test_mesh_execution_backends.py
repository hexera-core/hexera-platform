# Responsibility: Verify the composition root always builds the remote executor, and both satisfy the same contract.
from __future__ import annotations

import pytest

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.mesh_execution.local import LocalMeshExecutor
from meshpipeline.contracts.mesh_execution import MeshExecutor, run_mesh, set_mesh_executor
from meshpipeline.runtime.composition import build_mesh_executor


@pytest.fixture(autouse=True)
def _no_executor_leak():
    yield
    set_mesh_executor(None)


# the application always delegates: there is no selection
def test_the_composition_root_always_builds_the_remote_executor():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import CloudRunMeshExecutor
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    # Remote, THROUGH the submission authority: nothing may reach the provider
    # except via the claim that makes a replay reuse an accepted submission.
    composed = build_mesh_executor()
    assert isinstance(composed, ClaimingMeshExecutor)
    assert isinstance(composed._inner, CloudRunMeshExecutor)


def test_no_environment_value_can_make_the_application_mesh_locally(monkeypatch):
    from meshpipeline.adapters.mesh_execution.cloud_run_client import CloudRunMeshExecutor
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    for name, value in (("MESH_BACKEND", "local"), ("MESH_EXECUTOR", "local"),
                        ("MESH_MODE", "local")):
        monkeypatch.setenv(name, value)
    # Remote, THROUGH the submission authority: nothing may reach the provider
    # except via the claim that makes a replay reuse an accepted submission.
    composed = build_mesh_executor()
    assert isinstance(composed, ClaimingMeshExecutor)
    assert isinstance(composed._inner, CloudRunMeshExecutor)


def _executors():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import CloudRunMeshExecutor
    return [LocalMeshExecutor(), CloudRunMeshExecutor()]


@pytest.mark.parametrize("executor", _executors(), ids=["local", "cloudrun"])
def test_both_executors_are_reachable_through_the_neutral_entry(executor, monkeypatch, tmp_path):
    seen: dict = {}

    def _spy(workspace, *, engine, timeout):
        seen.update(workspace=workspace, engine=engine, timeout=timeout)
        return {"rc": 0, "timed_out": False, "log_tail": ""}

    monkeypatch.setattr(executor, "run", _spy)
    set_mesh_executor(executor)
    out = run_mesh(tmp_path, engine="cfmesh", timeout=123)
    assert out["rc"] == 0
    assert seen["engine"] == "cfmesh" and seen["timeout"] == 123


def test_meshing_without_a_composed_executor_fails_loudly():
    from meshpipeline.contracts.mesh_execution import MeshExecutionError
    set_mesh_executor(None)
    with pytest.raises(MeshExecutionError):
        run_mesh("/tmp/ws", engine="cfmesh", timeout=10)


# the local executor: real, in-process dispatch
def test_the_local_executor_meshes_through_the_neutral_engine_dispatch(monkeypatch, tmp_path):
    import meshpipeline.adapters.mesh_execution.local as local_mod

    called: dict = {}

    def _fake_dispatch(workspace, *, engine, timeout):
        called.update(workspace=workspace, engine=engine, timeout=timeout)
        return {"rc": 0, "timed_out": False, "log_tail": "meshed"}

    monkeypatch.setattr(local_mod, "run_engine_local", _fake_dispatch)
    out = LocalMeshExecutor().run(tmp_path, engine="gmsh", timeout=60)
    assert out == {"rc": 0, "timed_out": False, "log_tail": "meshed"}
    assert called["engine"] == "gmsh" and called["timeout"] == 60


def test_the_local_executor_needs_no_cloud_configuration(monkeypatch):
    for name in ("GCP_PROJECT_ID", "GCP_MESH_BUCKET", "CLOUDRUN_JOB",
                 "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setattr(provcfg, name, "")
    assert isinstance(LocalMeshExecutor(), MeshExecutor)


def test_the_cloud_run_executor_requires_its_config_before_it_meshes(monkeypatch):
    from meshpipeline.adapters.mesh_execution.cloud_run_client import require_cloudrun_config
    from meshpipeline.settings.env import ConfigurationError

    for name in ("GCP_PROJECT_ID", "GCP_MESH_BUCKET", "CLOUDRUN_JOB",
                 "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setattr(provcfg, name, "")
    with pytest.raises(ConfigurationError) as ei:
        require_cloudrun_config()
    assert "GCP_PROJECT_ID" in str(ei.value)
