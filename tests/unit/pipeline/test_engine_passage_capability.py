# Responsibility: Verify an engine is recommended, switched to and offered only when its declared passage capability covers the case, and that a case no other engine covers gets an honest out.
"""SEPARATE PASSAGES AND THE ENGINES THAT MESH THEM.

The round-3 sweep (2026-10-18) had 11 dev cases whose openings lie on several passages that do
not connect (two coronary trees, an AAA phantom's 5 and 9 outlet groups, a hotend nozzle). cfMesh
meshes them side by side (one cartesianMesh run per passage, merged); when its mesh failed, the
ladder moved to snappyHexMesh "which delivers the same approved mesh" - and snappy's driver, which
fills only the passage its seed point is in, refused every one before it ran. These tests hold
each engine to its declared passage count (FlowSupport.max_passages), everywhere an engine is
chosen: the recommender, the ladder's rungs, the switch, the offer a failed run ends with.
"""
from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.cad.fluid_passages import (
    MAX_PASSAGES,
    PASSAGES_READ_FILE,
    passage_count,
    staged_passage_count,
)
from meshpipeline.cad.shape_traits import ShapeTraits
from meshpipeline.engines.capability import covers_passages, engines_covering, ladder_order
from meshpipeline.engines.registry import engine_names, get_spec
from meshpipeline.pipeline import engine_fallback as lad

INTERNAL = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "outlet_2", "type": "outlet"}, {"name": "wall", "type": "wall"}]


def _cfmesh_broke(passages: int | None = None, **kw) -> dict:
    """cfMesh, recommended and confirmed, whose first attempt came out broken - the shape of
    every one of the 11 sweep cases at the moment the ladder decided."""
    rec = {"approved": "cfmesh", "source": lad.SOURCE_SUGGESTED,
           "order": ["cfmesh", "snappy", "vmtk", "gmsh"]}
    if passages is not None:
        rec.update(passages=passages, passages_source=lad.PASSAGES_STAGED)
    s = {"job_id": "j", "engine": "cfmesh", "engine_source": lad.SOURCE_SUGGESTED,
         "purpose": "internal_cfd", "input_kind": "body-surface", "dimensionality": "3D",
         "intake_patches": [dict(p) for p in INTERNAL], "engine_params": {},
         "request_txt": "Internal CFD mesh of the part: water through it at 5 m/s.",
         "review_brief_txt": "", "retry_count": 1, "executor_success": False,
         "executor_failed_gate": "finalize", "executor_failure_cause": "mesh_quality",
         "executor_failure_facts": {"fatal": ["incorrectly oriented faces"], "meshers_started": 1},
         "builder_deadline_epoch": 0.0, "pipeline_deadline_epoch": 0.0,
         "geometry": {"ref": {"original_filename": "phantom.stl", "suffix_hint": ".stl"}},
         "engine_ladder": rec}
    s.update(kw)
    return s


@pytest.fixture
def ladder_on(monkeypatch):
    import meshpipeline.pipeline.engine_select as es

    said: list[str] = []

    async def _pub(job_id, text, op_id):
        said.append(text)

    monkeypatch.setattr(es, "_publish", _pub)
    monkeypatch.setattr(es, "_log_selection", lambda *a, **k: None)
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 2)
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", True)
    return said


# the declarations

def test_every_internal_engine_declares_how_many_passages_one_job_meshes():
    declared = {n: get_spec(n).max_passages_for("internal") for n in engine_names()
                if get_spec(n).max_passages_for("internal") is not None}
    assert set(declared) == set(ladder_order("internal"))
    # cfMesh meshes every passage side by side, up to what one mesh holds (its driver's own limit);
    # snappy fills the one its seed point is in; VMTK and gmsh fill one lumen / one volume
    assert declared == {"cfmesh": MAX_PASSAGES, "snappy": 1, "vmtk": 1, "gmsh": 1}


def test_the_snappy_refusal_names_the_engines_the_declarations_name():
    from meshpipeline.engines.snappy.drivers import _meshes_them
    assert _meshes_them(2) == ("Mesh it with cfMesh, which meshes separate passages side by side, "
                               "or ")
    assert _meshes_them(MAX_PASSAGES + 1) == "Or "        # past every engine: one file per passage


