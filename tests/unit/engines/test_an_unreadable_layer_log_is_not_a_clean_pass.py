# Responsibility: hold the conjugate engine to publishing no layer FIGURE and no layer VERDICT
# on a run whose layer log could not be read - only the statement that no floor was judged - so
# the one empty list a reader is entitled to read as a clean pass is only ever published by a pass
# that was actually judged, and so that such a run does not read as a mesh that never grew a layer.
# Boundaries: layers.read_layer_log/measured/shortfalls, finalize._layers_published/_near_wall,
# render/mesh_facts.quality and the declared layer_short criterion. No meshing, no case directory,
# no rendering.
"""A RUN WHERE THE JUDGEMENT NEVER HAPPENED REPORTED AS A CLEAN ONE.

`render/mesh_facts._LINE_METRICS` states the rule in its own words: `[]` is "this was judged and
it met every floor", an absent key is "nobody judged it", and collapsing the two is the failure
that file exists to stop. `finalize._layers_published` broke it from the producer side.

MEASURED 2026-11-17, SYNTHETIC - an empty log string through the shipped producers, which is what
`conjugate.build` hands over when `snappyHexMesh.log` is missing or unparseable:

    layers.read_layer_log("")   every figure None except iterations, which is len(extruding) = 0
    layers.measured(...)        {"layer_iterations": 0}        - 0 is not None and not "", so it
                                                                 survives the filter
    finalize._layers_published  {"layer_iterations": 0, "layer_short": []}
    mesh_facts.quality(...)     layer_iterations 0, layer_short []
    the declared criterion      layer_short, op="empty" -> Criterion.evaluate returns `not []`
                                                        -> True -> PASS

So a run that measured nothing about its layers published the same empty shortfall as the
hand-built run that met all four floors, and the engine's own report card called it a pass. The
one guard in that function, `if not out: return {}`, asked whether ANY figure survived - and the
iteration count always does - so it never fired on the case it was written for.

WHAT IS SYNTHETIC HERE AND WHAT IS NOT. Every log in this file is text, read by the shipped
parser; nothing here opens a mesh or draws a picture. REAL_LOG and GOOD_LOG are imported from
`test_a_layer_pass_on_the_native_path_is_published` rather than copied, so the REAL PLATE's figures
(job 0ff6f9e5, 2026-11-16) have one spelling in this suite. The unreadable and truncated logs below
are SYNTHETIC and are not readings of any part.
"""
from __future__ import annotations

import pytest
from test_a_layer_pass_on_the_native_path_is_published import GOOD_LOG, REAL_LOG

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import layers as LY
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID
from meshpipeline.engines.cfmesh_conjugate.criteria import CRITERIA_ROWS
from meshpipeline.render.mesh_facts import quality

#: SYNTHETIC, and asserted on nowhere: a plan for `_near_wall` to read a background cell and a
#: level count out of.
PLAN: dict = {"background_cell_m": 2.0e-04, "levels": 3, "layers": 3}

#: THE LOG THAT COULD NOT BE READ. `conjugate.build` does
#: `read_layer_log(log.read_text(...) if log.is_file() else "")`, so a missing file and a file
#: whose contents match none of the parser's regexes arrive here as the same thing. SYNTHETIC.
UNREADABLE: tuple[str, ...] = (
    "",
    "/*---------------------------------------------------------------------------*\\\n"
    "| =========                 |                                                 |\n"
    "\\*---------------------------------------------------------------------------*/\n"
    "Create time\n"
    "Create mesh for time = 0\n",
    "\x00\x00\x00\x00 the stage was killed before it wrote anything a parser can read \x00\x00",
)

#: The ONE row of the engine's declared report card this is about.
LAYER_SHORT = next(c for c in CRITERIA_ROWS if c.key == "layer_short")


