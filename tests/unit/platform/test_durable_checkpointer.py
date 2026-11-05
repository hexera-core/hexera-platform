# Responsibility: Verify production requires a durable checkpointer that no environment value can turn off.
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
PIPELINE_RUN = REPO / "src" / "meshpipeline" / "application" / "pipeline_run.py"


def test_dev_default_does_not_require_durable_checkpointer():
    import meshpipeline.settings.policy as polcfg
    # the ambient test env is dev/ci → durable not required (MemorySaver opt-in allowed)
    assert polcfg.ENV in {"dev", "development", "local", "test", "testing", "ci"}
    assert polcfg.REQUIRE_DURABLE_CHECKPOINTER is False


def test_production_requires_a_durable_checkpointer(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("MESH_API_KEY", "k")          # policy refuses prod without auth secrets
    monkeypatch.setenv("USER_TOKEN_SECRET", "s")
    import meshpipeline.settings.policy as polcfg
    try:
        pol = importlib.reload(polcfg)
        assert pol.REQUIRE_DURABLE_CHECKPOINTER is True
    finally:
        for k in ("ENV", "MESH_API_KEY", "USER_TOKEN_SECRET"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(polcfg)


def test_production_cannot_disable_durable_checkpointing_by_env_value(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.settings.env import ConfigurationError
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("MESH_API_KEY", "k")
    monkeypatch.setenv("USER_TOKEN_SECRET", "s")
    try:
        for falsey in ("false", "0", "no", "off", "FALSE"):
            monkeypatch.setenv("REQUIRE_DURABLE_CHECKPOINTER", falsey)
            with pytest.raises(ConfigurationError, match="cannot be disabled"):
                importlib.reload(polcfg)
    finally:
        for k in ("ENV", "MESH_API_KEY", "USER_TOKEN_SECRET", "REQUIRE_DURABLE_CHECKPOINTER"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(polcfg)


def test_production_forces_durable_on_even_when_the_env_says_nothing(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setenv("ENV", "staging")            # any non-dev environment, not just 'production'
    monkeypatch.setenv("MESH_API_KEY", "k")
    monkeypatch.setenv("USER_TOKEN_SECRET", "s")
    try:
        assert importlib.reload(polcfg).REQUIRE_DURABLE_CHECKPOINTER is True
    finally:
        for k in ("ENV", "MESH_API_KEY", "USER_TOKEN_SECRET"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(polcfg)


def test_explicit_override_can_require_durable_even_in_dev(monkeypatch):
    monkeypatch.setenv("REQUIRE_DURABLE_CHECKPOINTER", "true")
    import meshpipeline.settings.policy as polcfg
    try:
        assert importlib.reload(polcfg).REQUIRE_DURABLE_CHECKPOINTER is True
    finally:
        monkeypatch.delenv("REQUIRE_DURABLE_CHECKPOINTER", raising=False)
        importlib.reload(polcfg)


def test_memorysaver_fallback_is_guarded_by_require_durable():
    src = PIPELINE_RUN.read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_async")
    # find the MemorySaver() call and the guard that must precede it in the same except handler
    has_memorysaver = any(
        isinstance(n, ast.Call) and getattr(n.func, "id", "") == "MemorySaver"
        for n in ast.walk(fn))
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.Attribute) and n.attr == "REQUIRE_DURABLE_CHECKPOINTER"]
    assert has_memorysaver, "MemorySaver fallback not found (structure changed - update this guard)"
    assert guards, "the MemorySaver fallback is not guarded by REQUIRE_DURABLE_CHECKPOINTER"
    # and the guard must raise (fail closed), not merely log
    raises_in_fn = any(isinstance(n, ast.Raise) for n in ast.walk(fn))
    assert raises_in_fn, "the durable-checkpointer guard must fail closed (raise), not warn"
