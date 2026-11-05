# Responsibility: Verify a run projects to one episode per agent, in order, with only real conversations exported.
from __future__ import annotations

from pathlib import Path

from meshpipeline.capture.trajectory import (  # noqa: E402
    build_episodes,
    episode_filename,
    episode_to_sft,
)


class _Ev:
    def __init__(self, event_type, payload):
        self.event_type, self.payload, self.attempt = event_type, payload, None


def _no_conversation(_dir):
    return []


def _events():
    return [
        _Ev("intake_complete", {"intake_system_snapshot": "SYS",
            "intake_turns": [{"role": "user", "content": "mesh it"},
                             {"role": "assistant", "content": "ok"}],
            "request_txt": "R", "purpose": "internal_cfd", "mesh_engine": "snappy"}),
        _Ev("engine_select_run", {"chosen": "snappy", "source": "user"}),
        _Ev("builder_attempt", {"builder_message_histories": [
            {"role": "system", "content": "B"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "run_mesh", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}],
            "builder_tools_definition": [{"type": "function",
                "function": {"name": "run_mesh"}}],
            "executor_success": True}),
        _Ev("executor_run", {"success": True, "executor_stdouts": "done",
            "executor_stderrs": ""}),
        _Ev("final_result_built", {"final_result": {"schema_version": 1, "status": "succeeded"},
            "terminal_message": "delivered"}),
    ]


def _build(events=None):
    return build_episodes(events or _events(), model_configs={},
                          read_conversation=_no_conversation)


def test_one_episode_per_agent_in_execution_order():
    eps = _build()
    assert [e["name"] for e in eps] == [
        "intake", "engine_select", "builder.attempt_1", "executor.attempt_1", "final_result"]
    assert [e["seq"] for e in eps] == [1, 2, 3, 4, 5]
    assert episode_filename(eps[2]) == "03_builder.attempt_1.json"


def test_llm_vs_deterministic_kinds():
    kinds = {e["name"]: e["kind"] for e in _build()}
    assert kinds["intake"] == "llm"
    assert kinds["builder.attempt_1"] == "llm"
    # the terminal episode is DETERMINISTIC - no model composes the verdict
    assert kinds["final_result"] == "deterministic"
    assert kinds["engine_select"] == "deterministic"
    assert kinds["executor.attempt_1"] == "deterministic"


def test_llm_episode_messages_are_the_openai_conversation():
    builder = next(e for e in _build() if e["name"] == "builder.attempt_1")
    roles = [m["role"] for m in builder["messages"]]
    assert roles == ["system", "assistant", "tool"]
    assert builder["messages"][1]["tool_calls"][0]["function"]["name"] == "run_mesh"
    assert builder["tools"]                     # per-episode tools sidecar


def test_deterministic_episode_has_a_decision_not_messages():
    es = next(e for e in _build() if e["name"] == "engine_select")
    assert "messages" not in es
    assert es["decision"] == {"chosen": "snappy", "source": "user"}


def test_sft_projects_llm_episodes_only():
    eps = _build()
    builder = next(e for e in eps if e["name"] == "builder.attempt_1")
    sft = episode_to_sft(builder)
    assert set(sft) == {"messages", "tools"}
    assert sft["messages"] == builder["messages"]
    # deterministic → not a chat-training example
    executor = next(e for e in eps if e["name"] == "executor.attempt_1")
    assert episode_to_sft(executor) is None


def test_a_lone_system_prompt_is_not_a_training_example():
    empty = {"kind": "llm", "messages": [{"role": "system", "content": "x"}], "tools": []}
    assert episode_to_sft(empty) is None


def test_intake_collapses_to_the_final_submission():
    evs = [
        _Ev("intake_complete", {"request_txt": "first", "intake_turns": []}),
        _Ev("engine_select_run", {"chosen": "cfmesh", "source": "user"}),
        _Ev("intake_complete", {"request_txt": "final", "intake_turns": []}),
    ]
    eps = _build(evs)
    intakes = [e for e in eps if e["agent"] == "intake"]
    assert len(intakes) == 1
    assert intakes[0]["output"]["request_txt"] == "final"


def test_reviewer_score_reflects_the_verdict():
    evs = [_Ev("reviewer_run", {"verdict": "PASS", "reviewer_review_dirs": "",
                                "axis_findings": {"a": True}})]
    rev = _build(evs)[0]
    assert rev["outcome"]["score"] == 1
    assert rev["output"]["verdict"] == "PASS"


# model provenance: every LLM episode names its model  #

def test_every_llm_agent_has_a_model_config_in_the_dispatcher():
    import re
    tasks_src = (Path(__file__).parent.parent.parent.parent
                 / "src" / "meshpipeline" / "application" / "pipeline_run.py").read_text()
    block = re.search(r"agent_model_configs=\{(.*?)\n            \},", tasks_src, re.S)
    assert block, "could not locate the agent_model_configs literal"
    configured = set(re.findall(r'"(\w+)":\s*\{', block.group(1)))

    llm_agents = {"intake", "planner", "builder", "reviewer"}
    missing = llm_agents - configured
    assert not missing, f"LLM agents with no recorded model provenance: {sorted(missing)}"


def test_intake_episode_carries_the_model_it_ran_on():
    eps = build_episodes(_events(), model_configs={"intake": {"model": "deepseek-v4-pro"}},
                         read_conversation=_no_conversation)
    intake = next(e for e in eps if e["agent"] == "intake")
    assert intake["model"] == "deepseek-v4-pro"
