# Responsibility: Verify the two events the run's live line adds - the plan and the mesher's own
# stage - say what they say in plain words, and that the engine is announced with why it was chosen.
# Boundaries: the event constructors, the plan's refinement words and the engine sentence; no stream.
from __future__ import annotations

import meshpipeline.events as E
from meshpipeline.engines.snappy.drivers import plan_refinements
from meshpipeline.pipeline.engine_select import engine_choice_words


def test_the_plan_is_one_plain_line():
    ev = E.plan("builder", cells=2_081_224, layers=5,
                refinements=plan_refinements(surface_level=3, wake=True, boxes=2)).wire()
    assert ev["type"] == "plan" and ev["stage"] == "builder"
    assert ev["summary"] == ("about 2.1M cells, 5 wall layers, surface refinement level 3, "
                             "finer cells in the wake, 2 refinement boxes")
    assert E.plan("builder", cells=850_000, layers=0).wire()["summary"] == "about 850k cells, no wall layers"
    assert E.plan("builder", layers=1).wire()["summary"] == "1 wall layer"


def test_an_internal_plan_says_the_cells_across_the_bore():
    assert plan_refinements(cells_across=24, near_wall=True) == ["about 24 cells across the bore",
                                                                 "a finer band near the wall"]


def test_the_mesher_stage_is_carried_as_it_was_said():
    ev = E.mesh_stage("builder", "layers", "adding wall layers").wire()
    assert (ev["type"], ev["key"], ev["label"]) == ("mesh_stage", "layers", "adding wall layers")
    assert {"plan", "mesh_stage"} <= E.EVENT_TYPES


def test_the_engine_is_named_by_its_label_with_why():
    assert engine_choice_words("cfmesh", "measured") == \
        "Mesh engine: cfMesh - it did best in our lab on shapes like this one"
    assert engine_choice_words("snappy", "default").startswith("Mesh engine: snappyHexMesh - ")
    assert engine_choice_words("snappy", "nobody") == "Mesh engine: snappyHexMesh"
