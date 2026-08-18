# Responsibility: Verify a builder turn's opening, tools, exits and state patch, across all five engines.
# Boundaries: the node delegates policy; it dispatches nothing and performs no terminal mutation.
from __future__ import annotations

import asyncio
import types

import pytest

import meshpipeline.agents.builder.agent as agent
import meshpipeline.agents.builder.attempt as attempt_mod
import meshpipeline.agents.builder.attempt_capture as attempt_capture
import meshpipeline.agents.builder.invoke as invoke
import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.engines.registry import engine_names, get_spec

ENGINES = sorted(engine_names())


@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    # The builder node publishes through the ownership-checked port. These tests exercise the
    # node's own contract, so the port is stood in for; ownership has its own suites.
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)


@pytest.fixture(autouse=True)
def _no_capture_writes(monkeypatch):
    written: list = []
    monkeypatch.setattr(attempt_capture, "TrainingLogger",
                        lambda job_id, *a, **k: types.SimpleNamespace(
                            log=lambda kind, **kw: written.append((kind, kw))))
    return written


def _state(tmp_path, **over):
    st = {
        "job_id": "job-6f", "engine": "cfmesh", "retry_count": 0, "builder_mode": "initial",
        "geometry": {}, "request_txt": "author a mesh", "review_brief_txt": "criteria",
        "intake_patches": [{"name": "wall", "type": "wall"}], "openfoam_workspace": str(tmp_path),
        "dimensionality": "3D", "purpose": "external_cfd", "engine_params": {},
    }
    st.update(over)
    return st


class _Probe:

    def __init__(self):
        self.driver_calls = 0
        self.loop_calls = 0
        self.engines_seen: list = []
        self.workspaces: list = []


def _run(state, probe, *, driver=None, loop=None, monkeypatch=None):
    async def _default_driver(workspace, st, *, job_id, publish, source_path, run):
        probe.driver_calls += 1
        probe.workspaces.append(workspace)
        return True, "authored", run.outcome(produced_deliverable=True)

    async def _default_loop(messages, workspace, **kw):
        probe.loop_calls += 1
        probe.engines_seen.append(kw.get("engine"))
        probe.workspaces.append(workspace)
        return "done", list(messages)

    import meshpipeline.agents.builder.loop as builder_loop
    monkeypatch.setattr(builder_loop, "_run_tool_loop", loop or _default_loop)
    # The engine's own build driver is stubbed for the whole matrix. What is under test is the
    # NODE's contract - one invocation, one patch, the same carried fields - and that must read
    # identically whether the engine drives itself (snappy) or authors through tool calls. The
    # driver seam itself has its own tests; running the real snappy driver here would only prove
    # that snappy needs real geometry.
    monkeypatch.setattr(invoke, "get_spec",
                        lambda *_a, **_k: types.SimpleNamespace(build_driver=driver),
                        raising=False)
    return asyncio.run(agent.node_builder(state))


# every engine, both modes

@pytest.mark.parametrize("engine", ENGINES)
def test_1_an_initial_turn_runs_for_every_engine(engine, tmp_path, monkeypatch):
    probe = _Probe()
    out = _run(_state(tmp_path, engine=engine), probe, monkeypatch=monkeypatch)
    ran = probe.driver_calls + probe.loop_calls
    assert ran == 1, f"{engine}: the turn invoked the builder {ran} times"
    assert out["retry_count"] == 1
    assert out["openfoam_workspace"], "the turn reported no workspace"


@pytest.mark.parametrize("engine", ENGINES)
def test_2_a_retry_continues_on_the_same_engine(engine, tmp_path, monkeypatch):
    probe = _Probe()
    first = _run(_state(tmp_path, engine=engine), probe, monkeypatch=monkeypatch)
    out = _run(_state(tmp_path, engine=engine, builder_mode="retry", retry_count=1,
                      openfoam_workspace=first["openfoam_workspace"],
                      builder_deadline_epoch=first["builder_deadline_epoch"]),
               probe, monkeypatch=monkeypatch)
    assert probe.driver_calls + probe.loop_calls == 2
    assert out["retry_count"] == 2
    assert out["builder_deadline_epoch"] == first["builder_deadline_epoch"], (
        "the retry reset the aggregate deadline")
    if probe.engines_seen:
        assert set(probe.engines_seen) == {engine}, "the retry changed engine"


