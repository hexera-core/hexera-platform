# Responsibility: Verify both dispatch sites cross the execution seam, and each backend is selected there.
from __future__ import annotations

from pathlib import Path

import pytest

import meshpipeline.settings.providers as provcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_run_from_job_loads_the_snapshot_and_runs_the_same_pipeline(monkeypatch):
    import meshpipeline.application.pipeline_run as ex
    from meshpipeline.application.dispatch_contract import DISPATCH_SCHEMA_VERSION

    saved = {"job_id": "J", "owner_id": "u", "mesh_engine": "cfmesh", "purpose": "internal_cfd"}

    async def _fake_load(job_id):
        assert job_id == "J"
        return {"schema_version": DISPATCH_SCHEMA_VERSION, **saved}
    monkeypatch.setattr(ex, "_load_payload", _fake_load)

    called = {}
    monkeypatch.setattr(ex, "run_pipeline", lambda **kw: called.update(kw) or {"status": "succeeded"})

    out = ex.run_from_job("J")
    assert out["status"] == "succeeded"
    # A persisted row is replayed verbatim, plus the documented defaults a DIRECT dispatch omits
    # (the provenance id and the typed patch contract). See
    # application/dispatch_contract._OPTIONAL_DISPATCH_DEFAULTS. The envelope version itself is
    # never a run argument.
    assert {k: called[k] for k in saved} == saved, \
        "run_from_job did not pass the persisted snapshot verbatim"
    assert called == {**saved, "approved_snapshot_id": "", "approved_patch_contract": None}


def test_run_from_job_refuses_a_job_with_no_snapshot(monkeypatch):
    import meshpipeline.application.pipeline_run as ex

    async def _none(job_id):
        return None
    monkeypatch.setattr(ex, "_load_payload", _none)
    with pytest.raises(SystemExit):
        ex.run_from_job("missing")


def test_deferred_backend_persists_without_launching(monkeypatch):
    import asyncio

    from meshpipeline.adapters.pipeline_execution import deferred as deferred_pipeline

    launched = {"celery": False}
    import meshpipeline.adapters.pipeline_execution.celery as wt

    class _T:
        @staticmethod
        def apply_async(**k):
            launched["celery"] = True
    monkeypatch.setattr(wt, "run_simulation", _T)

    asyncio.run(deferred_pipeline.launch(None, "J", {"job_id": "J"}))
    assert launched["celery"] is False, "deferred backend launched Celery"


def test_the_seam_selects_each_backend_module(monkeypatch):
    import meshpipeline.runtime.composition as comp
    from meshpipeline.adapters.pipeline_execution import celery as celery_pipeline
    from meshpipeline.adapters.pipeline_execution import deferred as deferred_pipeline
    for name, mod in (("celery", celery_pipeline), ("deferred", deferred_pipeline)):
        monkeypatch.setattr(provcfg, "PIPELINE_BACKEND", name)
        assert comp.build_pipeline_launcher() is mod


def test_the_pipeline_code_does_not_import_the_execution_seam():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rln", "adapters.pipeline_execution", str(APP / "pipeline/graph.py"),
         str(APP / "engines"), str(APP / "agents"), str(APP / "pipeline")],
        capture_output=True, text=True).stdout.strip()
    assert not hits, f"pipeline code imports the launch adapters (wrong direction): {hits}"


def test_both_dispatch_sites_go_through_the_seam_not_apply_async_directly():
    for f in ("api/v1/chat.py", "api/v1/simulation.py"):
        src = (APP / f).read_text()
        assert "run_simulation.apply_async" not in src, (
            f"{f} still calls apply_async directly - the job would have no dispatch_payload")
        # Both routes delegate the dispatch itself, so the seam is asserted at the authority that
        # now performs it: the chat route hands the whole approval transaction to
        # agents/intake/approval, and the dispute route hands dispatch-and-its-failure to
        # application/dispute_operation, which is what makes a refused launch recoverable.
        if f == "api/v1/chat.py":
            src = (APP / "agents" / "intake" / "approval.py").read_text()
        if f == "api/v1/simulation.py":
            src = (APP / "application" / "dispute_operation.py").read_text()
        assert "dispatch_pipeline" in src or "execution.pipeline" in src, (
            f"{f} does not dispatch through the execution seam")
