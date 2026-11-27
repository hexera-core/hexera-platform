# Responsibility: hold the ONE user-facing reader of a delivered mesh's facts to publishing the
# layer count the mesh HAS, at the precision it was measured at, with the ask, the flag that says
# which of the two it is, and the shortfall beside it.
# Boundaries: render/mesh_facts.quality only. No meshing, no manifest assembly, no rendering.
"""THE READER PUBLISHED A THIRD NUMBER.

A run of this product asked for 3 near-wall layers and grew 2.25. For months the record published
3 - the REQUEST - as though it were the result, and that 3 was quoted to the customer as an
achievement. `cfmesh_conjugate/finalize` was fixed: the manifest now publishes
`near_wall_layers 2.25`, `near_wall_layers_asked 3`, `near_wall_layers_measured true` and fifteen
further layer and near-wall keys.

MEASURED 2026-11-17, handing that manifest block to `render/mesh_facts.quality`, which is the one
reader the viewer's "Delivered mesh" panel is built from:

    manifest : near_wall_layers 2.25, plus 15 other layer / near-wall keys
    payload  : {"near_wall_layers": 2, "near_wall_levels": 3, "near_wall_cell_m": 2.5e-05}

`_as_int(2.25)` is 2, because `near_wall_layers` sat in `_INT_METRICS`; and NONE of the other
fifteen keys was in either tuple, so the ask, the flag that says whether the figure is a
measurement at all, every figure this engine draws its own four floors on, and the three-line
shortfall all stopped here. The user had been quoted 3; they would now be quoted 2, which is
neither the request nor the achievement - and with the ask gone from the record, no reader could
see that anything had fallen short. A change whose whole purpose is to stop publishing an intention
as a measurement cannot ship while its one user-facing reader publishes a figure the mesh does not
have.

THE FOUR FLOORS ARE `cfmesh_conjugate/layers.py`'s OWN - KEPT_OF_OFFERED_FLOOR_PCT 95.0,
THICKNESS_FLOOR_PCT 75.0, LAYERS_FLOOR_FRACTION 0.75, ILLEGAL_PER_MILLION_CEILING 200.0 - and they
are imported here rather than restated, because this engine has already paid once for one quantity
with two readers that disagreed. The test that all four can be checked FROM THE PAYLOAD is what the
key list is for: a reader handed the coverage but not the thickness, or the faces in error but not
the cells they were counted over, cannot see three of the four.

WHAT IS SYNTHETIC HERE AND WHAT IS NOT. Every block below is a hand-built dict - SYNTHETIC - so
this file tests the reader and nothing else. The FIGURES in it are the real plate's (job 0ff6f9e5,
2026-11-16) and the hand-built run's (2026-11-08). The companion file
`tests/unit/engines/test_the_real_plates_layer_block_reaches_the_reader.py` runs the same questions
on a block built by the shipped producers from the plate's own `snappyHexMesh.log`, which is what
stops this fixture drifting away from what finalize publishes. Nothing here is a reading of a mesh.
"""
from __future__ import annotations

import pathlib

import pytest

from meshpipeline.engines.cfmesh_conjugate.layers import (
    ILLEGAL_PER_MILLION_CEILING,
    LAYERS_FLOOR_FRACTION,
    THICKNESS_FLOOR_PCT,
)
from meshpipeline.render.mesh_facts import quality

