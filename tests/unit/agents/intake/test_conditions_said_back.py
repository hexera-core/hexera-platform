"""CFDREQ-37: the flow conditions the user typed are said back before the plan line - the values the plan really
carries, in a sentence the model writes under a guard, with the code's own sentence when it cannot.

On the live site a user typed "Air at 20 °C, 40 m/s, wall functions is fine." and got only "Here's the plan. ...",
so they could not tell whether 40 m/s was taken."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.intake.test_standard_questionnaire import EXTERNAL_DECL, INTERNAL_DECL, _run

import meshpipeline.agents.intake.approval as ap
from meshpipeline.agents.intake import conditions_reply as CR
from meshpipeline.agents.intake import flow
from meshpipeline.contracts import flow_conditions as FC

CAR = [("say", "external aero over this car"), ("stage", EXTERNAL_DECL)]
ELBOW = [("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL)]
TYPED = "Air at 20 °C, 40 m/s, wall functions is fine."


def _last(world) -> str:
    return str(world.session.messages[-1]["content"])


def _head(world) -> str:
    text = _last(world)
    assert text.endswith("\n\n" + ap.PROCEED_ASK), text
    return text[: -len(ap.PROCEED_ASK)].strip()


# ------------------------------------------------------------------------------- through the chat ----

@pytest.mark.asyncio
async def test_typed_conditions_are_said_back_with_the_values_the_plan_stored(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", TYPED)])
    got = FC.read(world.session.request_txt, world.session.intake_patches)
    assert got["speed_words"] == "40 m/s" and got["fluid_words"] == "air at 20 °C" and got["wall"] == "functions"
    # the stand-in model said nothing usable: the code's own sentence, then the plan line as before
    assert _head(world) == "Got it: air at 20 °C, 40 m/s, with wall functions."
    assert world.model.reply_calls == 1 and world.model.calls == 0
    assert flow.steps(world.session.messages)[-1] == flow.PLAN


@pytest.mark.asyncio
async def test_the_models_sentence_is_said_when_it_holds_the_stored_values(monkeypatch):
    ok = "Sure, air at 20 °C and 40 m/s with wall functions."
    world = await _run(monkeypatch, CAR + [("say", TYPED)], said_back=ok)
    assert _head(world) == ok


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [
    "Got it: air at 20 °C, 45 m/s, with wall functions.",        # a number the plan does not hold
    "Got it: air at 20 °C, 40 km/h, with wall functions.",       # the speed in another unit
    "Got it: water at 20 °C, 40 m/s, with wall functions.",      # another fluid
    "Got it: air at 20 °C, 40 m/s, with a resolved wall.",       # the other wall treatment
    "Got it: air at 20 °C, 40 m/s, with wall functions. Ready to go?",
])
async def test_a_wrong_model_sentence_is_refused_and_the_plain_one_said(monkeypatch, wrong):
    world = await _run(monkeypatch, CAR + [("say", TYPED)], said_back=wrong)
    assert _head(world) == "Got it: air at 20 °C, 40 m/s, with wall functions."


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["OK", "ok", "sounds good"])
async def test_an_ok_stays_the_plan_line_alone(monkeypatch, answer):
    world = await _run(monkeypatch, CAR + [("say", answer)])
    assert _last(world) == ap.PROCEED_ASK and world.model.reply_calls == 0


@pytest.mark.asyncio
async def test_the_resolve_the_wall_button_is_said_back(monkeypatch):
    world = await _run(monkeypatch, ELBOW + [("say", flow.CONDITIONS_CHOICES[1][1])])
    assert _head(world) == "Got it: water at 20 °C, 1 m/s at the inlet, with a resolved wall (y⁺ ≈ 1)."


@pytest.mark.asyncio
async def test_a_number_that_could_not_be_read_is_told(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "air, 40, wall functions")])
    # no unit: no speed was read, so the proposal's stands - and the reply says so instead of hiding it
    assert "Fluid: air at 20 °C at 10 m/s" in world.session.request_txt
    assert _head(world) == ("Got it: air at 20 °C, 10 m/s, with wall functions. I couldn't read “40” as a flow "
                            "condition, so it isn't used.")


@pytest.mark.asyncio
async def test_a_temperature_the_mesh_is_not_sized_for_is_told(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "air at 35 °C, 25 m/s")])
    assert _head(world) == ("Got it: air at 20 °C, 25 m/s, with wall functions. The mesh is sized for air at 20 °C, "
                            "so the 35 °C you gave isn't used.")


@pytest.mark.asyncio
async def test_a_fluid_the_first_layer_cannot_be_sized_for_is_told(monkeypatch):
    world = await _run(monkeypatch, ELBOW + [("say", "no, oil at 3 m/s")])
    head = _head(world)
    assert head.startswith("Got it: oil, 3 m/s at the inlet, with wall functions.")
    assert "keep the mesher's own size, because we have no viscosity for oil" in head


@pytest.mark.asyncio
async def test_conditions_named_before_the_stage_are_said_back_with_the_first_plan(monkeypatch):
    world = await _run(monkeypatch, [("say", "CFD of water through this elbow at 2 m/s"), ("stage", INTERNAL_DECL)])
    assert _head(world) == "From what you said, I'm using water at 20 °C, 2 m/s at the inlet, with wall functions."


@pytest.mark.asyncio
async def test_the_model_is_never_waited_on_past_the_time_cap():
    import asyncio

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "Got it: air at 20 °C, 40 m/s, with wall functions."
    f = _facts(TYPED)
    got = await CR.written(f, ask_fn=slow, timeout_s=0.05)
    assert got["source"] == CR.SOURCE_FALLBACK and got["text"] == f["plain_version_that_is_exactly_right"]


# ------------------------------------------------------------------------------------- the pieces ----

def _facts(said: str, *, speed: str = "40 m/s", wall: str = "functions", accepted: str = "") -> dict:
    msgs = [{"role": "user", "content": "external aero over this car"},
            {"role": "assistant", "content": EXTERNAL_DECL},
            {"role": "assistant", "content": flow.EXTERNAL_LEAD + " I'd go with ..."},
            {"role": "user", "content": said}]
    request = FC.sentence("air at 20 °C", speed, wall, internal=False) + (f" {FC.ACCEPTED_LEAD} {accepted}." if accepted
                                                                         else "")
    f = CR.facts(msgs, request, first_plan=True)
    assert f is not None
    return f


def test_the_values_said_back_are_the_plans_not_the_words():
    # the plan is the truth: words that say 50 m/s beside a plan that carries 40 m/s are told as not taken
    f = _facts("air, 50 m/s", speed="40 m/s")
    assert f["taken"]["speed"] == "40 m/s"
    assert f["not_taken"] == [{"words": "50 m/s", "why": "another speed than the one taken (40 m/s)"}]
    assert CR.check("Got it: air at 20 °C, 40 m/s, with wall functions.", f) != ""
    assert CR.check("Got it: air at 20 °C, 40 m/s, with wall functions. I took 40 m/s, not 50 m/s.", f) == ""


def test_the_plain_sentence_always_passes_its_own_guard():
    for said, kw in ((TYPED, {}), ("air at 35 °C, 25 m/s", {"speed": "25 m/s"}), ("air, 40, wall functions",
                                                                                 {"speed": "10 m/s"}),
                     ("the usual please", {"speed": "10 m/s", "accepted": "the fluid, its speed, the wall treatment"})):
        f = _facts(said, **kw)
        assert CR.check(f["plain_version_that_is_exactly_right"], f) == "", f


def test_words_that_named_nothing_are_said_as_an_ok():
    f = _facts("the usual please", speed="10 m/s", accepted="the fluid, its speed, the wall treatment")
    assert f["from_their_words"] == [] and f["not_taken"] == []
    assert f["plain_version_that_is_exactly_right"] == "I took that as an OK to air at 20 °C, 10 m/s, with wall functions."


def test_the_guard_refuses_a_plan_line_a_rule_id_and_an_exclamation():
    f = _facts(TYPED)
    for bad in ("Got it: air at 20 °C, 40 m/s, with wall functions. Press Proceed when ready.",
                "Got it: air at 20 °C, 40 m/s, with wall functions (CFDREQ-8).",
                "Got it: air at 20 °C, 40 m/s, with wall functions!"):
        assert CR.check(bad, f) != "", bad


@pytest.mark.asyncio
async def test_a_failed_model_gives_the_plain_sentence():
    f = _facts(TYPED)
    got = await CR.written(f, ask_fn=AsyncMock(side_effect=RuntimeError("down")))
    assert got == {"text": "Got it: air at 20 °C, 40 m/s, with wall functions.", "source": CR.SOURCE_FALLBACK,
                   "why": "no answer (RuntimeError)"}


def test_the_prompt_blocks_are_the_plan_steps_voice_and_language():
    names = [b[0] for b in CR.CONDITIONS_REPLY_PROMPT_BLOCKS]
    assert names[0] == "voice" and names[-1] == "language"
    assert "taken" in CR.compose_system()
