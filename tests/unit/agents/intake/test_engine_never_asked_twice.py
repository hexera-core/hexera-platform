# Responsibility: Verify an engine the user already chose in their own words is selected when the
# model proposes it, whichever turn the proposal lands in, and that a later change of mind, a
# question or a refusal is the word that counts.
# Boundaries: engine_selection.user_chose over conversations, and one intake turn end to end with
# the model's rounds scripted (as test_intake_admission does).
"""The intake soak found every persona that answered "which engine?" with "snappyHexMesh" asked a
setup question next and then "Selected engine: snappyHexMesh. This is a proposal, not a selection
... Do you want to select snappyHexMesh?" - the model proposed the engine one turn after the user
named it, and only the latest message was searched for the name."""
from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es


@pytest.mark.parametrize("said", [
    ["snappyHexMesh", "k-omega SST, wall functions, yes"],
    ["use snappy please", "ok", "water at 2 m/s"],
    ["I'll go with snappyHexMesh for a body-fitted mesh", "10 layers is fine"],
    ["cfMesh", "actually, make it snappyHexMesh", "yes, that's fine"],
    ["Sounds great, snappyHexMesh it is - go right ahead!", "sure"],
])
def test_the_latest_naming_of_an_engine_is_the_users_choice(said):
    assert es.user_chose("snappy", said) is True


@pytest.mark.parametrize("said", [
    [],                                                        # never named
    ["water at 2 m/s", "yes"],
    ["snappyHexMesh", "actually use cfMesh instead"],          # changed their mind: cfMesh counts
    ["what is the difference between snappyHexMesh and cfMesh?"],
    ["snappyHexMesh?"],                                        # asked, not chosen
    ["not snappyHexMesh", "fine"],                             # turned down
    ["no, don't use snappyHexMesh"],
    ["snappyHexMesh multi-region"],                            # a different engine
])
def test_no_choice_or_a_later_word_against_it_is_no_choice(said):
    assert es.user_chose("snappy", said) is False


def test_the_fuzz_over_conversations_keeps_the_latest_naming():
    rng = random.Random(20260929)
    engines = {"snappy": "snappyHexMesh", "cfmesh": "cfMesh", "gmsh": "Gmsh"}
    filler = ["yes", "water at 2 m/s", "k-omega SST", "10 layers", "ok", "whatever you think"]
    for _ in range(300):
        convo, last = [], None
        for _ in range(rng.randint(1, 6)):
            if rng.random() < 0.4:
                last = rng.choice(list(engines))
                convo.append(rng.choice(("{e}", "use {e}", "I'll go with {e}", "{e} please")).format(e=engines[last]))
            else:
                convo.append(rng.choice(filler))
        for key in engines:
            assert es.user_chose(key, convo) is (key == last), (key, convo)


# --------------------------------------------------------------- one turn, end to end ----
def _tool_call(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=args))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


def _run(messages, responses):
    import meshpipeline.adapters.model_inference.router as llm_router
    seen: list = []
    it = iter(responses)

    async def _call(**kw):
        seen[:] = [m for m in (kw.get("messages") or []) if isinstance(m, dict) and m.get("role") == "tool"]
        return next(it)
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": messages}
    with patch.object(llm_router, "call_intake_model", _call):
        out = asyncio.run(intake.node_intake(state))
    return out, seen


def test_a_proposal_a_turn_after_the_user_named_the_engine_is_selected_not_asked():
    messages = [{"role": "user", "content": "water through this elbow at 2 m/s"},
                {"role": "assistant", "content": "Which meshing toolchain do you want to use?"},
                {"role": "user", "content": "snappyHexMesh"},
                {"role": "assistant", "content": "Wall functions with 5 layers - is that suitable?"},
                {"role": "user", "content": "yes, wall functions"}]
    out, seen = _run(messages, [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="Good - I'll check the setup next."),
    ])
    assert "SELECTED" in seen[-1]["content"]
    sel = out["intake_gate"]["selection"]
    assert sel["engine"] == "snappy" and sel["state"] == es.CONFIRMED
    assert "Do you want to select" not in out["messages"][-1]["content"]


def test_a_proposal_of_an_engine_the_user_moved_away_from_is_still_asked():
    messages = [{"role": "user", "content": "snappyHexMesh"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "hmm, actually cfMesh"}]
    out, _ = _run(messages, [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="unreached"),
    ])
    assert out["intake_gate"]["selection"]["state"] == es.PROPOSED
    assert "Do you want to select snappyHexMesh?" in out["messages"][-1]["content"]