@pytest.mark.parametrize("n, flow, expect", [
    (None, "internal", {"cfmesh", "snappy", "vmtk", "gmsh"}),     # not counted: nothing ruled out
    (1, "internal", {"cfmesh", "snappy", "vmtk", "gmsh"}),
    (2, "internal", {"cfmesh"}),
    (MAX_PASSAGES, "internal", {"cfmesh"}),
    (MAX_PASSAGES + 1, "internal", set()),
    (5, "external", {"cfmesh", "snappy", "gmsh"}),                # passages are an internal fact
])
def test_the_capability_filter(n, flow, expect):
    order = ladder_order(flow)
    assert set(engines_covering(order, flow, n)) == expect
    assert all(covers_passages(get_spec(e), flow, n) == (e in expect) for e in order)


# the count: measured before the run, read off every attempt's own staging

def _two_tubes(scale: float = 1.0) -> tuple[np.ndarray, list[dict]]:
    from meshpipeline.cad.stl_io import _box_triangles

    def tube(x0):                     # an open square tube 10 x 10 x 40 mm, open at both ends
        return _box_triangles((x0, 0, 0), (x0 + 0.01, 0.01, 0.04))[:8]

    def port(name, role, x, z):
        return {"name": name, "type": role, "near_mm": [x, 5.0, z], "width_mm": 10.0,
                "height_mm": 10.0}

    tris = np.asarray(tube(0.0) + tube(0.03), dtype=float) * scale
    return tris, [port("inlet", "inlet", 5, 0), port("outlet", "outlet", 5, 40),
                  port("outlet_2", "outlet", 35, 0), port("outlet_3", "outlet", 35, 40),
                  {"name": "wall", "type": "wall"}]


def test_the_staging_counts_two_tubes_that_do_not_touch_as_two_passages():
    tris, patches = _two_tubes()
    assert passage_count(tris, patches) == 2
    assert passage_count(tris[:8], patches[:2] + patches[-1:]) == 1
    assert passage_count(tris, patches[:1]) is None                # one opening: nothing to count


def test_the_shape_traits_carry_the_passage_count(tmp_path):
    from meshpipeline.cad.shape_traits import measure_file
    from meshpipeline.cad.stl_io import write_stl_binary

    tris, patches = _two_tubes(scale=1000.0)                       # the file is in millimetres
    path = tmp_path / "two_tubes.stl"
    write_stl_binary(path, [tuple(map(tuple, t)) for t in tris.tolist()])
    traits = measure_file(path, flow="internal", input_kind="body-surface", patches=patches,
                          surface_unit_to_m=0.001)
    assert traits.passages == 2
    assert measure_file(path, flow="external", patches=patches).passages is None


def test_an_attempts_own_staging_is_the_count_the_ladder_keeps(tmp_path):
    assert staged_passage_count(tmp_path) is None
    (tmp_path / PASSAGES_READ_FILE).write_text(json.dumps(
        {"passages": [{"ports": ["inlet"]}, {"ports": ["outlet_1"]}], "read": True}))
    assert staged_passage_count(tmp_path) == 2
    st = lad.with_staged_passages({"openfoam_workspace": str(tmp_path),
                                   "engine_ladder": {"passages": 1, "passages_source": "measured"}})
    assert lad.passages_of(st) == 2 and st["engine_ladder"]["passages_source"] == lad.PASSAGES_STAGED
    (tmp_path / PASSAGES_READ_FILE).write_text(json.dumps({"passages": [], "read": False}))
    assert staged_passage_count(tmp_path) is None


# the recommender

def test_the_recommender_ranks_only_engines_that_mesh_the_passages():
    from meshpipeline.engines.fitness import recommend
    able = ["cfmesh", "snappy", "vmtk", "gmsh"]
    several = ShapeTraits(flow="internal", form="cad", passages=2)
    assert recommend(several, able).ranking == ["cfmesh"]
    one = ShapeTraits(flow="internal", form="cad", passages=1)
    assert set(recommend(one, able).ranking) == set(able)
    # past every engine's declaration nothing is hidden: the run's own refusal says what to do
    many = ShapeTraits(flow="internal", form="cad", passages=MAX_PASSAGES + 1)
    assert set(recommend(many, able).ranking) == set(able)


# the ladder

def test_without_a_count_the_ladder_is_as_it_was():
    st = _cfmesh_broke()
    assert [r.engine for r in lad.ladder(st)][:2] == ["cfmesh", "snappy"]
    assert lad.decide(st, record=lad.with_attempt(st, lad.classify(st)), now=0.0).switch_to == "snappy"


