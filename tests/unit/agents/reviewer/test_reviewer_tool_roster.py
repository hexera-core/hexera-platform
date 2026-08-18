# Responsibility: Verify every offered tool is executable by the runtime, and the model declares no aggregate verdict.
from __future__ import annotations

import pytest

from meshpipeline.agents.reviewer.render_runtime import (
    VERDICT_TOOLS,
    VIEWER_CONFIGURE_TOOLS,
    VIEWER_RENDERING_TOOLS,
)
from meshpipeline.agents.reviewer.tools import ACTIONS, REVIEWER_TOOLS
from meshpipeline.agents.reviewer.unified import UNIFIED_TOOLS


def _names(roster) -> set[str]:
    return {t["function"]["name"] for t in roster}


def test_every_offered_tool_is_one_the_runtime_can_execute():
    offered = _names(UNIFIED_TOOLS)
    executable = VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS | {"submit_findings"}
    assert offered <= executable, offered - executable


def test_the_declared_roster_is_exactly_the_viewer_tools():
    assert _names(REVIEWER_TOOLS) == (VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS
                                      | VERDICT_TOOLS)


def test_every_viewer_tool_has_a_user_facing_action_word():
    viewer = VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS
    assert viewer <= set(ACTIONS), viewer - set(ACTIONS)


def test_the_reviewer_has_exactly_one_submission_tool():
    submissions = [t for t in UNIFIED_TOOLS
                   if t["function"]["name"] not in VIEWER_RENDERING_TOOLS | VIEWER_CONFIGURE_TOOLS]
    assert [t["function"]["name"] for t in submissions] == ["submit_findings"]


def test_the_model_is_never_asked_to_declare_an_aggregate_verdict():
    assert VERDICT_TOOLS == frozenset()
    import inspect

    from meshpipeline.agents.reviewer import loop_policy, unified
    for mod in (loop_policy, unified):
        src = inspect.getsource(mod)
        assert 'return "FAIL" if' not in src and 'return "PASS" if' not in src


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
