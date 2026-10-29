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

THE ANSWER IS A DELIBERATE ZERO and that is why it cannot be shipped alone. A conjugate interface
is conformal because both regions are the same cells from the same octree; an extruded prism layer
is a new cell inserted on ONE side of a patch, so a layer stack on the interface would leave every
face without its partner, which is the single property the whole approach exists to guarantee
(cfmesh_conjugate/finalize.NEAR_WALL_LAYERS says it at length). What the mesh has instead is graded
octree refinement toward the wall. So the three numbers travel together or none of them means
anything, and the viewer puts the reason on the row.
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

VIEWERS = ("ui/js/viewer/viewer.js",
           "apps/console/public/static/js/viewer/viewer.js")


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


def test_the_zero_carries_its_reason_in_the_viewer():
    """"0" beside "Boundary layers" with nothing else on the row reads as a mesh that failed to
    grow them."""
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        assert "none by design" in src, f"{rel} shows a bare zero"
        assert "without its partner" in src, f"{rel} does not say why there are none"
