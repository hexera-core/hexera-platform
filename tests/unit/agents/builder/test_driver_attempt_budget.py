# Responsibility: Verify a deterministic driver's attempt may use what the builder has left (PLAN-fea-13), while the model loop keeps its per-turn slice.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshpipeline.agents.builder import invoke


def test_a_driver_gets_the_builders_remaining_budget_and_the_loop_its_slice(monkeypatch):
    seen = {}

    async def fake_driver_run(driver, attempt, state, *, job_id, publish, timeout_s):
        seen["driver"] = timeout_s
        return True, "", None

    async def fake_loop(attempt, state, *, job_id, publish, timeout_s):
        seen["loop"] = timeout_s
        return "", []

    monkeypatch.setattr(invoke, "_run_engine_driver", fake_driver_run)
    monkeypatch.setattr(invoke, "_run_shared_loop", fake_loop)
    class _Spec:
        def __init__(self, driver):
            self.driver = driver

        def build_driver_for(self, _state):
            return self.driver

    monkeypatch.setattr(invoke, "get_spec", lambda _e: _Spec(lambda *a, **k: None))
    attempt = SimpleNamespace(engine="gmsh")
    asyncio.run(invoke.run_attempt(attempt, {}, job_id="t", publish=None, timeout_s=3600, driver_timeout_s=9000))
    assert seen["driver"] == 9000
    monkeypatch.setattr(invoke, "get_spec", lambda _e: _Spec(None))
    asyncio.run(invoke.run_attempt(attempt, {}, job_id="t", publish=None, timeout_s=3600, driver_timeout_s=9000))
    assert seen["loop"] == 3600
