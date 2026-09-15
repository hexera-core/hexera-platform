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


def test_an_uncredentialled_openai_route_refuses_with_the_variable_name(monkeypatch):
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        providers.client_for(_target())


def test_an_unknown_provider_names_every_configured_one():
    unknown = RouteTarget(provider="cohere", model="x", account="default",
                          circuit_group="x")
    with pytest.raises(ValueError, match="openai"):
        providers.client_for(unknown)
