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

THE REASON IT USED TO PUT THERE WAS FALSE, which is what the last test here now holds. It said an
extruded prism layer is a cell inserted on ONE side of a patch, so a stack on the interface would
leave every face without its partner. Measured on the coupon, 2026-10-30: `generateBoundaryLayers`
with `optimiseLayer 0` kept all 14,477 interface faces identical on both sides to 4e-18 m, at
99.97% coverage. Layers do not break conformality.

What actually rules them out is that a prism stack does not FIT. In a 98 um passage at an
affordable cell size the stack takes the quality to checkMesh's own bar - non-orthogonality 13.82
before, 64.95 after, against a limit of 65 - and buys nothing, because across a channel that
narrow the cells ACROSS the channel are the near-wall resolution. That makes the cells-across
reading the real answer to the question they asked, and it is why the two are on the same panel.

A wrong reason on a customer's own mesh is worse than a bare zero: it is a claim about their
geometry that does not hold, and it also argued against a second run with layers on the manifolds,
where a stack does fit and would give them a real count.
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
    grow them, so the row has to say why there are none."""
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        row = src[src.index("'Boundary layers'"):]
        row = row[:row.index("near_wall_cell_m")]
        assert "none" in row, f"{rel} shows a bare zero"
        assert len(row.strip()) > 120, f"{rel} does not say why there are none"


def test_the_reason_is_the_one_that_was_measured_and_not_the_one_that_was_refuted():
    """THE CLAIM THIS FILE USED TO ASSERT WAS WRONG, and it was wrong in the direction that costs
    the customer: it told them layers are impossible on their mesh, when what is true is that they
    do not fit in THESE passages and do fit in the manifolds.

    `optimiseLayer 0` kept 14,477 of 14,477 interface faces matched to 4e-18 m. So the row must not
    claim conformality is the obstacle, and it must name the one that is - the fit, and the
    cells-across resolution that stands in for it.
    """
    for rel in VIEWERS:
        src = pathlib.Path(rel).read_text()
        row = src[src.index("'Boundary layers'"):]
        row = row[:row.index("near_wall_cell_m")]
        for refuted in ("without its partner", "one side of a patch", "by design"):
            assert refuted not in row, (
                f"{rel} still tells the customer layers would break the interface, which was "
                f"measured false: {refuted!r}")
        assert "fit" in row, f"{rel} does not say that a prism stack does not fit"
        assert "across" in row, f"{rel} does not say what stands in for the layers"
