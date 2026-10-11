# Responsibility: Prove the snap-grid mesher is wired in as a declared engine: admitted for ECXML only, placed (never fused) at materialization, staged and run by its deterministic driver, finalized into a manifest its own gates accept - and refused by them when a region or an interface is wrong.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tests.unit.cad.ecxml_models import tiny_board

from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec
from meshpipeline.engines.runtime import get_engine
from meshpipeline.engines.snapgrid import runner as R
from meshpipeline.engines.snapgrid.grid import GridPlan
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml


def _ecxml(tmp_path: Path, name: str = "source.ecxml") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes(tiny_board().xml())
    return p


# ------------------------------------------------------------------------ the declaration --
def test_it_is_a_declared_multiregion_engine_that_reads_ecxml_only():
    spec = get_spec("snapgrid")
    assert spec.implemented and spec.build_driver is not None
    assert spec.reads_source_formats == ("ecxml",)
    assert {(c.input_kind, c.output_kind) for c in spec.capabilities} == {
        ("solid-assembly", "multiregion-volume")}
    assert [g.key for g in spec.gates] == ["manifest_valid", "quality_floor", "file_fidelity",
                                           "patch_contract", "regions_split", "interfaces"]
    # no other engine starts reading one format only
    from meshpipeline.engines.registry import engine_names
    assert [n for n in engine_names() if get_spec(n).reads_source_formats] == ["snapgrid"]


def test_admission_refuses_a_step_upload_and_admits_the_ecxml():
    from meshpipeline.agents.intake.validation import preview_admission

    def verdict(facts):
        return preview_admission("snapgrid", "conjugate_heat_transfer", "solid-assembly", "3D",
                                 geometry_facts=facts)

    step = verdict({"source_format": "step"})
    assert step["verdict"] == "impossible"
    assert step["blocking_rule_code"] == "source_format_unsupported"
    assert verdict({"source_format": "ecxml"})["verdict"] == "supported"
    # the other multi-region engine reads every format, as before
    other = preview_admission("snappy_multiregion", "conjugate_heat_transfer", "solid-assembly",
                              "3D", engine_params={"fluid_topology": "internal"},
                              geometry_facts={"source_format": "step"})
    assert other.get("blocking_rule_code") != "source_format_unsupported"


def test_the_uploads_facts_name_its_format(tmp_path):
    from meshpipeline.cad.regions import regions_of

    facts = regions_of(_ecxml(tmp_path, "board.ecxml")).as_facts()
    assert facts["source_format"] == "ecxml" and facts["region_count"] > 1


def test_an_ecxml_run_on_this_engine_is_placed_never_fused():
    from meshpipeline.application.geometry_materializer import ecxml_form_for

    assert ecxml_form_for("snapgrid") == "placed"
    assert ecxml_form_for("snappy_multiregion") == "fused"
    assert ecxml_form_for("") == "fused"
    assert ecxml_form_for("no-such-engine") == "fused"


def test_the_plan_follows_the_fidelity_and_never_exceeds_the_hard_limit(monkeypatch):
    import meshpipeline.settings.policy as polcfg

    assert R.plan_for("draft")["min_cells_across"] == 1
    assert R.plan_for("")["fidelity"] == "standard"
    assert R.plan_for("standard", step=1)["fidelity"] == "max"
    assert R.plan_for("max", step=1) is None
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 1_000_000)
    assert R.plan_for("max")["max_cells"] == 1_000_000
    assert R.cli_args({"max_cells": 5, "growth": 1.2, "other": 1}) == [
        "--max-cells", "5", "--growth", "1.2"]


def test_the_driver_finds_the_upload_beside_its_canonical_form(tmp_path):
    src = _ecxml(tmp_path)
    (tmp_path / "source.stl").write_text("solid x\nendsolid x\n")
    assert R.find_source(str(tmp_path / "source.stl")) == src
    assert R.find_source(str(src)) == src
    step = tmp_path / "other" / "source.step"
    step.parent.mkdir()
    step.write_text("ISO-10303-21;\n")
    assert R.find_source(str(step)) is None
    assert R.find_source("") is None


