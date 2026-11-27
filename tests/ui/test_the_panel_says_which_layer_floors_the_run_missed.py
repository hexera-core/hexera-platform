# Responsibility: Verify in a real browser that the "Delivered mesh" panel shows the layer figures
# the payload now carries - the measured count against the ask, how well the stack grew, and the
# engine's own sentences for every floor it fell under - rather than dropping them into "Other"
# under their raw key names.
# Boundaries: the payload is built by the shipped backend producers; only the network is stood in
# for. Nothing here opens a mesh, cuts an interior or renders a volume.
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

REPO = Path(__file__).parents[2]

# ONE SPELLING OF THE MEASUREMENT. The real plate's layer log (job 0ff6f9e5, 2026-11-16,
# transcribed from that run's own preserved snappyHexMesh.log) lives in the engines tier, and the
# figures in this file are read out of it by the shipped parser rather than copied. Copying the
# table here would give this repo's one real layer measurement a second spelling that could
# disagree, which is the failure this engine has already paid for once.
sys.path.insert(0, str(REPO / "tests" / "unit" / "engines"))
#: THE ZERO-LAYER LOG comes from the engines-tier file that measures the producer side of the
#: same defect, imported for the same reason the plate's log is: one spelling. SYNTHETIC.
from test_a_floor_with_nothing_to_divide_by_was_not_judged import (  # noqa: E402
    ZERO_LAYER_LOG,
)
from test_a_layer_pass_on_the_native_path_is_published import (  # noqa: E402
    GOOD_LOG,
    REAL_LOG,
)

from meshpipeline.engines.cfmesh_conjugate import finalize as F  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate import layers as LY  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID  # noqa: E402
from meshpipeline.render.mesh_facts import quality  # noqa: E402

JOB = "layers-job"

# WHY THIS TEST EXISTS. The backend started publishing ten layer keys on 2026-11-17 -
# near_wall_layers_asked, near_wall_layers_measured, layer_coverage_pct, layer_thickness_pct,
# layer_thickness_m, layer_faces_in_error, layer_cells_added, layer_iterations, layer_patch and
# layer_short. viewer.js knew none of them, so every one fell through to the "Other" group under
# its raw key name; `layer_short` went through the generic row's String(), which drew the real
# plate's three shortfall sentences as ONE comma-joined line and an empty list as a labelled row
# with no value; and `near_wall_layers_measured` - the only thing in the payload that says whether
# the count beside it is a measurement or the ask - sat in "Other", away from the row it qualifies.
# A reader looking at that panel could not see that this run fell under three of this engine's own
# four floors, with every figure needed to see it already on the page.
#
# NOTHING HERE RESTATES A FLOOR. The four bars are in engines/cfmesh_conjugate/layers.py and the
# panel shows `layers.shortfalls`' own sentences, which carry their own numbers. The assertions
# below import those constants to check the sentences, exactly as the two unit-tier files do.

#: SYNTHETIC: a plan for `finalize._near_wall` to read a background cell and a level count out of.
#: The plate's own two are not in its layer log and nothing here asserts on them.
PLAN: dict = {"background_cell_m": 2.0e-04, "levels": 3, "layers": 3}

#: THE LOG THAT WAS READ IN PART. SYNTHETIC: the plate's first extrusion line and its cell count
#: with the layer table and the illegal-face line cut off. ONE of this engine's four layer floors
#: has a figure behind it, and the panel used to report that as four.
TRUNCATED_LOG = ("Extruding 16122330 out of 16582198 faces (97.2267%). "
                 "Removed extrusion at 334828 faces.\n"
                 "Added 49746594 out of 49746594 cells (100%).\n")


def _payload_quality(log: str | None, *, asked: int = 3) -> dict:
    """The viewer payload's `quality` block, built by the shipped producers end to end.

    `conjugate.build` reads the layer log and writes back the shortfalls it computed against the
    pass's own `cells_added`; `finalize.quality_of` does `q.update(_layers_published(raw))` then
    `q.update(_near_wall(plan, grown))`; `render/mesh_facts.quality` is the one reader the panel is
    built from. This is that chain without a case directory, so a figure the backend stops
    publishing fails this test rather than quietly emptying the panel.
    """
    q: dict = {"cells": 102_536_327, "faces": 312_000_000}
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
    return quality({"mesh_mode": "cfmesh_conjugate", "mesh_units": "m",
                    "cell_count": q["cells"], "quality": q})


