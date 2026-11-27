# Responsibility: take the real plate's own layer log through the shipped producers into the one
# reader the viewer's panel is built from, so the hand-built block in tests/unit/render cannot
# drift away from what finalize actually publishes.
# Boundaries: layers.read_layer_log/shortfalls, finalize._layers_published/_near_wall and
# render/mesh_facts.quality. No meshing, no case directory, no rendering.
"""THE LAST HOP, AND IT WAS THE ONE NOBODY LOOKED AT.

Round 4 fixed the manifest: `finalize` stopped publishing the plan's ask as `near_wall_layers` and
started publishing the 2.25 the mesh has, with the ask, the measured flag, eleven layer figures,
the coverage curve and a three-line shortfall beside it. Measured 2026-11-17, that whole block
reached `render/mesh_facts.quality` - the one reader the "Delivered mesh" panel is built from - and
came out as

    {"near_wall_layers": 2, "near_wall_levels": 3, "near_wall_cell_m": 2.5e-05}

Fifteen keys dropped, and the one that survived was coerced to 2 by `_as_int`. The user had been
quoted 3, the request; they would have been quoted 2, which is neither the request nor the
achievement, with nothing on the page to show that anything fell short.

WHY THIS FILE LIVES UNDER tests/unit/engines. The figures are REAL PLATE - job 0ff6f9e5,
2026-11-16 - and they come from one place: `REAL_LOG` in
`test_a_layer_pass_on_the_native_path_is_published.py`, transcribed from that run's own preserved
`snappyHexMesh.log`. Copying those thirty lines into tests/unit/render to keep this test beside the
module it exercises would give the measurement two spellings that could disagree, which is the
failure this engine has already paid for once. So the fixture stays where it is and the test comes
to it.

    extrusion, iteration 0               97.2267%
    ...ten repair iterations, every one DOWN
    extrusion, iteration 9               88.5826%     against COVERAGE_FLOOR_PCT 92.0
    fluid_to_solid, layers in the mesh   2.25 of 3    LAYERS_FLOOR_FRACTION 0.75 x 3 = 2.25 exactly
    fluid_to_solid, thickness achieved   71.9%        against THICKNESS_FLOOR_PCT 75.0
    faces in error                       8,916 over 37,247,044 cells added = 239 per million
                                                      against ILLEGAL_PER_MILLION_CEILING 200.0

WHAT IS NOT MEASURED HERE. `PLAN` below is SYNTHETIC: the plate's background cell size and level
count are not in its layer log, and this file asserts on neither - they exist only so
`finalize._near_wall` has a plan to read. Nothing in this file is a reading of a mesh interior or
of a rendered picture; it is a reading of a log and of two dictionaries.
"""
from __future__ import annotations

import pytest
from test_a_layer_pass_on_the_native_path_is_published import GOOD_LOG, REAL_LOG

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import layers as LY
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID
from meshpipeline.render.mesh_facts import quality

#: SYNTHETIC, and asserted on nowhere: a plan for `_near_wall` to read a background cell and a
#: level count out of. The plate's own two are not in its layer log.
PLAN: dict = {"background_cell_m": 2.0e-04, "levels": 3, "layers": 3}

#: The four names the reader leaves behind ON PURPOSE: the achievement and the ask under the one
#: further spelling each that something in the engine still reads (`_near_wall` takes the figure
#: out of `layers_in_mesh`, `_said` prints both), the pass's seconds (a cost, not a reading of the
#: mesh) and the ten-point coverage curve (a series, where the panel has one value to a row).
#:
#: THERE WERE FIVE. `near_wall_layers_in_mesh` was a THIRD spelling of 2.25, published for one
#: stated reason - to carry the figure "under a key nothing coerces" while `mesh_facts._as_int`
#: was turning `near_wall_layers` into 2. That cast is gone (the key is in `_FLOAT_METRICS`), so
#: the duplicate was guarding a coercion that no longer happens, and a grep of src/, ui/ and apps/
#: found no reader of it at all. `finalize._near_wall` stopped publishing it on 2026-11-17.
#: tests/unit/render still feeds it to `mesh_facts.quality` on purpose: every manifest written
#: before that date carries it, and the reader has to go on dropping it.
LEFT_BEHIND: list[str] = ["layer_coverage_by_iteration", "layer_seconds", "layers_asked",
                          "layers_in_mesh"]


