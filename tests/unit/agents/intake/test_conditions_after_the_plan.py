"""CFDREQ-37 (rulebook 122): the flow conditions are asked AFTER the plan, not before it.

Once the stage is confirmed, the plan card and its checklist come straight away, built on the conditions the
question proposes; then one more message asks about them, with the same two buttons. Nothing waits on it: Proceed
or a go starts the run on the assumed conditions; OK keeps them; "Resolve the wall" or typed conditions redo the
plan, and the new plan's message says back what was taken."""
from __future__ import annotations

import pytest
from tests.unit.agents.intake.test_standard_questionnaire import EXTERNAL_DECL, INTERNAL_DECL, _run

import meshpipeline.agents.intake.approval as ap
from meshpipeline.agents.intake import flow
from meshpipeline.application import purpose_map as pm
from meshpipeline.contracts import flow_conditions as FC

CAR = [("say", "external aero over this car"), ("stage", EXTERNAL_DECL)]
ELBOW = [("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL)]


def _conditions(world) -> dict:
    got = FC.read(world.session.request_txt, world.session.intake_patches)
    assert got is not None
    return got


@pytest.mark.asyncio
@pytest.mark.parametrize("script,lead,fluid,speed", [
    (CAR, flow.EXTERNAL_LEAD, "air at 20 °C", "10 m/s"),
    (ELBOW, flow.INTERNAL_LEAD, "water at 20 °C", "1 m/s at the inlet"),
])
async def test_the_plan_comes_first_then_the_question_under_it(monkeypatch, script, lead, fluid, speed):
    world = await _run(monkeypatch, script[:1])
    nxt = (await world.proceed_on_stage(script[1][1])).model_dump(mode="json")
    msgs = world.session.messages
    # the plan line, then one more message: the assumed conditions, with the two buttons
    assert msgs[-2]["content"] == ap.PROCEED_ASK
    asked = msgs[-1]["content"]
    assert asked == f"{lead} {fluid} at {speed}, with {flow.WALL_FUNCTIONS} - OK, or tell me what to change."
    assert [c["label"] for c in pm.choices_for(asked)] == ["OK", "Resolve the wall (y⁺ ≈ 1)"]
    assert flow.steps(msgs)[-3:] == [flow.CONFIRMED, flow.PLAN, flow.FLOW_QUESTION]
    # the plan stands on the assumed conditions, with Proceed
    assert ap.is_live(world.session.intake_gate.get("approval"))
    assert _conditions(world)["wall"] == "functions"
    assert world.model.calls == 0
    # what the console was handed: the plan's line, the card, then the question with its buttons
    assert nxt["reply"] == ap.PROCEED_ASK and nxt["choices"] == [] and nxt["brief"]["proceed_ask"] == ap.PROCEED_ASK
    assert nxt["then"]["reply"] == asked and [c["send"] for c in nxt["then"]["choices"]] == ["OK", "Resolve the wall (y⁺ ≈ 1)"]


@pytest.mark.asyncio
@pytest.mark.parametrize("go", ["Proceed", "go", "let's mesh it"])
async def test_proceed_or_go_without_answering_starts_the_run_on_the_assumed_conditions(monkeypatch, go):
    world = await _run(monkeypatch, CAR + [("say", go)])
    assert world.dispatched
    assert _conditions(world)["speed_words"] == "10 m/s"


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", ["OK", "okay", "sounds good"])
async def test_ok_keeps_the_assumed_conditions_and_starts_nothing(monkeypatch, ok):
    world = await _run(monkeypatch, CAR + [("say", ok)])
    assert not world.dispatched and ap.is_live(world.session.intake_gate.get("approval"))
    assert world.session.messages[-1]["content"] == ap.PROCEED_ASK
    assert _conditions(world)["speed_words"] == "10 m/s"
    # and Proceed still starts it
    await world.say("Proceed")
    assert world.dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,check", [
    ("Resolve the wall (y⁺ ≈ 1)", {"wall": "resolved", "speed_words": "1 m/s"}),
    ("no, oil at 3 m/s", {"fluid": "oil", "speed_words": "3 m/s"}),
    ("water at 2.5 m/s, resolve the wall", {"wall": "resolved", "speed_words": "2.5 m/s"}),
])
async def test_each_answer_redoes_the_plan_and_the_card_carries_it(monkeypatch, answer, check):
    world = await _run(monkeypatch, ELBOW + [("say", answer)])
    got = _conditions(world)
    assert {k: got[k] for k in check} == check
    assert ap.is_live(world.session.intake_gate.get("approval"))
    text = world.session.messages[-1]["content"]
    assert text.startswith("Got it: ") and text.endswith("\n\n" + ap.PROCEED_ASK), text
    # the card the console draws is the new plan's
    assert world.answers[-1].brief and world.answers[-1].awaiting_confirmation
    # asked once: never again after the answer
    assert flow.steps(world.session.messages).count(flow.FLOW_QUESTION) == 1


@pytest.mark.asyncio
async def test_conditions_named_first_are_not_asked_and_are_said(monkeypatch):
    world = await _run(monkeypatch, [("say", "CFD of water through this elbow at 2 m/s"), ("stage", INTERNAL_DECL)])
    assert flow.FLOW_QUESTION not in flow.steps(world.session.messages)
    assert world.session.messages[-1]["content"] == (
        "From what you said, I'm using water at 20 °C, 2 m/s at the inlet, with wall functions.\n\n" + ap.PROCEED_ASK)


@pytest.mark.asyncio
async def test_a_requirement_typed_while_the_question_stands_stays_a_note(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "finer around the air intake")])
    # a fluid word in a mesh requirement is no answer: the plan's conditions stand
    assert _conditions(world)["speed_words"] == "10 m/s" and ap.is_live(world.session.intake_gate.get("approval"))
    assert "Got it:" not in world.session.messages[-1]["content"]


def test_a_stored_conversation_with_the_question_before_the_plan_still_reads():
    old = [{"role": "assistant", "content": "What flows through it, and how fast? I'd go with water at 20 °C at 1 m/s at "
                                            "the inlet, with wall functions (y⁺ 30–300) - OK, or tell me what to change."}]
    assert flow.steps(old) == [flow.FLOW_QUESTION] and flow.is_conditions_question(old[0]["content"])