def _grid(name: str, nx: int, ny: int, h: float = 0.001) -> dict:
    """A flat nx-by-ny sheet of quads in the (points, polys) form a boundary reader hands over.
    A SURFACE, and the smallest one that makes the viewer open at all - the panel under test is
    text, and no cut of a real mesh interior is drawn anywhere in this file."""
    xs = np.arange(nx + 1) * h
    ys = np.arange(ny + 1) * h
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1).astype(np.float32)
    pid = lambda i, j: i * (ny + 1) + j                                   # noqa: E731
    quads = [[4, pid(i, j), pid(i + 1, j), pid(i + 1, j + 1), pid(i, j + 1)]
             for i in range(nx) for j in range(ny)]
    return {"name": name, "type": "wall", "face_count": nx * ny,
            "points": pts.tobytes(),
            "polys": np.asarray(quads, dtype=np.uint32).reshape(-1).tobytes()}


def _payload(q: dict) -> dict:
    surf = polymesh_response([_grid("fluid_to_solid", 20, 20)], {"p10": 0.001, "typ": 0.001},
                             {"fluid_to_solid": "wall"}, "m")
    assert surf is not None and surf["kind"] == "polymesh"
    surf.update(is_mesh=True, parts=[], quality=q)
    return surf


def _open(live, payload: dict) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__JOB__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=90,
                  what="the viewer to initialise")


def _facts(live) -> str:
    """The whole panel as a reader sees it.

    LOWERCASED. `theme.css` sets `.mx-k::first-letter{text-transform:uppercase}`, which `innerText`
    reports as the rendered capital, so a sentence the backend wrote in lower case arrives here
    with its first letter changed. Every assertion in this file is about which words and figures
    are on the page, never about their case.
    """
    return (live.evaluate(
        f"(document.getElementById('v-facts-{JOB}')||{{}}).innerText || ''") or "").lower()


def _group_of(live, key: str) -> str:
    """Which titled group the row for `key` is drawn in, as its heading reads."""
    return live.evaluate(
        f"""(() => {{
          const el = document.querySelector('#v-facts-{JOB} [data-key="{key}"]');
          if (!el) return '';
          const g = el.closest('.mx-group');
          return g ? (g.querySelector('.mx-gh')||{{}}).textContent || '' : '(ungrouped)';
        }})()""") or ""


def _rows_for(live, key: str) -> int:
    return int(live.evaluate(
        f"document.querySelectorAll('#v-facts-{JOB} [data-key=\\\"{key}\\\"]').length") or 0)


def _row_text(live, key: str) -> str:
    """The first row drawn for `key`, as a reader sees it. Lower-cased, for `_facts`' reason."""
    return (live.evaluate(
        f"(document.querySelector('#v-facts-{JOB} [data-key=\\\"{key}\\\"]')||{{}})"
        f".innerText || ''") or "").lower()


# #
# the real plate: three floors missed, and a reader has to be able to see it
# #
def test_the_three_floors_the_real_plate_missed_are_each_said_on_the_panel(live):
    """REAL PLATE (job 0ff6f9e5) figures, read from that run's own layer log. SURFACE only - the
    panel is text drawn beside a 400-face sheet, and no interior is cut here."""
    q = _payload_quality(REAL_LOG)
    assert len(q["layer_short"]) == 3, "the premise: the payload carries three shortfall lines"
    _open(live, _payload(q))
    text = _facts(live)
    assert _rows_for(live, "layer_short") == 3, (
        "the three sentences that say which of this engine's own floors the run missed are not "
        f"three rows on the panel; the whole panel reads:\n{text}")
    for mark in (f"{LY.KEPT_OF_OFFERED_FLOOR_PCT:g}%", f"{LY.THICKNESS_FLOOR_PCT:g}%",
                 f"{LY.ILLEGAL_PER_MILLION_CEILING:g}"):
        assert mark in text, (
            f"a reader cannot see the floor {mark} this run fell under, although the sentence "
            f"that names it is in the payload:\n{text}")
    assert "88.5826%" in text and "71.9%" in text and "239 per million" in text, (
        f"and the measured figures the misses are drawn on are not on the page:\n{text}")
    assert "fell short" in text, (
        f"nothing on the panel says these three sentences are shortfalls:\n{text}")
    assert_clean(live, "the viewer with the real plate's layer block")


