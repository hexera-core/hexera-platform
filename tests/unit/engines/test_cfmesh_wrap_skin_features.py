# Responsibility: Pin LAD-cfd-10 - cfMesh meshes the wrap rung's skin from its named STL with no feature edges but its patch borders; every other surface keeps its feature edges.
from __future__ import annotations

import json
import subprocess

import pytest

from meshpipeline.cad.surface_wrap import WRAP_REQUEST_FILE
from meshpipeline.engines.cfmesh import cfmesh_runner as R


def _ws(tmp_path, *, wrapped: bool):
    (tmp_path / "geom.stl").write_text("solid wall\nendsolid wall\n")
    if wrapped:
        (tmp_path / WRAP_REQUEST_FILE).write_text(json.dumps({"flow": "internal", "engine": "cfmesh",
                                                             "rule": "LAD-cfd-5"}))
    return tmp_path


def _feature_edges_ran(monkeypatch) -> list:
    calls: list = []

    def fake(cmd, cwd=None, **_kw):
        calls.append(cmd)
        if "surfaceFeatureEdges" in " ".join(cmd):
            (R.Path(cwd) / "geom.fms").write_text("fms")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(R, "run_guarded", fake)
    return calls


def test_a_wrapped_skin_is_meshed_from_its_named_stl_with_no_feature_edges(tmp_path, monkeypatch):
    # the cooling pipe's wrap skin: 30-degree feature edges left 5 zero-area faces on its folds; none gave a valid mesh
    ws = _ws(tmp_path, wrapped=True)
    (ws / "geom.fms").write_text("an older attempt's feature file")
    calls = _feature_edges_ran(monkeypatch)
    assert R._to_fms(ws, 30.0, "/x/bashrc") == "geom.stl"
    assert calls == []
    assert not (ws / "geom.fms").exists()
    assert R.wrapped_skin(ws)


def test_a_surface_off_the_rung_keeps_its_feature_edges(tmp_path, monkeypatch):
    ws = _ws(tmp_path, wrapped=False)
    calls = _feature_edges_ran(monkeypatch)
    assert R._to_fms(ws, 30.0, "/x/bashrc") == "geom.fms"
    assert any("surfaceFeatureEdges -angle 30" in " ".join(c) for c in calls)
    assert not R.wrapped_skin(ws)


@pytest.mark.parametrize("wrapped", [True, False])
def test_the_case_reads_the_surface_it_was_given(tmp_path, monkeypatch, wrapped):
    ws = _ws(tmp_path, wrapped=wrapped)
    _feature_edges_ran(monkeypatch)
    prep = R.prepare_surface_internal(ws, surfaces_src={"wall": str(_tri(tmp_path))})
    assert prep["surface_file"] == ("geom.stl" if wrapped else "geom.fms")


def _tri(tmp_path):
    p = tmp_path / "src_wall.stl"
    p.write_text("solid wall\nfacet normal 0 0 1\n outer loop\n  vertex 0 0 0\n  vertex 1 0 0\n  vertex 0 1 0\n"
                 " endloop\nendfacet\nendsolid wall\n")
    return p
