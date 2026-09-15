# Responsibility: Pin the exact kwargs every REAL route produces through kwargs_for, keyed by
# the route's own `.role` - not a hardcoded role string.
# Boundaries: this is a golden-value guard on the live DeepInfra/DeepSeek wire shape, not a
# behaviour test; it exists to catch two failure modes together:
#   1. a role added to ROUTE_MATRIX without a matching spec_for() branch (KeyError in prod), and
#   2. a future edit to _openai_wire silently changing what a live provider is sent.
# Collaborates with: call_kwargs.py (spec_for, kwargs_for), and every role's settings module,
# whose constants are referenced here (never re-typed as literals) so an operator's env override
# moves the expectation instead of silently invalidating it.
from __future__ import annotations

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
import meshpipeline.engines.snappy.settings as pcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.adapters.model_inference import protocols
from meshpipeline.adapters.model_inference.protocols.chat_completions import ChatCompletions
from meshpipeline.agent_tools.shared.settings import SUMMARIZER_ROUTE
from meshpipeline.agents.builder.settings import BUILDER_ROUTE
from meshpipeline.agents.intake.settings import INTAKE_ROUTE
from meshpipeline.agents.reviewer.settings import VISUAL_REVIEWER_ROUTE
from meshpipeline.engines.snappy.settings import PLANNER_ROUTE

_THINKING_OFF = {"thinking": {"type": "disabled"}}


def _kwargs_for_route(route):
    # PROTOCOL-BLIND UNTIL NOW. Every expectation in this file is a chat-completions kwargs dict,
    # and kwargs_for only serves the providers that speak that format - `openai` is deliberately
    # absent from _BUILDERS because a Responses request is not a kwargs dict at all. So the first
    # role pointed at `openai` would have hard-failed this golden guard with a KeyError in the
    # middle of a cutover, which reads as "the cutover broke sampling" rather than "this test
    # asks a question that does not apply to that wire format". The protocol seam is the
    # authority on which question applies, so ask it rather than listing provider names here.
    # ORDER MATTERS, AND IS NOT TIDINESS. spec_for() is resolved BEFORE the skip below, because
    # using route.role (not a literal) is the only thing binding ROUTE_MATRIX's roles to
    # spec_for's roles - a role added to one without the other fails here with a clear KeyError
    # instead of surfacing as a per-attempt KeyError in production. That binding holds for EVERY
    # protocol: build_request() is handed a SamplingSpec too, so a Responses route consults
    # spec_for on every attempt exactly as a chat route does. Skipping first would take the
    # binding check away from the very roles the skip exists for.
    spec = ck.spec_for(route.role)
    protocol = protocols.protocol_for(route.primary.provider)
    if not isinstance(protocol, ChatCompletions):
        pytest.skip(
            f"{route.role} is routed at {route.primary.label}, which speaks "
            f"{type(protocol).__name__}, not chat completions - its request shape is pinned by "
            "tests/unit/infra/test_responses_request.py, not by a kwargs golden")
    return ck.kwargs_for(route.primary, spec)


def test_builder_route_golden_kwargs():
    assert _kwargs_for_route(BUILDER_ROUTE) == {
        "model": BUILDER_ROUTE.primary.model,
        "temperature": bcfg.BUILDER_TEMPERATURE,
        "max_tokens": bcfg.BUILDER_MAX_TOKENS,
        "top_p": bcfg.BUILDER_TOP_P,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"min_p": bcfg.BUILDER_MIN_P},
    }


def test_planner_route_golden_kwargs():
    assert _kwargs_for_route(PLANNER_ROUTE) == {
        "model": PLANNER_ROUTE.primary.model,
        "temperature": pcfg.PLANNER_TEMPERATURE,
        "max_tokens": pcfg.PLANNER_MAX_TOKENS,
        "top_p": pcfg.PLANNER_TOP_P,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"min_p": pcfg.PLANNER_MIN_P},
    }


def test_visual_reviewer_route_golden_kwargs():
    assert _kwargs_for_route(VISUAL_REVIEWER_ROUTE) == {
        "model": VISUAL_REVIEWER_ROUTE.primary.model,
        "temperature": rcfg.REVIEWER_TEMPERATURE,
        "max_tokens": rcfg.REVIEWER_MAX_TOKENS,
        "top_p": rcfg.REVIEWER_TOP_P,
        "presence_penalty": rcfg.REVIEWER_PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"top_k": rcfg.REVIEWER_TOP_K},
    }


def test_intake_route_golden_kwargs():
    assert _kwargs_for_route(INTAKE_ROUTE) == {
        "model": INTAKE_ROUTE.primary.model,
        "temperature": icfg.INTAKE_TEMPERATURE,
        "max_tokens": icfg.INTAKE_MAX_TOKENS,
        "extra_body": {**_THINKING_OFF, "min_p": icfg.INTAKE_MIN_P},
    }


def test_summarizer_route_golden_kwargs():
    assert _kwargs_for_route(SUMMARIZER_ROUTE) == {
        "model": SUMMARIZER_ROUTE.primary.model,
        "temperature": provcfg.SEARCH_SUMMARIZER_TEMPERATURE,
        "max_tokens": provcfg.SEARCH_SUMMARIZER_MAX_TOKENS,
        "extra_body": dict(_THINKING_OFF),
    }


# The five cases above name their routes, so a SIXTH role would never reach them - the header's
# first guarantee was written for a test that enumerates, which cannot see what it does not name.
# These two close that: one walks the registry itself, the other pins the refusal at the far end.
def test_every_configured_route_has_a_sampling_spec():
    from meshpipeline.adapters.model_inference.routes import all_routes
    for role, route in sorted(all_routes().items()):
        ck.spec_for(route.role)       # KeyError here means a role was added without a branch


def test_an_unknown_role_is_refused_rather_than_defaulted():
    # A role with no branch must FAIL, never fall back to some other role's sampling: silently
    # meshing at another role's temperature is the kind of wrong answer nobody goes looking for.
    with pytest.raises(KeyError, match="summariser"):
        ck.spec_for("summariser")     # British spelling is not a role
