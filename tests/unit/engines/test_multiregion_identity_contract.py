# Responsibility: Verify a delivered multi-region case is refused unless its declared regions and meshes agree exactly.
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from tests._scan import scanned

import meshpipeline.engines.snappy_multiregion.multiregion_runner as MR
from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec
from meshpipeline.pipeline.graph import route_after_executor

ENGINE = "snappy_multiregion"
REGIONS = [{"name": "fluid", "type": "fluid", "solids": [0]},
           {"name": "solid", "type": "solid", "solids": [1]}]


@pytest.fixture(autouse=True)
def measured_regions(monkeypatch):
    monkeypatch.setattr(MR, "_single_region_check_mesh",
                        lambda ws, region="": {"cells": 500, "fatal": [], "skew_fraction": 1e-5,
                                               "max_non_ortho": 40.0})


def _boundary(*, interface: str, faces: int) -> str:
    return f"""FoamFile{{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }}
2
(
    outer
    {{
        type            wall;
        nFaces          200;
        startFace       0;
    }}
    {interface}
    {{
        type            mappedWall;
        nFaces          {faces};
        startFace       200;
    }}
)
"""


def _case(tmp_path, *, regions=("fluid", "solid"), region_properties=True,
          declared=None, iface_faces=(64, 64)):
    const = tmp_path / "constant"
    const.mkdir(parents=True, exist_ok=True)
    if region_properties:
        (const / "regionProperties").write_text(
            "FoamFile{ version 2.0; format ascii; class dictionary; object regionProperties; }\n"
            "regions ( fluid ( fluid ) solid ( solid ) );\n")
    names = list(regions)
    for i, name in enumerate(names):
        pm = const / name / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        other = names[1 - i] if len(names) == 2 else "other"
        for f in ("owner", "neighbour", "points", "faces"):
            (pm / f).write_text("0\n(\n)\n")
        (pm / "boundary").write_text(
            _boundary(interface=f"{name}_to_{other}", faces=iface_faces[i % len(iface_faces)]))
    (tmp_path / ".regions.json").write_text(json.dumps(declared if declared is not None else REGIONS))
    return tmp_path


def _finalize_and_gate(ws, *, engine_params=None, gates=None):
    result = MR.finalize(str(ws), intake_patches=[], engine=ENGINE, domain="cht",
                         internal_flow=True)
    executor_success = bool(result["success"])
    gate_rows: list = []
    failed, feedback = "", ""
    if executor_success:                       # exactly as pipeline/executor.py:87 sequences it
        ep = dict(engine_params or {})
        if (ws / ".regions.json").exists():
            ep.setdefault("_regions", json.loads((ws / ".regions.json").read_text()))
        ctx = GateCtx(workspace=ws, engine=ENGINE, domain="cht", intake_patches=[], engine_params=ep)
        ok, failed, feedback = run_gates(
            gates if gates is not None else get_spec(ENGINE).gates, ctx,
            on_result=lambda k, o, fb: gate_rows.append((k, bool(o), str(fb))))
        executor_success = bool(ok)
    else:
        failed, feedback = "finalize", result.get("output", "")
    route = route_after_executor({"executor_success": executor_success, "retry_count": 99,
                                  "solvability_failed": False})
    return {
        "finalize_success": bool(result["success"]),
        "finalize_output": result.get("output", ""),
        "executor_success": executor_success,
        "failed": failed, "feedback": feedback, "gate_rows": gate_rows,
        "delivery_marker": executor_success,
        "reviewer_invocations": 1 if route == "node_reviewer" else 0,
        "route": route,
    }


# the valid case

def test_a_genuine_two_region_case_is_delivered_and_reviewed_exactly_once(tmp_path):
    ws = _case(tmp_path)
    assert (ws / "constant" / "regionProperties").exists()
    assert sorted(p.name for p in (ws / "constant").iterdir()
                  if p.is_dir()) == ["fluid", "solid"]

    out = _finalize_and_gate(ws)
    assert out["finalize_success"], out["finalize_output"]
    assert out["executor_success"], f"gate {out['failed']} rejected a valid case: {out['feedback']}"
    assert {k for k, _, _ in out["gate_rows"]} == {g.key for g in get_spec(ENGINE).gates}
    assert out["delivery_marker"] is True
    assert out["reviewer_invocations"] == 1
    assert out["route"] == "node_reviewer"

    manifest = json.loads((ws / "mesh_manifest.json").read_text())
    assert manifest["mesh_units"] == "m"
    assert [r["name"] for r in manifest["quality"]["regions"]] == ["fluid", "solid"]


# the invalid cases

