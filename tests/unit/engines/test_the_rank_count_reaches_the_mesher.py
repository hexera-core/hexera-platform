# Responsibility: Verify the conjugate engine's MPI rank count is decided once, reaches BOTH size
# gates and the written plan, and never lifts a part that fits on one process.
# Boundaries: no mesher. The gates and ranks_for are pure functions; the wiring is read from source
# because the alternative is driving four geometries through a whole driver.
"""THE CEILING IS PER PROCESS, AND NOTHING TOLD THE PLANNER HOW MANY THERE WOULD BE.

cfMesh addresses its octree boxes with 32-bit labels and the list that overflows is per process,
so 178,956,970 cells is a one-process limit and ranks multiply it. `octree_ceiling_cells(ranks)`
has always known that. Three things did not:

* `plan_two_scales` took a `ranks` argument that no caller passed, so it always sized for one.
* `refuse_for_octree` took no `ranks` argument AT ALL, so even a correctly sized two-rank plan was
  declined seventy lines later against a one-process ceiling - and the refusal it wrote said "on
  one process" about a run that was never going to be one process.
* `ConjugatePlan.ranks` existed, defaulted to 1, and had no writer anywhere.

Measured on the first real customer cold plate (ColdPlateAssembly_CLEAN.step, 178 x 41.8 x 101 mm,
broad passage 2.11 mm, microchannels 0.0982 mm) at a 400,000,000 cell limit: refused at one rank,
and at two it plans 333,869,285 cells (267,095,428 unmargined). The engine's own refusal already
said so - "a configuration that fits the cell limit was refused on the octree ceiling alone and
would build on 2 ranks".

WHAT MUST NOT CHANGE, and these tests exist mostly for this. Parallel changes the mesh: on the
coupon, serial 29,812/29,262 cells against parallel 29,813/29,261 - one cell moves between the
regions, because topoSet classifies a cell on a processor boundary differently. So a part that fits
on one process must keep being built on one process, or every gate figure already measured quietly
means something else.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import driver as D
from meshpipeline.engines.determinism import CONJUGATE_RANKS_MAX, conjugate_ranks

#: The customer plate, as the committed two-scale fixture states it.
LO = (0.0, 0.0, 0.0)
HI = (0.1775, 0.04184, 0.1010)
SCALES = {"broad": {"width_m": 0.0021123723, "area_m2": 0.0388135},
          "fine": {"width_m": 0.0000982, "area_m2": 0.02171267}}
LIMIT = 400_000_000


def _plan(ranks: int):
    return B.plan_two_scales(block_lo=LO, block_hi=HI, scales=SCALES, limit=LIMIT,
                             asked_across=4, prismatic_axis=1, ranks=ranks)


# #
# the gates
# #
def test_the_octree_gate_takes_a_rank_count_at_all():
    """It took none, which is why a two-rank plan was declined against a one-rank ceiling."""
    assert "ranks" in inspect.signature(B.refuse_for_octree).parameters


def test_the_ceiling_a_refusal_quotes_is_the_one_it_judged_against():
    """A refusal that names 178,956,970 while judging a two-process run is a lie about the run."""
    said = B.refuse_for_octree(10 ** 12, ranks=2)
    assert f"{B.octree_ceiling_cells(2):,}" in said
    assert f"{B.octree_ceiling_cells(1):,}" not in said
    assert "across 2 processes" in said and "on one process" not in said


def test_the_octree_gate_lets_through_what_two_processes_can_address():
    one = B.octree_ceiling_cells(1)
    assert B.refuse_for_octree(one + 1), "one process cannot address this and must say so"
    assert B.refuse_for_octree(one + 1, ranks=2) == "", "two processes can, and must not refuse"


# #
# the plate
# #
def test_the_plate_is_refused_on_one_process_and_planned_on_two():
    refused, why = _plan(1)
    assert refused is None, "one process should not fit this plate"
    assert "would build on 2 ranks" in why, "the refusal should say what it would have taken"
    plan, said = _plan(2)
    assert plan is not None, "two processes should fit it: %s" % why
    assert plan["estimate"] <= LIMIT
    assert said and "two cell sizes" in said


def test_the_plan_carries_the_unmargined_count_for_sizing_the_decomposition():
    """ESTIMATE_MARGIN keeps a run inside a budget; the label ceiling is a hard limit on the cells
    that will actually exist, so padding it would ask for a rank the mesh does not need."""
    plan, _ = _plan(2)
    assert "raw" in plan
    assert plan["raw"] < plan["estimate"]
    assert abs(plan["estimate"] / plan["raw"] - B.ESTIMATE_MARGIN) < 1e-6


def test_the_demand_is_two_and_is_read_off_the_raw_count():
    plan, _ = _plan(2)
    assert C.ranks_for(plan["raw"], available=2) == 2


# #
# what must not move
# #
def test_a_mesh_that_fits_one_process_stays_on_one_process():
    """However high the ceiling is set. This is what keeps every measured part's gates meaningful."""
    for cells in (1, 41_616, 8_000_000, C.OCTREE_BOXES_PER_PROCESS):
        assert C.ranks_for(cells, available=8) == 1, "%d cells does not need a second process" % cells


def test_the_pinned_default_is_one_process():
    ranks, basis = conjugate_ranks()
    assert ranks == 1, "the default must not change any existing run"
    assert "CONJUGATE_RANKS" in basis


def test_the_pinned_count_is_capped_at_the_smallest_route():
    """Every route has at least 8 cores, and the sizing happens before the machine is chosen."""
    assert CONJUGATE_RANKS_MAX == 8


# #
# the wiring
# #
def _driver_source() -> str:
    return Path(inspect.getsourcefile(D)).read_text(encoding="utf-8")


def test_the_count_is_decided_once_and_reaches_both_gates():
    src = _driver_source()
    assert src.count("det.conjugate_ranks()") == 1, "the count must be decided in one place"
    assert "ranks=ranks)" in src
    assert src.count("ranks=ranks)") >= 2, "the count reaches only one of the two size gates"


def test_the_count_reaches_the_written_plan():
    """`ConjugatePlan.ranks` had no writer at all, so the mesher was handed 1 forever. native.py
    reads `plan.ranks` and passes it to `build`, so the plan is the only way it travels."""
    src = _driver_source()
    assert "dataclasses.replace(plan, ranks=" in src, "nothing writes the plan's rank count"
    assert "C.ranks_for(" in src, "the plan's count must come from the mesh size, not the ceiling"


def test_the_rank_count_is_stamped_because_it_changes_the_mesh():
    """One cell moves between the regions at two ranks, so two runs at different counts are two
    different meshes and the stamp has to say which."""
    native = Path(inspect.getsourcefile(__import__(
        "meshpipeline.engines.cfmesh_conjugate.native", fromlist=["x"]))).read_text(encoding="utf-8")
    assert 'note_setting("conjugate_ranks"' in native
