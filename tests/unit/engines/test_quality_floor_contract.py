# Responsibility: Verify the quality gate judges from measurements, and an unmeasured bar is a failure, not a pass.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.quality_criteria import BUILD_TIME_KEYS, criteria_for
from meshpipeline.engines.registry import get_spec
from meshpipeline.pipeline.graph import route_after_executor

# The three engines whose declared bars were unenforced before this contract existed.
REPAIRED = ("cfmesh", "snappy", "snappy_multiregion")


def _stage(tmp_path, engine: str) -> None:
    def _write(pm):
        pm.mkdir(parents=True, exist_ok=True)
        for f in ("owner", "neighbour", "points", "faces", "boundary"):
            (pm / f).write_text("0\n(\n)\n")

    if engine == "snappy_multiregion":
        (tmp_path / "constant").mkdir(parents=True, exist_ok=True)
        regions = ("fluid", "solid")
        (tmp_path / "constant" / "regionProperties").write_text(
            "regions ( " + " ".join(f"{r} ( {r} )" for r in regions) + " );\n")
        for region in regions:
            _write(tmp_path / "constant" / region / "polyMesh")
    else:
        _write(tmp_path / "constant" / "polyMesh")


def _healthy(engine: str) -> dict:
    q: dict = {"fatal": [], "cells": 1000}
    for c in criteria_for(engine):
        if not c.gating or c.key in BUILD_TIME_KEYS:
            continue
        if c.op == "empty":
            q[c.key] = []
        elif c.op == "==":
            q[c.key] = c.threshold
        elif c.op == "<=":
            q[c.key] = c.threshold / 2 if isinstance(c.threshold, float) else c.threshold
        elif c.op == ">":
            q[c.key] = c.threshold + 1 if isinstance(c.threshold, int) else c.threshold * 10 + 1
    q.pop("wall_faces", None)          # manifest-derived, not a quality key
    if engine == "snappy_multiregion":
        # what regions_split and interfaces require of a delivered case
        q |= {"regions": [{"name": "fluid", "type": "fluid", "cells": 600},
                          {"name": "solid", "type": "solid", "cells": 400}],
              "regions_missing": [], "regions_undeclared": [], "interface_ok": True}
    return {
        "schema_version": "2.1",
        "geometry": {},
        "patches": {"body": []},
        "cell_count": 1000,
        "mesh_units": "m",
        "mesh_written": True,
        "patch_types": {"body": "wall"},
        "patch_face_counts": {"body": 500},
        "validation": {"has_wall": True, "has_inflow": True, "has_outflow": True,
                       "patch_validation": {"body": True}},
        "quality": q,
    }


def _numeric_bars(engine: str):
    return [c for c in criteria_for(engine)
            if c.gating and c.key not in BUILD_TIME_KEYS and c.op in ("<=", ">", "==", "empty")]


def _run_chain(tmp_path, engine: str, manifest: dict):
    _stage(tmp_path, engine)
    (tmp_path / "mesh_manifest.json").write_text(json.dumps(manifest))
    ctx = GateCtx(workspace=tmp_path, engine=engine, domain="d",
                  intake_patches=[], engine_params={})
    ctx.manifest = manifest          # the gate under test reads the manifest, not the workspace
    results: list = []
    ok, failed, feedback = run_gates(
        get_spec(engine).gates, ctx,
        on_result=lambda k, o, fb: results.append((k, bool(o), str(fb))))
    return ok, failed, feedback, results


# positive control

@pytest.mark.parametrize("engine", REPAIRED)
def test_a_mesh_clearing_every_declared_bar_passes_the_quality_gate(tmp_path, engine):
    _, _, _, results = _run_chain(tmp_path, engine, _healthy(engine))
    quality = [r for r in results if r[0] == "quality_floor"]
    assert quality, f"{engine} never ran a quality_floor gate: {[r[0] for r in results]}"
    assert quality[0][1] is True, f"a healthy {engine} mesh was rejected: {quality[0][2]}"


# the repair

def _fail_bar(m: dict, c) -> dict:
    if c.op == "empty":
        m["quality"][c.key] = [f"{c.key}-defect"]
    elif c.op == "<=":
        m["quality"][c.key] = c.threshold * 1000 + 1
    elif c.op == ">":
        m["quality"][c.key] = 0 if isinstance(c.threshold, int) else 0.0
    elif c.op == "==":
        m["quality"][c.key] = (not c.threshold) if isinstance(c.threshold, bool) else "wrong"
    if c.key == "wall_faces":                 # manifest-derived, not a quality key
        m["quality"].pop("wall_faces", None)
        m["patch_face_counts"] = {"body": 0}
    return m


