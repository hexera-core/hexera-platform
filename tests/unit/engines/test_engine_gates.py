# Responsibility: Verify a gate chain stops on a blocking rejection, and a crashing gate is a system failure.
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.engines.gates import GateCtx, GateSpec, run_gates
from meshpipeline.engines.registry import get_spec  # noqa: E402
from meshpipeline.errors import FailureClass, SystemFailure  # noqa: E402


def test_both_flow_engines_declare_the_same_gate_chain():
    for name in ("cfmesh", "snappy"):
        assert [g.key for g in get_spec(name).gates] == [
            "manifest_valid", "patch_contract", "boundary_types", "quality_floor"]


def test_every_engine_gates_its_own_declared_quality_bars():
    from meshpipeline.engines.quality_criteria import BUILD_TIME_KEYS, criteria_for

    quality_gates = {"quality_floor", "sicn_floor", "manifest_valid"}
    for name in ("cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk"):
        bars = {c.key for c in criteria_for(name) if c.gating} - BUILD_TIME_KEYS
        assert bars, f"{name} declares no post-build gating criterion at all"
        keys = {g.key for g in get_spec(name).gates}
        assert keys & quality_gates, (
            f"{name} declares gating bars {sorted(bars)} but has no gate that could enforce them: "
            f"{sorted(keys)}")


def test_crashing_gate_is_a_system_failure_not_a_rejection(tmp_path):
    def _boom(ctx):
        raise ValueError("negative axis")
    import pytest
    with pytest.raises(SystemFailure) as ei:
        run_gates((GateSpec(key="exploding", check=_boom),),
                  GateCtx(workspace=tmp_path))
    assert ei.value.dependency == "gate:exploding"
    assert ei.value.failure_class is FailureClass.INTERNAL


def test_blocking_rejection_stops_the_chain(tmp_path):
    calls = []
    gates = (
        GateSpec(key="a", check=lambda c: (calls.append("a"), (False, "no"))[1]),
        GateSpec(key="b", check=lambda c: (calls.append("b"), (True, ""))[1]),
    )
    ok, key, fb = run_gates(gates, GateCtx(workspace=tmp_path))
    assert (ok, key, fb) == (False, "a", "no") and calls == ["a"]


def test_manifest_valid_gate_speaks_the_original_vocabulary(tmp_path):
    ok, key, fb = run_gates(get_spec("cfmesh").gates, GateCtx(workspace=tmp_path))
    assert not ok and key == "manifest_valid"
    assert "[MANIFEST_VALIDATION_FAILED]" in fb
    assert "Builder did not write manifest" in fb


def test_patch_contract_gate_skips_without_a_contract(tmp_path):
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({"patches": {}}))
    ok, _, _ = run_gates(
        (get_spec("cfmesh").gates[1],),
        GateCtx(workspace=tmp_path, intake_patches=[]))
    assert ok


def test_patch_contract_gate_rejects_mismatch(tmp_path):
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({
        "patches": {"body": []}, "patch_types": {"body": "wall"}}))
    ok, key, fb = run_gates(
        (get_spec("cfmesh").gates[1],),
        GateCtx(workspace=tmp_path,
                intake_patches=[{"name": "wing", "type": "wall"},
                                {"name": "farfield", "type": "farfield"}]))
    assert not ok and key == "patch_contract"
    assert "[CONTRACT_MISMATCH]" in fb          # original coaching text intact
    assert "fix the patch definitions" in fb.lower()


def test_executor_iterates_declarations_no_inline_gates():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "pipeline" / "executor.py").read_text()
    assert "run_gates(" in src and "get_spec(" in src
    assert "_validate_manifest" not in src
    assert "check_contract" not in src


# cross-engine reconciliation audit (2026-07-12): foam TYPES are artifact-checked

