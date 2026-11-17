# Responsibility: The drawing budget is shared so no patch is starved by one that cannot be drawn
# well at any share.
"""WHAT PROPORTIONAL SHARING COST.

Job 0ff6f9e5 delivered a 283,410,955-cell conjugate cold plate. Its boundary is 17,070,364 faces
and 16,582,198 of them - 97.1% - are the fluid/solid interface, every microchannel wall. Shared in
proportion to face count, the viewer drew:

    fluid/fluid_to_solid   16,582,198 faces ->  474,134 triangles, merged at 0.406 mm
    solid/wall_outer          485,526 faces ->   16,426 triangles, merged at 2.038 mm
    fluid/mouth_2               1,306 faces ->       62 triangles, merged at 1.252 mm

The part's walls are 0.500 mm. Clustering the outer skin onto a grid 4.1x coarser than the wall
collapses vertices THROUGH it, and the preview came out torn and speckled over a mesh that is
sound. Meanwhile the interface bought almost nothing for the 97% it took: resolution goes as the
square root of the count, so 30x the triangles is 5.5x the detail, and it is unreadable either way
against a 98.2 um channel.
"""
from __future__ import annotations

from meshpipeline.render.polymesh_budget import MIN_TRIANGLES_PER_PATCH, _share_out

BUDGET = 800_000

#: the plate's own four patches, as triangle demands (two per quad face)
PLATE = {
    "fluid/fluid_to_solid": 16_582_198 * 2,
    "fluid/mouth_2": 1_306 * 2,
    "fluid/mouth_3": 1_334 * 2,
    "solid/wall_outer": 485_526 * 2,
}


def _patches(demand: dict[str, int]) -> list[dict]:
    # face_count is what _wants falls back to for a patch it cannot decode, which is every patch
    # here: these carry no points or polys, only the demand under test.
    return [{"name": n, "face_count": t} for n, t in demand.items()]


def _alloc(demand: dict[str, int], budget: int = BUDGET) -> dict[str, int]:
    return dict(zip(demand, _share_out(_patches(demand), budget)))


def test_the_outer_skin_is_not_starved_by_the_interface():
    got = _alloc(PLATE)
    # proportional gave the skin 22,754 of 800,000. It must now get a share of the same order as
    # the interface, because it is the surface a reader is deciding about.
    assert got["solid/wall_outer"] > 300_000, (
        f"the outer skin got {got['solid/wall_outer']:,} triangles of {BUDGET:,}; proportional "
        "sharing gave it 22,754 and clustered it at 2.038 mm, four times the part's 0.500 mm wall")
    assert got["solid/wall_outer"] >= got["fluid/fluid_to_solid"] * 0.9


def test_a_patch_that_fits_is_given_exactly_what_it_needs():
    got = _alloc(PLATE)
    for small in ("fluid/mouth_2", "fluid/mouth_3"):
        assert got[small] == PLATE[small], (
            f"{small} needs {PLATE[small]:,} triangles and was given {got[small]:,}; proportional "
            "sharing floored it to 64 and drew a 1,306-face mouth at 1.25 mm")


def test_the_budget_is_not_exceeded():
    for budget in (1_000, 100_000, BUDGET, 4_000_000):
        got = _alloc(PLATE, budget)
        spent = sum(got.values())
        floor = MIN_TRIANGLES_PER_PATCH * len(PLATE)
        assert spent <= max(budget, floor), (
            f"budget {budget:,} but {spent:,} triangles were handed out")


def test_nothing_is_cut_below_the_floor():
    # a boundary that is not drawn reads to a user as a boundary that is not there
    got = _alloc({"huge": 50_000_000, "a": 10, "b": 10, "c": 10}, 1_000)
    for n, v in got.items():
        assert v >= MIN_TRIANGLES_PER_PATCH, f"{n} was cut to {v}"


def test_one_patch_takes_the_whole_budget():
    got = _alloc({"only": 10_000_000}, BUDGET)
    assert got["only"] == BUDGET


def test_a_surplus_is_handed_back_not_dropped():
    # three tiny and one huge: the huge one must receive what the tiny ones did not need
    demand = {"tiny_a": 100, "tiny_b": 100, "huge": 10_000_000}
    got = _alloc(demand, 10_000)
    assert got["tiny_a"] == 100 and got["tiny_b"] == 100
    assert got["huge"] == 10_000 - 200, (
        f"the huge patch got {got['huge']:,}; the 9,800 the tiny ones did not need was dropped")


def test_every_patch_is_offered_a_share_before_any_gets_seconds():
    # two equally impossible patches split it evenly rather than first-come
    got = _alloc({"a": 10_000_000, "b": 10_000_000}, BUDGET)
    assert abs(got["a"] - got["b"]) <= 1
    assert got["a"] + got["b"] <= BUDGET