#: THE REAL PLATE'S LAYER BLOCK as finalize publishes it - job 0ff6f9e5, 2026-11-16. SYNTHETIC
#: dict, REAL PLATE figures: 2.25 of 3 layers over 88.5826% of a 16,582,198-face interface at 71.9%
#: of the asked thickness, 8,916 faces in error over the 37,247,044 cells the pass added.
#: `near_wall_layers_in_mesh` IS NOT IN HERE and must not come back: it was a second name for
#: 2.25 with no reader anywhere in the product, and `finalize` stopped publishing it the moment
#: the coercion it was guarding went away.
GREW: dict = {
    "near_wall_layers": 2.25,
    "near_wall_layers_asked": 3,
    "near_wall_layers_measured": True,
    # all four floors had a figure, and three of them were missed - which is the OTHER list
    "layer_floors_unjudged": [],
    "layer_floors_total": 4,
    "layer_coverage_pct": 88.5826,
    "layer_thickness_pct": 71.9,
    "layer_thickness_m": 1.09e-05,
    "layer_faces_in_error": 8916,
    "layer_cells_added": 37_247_044,
    "layer_iterations": 10,
    "layer_seconds": 2537.74,
    "layer_patch": "fluid_to_solid",
    "layers_asked": 3,
    "layers_in_mesh": 2.25,
    "layer_coverage_by_iteration": [97.2267, 90.2273, 89.6661, 89.2765, 89.1136,
                                    88.9878, 88.8744, 88.7604, 88.6609, 88.5826],
    "layer_short": [
        "covered 88.5826% of the interface against the 92% this engine aims at",
        "fluid_to_solid came back at 71.9% of the thickness it was asked for against 75%",
        "left 8,916 faces in error over 37,247,044 cells, 239 per million against 200",
    ],
}

#: A PASS THAT MET EVERY FLOOR: the hand-built run of 2026-11-08, from layers.py's own docstring.
#: Its shortfall list is EMPTY, which is a judgement, not an absence.
MET: dict = {
    "near_wall_layers": 2.84, "near_wall_layers_asked": 3,
    "near_wall_layers_measured": True, "layer_coverage_pct": 95.553, "layer_thickness_pct": 94.6,
    "layer_thickness_m": 1.43e-05, "layer_faces_in_error": 2888, "layer_cells_added": 47_232_886,
    "layer_iterations": 1, "layer_patch": "fluid_to_solid", "layer_short": [],
    "layer_floors_unjudged": [], "layer_floors_total": 4,
}

#: ...AND THE SAME PASS WITH ONLY PART OF ITS LOG READ. One floor had a figure and it was met, so
#: `layer_short` is `[]` again - the one value a reader is entitled to read as a clean pass - and
#: the only thing separating this payload from MET above is the list of floors nobody judged.
PART_READ: dict = {
    "near_wall_layers": 3, "near_wall_layers_asked": 3, "near_wall_layers_measured": False,
    "layer_coverage_pct": 97.2267, "layer_cells_added": 49_746_594, "layer_iterations": 1,
    "layer_short": [],
    "layer_floors_unjudged": ["thickness", "layers kept", "faces in error"],
    "layer_floors_total": 4,
}

#: NO PASS RAN - `layers: 0` in the plan, which is every conjugate mesh this engine delivered
#: before 2026-11-16. `finalize._near_wall` puts the ASK under `near_wall_layers` and flags it
#: unmeasured, and no layer figure exists to publish.
NONE_ASKED: dict = {"near_wall_layers": 0, "near_wall_layers_asked": 0,
                    "near_wall_layers_measured": False}

#: THE LOG COULD NOT BE READ. Three layers were asked for and nothing measured what grew, so
#: `finalize._near_wall` publishes the identical shape with the ASK standing in. This is the one
#: case where `near_wall_layers` is an intention, and the flag is the only thing that says so.
UNREAD: dict = {"near_wall_layers": 3, "near_wall_layers_asked": 3,
                "near_wall_layers_measured": False}

#: Every key this reader is expected to carry out of a judged layer pass.
CARRIED: tuple[str, ...] = (
    "near_wall_layers", "near_wall_layers_asked", "near_wall_layers_measured",
    "layer_coverage_pct", "layer_thickness_pct", "layer_thickness_m",
    "layer_faces_in_error", "layer_cells_added", "layer_iterations",
    "layer_patch", "layer_short",
    # ...and how many of this engine's four floors that shortfall list is a verdict on
    "layer_floors_unjudged", "layer_floors_total",
)


