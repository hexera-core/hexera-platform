# Responsibility: Verify that an FEA plan is never coarsened for the size field's own cube budget (PLAN-fea-8, rulebook 32): an octree that stopped short still says how many elements the plan can at most need, and a plan whose ceiling fits keeps the rules' sizes.
# Boundaries: the pure checks write histograms by hand; the geometry checks plan a staged plate through the real driver with the cube budget cut down, nothing is meshed.
"""THE OCTREE BUDGET IS NOT AN ELEMENT BUDGET (FEA hunt, 27 Oct).

A thin wide part - a 2 m x 1 m x 5 mm panel, a glazed door, a sheet-metal bracket - fills the size
field's octree with cubes long before it has many elements: the octree stops at MAX_CUBES, the
histogram says "incomplete", and the plan used to read that as "over the limit" and coarsen its walls
to 1.3 elements while about 3M elements stood against a 145M limit. 4 of the first 16 real assemblies
the hunt swept came back with walls short of 2 elements that way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines import fea_plan as P
from meshpipeline.engines.fea_size_field import Histogram, elements

#: a 200 x 100 x 5 mm plate's record (as tests/unit/engines/test_fea_plan.py writes it, trimmed)
RECORD = {
    "bodies": 1,
    "thickness": {"min_mm": 5.0, "face": "Face_3"},
    "faces": {"Face_1": {"kind": "plane", "thickness_min_mm": 200.0},
              "Face_3": {"kind": "plane", "thickness_min_mm": 5.0}},
    "hot_spots": [],
}
ROWS = [(2.5e-3, 1e-4), (0.01, 1e-5)]


def _finish(hist: Histogram, limit: int = P.ELEMENT_LIMIT) -> dict:
    return P.finish(RECORD, P.zones(RECORD), hist, diag_m=0.2236, limit=limit)


def test_a_count_stopped_short_fits_when_its_ceiling_fits():
    ceiling = [(2.5e-3, 3e-4), (0.01, 1e-5)]
    plan = _finish(Histogram(rows=ROWS, complete=False, upper=ceiling))
    assert plan["fits"]
    assert plan["estimate"]["ceiling"] >= plan["estimate"]["elements"]
    assert plan["histogram_ceiling"] and not plan["histogram_complete"]


def test_a_count_stopped_short_whose_ceiling_is_over_the_limit_still_coarsens():
    ceiling = [(2.5e-3, 3e-4), (0.01, 1e-5)]
    need = elements(Histogram(rows=ceiling), 0.02236, P.PER_H3)
    plan = _finish(Histogram(rows=ROWS, complete=False, upper=ceiling), limit=int(need))   # 0.85 x need < need
    assert not plan["fits"]


def test_a_count_stopped_short_with_no_ceiling_is_never_taken_to_fit():
    """the old reading stands where nothing bounds the count (a histogram recorded before rulebook 32)"""
    assert not _finish(Histogram(rows=ROWS, complete=False))["fits"]


def test_a_complete_count_is_read_as_before():
    plan = _finish(Histogram(rows=ROWS))
    assert plan["fits"] and "ceiling" not in plan["estimate"] and "histogram_ceiling" not in plan


# # the real size field on a staged part

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_size_field as SF  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def test_the_ceiling_bounds_the_finished_count(tmp_path, monkeypatch):
    """the ceiling of a cut-short count is never under what the whole count finds"""
    ws = _stage(fx.plate_with_hole(), tmp_path / "bound")
    record, surface, diag = FD.measure_staged(ws)
    full = FD.make_plan(record, surface, diag)
    assert full["histogram_complete"]
    for cubes in (20, 2_000, 20_000):
        monkeypatch.setattr(SF, "MAX_CUBES", cubes)
        cut = FD.make_plan(record, surface, diag)
        if cut["histogram_complete"]:
            continue
        assert cut["estimate"]["ceiling"] >= full["estimate"]["elements"]
        assert cut["estimate"]["elements"] <= cut["estimate"]["ceiling"]


def test_the_cube_budget_never_coarsens_a_plan_far_under_the_limit(tmp_path, monkeypatch):
    """the hunt's class: the octree stops on its cubes, the plan stands at the rules' sizes"""
    ws = _stage(fx.plate_with_hole(), tmp_path / "budget")
    record, surface, diag = FD.measure_staged(ws)
    whole = FD.choose_plan(record, surface, diag)
    monkeypatch.setattr(SF, "MAX_CUBES", 2_000)
    cut = FD.choose_plan(record, surface, diag)
    assert not cut["histogram_complete"]
    assert cut["fits"] and not cut.get("coarsened") and not cut.get("resolution_short")
    assert [z["size_m"] for z in cut["zones"]] == [z["size_m"] for z in whole["zones"]]
