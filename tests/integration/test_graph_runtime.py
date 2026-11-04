# Responsibility: Verify the real graph compiles with every node and the engine seam resolves to a real runner.
import pytest

# Genuine absence -> honest missing-environment skip. Stub CONTAMINATION (the unit conftest
# ran in this process) is caught earlier and LOUDLY by tests/integration/conftest.py, so it can
# never masquerade as one of these skips.
for _dep in ("langgraph", "pyvista"):
    pytest.importorskip(_dep)

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

# Real runtime image (langgraph/pyvista) but NO external service - see the `hermetic` marker.
pytestmark = pytest.mark.hermetic

_EXPECTED_NODES = {
    "node_intake", "node_engine_select", "node_builder",
    "node_executor", "node_classifier", "node_reviewer",
    "node_failure_handler",
}


def test_real_graph_compiles_with_all_nodes():
    # Imports every node (agents/* + pipeline/*) and compiles the graph - fails if
    # any node import or the wiring broke. The single most valuable runtime guard.
    # STATE_SCHEMA_VERSION lives in the neutral state contract, not the graph composition
    # module (a node importing graph.py would be a cycle - see contracts/pipeline_state.py).
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
    from meshpipeline.pipeline.graph import build_graph
    g = build_graph(checkpointer=MemorySaver())
    nodes = set(g.get_graph().nodes)
    missing = _EXPECTED_NODES - nodes
    assert not missing, f"graph missing nodes: {missing}"
    assert STATE_SCHEMA_VERSION >= 1


def test_the_real_compiled_graph_has_no_terminal_response_node():
    from meshpipeline.pipeline.graph import build_graph
    g = build_graph(checkpointer=MemorySaver())
    for node in g.get_graph().nodes:
        low = str(node).lower()
        for bad in ("closer", "outcome", "final", "result", "terminal", "closing"):
            assert bad not in low, f"graph compiled a terminal-response node: {node}"


def test_engine_seam_resolves_to_real_runner():
    # The mesh-engine seam must forward to the concrete cfMesh runner at runtime.
    from meshpipeline.engines.runtime import get_engine
    eng = get_engine()
    assert eng.name == "cfmesh"
    assert callable(eng.inspect_stl)      # __getattr__ → openfoam.cfmesh_runner
    assert callable(eng.run_cartesian_mesh)


def test_engine_resolution_runs_on_real_code_without_crashing(monkeypatch):
    # A deterministic node runs end-to-end on real code. Topology is DERIVED from
    # the declared purpose (never read from engine_params - that key no longer
    # exists there); an unpinned internal-flow run resolves to an engine whose
    # capability produces internal topology, preferring the default when it
    # qualifies. No purpose, no pin → the deterministic default.
    import asyncio

    import meshpipeline.pipeline.engine_select as es
    from meshpipeline.engines.purposes import engines_producing_topology
    from meshpipeline.engines.registry import ENGINE_CATALOG, default_engine
    from meshpipeline.pipeline.engine_select import node_engine_select

    # The selection ANNOUNCEMENT is execution-owned: authorizing it is a PostgreSQL round trip
    # against a claim, which this hermetic module has no service to make. So the announcement is
    # recorded here and the node's own decision is what is asserted; that the announcement is
    # refused without a live claim, and published under one, is
    # tests/integration/test_pipeline_node_ownership.py's contract.
    announced: list = []

    async def _publish(job_id, text, op_id):
        announced.append({"job_id": job_id, "text": text, "op_id": op_id})

    monkeypatch.setattr(es, "_publish", _publish)

    out = asyncio.run(node_engine_select({"job_id": "it", "purpose": "internal_cfd"}))
    _internal = engines_producing_topology(ENGINE_CATALOG, "internal")
    _expected = default_engine() if default_engine() in _internal else sorted(_internal)[0]
    assert out == {"engine": _expected}
    out = asyncio.run(node_engine_select({"job_id": "it"}))
    assert out == {"engine": default_engine()}

    assert [a["op_id"] for a in announced] == ["forced", "resolved"], announced
    assert [a["text"] for a in announced] == [es.engine_choice_words(_expected, "topology_internal"),
                                              es.engine_choice_words(default_engine(), "default")], announced