# ------------------------------------------------------------------------------ the driver --
class _Run:
    def __init__(self):
        self.fences: list[str] = []
        self.native: list[bool] = []

    async def fence(self, phase):
        self.fences.append(phase)

    def note_authoring(self):
        pass

    def note_native_run(self, *, produced_usable_mesh):
        self.native.append(produced_usable_mesh)

    def outcome(self, *, produced_deliverable, exhausted=False, failure_marker=""):
        from meshpipeline.agents.builder.driver_run import BuildDriverOutcome
        return BuildDriverOutcome(produced_deliverable, "submit_mesh:success"
                                  if produced_deliverable else "", failure_marker=failure_marker)


def test_the_driver_stages_the_model_and_the_plan_then_runs_the_mesher(tmp_path, monkeypatch):
    from meshpipeline.contracts.mesh_execution import read_native_payload
    from meshpipeline.engines.snapgrid import driver

    geo = tmp_path / "geometry"
    _ecxml(geo)
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    seen = {}

    def fake_run(workspace, *, timeout, context=None):
        seen["ws"], seen["timeout"] = Path(workspace), timeout
        return {"rc": 0, "cells": 1234, "log_tail": ""}

    monkeypatch.setattr(get_engine("snapgrid"), "run_cartesian_mesh", fake_run)
    run = _Run()
    ok, text, outcome = asyncio.run(driver.drive(
        ws, {"effective_mesh_fidelity": "draft"}, job_id="j", publish=None, run=run,
        source_path=str(geo / "source.stl")))
    assert ok and text == "submit_mesh:success" and outcome.produced_deliverable
    assert seen["ws"] == ws and seen["timeout"] > 0
    assert (ws / R.SOURCE_NAME).read_bytes() == (geo / "source.ecxml").read_bytes()
    assert json.loads((ws / R.PLAN_NAME).read_text())["fidelity"] == "draft"
    assert set(read_native_payload(ws)) == {R.SOURCE_NAME, R.PLAN_NAME}
    assert run.fences[-1] == "deliver builder outcome" and run.native == [True]


def test_the_driver_refuses_a_run_with_no_ecxml_and_a_retry_with_no_finer_level(tmp_path):
    from meshpipeline.agents.builder.driver_run import STOP_REVIEWED_CASE_REPEATS
    from meshpipeline.engines.snapgrid import driver

    ws = tmp_path / "ws"
    ws.mkdir()
    ok, text, _ = asyncio.run(driver.drive(ws, {}, job_id="j", publish=None, run=_Run(),
                                           source_path=str(tmp_path / "x.step")))
    assert not ok and text == driver.NO_SOURCE
    _ecxml(tmp_path / "g")
    review_retry = {"effective_mesh_fidelity": "max",
                    "classifier_result": {"error_source": "reviewer_fail"}}
    ok, text, _ = asyncio.run(driver.drive(ws, review_retry, job_id="j", publish=None,
                                           run=_Run(),
                                           source_path=str(tmp_path / "g" / "source.ecxml")))
    assert not ok and text == STOP_REVIEWED_CASE_REPEATS