def test_the_engine_matrix_covers_every_registered_engine():
    # The point is that the matrix cannot silently shrink, or miss an engine that was registered
    # after it was written. Both follow from comparing it to the registry; a fixed number said
    # neither, and turned a correctly added sixth engine into a failure here.
    from meshpipeline.engines.registry import engine_names

    assert set(ENGINES) == set(engine_names()), (
        f"the builder engine matrix {sorted(ENGINES)} is not the registry {sorted(engine_names())}")


# prompts and tools

@pytest.mark.parametrize("engine", ENGINES)
def test_3_the_opening_is_the_shared_prompt_plus_this_engines_guidance(engine, tmp_path):
    prepared = attempt_mod.prepare(_state(tmp_path, engine=engine), job_id="j", mode="initial")
    system = prepared.system_snapshot
    assert system, f"{engine}: no system prompt was composed"
    assert prepared.engine == engine
    other = [e for e in ENGINES if e != engine]
    prepared_others = {e: attempt_mod.prepare(_state(tmp_path / e, engine=e), job_id="j",
                                              mode="initial").system_snapshot for e in other}
    assert all(system != s for s in prepared_others.values()), (
        f"{engine}'s opening is identical to another engine's - guidance is not engine-specific")


@pytest.mark.parametrize("engine", ENGINES)
def test_4_an_attempt_carries_only_the_tools_its_engine_declares(engine, tmp_path):
    prepared = attempt_mod.prepare(_state(tmp_path, engine=engine), job_id="j", mode="initial")
    declared = set(get_spec(engine).tool_names)
    offered = [t["function"]["name"] for t in prepared.tools]
    assert offered, f"{engine}: no tools were assembled"
    assert set(offered) <= declared, (
        f"{engine} was offered undeclared tools: {sorted(set(offered) - declared)}")


def test_the_builder_tool_manifest_is_unchanged():
    import hashlib
    import json
    from pathlib import Path

    from meshpipeline.agents.builder import tools as T

    baseline = json.loads(
        (Path(__file__).parent / "builder_tool_manifest.json").read_text())

    def _norm(o):
        if isinstance(o, dict):
            return {k: _norm(o[k]) for k in sorted(o)}
        return [_norm(x) for x in o] if isinstance(o, list) else o

    live = [{"position": i, "name": s["function"]["name"],
             "description_sha": hashlib.sha256(
                 s["function"].get("description", "").encode()).hexdigest()[:16],
             "schema": _norm(s["function"].get("parameters", {})), "type": s.get("type")}
            for i, s in enumerate(T.TOOLS)]
    assert live == baseline["registry"]
    # The baseline comparison above already pins every tool, its description hash and its schema.
    # What remains contractual is that the recorded engine scope covers the registry, not that
    # there are nine tools and five engines today.
    from meshpipeline.engines.registry import engine_names

    assert set(baseline["engine_scope"]) == set(engine_names()), \
        "the recorded per-engine tool scope does not cover the registry"
    for engine, expected in baseline["engine_scope"].items():
        assert [t["function"]["name"] for t in T._active_tools(engine)] == expected


# the REAL shared loop

def test_5_a_turn_executes_real_tools_through_the_real_shared_loop(tmp_path, monkeypatch):
    import json as _json

    from meshpipeline.contracts.model_inference import (
        ModelRoundResult,
        ProviderAttemptInfo,
        ToolCallRequest,
    )

    rounds = iter([
        ModelRoundResult(
            tool_calls=(ToolCallRequest(id="c1", name="write_file",
                                        arguments=_json.dumps({"path": "notes.txt",
                                                               "content": "cell=0.05"})),),
            assistant_text="", finish_reason="tool_calls",
            provider=ProviderAttemptInfo(1, "p", "m")),
        ModelRoundResult(tool_calls=(), assistant_text="done", finish_reason="stop",
                         provider=ProviderAttemptInfo(1, "p", "m")),
    ])
    calls = {"n": 0}

    async def _provider(**kw):
        calls["n"] += 1
        try:
            return next(rounds)
        except StopIteration:
            return ModelRoundResult(tool_calls=(), assistant_text="done", finish_reason="stop",
                                    provider=ProviderAttemptInfo(1, "p", "m"))

    import meshpipeline.agents.builder.loop as builder_loop
    monkeypatch.setattr(builder_loop, "_provider_call", lambda *a, **k: _provider)
    monkeypatch.setattr(invoke, "get_spec",
                        lambda *_a, **_k: types.SimpleNamespace(build_driver=None), raising=False)

    out = asyncio.run(agent.node_builder(_state(tmp_path, engine="cfmesh")))

    assert calls["n"] >= 1, "the real loop never called the provider"
    workspace = out["openfoam_workspace"]
    from pathlib import Path
    assert (Path(workspace) / "notes.txt").read_text() == "cell=0.05", (
        "the real tool never ran through the real loop")


