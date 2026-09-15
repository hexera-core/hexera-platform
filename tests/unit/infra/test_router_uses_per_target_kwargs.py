# Responsibility: Verify the router builds its call parameters from the target, not a frozen dict.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import router
from meshpipeline.contracts.model_routing import RouteTarget


def test_for_target_takes_a_role_and_returns_that_roles_kwargs():
    target = RouteTarget(provider="deepinfra", model="zai-org/GLM-5.2",
                         account="default", circuit_group="deepinfra")
    kw = router._for_target("builder", target)
    assert kw["model"] == "zai-org/GLM-5.2"
    assert kw["stream"] is True
    assert kw["extra_body"]["min_p"] == pytest.approx(0.05)


def test_for_target_follows_the_target_not_the_roles_default_model():
    # The point of the refactor: the same role, a different target, different parameters.
    target = RouteTarget(provider="deepseek", model="deepseek-v4-pro",
                         account="default", circuit_group="deepseek")
    kw = router._for_target("intake", target)
    assert kw["model"] == "deepseek-v4-pro"
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}


def test_the_router_no_longer_imports_the_shipped_kwargs_dicts():
    src = (router.__file__)
    text = open(src, encoding="utf-8").read()
    for name in ("BUILDER_CALL_KWARGS", "REVIEWER_CALL_KWARGS", "INTAKE_CALL_KWARGS",
                 "SUMMARIZER_CALL_KWARGS", "_planner_call_kwargs"):
        assert name not in text, (
            f"router.py still reaches for {name}; a frozen per-role dict cannot express a "
            "provider that rejects min_p or top_k")
