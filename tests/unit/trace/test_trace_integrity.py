# Responsibility: Verify one tool call yields one authoritative result, keeping engineering words and losing identity.
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tracing import TraceContext
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.trace.policy import RAW, SAFE, project
from meshpipeline.trace.sanitizer import MODEL_MARK, sanitize_payload, scrub_text

UI = Path(__file__).parent.parent.parent.parent / "ui"


# F1  tool lifecycle


class _Pub:
    def __init__(self):
        self.events: list[dict] = []

    def _add(self, build):
        self.events.append(build().wire())

    def reasoning(self, rid, phase, **kw):
        import meshpipeline.events as E
        self._add(lambda: E.reasoning("builder", rid, "builder", phase, **kw))

    def tool_call(self, cid, tool_name, arguments=None, status="started"):
        import meshpipeline.events as E
        self._add(lambda: E.tool_call("builder", cid, "builder", tool_name,
                                      arguments, status))

    def tool_result(self, rid, call_id, tool_name, result=None, status="success",
                    duration_ms=None):
        import meshpipeline.events as E
        self._add(lambda: E.tool_result("builder", rid, call_id, "builder", tool_name,
                                        result, status, duration_ms))

    def rationale(self, conclusion, because=""):
        import meshpipeline.events as E
        self._add(lambda: E.rationale("builder", "Builder", conclusion, because))

    def of(self, kind):
        return [w for w in self.events if w["type"] == kind]


class _Driver:

    role = AgentRole.builder

    def __init__(self):
        self.validated: list[str] = []      # executor entered
        self.handler_ran: list[str] = []    # underlying operation performed

    def limits(self): return LoopLimits(max_rounds=3)
    def before_round(self, tally, messages): return None
    def forced_tool(self, tally): return None
    def category_of(self, name): return "work"
    def extension(self): return {}
    def is_supersession(self, exc): return False
    def observe(self, tally): return ProgressObservation(made_progress=True)
    def correction(self, stage, tally, observation): return None
    def on_plaintext(self, tally, text): return RoundDecision.stop(LoopExit.turn_complete)
    async def close_out(self, tally): return None

    async def execute(self, inv: ToolInvocation) -> ToolOutcome:
        self.validated.append(inv.tool)
        if inv.parsed is None:
            return ToolOutcome(content="[SYSTEM] arguments were not valid JSON",
                               accepted=False)
        self.handler_ran.append(inv.tool)
        if inv.tool == "submit_mesh":
            return ToolOutcome(content="ok", accepted=True, terminal=True, payload="done")
        return ToolOutcome(content={"written": "system/meshDict"}, accepted=True)


def _round(*calls, **kw):
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=f"p{i}", name=n, arguments=a)
                         for i, (n, a) in enumerate(calls)), **kw)


async def _run(driver, rounds, pub):
    it = iter(rounds)

    async def provider(**kw):
        try:
            return next(it)
        except StopIteration:
            return ModelRoundResult(assistant_text="done")

    return await run_agent_loop(
        driver=driver, provider_call=provider, messages=[], tools=[],
        job_id="j", user_id="u",
        append_tool_result=lambda m, cid, c: m.append({"role": "tool", "content": str(c)}),
        trace=TraceContext(publisher=pub, job_id="j", role="builder", attempt=1))


@pytest.fixture
def safe(monkeypatch):
    import meshpipeline.settings.policy as P
    monkeypatch.setattr(P, "PUBLIC_TRACE_MODE", "safe", raising=False)


async def test_one_call_produces_exactly_one_authoritative_result(safe):
    pub, d = _Pub(), _Driver()
    await _run(d, [_round(("write_file", "{not json"), ("read_file", '{"p":"a"}'),
                          ("submit_mesh", "{}"))], pub)
    calls, results = pub.of("tool_call"), pub.of("tool_result")
    assert len(results) == len(calls), (
        f"{len(calls)} calls produced {len(results)} results - a call must have "
        f"exactly one terminal outcome")
    ids = [w["id"] for w in results]
    assert len(ids) == len(set(ids)), f"duplicate tool_result ids: {ids}"
    by_call: dict[str, set[str]] = {}
    for w in results:
        by_call.setdefault(w["tool_call_id"], set()).add(w["status"])
    bad = {k: v for k, v in by_call.items() if len(v) > 1}
    assert not bad, f"a single call reported more than one outcome: {bad}"


async def test_malformed_arguments_are_blocked_and_never_reach_the_handler(safe):
    pub, d = _Pub(), _Driver()
    await _run(d, [_round(("write_file", "{not json"), ("submit_mesh", "{}"))], pub)
    assert pub.of("tool_call")[0]["status"] == "blocked"
    assert pub.of("tool_result")[0]["status"] == "blocked", (
        "a refusal of MALFORMED arguments is blocked - the tool never ran - not failure")
    assert "write_file" in d.validated, "the executor's validation layer was bypassed"
    assert "write_file" not in d.handler_ran, (
        "a side-effecting handler ran with malformed arguments")
    assert d.handler_ran == ["submit_mesh"]


async def test_a_valid_call_still_executes_and_reports_one_success(safe):
    pub, d = _Pub(), _Driver()
    await _run(d, [_round(("read_file", '{"p":"a"}'), ("submit_mesh", "{}"))], pub)
    assert d.handler_ran == ["read_file", "submit_mesh"]
    results = pub.of("tool_result")
    assert [w["status"] for w in results] == ["success", "success"]
    assert len(results) == 2