def _manifest(**quality_keys) -> dict:
    """A delivered conjugate manifest carrying `quality_keys`. SYNTHETIC. The three figures it
    always has are the near-wall resolution that travels beside a layer count on every run."""
    q: dict = {"cells": 102_536_327, "near_wall_levels": 4, "near_wall_cell_m": 2.51578e-05}
    q.update(quality_keys)
    return {"mesh_mode": "cfmesh_conjugate", "mesh_units": "m",
            "cell_count": q["cells"], "quality": q}


# #
# the figure itself
# #
def test_the_figure_the_reader_publishes_is_the_one_the_mesh_grew():
    """2.25 must survive as 2.25. `_as_int` made it 2 - a number that is neither the 3 that was
    asked for nor the 2.25 that grew, published into the one panel a customer reads."""
    p = quality(_manifest(**GREW))
    assert p.get("near_wall_layers") == pytest.approx(2.25), (
        "the reader publishes a layer count the mesh does not have: the ask was 3, the mesh grew "
        f"2.25, and the payload says {p.get('near_wall_layers')!r}")


def test_the_ask_travels_with_it_or_no_reader_can_see_a_shortfall():
    """A reader given only 2.25 cannot tell that anything fell short, which is the mirror of the
    defect that published 3 alone."""
    p = quality(_manifest(**GREW))
    assert p.get("near_wall_layers_asked") == 3, (
        "the ask is not in the payload, so a reader sees a layer count with nothing to compare it "
        "against and cannot see that the run came back under what was asked for")
    assert p["near_wall_layers"] < p["near_wall_layers_asked"]


def test_the_payload_says_whether_the_count_is_a_measurement_or_an_intention():
    """3 asked and 3 grown are the same number and only one of them is evidence."""
    assert quality(_manifest(**GREW)).get("near_wall_layers_measured") is True, (
        "nothing in the payload says that 2.25 was measured, so a reader cannot tell this figure "
        "from the plan's ask standing in for one")
    assert quality(_manifest(**UNREAD)).get("near_wall_layers_measured") is False, (
        "a run whose layer log could not be read publishes the ASK as the count; without the flag "
        "the payload presents an intention as a measurement, which is the whole defect")


def test_which_surface_the_layers_are_on_reaches_the_reader():
    p = quality(_manifest(**GREW))
    assert p.get("layer_patch") == "fluid_to_solid", (
        "a layer figure with no patch beside it could be about any boundary in the case; on a "
        "conjugate mesh the layers are on the coupling and that is the patch the mesh exists for")
    assert "layer_patch" not in quality(_manifest(layer_patch="   ")), (
        "a blank name is not an answer to which surface")


# #
# the floors
# #
def test_all_four_of_this_engines_layer_floors_can_be_checked_from_the_payload_alone():
    """The point of the key list. Three of the four were missed on the real plate and the payload
    carried none of the numbers any of them is drawn on."""
    p = quality(_manifest(**GREW))
    missing = [k for k in ("layer_coverage_pct", "layer_thickness_pct", "layer_faces_in_error",
                           "layer_cells_added", "near_wall_layers", "near_wall_layers_asked")
               if k not in p]
    assert not missing, (
        "the payload cannot be checked against this engine's own floors - a reader would have to "
        f"open the manifest to find out whether the run met them: {missing}")
    # COVERAGE IS PUBLISHED AND IS NO LONGER A BAR, which is this engine's own finding rather
    # than a loosening. Coverage is two things added: faces `featureAngle` declined to offer,
    # which is the part's shape, and faces offered and then lost, which is the mover. Job
    # b8f09ae9 measured the split - 91.1145% offered, 99.5641% of that kept - so of 9.28
    # missing points 8.89 were never the pass's to grow. The bar is on what it KEPT now; the
    # figure still has to reach the reader, which is what this file is about.
    assert p["layer_coverage_pct"] == 88.5826, "the measured coverage still reaches the reader"
    assert p["layer_thickness_pct"] < THICKNESS_FLOOR_PCT, "thickness, 71.9% against 75.0"
    per_million = p["layer_faces_in_error"] * 1e6 / p["layer_cells_added"]
    assert round(per_million) == 239 and per_million > ILLEGAL_PER_MILLION_CEILING, (
        "faces in error per million cells added is the fourth floor and it takes BOTH counts; "
        f"got {per_million!r}")
    # ...and the one floor the plate MET, exactly at the boundary: 2.25 is 0.75 x 3 to the digit
    # and `layers.shortfalls` compares with a strict `<`, so this is a pass and not a miss.
    floor = LAYERS_FLOOR_FRACTION * p["near_wall_layers_asked"]
    assert p["near_wall_layers"] == pytest.approx(floor)
    assert not p["near_wall_layers"] < floor, (
        "2.25 is LAYERS_FLOOR_FRACTION x 3 exactly, so the layers floor is met at the boundary")


