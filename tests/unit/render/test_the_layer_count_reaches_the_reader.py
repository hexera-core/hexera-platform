"""The number of boundary layers in the fluid, which is the third of the six readings.

Corintis asked for six things by name on 2026-10-29: mesh count, quality metrics, the number of
boundary layers in the fluid, min and max cell sizes, cell types with a count per type, and a
highlight of poor-quality cells. `render/mesh_facts` says what happens to a metric that is not in
its list, in its own words:

    a metric that is not here never reaches a reader however faithfully the mesher measured it -
    which is how "cell types and count per type" and "min and max cell sizes" came to be measured
    on every run and shown on none

The third reading went the same way and was still going that way. Measured on the delivered
coupon's own manifest (job 18e2f4ed):

    manifest quality : near_wall_layers 0, near_wall_levels 3, near_wall_cell_m 1.96e-05
    viewer payload   : none of the three

THE ANSWER IS A DELIBERATE ZERO and that is why it cannot be shipped alone. What the mesh has
instead is graded octree refinement toward the wall, so the three numbers travel together or none
of them means anything, and the viewer puts the reason on the row.

TWO REASONS WERE PUT ON THAT ROW AND BOTH WERE MEASURED FALSE. The first said an extruded prism
layer is a cell inserted on ONE side of a patch, so a stack on the interface would leave every
face without its partner: measured on the coupon, 2026-10-30, `generateBoundaryLayers` with
`optimiseLayer 0` kept all 14,477 interface faces identical on both sides to 4e-18 m at 99.97%
coverage. Layers do not break conformality.

THE SECOND WAS THE FIT, and this file used to require it. It said a prism stack does not go across
a 98 um passage at an affordable cell size without taking the quality to checkMesh's own bar
(non-orthogonality 13.82 before, 64.95 after, against a limit of 65). What that reading was
measuring was a stack 109% of its own BACKGROUND CELL, which is a dictionary defect and not a
property of the part - `layers.fit_complaint` refuses exactly that dict now. At 60.0% of a cell
the same part came back with 2.84 of 3 layers over 95.553% of a 16,619,777-face interface
(2026-11-08), and the first customer plate was delivered with 2.25 (job 0ff6f9e5, 2026-11-16). The
engine grows layers. So the row no longer argues from the geometry at all: it says what the payload
measured, with the ask and the measured flag beside it, and `layer_short` carries the engine's own
sentences for every floor the pass fell under.

A wrong reason on a customer's own mesh is worse than a bare zero: it is a claim about their
geometry that does not hold, and this one also argued against the run that would be worth doing.
The last test here holds the row to carrying NEITHER claim.
"""
from __future__ import annotations

import pathlib

from meshpipeline.render.mesh_facts import quality

#: the delivered coupon's own numbers
MANIFEST = {
    "mesh_mode": "cfmesh_conjugate",
    "mesh_units": "m",
    "cell_count": 6_582_471,
    "quality": {
        "cells": 6_582_471,
        "near_wall_layers": 0,
        "near_wall_levels": 3,
        "near_wall_cell_m": 1.96e-05,
        "interface_faces_fluid": 346_456,
        "interface_faces_solid": 346_456,
        "min_cell_m": 1.169e-05,
        "max_cell_m": 1.605e-04,
    },
}

#: BOTH OF THESE ARE SERVED, AND THIS FILE SAID ONE OF THEM WAS NOT. It said "the apps/console
#: path is a second copy nothing serves", and scoped the one gate over the refuted claim to the
#: other file on the strength of it. The chain was traced end to end on 2026-11-17:
#:
#:   ui/js/viewer/viewer.js
#:       docker-compose.yml:185 mounts ./ui at /srv/ui:ro, settings/inventory's STATIC_DIR
#:       defaults to ./ui, and the api serves it at /static. tests/ui/conftest points STATIC_DIR
#:       here, so this is the copy the browser suite drives.
#:   apps/console/public/static/js/viewer/viewer.js
#:       Dockerfile:682 COPYs apps/console/public/ into the console image;
#:       apps/console/src/app/(dashboard)/runs/[jobId]/page.tsx renders Workbench;
#:       workbench.tsx:85 loads /static/js/main.js; main.js:34 imports ./viewer/viewer.js; and
#:       apps/console/next.config.ts declares no rewrites. Next serves public/ from the root, so
#:       that path IS this file and the console's run page runs it.
#:
#: While it was believed unserved it kept printing "none: a prism stack does not fit across a
#: passage this narrow" and "graded refinement instead" to every reader of the console run page -
#: a claim about the customer's own geometry that this engine's delivered mesh, with 2.25 layers
#: in it, refutes. 8787 has served a stale COPY of a viewer script before now, so "which file is
#: actually served" is a known trap in this repository and is traced here rather than assumed.
VIEWERS = ("ui/js/viewer/viewer.js",
           "apps/console/public/static/js/viewer/viewer.js")
