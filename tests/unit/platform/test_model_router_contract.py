# Responsibility: Verify the router implements every role operation, and the neutral port names no vendor.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.adapters.model_inference import router
from meshpipeline.contracts import model_inference

# Every logical role the product can call, and the port function that reaches it.
ROLE_OPERATIONS = [
    "call_builder_model",
    "call_planner_model",
    "call_intake_model",
    "call_reviewer_with_tools",
    "call_summarizer_model",
]


@pytest.mark.parametrize("operation", ROLE_OPERATIONS)
def test_the_concrete_router_implements_every_role_operation(operation):
    fn = getattr(router, operation, None)
    assert fn is not None, f"the router does not implement {operation}"
    assert inspect.iscoroutinefunction(fn), f"{operation} must be awaitable"


@pytest.mark.parametrize("operation", ROLE_OPERATIONS)
def test_the_neutral_port_exposes_every_role_operation(operation):
    fn = getattr(model_inference, operation, None)
    assert fn is not None, f"the neutral port does not expose {operation}"
    assert inspect.iscoroutinefunction(fn)


def test_the_router_satisfies_the_model_router_protocol():
    assert isinstance(router, model_inference.ModelRouter)


def test_the_protocol_and_the_port_agree_on_the_role_set():
    protocol_ops = {n for n in dir(model_inference.ModelRouter)
                    if n.startswith("call_") and not n.startswith("_")}
    port_ops = {n for n in dir(model_inference)
                if n.startswith("call_") and inspect.iscoroutinefunction(getattr(model_inference, n))}
    assert protocol_ops == port_ops == set(ROLE_OPERATIONS)


def test_the_port_imports_no_vendor_sdk_and_names_no_vendor_operation():
    import ast

    tree = ast.parse(inspect.getsource(model_inference))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for vendor in ("openai", "anthropic", "httpx", "redis", "google"):
        assert vendor not in imported, f"the neutral port imports the {vendor!r} SDK"

    for op in ROLE_OPERATIONS:
        low = op.lower()
        for vendor in ("deepinfra", "deepseek", "fireworks", "together", "glm", "qwen", "openai"):
            assert vendor not in low, f"operation {op!r} names a provider"


def test_the_port_did_not_grow_an_unrestricted_call_any_model():
    assert not hasattr(model_inference, "call_any_model")
    assert not hasattr(model_inference, "call_model")


async def test_an_unconfigured_router_fails_clearly_for_a_new_role():
    model_inference.set_model_router(None)
    try:
        with pytest.raises(model_inference.ModelInferenceError, match="set_model_router"):
            await model_inference.call_planner_model([])
    finally:
        model_inference.set_model_router(router)
