# Responsibility: Verify that cfMesh's check_mesh prefers the measurement taken beside the mesh over a local checkMesh run.
from __future__ import annotations

import json

import pytest

import meshpipeline.engines.cfmesh.foam_exec as F


class _Proc:
    def __init__(self, out=""):
        self.returncode = 0
        self.stdout = out
        self.stderr = ""


def test_the_measurement_taken_beside_the_mesh_is_read_back_without_a_local_run(tmp_path,
                                                                                monkeypatch):
    measured = {"mesh_ok": True, "max_non_ortho": 38.2, "max_skewness": 1.7, "cells": 5000,
                "faces": 16000, "skew_faces": 0, "skew_fraction": 0.0, "fatal": []}
    (tmp_path / "mesh_quality.json").write_text(json.dumps(measured))

    def _no_spawn(*a, **k):
        raise AssertionError("checkMesh was shelled out although a measurement was on disk")
    monkeypatch.setattr(F, "run_guarded", _no_spawn)
    assert F.check_mesh(tmp_path) == measured


@pytest.mark.parametrize("junk", ["", "not json", "[]", "{}"])
def test_an_unreadable_or_empty_measurement_falls_back_to_measuring(tmp_path, monkeypatch, junk):
    (tmp_path / "mesh_quality.json").write_text(junk)
    monkeypatch.setattr(F, "scan_case_dicts", lambda ws: "")
    monkeypatch.setattr(F, "run_guarded", lambda *a, **k: _Proc("Mesh OK\n"))
    q = F.check_mesh(tmp_path)
    assert q["mesh_ok"] is True and q["fatal"] == []


def test_a_region_query_never_uses_the_whole_mesh_measurement(tmp_path, monkeypatch):
    (tmp_path / "mesh_quality.json").write_text(json.dumps({"mesh_ok": True, "cells": 1}))
    monkeypatch.setattr(F, "scan_case_dicts", lambda ws: "")
    seen = []
    monkeypatch.setattr(F, "run_guarded",
                        lambda argv, **k: (seen.append(argv[-1]), _Proc("Mesh OK\n"))[1])
    F.check_mesh(tmp_path, region="fluid")
    assert seen and "-region fluid" in seen[0]
