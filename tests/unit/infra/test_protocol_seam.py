# Responsibility: Verify each provider resolves to a wire protocol, and an unknown one is refused.
# Boundaries: the registry and its contract; the protocols' own behaviour is tested by their suites.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.adapters.model_inference import protocols


@pytest.mark.parametrize("provider", ["deepinfra", "deepseek"])
def test_a_wire_provider_resolves_to_the_chat_completions_protocol(provider):
    proto = protocols.protocol_for(provider)
    assert proto.__class__.__name__ == "ChatCompletions"


def test_an_unregistered_provider_is_refused_and_names_what_exists():
    with pytest.raises(KeyError, match="cohere"):
        protocols.protocol_for("cohere")


def test_every_protocol_satisfies_the_interface():
    # A protocol missing normalize() would fail at the end of a real call, after the
    # provider has already been paid for the tokens.
    for provider in protocols.registered():
        proto = protocols.protocol_for(provider)
        assert inspect.iscoroutinefunction(proto.invoke), provider
        assert callable(proto.normalize), provider
