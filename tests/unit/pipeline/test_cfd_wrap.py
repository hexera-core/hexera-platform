"""The CFD ladder's last rung (pipeline/cfd_wrap, LAD-cfd-5): reached only when a flow run would end
with no mesh, never for a failure that is not the geometry's, once, and said beside the mesh."""
from __future__ import annotations

import asyncio
import time

import pytest

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.pipeline import cfd_wrap as cw
from meshpipeline.pipeline.graph import route_after_executor


def _failed(**kw) -> dict:
    st = {"job_id": "j", "purpose": "external_aero", "engine": "cfmesh",
          "engine_source": "suggested_confirmed", "executor_success": False,
          "retry_count": bcfg.MAX_BUILDER_RETRIES + 1, "executor_failed_gate": "finalize",
          "executor_failure_cause": "mesh_quality",
          "executor_failure_facts": {"fatal": ["incorrectly oriented faces"], "meshers_started": 1},
          "intake_patches": [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
          "engine_ladder": {"approved": "cfmesh", "source": "suggested_confirmed",
                            "attempts": [{"attempt": 1, "engine": "cfmesh", "kind": "engine"}]},
          "pipeline_deadline_epoch": time.time() + 10 * 3600}
    st.update(kw)
    return st


@pytest.fixture(autouse=True)
def _flow(monkeypatch):
    # the purpose catalogue is the engines' own; pin the flow the rung reads, not the catalogue
    monkeypatch.setattr(cw, "flow_of_state", lambda s: {"external_aero": "external",
                                                       "internal_flow": "internal"}.get(
                                                           s.get("purpose"), ""))
    monkeypatch.setattr(cw, "wrap_engine", lambda s: "" if s.get("engine") == "nowrap" else "snappy")


def test_a_run_that_would_end_with_no_mesh_goes_to_the_wrap():
    assert route_after_executor(_failed()) == "node_cfd_wrap"


def test_a_run_that_delivers_never_reaches_it():
    st = _failed(executor_success=True, requirement_caveats=[])
    assert route_after_executor(st) == "node_reviewer"


@pytest.mark.parametrize("cause", ["run_infrastructure", "domain_extent", "boundary_type",
                                   "contract_mismatch", "region_split", "review_incomplete"])
def test_a_failure_that_is_not_the_geometrys_gets_no_wrap(cause):
    assert route_after_executor(_failed(executor_failure_cause=cause)) == "__end__"


def test_the_rung_runs_once():
    st = _failed(cfd_wrap={"tried": True, "engine": "snappy"})
    assert route_after_executor(st) == "__end__"
    # ...and a failure on it ends the run even with attempts left
    st = _failed(cfd_wrap={"tried": True, "engine": "snappy"}, retry_count=1)
    assert route_after_executor(st) == "__end__"


def test_switched_off_dispute_and_no_engine_end_as_before(monkeypatch):
    import meshpipeline.settings.policy as pol
    assert route_after_executor(_failed(user_dispute={"why": "x"})) == "__end__"
    assert route_after_executor(_failed(engine="nowrap")) == "__end__"
    assert route_after_executor(_failed(purpose="structural_fea")) == "__end__"
    monkeypatch.setattr(pol, "CFD_WRAP_ENABLED", False)
    assert route_after_executor(_failed()) == "__end__"


def test_too_little_time_left_is_a_guard_clock():
    st = _failed(pipeline_deadline_epoch=time.time() + 5)
    assert cw.why_not(st) == "too little time left for the wrap rung"


def test_a_setup_the_engines_cannot_build_gets_no_wrap():
    st = _failed(geometry_unsuitable_reason="symmetry", executor_failure_cause="geometry_rejected",
                 executor_failure_facts={"phases": ["declared"]})
    assert "cannot build" in cw.why_not(st)
    st = _failed(geometry_unsuitable_reason="self-intersects",
                 executor_failure_cause="geometry_rejected",
                 executor_failure_facts={"phases": ["measured"], "before_meshing": True})
    assert cw.why_not(st) == ""


def test_the_node_starts_one_fresh_attempt_on_the_wrap_engine(monkeypatch):
    import meshpipeline.pipeline.engine_select as es

    async def _quiet(*a, **k):
        return None
    monkeypatch.setattr(es, "_publish", _quiet)
    st = _failed(geometry_unsuitable_reason="x")
    out = asyncio.run(cw.node_cfd_wrap(st))
    assert out["engine"] == "snappy" and out["builder_mode"] == "initial"
    assert out["geometry_unsuitable_reason"] == ""
    rec = out[cw.STATE_KEY]
    assert rec["tried"] and rec["rule"] == "LAD-cfd-5" and rec["after"] == ["cfmesh"]
    sw = out["engine_ladder"]["switches"][-1]
    assert sw["to"] == "snappy" and sw["changes"] == ["meshed on a wrapped surface"]


def test_the_request_is_written_only_on_the_rung(tmp_path):
    from meshpipeline.cad.surface_wrap import WRAP_REQUEST_FILE
    assert not cw.write_request(tmp_path, {})
    assert cw.write_request(tmp_path, {"cfd_wrap": {"tried": True, "flow": "internal"}})
    assert cw.read_request(tmp_path)["flow"] == "internal"
    assert not cw.write_request(tmp_path, {})
    assert not (tmp_path / WRAP_REQUEST_FILE).exists()


def test_the_delivery_says_how_far_the_wrap_lies_from_the_cad(tmp_path):
    import json

    from meshpipeline.application import final_result as fr
    from meshpipeline.cad.surface_wrap import WRAP_REPORT_FILE, summary_words
    rep = {"distance_m": 0.0002, "cube_m": 8e-5, "max_m": 0.0011, "p95_m": 0.0001,
           "max_on_cad_m": 0.0004, "bridged_spots": 3,
           "spots": [{"centre_m": [0.1, 0.2, 0.3], "patch": "body", "max_m": 0.0011, "area_m2": 1e-6}]}
    rep["words"] = summary_words(rep)
    (tmp_path / WRAP_REPORT_FILE).write_text(json.dumps(rep))
    st = {"cfd_wrap": {"tried": True, "engine": "snappy", "after": ["cfmesh"],
                       "failure": {"reason": "its mesh came out broken"}},
          "openfoam_workspace": str(tmp_path), "executor_success": True}
    out = fr.RunOutcome.from_graph_state(st)
    c = fr.wrapped_surface_caveat(out)
    assert c["kind"] == fr.WRAPPED_SURFACE and c["bridged_spots"] == 3
    text = "\n".join(cw.caveat_lines(c))
    assert "within 0.4 mm of your CAD everywhere except 3 bridged spots" in text
    assert "(100, 200, 300) mm on body" in text and "cfMesh" in text or "cfmesh" in text.lower()
    # a failed mesh carries no such note
    assert fr.wrapped_surface_caveat(fr.RunOutcome.from_graph_state(
        {**st, "executor_success": False})) is None
    # nor any mesh not built on the rung
    assert fr.wrapped_surface_caveat(fr.RunOutcome.from_graph_state(
        {**st, "cfd_wrap": {}})) is None


def test_the_mesh_report_lists_the_wrap():
    from meshpipeline.render.mesh_report import mesh_report_html
    html = mesh_report_html({"wrap": {"words": "meshed on a wrapped surface: within 1 mm",
                                      "distance_m": 0.001, "bridged_spots": 1,
                                      "spots": [{"centre_m": [0, 0, 0], "patch": "body",
                                                 "max_m": 0.002, "area_m2": 1e-6}]}})
    assert "Wrapped surface" in html and "Bridged at" in html
    assert "Wrapped surface" not in mesh_report_html({})