def test_the_shortfall_itself_reaches_the_reader():
    """The figures let a reader derive the misses; these three lines say them."""
    short = quality(_manifest(**GREW)).get("layer_short")
    assert short and len(short) == 3, (
        "the lines that say which of this engine's own floors the run missed do not reach the "
        f"viewer's reader at all, so the panel shows a layer count and no verdict: {short!r}")
    joined = " ".join(short)
    # The floors this payload actually trips. It predates the offered/kept instrumentation so it
    # carries no `layer_kept_of_offered_pct`, and an unmeasured advisory figure must not read as
    # a miss - which is pinned in test_the_layer_coverage_figure_is_citable.
    for bar in (f"{THICKNESS_FLOOR_PCT:g}", f"{ILLEGAL_PER_MILLION_CEILING:g}"):
        assert bar in joined, (bar, joined)


def test_a_pass_that_met_every_floor_publishes_an_empty_list_and_not_nothing():
    """EMPTY IS A JUDGEMENT. Filtered out on truthiness it becomes indistinguishable from a run
    nobody judged, which is the same collapse in the other direction."""
    p = quality(_manifest(**MET))
    assert p.get("layer_short") == [], (
        "an empty shortfall does not survive the reader, so a pass that met every floor is "
        "published identically to a pass nobody judged: "
        f"{p.get('layer_short')!r}")
    assert p.get("near_wall_layers") == pytest.approx(2.84)
    assert p.get("near_wall_layers_measured") is True
    assert p.get("layer_floors_unjudged") == [], (
        "and nothing says all four floors had a figure, which is the only thing that makes that "
        f"empty shortfall mean what this file says it means: {p.get('layer_floors_unjudged')!r}")


def test_a_partly_judged_pass_is_told_apart_from_one_that_met_every_floor():
    """THE TWO PAYLOADS THAT USED TO BE THE SAME SHAPE. MET and PART_READ both carry
    `layer_short` `[]`; one met four floors and the other met one and never looked at three.
    The reader has to carry what separates them, or the panel has to invent it."""
    met, part = quality(_manifest(**MET)), quality(_manifest(**PART_READ))
    assert met.get("layer_short") == part.get("layer_short") == [], "the premise"
    assert part.get("layer_floors_unjudged") == ["thickness", "layers kept", "faces in error"], (
        "a payload whose shortfall list is empty because three floors were never judged reaches "
        "the reader indistinguishable from one that cleared all four: "
        f"{part.get('layer_floors_unjudged')!r}")
    assert part.get("layer_floors_total") == 4, (
        "and with no denominator the panel cannot say 'one of four' without writing the 4 into "
        f"the viewer, which is a second reader of this engine's constant: {part!r}")
    assert part.get("near_wall_layers_measured") is False, (
        "the flag that contradicted the empty list has to keep saying what it says")


def test_the_names_of_the_unjudged_floors_survive_as_names():
    """A LIST, through `_as_lines`: a bare string would be published as a list of its characters
    and a tuple has to come back as a list, because the one consumer is JSON on its way to a
    browser."""
    p = quality(_manifest(**{**PART_READ, "layer_floors_unjudged": ("thickness", "  ", "")}))
    assert p["layer_floors_unjudged"] == ["thickness"], (
        f"a blank is not the name of a floor: {p['layer_floors_unjudged']!r}")
    bad = quality(_manifest(**{**PART_READ, "layer_floors_unjudged": "thickness"}))
    assert "layer_floors_unjudged" not in bad, (
        "a bare string reached the reader and would be drawn as nine floors with one letter each: "
        f"{bad.get('layer_floors_unjudged')!r}")


