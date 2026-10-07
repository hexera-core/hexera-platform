# Responsibility: Verify the external snappy case's use of the sealed-space reading - measured
# once per wall cell, switchable, never a prerequisite - the plain words it adds to the note,
# and the leak-closure targets the dictionary carries.
# Boundaries: the driver's helpers and the renderer; the reading is tested in test_sealed_cavities.
from __future__ import annotations

import asyncio

import numpy as np

import meshpipeline.engines.snappy.settings as scfg
from meshpipeline.engines.sealed_cavities import CavityReading, SealedCavity
from meshpipeline.engines.snappy import drivers as D
from meshpipeline.engines.snappy import snappy_runner as R

REC = {"base_cell": 0.04, "surface_level": (2, 2), "afford_level": 3}


class _Surface:
    def __init__(self, path):
        self.path = path


def _reading(n=1):
    cav = tuple(SealedCavity(point=(0.0, 0.05, 0.02), volume_m3=1e-4, clearance_m=0.01)
                for _ in range(n))
    return CavityReading(cavities=cav, wet_sides=np.ones(4, dtype=np.int8), voxel_m=3e-4,
                         seal_gap_m=1.3e-3)


def test_the_note_says_what_was_kept_out_and_why_in_plain_words():
    note = D._sealed_note(_reading())
    assert "1 enclosed space inside the body" in note
    assert "narrower than 1.3 mm" in note and "kept out of the mesh" in note
    assert "2 enclosed spaces" in D._sealed_note(_reading(2))
    assert D._sealed_note(None) == "" and D._sealed_note(_reading(0)) == ""


def test_switched_off_or_unmeasurable_reads_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(scfg, "SNAPPY_SEAL_CAVITIES", False)
    assert asyncio.run(D._sealed_cavities({}, _Surface(tmp_path / "x.stl"), REC, {})) is None
    monkeypatch.setattr(scfg, "SNAPPY_SEAL_CAVITIES", True)
    # a plan the cell cannot be read from, or a surface with no file: no reading, no failure
    assert asyncio.run(D._sealed_cavities({}, _Surface(tmp_path / "x.stl"), {}, {})) is None
    assert asyncio.run(D._sealed_cavities({}, object(), REC, {})) is None


def test_one_reading_per_wall_cell(monkeypatch, tmp_path):
    import meshpipeline.engines.sealed_cavities as SC
    calls = []

    def _fake(tris, *, cell_m, **kw):
        calls.append(cell_m)
        return _reading()
    monkeypatch.setattr(scfg, "SNAPPY_SEAL_CAVITIES", True)
    monkeypatch.setattr(SC, "read_cavities", _fake)
    import meshpipeline.cad.thin_features as TF
    monkeypatch.setattr(TF, "staged_triangles", lambda p: [])
    cache: dict = {}
    s = _Surface(tmp_path / "x.stl")
    a = asyncio.run(D._sealed_cavities(cache, s, REC, {}))
    b = asyncio.run(D._sealed_cavities(cache, s, REC, {}))
    assert a is b and len(calls) == 1, "the same wall cell is read once per build"
    asyncio.run(D._sealed_cavities(cache, s, REC, {"surface_level": [3]}))
    assert len(calls) == 2, "a plan with a different wall cell gets its own reading"


def test_without_a_reading_the_field_is_the_builds_own(tmp_path):
    marker = object()
    assert asyncio.run(D._wetted_field({}, _Surface(tmp_path / "x.stl"), None, marker)) is marker


RENDER_REC = {"base_cell": 0.041666666666666664, "surface_level": (2, 2), "afford_level": 3,
              "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
              "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
              "min_feature": 0.001, "surface_area": 1.0, "cells_across_min_feature": 8}


def _render(tmp_path, **kw):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    return R.render_snappy_case(
        tmp_path, surface_name="body", feature_file="body.eMesh",
        analysis={"bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "L": 1.0},
        recommendation=RENDER_REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
        strategy={"n_layers": 5, "first_layer_rel": 0.35, "max_cells": 2_000_000}, **kw)


def test_sealed_points_become_leak_closure_targets(tmp_path):
    summary = _render(tmp_path, outside_points=[(0.5, 0.0, 0.0), (0.2, 0.1, 0.0)])
    text = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    assert "locationsOutsideMesh ((0.5 0 0) (0.2 0.1 0)); useLeakClosure true;" in text
    assert summary["sealed_points"] == [[0.5, 0.0, 0.0], [0.2, 0.1, 0.0]]


def test_no_sealed_points_leave_no_trace(tmp_path):
    summary = _render(tmp_path)
    text = (tmp_path / "system" / "snappyHexMeshDict").read_text()
    assert "locationsOutsideMesh" not in text and "useLeakClosure" not in text
    assert "sealed_points" not in summary