def test_the_shortfall_lines_are_not_one_comma_joined_line(live):
    """`String(['a','b','c'])` is "a,b,c". Three findings drawn as one line is one finding as far
    as a reader is concerned, and the first one swallows the other two."""
    q = _payload_quality(REAL_LOG)
    _open(live, _payload(q))
    text = _facts(live)
    joined = ",".join(q["layer_short"]).lower()
    assert joined not in text, (
        "the three shortfall sentences are drawn as a single comma-joined line, which is what "
        f"String(array) produces:\n{text}")
    for line in q["layer_short"]:
        assert line.lower() in text, (
            f"a shortfall sentence is not on the page whole:\n{line}\n{text}")
    assert_clean(live, "the viewer with three shortfall lines")


def test_the_measured_layer_count_is_drawn_against_the_ask_on_one_row(live):
    """2.25 with nothing beside it cannot be seen to have fallen short of anything, which is how
    3 came to be quoted to this customer as an achievement."""
    q = _payload_quality(REAL_LOG)
    _open(live, _payload(q))
    text = _facts(live)
    assert "boundary layers" in text and "2.25" in text, f"the count itself is missing:\n{text}"
    assert "3 asked for" in text, f"the ask is not drawn beside the measured count:\n{text}"
    # and the two must be ON THE SAME ROW: the ask in a different group is a second figure, which
    # is how 2.25 and 3 came to be read as two readings instead of one shortfall
    row = (live.evaluate(
        f"(document.querySelector('#v-facts-{JOB} [data-key=\\\"near_wall_layers\\\"]')"
        f"||{{}}).innerText || ''") or "").lower()
    assert "2.25" in row and "3 asked for" in row, (
        f"the measured count and the ask are not on one row: the row reads {row!r}")
    assert_clean(live, "the viewer with a measured layer count")


def test_no_layer_figure_is_left_in_the_other_group_under_its_raw_key(live):
    """THE DEFECT, stated as the thing a reader sees: ten keys in a bucket called Other, named
    `layer coverage pct` and `near wall layers measured`."""
    q = _payload_quality(REAL_LOG)
    _open(live, _payload(q))
    text = _facts(live)
    for key in ("near_wall_layers_asked", "near_wall_layers_measured", "layer_coverage_pct",
                "layer_thickness_pct", "layer_thickness_m", "layer_faces_in_error",
                "layer_cells_added", "layer_iterations", "layer_patch", "layer_short"):
        assert key in q, f"{key} is not in the payload, so this test is measuring nothing"
        where = _group_of(live, key)
        assert where != "Other", (
            f"{key} is drawn in the panel's Other group, under its raw key name, away from the "
            f"layer rows it belongs with:\n{text}")
        assert key.replace("_", " ") not in text, (
            f"{key} is labelled with its own key spelled out - '{key.replace('_', ' ')}' - which "
            f"is what the Other group does to a figure this build has no name for:\n{text}")
    assert _group_of(live, "near_wall_layers_measured") in ("", "At the wall"), (
        "the flag that says whether the layer count is a measurement or an intention has to be "
        "on the row it qualifies, not in a group of its own")