def _published(log: str | None, *, asked: int = 3) -> dict:
    """The manifest quality block for a layer pass, built by the shipped producers.

    `conjugate.build` reads the log and writes back the shortfalls it computed against the pass's
    own `cells_added`; `finalize.quality_of` then does `q.update(_layers_published(raw))` followed
    by `q.update(_near_wall(plan, grown))`. This is that pair without a case directory.

    `complain` is NOT asserted away here, unlike the other files in this suite: the case this one
    is about is a report that reached `finalize` with nothing in it, and `complain` refusing it
    inside the mesher is exactly why the report can only get here across the container boundary.
    """
    q: dict = {"cells": 102_536_327}
    grown: dict = {}
    if log is not None:
        raw = LY.read_layer_log(log)
        cells = int(raw.get("cells_added") or 0)
        short = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID, n_layers=asked,
                                   cells=cells))
        if short:                       # exactly as conjugate.build writes it: only when non-empty
            raw["shortfalls"] = short
        grown = F._layers_published(raw)
        q.update(grown)
    q.update(F._near_wall({**PLAN, "layers": asked}, grown))
    return q


def _payload(log: str | None, *, asked: int = 3) -> dict:
    """...and what the one reader the viewer's panel is built from hands on."""
    q = _published(log, asked=asked)
    return quality({"mesh_mode": "cfmesh_conjugate", "mesh_units": "m",
                    "cell_count": q["cells"], "quality": q})


# #
# the premise, re-measured from the shipped parser rather than assumed
# #
@pytest.mark.parametrize("log", UNREADABLE)
def test_an_unreadable_log_really_does_leave_the_parser_with_only_an_iteration_count(log: str):
    """Why `if not out: return {}` could never have worked. SYNTHETIC."""
    raw = LY.read_layer_log(log)
    got = LY.measured(raw, interface_patch=FLUID_TO_SOLID)
    assert got == {"layer_iterations": 0}, (
        "the premise of this file is that an unreadable log leaves exactly one figure behind and "
        f"that it is a zero, which is not None and not '': {got!r}")
    assert LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID, n_layers=3, cells=0) == [], (
        "and that all four floors are skipped, so the shortfall list is empty for the reason "
        "'nobody judged it' and not for the reason 'it met every floor'")


# #
# absent must read as absent
# #
@pytest.mark.parametrize("log", UNREADABLE)
def test_a_run_whose_layer_log_could_not_be_read_publishes_no_shortfall(log: str):
    """THE DEFECT. An empty shortfall is a judgement; this run has none to publish."""
    q = _published(log)
    assert "layer_short" not in q, (
        "a run whose layer log could not be read publishes an EMPTY shortfall list, which this "
        "engine's own reader defines as 'this was judged and it met every floor'. Nothing judged "
        f"it: {q.get('layer_short')!r}")
    assert "layer_iterations" not in q, (
        "and it publishes a zero iteration count beside it, which is not a reading of a layer "
        f"pass but the absence of one: {q.get('layer_iterations')!r}")
    # NOTHING ABOUT LAYERS EXCEPT THAT NOTHING WAS JUDGED, and that one statement is required
    # rather than tolerated. Until 2026-11-17 this published {}, the same payload a mesh that
    # never grew a layer publishes, so the two were one panel - see
    # `test_a_floor_with_nothing_to_divide_by_was_not_judged`, which measures both sides of it.
    # No figure, no verdict, and no iteration count still, because none of those was measured.
    carried = sorted(k for k in q if k.startswith("layer"))
    assert carried == ["layer_floors_total", "layer_floors_unjudged"], (
        f"nothing about layers was measured and the manifest carries {carried}")
    assert q["layer_floors_unjudged"] == [n for n, _ in LY.LAYER_FLOORS], (
        "and the one thing it may carry is that no floor was judged, in full and by name: "
        f"{q['layer_floors_unjudged']!r}")


@pytest.mark.parametrize("log", UNREADABLE)
def test_the_declared_report_card_row_reads_unavailable_and_never_a_pass(log: str):
    """The row `criteria.py` says is "the one place a miss is said where a reader and the reviewer
    both see it". Its own rationale already promised this - "Absent when no layer pass was judged,
    which never reads as a pass" - and the producer did not honour it."""
    assert LAYER_SHORT.op == "empty" and LAYER_SHORT.key == "layer_short"
    verdict = LAYER_SHORT.evaluate(_published(log))
    assert verdict is None, (
        "the engine's own report card marks the layer floors PASSED on a run that measured "
        f"nothing about its layers: Criterion.evaluate returned {verdict!r}, where an unjudged "
        "row has to come back None (UNAVAILABLE)")


