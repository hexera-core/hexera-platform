# Responsibility: Verify intake admission and the engine gate stay distinct layers, reading different evidence.
from __future__ import annotations

from meshpipeline.engines.registry import all_engine_names, get_spec

FIVE_ENGINES = {"cfmesh", "snappy", "gmsh", "snappy_multiregion", "vmtk"}


def test_all_five_engines_still_declare_a_patch_contract_gate():
    assert set(all_engine_names()) == FIVE_ENGINES, "the engine roster changed - update this guard"
    for name in FIVE_ENGINES:
        keys = {g.key for g in get_spec(name).gates}
        assert "patch_contract" in keys, (
            f"engine {name!r} no longer declares a patch_contract gate - the mesh-side proof that "
            "the delivered boundaries match the approved contract was removed (defence in depth)")


def test_the_engine_gate_reads_the_delivered_mesh_not_the_approval():
    from meshpipeline.engines import contract as gate_contract

    # the shared gate body exists and is applicability-aware (empty declaration = not applicable)
    assert hasattr(gate_contract, "check_contract")
    assert hasattr(gate_contract, "contract_applicable")
    assert gate_contract.contract_applicable([{"name": "wall", "type": "wall"}]) is True
    assert gate_contract.contract_applicable([]) is False


def test_admission_and_engine_gate_are_distinct_layers():
    import inspect

    # Admission is an application-layer check that runs before build_graph. The contract check
    # itself now lives in application/approved_patch_contract; the ORDERING guarantee - it runs
    # before the graph exists - is still owned by the run, so both halves are asserted.
    import meshpipeline.application.approved_patch_contract as apc
    import meshpipeline.application.pipeline_run as pr

    assert "assert_satisfied_by" in inspect.getsource(apc.check_admission), (
        "the application-layer admission gate for the approved patch contract is gone")
    src = inspect.getsource(pr._run_async)
    assert "check_admission" in src, "the run no longer performs admission at all"
    admission_pos = src.index("check_admission")
    build_pos = src.index("build_graph(")
    assert admission_pos < build_pos, (
        "admission must run BEFORE the graph is built - a mismatch may never reach the builder")

    # the engine gate is a separate, per-engine GateSpec (not the application check)
    from meshpipeline.engines.registry import get_spec
    gate = next(g for g in get_spec("cfmesh").gates if g.key == "patch_contract")
    assert gate.check is not None and "approved_patch_contract" not in inspect.getsource(gate.check), (
        "the engine gate must judge the delivered mesh, independently of the application contract")
