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
    assert [a["text"] for a in announced] == [f"Mesh engine: {_expected}",
                                              f"Mesh engine: {default_engine()}"], announced


def test_gmsh_deterministic_path_on_real_geometry():
    import json
    from pathlib import Path

    step = Path("/tmp/elbow90_fluid.step")
    if not step.exists():
        import pytest
        pytest.skip("real STEP not staged (make test-integration stages it)")
    # THE TYPED COORDINATE STATE is not optional: the gmsh bundle refuses to stage a B-rep without
    # it, because the physical size of the imported model would otherwise be a guess. This case
    # passed `prepared=None` from before that contract existed, and the licensed-CAD skip meant it
    # never ran again to say so.
    from meshpipeline.contracts.coordinate_state import from_source_file
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )
    from meshpipeline.engines.gates import GateCtx, run_gates
    from meshpipeline.engines.gmsh import gmsh_runner as runner
    from meshpipeline.engines.registry import get_spec

    prepared = from_source_file(GeometryInterpretation(
        interpretation_id="i-it", owner_id="o-it", geometry_source_id="s-it",
        unit=LengthUnit.millimetre, scale_to_metres=scale_to_metres(LengthUnit.millimetre),
        basis=ResolutionBasis.user_confirmed))

    ws = Path("/tmp/it_gmsh_ws")
    ws.mkdir(exist_ok=True)
    runner.tessellate_to_stl(step, ws / "input.stl", prepared=prepared)
    info = runner.inspect_stl(ws)
    assert info["volumes"] >= 1
    largest = max(info["surfaces"], key=lambda s: s["area"])["tag"]
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": 2, "size": {"mode": "factor", "value": 0.08},
        "groups": [{"name": "fixed_base", "role": "fixed",
                    "surface_tags": [largest]}]}))
    # _run_gmsh_local is the mesh EXECUTION (runs in the Cloud Run container);
    # run_cartesian_mesh is now the cloud-dispatch seam. Integration validates the real
    # subprocess, so it calls the execution fn - the same one do_mesh runs on Cloud Run.
    res = runner._run_gmsh_local(ws, timeout=300)
    assert res["rc"] == 0, res["log_tail"]
    out = runner.finalize(str(ws), [{"name": "fixed_base", "type": "fixed"}],
                          "gmsh", domain="integration elbow FEA",
                          engine_params={"element_order": "2"})
    assert out["success"], out["output"]
    ok, key, fb = run_gates(get_spec("gmsh").gates, GateCtx(
        workspace=ws, engine="gmsh",
        intake_patches=[{"name": "fixed_base", "type": "fixed"}]))
    assert ok, f"{key}: {fb}"
