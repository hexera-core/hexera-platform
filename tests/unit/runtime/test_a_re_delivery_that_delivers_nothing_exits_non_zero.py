# Responsibility: Verify the re-delivery entry point reports a refusal as a failure, never as a finished delivery.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import redeliver as RD
from meshpipeline.runtime import redeliver as CLI

JOB = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    """Everything the entry point binds to the world, replaced: this is about its exit code."""
    import meshpipeline.persistence.session as session
    from meshpipeline.runtime import composition

    async def _dispose(): return None

    monkeypatch.setattr(composition, "install_adapters", lambda: None)
    monkeypatch.setattr(session, "dispose_engine", _dispose)
    monkeypatch.setattr(session, "get_db", object())


def _answer(monkeypatch, outcome, seen=None):
    async def _redeliver(session_factory, job_repo, job_id, **kw):
        if seen is not None:
            seen.update({"job_id": job_id, **kw})
        return outcome
    monkeypatch.setattr(RD, "redeliver_job", _redeliver)


def test_a_re_delivered_run_exits_zero(monkeypatch):
    _answer(monkeypatch, RD.Redelivery(job_id=JOB, workspace="/srv/workspaces/x",
                                       delivered=[{"type": "mesh_bundle"}],
                                       delivered_types=["mesh_bundle", "viewer_data"],
                                       required_ready=True, restated=True))
    assert CLI.main(["--job-id", JOB]) == 0


def test_a_refused_re_delivery_exits_non_zero(monkeypatch):
    _answer(monkeypatch, RD.Redelivery(job_id=JOB, refused=RD.ALREADY_DELIVERED))
    assert CLI.main(["--job-id", JOB]) == 1


def test_a_delivery_whose_required_outputs_are_not_ready_exits_non_zero(monkeypatch):
    # The uploader can return having stored an OPTIONAL artifact while a required one was refused
    # as a conflict. An operator watching an exit code must not read that as a finished delivery.
    _answer(monkeypatch, RD.Redelivery(job_id=JOB, workspace="/srv/workspaces/x",
                                       delivered=[{"type": "mesh"}], delivered_types=["mesh"],
                                       required_ready=False))
    assert CLI.main(["--job-id", JOB]) == 1


def test_the_workspace_is_the_runs_own_unless_an_operator_names_one(monkeypatch):
    seen: dict = {}
    _answer(monkeypatch, RD.Redelivery(job_id=JOB, required_ready=True), seen)
    assert CLI.main(["--job-id", JOB]) == 0
    assert seen["workspace"] is None, "the CLI invented a workspace of its own"

    seen.clear()
    assert CLI.main(["--job-id", JOB, "--workspace", "/srv/workspaces/x/generation_1/attempt_1"]) == 0
    assert seen["workspace"] == "/srv/workspaces/x/generation_1/attempt_1"


def test_it_runs_no_graph_and_no_mesher():
    import inspect

    src = inspect.getsource(CLI)
    assert "run_from_job" not in src and "build_graph" not in src, (
        "the re-delivery entry point can start a pipeline run")