def test_an_engine_that_meshes_one_passage_is_no_rung_for_a_case_of_two():
    st = _cfmesh_broke(passages=2)
    assert [r.engine for r in lad.ladder(st)] == ["cfmesh"]
    assert "snappy" not in lad.able_engines(st)
    # every other engine takes this STL for internal flow - and meshes one passage per job
    assert lad.held_back_by_passages(st, exclude=("cfmesh",)) == ["snappy", "vmtk", "gmsh"]
    d = lad.decide(st, record=lad.with_attempt(st, lad.classify(st)), now=0.0)
    assert not d.switch and d.why == "no untried engine meshes 2 separate passages in one job"
    assert lad.moves_on_from_broken_mesh(st, now=0.0) == ""


def test_the_node_reads_the_attempts_staging_and_stays_on_cfmesh(tmp_path, ladder_on):
    (tmp_path / PASSAGES_READ_FILE).write_text(json.dumps(
        {"passages": [{"ports": ["inlet", "outlet"]}, {"ports": ["outlet_2"]}], "read": True}))
    out = asyncio.run(lad.node_engine_fallback(_cfmesh_broke(openfoam_workspace=str(tmp_path))))
    assert "engine" not in out and not ladder_on                   # no switch, nothing announced
    assert out["engine_ladder"]["passages"] == 2
    assert out["engine_ladder"]["passages_source"] == lad.PASSAGES_STAGED


def test_a_snappy_run_on_a_case_of_two_still_moves_to_cfmesh():
    # the other way round: the engine that refuses is the first one, and the one that meshes both
    # passages is the rung - same as before
    st = _cfmesh_broke(passages=2, engine="snappy", executor_failure_cause="not_built",
                       executor_failure_facts={"meshers_started": 0})
    st["engine_ladder"]["approved"] = "snappy"
    d = lad.decide(st, record=lad.with_attempt(st, lad.classify(st)), now=0.0)
    assert d.switch_to == "cfmesh"


# the offer a failed run ends with

def test_the_offer_says_why_no_engine_was_switched_to_and_gives_both_outs():
    st = _cfmesh_broke(passages=2, retry_count=3)
    rec = lad.final_record(st, succeeded=False, system_failure=False)
    o = rec["offer"]
    assert o["kind"] == "passages" and o["engine"] == "cfmesh" and o["reply"] == "use cfMesh"
    assert o["held_back"] == ["snappy", "vmtk", "gmsh"] and o["passages"] == 2
    text = o["text"]
    assert text.startswith("cfMesh could not mesh this shape: its mesh came out broken")
    assert "2 separate passages that do not connect" in text
    assert ("snappyHexMesh, VMTK and Gmsh mesh only one passage per job, so I did not switch to "
            "them") in text
    assert "one file per passage" in text and 'reply "use cfMesh"' in text


def test_past_every_engines_limit_the_out_is_one_file_per_passage():
    n = MAX_PASSAGES + 1
    o = lad.final_record(_cfmesh_broke(passages=n, retry_count=3), succeeded=False,
                         system_failure=False)["offer"]
    assert o["kind"] == "passages" and o["engine"] == "" and o["reply"] == ""
    assert f"lie on {n} separate passages" in o["text"] and "The way on:" in o["text"]
    assert "use cfMesh" not in o["text"] and "one file per passage" in o["text"]


def test_a_case_of_one_passage_keeps_the_offer_it_had():
    o = lad.final_record(_cfmesh_broke(passages=1, retry_count=3), succeeded=False,
                         system_failure=False)["offer"]
    assert o["kind"] == "engine" and o["engine"] == "snappy"


# the start-of-run log

def test_a_confirmed_recommendation_is_not_logged_as_the_users_pin(caplog, monkeypatch):
    import meshpipeline.pipeline.engine_select as es

    async def _no_order(state, engine):
        return {}

    monkeypatch.setattr(es, "_measured_ladder", _no_order)
    monkeypatch.setattr(es, "_log_selection", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger=es.logger.name):
        asyncio.run(es.node_engine_select({"job_id": "j", "engine": "cfmesh",
                                           "engine_source": lad.SOURCE_SUGGESTED}))
        asyncio.run(es.node_engine_select({"job_id": "j", "engine": "cfmesh",
                                           "engine_source": lad.SOURCE_USER}))
    lines = [r.getMessage() for r in caplog.records if "pinned" in r.getMessage()]
    assert "engine 'cfmesh' pinned (recommended, confirmed by the user)" in lines[0]
    assert "engine 'cfmesh' pinned (user named it)" in lines[1]