# #
# absent, and what it must not be mistaken for
# #
def test_a_run_that_grew_no_layers_is_a_zero_that_says_it_was_not_measured():
    p = quality(_manifest(**NONE_ASKED))
    assert p.get("near_wall_layers") == 0
    assert p.get("near_wall_layers_asked") == 0, (
        "the ask is not published under its own name, so a reader cannot tell a layer count that "
        "was asked for from one that was measured")
    assert p.get("near_wall_layers_measured") is False, (
        "a bare zero with no flag beside it reads as a measurement of nothing grown; this run "
        "asked for nothing and measured nothing, which is a different statement")
    for k in ("layer_short", "layer_coverage_pct", "layer_thickness_pct", "layer_thickness_m",
              "layer_faces_in_error", "layer_cells_added", "layer_iterations", "layer_patch"):
        assert k not in p, f"{k} was invented for a run that never grew a layer: {p.get(k)!r}"


def test_a_run_whose_log_could_not_be_read_never_reads_as_a_pass():
    p = quality(_manifest(**UNREAD))
    assert p.get("near_wall_layers") == 3 and p.get("near_wall_layers_asked") == 3
    assert p.get("near_wall_layers_measured") is False, (
        "the only thing separating this payload from a mesh that grew its three layers is the "
        "flag, and without it the reader presents the ask as the achievement")
    assert "layer_short" not in p, (
        "an empty shortfall here would claim a layer pass was judged and met every floor, on a "
        "run whose log could not be read")
    assert "layer_coverage_pct" not in p, "no coverage was measured, so none may be published"


def test_an_engine_that_says_nothing_about_layers_is_unchanged():
    """Every single-region engine, and every conjugate run from before the pass was measured.
    Absent must stay absent: a 0.0 invented here would read as a measurement of a failure."""
    p = quality({"mesh_mode": "cfmesh", "mesh_units": "m", "cell_count": 10,
                 "quality": {"cells": 10}})
    for k in CARRIED:
        assert k not in p, (
            f"{k} is absent from the manifest and the reader published {p.get(k)!r}, which a "
            f"reader cannot tell from a measurement")


def test_the_near_wall_resolution_still_travels_beside_the_count():
    """A layer count alone does not say how the wall was resolved, and these two were the whole
    answer on every run that grew none."""
    p = quality(_manifest(**GREW))
    assert p["near_wall_levels"] == 4
    assert p["near_wall_cell_m"] == pytest.approx(2.51578e-05)


# #
# what the reader leaves behind, and what it refuses
# #
def test_the_same_figure_is_not_published_under_four_names():
    """DELIBERATE. `layers_in_mesh` is 2.25 again and `layers_asked` is 3 again; the manifest keeps
    both because `finalize` itself reads them - `_near_wall` to get the figure at all, `_said` to
    print it and `layers.floors_unjudged` to say whether the layers floor had a reading - but one
    panel showing one number four times is four readings to a reader who has one. The pass's
    seconds are a cost and belong with the timings, and the ten-point coverage curve is a series
    where this panel has one value to a row."""
    p = quality(_manifest(**GREW))
    assert p["near_wall_layers"] == pytest.approx(2.25)
    for dup in ("layers_in_mesh", "layers_asked"):
        assert dup not in p, f"{dup} repeats a figure the payload already carries"
    assert "layer_seconds" not in p
    assert "layer_coverage_by_iteration" not in p


