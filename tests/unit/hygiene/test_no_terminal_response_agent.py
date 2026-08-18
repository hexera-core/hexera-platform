# Responsibility: Verify no model role, prompt or graph node owns the terminal response.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]

# Role names that would mean "a model composes the terminal user-facing result", whatever the
# term of art. Matched against the RUNTIME route registry, not against text.
FORBIDDEN_ROLE_SUBSTRINGS = (
    "closer", "closing", "finalizer", "finalization", "finalize",
    "outcome", "result", "terminal", "response",
)


def test_no_registered_model_role_owns_the_terminal_response():
    from meshpipeline.adapters.model_inference.routes import all_routes
    for role in all_routes():
        low = role.lower()
        for bad in FORBIDDEN_ROLE_SUBSTRINGS:
            assert bad not in low, (
                f"model role {role!r} looks like a terminal-response agent (matched {bad!r}). "
                "The final verdict is application-rendered; no model role may own it.")


def test_the_registered_roles_are_exactly_the_live_model_callers():
    from meshpipeline.adapters.model_inference.routes import all_routes
    assert sorted(all_routes()) == [
        "builder", "intake", "planner", "summarizer", "visual_reviewer",
    ]


def test_the_prompt_registry_loads_no_terminal_prompt():
    from meshpipeline.settings.policy import REQUIRED_PROMPTS
    for attr, fname in REQUIRED_PROMPTS:
        for bad in ("closer", "outcome", "final", "result", "terminal"):
            assert bad not in attr.lower(), f"prompt registry entry {attr!r}"
            assert bad not in fname.lower(), f"prompt registry file {fname!r}"


def test_graph_construction_registers_no_terminal_node():
    from unittest.mock import patch

    import meshpipeline.pipeline.graph as graph_mod

    class _Recorder:
        def __init__(self, *a, **k):
            self.nodes: set[str] = set()

        def add_node(self, name, fn):
            self.nodes.add(name)

        def add_edge(self, *a, **k):
            pass

        def add_conditional_edges(self, *a, **k):
            pass

        def compile(self, **k):
            return self

    rec = _Recorder()
    with patch.object(graph_mod, "StateGraph", return_value=rec):
        graph_mod.build_graph(checkpointer=object())

    assert rec.nodes, "no nodes recorded - the sweep would be vacuous"
    for node in rec.nodes:
        low = node.lower()
        for bad in ("closer", "outcome", "final", "result", "terminal", "closing"):
            assert bad not in low, (
                f"graph registered a terminal node {node!r}. The graph ends at execution; "
                "the terminal verdict is produced by application code afterwards.")
