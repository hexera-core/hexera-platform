# Responsibility: a polyMesh past the viewer field cap is refused from its header, never loaded.
from __future__ import annotations

from pathlib import Path

from meshpipeline.render import face_quality as FQ


def _faces_file(tmp_path: Path, count: int) -> Path:
    pm = tmp_path / "polyMesh"; pm.mkdir()
    (pm / "faces").write_text(
        "/*--------------------------------*- C++ -*----------------------------------*\\\n"
        "  =========                 |\n"
        "\\*---------------------------------------------------------------------------*/\n"
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       faceList;\n"
        "    object      faces;\n}\n// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
        f"{count}\n(\n4(0 1 2 3)\n)\n")
    return pm


def test_declared_count_reads_the_header(tmp_path):
    pm = _faces_file(tmp_path, 12345)
    assert FQ.declared_count(pm / "faces") == 12345
    assert FQ.declared_count(pm / "missing") is None


def test_a_mesh_past_the_cap_is_refused_without_being_loaded(tmp_path, monkeypatch):
    # shell_tube_bundle_009: 7.3 M cells, ~22 M faces; loading ~1 GB of arrays only to skip them
    # was part of what the kernel killed at the worker's 8 GiB
    pm = _faces_file(tmp_path, FQ.MAX_FACES_FOR_FIELDS + 1)

    def _never(*_a, **_k):
        raise AssertionError("read_polymesh must not run for a mesh past the cap")
    monkeypatch.setattr(FQ, "read_polymesh", _never)
    assert FQ.quality_fields(pm, non_ortho_limit=65.0) is None


def test_a_mesh_under_the_cap_still_goes_to_the_reader(tmp_path, monkeypatch):
    pm = _faces_file(tmp_path, 1)
    seen = []

    def _reader(p):
        seen.append(p)
        raise FQ.UnreadableMesh("stub")
    monkeypatch.setattr(FQ, "read_polymesh", _reader)
    assert FQ.quality_fields(pm, non_ortho_limit=65.0) is None
    assert seen == [pm]
