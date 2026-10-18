# Responsibility: Verify what rides home with a mesh: scratch never does, and the optional volume export is left out
# only when the archive would not fit the extraction ceiling - never the mesh, its boundary or its measurements.
from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import meshpipeline.engines.return_scope as RS


def _ws(tmp_path: Path, *, volume_bytes: int = 3_000_000) -> Path:
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    # ASCII mesh files compress well; the volume export (binary) barely does
    (pm / "owner").write_text("\n".join(str(i % 977) for i in range(200_000)))
    (pm / "points").write_text("\n".join(f"({i * 0.001:.6f} 0.1 0.2)" for i in range(50_000)))
    vtk = tmp_path / "VTK" / "case_0"
    (vtk / "boundary").mkdir(parents=True)
    (vtk / "internal.vtu").write_bytes(os.urandom(volume_bytes))
    (vtk / "boundary" / "wall.vtp").write_bytes(os.urandom(50_000))
    (tmp_path / "mesh_quality.json").write_text(json.dumps({"cells": 1}))
    return tmp_path


def _archive_bytes(ws: Path) -> int:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(ws.iterdir()):
            tf.add(str(child), arcname=child.name)
    return len(buf.getvalue())


def test_the_measure_matches_the_real_archive_and_never_reads_low(tmp_path):
    ws = _ws(tmp_path, volume_bytes=6_000_000)
    est, real = RS.estimate_archive_bytes(ws), _archive_bytes(ws)
    assert 0.99 * real <= est <= 1.15 * real, (est, real)


def test_a_file_that_compresses_unevenly_is_measured_whole(tmp_path):
    # compressible at its start, middle and end, random between: a sample of three would read it
    # as almost nothing
    rnd = os.urandom(3_000_000)
    p = tmp_path / "uneven.bin"
    p.write_bytes(b"\0" * 1_100_000 + rnd + b"\0" * 1_100_000 + rnd + b"\0" * 1_100_000)
    real = len(gzip.compress(p.read_bytes(), 9))
    assert real <= RS.compressed_size(p) <= 1.05 * real + 1024


def test_nothing_is_left_out_of_an_archive_that_fits(tmp_path):
    ws = _ws(tmp_path)
    assert RS.scope_return(ws, ceiling=512 * 2**20) is None
    assert (ws / "VTK" / "case_0" / "internal.vtu").exists()
    assert not (ws / RS.RETURN_SCOPE_FILE).exists()


def test_the_volume_export_is_left_out_when_the_archive_would_not_fit(tmp_path):
    ws = _ws(tmp_path)
    before = RS.estimate_archive_bytes(ws)
    rec = RS.scope_return(ws, ceiling=int(before * 0.6))
    assert rec and rec["fits"]
    assert [x["file"] for x in rec["left_out"]] == ["VTK/case_0/internal.vtu"]
    assert not (ws / "VTK" / "case_0" / "internal.vtu").exists()
    # the mesh, its boundary and its measurements all stay
    for keep in ("constant/polyMesh/owner", "constant/polyMesh/points",
                 "VTK/case_0/boundary/wall.vtp", "mesh_quality.json"):
        assert (ws / keep).exists(), keep
    assert "volume export" in rec["note"]
    assert json.loads((ws / RS.RETURN_SCOPE_FILE).read_text())["left_out"]


def test_a_mesh_too_big_even_without_its_volume_says_so(tmp_path):
    ws = _ws(tmp_path)
    rec = RS.scope_return(ws, ceiling=1000)
    assert rec and not rec["fits"] and (ws / "constant" / "polyMesh" / "owner").exists()


def test_scratch_never_rides_home(tmp_path):
    ws = _ws(tmp_path)
    (ws / "_rescue" / "as_built").mkdir(parents=True)
    (ws / "_rescue" / "as_built" / "owner").write_text("x")
    rec = RS.scope_return(ws, ceiling=512 * 2**20)
    assert rec and rec["scratch_removed"] == ["_rescue/"] and not (ws / "_rescue").exists()
    assert (ws / "VTK" / "case_0" / "internal.vtu").exists()


def test_every_engine_run_is_scoped_before_it_goes_home(tmp_path, monkeypatch):
    import meshpipeline.engines.dispatch as D
    ws = _ws(tmp_path)
    (ws / "_rescue").mkdir()

    def _runner(workspace, *, bashrc, timeout):
        return {"rc": 0, "timed_out": False}
    monkeypatch.setattr(D, "engine_runners", lambda: {"gmsh": _runner})
    out = D.run_engine_local(ws, engine="gmsh", timeout=10)
    assert out["rc"] == 0 and out["return_scope"]["scratch_removed"] == ["_rescue/"]


def test_an_empty_file_still_costs_its_archive_header(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert 512 <= RS.compressed_size(p) <= 600
    assert RS.compressed_size(Path(gzip.__file__)) > 600
