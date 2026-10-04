# Responsibility: Verify meshQualityControls minVol follows the mesh's own cell scale - the one
# absolute quality bar snappyHexMesh reads - in the external and the internal case.
# Boundaries: the dictionaries this system writes; the mesher's response is measured in the lab.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshpipeline.engines.snappy import snappy_runner as R

REC = {"base_cell": 0.041666666666666664, "surface_level": (2, 2), "afford_level": 3,
       "feature_level": 3, "feature_level_true": 3, "budget_capped": False,
       "distance_bands": [(0.0625, 2), (0.25, 1)], "resolve_feature_angle": 35.0,
       "min_feature": 0.001, "surface_area": 1.0, "cells_across_min_feature": 8}
ANALYSIS = {"bbox_min": [0.0, -0.5, -0.5], "bbox_max": [1.0, 0.5, 0.5], "L": 1.0}
STRATEGY = {"n_layers": 5, "first_layer_rel": 0.35, "max_cells": 2_000_000}


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _min_vol_in(text: str) -> float:
    return float(re.search(r"minVol ([0-9.eE+-]+);", text).group(1))


def test_min_vol_keeps_the_historical_bar_for_metre_scale_cells():
    assert R.layer_min_vol(0.05, n_layers=5, min_thickness_rel=0.05) == R.MIN_VOL_HISTORICAL


def test_min_vol_drops_below_the_thinnest_legitimate_layer_cell_on_fine_cells():
    # a 0.2 mm wall cell: a five-layer stack's cells hold a few 1e-13 m^3 - the absolute bar
    # made every one of them illegal (SAE notchback: 182 357 faces in the first layer check)
    h = 0.0002
    mv = R.layer_min_vol(h, n_layers=5, min_thickness_rel=0.05)
    first_layer = 0.35 * h / 1.2 ** 4
    legit_pyramid = h * h * first_layer / 6.0
    assert 0.0 < mv < 1e-2 * legit_pyramid < R.MIN_VOL_HISTORICAL


def test_min_vol_is_monotone_in_the_cell_and_never_zero():
    vols = [R.layer_min_vol(h, n_layers=5, min_thickness_rel=0.05)
            for h in (1e-6, 1e-5, 1e-4, 1e-3, 1e-2)]
    assert all(v > 0.0 for v in vols)
    assert vols == sorted(vols)
    assert R.layer_min_vol(float("nan"), n_layers=5, min_thickness_rel=0.05) \
        == R.MIN_VOL_HISTORICAL


def _render(tmp_path, *, scale=1.0):
    rec = dict(REC, base_cell=REC["base_cell"] * scale,
               distance_bands=[(d * scale, lv) for d, lv in REC["distance_bands"]])
    return R.render_snappy_case(
        _ws(tmp_path), surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
        recommendation=rec, domain_min=[-4.0 * scale, -3.0 * scale, -3.0 * scale],
        domain_max=[7.0 * scale, 4.0 * scale, 4.0 * scale], strategy=STRATEGY)


def test_the_external_dict_carries_the_scaled_min_vol(tmp_path):
    _render(tmp_path / "m")
    text = (tmp_path / "m" / "system" / "snappyHexMeshDict").read_text()
    assert _min_vol_in(text) == pytest.approx(1e-13), "metre scale: unchanged"
    _render(tmp_path / "mm", scale=0.01)
    small = _min_vol_in((tmp_path / "mm" / "system" / "snappyHexMeshDict").read_text())
    assert small < 1e-15, "a body a hundred times smaller gets a bar for its own cells"


def test_the_internal_dict_carries_the_scaled_min_vol(tmp_path):
    def _internal(ws, scale):
        R.render_internal_case(
            _ws(ws), names={"wall": "wall", "inlet": "inlet", "outlet": "outlet"},
            features={"wall": "wall.eMesh", "inlet": "inlet.eMesh", "outlet": "outlet.eMesh"},
            interior_point=[0.5 * scale, 0.0, 0.0], bbox_min=[0.0, -0.05 * scale, -0.05 * scale],
            bbox_max=[1.0 * scale, 0.05 * scale, 0.05 * scale], base_cell=0.1 * scale / 24,
            surface_level=2, feature_level=3, n_layers=5)
        return _min_vol_in((ws / "system" / "snappyHexMeshDict").read_text())
    big = _internal(tmp_path / "m", 1.0)
    small = _internal(tmp_path / "mm", 0.01)          # a 10 mm part, a 1 mm bore
    assert big <= R.MIN_VOL_HISTORICAL
    assert small < big / 1e4, "minVol follows the cell volume (scale cubed)"