def test_the_duplicate_that_had_no_reader_cannot_come_back():
    """A FIELD NOBODY READS IS A FIELD NOBODY MAINTAINS, and this one was published for a day.
    `near_wall_layers_in_mesh` existed to carry the exact 2.25 "under a key nothing coerces",
    because `near_wall_layers` was being cast to 2 by `mesh_facts._as_int` - and when that cast was
    removed the duplicate was guarding a coercion that no longer happens. Nothing in src/, ui/ or
    apps/ ever read it.

    GREPPED HERE RATHER THAN REMEMBERED, over the shipped source: the record in
    `finalize._near_wall`'s docstring says it is gone, and a docstring is not a gate. If it is
    reintroduced it has to arrive with a reader.
    """
    src = pathlib.Path("src/meshpipeline")
    producers = [str(p) for p in src.rglob("*.py")
                 if "near_wall_layers_in_mesh" in p.read_text()
                 and p.name != "finalize.py"]
    assert not producers, (
        "a layer figure is published under a second name again; the last one reached the manifest "
        f"with no reader anywhere in the product: {producers}")
    # and the one mention that is allowed is the record of why it went, not a publication
    hits = [ln for ln in (src / "engines/cfmesh_conjugate/finalize.py").read_text().splitlines()
            if "near_wall_layers_in_mesh" in ln]
    assert all(ln.lstrip().startswith(("#", "`", "'", '"')) or "IS GONE" in ln for ln in hits), (
        f"finalize publishes the duplicate again rather than recording why it went: {hits}")
    assert "near_wall_layers_in_mesh" not in quality(_manifest(**GREW))


def test_the_coverage_figure_reaches_the_reader_on_every_engine_that_measures_it():
    """THE ONE KEY HERE WITH MORE THAN ONE PRODUCER, and the only part of this change that is not
    about the conjugate engine. `layer_coverage_pct` is written by cfmesh_conjugate, cfmesh,
    snappy, snappy_multiregion and vmtk, all meaning the share of wall faces that ended up layered,
    and `engines/verdict_limits` declares the single cross-engine limit on it. It reached the
    viewer on none of them: a snappy mesh whose layers covered 47.26% of its walls showed a panel
    with no mention of layers at all. SYNTHETIC - 47.26 is the figure in this repo's own snappy
    fixture, `tests/unit/engines/test_snappy_layer_coverage.py`, not a run measured here.
    """
    p = quality({"mesh_mode": "snappy", "mesh_units": "m", "cell_count": 1_219_085,
                 "quality": {"cells": 1_219_085, "layer_coverage_pct": 47.26}})
    assert p.get("layer_coverage_pct") == pytest.approx(47.26), (
        "a layer coverage this engine measured does not reach the one panel a customer reads, so "
        "a mesh that layered under half its walls looks like a mesh nobody asked for layers on")
    # and it is still the only layer figure such a run has, so nothing else is invented beside it
    for k in ("near_wall_layers", "near_wall_layers_asked", "near_wall_layers_measured",
              "layer_short", "layer_patch"):
        assert k not in p, f"{k} was invented for an engine that never published it: {p.get(k)!r}"


def test_a_flag_is_a_bool_and_not_a_small_number():
    """`_as_int` and `_as_float` refuse a bool on purpose, so a flag needs its own reader - and
    that reader refuses a 1, because a 1 standing in for a yes is a guess at what an engine meant
    about whether its own figure was measured."""
    assert "near_wall_layers_measured" not in quality(_manifest(near_wall_layers_measured=1))
    assert "near_wall_layers_measured" not in quality(_manifest(near_wall_layers_measured="true"))
    assert quality(_manifest(near_wall_layers_measured=False)).get(
        "near_wall_layers_measured") is False, (
        "a False flag is the answer on every run that measured nothing, and a reader that filters "
        "it out leaves the ask standing as though it had been measured")


def test_a_shortfall_that_is_not_a_list_is_refused():
    """A bare string published as a list of findings is a list of its characters."""
    assert "layer_short" not in quality(_manifest(layer_short="covered 88.5826% of 92%"))
    assert quality(_manifest(layer_short=["  a  ", "", "b"])).get("layer_short") == ["a", "b"], (
        "a list of findings is carried line by line, stripped, with the blanks dropped")
