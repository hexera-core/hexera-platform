# Prints a digest of everything the intake model and the mesh planner are handed for ONE stored corpus
# measurement, through the real paths. Run in two checkouts by `check_flags_gone_changed_nothing.py`:
# the pre-cleanup tree with every geometry flag ON, and this one, which has no geometry flag at all.
#
# IT SAYS WHERE ITS IMPORTS CAME FROM before it prints a digest, and refuses otherwise. This machine has
# one venv with `meshpipeline` installed from the live tree and one with `geometry_agent` installed from
# another, so a harness that only sets PYTHONPATH can measure a tree it was never pointed at. That has
# cost this project three times. It is an assertion here, not a comment.
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import types
from pathlib import Path
from types import SimpleNamespace

WS = Path(os.environ["FG_WS"])
TREE = os.environ["FG_TREE"]
AGENT = os.environ["FG_AGENT"]
DOC = json.loads(Path(os.environ["FG_DOC"]).read_text(encoding="utf-8"))
_ROW = os.environ.get("FG_ROW") or ""
ROW_STATE = json.loads(Path(_ROW).read_text(encoding="utf-8")) if _ROW else None
FLAGS = [f for f in (os.environ.get("FG_FLAGS") or "").split(",") if f]
#: With no flags named: `gone` means this tree must carry no geometry gate at all, and `off` means it
#: still carries them and they are left at their shipped defaults. The second is how the reference tree
#: is asked what the flags were worth, which is the only thing that proves this gate can see them.
EXPECT = (os.environ.get("FG_EXPECT") or "gone").strip()

SHA = str(DOC.get("source_sha256") or ((DOC.get("source") or {}).get("sha256")) or "a" * 64)
SRC = SimpleNamespace(id="11111111-1111-4111-8111-111111111111", owner_id="owner-gate",
                      object_key="uploads/11111111-1111-4111-8111-111111111111", sha256=SHA,
                      size_bytes=173563, original_filename="part.step", suffix_hint=".step")
TURNS = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
         {"role": "user", "content": "Internal flow of water through this part at 2 m/s. "
                                     "Mesh budget: keep the total cell count under 2 million cells."}]


def _d(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _provenance() -> None:
    """Which meshpipeline and which geometry_agent this process actually imported. Asserted, not hoped."""
    import geometry_agent

    import meshpipeline
    mine, theirs = str(Path(meshpipeline.__file__).resolve()), str(Path(geometry_agent.__file__).resolve())
    print(f"FG where meshpipeline {mine}")
    print(f"FG where geometry_agent {theirs}")
    assert mine.startswith(str(Path(TREE).resolve())), f"meshpipeline came from {mine}, not {TREE}"
    assert theirs.startswith(str(Path(AGENT).resolve())), f"geometry_agent came from {theirs}, not {AGENT}"


def _flags(monkeypatch) -> None:
    """Turn the named flags on, or, with none named, prove there is no geometry gate left to turn on."""
    import meshpipeline.settings.policy as polcfg

    if not FLAGS:
        # NO EXEMPTIONS. This list used to exclude GEOMETRY_MEASURED_STOPS_ENABLED and
        # GEOMETRY_FLUID_SIDE_ENABLED as "settings, not gates". They outlived their readers inside the package
        # and this check, whose whole job is to prove no geometry gate is left, was the one place that would
        # have caught it and was told not to look. Both are retired now and nothing is excluded.
        left = [n for n in dir(polcfg) if n.startswith("GEOMETRY_") and n.endswith("_ENABLED")]
        print(f"FG gates {','.join(left) if left else 'NONE'}")
        if EXPECT == "off":
            assert left, "this tree carries no geometry gate, so it cannot stand in for the one that did"
            on = [n for n in left if getattr(polcfg, n)]
            assert not on, f"a geometry gate is on in a tree asked for its off state: {on}"
            return
        assert not left, f"this tree still carries geometry gates: {left}"
        return
    for name in FLAGS:
        assert hasattr(polcfg, name), f"{name} is not a setting in this tree, so this case proves nothing"
        monkeypatch.setattr(polcfg, name, True)
    print(f"FG gates {','.join(FLAGS)}")


def _reads(monkeypatch) -> None:
    """The stored measurement and the stored survey row, answered from the fixtures this gate supplies."""
    import meshpipeline.cad.regions as regions

    async def doc(_ref, _digest):
        return DOC

    async def none(_ref):
        return None

    for attr, fn in (("_stored_document", doc), ("_analysis_from_object_store", none)):
        if hasattr(regions, attr):
            monkeypatch.setattr(regions, attr, fn)
    from meshpipeline.application import geometry_survey as gs

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
    return chat._build_intake_state(session, "owner-gate")


def test_emit(monkeypatch):
    _provenance()
    _flags(monkeypatch)
    _reads(monkeypatch)

    # ---- what the intake model is handed: its system prompt and the exact tool list ----
    import meshpipeline.agents.intake.agent as agent
    from meshpipeline.contracts.model_inference import ModelRoundResult
    grab: dict = {}

    async def intake_call(**kw):
        grab.setdefault("intake_system", kw["messages"][0]["content"])
        grab.setdefault("intake_tools", json.dumps(kw.get("tools"), sort_keys=True))
        return ModelRoundResult(assistant_text="What fluid is it?", finish_reason="stop")

    monkeypatch.setattr(agent.llm_router, "call_intake_model", intake_call)
    asyncio.run(agent.node_intake(_intake_state()))

    # ---- what the mesh planner is handed: both its messages, its request and its typed block ----
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
        grab["planner_system"] = messages[0]["content"]
        grab["planner_user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    monkeypatch.setattr(planner.llm_router, "call_planner_model", planner_call)
    from tests._geometry_support import prepared_surface
    kw = {"workspace": WS, "job_id": "flags-gone", "request_txt": "mesh it",
          "surface": prepared_surface(WS / "input.stl",
                                      source_id="11111111-1111-1111-1111-111111111111",
                                      interpretation_id="22222222-2222-2222-2222-222222222222")}
    st = _intake_state()
    st["request_txt"] = kw["request_txt"]
    got = asyncio.run(drivers._planner_inputs(st))
    kw["request_txt"], block = got[0], got[1]
    kw["geometry_agent"] = block
    if "geometry_agent" not in inspect.signature(planner.plan_with_accounting).parameters:
        raise AssertionError("this checkout's planner takes no typed block, so it is not a tree this gate compares")
    # THE THIRD VALUE IS PRINTED, never folded into a digest. It is the reason the geometry agent's step
    # was not used, and a gate that drops it cannot tell "nothing changed" from "nothing ran".
    if len(got) > 2 and got[2]:
        print(f"FG fell_back {got[2]}")
    asyncio.run(planner.plan_with_accounting(**kw))

    for label in ("intake_system", "intake_tools", "planner_user", "planner_system"):
        text = grab.get(label) or ""
        print(f"FG {label} {_d(text)} {len(text)}")
    # A BLOCK AT ALL, hashed on its own: an empty dict and a missing argument hash the same downstream.
    print(f"FG planner_block {'NONE' if block is None else _d(json.dumps(block, sort_keys=True, default=str))}")
    print(f"FG request_txt {_d(kw['request_txt'])} {len(kw['request_txt'])}")
