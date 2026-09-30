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
    ["I used snappyHexMesh last time"],                        # a mention, not a choice (review, #92)
    ["my colleague ran snappyHexMesh on this before", "ok"],
    ["is snappyHexMesh any good for this"],
    ["snappyHexMesh", "forget it"],                            # taken back without naming it (review, #92)
    ["use snappy", "actually don't use that"],
    ["snappyHexMesh", "no, wait"],
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
                convo.append(rng.choice(("{e}", "use {e}", "I'll go with {e}", "{e} please", "{e} it is",
                                         "let's go with {e}")).format(e=engines[last]))
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
    assert "I'd mesh this with" not in out["messages"][-1]["content"]


def test_a_proposal_of_an_engine_the_user_moved_away_from_is_still_asked():
    messages = [{"role": "user", "content": "snappyHexMesh"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "hmm, actually cfMesh"}]
    out, _ = _run(messages, [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="unreached"),
    ])
    assert out["intake_gate"]["selection"]["state"] == es.PROPOSED
    assert "I'd mesh this with snappyHexMesh" in out["messages"][-1]["content"]


# ------------------------------------------- an engine that cannot mesh what was confirmed ----
def _confirmed(kind: str, flow: str) -> dict:
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    return {"role": "assistant", "content": f"{CONFIRMED_MARK} the file is the fluid volume itself, input_kind "
                                            f"{kind}; the fluid flows {flow} it. Openings: inlet (inlet); outlet (outlet)."}


def test_the_model_never_proposes_an_engine_that_cannot_mesh_the_confirmed_geometry():
    # the soak (straight_reducer_015_fluid, "your call"): cfMesh proposed for a fluid volume, then
    # refused, then "change the input to a body surface?" to a user who had confirmed a fluid volume
    messages = [_confirmed("fluid-domain", "through"),
                {"role": "user", "content": "Your call, pick the normal thing."}]
    out, seen = _run(messages, [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "snappy"}))]),
        _resp(content="unreached"),
    ])
    assert "Not proposed: cfMesh cannot mesh what the user confirmed" in seen[0]["content"]
    assert "snappyHexMesh" in seen[0]["content"] and "cfMesh" not in seen[0]["content"].split("Engines that can:")[1]
    assert out["intake_gate"]["selection"]["engine"] == "snappy"


def test_an_engine_the_user_named_is_theirs_even_when_it_cannot_mesh_it():
    # their choice goes forward to the admission, which says why and what would pass
    messages = [_confirmed("fluid-domain", "through"), {"role": "user", "content": "use cfMesh"}]
    out, seen = _run(messages, [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": "cfmesh"}))]),
        _resp(content="cfMesh it is - checking it against your file next."),
    ])
    assert out["intake_gate"]["selection"]["engine"] == "cfmesh"


@pytest.mark.parametrize("kind,flow,engine", [("body-surface", "through", "cfmesh"), ("solid-body", "around", "snappy"),
                                              ("fluid-domain", "through", "snappy")])
def test_an_engine_that_can_mesh_the_confirmed_geometry_is_proposed_as_before(kind, flow, engine):
    out, _ = _run([_confirmed(kind, flow), {"role": "user", "content": "which one do you suggest?"}], [
        _resp([_tool_call("propose_engine_selection", json.dumps({"engine": engine}))]),
        _resp(content="unreached"),
    ])
    assert out["intake_gate"]["selection"]["state"] == es.PROPOSED and out["intake_gate"]["selection"]["engine"] == engine


def test_the_confirmed_case_is_read_back_from_the_stage_sentence_only():
    from meshpipeline.api.v1.geometry import ConfirmIn
    from meshpipeline.application.geometry_confirmation import confirmation_message, declared_case
    for kind in ("body-surface", "fluid-domain", "solid-body"):
        for flow, purpose in (("internal", "internal_cfd"), ("external", "external_cfd")):
            msg = confirmation_message(ConfirmIn(input_kind=kind, flow=flow, unit="mm"))
            assert declared_case([{"role": "assistant", "content": msg}]) == (purpose, kind)
    assert declared_case([{"role": "user", "content": "input_kind fluid-domain; the fluid flows through it."}]) is None
    assert declared_case([]) is None


@pytest.mark.parametrize("said", ["actually the file is the wall, not the fluid volume", "it's a body surface",
                                  "the fluid flows around it, not through it", "the part is hollow"])
def test_a_later_word_about_what_the_file_is_lifts_the_stages_gate(said):
    # review on #92: after the stage, the chat may correct it - the stage's sentence is not the last word
    from meshpipeline.application.geometry_confirmation import declared_case
    msgs = [_confirmed("fluid-domain", "through"), {"role": "user", "content": said}]
    assert declared_case(msgs) is None
    assert declared_case([_confirmed("fluid-domain", "through"), {"role": "user", "content": "your call"}]) \
        == ("internal_cfd", "fluid-domain")