#: THE ONE THE BROWSER SUITE DRIVES. Kept as a name because tests/ui runs against this tree, not
#: because the other is unserved; every gate below holds BOTH.
SERVED = "ui/js/viewer/viewer.js"


#: EVERY LAYER FIGURE A READER CAN BE SHOWN, derived from the producers rather than typed out here.
#: Shared with the two browser guards in tests/ui, which held the same list and drifted the same
#: way - see tests/layer_keys for the measurement that forced it.
from tests.layer_keys import published_layer_keys as _published_layer_keys  # noqa: E402


def test_the_layer_count_reaches_the_payload():
    """Zero is an answer. Dropping it is not."""
    q = quality(MANIFEST)
    assert q["near_wall_layers"] == 0
    assert "near_wall_layers" in q, "a reading the customer asked for, filtered out of the payload"


def test_what_stands_in_for_the_layers_travels_with_the_zero():
    """A bare 0 reads as a failure. The graded levels and the cell size AT the wall are the
    near-wall resolution a reader asking about layers actually wants."""
    q = quality(MANIFEST)
    assert q["near_wall_levels"] == 3
    assert q["near_wall_cell_m"] == 1.96e-05


def test_the_interface_count_reaches_the_payload_from_both_sides():
    """Equal counts on the two sides ARE the conformality, and it is the one patch a conjugate
    mesh exists for. The parts panel could not show a face count for it at all."""
    q = quality(MANIFEST)
    assert q["interface_faces_fluid"] == q["interface_faces_solid"] == 346_456


def test_a_mesh_that_reports_none_of_this_is_unchanged():
    """Every single-region engine, and every conjugate run from before the engine measured it."""
    q = quality({"mesh_mode": "cfmesh", "mesh_units": "m", "cell_count": 10,
                 "quality": {"cells": 10}})
    for k in ("near_wall_layers", "near_wall_levels", "near_wall_cell_m",
              "interface_faces_fluid", "interface_faces_solid"):
        assert k not in q


def test_the_viewer_names_each_one_rather_than_dumping_it_under_other():
    """mesh_facts states the pairing: "Adding a metric is an edit here AND in the viewer's label
    table; that pairing is the point." Without the label the metric still reaches the user, in the
    catch-all "Other" group as `near wall layers`, which is not an answer to the question asked."""
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        assert "'Boundary layers'" in src, f"{rel} has no label for the layer count"
        assert "Cell at the wall" in src, f"{rel} does not show the wall's cell size"
        assert "At the wall" in src, f"{rel} has no group to put them in"
        for k in ("near_wall_layers", "near_wall_levels", "near_wall_cell_m",
                  "interface_faces_fluid"):
            assert k in src, f"{rel} does not mention {k}"
        # AND EVERY ONE THE BACKEND PUBLISHES ABOUT THE LAYER PASS ITSELF. A key this build has
        # no name for still reaches the user - in the catch-all "Other" group, labelled with its
        # own key spelled out - so "it reaches them" and "it is an answer" are different tests.
        # The console's copy knew NONE of these until 2026-11-17 while serving its own run page.
        #
        # READ OFF THE PRODUCER AND NOT TYPED OUT HERE, which is the whole point of this edit.
        # This list WAS twelve literal names, and so were the two in tests/ui - and when 0248dcfb
        # moved the engine's first layer floor onto `layer_kept_of_offered_pct` and published that
        # figure and `layer_offered_pct` beside coverage, all three lists stayed at twelve and all
        # three tests passed while the customer's panel drew both new figures in the Other group
        # as "layer offered pct 97.23". A guard against drift that has to be edited in step with
        # the thing it guards is not a guard. `finalize._LAYER_FIGURES` is the authority for what
        # travels into the manifest, and `mesh_facts._FLOAT_METRICS`/`_INT_METRICS` for what
        # survives into the payload, so the intersection is exactly what a reader can be shown.
        for k in _published_layer_keys():
            assert k in src, (
                f"{rel} has no name for {k}, so it draws it in the Other group under its raw key")


def test_the_zero_carries_its_reason_in_the_viewer():
    """"0" beside "Boundary layers" with nothing else on the row reads as a mesh that failed to
    grow them, so the row has to say why there are none."""
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        row = src[src.index("'Boundary layers'"):]
        row = row[:row.index("near_wall_cell_m")]
        assert "none" in row, f"{rel} shows a bare zero"
        assert len(row.strip()) > 120, f"{rel} does not say why there are none"