def _published(log: str | None, *, asked: int = 3) -> dict:
    """The manifest quality block for a layer pass, built by the shipped producers.

    `conjugate.build` reads the log and writes the shortfalls it computed - against the pass's own
    `cells_added`, which no other artifact holds - back onto the same dict. `finalize.quality_of`
    then does exactly `q.update(_layers_published(raw))` followed by
    `q.update(_near_wall(plan, grown))`. This is that pair, without a case directory.
    """
    q: dict = {"cells": 102_536_327}
    grown: dict = {}
    if log is not None:
        raw = LY.read_layer_log(log)
        cells = int(raw.get("cells_added") or 0)
        assert not LY.complain(raw, interface_patch=FLUID_TO_SOLID, n_layers=asked, cells=cells), (
            "this fixture is a pass that GREW something; complain is for the ones that grew none")
        raw["shortfalls"] = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID,
                                               n_layers=asked, cells=cells))
        grown = F._layers_published(raw)
        q.update(grown)
    q.update(F._near_wall({**PLAN, "layers": asked}, grown))
    return q


def _payload(log: str | None, *, asked: int = 3) -> dict:
    """...and what the viewer's reader hands on from it."""
    q = _published(log, asked=asked)
    return quality({"mesh_mode": "cfmesh_conjugate", "mesh_units": "m",
                    "cell_count": q["cells"], "quality": q})


def test_the_producers_still_put_the_plates_own_figure_in_the_manifest():
    """The premise of everything below, re-measured from the log rather than assumed."""
    q = _published(REAL_LOG)
    assert q["near_wall_layers"] == pytest.approx(2.25)
    assert q["near_wall_layers_asked"] == 3
    assert q["near_wall_layers_measured"] is True
    assert q["layer_coverage_pct"] == pytest.approx(88.5826)
    assert q["layer_thickness_pct"] == pytest.approx(71.9)
    assert q["layer_faces_in_error"] == 8916 and q["layer_cells_added"] == 37_247_044
    assert len(q["layer_short"]) == 3


def test_the_reader_hands_the_plates_own_figure_to_the_viewer():
    p = _payload(REAL_LOG)
    assert p.get("near_wall_layers") == pytest.approx(2.25), (
        "the figure the viewer is given is not the one the mesh has: the plate grew 2.25 layers of "
        f"the 3 it asked for and the payload says {p.get('near_wall_layers')!r}")
    assert p.get("near_wall_layers_asked") == 3, (
        "and the ask is gone from the payload, so nothing a reader can see says the run was short")
    assert p.get("near_wall_layers_measured") is True


def test_the_reader_carries_everything_the_four_floors_are_judged_on():
    p = _payload(REAL_LOG)
    missing = [k for k in ("layer_coverage_pct", "layer_thickness_pct", "layer_faces_in_error",
                           "layer_cells_added", "near_wall_layers", "near_wall_layers_asked")
               if k not in p]
    assert not missing, (
        f"the plate missed three of this engine's four floors and the payload carries none of the "
        f"numbers those floors are drawn on: {missing}")
    # the figure, published; the floor it used to be compared against is gone - the bar is on
    # what the pass KEPT of what it was offered now (layers.KEPT_OF_OFFERED_FLOOR_PCT)
    assert p["layer_coverage_pct"] == 88.5826
    assert p["layer_thickness_pct"] < LY.THICKNESS_FLOOR_PCT
    assert round(p["layer_faces_in_error"] * 1e6 / p["layer_cells_added"]) == 239
    floor = LY.LAYERS_FLOOR_FRACTION * p["near_wall_layers_asked"]
    assert p["near_wall_layers"] == pytest.approx(floor) and not p["near_wall_layers"] < floor, (
        "the fourth floor was met at the boundary - 2.25 is 0.75 x 3 to the digit and the rule is "
        "a strict less-than - and the payload has to be able to show that too")


