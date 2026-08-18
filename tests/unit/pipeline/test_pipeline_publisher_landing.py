# Responsibility: Verify the pipeline's owned execution events are gated and its closing events are not.
# Boundaries: the pipeline lifecycle boundary only - reviewer, builder tools and maintenance are their own roots.
from __future__ import annotations

import ast
import pathlib

import pytest

import meshpipeline.pipeline.executor as executor_mod

SRC = pathlib.Path(executor_mod.__file__).resolve().parents[1]

#: The owned phase: these run inside the graph, under the claim taken before it started.
EXECUTION = {
    "pipeline/executor.py": {"astage": 1, "anote": 4, "acheck": 6},
    "pipeline/engine_select.py": {"astage": 1, "anote": 1},
    "pipeline/geometry_admission.py": {"astage": 1, "anote": 1},
}

#: The post-ownership phase: published exactly when the claim is gone, so they must NOT be gated.
CLOSING = {"application/pipeline_run.py": 5}

#: A separate authority again: maintenance runs with no claim at all.
MAINTENANCE = {"application/maintenance/cleanup.py": 1}

GATED_PREFIX = "a"
SYNC_EVENTS = {"stage", "note", "check", "closing", "error", "warn", "meshed", "meshing",
               "rationale", "reasoning", "attempt", "verdict", "file", "search", "screenshot",
               "action", "tool_call", "tool_result"}


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text())


def _publisher_calls(rel: str, tree=None):
    for node in ast.walk(tree if tree is not None else _tree(rel)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        recv = node.func.value
        name = (recv.id if isinstance(recv, ast.Name)
                else recv.attr if isinstance(recv, ast.Attribute)
                else ast.unparse(recv) if isinstance(recv, ast.Call) else "")
        if "pub" not in name.lower() and "publish" not in name.lower():
            continue
        yield node


# the owned phase


@pytest.mark.parametrize("rel", sorted(EXECUTION))
def test_each_owned_module_publishes_exactly_its_gated_events(rel):
    counted: dict[str, int] = {}
    for call in _publisher_calls(rel):
        counted[call.func.attr] = counted.get(call.func.attr, 0) + 1
    assert counted == EXECUTION[rel], (
        f"{rel} no longer publishes what the pipeline execution closure says it does\n"
        f"  expected: {EXECUTION[rel]}\n  found:    {counted}")


def test_the_pipeline_execution_closure_is_fifteen_sites():
    assert sum(sum(v.values()) for v in EXECUTION.values()) == 15


@pytest.mark.parametrize("rel", sorted(EXECUTION))
def test_no_synchronous_event_remains_in_the_owned_phase(rel):
    stragglers = [f"{rel}:{c.lineno} {c.func.attr}" for c in _publisher_calls(rel)
                  if c.func.attr in SYNC_EVENTS]
    assert stragglers == [], (
        f"these reach Redis without an ownership check while the claim is current: {stragglers}")


@pytest.mark.parametrize("rel", sorted(EXECUTION))
def test_every_owned_publication_is_awaited(rel):
    tree = _tree(rel)
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    unawaited = [f"{rel}:{c.lineno} {c.func.attr}" for c in _publisher_calls(rel, tree)
                 if c.func.attr.startswith(GATED_PREFIX) and id(c) not in awaited]
    assert unawaited == [], f"a gated publication is not awaited: {unawaited}"


@pytest.mark.parametrize("rel", sorted(EXECUTION))
def test_the_owned_phase_builds_only_the_execution_publisher(rel):
    src = (SRC / rel).read_text()
    assert "execution_publisher(" in src, f"{rel} does not construct the execution publisher"
    built = [n for n in ast.walk(_tree(rel))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "publisher"]
    assert built == [], f"{rel} still constructs a plain publisher at {[n.lineno for n in built]}"


def test_the_owned_phase_never_reaches_around_the_gate():
    for rel in EXECUTION:
        src = (SRC / rel).read_text()
        assert "_inner" not in src, f"{rel} reaches around the ownership check"


# the post-ownership phase


@pytest.mark.parametrize("rel,count", sorted(CLOSING.items()))
def test_the_closing_events_stay_on_the_post_ownership_authority(rel, count):
    calls = [c for c in _publisher_calls(rel) if c.func.attr in ("closing", "aclosing")]
    assert len(calls) == count, f"{rel} has {len(calls)} closing events, expected {count}"
    gated = [f"{rel}:{c.lineno}" for c in calls if c.func.attr == "aclosing"]
    assert gated == [], (
        "these closing events were gated on execution ownership, but they publish precisely "
        f"when the claim is gone - they would become unpublishable: {gated}")


def test_the_closing_authority_never_enters_the_pipeline_execution_nodes():
    # the executor, engine selection and geometry admission take no publisher argument at all:
    # each builds its own execution publisher, so nothing can hand them the closing one.
    for rel in EXECUTION:
        for node in ast.walk(_tree(rel)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            named = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
            leaked = [a for a in named if a in ("publish", "publisher", "_pub")]
            assert leaked == [], (
                f"{rel}::{node.name} accepts {leaked} - a publisher handed in from outside could "
                "be the post-ownership one")


# maintenance


@pytest.mark.parametrize("rel,count", sorted(MAINTENANCE.items()))
def test_maintenance_closing_is_its_own_ungated_authority(rel, count):
    calls = [c for c in _publisher_calls(rel) if c.func.attr in ("closing", "aclosing")]
    assert len(calls) == count
    assert [c.func.attr for c in calls] == ["closing"], (
        "maintenance runs with no execution claim; gating its closing event would make it "
        "unpublishable")
    assert "execution_publisher" not in (SRC / rel).read_text(), \
        "the maintenance route acquired an execution publisher"