# what a turn can end as

def test_6_a_final_round_with_no_tool_still_produces_a_patch(tmp_path, monkeypatch):
    probe = _Probe()

    async def _quiet(messages, workspace, **kw):
        probe.loop_calls += 1
        return "nothing to do", []
    out = _run(_state(tmp_path), probe, loop=_quiet, monkeypatch=monkeypatch)
    assert probe.loop_calls == 1
    assert out["retry_count"] == 1 and out["openfoam_workspace"]


def test_8_a_malformed_provider_response_is_the_loops_problem_not_the_nodes():
    import inspect

    node = inspect.getsource(agent.node_builder)
    for parsing in ("finish_reason", "json.loads", "assistant_text", ".choices",
                    "ModelRoundResult", "ToolCallRequest"):
        assert parsing not in node, f"the node parses provider output again: {parsing}"


def test_an_engine_that_declares_a_build_driver_takes_the_driver_path(tmp_path, monkeypatch):
    probe = _Probe()

    async def _driver(workspace, st, *, job_id, publish, source_path, run):
        probe.driver_calls += 1
        return True, "authored", run.outcome(produced_deliverable=True)

    out = _run(_state(tmp_path), probe, driver=_driver, monkeypatch=monkeypatch)
    assert probe.driver_calls == 1 and probe.loop_calls == 0, (
        "a declared build driver did not take the driver path")
    assert out["retry_count"] == 1


def test_9_a_provider_failure_becomes_an_api_failure_patch(tmp_path, monkeypatch):
    probe = _Probe()

    async def _down(messages, workspace, **kw):
        probe.loop_calls += 1
        raise RuntimeError("[API_FAILURE] openai: 500")
    out = _run(_state(tmp_path), probe, loop=_down, monkeypatch=monkeypatch)
    assert probe.loop_calls == 1
    assert out["api_failure"] == "openai: 500"
    assert "executor_success" not in out, "a provider failure claimed execution truth"
    assert out["openfoam_workspace"] and out["builder_deadline_epoch"], (
        "the failure exit dropped a carried field")


def test_10_a_tool_failure_inside_the_loop_does_not_end_the_turn_as_a_failure(tmp_path,
                                                                              monkeypatch):
    probe = _Probe()

    async def _loop(messages, workspace, **kw):
        probe.loop_calls += 1
        kw["tool_calls_out"].append({"tool": "run_mesh", "error": "refused"})
        return "the mesh did not build", list(messages)
    out = _run(_state(tmp_path), probe, loop=_loop, monkeypatch=monkeypatch)
    assert probe.loop_calls == 1 and "api_failure" not in out


def test_7_a_timeout_is_not_authored_but_still_reports_the_workspace(tmp_path, monkeypatch):
    probe = _Probe()

    async def _slow(messages, workspace, **kw):
        probe.loop_calls += 1
        raise TimeoutError()
    out = _run(_state(tmp_path), probe, loop=_slow, monkeypatch=monkeypatch)
    assert probe.loop_calls == 1
    assert out["openfoam_workspace"] and "api_failure" not in out


# context and engine

def test_11_reviewer_feedback_reaches_the_next_turn_fenced_as_untrusted(tmp_path):
    prepared = attempt_mod.prepare(
        _state(tmp_path, builder_mode="rebuild",
               reviewer_feedback="the boundary layer is too coarse"), job_id="j", mode="rebuild")
    opening = prepared.messages[1]["content"]
    assert "the boundary layer is too coarse" in opening
    assert "UNTRUSTED ADVISORY" in opening
    assert "MUST NOT change the selected engine" in opening