async def test_a_mixed_round_preserves_order_and_cardinality(safe):
    pub, d = _Pub(), _Driver()
    await _run(d, [_round(("read_file", '{"p":"a"}'), ("write_file", "{bad"),
                          ("list_directory", '{"p":"."}'), ("submit_mesh", "{}"))], pub)
    calls, results = pub.of("tool_call"), pub.of("tool_result")
    assert [w["status"] for w in calls] == ["started", "blocked", "started", "started"]
    assert [w["status"] for w in results] == ["success", "blocked", "success", "success"]
    assert [w["tool_call_id"] for w in results] == [w["id"] for w in calls], \
        "results are not paired with their calls in provider order"
    assert d.handler_ran == ["read_file", "list_directory", "submit_mesh"]


async def test_replay_carries_the_same_single_authoritative_result(safe):
    pub, d = _Pub(), _Driver()
    await _run(d, [_round(("write_file", "{not json"), ("submit_mesh", "{}"))], pub)
    stored = pub.of("tool_result")
    replayed = [project(w, SAFE) for w in stored]
    assert len(replayed) == len(stored) == 2
    ids = [w["id"] for w in replayed]
    assert len(ids) == len(set(ids))
    assert [w["status"] for w in replayed] == ["blocked", "success"]


# F2  sanitizer


#: Legitimate engineering content that must survive byte-for-byte.
PASS_THROUGH = [
    "div(phi,U) bounded Gauss linearUpwind grad(U);",
    "div(phi,k) bounded Gauss upwind;",
    "phi angle 30 deg",
    "yi coordinate",
    "the nova region of the wing",
    "titan geometry",
    "grok the topology",
    "snappyHexMesh",
    "checkMesh",
    "cfMesh",
    "Gmsh",
    "VMTK",
    "prism layer growth ratio 1.2",
    "max non-orthogonality 65.1943, max skewness 7.80077",
    "aircraft wall farfield inlet outlet symmetry",
    "3,907,590 cells and 12,230,050 faces in 1 region",
    "the base mesh is fine and the wake is refined",
]

#: Model or provider identity that must never reach a public page.
MUST_REDACT = [
    "GLM 5.2", "GLM-5.2", "zai-org/GLM-5.2", "GPT-5.6", "gpt-4o-mini-2024",
    "claude opus 4.5", "deepseek-v4-pro", "moonshotai/Kimi-K2.5",
    "phi-4", "grok-2", "nova-pro-1", "openai", "anthropic", "fp_44709d6fcb",
]


@pytest.mark.parametrize("text", PASS_THROUGH)
def test_legitimate_engineering_vocabulary_survives_untouched(text):
    out = scrub_text(text)
    assert out == text, (
        f"the sanitizer corrupted genuine technical content:\n  in : {text!r}\n"
        f"  out: {out!r}")


@pytest.mark.parametrize("text", MUST_REDACT)
@pytest.mark.parametrize("m", [SAFE, RAW])
def test_model_and_provider_identity_never_crosses_in_either_mode(text, m, monkeypatch):
    import meshpipeline.settings.policy as P
    monkeypatch.setattr(P, "PUBLIC_TRACE_MODE", m, raising=False)
    monkeypatch.setattr(P, "ALLOW_PUBLIC_RAW_TRACE", True, raising=False)
    out = project({"type": "reasoning", "stage": "builder", "id": "r", "agent": "builder",
                   "phase": "completed", "status": "success", "duration_ms": 10,
                   "content": f"routed via {text} now"}, m)
    body = json.dumps(out)
    core = re.split(r"[/\s-]", text)[0]
    assert core.lower() not in body.lower(), f"{text!r} survived in {m} mode"


def test_an_ambiguous_stem_is_redacted_only_with_a_version():
    assert scrub_text("div(phi,U)") == "div(phi,U)"
    assert "phi" not in scrub_text("running phi-4 now").lower()


def test_nested_payloads_keep_domain_content_and_lose_identity():
    out = sanitize_payload({
        "fvSchemes": {"div": "div(phi,U) bounded Gauss linearUpwind"},
        "layers": [{"patch": "aircraft", "growth": 1.2}, {"patch": "farfield"}],
        "note": "planned by GLM 5.2",
        "api_key": "sk-live-SENTINEL0123456789",
        "cells": 3907590,
    })
    assert out["fvSchemes"]["div"] == "div(phi,U) bounded Gauss linearUpwind"
    assert out["layers"][0] == {"patch": "aircraft", "growth": 1.2}
    assert out["cells"] == 3907590
    assert "GLM" not in out["note"] and MODEL_MARK in out["note"]
    assert "SENTINEL" not in str(out["api_key"])


def test_reasoning_text_keeps_its_engineering_and_loses_its_identity(monkeypatch):
    import meshpipeline.settings.policy as P
    monkeypatch.setattr(P, "PUBLIC_TRACE_MODE", "raw", raising=False)
    monkeypatch.setattr(P, "ALLOW_PUBLIC_RAW_TRACE", True, raising=False)
    out = project({"type": "reasoning", "stage": "builder", "id": "r", "agent": "builder",
                   "phase": "completed", "status": "success", "duration_ms": 10,
                   "content": "As GLM 5.2 I set div(phi,U) to linearUpwind and grew "
                              "prism layers on the aircraft wall."}, RAW)
    assert "div(phi,U)" in out["content"], "raw reasoning lost its scheme"
    assert "prism layers" in out["content"] and "aircraft wall" in out["content"]
    assert "GLM" not in out["content"]


# F3  reasoning header
