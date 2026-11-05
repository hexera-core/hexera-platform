# Responsibility: Verify the live cell/element count is read from the lines the meshers REALLY print, using snippets of real logs (fixtures/mesher_logs: snappyHexMesh from a layer-coverage replay, gmsh from an FEA regression run, cfMesh from a public-corpus lab run - paths removed).
from __future__ import annotations

import json
import shutil
from pathlib import Path

from meshpipeline.engines.mesher_stage import read_stage

LOGS = Path(__file__).parent / "fixtures" / "mesher_logs"


def _ws(tmp_path: Path, fixture: str, as_name: str, text: str | None = None) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    if text is None:
        shutil.copy(LOGS / fixture, ws / as_name)
    else:
        (ws / as_name).write_text(text)
    return ws


def test_snappy_counts_its_mesh_summaries_not_the_float_estimate(tmp_path):
    got = read_stage(_ws(tmp_path, "snappyHexMesh_layers.log", "snappyHexMesh_1.log"))
    assert got["key"] == "layers" and got["n"] == 2679296


def test_snappy_stopped_at_the_ideal_estimate_keeps_the_last_real_count(tmp_path):
    text = (LOGS / "snappyHexMesh_layers.log").read_text()
    cut = text[: text.index("Shrinking and layer addition phase")]
    assert "cells:3.08262e+06" in cut                       # the float estimate is the last cells: line
    got = read_stage(_ws(tmp_path, "", "snappyHexMesh_1.log", cut))
    assert got["n"] == 2044453


def test_gmsh_counts_the_tetrahedra_it_created(tmp_path):
    ws = _ws(tmp_path, "gmshfea_volume.log", "gmshfea.log")
    (ws / "fea_stage.json").write_text(json.dumps({"key": "volume", "label": "filling the solid with elements"}))
    got = read_stage(ws)
    assert got["key"] == "volume" and got["n"] == 185067


def test_gmsh_before_the_volume_gives_no_count(tmp_path):
    text = (LOGS / "gmshfea_volume.log").read_text()
    early = text[: text.index("Meshing 3D")]                 # "75986 nodes 151947 elements" is the surface, not a count
    ws = _ws(tmp_path, "", "gmshfea.log", early)
    assert "n" not in read_stage(ws)


def test_cfmesh_counts_its_cells_and_ignores_box_counts(tmp_path):
    got = read_stage(_ws(tmp_path, "cartesianMesh_cells.log", "cartesianMesh.log"))
    assert got["key"] == "cells"
    assert got["n"] == 101384                                # the last "New cells size", not "Octree nodes 131081"
    text = (LOGS / "cartesianMesh_cells.log").read_text()
    first = text[: text.index("Finished extracting polyMesh")]
    got = read_stage(_ws(tmp_path / "b", "", "cartesianMesh.log", first)) if (tmp_path / "b").mkdir() is None else None
    assert got["n"] == 101392
