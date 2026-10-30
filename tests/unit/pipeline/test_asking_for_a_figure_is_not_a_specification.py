"""Asking to SEE the layer count is a question about the mesh, not a specification for it.

Corintis asked, in the list of readings they want on a delivered mesh: "...quality metrics to know
if the mesh is of good quality, number of boundary layers in the fluid, min and max cell sizes..."

Read as a specification, that one clause cost us three things at once, measured on 2026-10-30
against their actual words:

    layer_request(their words)      -> requested=True
    plan_requirements(...)          -> ('prism_layers',)
    uncovered('cfmesh_conjugate')   -> ('prism_layers',)

which makes `review_delivery` label a mesh that passed every gate "no prism layers measured on the
wall", and makes `fitness.NO_LAYERS_VALUE` scale the conjugate engine's measured success rate by
0.7 - demoting the only engine that can mesh that part, because the customer asked to see a figure.

The comment on `_LAYER_WORDS` called a false positive harmless: "a false 'yes' only turns an
automatic switch into a question". It does not. It marks a good delivery as failing a requirement
and moves the recommender off the engine that works.
"""
from __future__ import annotations

import pytest

from meshpipeline.pipeline.engine_fallback import layer_request, plan_requirements, uncovered

#: Their own words, kept verbatim, because the point of this test is that THIS sentence is a
#: question and not a specification.
CORINTIS = ("We would like to know for example the mesh count, quality metrics to know if the "
            "mesh is of good quality, number of boundary layers in the fluid, min and max cell "
            "sizes, cell types and count per type, highlight of poor-quality cells for instance.")


def test_asking_for_the_layer_count_is_not_asking_for_layers():
    assert layer_request(CORINTIS).requested is False


@pytest.mark.parametrize("asked", [
    "number of boundary layers in the fluid",
    "how many boundary layers does it have",
    "please report the count of prism layers",
    "what quantity of inflation layers are on the wall",
    "the number of near-wall layers",
])
def test_every_way_of_asking_for_the_figure(asked):
    assert layer_request(asked).requested is False, asked


@pytest.mark.parametrize("asked,count", [
    ("please use 4 boundary layers on the wall", 4),
    ("8 prism layers at the wall please", 8),
    # BOTH IN ONE BRIEF. The reporting phrase is cut out and what is LEFT is asked again, so a
    # brief that asks for the figure AND specifies layers is still a specification.
    ("number of boundary layers in the fluid, and use 3 prism layers", 3),
])
def test_a_stated_count_is_still_a_specification(asked, count):
    got = layer_request(asked)
    assert got.requested is True, asked
    assert got.count == count, asked


@pytest.mark.parametrize("asked", [
    "we want prism layers at the wall",
    "the mesh should be wall-resolved",
    "keep y+ below 1",
    "give it inflation layers",
])
def test_asking_for_layers_is_still_asking_for_layers(asked):
    assert layer_request(asked).requested is True, asked


def test_their_question_leaves_the_conjugate_engine_covering_the_plan():
    """The consequence, not just the parse. With their words read correctly there is no
    prism_layers requirement, so the engine that can actually mesh their part is not marked as
    failing one."""
    from meshpipeline.engines.registry import get_spec

    spec = get_spec("cfmesh_conjugate")
    state = {"request_txt": CORINTIS, "review_brief_txt": "",
             "approved_engine": "cfmesh_conjugate"}
    req = plan_requirements(state, spec)
    assert "prism_layers" not in req, req
    assert uncovered("cfmesh_conjugate", req) == (), req
