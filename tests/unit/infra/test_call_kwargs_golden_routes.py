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

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
import meshpipeline.engines.snappy.settings as pcfg
import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.agent_tools.shared.settings import SUMMARIZER_ROUTE
from meshpipeline.agents.builder.settings import BUILDER_ROUTE
from meshpipeline.agents.intake.settings import INTAKE_ROUTE
from meshpipeline.agents.reviewer.settings import VISUAL_REVIEWER_ROUTE
from meshpipeline.engines.snappy.settings import PLANNER_ROUTE

_THINKING_OFF = {"thinking": {"type": "disabled"}}


def _kwargs_for_route(route):
    # The point of using route.role (not a literal) is that this line is the only thing binding
    # ROUTE_MATRIX's roles to spec_for's roles - a role added to one without the other fails
    # here with a clear KeyError instead of surfacing as a per-attempt KeyError in production.
    return ck.kwargs_for(route.primary, ck.spec_for(route.role))


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
