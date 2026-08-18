# Responsibility: Verify every mesh dispatches to the remote executor, with no local fallback and no silent success.
from pathlib import Path

import meshpipeline.settings.providers as provcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

import pytest  # noqa: E402

from meshpipeline.contracts.mesh_execution import set_mesh_executor  # noqa: E402

#: A well-formed operation identity. The provider executor requires one; deriving it is the
#: claim authority's job, and asserting that derivation belongs to its own suite.
KEY = "b" * 64

_FULL = {"GCP_PROJECT_ID": "proj", "GCP_MESH_BUCKET": "bkt", "CLOUDRUN_JOB": "job",
         "GOOGLE_APPLICATION_CREDENTIALS": "/secrets/sa.json"}


def _set(monkeypatch, **overrides):
    for k, v in {**_FULL, **overrides}.items():
        monkeypatch.setattr(provcfg, k, v)


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    set_mesh_executor(None)   # never leak a test executor into other tests


class _RecordingExecutor:
    def __init__(self):
        self.seen = []

    def run(self, workspace, *, engine, timeout):
        self.seen.append(engine)
        return {"rc": 0, "timed_out": False, "log_tail": "remote-ok"}


def test_require_config_raises_loudly_when_unconfigured(monkeypatch):
    from meshpipeline.adapters.mesh_execution.cloud_run_client import require_cloudrun_config
    from meshpipeline.settings.env import ConfigurationError
    for missing in _FULL:
        _set(monkeypatch, **{missing: ""})
        with pytest.raises(ConfigurationError, match=missing):
            require_cloudrun_config()
    _set(monkeypatch)
    require_cloudrun_config()   # fully configured → no raise


def test_no_executor_configured_raises(monkeypatch):
    import meshpipeline.engines.cfmesh.native as cr
    from meshpipeline.contracts.mesh_execution import MeshExecutionError
    set_mesh_executor(None)
    monkeypatch.setattr(cr, "scan_case_dicts", lambda ws: None)
    with pytest.raises(MeshExecutionError):
        cr.run_cartesian_mesh(Path("/tmp/nonexistent-ws"), timeout=5)


def test_run_cartesian_mesh_dispatches_to_the_executor_with_no_local_fallback(monkeypatch):
    import meshpipeline.engines.cfmesh.native as cr
    ex = _RecordingExecutor()
    set_mesh_executor(ex)
    monkeypatch.setattr(cr, "scan_case_dicts", lambda ws: None)
    # `_run_<engine>_local` is the EXECUTION that runs inside the mesh container, never on the
    # worker: the name says local, the machine does not.
    monkeypatch.setattr(cr, "_run_cartesian_mesh_local",
                        lambda *a, **k: pytest.fail("worker must NEVER run the mesh locally"))

    out = cr.run_cartesian_mesh(Path("/tmp/nonexistent-ws"), timeout=5)
    assert out["log_tail"] == "remote-ok"
    assert ex.seen == ["cfmesh"]


def test_a_cloud_failure_is_a_loud_mesh_failure_not_a_local_run():
    from meshpipeline.adapters.mesh_execution.cloud_run_client import _fail
    d = _fail("snappy", "boom")
    assert d["rc"] != 0 and "CLOUD_RUN_FAILED" in d["log_tail"]


def test_every_engine_dispatches_through_the_contract(monkeypatch):
    ex = _RecordingExecutor()
    set_mesh_executor(ex)
    # scan guards for the OpenFOAM engines would reject an empty ws; stub them out
    import meshpipeline.engines.cfmesh.cfmesh_runner as _cf
    import meshpipeline.engines.snappy.snappy_runner as _sn
    from meshpipeline.engines.runtime import get_engine
    monkeypatch.setattr(_cf, "scan_case_dicts", lambda ws: None)
    monkeypatch.setattr(_sn, "scan_case_dicts", lambda ws: None)

    for name in ("cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk"):
        get_engine(name).run_cartesian_mesh(Path("/tmp/nonexistent-ws"), timeout=5)
    assert set(ex.seen) == {"cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk"}


def test_engine_dispatch_covers_every_engine_and_rejects_unknown():
    from meshpipeline.engines.dispatch import engine_runners, run_engine_local
    from meshpipeline.engines.registry import engine_names
    runners = engine_runners()
    assert set(runners) == set(engine_names())
    for fn in runners.values():
        assert callable(fn)
    unknown = run_engine_local(Path("/tmp/x"), engine="not_an_engine", timeout=1)
    assert unknown["rc"] != 0 and "unknown engine" in unknown["log_tail"]


# the enforced topology: the application composes remote execution and cannot compose local


def test_the_installed_application_executor_is_the_remote_one(monkeypatch):
    _set(monkeypatch)
    from meshpipeline.adapters.mesh_execution.cloud_run_client import CloudRunMeshExecutor
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    from meshpipeline.runtime.composition import build_mesh_executor
    # Remote, THROUGH the submission authority: nothing may reach the provider
    # except via the claim that makes a replay reuse an accepted submission.
    composed = build_mesh_executor()
    assert isinstance(composed, ClaimingMeshExecutor)
    assert isinstance(composed._inner, CloudRunMeshExecutor)


def test_a_dispatched_mesh_reaches_the_remote_transport(monkeypatch, tmp_path):
    _set(monkeypatch)
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    from meshpipeline.runtime.composition import build_mesh_executor

    reached = {}
    # The operation identity now travels with the call: the provider exchange has to land in a
    # namespace a replacement worker can re-derive.
    monkeypatch.setattr(crc, "run_mesh_remote",
                        lambda ws, *, engine, timeout, operation_key: reached.update(
                            engine=engine, timeout=timeout, operation_key=operation_key)
                        or {"rc": 0, "timed_out": False})
    # The PROVIDER executor, which is what this test asserts. The authority in front of it
    # refuses an unowned run outright, and that boundary is covered by its own control.
    provider = build_mesh_executor()._inner
    provider.run(tmp_path, engine="cfmesh", timeout=90, operation_key=KEY)
    assert reached["engine"] == "cfmesh" and reached["timeout"] == 90
    assert reached["operation_key"] == KEY and len(KEY) == 64


def test_missing_remote_configuration_fails_before_any_mesh_process_starts(monkeypatch, tmp_path):
    _set(monkeypatch, CLOUDRUN_JOB="")
    import meshpipeline.adapters.mesh_execution.local as local_mod
    from meshpipeline.runtime.composition import build_mesh_executor
    from meshpipeline.settings.env import ConfigurationError

    monkeypatch.setattr(local_mod, "run_engine_local",
                        lambda *a, **k: pytest.fail("a local mesh process was started"))
    provider = build_mesh_executor()._inner
    with pytest.raises(ConfigurationError, match="CLOUDRUN_JOB"):
        provider.run(tmp_path, engine="cfmesh", timeout=30, operation_key=KEY)


def test_a_remote_failure_stays_a_remote_failure(monkeypatch, tmp_path):
    _set(monkeypatch)
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    import meshpipeline.adapters.mesh_execution.local as local_mod
    from meshpipeline.runtime.composition import build_mesh_executor

    monkeypatch.setattr(local_mod, "run_engine_local",
                        lambda *a, **k: pytest.fail("fell back to a local mesh"))
    monkeypatch.setattr(crc, "run_mesh_remote",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cloud run refused")))
    provider = build_mesh_executor()._inner
    with pytest.raises(RuntimeError, match="cloud run refused"):
        provider.run(tmp_path, engine="cfmesh", timeout=30, operation_key=KEY)
