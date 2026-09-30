# Responsibility: Prove the snappy driver declares its remote case - dicts plus staged STLs - and nothing a prior pass wrote back.
# Boundaries: the driver's member computation; the fact's effect on tar and digest is proven in tests/unit/platform.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.snappy.drivers import _native_payload_members


def _attempt_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "attempt_1"
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "snappyHexMeshDict").write_text("d")
    tri = ws / "constant" / "triSurface"
    tri.mkdir(parents=True)
    (tri / "body.stl").write_text("solid body")
    (ws / "input.stl").write_text("solid input")
    return ws


def test_declares_the_dicts_and_every_staged_trisurface_stl(tmp_path):
    ws = _attempt_workspace(tmp_path)
    (ws / "constant" / "triSurface" / "inlet.stl").write_text("solid inlet")   # internal flow
    assert _native_payload_members(ws) == [
        "system", "constant/triSurface/body.stl", "constant/triSurface/inlet.stl",
    ]


def test_excludes_prior_pass_outputs_and_local_only_state(tmp_path):
    ws = _attempt_workspace(tmp_path)
    # a completed pass's collected outputs, sharing the attempt's workspace
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "faces").write_text("f")
    (ws / "constant" / "triSurface" / "body.eMesh").write_text("e")   # remote regenerates it
    (ws / "VTK").mkdir()
    (ws / "mesh.msh").write_text("m")
    (ws / "snappyHexMesh.log").write_text("l")
    members = _native_payload_members(ws)
    assert members == ["system", "constant/triSurface/body.stl"]
    assert "input.stl" not in members                                 # planner-side only


def test_carries_the_flow_topology_fact_the_remote_reads(tmp_path):
    # native.py decides whether to measure the passage from `flow_topology`; left out of the
    # payload, the Cloud Run mesher never saw it - a FileNotFoundError after every mesh and no
    # internal-flow mesh ever measured, so the resolution floor judged nothing
    from meshpipeline.engines.snappy.drivers import REMOTE_WORKSPACE_FACTS
    ws = _attempt_workspace(tmp_path)
    (ws / "flow_topology").write_text("internal")
    members = _native_payload_members(ws)
    assert "flow_topology" in members and "flow_topology" in REMOTE_WORKSPACE_FACTS
    assert members == ["system", "constant/triSurface/body.stl", "flow_topology"]


def test_the_declared_payload_uploads_the_fact(tmp_path):
    from meshpipeline.contracts.mesh_execution import (
        note_native_payload,
        submission_payload_files,
    )
    ws = _attempt_workspace(tmp_path)
    (ws / "flow_topology").write_text("external")
    note_native_payload(ws, _native_payload_members(ws))
    shipped = {f.relative_to(ws).as_posix() for f in submission_payload_files(ws)}
    assert "flow_topology" in shipped


def test_a_workspace_without_the_fact_declares_nothing_it_lacks(tmp_path):
    # older staging, or a run that never wrote it: the member list names only what exists, so
    # the payload never refuses over a missing optional fact
    ws = _attempt_workspace(tmp_path)
    assert "flow_topology" not in _native_payload_members(ws)


def test_the_remote_reads_the_fact_without_raising_when_it_is_absent(tmp_path):
    from meshpipeline.engines.workspace_facts import read_flow_topology
    assert read_flow_topology(tmp_path) == ""
    (tmp_path / "flow_topology").write_text("Internal\n")
    assert read_flow_topology(tmp_path) == "internal"
