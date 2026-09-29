# Responsibility: Prove the snappy driver declares its remote case - dicts, staged STLs and its own contact line - and nothing a prior pass wrote back.
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


def test_ships_the_contact_line_the_driver_authored(tmp_path):
    # a body the raised floor cuts: the line where it meets the floor is a feature file the
    # renderer writes, and the remote cannot regenerate it from the STL - it must travel
    from meshpipeline.engines.snappy.snappy_runner import CONTACT_LINE_FILE
    ws = _attempt_workspace(tmp_path)
    (ws / "constant" / "triSurface" / "body.eMesh").write_text("e")   # remote regenerates it
    (ws / "constant" / "triSurface" / CONTACT_LINE_FILE).write_text("line")
    assert _native_payload_members(ws) == [
        "system", "constant/triSurface/body.stl", f"constant/triSurface/{CONTACT_LINE_FILE}",
    ]
