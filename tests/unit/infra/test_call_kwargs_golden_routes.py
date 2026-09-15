# Responsibility: Pin the exact chat-completions kwargs every REAL route's sampling spec
# produces, keyed by the route's own `.role` - not a hardcoded role string.
# Boundaries: this is a golden-value guard on the DeepInfra/DeepSeek wire shape, not a behaviour
# test; it exists to catch two failure modes together:
#   1. a role added to ROUTE_MATRIX without a matching spec_for() branch (KeyError in prod), and
#   2. a future edit to _openai_wire silently changing what a chat provider is sent.
# WHY THE TARGET IS EXPLICIT AND THE ROUTE'S OWN TARGET IS NOT USED. Every expectation below is
# a chat-completions kwargs dict, and kwargs_for only serves the providers that speak that
# format. All five roles are routed at `openai` now, which speaks Responses - a kwargs dict is
# not a question that format can answer, so reading the live target would leave this file
# skipping every case it was written for. The ROLE half of each expectation (its settings
# module's constants) still comes from the live route; only the TARGET is pinned, so what is
# asserted is "this role's sampling intent, rendered for a chat provider".
# Collaborates with: call_kwargs.py (spec_for, kwargs_for), tests/unit/infra/test_responses_request.py
# (which pins the shape the deployed routes actually send), and every role's settings module,
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
from meshpipeline.contracts.model_routing import RouteTarget
from meshpipeline.engines.snappy.settings import PLANNER_ROUTE

_THINKING_OFF = {"thinking": {"type": "disabled"}}

#: The chat targets these goldens describe - the providers each role was served by before the
#: OpenAI cutover, and the two a deployment can still point a role back at. Declared here rather
#: than read from the catalogue: an expectation derived from the thing it checks proves nothing.
_CHAT_TARGET = {
    "builder":         RouteTarget(provider="deepinfra", model="zai-org/GLM-5.2",
                                   account="default", circuit_group="builder"),
    "planner":         RouteTarget(provider="deepinfra", model="zai-org/GLM-5.2",
                                   account="default", circuit_group="planner"),
    "visual_reviewer": RouteTarget(provider="deepinfra",
                                   model="Qwen/Qwen3-VL-235B-A22B-Thinking",
                                   account="default", circuit_group="reviewer"),
    "intake":          RouteTarget(provider="deepseek", model="deepseek-v4-pro",
                                   account="default", circuit_group="intake"),
    "summarizer":      RouteTarget(provider="deepseek", model="deepseek-v4-flash",
                                   account="default", circuit_group="summarizer"),
}


def _chat_kwargs_for(route):
    # ORDER MATTERS, AND IS NOT TIDINESS. spec_for() is resolved FIRST, because using route.role
    # (not a literal) is the only thing binding ROUTE_MATRIX's roles to spec_for's roles - a role
    # added to one without the other fails here with a clear KeyError instead of surfacing as a
    # per-attempt KeyError in production. That binding holds for EVERY protocol: build_request()
    # is handed a SamplingSpec too, so a Responses route consults spec_for on every attempt
    # exactly as a chat route does.
    spec = ck.spec_for(route.role)
    target = _CHAT_TARGET[route.role]
    # An ASSERTION, where a protocol-blind skip used to be. The skip read the LIVE route and so
    # fired for all five roles the moment they moved to Responses, quietly reducing this file to
    # its binding check. Pinned to an explicit chat provider the question always applies - and
    # this line is what says so, rather than assuming it.
    assert isinstance(protocols.protocol_for(target.provider), ChatCompletions), (
        f"{target.provider} no longer speaks chat completions; these goldens describe a wire "
        "format it has left")
    return ck.kwargs_for(target, spec)


def test_builder_route_golden_kwargs():
    assert _chat_kwargs_for(BUILDER_ROUTE) == {
        "model": _CHAT_TARGET["builder"].model,
        "temperature": bcfg.BUILDER_TEMPERATURE,
        "max_tokens": bcfg.BUILDER_MAX_TOKENS,
        "top_p": bcfg.BUILDER_TOP_P,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"min_p": bcfg.BUILDER_MIN_P},
    }


def test_planner_route_golden_kwargs():
    assert _chat_kwargs_for(PLANNER_ROUTE) == {
        "model": _CHAT_TARGET["planner"].model,
        "temperature": pcfg.PLANNER_TEMPERATURE,
        "max_tokens": pcfg.PLANNER_MAX_TOKENS,
        "top_p": pcfg.PLANNER_TOP_P,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"min_p": pcfg.PLANNER_MIN_P},
    }


def test_visual_reviewer_route_golden_kwargs():
    assert _chat_kwargs_for(VISUAL_REVIEWER_ROUTE) == {
        "model": _CHAT_TARGET["visual_reviewer"].model,
        "temperature": rcfg.REVIEWER_TEMPERATURE,
        "max_tokens": rcfg.REVIEWER_MAX_TOKENS,
        "top_p": rcfg.REVIEWER_TOP_P,
        "presence_penalty": rcfg.REVIEWER_PRESENCE_PENALTY,
        "stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"top_k": rcfg.REVIEWER_TOP_K},
    }


def test_intake_route_golden_kwargs():
    assert _chat_kwargs_for(INTAKE_ROUTE) == {
        "model": _CHAT_TARGET["intake"].model,
        "temperature": icfg.INTAKE_TEMPERATURE,
        "max_tokens": icfg.INTAKE_MAX_TOKENS,
        "extra_body": {**_THINKING_OFF, "min_p": icfg.INTAKE_MIN_P},
    }


def test_summarizer_route_golden_kwargs():
    assert _chat_kwargs_for(SUMMARIZER_ROUTE) == {
        "model": _CHAT_TARGET["summarizer"].model,
        "temperature": provcfg.SEARCH_SUMMARIZER_TEMPERATURE,
        "max_tokens": provcfg.SEARCH_SUMMARIZER_MAX_TOKENS,
        "extra_body": dict(_THINKING_OFF),
    }


# The five cases above name their routes, so a SIXTH role would never reach them - the header's
# first guarantee was written for a test that enumerates, which cannot see what it does not name.
# These three close that: one walks the registry itself, one keeps the pinned-target table from
# falling behind it, and the last pins the refusal at the far end.
def test_every_configured_route_has_a_sampling_spec():
    from meshpipeline.adapters.model_inference.routes import all_routes
    for role, route in sorted(all_routes().items()):
        ck.spec_for(route.role)       # KeyError here means a role was added without a branch


def test_every_configured_role_has_a_pinned_chat_target():
    # Without this, a role added to ROUTE_MATRIX would simply have no golden here and nobody
    # would notice: the five named cases cannot miss what they never mention.
    from meshpipeline.adapters.model_inference.routes import all_routes
    assert sorted(all_routes()) == sorted(_CHAT_TARGET)


def test_an_unknown_role_is_refused_rather_than_defaulted():
    # A role with no branch must FAIL, never fall back to some other role's sampling: silently
    # meshing at another role's temperature is the kind of wrong answer nobody goes looking for.
    with pytest.raises(KeyError, match="summariser"):
        ck.spec_for("summariser")     # British spelling is not a role
