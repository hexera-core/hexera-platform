# The verifier's OWN off-state emitter, written independently of check_vision_off_is_byte_identical.py.
# Prints a digest of everything a model is handed on the two paths a geometry flag could possibly touch,
# with EVERY geometry flag left at its shipped default. Nothing here sets a flag on.
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import types
from pathlib import Path
from types import SimpleNamespace

WS = Path(os.environ["VOFF_WS"])
DOC = json.loads(Path(os.environ["VOFF_DOC"]).read_text(encoding="utf-8")) if os.environ.get("VOFF_DOC") else None
ROW_STATE = json.loads(Path(os.environ["VOFF_ROW"]).read_text(encoding="utf-8")) if os.environ.get("VOFF_ROW") else None
SHA = str((DOC or {}).get("source_sha256") or "a" * 64)

SRC = SimpleNamespace(id="11111111-1111-4111-8111-111111111111", owner_id="owner-off",
                      object_key="uploads/11111111-1111-4111-8111-111111111111", sha256=SHA,
                      size_bytes=173563, original_filename="part.step", suffix_hint=".step")
TURNS = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
         {"role": "user", "content": "Internal flow of water through this part at 2 m/s. "
                                     "Mesh budget: keep the total cell count under 2 million cells."}]


def _d(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _no_geometry_flag_is_on() -> list[str]:
    """Every geometry flag, read off the policy module itself, must be falsy. Reported, not assumed."""
    import meshpipeline.settings.policy as polcfg
    on = []
    for name in dir(polcfg):
        if name.startswith("GEOMETRY_") and isinstance(getattr(polcfg, name), bool) and getattr(polcfg, name):
            on.append(name)
    return on


def _reads(monkeypatch) -> None:
    """The stored measurement and the stored survey row ARE THERE. Off, nothing may read them."""
    import meshpipeline.cad.regions as regions

    async def doc(_ref, _digest):
        return DOC

    async def none(_ref):
        return None

    for attr, fn in (("_stored_document", doc), ("_analysis_from_object_store", none)):
        if hasattr(regions, attr):
            monkeypatch.setattr(regions, attr, fn)
    try:
        from meshpipeline.application import geometry_survey as gs
    except ImportError:
        return

    async def row(*_a, **_k):
        return ROW_STATE

    async def kept(*_a, **_k):
        return True

    monkeypatch.setattr(gs, "load", row)
    if hasattr(gs, "save"):
        monkeypatch.setattr(gs, "save", kept)


def _intake_state():
    from meshpipeline.api.v1 import chat
    session = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222", messages=list(TURNS), geometry_source=SRC,
        request_txt="", review_brief_txt="", domain="", mesh_engine="", engine_params={},
        intake_patches=[], dimensionality="", purpose="", input_kind="")
    return chat._build_intake_state(session, "owner-off")


def test_verifier_off_emit(monkeypatch):
    on = _no_geometry_flag_is_on()
    print(f"VOFF flags_on {','.join(on) if on else 'NONE'}")
    _reads(monkeypatch)

    # ---- the intake model's system prompt and its tool list ----
    import meshpipeline.agents.intake.agent as agent
    from meshpipeline.contracts.model_inference import ModelRoundResult
    grab: dict = {}

    async def intake_call(**kw):
        grab.setdefault("intake_system", kw["messages"][0]["content"])
        grab.setdefault("intake_tools", json.dumps(kw.get("tools"), sort_keys=True))
        return ModelRoundResult(assistant_text="What fluid is it?", finish_reason="stop")

    monkeypatch.setattr(agent.llm_router, "call_intake_model", intake_call)
    asyncio.run(agent.node_intake(_intake_state()))

    # ---- the planner's user message, and the typed block it is handed beside it ----
    import meshpipeline.cad.analysis as analysis
    import meshpipeline.engines.snappy.drivers as drivers
    import meshpipeline.engines.snappy.planner as planner
    WS.mkdir(parents=True, exist_ok=True)
    (WS / "input.stl").write_text("solid x\nendsolid x\n", encoding="utf-8")
    monkeypatch.setattr(analysis, "analyze_surface",
                        lambda _s: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0, "min_feature": 0.01})
    monkeypatch.setattr(analysis, "recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2})
    monkeypatch.setattr(planner, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None), raising=False)

    async def planner_call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        grab["planner_user"] = messages[1]["content"]
        grab["planner_system"] = messages[0]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    monkeypatch.setattr(planner.llm_router, "call_planner_model", planner_call)
    from tests._geometry_support import prepared_surface
    kw = {"workspace": WS, "job_id": "off-job", "request_txt": "mesh it",
          "surface": prepared_surface(WS / "input.stl",
                                      source_id="11111111-1111-1111-1111-111111111111",
                                      interpretation_id="22222222-2222-2222-2222-222222222222")}
    block = None
    if "geometry_agent" in inspect.signature(planner.plan_with_accounting).parameters:
        st = _intake_state()
        st["request_txt"] = kw["request_txt"]
        if hasattr(drivers, "_planner_inputs"):
            got = asyncio.run(drivers._planner_inputs(st))
            kw["request_txt"], block = got[0], got[1]
        else:
            block = asyncio.run(drivers._agent_block(st))
        kw["geometry_agent"] = block
    asyncio.run(planner.plan_with_accounting(**kw))

    for label in ("intake_system", "intake_tools", "planner_user", "planner_system"):
        text = grab.get(label) or ""
        print(f"VOFF {label} {_d(text)} {len(text)}")
    # A BLOCK AT ALL is a leak with every flag off, so it is hashed as its own line rather than folded in.
    print(f"VOFF planner_block {'NONE' if block is None else _d(json.dumps(block, sort_keys=True, default=str))}")
    print(f"VOFF request_txt {_d(kw['request_txt'])} {len(kw['request_txt'])}")
