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
and at two it planned 333,869,285 cells (267,095,428 unmargined). The engine's own refusal already
said so - "a configuration that fits the cell limit was refused on the octree ceiling alone and
would build on 2 ranks".

THOSE TWO NUMBERS HAVE SINCE MOVED TWICE, and the entry is kept as written because it is what this
file was built on. The plan they describe is stretched 8x, and docs/semiconductor/MESH_TIME.md
later measured what a stretch costs in the optimiser - ws_shell at scaleY 6 took 38.7 h against
cbox2 at scaleY 3 in 652 s on 1.53x the cells. STRETCH_LADDER stopped at 4 for a while, and at 4
one process could take a coarser broad rung instead of being refused.

IT STOPS AT 3 NOW, and one process is REFUSED AGAIN, which puts this file back on the case it was
written for. 4 was always the rung MESH_TIME.md had already refuted ("7.5x slower for 7% fewer
cells"); it was kept because the ladder was built out of powers of two and 3 was never on it. The
first customer run through the whole product took 4 and the mesher segfaulted after 2,272 s, in
the octree projectSurfaceAfterBackScaling builds after the back-scale (job 0fb28a1b, 2026-11-13,
three identical attempts). At 3 the same plate at the same limit plans 338,720,238 cells
(270,976,191 unmargined) on two ranks, and one process has no rung at all: the coarsest fine cell
above the four-cell floor is 1.51x what it can address.

WHAT MUST NOT CHANGE, and these tests exist mostly for this. Parallel changes the mesh: on the
coupon, serial 29,812/29,262 cells against parallel 29,813/29,261 - one cell moves between the
regions, because topoSet classifies a cell on a processor boundary differently. So a part that fits
on one process must keep being built on one process, or every gate figure already measured quietly
means something else.
"""
from __future__ import annotations

import inspect

import pytest
from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import driver as D
from meshpipeline.engines import determinism as DET
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
def test_one_process_is_refused_and_two_get_the_rung_that_was_wanted():
    """The rank count is what buys this part a plan at all, which is the case this file is for.

    It briefly bought resolution instead - 4.09 across on one process, 4.46 on two - while
    STRETCH_LADDER stopped at 4. At 3 the one-process rung is gone: see the module docstring.
    """
    one, why = _plan(1)
    assert one is None, "one process cannot address any rung at a stretch that meshes: %s" % one
    assert "would build on 2 ranks" in why, why

    two, said = _plan(2)
    assert two is not None, "two processes should fit it: %s" % said
    assert two["fine_across"] == pytest.approx(4.46, abs=0.02)
    assert two["estimate"] <= LIMIT
    assert C.ranks_for(two["raw"]) == 2, "the plan has to be addressable by the ranks it names"
    assert said and "two cell sizes" in said


def test_no_rung_is_planned_at_a_stretch_that_cannot_mesh():
    """Pinned so the rungs that cost 38.7 h and a segfault cannot quietly come back."""
    for ranks in (1, 2, 4, 8):
        plan, _ = _plan(ranks)
        if plan is None:
            continue
        assert plan["stretch"] <= 3.0, (
            "%d ranks planned a stretch of %g, which has no finishing run under it"
            % (ranks, plan["stretch"]))


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
# the back-scale projection, which is a second demand on the same number
# #
def test_a_stretched_parallel_mesh_takes_the_rank_count_the_projection_survives():
    """TWO PROCESSES IS WHAT THE CEILING ASKS FOR AND IT IS NOT ENOUGH.

    With an anisotropic stretch in the dict cfMesh rebuilds its octree after the back-scale, in
    `projectSurfaceAfterBackScaling`, and on two processes that rebuild segfaults inside
    `meshOctreeCube::refineCube`. Measured twice on the customer plate, at two different stretches
    on two different machines - 302,810,924 cells on an n2-highmem-32 and 326,081,268 on an
    n2-highmem-64, both rc=139 at the same instruction. The same stage completes at eight ranks on
    the same part (`ws_shell`) and on the box domain (`cbox2`). See
    conjugate.BACKSCALE_PROJECTION_RANKS.
    """
    plan, _ = _plan(8)
    assert C.ranks_for(plan["raw"], available=8) == 2, (
        "the label ceiling alone still only asks for two")
    assert C.ranks_for(plan["raw"], available=8, back_scaled=True) == C.BACKSCALE_PROJECTION_RANKS


def test_the_floor_never_lifts_a_mesh_off_one_process():
    """A part inside one process's octree never runs -parallel, so there is no projection to
    survive - and lifting it would change a mesh that has already been measured."""
    for cells in (1, 41_616, 8_000_000, C.OCTREE_BOXES_PER_PROCESS):
        assert C.ranks_for(cells, available=8, back_scaled=True) == 1, cells


def test_the_floor_is_only_for_a_plan_that_carries_a_stretch():
    """Without anisotropicSources `projectSurfaceAfterBackScaling` returns on its first line, so
    an unstretched parallel mesh never reaches the code that crashes and must not pay for it."""
    cells = C.OCTREE_BOXES_PER_PROCESS * 2
    assert C.ranks_for(cells, available=8) == 2
    assert C.ranks_for(cells, available=8, back_scaled=False) == 2


def test_the_floor_cannot_ask_for_more_than_the_deployment_offers():
    """`available` still caps it: a deployment pinned lower gets what it pinned, and the log says
    which count it took."""
    cells = C.OCTREE_BOXES_PER_PROCESS * 2
    assert C.ranks_for(cells, available=4, back_scaled=True) == 4
    assert C.ranks_for(cells, available=2, back_scaled=True) == 2


def test_the_driver_asks_for_the_floor_when_the_plan_carries_a_stretch():
    """The floor is useless unless the one caller passes it. `stretch_zones` is the plan's own
    record of an anisotropic source, written a few lines above the call."""
    src = _driver_source()
    assert "back_scaled=" in src, "the driver never tells ranks_for the plan is stretched"
    assert "plan.stretch_zones" in src, (
        "the stretch has to come from the plan, not from a separate guess")


# #
# what must not move
# #
def test_a_mesh_that_fits_one_process_stays_on_one_process():
    """However high the ceiling is set. This is what keeps every measured part's gates meaningful."""
    for cells in (1, 41_616, 8_000_000, C.OCTREE_BOXES_PER_PROCESS):
        assert C.ranks_for(cells, available=8) == 1, "%d cells does not need a second process" % cells


def test_one_process_can_still_be_forced(monkeypatch):
    """Setting it to 1 is the way to make a part that needs ranks be refused instead of meshed,
    so it has to keep working.

    MONKEYPATCHED AND NOT READ BARE. The value is a module constant bound at import from the
    process environment (determinism.CONJUGATE_RANKS <- optional_env), and settings/env.py loads
    the repo's own .env into that environment - so a row that calls `conjugate_ranks()` bare is
    testing the deployment, not the code.
    """
    monkeypatch.setattr(DET, "CONJUGATE_RANKS", "1")
    ranks, basis = conjugate_ranks()
    assert ranks == 1
    assert "CONJUGATE_RANKS" in basis


def test_the_default_shipped_in_the_source_offers_the_whole_fleet():
    """IT WAS 1, and the reason given was that parallel changes the mesh. That is true and it is
    why `ranks_for` keeps small parts serial - which it does whatever this says, so a default of 1
    protected nothing and refused every part that needs a second process. The customer plate plans
    270,976,191 cells, 1.51x one process's ceiling, and the engine's own refusal already names the
    remedy it then would not take."""
    src = Path(inspect.getsourcefile(DET)).read_text(encoding="utf-8")
    assert 'CONJUGATE_RANKS: str = optional_env("CONJUGATE_RANKS", "8")' in src


def test_the_default_still_leaves_a_part_that_fits_one_process_on_one_process(monkeypatch):
    """The claim the default rests on, checked rather than asserted: offering eight changes
    nothing for a mesh the ceiling does not bite."""
    monkeypatch.setattr(DET, "CONJUGATE_RANKS", "8")
    offered, _ = conjugate_ranks()
    assert offered == 8
    for cells in (1, 41_616, 8_000_000, C.OCTREE_BOXES_PER_PROCESS):
        assert C.ranks_for(cells, available=offered) == 1


def test_a_configured_rank_count_is_honoured_and_capped(monkeypatch):
    """The other half: setting it is the supported lever, so it has to actually take effect."""
    monkeypatch.setattr(DET, "CONJUGATE_RANKS", "2")
    assert conjugate_ranks()[0] == 2
    monkeypatch.setattr(DET, "CONJUGATE_RANKS", "99")
    ranks, basis = conjugate_ranks()
    assert ranks == CONJUGATE_RANKS_MAX and "capped" in basis


def test_the_ceiling_reads_as_a_ceiling_and_not_as_the_count(monkeypatch):
    """The driver prints this basis beside the count the PLAN took, so a basis that reads like a
    choice contradicts the line it sits in. On the first customer run after the default moved:

        conjugate: meshing across 2 processes - pinned by CONJUGATE_RANKS=8

    which an operator reads as a run that ignored its own setting. It is the opposite - 8 is the
    most this offers and `ranks_for` asked for 2 - and the words now say so. It was not wrong
    while the default was 1, because then the two numbers always agreed.
    """
    monkeypatch.setattr(DET, "CONJUGATE_RANKS", "8")
    ranks, basis = conjugate_ranks()
    assert ranks == 8
    assert "pinned" not in basis, basis
    assert "at most 8" in basis and "CONJUGATE_RANKS" in basis, basis
    # the case the words have to survive: the plan takes fewer than the ceiling offers
    assert C.ranks_for(270_983_796, available=ranks) == 2


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
