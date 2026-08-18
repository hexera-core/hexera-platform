# Responsibility: Verify the attempt deadline is set once, survives a retry, and reports exhaustion truthfully.
from __future__ import annotations

import asyncio
import time
import types

import pytest

import meshpipeline.agents.builder.agent as agent
import meshpipeline.agents.builder.attempt_capture as attempt_capture
import meshpipeline.agents.builder.invoke as invoke
import meshpipeline.agents.builder.settings as bcfg


# The builder node now publishes through the ownership-checked port. These tests exercise the
# node's own contract, not Redis or PostgreSQL, so the port is stood in for; nothing about
# ownership is asserted here - the gated publisher has its own suites.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)


class _DriverProbe:
    called = False

    async def __call__(self, workspace, state, *, job_id, publish, source_path):
        _DriverProbe.called = True
        return True, "authored"


def _run(state, driver):
    class _Spec:
        build_driver = staticmethod(driver)

    # Since the engine driver is resolved by `invoke` and the capture record is written
    # by `attempt_capture`; the node holds neither.
    orig_get_spec = invoke.get_spec if hasattr(invoke, "get_spec") else None
    orig_tl = attempt_capture.TrainingLogger
    invoke.get_spec = lambda *_a, **_k: _Spec()
    attempt_capture.TrainingLogger = lambda *a, **k: types.SimpleNamespace(
        log=lambda *a, **k: None)
    try:
        return asyncio.run(agent.node_builder(state))
    finally:
        if orig_get_spec is None:
            del invoke.get_spec
        else:
            invoke.get_spec = orig_get_spec
        attempt_capture.TrainingLogger = orig_tl


def _base_state(**over):
    st = {
        "job_id": "j", "engine": "cfmesh", "retry_count": 0, "builder_mode": "initial",
        "geometry": {}, "request_txt": "r", "review_brief_txt": "",
        "intake_patches": [], "openfoam_workspace": "",
    }
    st.update(over)
    return st


def test_deadline_is_set_once_on_the_first_attempt():
    before = time.time()
    out = _run(_base_state(), _DriverProbe())
    expected = before + bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS
    assert "builder_deadline_epoch" in out
    assert abs(out["builder_deadline_epoch"] - expected) < 30


def test_retry_does_not_reset_the_deadline():
    fixed = time.time() + 4321.0
    out = _run(_base_state(retry_count=1, builder_mode="retry", builder_deadline_epoch=fixed),
               _DriverProbe())
    assert out["builder_deadline_epoch"] == fixed


def test_exhausted_budget_fails_truthfully_without_a_new_attempt():
    _DriverProbe.called = False
    past = time.time() - 5.0
    out = _run(_base_state(retry_count=1, builder_mode="retry", builder_deadline_epoch=past),
               _DriverProbe())
    assert _DriverProbe.called is False, "an exhausted budget must not start another build attempt"
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1
    assert "executor_success" not in out
    assert out["builder_deadline_epoch"] == past   # carried, still not reset


def test_total_budget_is_finite_and_below_naive_worst_case():
    assert bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS > 0
    naive = bcfg.BUILDER_LOOP_TIMEOUT * bcfg.BUILDER_MAX_TOTAL_ATTEMPTS
    assert bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS < naive, (
        f"aggregate budget {bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS}s is not below the naive worst case "
        f"{naive}s it is meant to bound")
