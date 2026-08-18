# Responsibility: Verify a real native case passes every declared gate, and a failing bar is refused before delivery.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.native import _terminal_support as sup

from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec

ENGINES = ("cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk")

#: engine -> the gate that owns its numerical quality bars, and the bar this row violates.
QUALITY_GATE = {"cfmesh": "quality_floor", "snappy": "quality_floor",
                "snappy_multiregion": "quality_floor", "gmsh": "sicn_floor",
                "vmtk": "quality_floor"}

#: engine -> a quality edit that violates one of ITS declared gating bars.
QUALITY_BREAK = {
    "cfmesh": {"fatal": ["negative-volume cells"]},
    "snappy": {"skew_fraction": 0.5},
    "snappy_multiregion": {"skew_fraction": 0.5},
    "gmsh": {"min_sicn": 0.001},
    "vmtk": {"min_quality": 0.0},
}

#: engine -> a required deliverable whose absence must refuse the case.
DELIVERABLE = {
    "cfmesh": "constant/polyMesh/points",
    "snappy": "constant/polyMesh/points",
    "snappy_multiregion": "constant/regionProperties",
    "gmsh": "mesh.inp",
    "vmtk": "mesh.vtu",
}


def _gates(native: dict) -> tuple[bool, str, list]:
    engine = native["engine"]
    ctx = GateCtx(workspace=Path(native["workspace"]), engine=engine, domain=native["domain"],
                  intake_patches=native["contract"], engine_params={})
    results: list = []
    ok, failed, _ = run_gates(get_spec(engine).gates, ctx,
                              on_result=lambda k, o, fb: results.append((k, o, fb)))
    return bool(ok), failed or "", results


def _assert_refused(engine: str, ok: bool, failed: str, expected_gate: str, ws: Path) -> None:
    assert not ok, f"{engine}: the corrupted case passed its gate chain"
    assert failed == expected_gate, (
        f"{engine}: failed at {failed!r}, expected the owning gate {expected_gate!r} - another "
        "failure masked the intended one")
    assert not (ws / "delivery.marker").exists(), f"{engine}: a delivery marker was written"
    assert not (ws / "completed").exists(), f"{engine}: a completed-success state was written"


# column 1: the valid case
@pytest.mark.parametrize("engine", ENGINES)
def test_valid_case_passes_every_declared_gate(engine, tmp_path, canonical_provenance):
    native = sup.author_and_mesh(engine, tmp_path / engine)
    assert native["native_rc"] == 0, f"{engine}: native meshing failed: {native}"
    assert native["gates_ok"], f"{engine}: gate {native['failed_gate']} failed"

    ws = Path(native["workspace"])
    assert (ws / native["marker"]).exists(), f"{engine}: the declared deliverable is absent"
    manifest = json.loads((ws / "mesh_manifest.json").read_text())
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    assert manifest["mesh_units"] == COMPLETED_MESH_UNIT.value


# column 2: a gating numerical quality bar is violated
@pytest.mark.parametrize("engine", ENGINES)
def test_a_failing_quality_bar_is_refused_at_the_engines_quality_gate(
        engine, tmp_path, canonical_provenance):
    native = sup.author_and_mesh(engine, tmp_path / engine)
    assert native["gates_ok"], f"precondition: {engine} must pass cleanly first"

    ws = Path(native["workspace"])
    manifest = json.loads((ws / "mesh_manifest.json").read_text())
    quality = dict(manifest.get("quality") or {})
    quality.update(QUALITY_BREAK[engine])
    manifest["quality"] = quality
    (ws / "mesh_manifest.json").write_text(json.dumps(manifest))

    ok, failed, _ = _gates(native)
    _assert_refused(engine, ok, failed, QUALITY_GATE[engine], ws)


# column 3: a required deliverable is missing
@pytest.mark.parametrize("engine", ENGINES)
def test_a_missing_deliverable_is_refused_before_delivery(engine, tmp_path, canonical_provenance):
    native = sup.author_and_mesh(engine, tmp_path / engine)
    assert native["gates_ok"], f"precondition: {engine} must pass cleanly first"

    ws = Path(native["workspace"])
    target = ws / DELIVERABLE[engine]
    assert target.exists(), f"{engine}: {DELIVERABLE[engine]} was never produced"
    target.unlink()

    ok, failed, results = _gates(native)
    assert not ok, f"{engine}: a case missing {DELIVERABLE[engine]} passed its gate chain"
    assert failed, f"{engine}: refused without naming a gate: {results}"
    assert not (ws / "delivery.marker").exists()


def test_an_owner_only_snappy_polymesh_cannot_ship(tmp_path, canonical_provenance):
    from meshpipeline.engines.snappy.finalize import finalize
    from meshpipeline.engines.snappy.parallel_stages import REQUIRED_POLYMESH

    native = sup.author_and_mesh("snappy", tmp_path / "snappy")
    assert native["gates_ok"], "precondition: a clean snappy pass before we truncate it"

    ws = Path(native["workspace"])
    poly = ws / "constant" / "polyMesh"
    for component in REQUIRED_POLYMESH:
        if component != "owner":
            (poly / component).unlink()
    assert (poly / "owner").is_file(), "the interrupted-run shape is owner and nothing else"

    result = finalize(str(ws), intake_patches=native["contract"], engine="snappy",
                      domain=native["domain"], flow_topology="external")
    assert result["success"] is False, "an owner-only polyMesh was finalized as a success"
    assert result["output"].startswith("[SNAPPY]"), result["output"]
    for missing in ("points", "faces", "neighbour", "boundary"):
        assert missing in result["output"], f"the diagnostic does not name the missing {missing}"

    ok, failed, _ = _gates(native)
    assert not ok and failed, "an owner-only polyMesh passed the snappy gate chain"
    assert not (ws / "delivery.marker").exists()