@pytest.mark.parametrize("log", UNREADABLE)
def test_the_unreadable_run_still_says_its_layer_count_is_the_ask_and_not_a_measurement(log: str):
    """The half that was already right, held so the fix above cannot be made by deleting it."""
    p = _payload(log)
    assert p.get("near_wall_layers") == 3 and p.get("near_wall_layers_asked") == 3
    assert p.get("near_wall_layers_measured") is False, (
        "the flag is the only thing separating this payload from a mesh that grew its three "
        "layers, and without it the reader publishes the request as the achievement")


# #
# and a pass that WAS judged still publishes everything it measured
# #
def test_the_real_plates_three_shortfalls_still_reach_the_reader():
    """REAL PLATE (job 0ff6f9e5), SURFACE/VOLUME: neither - this is a reading of that run's own
    snappyHexMesh.log. The gate above must not be satisfied by publishing less."""
    p = _payload(REAL_LOG)
    short = p.get("layer_short")
    assert short is not None and len(short) == 3, (
        f"the plate missed three of this engine's four floors and the payload says {short!r}")
    assert p["layer_coverage_pct"] == pytest.approx(88.5826)
    assert p["layer_thickness_pct"] == pytest.approx(71.9)
    assert p["layer_faces_in_error"] == 8916 and p["layer_cells_added"] == 37_247_044
    assert p["layer_iterations"] == 10, "ten repair iterations IS a reading, and it has to survive"
    assert p["near_wall_layers"] == pytest.approx(2.25)
    assert LAYER_SHORT.evaluate(_published(REAL_LOG)) is False, (
        "three missed floors have to come back as a FAILED row, not an unavailable one")


def test_a_pass_that_met_every_floor_still_publishes_the_empty_list_that_says_so():
    """The hand-built run of 2026-11-08. SYNTHETIC fixture, measured figures. The whole value of
    an empty list is that only a judged pass can produce one, so it has to keep producing one."""
    q = _published(GOOD_LOG)
    assert q.get("layer_short") == [], (
        f"a pass that met all four floors no longer says so: {q.get('layer_short')!r}")
    assert LAYER_SHORT.evaluate(q) is True, "and its report-card row has to read PASSED"
    assert q["layer_coverage_pct"] > 0, "layers grew somewhere, the only bar left on it"
    assert q["layer_thickness_pct"] > LY.THICKNESS_FLOOR_PCT


#: THE LOG THAT WAS READ IN PART. SYNTHETIC: the plate's first extrusion line and its cell count,
#: with the layer table and the illegal-face line cut off, which is what a stage killed mid-write
#: leaves behind. Coverage WAS read; the thickness, the layer count and the illegal-face count
#: were not.
TRUNCATED = ("Extruding 16122330 out of 16582198 faces (97.2267%). "
             "Removed extrusion at 334828 faces.\n"
             "Added 49746594 out of 49746594 cells (100%).\n")

def _floors_row():
    """...and the row that says how many floors the row above is a verdict on.

    LOOKED UP INSIDE THE TEST, not at import. A missing row has to fail the assertion that reads
    it, with the sentence that says what a reader loses; at module scope it fails COLLECTION, and
    an error during collection takes every other test in this file with it and says nothing about
    what the panel told a customer.
    """
    return next((c for c in CRITERIA_ROWS if c.key == "layer_floors_unjudged"), None)


def test_a_partly_readable_log_is_judged_on_what_it_did_read():
    """BETWEEN THE TWO, and the reason the gate is 'did any floor have a reading' rather than
    'is the log complete'. Coverage WAS read, so the coverage floor was drawn and its verdict is
    real; the layer count and the thickness were not, so they are absent and the count falls back
    to the ask with the flag saying so."""
    q = _published(TRUNCATED)
    assert q["layer_coverage_pct"] == pytest.approx(97.2267)
    assert q.get("layer_short") == [], "coverage was judged and 97.2267% clears the floor"
    assert "layer_thickness_pct" not in q and "layers_in_mesh" not in q, (
        "the layer table was never reached, so no thickness and no layer count may be published")
    assert q["near_wall_layers_measured"] is False, (
        "and the count that is published is the ask, which the flag has to say")


