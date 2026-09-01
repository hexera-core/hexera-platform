# Responsibility: Pin the submission payload scope - a declared payload bounds BOTH the uploaded archive and the claim's digest, to the same set of files.
# Boundaries: the fact contract, the tar assembly and the digest; the snappy driver's own declaration is proven in tests/unit/engines.
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from meshpipeline.adapters.mesh_execution.cloud_run_client import _tar_dir
from meshpipeline.application.native_submission import workspace_digest
from meshpipeline.contracts.mesh_execution import (
    NATIVE_PASS_FACT,
    NATIVE_PAYLOAD_FACT,
    note_native_pass,
    note_native_payload,
    read_native_payload,
    submission_payload_files,
)


def _retry_pass_workspace(tmp_path: Path) -> Path:
    """An attempt workspace mid-retry: the revised case beside the previous pass's outputs."""
    ws = tmp_path / "attempt_2"
    # the revised case this pass wants meshed
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "blockMeshDict").write_text("blocks")
    (ws / "system" / "snappyHexMeshDict").write_text("snappy")
    (ws / "constant" / "triSurface").mkdir(parents=True)
    (ws / "constant" / "triSurface" / "body.stl").write_text("solid body")
    # the PREVIOUS pass's collected outputs, sharing the same workspace
    (ws / "constant" / "polyMesh").mkdir(parents=True)
    (ws / "constant" / "polyMesh" / "faces").write_text("f" * 4096)
    (ws / "constant" / "triSurface" / "body.eMesh").write_text("edges")
    (ws / "VTK" / "run_0").mkdir(parents=True)
    (ws / "VTK" / "run_0" / "internal.vtu").write_text("v" * 4096)
    (ws / "mesh.msh").write_text("m" * 4096)
    (ws / "snappyHexMesh.log").write_text("log")
    # local-only driver state
    (ws / "input.stl").write_text("solid input")
    (ws / ".last_plan.json").write_text("{}")
    return ws


def _members(tar_bytes: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return {m.name for m in tf.getmembers() if m.isreg()}


def _declare(ws: Path) -> None:
    note_native_pass(ws, 2)
    note_native_payload(ws, ["system", "constant/triSurface/body.stl"])


def test_no_declaration_keeps_the_whole_workspace_behaviour(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    names = _members(_tar_dir(ws))
    assert "constant/polyMesh/faces" in names          # everything ships, exactly as before
    assert "mesh.msh" in names
    assert submission_payload_files(ws) is None
    # the fallback digest is the historical formula over every file, verbatim
    parts = [f"{p.relative_to(ws)}:{hashlib.sha256(p.read_bytes()).hexdigest()}"
             for p in sorted(q for q in ws.rglob("*") if q.is_file())]
    assert workspace_digest(ws) == hashlib.sha256("\n".join(parts).encode()).hexdigest()


def test_declared_payload_scopes_the_archive_to_the_case_plus_the_facts(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    _declare(ws)
    names = _members(_tar_dir(ws))
    assert names == {
        "system/blockMeshDict", "system/snappyHexMeshDict",
        "constant/triSurface/body.stl",
        NATIVE_PASS_FACT, NATIVE_PAYLOAD_FACT,
    }
    # the prior pass's outputs and local-only state never ride along
    assert not any(n.startswith(("VTK/", "constant/polyMesh/")) for n in names)
    assert "input.stl" not in names
    assert "constant/triSurface/body.eMesh" not in names


def test_the_scoped_archive_extracts_through_the_fail_closed_extractor(tmp_path):
    from meshpipeline.sandbox.safe_extract import safe_extract_tar
    ws = _retry_pass_workspace(tmp_path)
    _declare(ws)
    dest = tmp_path / "remote"
    safe_extract_tar(fileobj=io.BytesIO(_tar_dir(ws)), dest=dest)
    # file members with nested paths materialise their parents on the far side
    assert (dest / "constant" / "triSurface" / "body.stl").read_text() == "solid body"
    assert (dest / "system" / "snappyHexMeshDict").exists()
    assert (dest / NATIVE_PASS_FACT).read_text() == "2"


def test_digest_scope_is_the_tar_scope_by_construction(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    _declare(ws)
    resolved = submission_payload_files(ws)
    assert resolved is not None
    assert {p.relative_to(ws).as_posix() for p in resolved} == _members(_tar_dir(ws))


def test_digest_ignores_what_the_upload_never_carries_and_pins_what_it_does(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    _declare(ws)
    before = workspace_digest(ws)
    # a replayed pass finds the workspace grown by post-dispatch writes - same payload, same digest
    (ws / "constant" / "polyMesh" / "faces").write_text("rebuilt")
    (ws / "mesh_manifest.json").write_text("{}")
    assert workspace_digest(ws) == before
    # but any byte the upload DOES carry re-identifies the payload
    (ws / "constant" / "triSurface" / "body.stl").write_text("solid other")
    assert workspace_digest(ws) != before


def test_the_facts_themselves_are_part_of_the_payload_identity(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    _declare(ws)
    base = workspace_digest(ws)
    note_native_pass(ws, 3)                        # a revised pass is a different submission
    assert workspace_digest(ws) != base
    note_native_pass(ws, 2)
    assert workspace_digest(ws) == base            # an exact replay re-derives the identity
    note_native_payload(ws, ["system"])            # a different declared scope likewise
    assert workspace_digest(ws) != base


def test_a_missing_declared_member_is_loud_never_a_smaller_upload(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    note_native_pass(ws, 2)
    note_native_payload(ws, ["system", "constant/triSurface/absent.stl"])
    with pytest.raises(ValueError, match="missing"):
        submission_payload_files(ws)
    with pytest.raises(ValueError, match="missing"):
        _tar_dir(ws)
    with pytest.raises(ValueError, match="missing"):
        workspace_digest(ws)


def test_a_member_escaping_the_workspace_is_refused(tmp_path):
    ws = _retry_pass_workspace(tmp_path)
    (ws / NATIVE_PAYLOAD_FACT).write_text('["../outside"]')
    with pytest.raises(ValueError, match="escapes"):
        submission_payload_files(ws)


@pytest.mark.parametrize("raw", ["not json", "{}", "[]", '[""]', "[1, 2]", '["a", 3]'])
def test_a_malformed_fact_fails_closed_to_the_whole_workspace(tmp_path, raw):
    ws = _retry_pass_workspace(tmp_path)
    (ws / NATIVE_PAYLOAD_FACT).write_text(raw)
    assert read_native_payload(ws) is None
    assert "mesh.msh" in _members(_tar_dir(ws))    # old behaviour, exactly