# ----------------------------------------------------------------- finalize and the gates --
def _checked_case(tmp_path) -> Path:
    """A real snap-grid case of the tiny board, with the native check's record filled in as a
    clean checkMesh would (the check itself needs OpenFOAM: the lab runs prove it)."""
    ws = tmp_path / "ws"
    src = _ecxml(tmp_path)
    mesh_ecxml(src, ws, GridPlan(max_cells=60_000), binary=False)
    get_engine("snapgrid").tessellate_to_stl(str(src), ws / "input.stl")
    report = json.loads((ws / R.REPORT_NAME).read_text())
    report["native"] = {
        "ok": True, "cells": report["polymesh"]["cells"], "problems": [], "regions": {},
        "whole": {"fatal": [], "faces": report["polymesh"]["faces"], "skew_faces": 0},
        "interfaces": [{"a": r["a"], "b": r["b"], "kind": r["kind"], "grid_faces": r["faces"],
                        "faces_a": r["faces"], "faces_b": r["faces"], "ok": True}
                       for r in report["interfaces"]],
        "max_non_ortho": 0.0, "max_skewness": 0.3, "skew_fraction": 0.0, "skew_faces": 0}
    (ws / R.REPORT_NAME).write_text(json.dumps(report))
    return ws


def _gates(ws: Path):
    ctx = GateCtx(workspace=ws, engine="snapgrid", domain="", intake_patches=[],
                  engine_params={})
    return run_gates(get_spec("snapgrid").gates, ctx)


def test_a_checked_case_finalizes_into_a_manifest_every_gate_accepts(tmp_path):
    ws = _checked_case(tmp_path)
    fin = get_engine("snapgrid").finalize(str(ws), [], "snapgrid", "", False, {}, "")
    assert fin["success"], fin
    manifest = json.loads((ws / "mesh_manifest.json").read_text())
    assert manifest["mesh_mode"] == "snapgrid"
    assert manifest["quality_criteria"]["production_grade"] is True
    q = manifest["quality"]
    assert q["file_checked"] and q["interface_ok"] and not q["regions_missing"]
    assert {r["name"] for r in q["regions"]} == set(R.meshed_regions(ws))
    assert set(manifest["patch_types"]) >= {"domain_xmin", "domain_zmax"}
    assert _gates(ws) == (True, "", "")
    # the viewer draws the delivered mesh: every part's meshed surface under its region name
    view = get_spec("snapgrid").viewer_surface(ws, roles=manifest["patch_types"], units="m")
    drawn = {p["name"] for p in view["patches"]}
    assert set(json.loads((ws / R.REPORT_NAME).read_text())["solid_regions"]) <= drawn
    assert all(p["face_count"] > 0 for p in view["patches"])
    for m in get_spec("snapgrid").deliverable.members:
        assert (ws / m.path).exists() or m.path == R.SOURCE_NAME


def test_the_gates_refuse_a_missing_region_and_a_broken_interface(tmp_path):
    import shutil

    ws = _checked_case(tmp_path)
    report = json.loads((ws / R.REPORT_NAME).read_text())
    report["native"]["interfaces"][0]["ok"] = False
    (ws / R.REPORT_NAME).write_text(json.dumps(report))
    get_engine("snapgrid").finalize(str(ws), [], "snapgrid", "", False, {}, "")
    ok, key, _ = _gates(ws)
    assert not ok and key == "quality_floor"       # interface_ok is a gating criterion too
    shutil.rmtree(ws / "constant" / report["solid_regions"][0])
    fin = get_engine("snapgrid").finalize(str(ws), [], "snapgrid", "", False, {}, "")
    assert not fin["success"]
    ok, key, _ = _gates(ws)
    assert not ok and key == "manifest_valid"


def test_finalize_refuses_a_case_nobody_checked(tmp_path):
    ws = tmp_path / "ws"
    mesh_ecxml(_ecxml(tmp_path), ws, GridPlan(max_cells=60_000), binary=False)
    fin = get_engine("snapgrid").finalize(str(ws), [], "snapgrid", "", False, {}, "")
    assert not fin["success"] and "checkMesh never ran" in fin["output"]


@pytest.mark.parametrize("missing", [R.SOURCE_NAME])
def test_the_native_runner_refuses_a_workspace_without_the_model(tmp_path, missing):
    res = R.run_native_build(tmp_path, bashrc="/nonexistent", timeout=5)
    assert res["rc"] == -2 and res["code"] == "snapgrid_source_missing"