def test_the_row_makes_neither_of_the_two_claims_that_were_measured_false():
    """BOTH REASONS THIS ROW HAS CARRIED ARE REFUTED, and until 2026-11-17 this test REQUIRED the
    second one - it asserted `"fit" in row`, so the one gate over this row was pinning a claim the
    engine's own delivered mesh contradicts.

    The conformality claim fell on the coupon (14,477 of 14,477 interface faces matched to 4e-18 m
    with `optimiseLayer 0`). The fit claim fell on the part itself: what it measured was a stack
    109% of its own background cell, a dictionary defect `layers.fit_complaint` now refuses, and at
    60% of a cell the same part grew 2.84 of 3 layers over 95.553% of its interface. The customer
    plate shipped with 2.25. A row that tells this customer a prism stack cannot fit across their
    passages is telling them something untrue about a mesh that has one.

    HELD ON BOTH COPIES SINCE 2026-11-17, and that is the fix for a second defect. This test was
    scoped to `ui/` "because the apps/console path is a second copy nothing serves" - which is
    false (the chain is traced beside VIEWERS above), so the console's run page went on printing
    the fit claim after it was measured false and deleted here. The two files are hand-maintained
    mirrors with no build step between them and they had drifted 3,554 lines against 1,836.
    """
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        # FROM THE TOP OF THE BLOCK, not from the label: the ask and the flag are read into locals
        # before the row is built, so a slice starting at `'Boundary layers'` cannot see them. The
        # comment above `const wall=[]` quotes both deleted sentences to say why they are gone, and
        # it is deliberately outside this slice - what is under test is what the panel PRINTS.
        row = src[src.index("const wall=[];"):]
        row = row[:row.index("near_wall_cell_m")]
        for refuted in ("without its partner", "one side of a patch", "by design"):
            assert refuted not in row, (
                f"{rel} still tells the customer layers would break the interface, which was "
                f"measured false: {refuted!r}")
        for refuted in ("does not fit", "a passage this narrow", "to its limit"):
            assert refuted not in row, (
                f"{rel} still tells the customer a prism stack does not fit across their "
                f"passages, on a mesh this engine delivered with 2.25 layers in it: {refuted!r}")
        # and what it says instead is the payload: the ask and the flag that says whether the
        # figure beside them was measured at all
        assert "near_wall_layers_asked" in row and "near_wall_layers_measured" in row, (
            f"{rel} draws the layer count with neither the ask nor the measured flag on its row, "
            "so a reader cannot tell 3 grown from 3 requested")


def test_the_graded_cell_is_not_drawn_instead_of_a_stack_the_mesh_has():
    """"graded refinement INSTEAD" was true while this engine grew no layers, and is a different
    claim now that it grows them: the plate has 2.25 layers AND four levels of graded refinement
    at the same wall, so "instead" tells a reader one of the two is not there."""
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        row = src[src.index("const wall=[];"):]
        row = row[:row.index("interface_faces_fluid")]
        assert "graded refinement instead" not in row, (
            f"{rel} draws the graded cell at the wall as a substitute for a layer stack the "
            "delivered mesh has")
        assert "graded refinement:" in row, (
            f"{rel} no longer says how the wall was graded at all, which is the reading that "
            "stands beside the layer count rather than against it")


def test_the_two_served_copies_say_the_same_thing_about_layers():
    """THE GATE THAT WOULD HAVE CAUGHT IT. Both files are served (see VIEWERS), nothing builds one
    from the other, and the console's copy had fallen far enough behind to be printing a refuted
    claim and to know none of the twelve layer keys the backend publishes.

    WHY THIS IS A REGION AND NOT THE WHOLE FILE. Unifying the two trees is not this change's to
    make: they diverge in eight other files and in three stylesheets (`ui/css/theme.css`,
    `tokens.css` and `viewer.css` all differ from the console's), so copying 1,718 further lines
    of viewer across would reference classes the console's own css does not define. What IS held
    identical is the block that makes the claims - the layer rows, the shortfall rows, the key
    table they are drawn from and the group assembly that titles them. The next divergence over
    those fails here instead of shipping.
    """
    marks = (("    const show=v=>", "    const has=k=>"),
             ("    const FOLDED=", "    const KNOWN="),
             ("    /* AT THE WALL", "    const sizes=SIZE.filter("),
             ("    const group=(title,body)=>", "\n  })();"))
    a, b = (pathlib.Path(r).read_text() for r in VIEWERS)
    for start, end in marks:
        cut = lambda s: s[s.index(start):s.index(end, s.index(start) + len(start))]  # noqa: E731
        assert cut(a) == cut(b), (
            f"the two served viewer copies disagree over the block at {start.strip()!r}, so one "
            "of the two tells a customer something the other does not")