def test_the_panel_no_longer_tells_this_customer_a_prism_stack_does_not_fit(live):
    """DELETED, and measured false before it was deleted. The row printed 'a prism stack does not
    fit across a passage this narrow without taking the cell quality to its limit' whenever the
    count was 0 - a reading taken on a stack 109% of its own background cell, which is a dictionary
    defect (`layers.fit_complaint` refuses that dict now) and not a property of the part. This
    engine grows layers; the plate has 2.25 of them."""
    q = _payload_quality(REAL_LOG)
    _open(live, _payload(q))
    text = _facts(live)
    assert "does not fit" not in text, f"a claim measured false is still on the page:\n{text}"
    assert "graded refinement instead" not in text, (
        f"the graded cell is not drawn 'instead' of a stack this mesh has:\n{text}")
    assert_clean(live, "the viewer with a layered mesh")


# #
# the other two states of the shortfall list, which must not read like each other
# #
def test_a_pass_that_met_every_floor_says_so_rather_than_drawing_an_empty_row(live):
    """`[]` is a judgement. Put through the generic row it drew a labelled cell with no value,
    which reads as a figure that failed to load."""
    q = _payload_quality(GOOD_LOG)
    assert q["layer_short"] == [], "the premise: a judged pass with nothing to report"
    _open(live, _payload(q))
    text = _facts(live)
    assert "no shortfall" in text, (
        f"a pass that met all four of this engine's layer floors says nothing on the panel:\n{text}")
    assert "fell short" not in text, f"a clean pass is titled as a shortfall:\n{text}"
    assert "2.84" in text, f"and its measured count is missing:\n{text}"
    assert_clean(live, "the viewer with a layer pass that met every floor")


def test_a_run_whose_layer_log_could_not_be_read_draws_no_verdict_at_all(live):
    """ABSENT IS ABSENT. The payload for such a run carries no layer_short at all, and the panel
    must not reassure a reader about a judgement that never happened - while still saying that the
    count it shows is the ask.

    WHERE THE PRODUCER SIDE OF THIS ACTUALLY IS, CORRECTED 2026-11-18. This pointed at
    `finalize._floors_had_a_reading`, which had no caller anywhere in src/, ui/, apps/ or tests/
    and was deleted last round. The guard that runs is the length comparison inside
    `finalize._layers_published` - `len(unjudged) >= len(layers.LAYER_FLOORS)` - which returns the
    two tally keys and nothing else when no floor had a figure to be judged on. This is the same
    correction already made twice in `render/mesh_facts.py`; it is made here because a pointer to
    a function that does not exist is worse than no pointer, as the next reader looks for the
    guard where it is not and does not find the one that runs. The assertion below is on
    behaviour and was correct before and after."""
    q = _payload_quality("")
    assert "layer_short" not in q and "layer_iterations" not in q, (
        "the premise: an unreadable log publishes nothing about layers")
    _open(live, _payload(q))
    text = _facts(live)
    assert "no shortfall" not in text and "fell short" not in text, (
        f"the panel delivers a verdict on a layer pass nothing judged:\n{text}")
    assert "not measured" in text, (
        "the count on the row is the 3 the mesher was asked for and the panel has to say so, or "
        f"it publishes the request as the achievement:\n{text}")
    assert_clean(live, "the viewer with an unreadable layer log")


def test_a_partly_judged_pass_is_not_drawn_as_a_pass_that_met_every_floor(live):
    """THE FOURTH STATE, and the panel had three. SYNTHETIC: a layer log holding only the
    Extruding and Added lines, which is what a stage killed mid-write leaves behind. Coverage was
    read and clears its floor, so `layer_short` is `[]` - and the panel drew that empty list as
    "no shortfall against this engine's layer floors" while the Boundary layers row three rows
    above it said "not measured - this is what the mesher was asked to grow". Two contradictory
    lines on one panel, on a run where three of the four floors were never evaluated.

    SURFACE only; no interior is cut here."""
    q = _payload_quality(TRUNCATED_LOG)
    assert q["layer_short"] == [] and q["near_wall_layers_measured"] is False, (
        "the premise: an empty shortfall list beside a count that was never measured")
    _open(live, _payload(q))
    text = _facts(live)
    assert "no shortfall" not in text, (
        "the panel tells this reader there was no shortfall against this engine's layer floors, "
        f"on a run where three of the four were never judged at all:\n{text}")
    assert "not measured" in text, (
        f"the half that was already right stopped being said:\n{text}")
    # and what it says instead names the floors and how many there are
    assert q.get("layer_floors_unjudged") == ["thickness", "layers kept", "faces in error"], (
        "the payload carries nothing that says which floors had no figure behind them: "
        f"{q.get('layer_floors_unjudged')!r}")
    for floor in q["layer_floors_unjudged"]:
        assert floor in text, (
            f"the panel does not name {floor!r} as a floor nothing judged, although the payload "
            f"carries it:\n{text}")
    assert "4 floors" in text or "the 4 floors" in text, (
        "the panel still has no denominator, so 'three of four' is arithmetic the reader has to "
        f"do from a number that is nowhere on the page:\n{text}")
    assert "could be judged" in text, (
        f"and the group is titled as though it were a verdict on the whole pass:\n{text}")
    assert_clean(live, "the viewer with a partly readable layer log")


