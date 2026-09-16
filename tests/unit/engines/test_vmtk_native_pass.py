# Responsibility: VMTK's mesh run claims a native run of its own for every pass, and ships only its case.
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.contracts.mesh_execution import (
    NATIVE_PASS_FACT,
    NATIVE_PAYLOAD_FACT,
    read_native_pass,
)
from meshpipeline.engines.vmtk import vmtk_runner as R


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "attempt_1"
    ws.mkdir()
    (ws / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.3}))
    (ws / "lumen.vtp").write_text("<VTKFile/>")
    (ws / "mesh.vtu").write_text("a prior pass's mesh")          # must NOT ride along
    (ws / "centerlines.vtp").write_text("a prior pass's output")
    return ws


def test_each_revised_run_in_an_attempt_is_its_own_pass(tmp_path, monkeypatch):
    # jobs 73cce02e / 65081ced: the second run in an attempt, with a revised spec, was refused as
    # a conflicting replay of the first - because no pass was ever recorded. A revised payload
    # claims a pass of its own; an unchanged retry keeps the one it had (replay-deduplicated).
    ws = _workspace(tmp_path)
    seen = []
    monkeypatch.setattr("meshpipeline.contracts.mesh_execution.run_mesh",
                        lambda w, *, engine, timeout: seen.append(read_native_pass(w)) or {"rc": 0})
    assert read_native_pass(ws) is None
    R.run_cartesian_mesh(ws, timeout=10)
    (ws / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.25}))
    R.run_cartesian_mesh(ws, timeout=10)
    R.run_cartesian_mesh(ws, timeout=10)                    # unchanged: the same pass again
    (ws / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.2}))
    R.run_cartesian_mesh(ws, timeout=10)
    assert seen == [1, 2, 2, 3]
    assert (ws / NATIVE_PASS_FACT).read_text().strip() == "3"


def test_the_payload_is_the_case_not_the_previous_result(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    monkeypatch.setattr("meshpipeline.contracts.mesh_execution.run_mesh",
                        lambda w, *, engine, timeout: {"rc": 0})
    R.run_cartesian_mesh(ws, timeout=10)
    members = (ws / NATIVE_PAYLOAD_FACT).read_text()
    assert "vmtk_spec.json" in members and "lumen.vtp" in members
    assert "mesh.vtu" not in members and "centerlines.vtp" not in members


def test_a_missing_optional_input_is_simply_not_listed(tmp_path):
    ws = _workspace(tmp_path)
    assert R._native_payload_members(ws) == ["vmtk_spec.json", "lumen.vtp"]
    (ws / "lumen_open.vtp").write_text("x")
    assert R._native_payload_members(ws) == ["vmtk_spec.json", "lumen.vtp", "lumen_open.vtp"]


def test_a_workspace_that_does_not_exist_still_dispatches_through_the_contract(tmp_path, monkeypatch):
    # the dispatch-contract test hands every engine's run tool a path that does not exist
    seen = []
    monkeypatch.setattr("meshpipeline.contracts.mesh_execution.run_mesh",
                        lambda w, *, engine, timeout: seen.append((str(w), engine)) or {"rc": 1})
    ws = tmp_path / "nonexistent-ws"
    assert R.run_cartesian_mesh(ws, timeout=10) == {"rc": 1}
    assert seen == [(str(ws), "vmtk")]
    assert not ws.exists()
