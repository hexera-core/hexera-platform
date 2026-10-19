# Responsibility: Verify the same question to an AI step gets the same answer, and that a changed prompt, model, input or owner is a new question.
from __future__ import annotations

import asyncio

from meshpipeline.contracts import decision_cache as dc
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest


class _Model:
    """A model that answers differently every time it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        return ModelRoundResult(tool_calls=(ToolCallRequest(id=f"call_{self.calls:08d}", name="run_mesh",
                                                            arguments=f'{{"n": {self.calls}}}'),),
                                reasoning_text="private")

    async def call_intake_model(self, messages, job_id="", user_id="", name="Intake", tools=None):
        self.calls += 1
        return ModelRoundResult(failure_marker="<<API_FAILURE:timeout>>")


def _conv(path="/srv/workspaces/0f8e2c1a-1111-2222-3333-444455556666/case/geom.stl", system="build it"):
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"mesh {path} at 2026-10-18T10:00:00Z"}]


def _router(mode="reuse"):
    inner = _Model()
    return inner, dc.CachingModelRouter(inner, model_of=lambda step: "openai:gpt-x",
                                        store=dc.MemoryDecisions(), mode=mode)


def test_the_same_question_gets_the_same_answer():
    inner, r = _router()
    a = asyncio.run(r.call_builder_model(_conv(), job_id="j1", user_id="u"))
    # another run: another workspace id, another time - the same question
    b = asyncio.run(r.call_builder_model(
        _conv(path="/srv/workspaces/9a9a9a9a-1111-2222-3333-444455556666/case/geom.stl"),
        job_id="j2", user_id="u"))
    assert inner.calls == 1
    assert a.tool_calls == b.tool_calls
    assert b.reasoning_text == ""                    # transport only: never kept
    log = dc.decisions_for("j2")
    assert log[-1]["source"] == "record" and log[-1]["decision"]["tool_calls"][0]["name"] == "run_mesh"
    assert dc.decisions_for("j1")[-1]["source"] == "model"


def test_a_new_prompt_a_new_input_or_another_owner_is_asked_again():
    inner, r = _router()
    asyncio.run(r.call_builder_model(_conv(), user_id="u"))
    asyncio.run(r.call_builder_model(_conv(system="build it better"), user_id="u"))
    asyncio.run(r.call_builder_model(_conv(path="/x/y/other.stl"), user_id="u"))
    asyncio.run(r.call_builder_model(_conv(), user_id="someone-else"))
    assert inner.calls == 4


def test_record_mode_always_asks_and_an_unusable_answer_is_never_kept():
    inner, r = _router(mode="record")
    asyncio.run(r.call_builder_model(_conv(), user_id="u"))
    asyncio.run(r.call_builder_model(_conv(), user_id="u"))
    assert inner.calls == 2
    inner, r = _router()
    asyncio.run(r.call_intake_model(_conv(), job_id="j3", user_id="u"))
    asyncio.run(r.call_intake_model(_conv(), job_id="j3", user_id="u"))
    assert inner.calls == 2                            # an outage is never replayed
    assert dc.decisions_for("j3")[-1]["source"].startswith("model (unusable")


def test_off_mode_is_the_old_behaviour():
    inner, r = _router(mode="off")
    asyncio.run(r.call_builder_model(_conv(), job_id="j4", user_id="u"))
    asyncio.run(r.call_builder_model(_conv(), job_id="j4", user_id="u"))
    assert inner.calls == 2 and dc.decisions_for("j4") == []


def test_an_image_counts_by_its_bytes_and_a_signed_url_by_its_path():
    a = dc._canon_text("see data:image/png;base64,aGVsbG8= and https://s/x.png?sig=1&t=2")
    b = dc._canon_text("see data:image/png;base64,aGVsbG8= and https://s/x.png?sig=9&t=8")
    assert a == b and "sha256:" in a