def test_the_real_plates_panel_says_three_of_four_rather_than_leaving_it_to_the_reader(live):
    """REAL PLATE (job 0ff6f9e5). All four floors had a figure and three were missed, and until
    2026-11-17 the only way to know the denominator was to read `layers.py`. The three sentences
    under "Where the layer pass fell short of this engine's own floors" must not move."""
    q = _payload_quality(REAL_LOG)
    _open(live, _payload(q))
    text = _facts(live)
    assert _rows_for(live, "layer_short") == 3, f"the three shortfall rows moved:\n{text}"
    assert "fell short" in text, f"and their heading is no longer the verdict:\n{text}"
    assert "4 of the 4 floors" in text, (
        f"the panel does not say how many floors this engine judged this pass by:\n{text}")
    assert q.get("layer_floors_unjudged") == [] and q.get("layer_floors_total") == 4, (
        "and the payload says neither that all four were judged nor that there are four: "
        f"{q.get('layer_floors_unjudged')!r}, {q.get('layer_floors_total')!r}")
    assert "not judged at all" not in text, (
        f"a run whose four floors all had a figure is drawn as partly judged:\n{text}")
    assert_clean(live, "the viewer with the real plate's four judged floors")


def test_a_mesh_with_no_layer_pass_at_all_is_unchanged(live):
    """Every conjugate mesh delivered before 2026-11-16, and every single-region engine. No layer
    rows, no verdict, and the near-wall resolution still drawn."""
    q = _payload_quality(None, asked=0)
    _open(live, _payload(q))
    text = _facts(live)
    assert "cell at the wall" in text, f"the near-wall resolution stopped being drawn:\n{text}"
    assert "no shortfall" not in text and "fell short" not in text, (
        f"a verdict appeared on a run that never grew a layer:\n{text}")
    assert "none were asked for" in text, (
        f"and a bare 0 on the Boundary layers row reads as a failure:\n{text}")
    assert_clean(live, "the viewer with no layer pass")


