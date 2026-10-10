# Responsibility: Verify the fallback ladder walks the engines in the MEASURED order for the run's geometry - the
# order the recommendation ranked them in - after the engine the user picked, and falls back to the declared
# order only when nothing could be measured.
# Boundaries: pipeline/engine_fallback.py and pipeline/engine_select.py over a real STEP solid built here and a
# synthetic fitness table; no mesher runs.
from __future__ import annotations

import asyncio

import pytest

try:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests._geometry_support import interpretation_ref, source_ref

import meshpipeline.pipeline.engine_select as select_mod
from meshpipeline.cad.shape_traits import TRAITS_VERSION
from meshpipeline.contracts.geometry_source import MaterializedGeometry
from meshpipeline.engines import fitness as F
from meshpipeline.pipeline import engine_fallback as lad

INTERNAL = [{"name": "inlet", "type": "inlet", "diameter_mm": 20.0},
            {"name": "outlet", "type": "outlet", "diameter_mm": 20.0},
            {"name": "wall", "type": "wall"}]
TUBE = {"flow": "internal", "form": "cad", "input_kind": "fluid-domain", "n_triangles": 100,
        "closed": True, "genus": 0, "ports": 2, "gap_vs_port": 1.0, "slenderness": 9.0}


def _tube_state(tmp_path, **kw) -> dict:
    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 10.0, 200.0).Shape()
    path = tmp_path / "tube.step"
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    ref = source_ref(local_file=path, filename="tube.step")
    mg = MaterializedGeometry(ref=ref, interpretation=interpretation_ref(geometry_source_id=ref.source_id),
                              local_path=path)
    s = {"job_id": "j", "engine": "", "engine_source": lad.SOURCE_SYSTEM, "purpose": "internal_cfd",
         "input_kind": "fluid-domain", "dimensionality": "3D", "geometry": mg.to_state(),
         "intake_patches": [dict(p) for p in INTERNAL], "engine_params": {},
         "request_txt": "Internal flow of water through a pipe.", "review_brief_txt": "",
         "retry_count": 0, "engine_ladder": {}}
    s.update(kw)
    return s


@pytest.fixture
def gmsh_best(monkeypatch):
    """A table in which Gmsh passed every tube and the others failed most: the measured order for a
    tube is then Gmsh first, against the declared cfMesh-first order."""
    rows = []
    for i in range(5):
        for engine, ok in (("gmsh", True), ("snappy", i < 3), ("cfmesh", i < 1), ("vmtk", i < 2)):
            rows.append({"shape": f"t{i}", "case": f"t{i}", "engine": engine, "format": "step",
                         "status": "pass" if ok else "fail", "traits": TUBE})
    table = F.table_from({"version": "test", "traits_version": TRAITS_VERSION, "rows": rows})
    monkeypatch.setattr(F, "committed_table", lambda: table)
    return table


def test_without_a_measured_order_the_declared_order_stands():
    assert lad.fallback_order("internal") == lad.fallback_order("internal", {"engine_ladder": {}})
    assert lad.measured_ranking({}) == []


def test_the_measured_order_leads_and_engines_it_did_not_rank_keep_their_declared_place():
    st = {"engine_ladder": {"order": ["vmtk", "snappy"]}}
    assert lad.fallback_order("internal", st) == ("vmtk", "snappy", "cfmesh", "gmsh")


def test_the_run_measures_its_geometry_and_ranks_the_engines_that_can_build_it(tmp_path, gmsh_best):
    info = lad.measure_order(_tube_state(tmp_path))
    assert info["order_source"] == "measured"
    assert info["order"][0] == "gmsh" and info["order_recommended"] == "gmsh"
    assert set(info["order"]) == set(lad.able_engines(_tube_state(tmp_path)))
    assert info["order_shape"] == "a long passage about as wide as its openings"
    assert info["order_reasons"]["gmsh"].startswith("passed 5 of 5 similar shapes")
    assert info["order_table"] == "test"


def test_a_run_with_no_geometry_or_no_flow_gets_no_order(tmp_path, gmsh_best):
    assert lad.measure_order({"purpose": "internal_cfd"}) == {}
    assert lad.measure_order({**_tube_state(tmp_path), "purpose": ""}) == {}


def test_a_run_nobody_chose_an_engine_for_takes_the_measured_recommendation(tmp_path, gmsh_best,
                                                                           monkeypatch):
    events = []

    async def _pub(job_id, text, op_id):
        return None

    monkeypatch.setattr(select_mod, "_publish", _pub)
    monkeypatch.setattr(select_mod, "_log_selection", lambda j, p, op_id="engine-select": events.append(p))
    out = asyncio.run(select_mod.node_engine_select(_tube_state(tmp_path)))
    assert out["engine"] == "gmsh"
    assert out["engine_ladder"]["order"][0] == "gmsh"
    assert events[0]["source"] == "measured" and events[0]["chosen"] == "gmsh"


def test_the_users_engine_stays_first_and_the_measured_order_follows_it(tmp_path, gmsh_best, monkeypatch):
    monkeypatch.setattr(select_mod, "_log_selection", lambda *a, **k: None)
    st = _tube_state(tmp_path, engine="snappy", engine_source=lad.SOURCE_USER)
    out = asyncio.run(select_mod.node_engine_select(st))
    assert "engine" not in out                                   # the pin is never changed
    st = {**st, **out}
    rungs = [r.engine for r in lad.ladder(st)]
    assert rungs[0] == "snappy"
    assert rungs[1:] == [e for e in out["engine_ladder"]["order"] if e != "snappy"]
    assert rungs[1] == "gmsh"


def test_the_closing_offer_names_the_best_measured_engine_not_the_first_declared(tmp_path, gmsh_best):
    # cfMesh and then snappyHexMesh failed. With no engine left that builds the same mesh (both
    # hex engines tried; cfMesh delivers what snappy does), the declared order would offer VMTK
    # next; the measured order for this tube offers the engine with the best record on tubes.
    st = _tube_state(tmp_path, engine="snappy", engine_source=lad.SOURCE_USER, retry_count=2,
                     executor_success=False, executor_failed_gate="finalize",
                     executor_failure_cause="engine_crashed",
                     engine_ladder={"attempts": [{"attempt": 1, "engine": "cfmesh",
                                                  "kind": lad.ENGINE, "cause": "finalize"}],
                                    "switches": [{"attempt": 2, "from": "cfmesh",
                                                  "to": "snappy"}]})
    plain = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    st["engine_ladder"] = {**st["engine_ladder"], **lad.measure_order(st)}
    measured = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    assert plain["engine"] == "vmtk", plain
    assert measured["engine"] == "gmsh", measured
    assert 'Reply "use Gmsh"' in measured["text"]


def test_an_order_already_on_the_record_is_not_measured_again(tmp_path, gmsh_best):
    st = _tube_state(tmp_path, engine_ladder={"order": ["cfmesh"]})
    assert asyncio.run(lad.seed_measured_order(st)) == {}


def test_a_measurement_that_does_not_finish_in_time_leaves_the_declared_order(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(lad, "measure_order", lambda state: time.sleep(0.5) or {"order": ["gmsh"]})
    assert asyncio.run(lad.seed_measured_order(_tube_state(tmp_path), timeout_s=0.05)) == {}
