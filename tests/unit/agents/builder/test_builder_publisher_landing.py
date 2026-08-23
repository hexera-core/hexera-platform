# Responsibility: Verify every event the builder's one publisher reaches is published under an ownership check.
# Boundaries: the closure of the publisher built in node_builder - other construction roots are their own units.
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

import meshpipeline.agents.builder.agent as agent

SRC = pathlib.Path(agent.__file__).resolve().parents[2]

#: Every module the publisher built in `node_builder` reaches, and the gated methods it must
#: publish through there. Derived once by static dataflow and by runtime object identity across
#: all five engines; this table is what the closure looks like after the migration.
CLOSURE = {
    "agents/builder/agent.py": {"anote": 4, "aattempt": 1},  # 4th: the no-progress stop tells the user WHY the ladder halted
    "agents/builder/loop.py": {"anote": 1},
    "agents/builder/executor.py": {"ameshed": 1, "awarn": 1, "afile": 1, "asearch": 1,
                                  "ameshing": 1},
    "agents/loop/tracing.py": {"atool_call": 1, "atool_result": 1, "areasoning": 2},
    "engines/snappy/drivers.py": {"ameshing": 1, "aerror": 2, "anote": 12, "ameshed": 2,
                                  "atool_call": 1, "atool_result": 1},
    "engines/snappy/planner.py": {"areasoning": 2},
    "contracts/rationale.py": {"arationale": 1},
}

#: Two modules carry BOTH routes: reviewer and intake keep the synchronous siblings there, and
#: only these functions are inside the builder's closure.
SCOPES = {
    "contracts/rationale.py": {"_asay"},
    "agents/loop/tracing.py": {"atool_call", "atool_result", "around_begin", "around_end"},
}

#: What a publisher may still be asked to do synchronously anywhere in the closure: nothing.
SYNC_EVENT_METHODS = {"note", "warn", "error", "stage", "attempt", "check", "action", "search",
                      "screenshot", "file", "reasoning", "rationale", "tool_call", "tool_result",
                      "meshing", "meshed", "verdict", "closing"}

GATED = {f"a{m}" for m in SYNC_EVENT_METHODS}


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text())


def _in_scope(tree: ast.AST, rel: str) -> list[ast.AST]:
    names = SCOPES.get(rel)
    if names is None:
        return [tree]
    found = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(found) == len(names), f"{rel} lost one of its closure functions: {names}"
    return found


def _publisher_calls(tree: ast.AST, rel: str):
    # A call on a name that is a publisher handle. The receiver is spelled consistently across
    # the codebase (`publish`, `_publish`, `self._publish`, `trace.publisher`).
    for scope in _in_scope(tree, rel):
        for node in ast.walk(scope):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            recv = node.func.value
            name = (recv.id if isinstance(recv, ast.Name)
                    else recv.attr if isinstance(recv, ast.Attribute) else "")
            if "publish" not in name.lower():
                continue
            yield node


# the root


def test_the_builder_node_builds_one_ownership_checked_publisher():
    node = next(n for n in ast.walk(_tree("agents/builder/agent.py"))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "node_builder")
    built = [n.func.id for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("publisher", "execution_publisher")]
    assert built == ["execution_publisher"], (
        f"node_builder constructs {built}; it must build exactly one ownership-checked publisher")


def _plain_publisher_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    # Every local spelling of the ungated factory: `from ...event_stream import publisher`,
    # the same with an `as` alias, and `import ...event_stream as x` used as `x.publisher`.
    direct: set[str] = set()
    modules: set[str] = {"meshpipeline.contracts.event_stream"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "contracts.event_stream"):
            for a in node.names:
                if a.name == "publisher":
                    direct.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in modules and a.asname:
                    modules.add(a.asname)
    return direct, modules


def test_no_plain_publisher_is_constructed_anywhere_in_the_closure():
    offenders = []
    for rel in CLOSURE:
        tree = _tree(rel)
        direct, modules = _plain_publisher_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            # `publisher(...)` under ANY local name it was imported as ...
            if isinstance(f, ast.Name) and f.id in direct:
                offenders.append(f"{rel}:{node.lineno} {f.id}(...)")
            # ... and `<event_stream module alias>.publisher(...)`
            elif isinstance(f, ast.Attribute) and f.attr == "publisher":
                base = f.value
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if name in modules or "event_stream" in name:
                    offenders.append(f"{rel}:{node.lineno} {name}.publisher(...)")
    assert offenders == [], (
        f"a second, ungated publisher is constructed inside the closure: {offenders}")


