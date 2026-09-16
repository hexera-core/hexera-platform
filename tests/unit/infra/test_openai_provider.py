# Responsibility: Verify the openai provider label resolves to a credentialled client, and refuses without one.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import providers
from meshpipeline.contracts.model_routing import RouteTarget
from meshpipeline.settings.env import ConfigurationError


def _target(model: str = "gpt-5.6-terra") -> RouteTarget:
    return RouteTarget(provider="openai", model=model, account="default",
                       circuit_group="openai")


def test_the_openai_label_resolves_to_a_client(monkeypatch):
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "sk-test")
    client = providers.client_for(_target())
    assert client.chat.completions is not None


def test_an_openai_client_exposes_the_responses_resource_the_protocol_calls(monkeypatch):
    # A PIN ON THE SDK FLOOR, not on this module. `client.responses` first shipped in
    # openai-python 1.66.0; requirements pinned 1.59.3, which has no responses resource at all -
    # so protocols/responses.py's `client.responses.create(...)` raised AttributeError, which
    # providers.classify cannot read and therefore calls APPLICATION_DEFECT: neither retryable
    # nor failover-eligible, one attempt, terminal, and a log line blaming a defect in this
    # product for a dependency that was never installed. Asserted here so a downgrade of the
    # openai pin fails in CI instead of on the first production call.
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "sk-test")
    client = providers.client_for(_target())
    assert getattr(client, "responses", None) is not None, (
        "the installed openai SDK has no `responses` resource - see requirements/runtime.txt")
    assert callable(client.responses.create)


def test_an_uncredentialled_openai_route_refuses_with_the_variable_name(monkeypatch):
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        providers.client_for(_target())


def test_an_unknown_provider_names_every_configured_one():
    unknown = RouteTarget(provider="cohere", model="x", account="default",
                          circuit_group="x")
    with pytest.raises(ValueError, match="openai"):
        providers.client_for(unknown)