def test_a_fewer_layers_offer_says_why_no_other_engine_was_tried():
    st = _cfmesh_broke(passages=2, retry_count=3,
                       request_txt="Internal CFD mesh, 5 prism layers on the wall.")
    o = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    assert o["kind"] == "fewer_layers"
    assert o["text"].endswith("(No other engine was tried: the openings lie on 2 separate "
                             "passages, and snappyHexMesh, VMTK and Gmsh mesh only one passage "
                             "per job.)")


# THE REPLAY: the 14 dev cases of the round-3 priority sweep's mesher_failed:snappy cluster, as the
# ladder saw them after cfMesh's first attempt (sweeps/round3-priority/results; dev side only).
# (case, what the user confirmed the file is, cfMesh's attempt-1 cause, its fatal defects, the
# passages the run's own staging read - None where snappy did run: one passage, or not read)
_SWEEP = [
    ("aaa_piv_phantoms/Neg_X_Side.stl", "body-surface", "mesh_quality",
     ["incorrectly oriented faces"], 5),
    ("aaa_piv_phantoms/Pos_Z_Side.stl", "body-surface", "patch_not_captured", [], 9),
    ("carotid_nonstenotic_plaques/right-04-icas.stl", "fluid-domain", "cell_budget", [], 2),
    ("gyroid_tumour_perfusion_phantoms/4.Gyroid_dualinput.stl", "body-surface", "mesh_quality",
     ["negative-volume cells", "incorrectly oriented faces"], 2),
    ("peripheral_vascular_models_vtp/model_6.vtp", "body-surface", "engine_crashed", [], None),
    ("peripheral_vascular_models_vtp/model_9.vtp", "body-surface", "engine_crashed", [], None),
    ("vmr_0249_h_coro_cad/Diseased_1.vtp", "fluid-domain", "mesh_quality",
     ["negative-volume cells", "open cells", "incorrectly oriented faces"], 2),
    ("vmr_0252_h_coro_cad/Diseased_4.vtp", "fluid-domain", "engine_crashed", [], 2),
    ("vmr_0257_h_coro_cad/Diseased_9.vtp", "fluid-domain", "engine_crashed", [], 2),
    ("vmr_0258_h_coro_cad/Diseased_10.vtp", "fluid-domain", "engine_crashed", [], 2),
    ("vmr_0263_h_coro_cad/Diseased_15.vtp", "fluid-domain", "engine_crashed", [], 2),
    ("vmr_0272_h_coro_h/Normal_4.vtp", "fluid-domain", "engine_crashed", [], None),
    ("vmr_0288_h_coro_h/Normal_20.vtp", "fluid-domain", "mesh_quality",
     ["negative-volume cells", "open cells", "incorrectly oriented faces"], 2),
    ("fclib_hotend_nozzle/CatNozzle.step", "body-surface", "patch_not_captured", [], 2),
]


@pytest.mark.parametrize("case, kind, cause, fatal, passages", _SWEEP, ids=[c[0] for c in _SWEEP])
def test_the_round3_cases_never_move_to_an_engine_that_refuses_them(case, kind, cause, fatal,
                                                                     passages):
    st = _cfmesh_broke(passages=passages, input_kind=kind, executor_failure_cause=cause,
                       executor_failure_facts={"fatal": fatal, "meshers_started": 1},
                       request_txt="Internal CFD mesh of the part, 5 prism layers on the wall.",
                       geometry={"ref": {"original_filename": case.split("/")[-1],
                                         "suffix_hint": "." + case.rsplit(".", 1)[-1]}})
    d = lad.decide(st, record=lad.with_attempt(st, lad.classify(st)), now=0.0)
    if passages is None:
        # one passage (snappy did run on these): the ladder is as it was
        assert d.switch_to == "snappy"
        return
    # a broken or absent mesh, and a measured check missed once, are all stopped by the passages:
    # no untried engine meshes them in one job, so cfMesh's own levers are all the run has left
    assert not d.switch and "separate passages" in d.why, d.why
    o = lad.final_record({**st, "retry_count": 9}, succeeded=False, system_failure=False)["offer"]
    assert o["kind"] in ("passages", "fewer_layers") and "separate passages" in o["text"]
    assert o.get("engine") in ("cfmesh", "") and "use snappyHexMesh" not in o["text"]
