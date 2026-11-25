"""SHMESH-11e: a mesh that fails with the closed faces cut at their seams is made again once with them whole."""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.cad import midsurface as ms
from meshpipeline.engines.gmsh import shell_adapter as SA
from meshpipeline.engines.shell_mesh import ShellMesh


def _tiny_mesh() -> ShellMesh:
    xyz = np.array([[0.0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]])
    return ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.ones(1), mid=["Mid_1"], part=["Part_1"])


def _calls(monkeypatch, fail_first: bool):
    seen: list[bool] = []

    def fake(brep, record, **_kw):
        seen.append(SA._WHOLE_NOW[0])
        if fail_first and len(seen) == 1:
            raise ValueError("The 1D mesh seems not to be forming a closed loop")
        return _tiny_mesh(), {"quality": {}}

    monkeypatch.setattr(SA, "mesh_shell", fake)
    return seen


def test_a_mesh_failing_with_cut_closed_faces_is_made_again_whole(tmp_path, monkeypatch):
    (tmp_path / ms.RECORD_FILE).write_text(json.dumps({}))
    seen = _calls(monkeypatch, fail_first=True)
    mesh, applied = SA.mesh_run(tmp_path)
    assert seen == [False, True]                      # cut first, then whole
    assert applied["split_whole"]["rule"] == "SHMESH-11e"
    assert "closed loop" in applied["split_whole"]["cut_failed"]
    assert any("SHMESH-11e" in n for n in mesh.notes)
    assert SA._WHOLE_NOW[0] is False                  # (the flag never outlives the retry)


def test_a_mesh_that_works_cut_is_made_once(tmp_path, monkeypatch):
    (tmp_path / ms.RECORD_FILE).write_text(json.dumps({}))
    seen = _calls(monkeypatch, fail_first=False)
    _mesh, applied = SA.mesh_run(tmp_path)
    assert seen == [False]
    assert "split_whole" not in applied


def test_no_retry_with_the_rule_off(tmp_path, monkeypatch):
    (tmp_path / ms.RECORD_FILE).write_text(json.dumps({}))
    _calls(monkeypatch, fail_first=True)
    monkeypatch.setattr(SA, "WHOLE_RETRY", False)
    with pytest.raises(ValueError, match="closed loop"):
        SA.mesh_run(tmp_path)
