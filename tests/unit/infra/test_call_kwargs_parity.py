# Responsibility: Verify the per-target kwargs builder reproduces the shipped dicts exactly.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.contracts.model_routing import RouteTarget


def _target(provider: str, model: str) -> RouteTarget:
    return RouteTarget(provider=provider, model=model, account="default",
                       circuit_group=provider)


def _shipped(role: str) -> dict:
    """The dicts as they exist today, read from the modules that own them."""
    from meshpipeline.adapters.model_inference import deepinfra, deepseek
    if role == "builder":
        return dict(deepinfra.BUILDER_CALL_KWARGS)
    if role == "planner":
        return dict(deepinfra._planner_call_kwargs())
    if role == "visual_reviewer":
        return dict(deepinfra.REVIEWER_CALL_KWARGS)
    if role == "intake":
        return dict(deepseek.INTAKE_CALL_KWARGS)
    if role == "summarizer":
        return dict(deepseek.SUMMARIZER_CALL_KWARGS)
    raise AssertionError(role)


ROLE_TARGET = {
    "builder":         ("deepinfra", "zai-org/GLM-5.2"),
    "planner":         ("deepinfra", "zai-org/GLM-5.2"),
    "visual_reviewer": ("deepinfra", "Qwen/Qwen3-VL-235B-A22B-Thinking"),
    "intake":          ("deepseek",  "deepseek-v4-pro"),
    "summarizer":      ("deepseek",  "deepseek-v4-flash"),
}


@pytest.mark.parametrize("role", sorted(ROLE_TARGET))
def test_the_builder_reproduces_the_shipped_kwargs_exactly(role):
    provider, model = ROLE_TARGET[role]
    built = ck.kwargs_for(_target(provider, model), ck.spec_for(role))
    expected = _shipped(role)
    expected["model"] = model
    assert built == expected, (
        f"{role}: the per-target builder changed the call. This refactor must be "
        f"behaviour-preserving for deepinfra and deepseek.")


def test_an_unknown_role_is_refused_rather_than_defaulted():
    with pytest.raises(KeyError, match="summariser"):
        ck.spec_for("summariser")   # British spelling is not a role