#: The bars that had NO gate consumer before this contract. Each is now owned by `quality_floor`.
#: The others (multiregion's regions_missing / interface_ok, and its `fatal`) were already enforced
#: by a dedicated earlier gate, so they are asserted only to reject - not to reject *here*.
NEWLY_OWNED = {
    "cfmesh": {"fatal"},
    "snappy": {"fatal", "skew_fraction", "wall_faces"},
    "snappy_multiregion": {"skew_fraction"},
}


@pytest.mark.parametrize("engine", REPAIRED)
def test_failing_any_declared_bar_rejects_the_mesh_and_blocks_the_reviewer(tmp_path, engine):
    bars = _numeric_bars(engine)
    assert bars, f"{engine} declares no failable post-build bar"
    for c in bars:
        ok, failed, feedback, _ = _run_chain(tmp_path, engine, _fail_bar(_healthy(engine), c))
        assert not ok, f"{engine}: failing `{c.key}` did not reject the mesh"
        assert failed in {g.key for g in get_spec(engine).gates}, (
            f"{engine}: `{c.key}` was rejected by `{failed}`, which is not a declared gate")
        assert c.key in feedback or c.label in feedback or c.key.split("_")[0] in feedback.lower(), (
            f"{engine}: the rejection for `{c.key}` does not name the bar: {feedback}")
        # and the reviewer is unreachable on that result
        assert route_after_executor(
            {"executor_success": False, "retry_count": 99, "solvability_failed": False}
        ) == "__end__"


@pytest.mark.parametrize("engine", REPAIRED)
def test_the_bars_this_repair_took_ownership_of_are_still_gating(engine):
    bars = {c.key for c in _numeric_bars(engine)}
    missing = NEWLY_OWNED[engine] - bars
    assert not missing, (
        f"{engine}: {sorted(missing)} no longer declares itself a post-build gating criterion, so "
        f"the quality gate no longer enforces it. Declared gating bars: {sorted(bars)}")


@pytest.mark.parametrize("engine", REPAIRED)
def test_the_previously_unenforced_bars_are_now_owned_by_the_quality_gate(tmp_path, engine):
    for c in _numeric_bars(engine):
        if c.key not in NEWLY_OWNED[engine]:
            continue
        ok, failed, feedback, _ = _run_chain(tmp_path, engine, _fail_bar(_healthy(engine), c))
        assert not ok and failed == "quality_floor", (
            f"{engine}: `{c.key}` should now be owned by quality_floor, got `{failed}`")
        assert "[QUALITY]" in feedback, f"{engine}: `{c.key}` rejection is not engine-owned: {feedback}"


@pytest.mark.parametrize("engine", REPAIRED)
def test_an_unmeasured_bar_is_a_failure_not_a_pass(tmp_path, engine):
    for c in _numeric_bars(engine):
        if c.key not in NEWLY_OWNED[engine]:
            continue                      # bars owned by an earlier gate have their own contract
        m = _healthy(engine)
        m["quality"].pop(c.key, None)
        if c.key == "wall_faces":
            m["patch_face_counts"] = {}
        ok, failed, feedback, _ = _run_chain(tmp_path, engine, m)
        assert not ok, f"{engine}: an unmeasured `{c.key}` passed the gate"
        assert failed == "quality_floor", f"{engine}: `{c.key}` absent was caught by `{failed}`"
        assert "quality-checked" in feedback, feedback


@pytest.mark.parametrize("engine", REPAIRED)
def test_the_gate_refuses_a_manifest_it_cannot_judge(tmp_path, engine):
    from meshpipeline.engines.quality_criteria import gate_declared_criteria
    ok, why = gate_declared_criteria(engine, {})
    assert not ok and "quality-checked" in why


# no silent weakening

def test_gmsh_and_vmtk_keep_their_own_floors():
    assert "sicn_floor" in {g.key for g in get_spec("gmsh").gates}
    assert "quality_floor" in {g.key for g in get_spec("vmtk").gates}
    assert any(c.key == "min_sicn" and c.gating for c in criteria_for("gmsh"))
    assert any(c.key == "min_quality" and c.gating for c in criteria_for("vmtk"))


def test_the_gate_derives_its_verdict_from_measurements_not_a_claimed_grade(tmp_path):
    m = _healthy("snappy")
    m["quality"]["skew_fraction"] = 0.9                      # a real, failing measurement
    m["quality_criteria"] = {"engine": "snappy", "production_grade": True, "criteria": []}
    ok, failed, feedback, _ = _run_chain(tmp_path, "snappy", m)
    assert not ok and failed == "quality_floor", (
        "the gate trusted a claimed production_grade over the measurements")
    assert "skew" in feedback.lower()