# #
# a floor whose test never ran is not a floor that was judged
# #
def test_the_panel_counts_the_floors_that_were_judged_and_not_the_keys_that_arrived(live):
    """SYNTHETIC: a pass that grew nothing - 0% extruded, zeros across the table row, and 90,000
    faces in error over no cells added at all.

    THREE of this engine's four floors were missed and the fourth could not be evaluated: it is
    faces in error PER MILLION CELLS ADDED and there is nothing to divide by. The panel printed
    "4 of the 4 floors this engine judges a layer pass by had a figure in the layer log" over a
    verdict on three - on the same page as a "Faces in error 90,000" row with no per-million
    figure beside it, which is that same missing denominator said twice. SURFACE only; no interior
    is cut here."""
    q = _payload_quality(ZERO_LAYER_LOG)
    # TWO, not three: the coverage floor is gone (the bar is on what the pass KEPT of what
    # it was offered - layers.KEPT_OF_OFFERED_FLOOR_PCT) and this fixture carries no
    # illegal-face count to divide, so thickness and layers-kept are what is left.
    assert len(q["layer_short"]) == 2, "the premise: two floors were missed"
    assert q["layer_faces_in_error"] == 90000 and q["layer_cells_added"] == 0, (
        "the premise: the face count was read and its denominator is zero")
    _open(live, _payload(q))
    text = _facts(live)
        # TWO, not three. The floor that replaced the coverage one is drawn on
    # `layer_kept_of_offered_pct` (layers.KEPT_OF_OFFERED_FLOOR_PCT), which this
    # all-zero log cannot produce - so it joins "faces in error" as unjudged, and the
    # payload reports layer_floors_unjudged ["faces kept of those offered",
    # "faces in error"] against layer_floors_total 4. Computed from the shipped
    # producers, not guessed.
    assert ("2 of the 4 floors this engine judges a layer pass by were judged on what this "
            "pass measured") in text, (
        "the panel tells this reader the pass was judged by all four of this engine's layer "
        f"floors, when two of the four tests are the only ones that ran:\n{text}")
    assert "4 of the 4 floors" not in text, (
        f"and four of four is still claimed somewhere on the page:\n{text}")
    row = _row_text(live, "layer_floors_unjudged")
    assert "faces in error" in row and "not judged" in row, (
        "the floor whose test never ran is not named as one nothing judged; that row reads "
        f"{row!r} and the whole panel:\n{text}")
    # and the real misses are still said, which is what a rule about zeroes would have
    # cost. TWO on this all-zero log: the floor that replaced the coverage one is drawn
    # on `layer_kept_of_offered_pct`, which this log cannot produce, so it is UNJUDGED
    # rather than missed - which the row above has just asserted by name.
    assert _rows_for(live, "layer_short") == 2, "the shortfall rows moved: " + text
    assert "fell short" in text, f"and their heading is no longer the verdict:\n{text}"
    assert_clean(live, "the viewer with a zero-layer pass")


# #
# a log that could not be read is not the absence of a pass
# #
def test_an_unreadable_layer_log_says_a_pass_ran_that_nothing_could_be_judged_on(live):
    """THE TWO STATES THAT WERE ONE PANEL. An unreadable layer log and no layer pass at all both
    came out of `finalize._layers_published` as {}, so both panels read "Boundary layers 3 / not
    measured - this is what the mesher was asked to grow" and said nothing else about layers.
    Neither reads as a clean pass, so this was never a false claim - but a reader cannot tell "we
    grew layers and cannot tell you how they came out" from "we grew none", and those call for
    different actions. SYNTHETIC: `read_layer_log("")`, which is what `conjugate.build` hands over
    when the layer log is missing or unparseable."""
    q = _payload_quality("")
    assert "layer_short" not in q, "the premise: nothing judged this pass, so there is no verdict"
    _open(live, _payload(q))
    text = _facts(live)
    assert "a layer pass ran" in text, (
        "the panel for a run whose layer log could not be read is the panel for a run that never "
        f"grew a layer, and a reader cannot tell which one they have:\n{text}")
    assert "none of the 4 floors" in text, (
        f"and it does not say how many floors were left unjudged, or of how many:\n{text}")
    assert "no shortfall" not in text and "fell short" not in text, (
        f"the panel delivers a verdict on a layer pass nothing judged:\n{text}")
    assert "not measured" in text, (
        "the count on the row is the 3 the mesher was asked for and the panel still has to say "
        f"so:\n{text}")
    assert_clean(live, "the viewer with an unreadable layer log")


def test_a_mesh_with_no_layer_pass_does_not_say_a_pass_ran(live):
    """The other half of that pair, and the reason the first one is readable as a difference. The
    plan asks for THREE here, the same as the arm above, so the only thing that differs between
    the two panels is whether a pass ran at all - this report carries no layers block, which is
    what `conjugate.build` leaves behind when the stage never ran."""
    q = _payload_quality(None, asked=3)
    assert "layer_floors_total" not in q and "layer_floors_unjudged" not in q, (
        "the premise: a run with no layer pass publishes no tally")
    _open(live, _payload(q))
    text = _facts(live)
    assert "a layer pass ran" not in text and "floors" not in text, (
        f"a run that never grew a layer is drawn as one whose log could not be read:\n{text}")
    assert "not measured" in text, (
        f"and the count on the row stopped being said to be the ask:\n{text}")
    assert_clean(live, "the viewer with no layer pass at all")