def test_boundary_types_gate_catches_a_failed_empty_retype(tmp_path):
    from meshpipeline.engines.cfmesh.deliverable import reconcile_boundary_types
    b = tmp_path / "constant" / "polyMesh"; b.mkdir(parents=True)
    (b / "boundary").write_text("""
3
(
    airfoil
    {
        type            wall;
        nFaces          10;
    }
    farfield
    {
        type            patch;
        nFaces          20;
    }
    frontAndBack
    {
        type            patch;
        nFaces          30;
    }
)
""")
    patches = [{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"},
               {"name": "frontAndBack", "type": "empty"}]
    out = reconcile_boundary_types(tmp_path, patches)
    assert "BOUNDARY_TYPE_MISMATCH" in out and "frontAndBack" in out and "'empty'" in out
    # fix the retype -> consistent
    (b / "boundary").write_text((b / "boundary").read_text().replace(
        "frontAndBack\n    {\n        type            patch;",
        "frontAndBack\n    {\n        type            empty;"))
    assert reconcile_boundary_types(tmp_path, patches) == ""


def test_vmtk_gate_reconciles_actual_vtu_boundaries():
    import meshpipeline.engines.vmtk.gates as G
    from meshpipeline.engines.gates import GateCtx
    patches = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
               {"name": "wall", "type": "wall"}]
    def _ctx(quality):
        c = GateCtx(workspace=None, engine="vmtk", domain="", intake_patches=patches,
                    engine_params={})
        c.manifest_or_load = lambda: {  # type: ignore[method-assign]
            "patch_types": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
            "patches": {"inlet": [], "outlet": [], "wall": []},
            "quality": quality}
        return c
    # cap_0 = entity 0 = unclassified remainder, NOT an opening (known-good aorta shape)
    ok, fb = G._gate_vmtk_patch_contract(_ctx(
        {"actual_boundaries": ["cap_0", "cap_2", "cap_3", "cap_4", "wall"], "expected_caps": 3}))
    assert ok, fb
    ok, fb = G._gate_vmtk_patch_contract(_ctx(
        {"actual_boundaries": ["cap_0", "cap_2", "cap_3", "wall"], "expected_caps": 3}))
    assert not ok and "VTU_BOUNDARY_MISMATCH" in fb and "2 capped" in fb
    ok, fb = G._gate_vmtk_patch_contract(_ctx(
        {"actual_boundaries": ["cap_0", "cap_2", "cap_3"], "expected_caps": 3}))
    assert not ok and "no wall" in fb


def test_every_gate_declares_what_it_proves_in_the_users_language():
    from meshpipeline.engines import registry as ec
    for name in ec.engine_names():
        for g in ec.get_spec(name).gates:
            assert g.proves, f"{name}: gate {g.key!r} declares no user-facing statement"
            # no IDENTIFIER-style vocabulary: a snake_case key must never appear, and
            # no snake_case token at all. (A key that is an ordinary English word -
            # 'interfaces' - may of course appear in an English sentence.)
            if "_" in g.key:
                assert g.key not in g.proves, (
                    f"{name}: gate {g.key!r} leaks its internal key into the statement")
            assert "_" not in g.proves, (
                f"{name}: gate {g.key!r} statement contains snake_case vocabulary")
            assert g.proves[0].isupper(), f"{name}: {g.key!r} statement is not a sentence"


def test_the_optional_solvability_contract_stays_explicitly_declared():
    from pathlib import Path

    from meshpipeline.engines.registry import engine_names
    from meshpipeline.engines.runtime import get_engine

    declared_none, performs_check = [], []
    for name in sorted(engine_names()):
        engine = get_engine(name)
        check = getattr(engine, "check_solvability", None)
        assert callable(check), f"{name} declares no solvability seam at all"
        (declared_none if check(Path("."), {}) is None else performs_check).append(name)

    # multiregion opts out for a structural reason, alongside the two non-OpenFOAM engines
    assert "snappy_multiregion" in declared_none, (
        "multiregion must keep declaring the optional-solvability opt-out; without it the executor "
        "seam would look for a whole-case solve that cannot exist for a split multi-region mesh")
    assert {"gmsh", "vmtk"} <= set(declared_none)
    # ...and the single-region OpenFOAM engines still perform theirs
    assert set(performs_check) == {"cfmesh", "snappy"}, performs_check