def test_a_stalled_review_is_reviewed_again_and_its_verdict_ends_the_run(monkeypatch):
    # The REAL compiled graph with every node stubbed: the Windsor run's shape (job 53bbce4b) - a
    # validated mesh whose first review stalls - now ends on the second review's verdict instead of
    # in the failure sink. The rerun rebuilds nothing: retry_count is the executor's, unchanged.
    import asyncio

    import meshpipeline.agents.reviewer.settings as rcfg
    from meshpipeline.pipeline import graph as G

    reviews: list[int] = []

    async def _noop(state):
        return {}

    async def _executor(state):
        return {"executor_success": True, "retry_count": 1}

    async def _reviewer(state):
        reviews.append(int(state.get("review_rerun_count", 0) or 0))
        if len(reviews) == 1:
            return {"api_failure": "reviewer_stalled"}
        return {"reviewer_verdict": "PASS"}

    for name in ("node_intake", "node_engine_select", "node_geometry_admission",
                 "node_builder", "node_classifier", "node_engine_fallback"):
        monkeypatch.setattr(G, name, _noop)
    monkeypatch.setattr(G, "node_executor", _executor)
    monkeypatch.setattr(G, "node_reviewer", _reviewer)
    monkeypatch.setattr(rcfg, "REVIEWER_RERUN_MAX", 2)
    monkeypatch.setattr(rcfg, "REVIEWER_RERUN_BACKOFF_S", 0)

    g = G.build_graph(checkpointer=MemorySaver())
    assert "node_review_retry" in set(g.get_graph().nodes)
    final = asyncio.run(g.ainvoke(
        {"job_id": "j-windsor", "retry_count": 0, "api_failure": ""},
        config={"configurable": {"thread_id": "t-windsor"}}))
    assert reviews == [0, 1], "the second review must know it is a rerun"
    assert final["reviewer_verdict"] == "PASS" and not final.get("api_failure")
    assert final["review_rerun_count"] == 1 and final["retry_count"] == 1


def test_a_rebuilt_mesh_is_not_reviewed_as_a_rerun_of_the_last_one(monkeypatch):
    # The rerun count belongs to ONE mesh. Attempt 1's review stalls and is rerun, that rerun FAILs
    # asking for a change, and the builder makes attempt 2: its review is a first look at a new
    # mesh - not "reviewing the same mesh again", and not excluded from the same-mesh reuse.
    import asyncio

    import meshpipeline.agents.reviewer.review_policy as RP
    import meshpipeline.agents.reviewer.settings as rcfg
    from meshpipeline.agents.builder.turn_patch import TurnPatch
    from meshpipeline.pipeline import graph as G

    reviews: list[tuple[int, int]] = []

    async def _noop(state):
        return {}

    async def _builder(state):
        return TurnPatch(workspace="/w", request_txt="r", review_brief_txt="b",
                         deadline_epoch=0.0,
                         retry_count=int(state.get("retry_count", 0) or 0) + 1).state()

    async def _executor(state):
        return {"executor_success": True}

    async def _reviewer(state):
        reviews.append((int(state.get("retry_count", 0) or 0),
                        int(state.get("review_rerun_count", 0) or 0)))
        if len(reviews) == 1:
            return {"api_failure": "reviewer_stalled"}
        if len(reviews) == 2:
            return {"reviewer_verdict": "FAIL", "reviewer_axis_findings": []}
        return {"reviewer_verdict": "PASS"}

    for name in ("node_intake", "node_engine_select", "node_geometry_admission",
                 "node_classifier", "node_engine_fallback"):
        monkeypatch.setattr(G, name, _noop)
    monkeypatch.setattr(G, "node_builder", _builder)
    monkeypatch.setattr(G, "node_executor", _executor)
    monkeypatch.setattr(G, "node_reviewer", _reviewer)
    monkeypatch.setattr(RP, "asks_for_rebuild", lambda findings: True)
    monkeypatch.setattr(rcfg, "REVIEWER_RERUN_MAX", 2)
    monkeypatch.setattr(rcfg, "REVIEWER_RERUN_BACKOFF_S", 0)

    g = G.build_graph(checkpointer=MemorySaver())
    final = asyncio.run(g.ainvoke(
        {"job_id": "j-rebuilt", "retry_count": 0, "api_failure": ""},
        config={"configurable": {"thread_id": "t-rebuilt"}}))
    assert reviews == [(1, 0), (1, 1), (2, 0)], (
        "attempt 2 is a new mesh: its review must not be counted as a rerun of attempt 1's")
    assert final["reviewer_verdict"] == "PASS"