def _assert_refused(out, *, marker: str = ""):
    assert not out["executor_success"], "a defective multiregion case was delivered"
    assert out["delivery_marker"] is False
    assert out["reviewer_invocations"] == 0
    assert out["route"] == "__end__"
    diag = out["feedback"] or out["finalize_output"]
    assert diag.strip(), "the refusal carried no diagnostic at all"
    assert "[" in diag, f"the refusal is not an engine-owned coded diagnostic: {diag}"
    if marker:
        assert marker in diag, f"expected {marker!r} in the diagnostic, got: {diag}"


def test_missing_region_properties_is_refused(tmp_path):
    _assert_refused(_finalize_and_gate(_case(tmp_path, region_properties=False)),
                    marker="regionProperties")


@pytest.mark.parametrize("body", [
    "regions ( fluid ( fluid\n",                 # truncated - never closes
    "regions ( );\n",                            # declares no region at all
    "",                                          # empty file
    "// nothing but a comment\n",                # no regions entry
])
def test_malformed_region_properties_is_refused(tmp_path, body):
    ws = _case(tmp_path)
    (ws / "constant" / "regionProperties").write_text(body)
    _assert_refused(_finalize_and_gate(ws), marker="regionProperties")


def test_region_properties_must_match_the_delivered_region_meshes(tmp_path):
    ws = _case(tmp_path)
    (ws / "constant" / "regionProperties").write_text("regions ( fluid ( fluid ) );\n")
    _assert_refused(_finalize_and_gate(ws), marker="regionProperties")


def test_a_declared_region_with_no_mesh_is_refused(tmp_path):
    # declared fluid+solid, but only fluid was actually split out
    out = _finalize_and_gate(_case(tmp_path, regions=("fluid",)))
    _assert_refused(out)
    assert "missing=['solid']" in out["finalize_output"] or "REGION_SPLIT_FAILED" in out["feedback"]


def test_an_undeclared_region_mesh_is_refused(tmp_path):
    # a third region appeared that the plan never declared (the live domain0 defect)
    out = _finalize_and_gate(_case(tmp_path, regions=("fluid", "solid", "domain0")))
    _assert_refused(out)


def test_only_one_valid_region_remaining_is_refused(tmp_path):
    # both declared, but the solid's polyMesh was lost - a single-region case is not multiregion
    ws = _case(tmp_path)
    for f in ("owner", "neighbour", "points", "faces", "boundary"):
        (ws / "constant" / "solid" / "polyMesh" / f).unlink()
    _assert_refused(_finalize_and_gate(ws))


def test_all_region_evidence_removed_is_refused(tmp_path):
    ws = _case(tmp_path)
    for name in ("fluid", "solid"):
        for f in scanned((ws / "constant" / name / "polyMesh").iterdir(), "the delivered region polyMesh files"):
            f.unlink()
    (ws / "constant" / "regionProperties").unlink()
    _assert_refused(_finalize_and_gate(ws), marker="regionProperties")


def test_non_conformal_interfaces_are_refused(tmp_path):
    out = _finalize_and_gate(_case(tmp_path, iface_faces=(64, 31)))   # counts disagree
    _assert_refused(out)
    assert "interfaces_ok=False" in out["finalize_output"] or \
           "INTERFACE_NON_CONFORMAL" in out["feedback"]


# the mutation contract

def test_the_contract_fails_when_both_redundant_identity_checks_are_bypassed(tmp_path, monkeypatch):
    import meshpipeline.engines.snappy_multiregion.gates as G

    def _blind(fn):
        def _wrapped(arg, *a, **k):
            ws = Path(arg.workspace if hasattr(arg, "workspace") else arg)
            rp = ws / "constant" / "regionProperties"
            created = not rp.exists()
            if created:
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text("regions ( fluid ( fluid ) solid ( solid ) );\n")
            try:
                return fn(arg, *a, **k)
            finally:
                if created:
                    rp.unlink()
        return _wrapped

    monkeypatch.setattr(MR, "finalize", _blind(MR.finalize))
    # added a THIRD independent guard: `regions_split` now reads the DELIVERED
    # regionProperties back and reconciles it against the region meshes on disk. The combined
    # bypass has to neutralise that one too, or this control stops demonstrating anything - it
    # would "pass" only because a guard it never touched happened to hold.
    _blinded = {"manifest_valid": G._gate_cht_manifest_valid,
                "regions_split": G._gate_regions_split}
    mutated = tuple(
        g if g.key not in _blinded
        else dataclasses.replace(g, check=_blind(_blinded[g.key]))
        for g in get_spec(ENGINE).gates)
    ws = _case(tmp_path, region_properties=False)
    out = _finalize_and_gate(ws, gates=mutated)

    assert out["executor_success"] and out["reviewer_invocations"] == 1, (
        "the combined bypass did not actually remove the guarantee, so this control proves nothing")
    with pytest.raises(AssertionError):
        _assert_refused(out, marker="regionProperties")
