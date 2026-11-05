# Responsibility: Verify a builder that stops without building ends the run on what the last attempt actually ended on.
# Boundaries: node_builder's patch, the refusal it writes, and the executor's account of it; the guard trip itself is engines/.
"""dev-areen, 25 Oct (FEA assemblies ox_cryo_ll, ox_mc_lid, hemy_mount_base): the FEA guard stopped
the mesher, the next attempt found the run's time spent ("Builder aggregate budget exhausted"), and
the executor - looking at that attempt's EMPTY workspace - told the user "The mesher (Gmsh) stopped
before it finished writing the mesh" (engine_crashed). No mesher had crashed. The run now ends on the
guard trip, with its reason, and says why it was not tried again. CFD runs take the same stop."""
from __future__ import annotations

import asyncio
import time
import types

import pytest

import meshpipeline.agents.builder.agent as agent
import meshpipeline.agents.builder.attempt_capture as attempt_capture
import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.contracts.failure_cause import describe
from meshpipeline.pipeline.graph import route_after_builder, route_after_executor


@pytest.fixture(autouse=True)
def published(monkeypatch):
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)


@pytest.fixture(autouse=True)
def _workspace_base(monkeypatch, tmp_path):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path / "workspaces")
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))


@pytest.fixture
def quiet_executor(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    import meshpipeline.pipeline.executor as ex

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr(ex, "TrainingLogger", _TL)
    install(monkeypatch, _ep)
    monkeypatch.setattr(ex, "execution_publisher", _ep.execution_publisher)

    class _NoMesh:
        name = "x"

        def finalize(self, *a, **k):
            return {"success": False, "output": "[GMSH] no mesh.inp", "stdout": "", "stderr": ""}
    monkeypatch.setattr(ex, "get_engine", lambda n="": _NoMesh())
    return ex


_TRIP = {"where": "fea.gmsh", "amount": 2_400_000, "ceiling_s": 13800, "stall_window_s": 900, "pass": 1,
         "stalled": True, "watchdog": {"seconds": 3300.0, "silence_at_stop_s": 905.0, "cpu_s": 0.0}}


def _spent(tmp_path, *, engine: str = "gmsh", **over) -> dict:
    """A retry whose run's time is already spent."""
    prev = tmp_path / "attempt_before"
    prev.mkdir(exist_ok=True)
    past = time.time() - 30.0
    st = {"job_id": "j", "engine": engine, "retry_count": 1, "builder_mode": "retry",
          "request_txt": "r", "review_brief_txt": "b", "intake_patches": [],
          "openfoam_workspace": str(prev), "builder_deadline_epoch": past,
          "pipeline_deadline_epoch": past, "executor_success": False}
    st.update(over)
    return st


def _guard_tripped(tmp_path, **over) -> dict:
    return _spent(tmp_path, executor_failed_gate="fea_guard", executor_failure_cause="run_infrastructure",
                  executor_failure_facts={"ours": True, "guard_trip": _TRIP, "deterministic": False},
                  executor_output="the mesher made no progress", **over)


def _through_the_executor(ex, state: dict) -> dict:
    out = asyncio.run(agent.node_builder(state))
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1
    st = {**state, **out}
    assert route_after_builder(st) == "node_executor"
    verdict = asyncio.run(ex.node_executor(st))
    return {**st, **verdict}


def test_a_spent_run_after_a_guard_trip_says_the_guard_trip(tmp_path, quiet_executor):
    st = _through_the_executor(quiet_executor, _guard_tripped(tmp_path))
    assert st["executor_failed_gate"] == "fea_guard"
    assert st["executor_failure_cause"] == "run_infrastructure"
    what, nxt = describe(st["executor_failure_cause"], st["executor_failure_facts"], engine="Gmsh")
    assert "stopped making progress" in what and "15 minutes" in what
    assert "not tried again" in what and "time was used up" in what
    assert "stopped before it finished writing" not in what
    assert "run it again" in nxt
    # and the run ends there: no attempt is left
    assert route_after_executor(st) == "__end__"


def test_a_cfd_run_spent_after_a_failed_check_says_that_check(tmp_path, quiet_executor):
    """The same stop for CFD: a snappy attempt that failed its quality check, then the time ran out -
    the user reads the quality failure, not a mesher crash."""
    st = _through_the_executor(quiet_executor, _spent(
        tmp_path, engine="snappy", executor_failed_gate="mesh_quality", executor_failure_cause="mesh_quality",
        executor_failure_facts={"checks": [{"key": "max_non_ortho", "label": "non-orthogonality",
                                            "measured": 78.0, "op": "<=", "threshold": 70.0}]},
        executor_output="[QUALITY] non-ortho 78"))
    assert st["executor_failure_cause"] == "mesh_quality"
    what, _ = describe(st["executor_failure_cause"], st["executor_failure_facts"])
    assert "non-orthogonality" in what and "not tried again" in what
    assert "stopped before it finished writing" not in what


def test_a_run_spent_before_any_attempt_failed_says_the_time_ran_out(tmp_path, quiet_executor):
    st = _through_the_executor(quiet_executor, _spent(tmp_path, retry_count=0, builder_mode="initial",
                                                      openfoam_workspace=""))
    assert st["executor_failed_gate"] == "builder_budget"
    assert st["executor_failure_cause"] == "run_infrastructure"
    what, _ = describe(st["executor_failure_cause"], st["executor_failure_facts"])
    assert "used up its time before a mesher could start" in what
    assert "crash" not in what and "stopped before it finished writing" not in what


def test_a_spent_run_after_a_reviewed_mesh_ends_on_that_mesh(tmp_path):
    st = _spent(tmp_path, executor_success=True, reviewer_verdict="FAIL", retry_count=2)
    out = asyncio.run(agent.node_builder(st))
    assert out["builder_stop"] == "builder_time_spent"
    assert out["retry_count"] == 2 and out["openfoam_workspace"] == st["openfoam_workspace"]
    assert route_after_builder({**st, **out}) == "__end__"


def test_a_no_progress_stop_says_the_repeated_failure(tmp_path, quiet_executor, monkeypatch):
    import meshpipeline.agents.builder.no_progress as no_progress
    monkeypatch.setattr(no_progress, "repeats_previous", lambda ws, sig: True)
    monkeypatch.setattr(no_progress, "failure_signature", lambda state: {"gate": "fea_guard"})
    st = _guard_tripped(tmp_path, builder_deadline_epoch=0.0,
                        pipeline_deadline_epoch=time.time() + 3600)
    st = _through_the_executor(quiet_executor, st)
    what, _ = describe(st["executor_failure_cause"], st["executor_failure_facts"])
    assert "stopped making progress" in what and "failed the same way twice" in what
