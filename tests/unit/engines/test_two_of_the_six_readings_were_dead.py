# Responsibility: Hold the two customer readings that were published wrong - the poor-cell sets,
# always empty, and the near-wall resolution, eight times too coarse on a two-scale part.
# Boundaries: cfmesh_conjugate/finalize.quality_of and native._as_dict's plan block; no meshing.
"""TWO OF THE SIX READINGS THE FIRST CUSTOMER ASKED FOR WERE PUBLISHED WRONG.

Both were one missing key, both had been wrong on every run including production, and both are on
the list Corintis asked for on 2026-10-29.

THE POOR-CELL SETS WERE ALWAYS EMPTY. `quality_of` builds `rows` with its keys named explicitly
and `poor_sets` was not among them; 120 lines later `q["poor_cell_sets"]` reads `poor_sets` off
those same rows. The data existed the whole time one dict away: native._as_dict writes it onto the
region with the comment "WHERE the poor cells are, beside how many of them there are". Worse, the
DRAWABLE half never went through here at all - viewer._poor_surfaces reads poor_cells/<region>/*.vtp
straight off the disk - so the highlight drew while the manifest said there was nothing to
highlight. A reader who checked the number was told their mesh was clean.

THE NEAR-WALL READING WAS EIGHT TIMES TOO COARSE. `finalize._near_wall` takes the deepest level of
`plan["fine_surfaces"]` over the coolant's own, and says why: "the thin passages get more levels
than the coolant as a whole, so the coolant's own level count understates the resolution at the
only wall that was hard to resolve". `native._as_dict`'s plan block never carried `fine_surfaces`,
so that loop never ran.

MEASURED on the first customer plate, whose fitted plan is levels 1 with a fine_surfaces entry at
levels 4 over the 98 um microchannels, background cell 0.3521 mm:

    published   near_wall_levels 1, near_wall_cell_m 176 um
    actual      near_wall_levels 4, near_wall_cell_m  22 um

Wrong in the direction that matters: 176 um against a 98 um channel reads as a mesh that cannot
resolve the thing it does resolve.
"""
from __future__ import annotations

import inspect

import json

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION

# the delivered two-region case and the run's own report, as the other conjugate tests build them
from test_conjugate_row import REPORT, case  # noqa: F401

#: their plate's own numbers, from ws_fitted/conjugate_plan.json
BG = 0.0003520562041666667
FINE = [{"file": "constant/triSurface/fluid_fine.stl", "levels": 4, "name": "microchannels",
         "thickness_m": 8.801405104166668e-05}]


def test_the_near_wall_reading_takes_the_deepest_refinement():
    """The whole point: on a two-scale part the wall is inside the fine surface, so its levels win."""
    got = F._near_wall({"background_cell_m": BG, "levels": 1, "fine_surfaces": FINE})
    assert got["near_wall_levels"] == 4, got
    assert abs(got["near_wall_cell_m"] - BG / 16.0) < 1e-12, got
    # and the number a reader sees, in microns, against this part's 98 um channels
    assert 20e-6 < got["near_wall_cell_m"] < 24e-6, got["near_wall_cell_m"]


def test_without_fine_surfaces_it_is_the_coolants_own_levels():
    """A part with no fine surface is unchanged: this never invented a level it was not given."""
    got = F._near_wall({"background_cell_m": BG, "levels": 3})
    assert got["near_wall_levels"] == 3
    assert abs(got["near_wall_cell_m"] - BG / 8.0) < 1e-12


def test_the_plan_the_report_publishes_carries_the_fine_surfaces():
    """_near_wall is fed report["plan"], and that block is built in native._as_dict. If it does not
    carry fine_surfaces the reading above cannot possibly be right, however correct _near_wall is."""
    src = inspect.getsource(N)
    i = src.find('"plan": {')
    assert i != -1, "the plan block moved; this test is anchored on it"
    block = src[i:i + 2600]
    assert '"fine_surfaces"' in block, (
        "the published plan block does not carry fine_surfaces, so the near-wall reading falls "
        "back to the coolant's coarser levels")


def test_the_poor_cell_sets_reach_the_rows_the_reading_is_built_from():
    """`q["poor_cell_sets"]` reads `poor_sets` off the rows quality_of builds. If the rows do not
    carry it the reading is [] whatever checkMesh found."""
    src = inspect.getsource(F.quality_of)
    assert '"poor_sets"' in src, (
        "quality_of's rows no longer carry poor_sets, so q['poor_cell_sets'] is empty again")


def _report_with_poor_sets(fluid_faces: int, solid_faces: int) -> dict:
    """The run's own report with poor sets on both regions, in the shape native._as_dict writes:
    `poor_sets` hangs off each region, beside its counts."""
    r = json.loads(json.dumps(REPORT))
    r["regions"][FLUID_REGION]["poor_sets"] = [{"name": "nonOrthoFaces", "faces": fluid_faces}]
    r["regions"][SOLID_REGION]["poor_sets"] = [{"name": "nonOrthoFaces", "faces": solid_faces}]
    r.setdefault("plan", {}).update({"background_cell_m": BG, "levels": 1, "fine_surfaces": FINE})
    return r


def test_a_region_with_poor_sets_publishes_them(case):
    """End to end through quality_of, with the proportion this part actually shows: on the 336M
    plate the metal held 27,932 of the 28,132 poor cells, and a reader needs to see which half."""
    q = F.quality_of(case, _report_with_poor_sets(200, 27932))
    sets = q.get("poor_cell_sets") or []
    assert sets, "a report with poor sets on both regions published none"
    by_region = {s["region"]: s for s in sets}
    assert set(by_region) == {FLUID_REGION, SOLID_REGION}, by_region
    assert by_region[SOLID_REGION]["faces"] == 27932, by_region[SOLID_REGION]
    assert by_region[SOLID_REGION]["faces"] > by_region[FLUID_REGION]["faces"]


def test_the_near_wall_reading_travels_with_it(case):
    """The other dead reading, through the same call: the plan's fine surface is where the wall is."""
    q = F.quality_of(case, _report_with_poor_sets(1, 1))
    assert q.get("near_wall_levels") == 4, q.get("near_wall_levels")
    assert abs(q.get("near_wall_cell_m") - BG / 16.0) < 1e-12, q.get("near_wall_cell_m")


def test_no_poor_sets_is_still_an_empty_list_and_not_a_crash(case):
    """A mesh that passes writes no sets at all, which is correct and must stay quiet."""
    q = F.quality_of(case, REPORT)
    assert q.get("poor_cell_sets") == []
