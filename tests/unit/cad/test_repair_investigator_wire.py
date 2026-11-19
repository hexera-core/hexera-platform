"""`converse_tools`: the investigator's NATIVE tool round, beside `converse` rather than inside it.

Hermetic: the model call is a fake `contracts.model_inference.call_repair_review_model`.

What this pins:
  - the same conversation-in / reply-out shape as `converse`, with the typed `tool_calls` kept;
  - `ToolsUnsupported` is a CAPABILITY ANSWER - `(None, True)` - never a failure, so a caller
    falls back to the JSON protocol without charging the model-call budget;
  - every other failure is `(None, False)`, the total contract `converse` already keeps;
  - the tool schemas handed over are the investigator's own, rendered once.
"""
from __future__ import annotations

import pytest

import meshpipeline.contracts.model_inference as mi
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair import model_author as ma
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ToolCallRequest,
    ToolsUnsupported,
    assistant_turn,
    tool_result_turn,
)

CONVERSATION = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


class FakeTooledCall:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def __call__(self, messages, job_id="", name="RepairReview", *, tools=None,
                       user_id=""):
        self.calls.append({"messages": messages, "job_id": job_id, "name": name, "tools": tools,
                           "user_id": user_id})
        value = self.results.pop(0) if self.results else ModelRoundResult()
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def tooled(monkeypatch):
    def install(*results):
        fake = FakeTooledCall(*results)
        monkeypatch.setattr(mi, "call_repair_review_model", fake)
        return fake
    return install


def test_converse_tools_hands_over_the_schemas_and_keeps_the_calls(tooled):
    call = ToolCallRequest(id="c1", name="probe", arguments='{"tier": "2"}')
    fake = tooled(ModelRoundResult(tool_calls=(call,), assistant_text=""))
    reply, unsupported = ma.converse_tools_sync(
        CONVERSATION, job_id="j", timeout_s=5.0, tools=it.tool_schemas(),
        name="RepairInvestigator", user_id="u-1")
    assert unsupported is False
    assert reply is not None
    assert [c.name for c in reply.tool_calls] == ["probe"]
    sent = fake.calls[0]
    assert sent["name"] == "RepairInvestigator"
    assert sent["user_id"] == "u-1"
    assert [s.name for s in sent["tools"]] == ["look", "measure", "probe"]
    assert sent["messages"] == CONVERSATION


def test_tools_unsupported_is_a_capability_answer_not_a_failure(tooled):
    tooled(ToolsUnsupported("this router cannot call tools"))
    reply, unsupported = ma.converse_tools_sync(CONVERSATION, job_id="j", timeout_s=5.0,
                                                tools=it.tool_schemas())
    assert (reply, unsupported) == (None, True)


@pytest.mark.parametrize("result", [RuntimeError("boom"),
                                    ModelRoundResult(failure_marker="provider_down")])
def test_every_other_failure_is_total_and_charged(tooled, result):
    tooled(result)
    assert ma.converse_tools_sync(CONVERSATION, job_id="j", timeout_s=5.0,
                                  tools=it.tool_schemas()) == (None, False)


def test_a_round_is_threaded_back_with_the_assistant_and_tool_turns(tooled):
    """A native round may carry MANY calls: the budget counts calls, so the loop must be able to
    answer each one and ask again with the longer list."""
    calls = (ToolCallRequest(id="c1", name="look", arguments="{}"),
             ToolCallRequest(id="c2", name="probe", arguments='{"tier": "0"}'))
    first = ModelRoundResult(tool_calls=calls)
    fake = tooled(first, ModelRoundResult(assistant_text="done"))
    reply, _ = ma.converse_tools_sync(CONVERSATION, job_id="j", timeout_s=5.0,
                                      tools=it.tool_schemas())
    assert len(reply.tool_calls) == 2
    threaded = [*CONVERSATION, assistant_turn(reply)]
    threaded += [tool_result_turn(c, it.envelope(i, it.ToolResult(c.name, True, {}, 0.0)))
                 for i, c in enumerate(reply.tool_calls, start=1)]
    second, unsupported = ma.converse_tools_sync(threaded, job_id="j", timeout_s=5.0,
                                                 tools=it.tool_schemas())
    assert (second.assistant_text, unsupported) == ("done", False)
    sent = fake.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in sent[-2:]] == ["c1", "c2"]


def test_the_json_protocol_prose_names_every_native_tool():
    """ONE source of truth, two transports: the fallback prose is rendered from the schemas."""
    prose = it.tool_prose()
    for schema in it.tool_schemas():
        assert f'"{schema.name}"' in prose or schema.name in prose
    assert "probe" in prose and "tier" in prose


def test_converse_is_untouched(tooled):
    """`converse`/`_ask` is the monkeypatch seam two existing suites depend on: no tools reach it,
    and it is still asked with exactly the three arguments it always was."""
    fake = tooled(ModelRoundResult(assistant_text="{}"))
    ma.converse_sync(CONVERSATION, job_id="j", timeout_s=5.0)
    assert fake.calls[0]["tools"] is None
    assert fake.calls[0]["user_id"] == ""