def test_12_the_failure_summary_reaches_a_retry_fenced_as_untrusted(tmp_path):
    prepared = attempt_mod.prepare(
        _state(tmp_path, builder_mode="retry", retry_count=1,
               classifier_result={"summary": "the mesh had 12 negative cells"}),
        job_id="j", mode="retry")
    opening = prepared.messages[1]["content"]
    assert "12 negative cells" in opening and "UNTRUSTED ADVISORY" in opening


def test_advisory_context_never_lands_on_the_system_prompt(tmp_path):
    prepared = attempt_mod.prepare(
        _state(tmp_path, builder_mode="rebuild", reviewer_feedback="SECRET-FEEDBACK-TOKEN"),
        job_id="j", mode="rebuild")
    assert "SECRET-FEEDBACK-TOKEN" not in prepared.system_snapshot
    assert "UNTRUSTED ADVISORY" not in prepared.system_snapshot


def test_13_an_absent_engine_falls_back_to_the_declared_default_never_an_inference(tmp_path):
    from meshpipeline.engines.registry import default_engine

    prepared = attempt_mod.prepare(_state(tmp_path, engine="", purpose="internal_cfd"),
                                   job_id="j", mode="initial")
    assert prepared.engine == default_engine(), "an absent engine was inferred, not defaulted"


def test_purpose_never_selects_the_engine(tmp_path):
    from meshpipeline.engines.registry import default_engine

    seen = {attempt_mod.prepare(_state(tmp_path / p, engine="", purpose=p),
                                job_id="j", mode="initial").engine
            for p in ("internal_cfd", "external_cfd", "structural")}
    assert seen == {default_engine()}, f"purpose changed the engine: {seen}"


def test_14_an_unknown_engine_is_never_silently_replaced(tmp_path):
    from meshpipeline.engines.registry import UnknownEngineError

    with pytest.raises(UnknownEngineError):
        attempt_mod.prepare(_state(tmp_path, engine="not-an-engine"), job_id="j", mode="initial")


# transcript and patch

def test_15_the_capture_transcript_drops_images_and_keeps_reasoning():
    out = attempt_capture.serialise_messages([
        {"role": "assistant", "content": "text", "reasoning_content": "the chain of thought"},
        {"role": "user", "content": [{"type": "image_url",
                                      "image_url": {"url": "data:image/png;base64,AAAA"}},
                                     {"type": "text", "text": "look"}]},
    ])
    blob = str(out)
    assert "the chain of thought" in blob, "reasoning was stripped from the corpus again"
    assert "base64,AAAA" not in blob and "[base64_image_omitted]" in blob