# the sites


@pytest.mark.parametrize("rel", sorted(CLOSURE))
def test_each_closure_module_publishes_exactly_its_gated_sites(rel):
    counted: dict[str, int] = {}
    for call in _publisher_calls(_tree(rel), rel):
        counted[call.func.attr] = counted.get(call.func.attr, 0) + 1
    assert counted == CLOSURE[rel], (
        f"{rel} no longer publishes what the closure says it does\n"
        f"  expected: {CLOSURE[rel]}\n  found:    {counted}")


def test_the_closure_is_the_size_the_migration_landed():
    total = sum(sum(m.values()) for m in CLOSURE.values())
    assert total == 37, (
        f"the closure is {total} sites, not the 36 it holds since the mesh run's\n"
        "announcement moved out of the tool and onto the executor's async side")


@pytest.mark.parametrize("rel", sorted(CLOSURE))
def test_no_synchronous_event_publication_remains_in_the_closure(rel):
    stragglers = [f"{rel}:{c.lineno} {c.func.attr}"
                  for c in _publisher_calls(_tree(rel), rel)
                  if c.func.attr in SYNC_EVENT_METHODS]
    assert stragglers == [], (
        f"these publications still reach Redis without an ownership check: {stragglers}")


@pytest.mark.parametrize("rel", sorted(CLOSURE))
def test_every_gated_publication_in_the_closure_is_awaited(rel):
    tree = _tree(rel)
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    unawaited = [f"{rel}:{c.lineno} {c.func.attr}" for c in _publisher_calls(tree, rel)
                 if c.func.attr in GATED and id(c) not in awaited]
    assert unawaited == [], (
        f"a gated publication is called without awaiting it, so the ownership check never runs "
        f"and the coroutine is discarded: {unawaited}")


# the trace boundary


def test_the_builder_traces_through_the_execution_context():
    src = (SRC / "agents/builder/loop.py").read_text()
    assert "ExecutionTraceContext(" in src, "the builder no longer builds an execution trace"
    assert "execution_trace=" in src, "the builder does not hand the loop its execution trace"
    assert "TraceContext(publisher" not in src.replace("ExecutionTraceContext(publisher", ""), \
        "the builder still builds a plain trace context"


@pytest.mark.parametrize("rel", ["agents/intake/agent.py"])
def test_intake_still_traces_synchronously(rel):
    src = (SRC / rel).read_text()
    assert "TraceContext(publisher" in src.replace("ExecutionTraceContext(publisher", ""), \
        f"{rel} stopped building the synchronous trace context"
    assert "ExecutionTraceContext" not in src, f"{rel} was migrated with the builder"
    assert "execution_trace=" not in src, f"{rel} routes through the builder's execution trace"


# one object, both branches


def _state(tmp_path, engine):
    return {"job_id": "job-landing", "engine": engine, "retry_count": 0,
            "builder_mode": "initial", "geometry": {}, "request_txt": "r",
            "review_brief_txt": "", "intake_patches": [], "openfoam_workspace": str(tmp_path)}


@pytest.mark.parametrize("engine,drives", [("snappy", True), ("cfmesh", False),
                                           ("gmsh", False), ("snappy_multiregion", False),
                                           ("vmtk", False)])
def test_the_one_publisher_reaches_whichever_branch_the_engine_takes(engine, drives, tmp_path,
                                                                     monkeypatch):
    import types

    from tests.execution_publisher_double import install

    import meshpipeline.agents.builder.attempt_capture as attempt_capture
    import meshpipeline.agents.builder.invoke as invoke
    import meshpipeline.agents.builder.loop as builder_loop

    made = install(monkeypatch, agent)
    reached: list = []

    async def _driver(workspace, st, *, job_id, publish, source_path, run):
        reached.append(publish)
        return True, "authored", run.outcome(produced_deliverable=True)

    async def _loop(messages, workspace, **kw):
        reached.append(kw.get("publish"))
        return "done", list(messages)

    monkeypatch.setattr(builder_loop, "_run_tool_loop", _loop)
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))
    monkeypatch.setattr(invoke, "get_spec",
                        lambda *_a, **_k: types.SimpleNamespace(
                            build_driver=_driver if drives else None), raising=False)

    asyncio.run(agent.node_builder(_state(tmp_path, engine)))

    assert len(made) == 1, f"{engine} built {len(made)} publishers, not one"
    assert reached and all(r is made[0] for r in reached), (
        f"{engine} did not carry the node's publisher into its branch: {reached}")