def test_a_partly_readable_log_says_how_many_floors_its_empty_shortfall_is_about():
    """THE DEFECT, and it is the same one at a smaller scale. `_layers_published`' own guard only
    withholds the block when EVERY floor is unjudged, so one reading admits the whole block - and
    `layer_short` `[]` is then
    published on a run where three of the four floors were never evaluated at all. This file's own
    rule says what that empty list means: "this was judged and it met every floor".

    MEASURED, SYNTHETIC, 2026-11-17, on the payload the panel is built from: coverage 97.2267,
    `layer_short` `[]`, `near_wall_layers_measured` False. So the one reader a customer has said
    both "not measured - this is what the mesher was asked to grow" and "no shortfall against this
    engine's layer floors" about the same pass.
    """
    q = _published(TRUNCATED)
    assert q.get("layer_floors_unjudged") == ["thickness", "layers kept", "faces in error"], (
        "the payload publishes an empty shortfall list - which this engine's own reader defines "
        "as 'judged, and it met every floor' - and nothing beside it says that three of the four "
        f"floors had no figure to be judged on: {q.get('layer_floors_unjudged')!r}")
    assert q.get("layer_floors_total") == 4, (
        "and there is no denominator on the page, so 'three of four' is arithmetic the reader "
        f"has to do from a number that is nowhere in the payload: {q.get('layer_floors_total')!r}")
    p = _payload(TRUNCATED)
    assert p.get("layer_floors_unjudged") == ["thickness", "layers kept", "faces in error"], (
        "the names reach the manifest and stop at the one reader the viewer's panel is built "
        f"from: {sorted(k for k in p if k.startswith('layer'))}")
    assert p.get("layer_floors_total") == 4


def test_the_partial_read_cannot_contradict_the_measured_flag():
    """ONE INPUT, TWO READERS, AND THEY MUST AGREE. `near_wall_layers_measured` is False exactly
    when `layers_in_mesh` is absent, and that same absence is the "layers kept" floor having
    nothing to judge it on - so a payload that says the count was not measured and that every
    floor was judged is self-contradictory, and that is the pair the panel printed."""
    for log in (TRUNCATED, GOOD_LOG, REAL_LOG):
        q = _published(log)
        measured = q.get("near_wall_layers_measured")
        unjudged = q.get("layer_floors_unjudged") or []
        assert measured is ("layers kept" not in unjudged), (
            f"near_wall_layers_measured is {measured!r} while the unjudged floors are "
            f"{unjudged!r}: the panel can print 'not measured' and 'every floor judged' at once")


def test_the_report_card_says_a_partly_judged_pass_is_partly_judged():
    """The row above it, `layer_short`, comes back a PASS on this run and is not wrong to: the one
    floor that had a figure was met. What it cannot do is say how many floors that is."""
    q = _published(TRUNCATED)
    assert LAYER_SHORT.evaluate(q) is True, (
        "the premise: the floors that WERE judged were met, so the shortfall row passes")
    row = _floors_row()
    assert row is not None and row.evaluate(q) is False, (
        "nothing on the engine's own report card says that PASSED verdict covers one floor of "
        f"four: the row is {row!r}")
    assert row.evaluate(_published(GOOD_LOG)) is True, (
        "a log every figure was read from has to pass this row")
    assert row.evaluate(_published(REAL_LOG)) is True, (
        "and so does the real plate, whose four floors all had a figure - three of them missed, "
        "which is the OTHER row's verdict and not this one's")
    # AND A RUN NOTHING JUDGED SAYS SO ON THE ROW rather than leaving it blank, which is the
    # 2026-11-17 change and the reason this assertion moved from None to False. "Unavailable" was
    # indistinguishable from the mesh that never grew a layer, whose payload carries no such row
    # at all. The row asks whether every floor had a figure to be judged on; on this run the
    # answer is measured, and it is NO. Never a pass either way, and never gating - the pass has
    # already moved the only copy of the coolant mesh and rewritten it in place.
    nothing_judged = row.evaluate(_published(""))
    assert nothing_judged is False, (
        "a run whose layer log could not be read has to say so on the engine's own report card, "
        f"where a mesh with no layer pass has no row at all: {nothing_judged!r}")
    assert row.gating is False, (
        "and saying it must never throw away a finished mesh over a defect in the record")