def test_the_three_lines_the_plate_was_short_by_reach_the_reader():
    short = _payload(REAL_LOG).get("layer_short")
    assert short and len(short) == 3, (
        "the shortfall the engine already computed stops at the viewer's reader, so the one place "
        f"a customer looks shows a layer count with no verdict on it: {short!r}")
    joined = " ".join(short)
    # no coverage sentence: this log predates the offered/kept instrumentation, so the floor
    # that replaced the coverage one cannot be evaluated on it and must not read as a miss
    assert "of the interface against" not in joined, joined
    assert "71.9%" in joined and f"{LY.THICKNESS_FLOOR_PCT:g}%" in joined
    assert "239 per million" in joined and f"{LY.ILLEGAL_PER_MILLION_CEILING:g}" in joined
    assert _payload(REAL_LOG).get("layer_patch") == FLUID_TO_SOLID


def test_a_pass_that_met_every_floor_reaches_the_reader_as_an_empty_list():
    """The hand-built run of 2026-11-08. EMPTY IS A JUDGEMENT and has to survive the reader, or a
    pass that met every floor is published identically to one nobody judged."""
    p = _payload(GOOD_LOG)
    assert p.get("layer_short") == [], (
        f"an empty shortfall does not survive the reader: {p.get('layer_short')!r}")
    assert p.get("near_wall_layers") == pytest.approx(2.84)
    assert p["layer_coverage_pct"] > 0, "layers grew somewhere, the only bar left on it"
    assert p["layer_thickness_pct"] > LY.THICKNESS_FLOOR_PCT


def test_a_run_with_no_layer_pass_reaches_the_reader_as_an_unmeasured_zero():
    """`layers: 0` and no pass - every conjugate mesh this engine delivered before 2026-11-16."""
    p = _payload(None, asked=0)
    assert p.get("near_wall_layers") == 0 and p.get("near_wall_layers_asked") == 0
    assert p.get("near_wall_layers_measured") is False, (
        "a zero with nothing beside it reads as a measurement of a pass that grew nothing, where "
        "this run asked for nothing and measured nothing")
    assert "layer_short" not in p, "an empty list here would claim a pass was judged"
    for k in ("layer_coverage_pct", "layer_thickness_pct", "layer_faces_in_error",
              "layer_cells_added", "layer_patch"):
        assert k not in p, f"{k} was invented for a run with no layer pass: {p.get(k)!r}"


def test_a_run_whose_log_could_not_be_read_reaches_the_reader_as_an_unmeasured_three():
    """The case that matters most, because its `near_wall_layers` IS the ask - the exact figure
    this whole change exists to stop presenting as a result."""
    p = _payload(None, asked=3)
    assert p.get("near_wall_layers") == 3 and p.get("near_wall_layers_asked") == 3
    assert p.get("near_wall_layers_measured") is False, (
        "the payload is identical to a mesh that grew its three layers except for this flag, so "
        "without it the reader publishes the request as the achievement all over again")
    assert "layer_short" not in p


def test_every_layer_figure_the_reader_leaves_behind_is_one_it_leaves_behind_on_purpose():
    """THE GUARD AGAINST THIS HAPPENING AGAIN. The defect was not a wrong number, it was fifteen
    figures that reached the manifest and stopped. Any figure finalize starts publishing has to be
    either carried or named here."""
    q = _published(REAL_LOG)
    p = _payload(REAL_LOG)
    left = sorted(k for k in q if ("layer" in k or "wall" in k) and k not in p)
    assert left == LEFT_BEHIND, (
        "a layer figure the manifest publishes stops at the viewer's reader with nothing saying "
        f"why. The five left behind on purpose are {LEFT_BEHIND}; this run leaves {left}")
