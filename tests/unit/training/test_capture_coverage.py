# Responsibility: Verify every producer that should emit a capture event does, threading its collector through.
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.capture_authority import CAPTURE_OWNER
from tests.product_modes import set_modes

import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}



def _events(authority, job_id: str) -> list[dict]:
    return [{"event_type": r["name"], "payload": r["payload"], "attempt": r["attempt"]}
            for r in authority.trusted_operations(owner_id=CAPTURE_OWNER, job_id=job_id)]


# engine_select_run events  #

def test_pinned_engine_emits_event_with_user_source(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False); monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path)); set_modes(monkeypatch, collection=True)
    from meshpipeline.pipeline.engine_select import node_engine_select
    out = asyncio.run(node_engine_select({"engine": "snappy", "job_id": "j1"}))
    assert out == {}
    evs = _events(capture_authority, "j1")
    assert evs[0]["event_type"] == "engine_select_run"
    assert evs[0]["payload"] == {"chosen": "snappy", "source": "user",
                                 "model": None, "usage": None, "planner_required": True}


def test_dispute_pin_emits_event_with_dispute_source(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False); monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path)); set_modes(monkeypatch, collection=True)
    from meshpipeline.pipeline.engine_select import node_engine_select
    asyncio.run(node_engine_select({"engine": "snappy", "job_id": "j2",
                                    "user_dispute": {"of_job_id": "x"}}))
    assert _events(capture_authority, "j2")[0]["payload"]["source"] == "dispute"


def test_default_resolution_emits_event(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False); monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path)); set_modes(monkeypatch, collection=True)
    import meshpipeline.pipeline.engine_select as es
    out = asyncio.run(es.node_engine_select({"job_id": "j3", "request_txt": "wing",
                                             "geometry_source": _GEOMETRY_SOURCE}))
    from meshpipeline.engines.registry import default_engine
    assert out == {"engine": default_engine()}
    pl = _events(capture_authority, "j3")[0]["payload"]
    assert pl["chosen"] == default_engine() and pl["source"] == "default"
    assert pl["model"] is None                     # no model was consulted


def test_internal_purpose_force_emits_event(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False); monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path)); set_modes(monkeypatch, collection=True)
    from meshpipeline.engines.registry import default_engine, engines_producing_topology
    from meshpipeline.pipeline.engine_select import node_engine_select
    asyncio.run(node_engine_select({"job_id": "j4", "purpose": "internal_cfd"}))
    pl = _events(capture_authority, "j4")[0]["payload"]
    assert pl == {"chosen": default_engine(), "source": "topology_internal",
                  "model": None, "usage": None, "planner_required": False}
    assert pl["chosen"] in engines_producing_topology("internal")


# planner_run events  #

def test_planner_call_is_captured(capture_authority, monkeypatch, tmp_path):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", tmp_path, raising=False); monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path)); set_modes(monkeypatch, collection=True)
    import meshpipeline.engines.snappy.planner as pl

    # geometry analysis stubbed - this test is about CAPTURE, not measuring
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda p: {"diag": 1.0, "extent": [1, 1, 1],
                                   "surface_area": 6.0, "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda a, max_cells: {"surface_level": 5, "feature_level": 6,
                                              "max_cells": max_cells})
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "input.stl").write_text("solid s\nendsolid s\n")

    def _R():
        from meshpipeline.contracts.model_inference import ModelRoundResult
        return ModelRoundResult(
            assistant_text='plan: {"approach": "wrap", "quality": "production", '
                           '"n_layers": 5, "max_cells": 4000000}',
            finish_reason="stop", input_tokens=100, output_tokens=40)

    # The planner is its OWN logical role, so its capture test fakes the planner's operation -
    # not the builder's, which it merely happens to share a model with today.
    async def _reply(messages, tools=None, tool_choice=None, job_id="", user_id="", **_kw):
        return _R()
    import meshpipeline.adapters.model_inference.router as r
    monkeypatch.setattr(r, "call_planner_model", _reply)

    from tests._geometry_support import prepared_surface
    plan = asyncio.run(pl.make_mesh_plan(workspace=ws, job_id="jp",
                                         surface=prepared_surface(ws / "input.stl"),
                                         request_txt="wing mesh"))
    assert plan and plan["approach"] == "wrap"
    evs = _events(capture_authority, "jp")
    assert evs and evs[0]["event_type"] == "planner_run"
    payload = evs[0]["payload"]
    assert payload["status"] == "ok"
    assert payload["plan"]["n_layers"] == 5
    assert "system_snapshot" in payload and payload["system_snapshot"]
    assert payload["response_text"].startswith("plan:")
    assert payload["usage"] == {"prompt_tokens": 100, "completion_tokens": 40}


# intake-phase search capture  #

def test_web_search_collector_receives_record_without_job(monkeypatch):
    monkeypatch.setattr(provcfg, "WEB_SEARCH_ENABLED", False)   # no network needed
    from meshpipeline.agent_tools.shared.web_search import web_search
    collected: list = []
    out = asyncio.run(web_search("naca 0012 mesh sizing", collector=collected))
    assert "disabled" in out
    assert len(collected) == 1
    assert collected[0]["type"] == "web_search"
    assert collected[0]["payload"]["query"] == "naca 0012 mesh sizing"
    assert collected[0]["payload"]["status"] == "disabled"


def test_intake_tool_threads_the_collector(monkeypatch):
    monkeypatch.setattr(provcfg, "WEB_SEARCH_ENABLED", False)
    from meshpipeline.agents.intake.agent import _execute_intake_tool
    collected: list = []
    _execute_intake_tool("web_search", {"query": "domain sizing"}, collected)
    assert collected and collected[0]["payload"]["query"] == "domain sizing"


def test_intake_returns_search_events_to_the_dispatch_channel():
    # the dispatch-channel keys are emitted by turn.py, which owns every graph patch
    root = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "agents" / "intake"
    assert '"_intake_search_events": record.search_events' in (root / "turn.py").read_text()
    # ...and are WRITTEN by the intake message authority, which owns every durable consequence of
    # a turn since. The chat route no longer touches the event channel.
    written = (root / "message.py").read_text()
    assert "_intake_search_events" in written and "append_intake_event" in written


# dispute + export  #

def test_worker_emits_dispute_context_event():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "application"
           / "job_service.py").read_text()
    assert '"dispute_context"' in src
    assert '"parent_engine"' in src


def test_exporter_records_engine_and_ships_only_images_not_video():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "application" /
           "maintenance" / "export.py").read_text()
    # renders/ copies IMAGE suffixes only, so the derived .mp4 session video is left out
    assert '.png' in src and '.jpg' in src
    assert '"schema_version": "4.0"' in src                 # the job_summary record
    assert '"engine":' in src                                # sample.json carries it


def test_completeness_requires_engine_and_conditionally_planner():
    from meshpipeline.capture.events import _SECTION_FIELD_CHECKS, REQUIRED_EVENT_TYPES, _planner_required_for
    assert "engine_select_run" in REQUIRED_EVENT_TYPES
    assert "classifier_rag_fix_chunks" not in _SECTION_FIELD_CHECKS["classifier"][1]

    class _E:
        def __init__(self, payload): self.payload = payload
    # the engine_select producer DECLARES planner_required per run; capture reads it as data
    assert _planner_required_for({"engine_select_run": [_E({"chosen": "snappy", "planner_required": True})]})
    assert not _planner_required_for({"engine_select_run": [_E({"chosen": "cfmesh", "planner_required": False})]})


# engine selection publishes through the ownership-checked port; this suite is about capture.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.application.execution_publisher as _ep
    return install(monkeypatch, _ep)
