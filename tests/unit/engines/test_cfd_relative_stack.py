# Responsibility: Verify CFDREQ-33 - a resolved wall refined to one level gets its planned stack written relative to the
# plain wall cell (each finer face the same stack in its own cells), the plain cell rides in the dictionary, and every
# reader of the planned first layer (layer_census.planned_first_of, census by height) turns it back into each face's
# planned height.
# Boundaries: pure over dictionary text and a small written polyMesh; no mesher is run.
from __future__ import annotations

import re

import pytest
from tests.unit.engines.test_cfd_external_wall import SHM, _ask
from tests.unit.engines.test_layer_census import _write_column_mesh

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import layer_census as LC

ONE_LEVEL = "castellatedMeshControls { refinementSurfaces { body { level (4 4); } } resolveFeatureAngle 25; }\n"
TWO_LEVELS = "castellatedMeshControls { refinementSurfaces { body { level (3 4); } } resolveFeatureAngle 25; }\n"


def _num(text: str, key: str) -> float | None:
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


def test_a_resolved_wall_at_one_level_gets_its_stack_on_each_face(tmp_path):
    wc = 0.0004                                           # the stack about 1.7 plain wall cells deep
    absolute = CR.snappy_dict(_ask("10 m/s", 0.06), TWO_LEVELS + SHM, wall_cell_m=wc, relative_ok=True)
    assert "relativeSizes false;" in absolute            # faces coarser than the wall cell: kept in metres
    first = _num(absolute, "firstLayerThickness")
    ask = _ask("10 m/s", 0.06)
    out = CR.snappy_dict(ask, ONE_LEVEL + SHM, wall_cell_m=wc, relative_ok=True)
    assert "relativeSizes true;" in out and f"{LC.PLAIN_CELL_KEY} {wc:g};" in out
    assert _num(out, "firstLayerThickness") == pytest.approx(first / wc, rel=1e-5)
    assert _num(out, "nSurfaceLayers") == _num(absolute, "nSurfaceLayers")
    assert _num(out, "minVol") < _num(absolute, "minVol")
    # the readers get the plain wall's height back, never the share as metres
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text(out)
    assert LC.planned_plain_cell_of(tmp_path) == pytest.approx(wc)
    assert LC.planned_first_of(tmp_path) == pytest.approx(first, rel=1e-5)


def test_a_stack_shallower_than_half_the_wall_cell_stays_in_metres():
    # the same ask on 1.6 mm wall cells: 10 layers from 0.026 mm make a 0.67 mm stack, 0.42 of the cell - it already
    # fits the cells of faces a level finer, so it is kept absolute there (relative, 00078646 lost its full stack)
    wc = 0.0016
    out = CR.snappy_dict(_ask("10 m/s", 0.06), ONE_LEVEL + SHM, wall_cell_m=wc, relative_ok=True)
    first, n = _num(out, "firstLayerThickness"), _num(out, "nSurfaceLayers")
    assert "relativeSizes false;" in out and LC.PLAIN_CELL_KEY not in out
    assert CR._stack_depth(first, _num(out, "expansionRatio"), int(n), None) < CR.RELATIVE_MIN_DEPTH * wc
    deep = CR.snappy_dict(_ask("10 m/s", 0.6), ONE_LEVEL + SHM, wall_cell_m=wc, relative_ok=True)
    assert "relativeSizes true;" in deep                   # 1.3 cells deep: relative, as before


def test_a_relative_stack_without_a_plain_cell_still_reads_no_height(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "snappyHexMeshDict").write_text("relativeSizes true; firstLayerThickness 0.3;")
    assert LC.planned_first_of(tmp_path) is None and LC.planned_plain_cell_of(tmp_path) is None


def test_a_face_finer_than_the_plain_cell_is_judged_on_its_own_plan(tmp_path):
    # unit-wide wall faces under a plain cell 4 wide: two levels finer, planned a first layer 4 times thinner
    pm = tmp_path / "polyMesh"
    _write_column_mesh(pm, [0.005, 0.006, 1.0, 1.0])
    flat, _ = LC.census(pm, 2, ["wall"], planned_first_m=0.02)
    fit, _ = LC.census(pm, 2, ["wall"], planned_first_m=0.02, plain_cell_m=4.0)
    assert flat["wall"]["first_vs_plan"] == 0.25          # read against the plain wall's plan: "thinner than asked"
    assert fit["wall"]["first_vs_plan"] == 1.0 and fit["wall"]["planned_plain_cell_m"] == 4.0
    assert fit["wall"]["area_at_plan"] == 1.0 and fit["wall"]["area_first_at_plan"] == 1.0