def test_15b_no_system_prompt_or_secret_reaches_the_returned_patch(tmp_path, monkeypatch):
    probe = _Probe()
    prepared = attempt_mod.prepare(_state(tmp_path), job_id="job-6f", mode="initial")
    snapshot = prepared.system_snapshot
    assert snapshot and len(snapshot) > 200, "no system prompt was composed to compare against"

    out = _run(_state(tmp_path), probe, monkeypatch=monkeypatch)
    blob = str(out)
    assert snapshot not in blob, "the composed system prompt reached the graph patch"
    # any substantial run of it, not just the whole thing
    for chunk in (snapshot[:120], snapshot[len(snapshot) // 2:][:120], snapshot[-120:]):
        assert chunk not in blob, "part of the system prompt reached the graph patch"
    for leaked in ("s3://", "minio", "Bearer", "execution_id", "worker_token"):
        assert leaked not in blob, f"the Builder patch leaks {leaked}"


@pytest.mark.parametrize("scenario", ["ok", "api_failure", "budget_exhausted"])
def test_16_every_exit_carries_the_same_common_state(scenario, tmp_path, monkeypatch):
    probe = _Probe()
    state = _state(tmp_path)
    if scenario == "budget_exhausted":
        state["builder_deadline_epoch"] = 1.0            # long past
        out = _run(state, probe, monkeypatch=monkeypatch)
    elif scenario == "api_failure":
        async def _down(messages, workspace, **kw):
            raise RuntimeError("[API_FAILURE] provider down")
        out = _run(state, probe, loop=_down, monkeypatch=monkeypatch)
    else:
        out = _run(state, probe, monkeypatch=monkeypatch)
    for field in ("retry_count", "openfoam_workspace", "request_txt", "review_brief_txt",
                  "builder_deadline_epoch"):
        assert field in out, f"the {scenario} exit dropped {field}"
    assert set(out) <= agent._BUILDER_RETURN_KEYS


def test_a_budget_exhausted_turn_starts_no_attempt(tmp_path, monkeypatch):
    probe = _Probe()
    out = _run(_state(tmp_path, builder_deadline_epoch=1.0), probe, monkeypatch=monkeypatch)
    assert probe.driver_calls + probe.loop_calls == 0, "an exhausted budget started a build"
    assert out["retry_count"] == bcfg.MAX_BUILDER_RETRIES + 1


# the node dispatches nothing

def test_22_the_node_performs_no_dispatch_or_terminal_mutation():
    import inspect

    node = inspect.getsource(agent.node_builder)
    for forbidden in ("dispatch", "final_result", "link_job", "terminal", "celery",
                      "send_task", "executor_success"):
        assert forbidden not in node, f"the Builder node performs {forbidden}"


def test_the_node_owns_no_policy_it_delegates():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(agent.node_builder))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for policy in ("_setup_workspace", "_build_initial_messages", "_active_tools",
                   "prepare_surface", "_run_tool_loop", "get_spec", "cap_child_deadline_epoch",
                   "advisory_block", "TrainingLogger", "copy2"):
        assert policy not in called, f"the node still owns {policy}"
    for delegated in ("prepare", "settle", "run_attempt", "assess", "record_attempt"):
        assert delegated in called, f"the node no longer delegates {delegated}"


def test_a_retry_inherits_the_prior_attempts_authored_spec(tmp_path, monkeypatch):
    from pathlib import Path

    probe = _Probe()
    first = _run(_state(tmp_path, engine="cfmesh"), probe, monkeypatch=monkeypatch)
    prev = Path(first["openfoam_workspace"])
    (prev / "geom_box.json").write_text('{"min": [0,0,0], "max": [1,1,1]}')
    (prev / "input.stl").write_text("solid s\nendsolid s\n")
    for relative in __import__("meshpipeline.agents.builder.tools",
                               fromlist=["get_spec_run_files"]).get_spec_run_files("cfmesh"):
        target = prev / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("authored by the previous attempt\n")

    out = _run(_state(tmp_path, engine="cfmesh", builder_mode="retry", retry_count=1,
                      openfoam_workspace=str(prev),
                      builder_deadline_epoch=first["builder_deadline_epoch"]),
               probe, monkeypatch=monkeypatch)

    new_ws = Path(out["openfoam_workspace"])
    assert new_ws != prev, "the retry reused the previous workspace"
    assert (new_ws / "geom_box.json").exists(), "geom_box.json was not carried forward"
    assert (new_ws / "input.stl").exists(), "the prepared surface was not carried forward"
    for relative in __import__("meshpipeline.agents.builder.tools",
                               fromlist=["get_spec_run_files"]).get_spec_run_files("cfmesh"):
        assert (new_ws / relative).exists(), f"the authored spec {relative} was not carried"


def test_a_repeated_retry_that_changes_nothing_exhausts_the_budget():
    from meshpipeline.agents.builder import noop as noop_mod

    first = noop_mod.assess(before="abc", after="abc", carried_count=0, retry_count=2)
    assert first.repeated and first.consecutive == 1 and not first.budget_exhausted
    assert first.retry_count == 2, "one no-op must not end the run"

    second = noop_mod.assess(before="abc", after="abc", carried_count=1, retry_count=3)
    assert second.budget_exhausted
    assert second.retry_count == bcfg.MAX_BUILDER_RETRIES + 1

    changed = noop_mod.assess(before="abc", after="def", carried_count=1, retry_count=3)
    assert not changed.repeated and changed.retry_count == 3

    nothing_authored = noop_mod.assess(before="", after="", carried_count=1, retry_count=3)
    assert not nothing_authored.repeated, (
        "an attempt that authored nothing twice was punished as a repeat")
