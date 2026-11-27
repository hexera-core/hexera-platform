"""CFDREQ-37 (amended): what the user typed is said back with the values the application stored.

- flow conditions typed at the plan card ("make it 50 m/s") redo the plan: the card's conditions sentence carries
  the new value and the reply says it;
- a typed through/around answer, a typed answer to "What are you meshing this for?" and a typed FEA analysis are
  each said back in one short sentence before the step's own line;
- the model writes the sentence under a guard; a wrong one is refused and the code's sentence said; a click on one
  of our buttons adds nothing."""
from __future__ import annotations

import pytest
from tests.unit.agents.intake.test_standard_questionnaire import (
    BAR,
    EXTERNAL_DECL,
    FEA_DECL,
    INTERNAL_DECL,
    _run,
)

import meshpipeline.agents.intake.approval as ap
from meshpipeline.agents.intake import flow
from meshpipeline.agents.intake import step_ack as SA
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts import flow_conditions as FC

CAR = [("say", "external aero over this car"), ("stage", EXTERNAL_DECL)]
THROUGH, AROUND = (send for _label, send in flow.FLOW_KIND_CHOICES)


def _last(world) -> str:
    return str(world.session.messages[-1]["content"])


def _model_says(monkeypatch, text: str) -> list:
    """The step-ack call answered by a stand-in that says `text` (the calls are recorded)."""
    calls: list = []

    async def _ask(messages, *, job_id, user_id):
        calls.append(messages)
        return text
    monkeypatch.setattr(SA, "_ask", _ask)
    return calls


# ------------------------------------------------------------------ 1. conditions at the plan card ----

@pytest.mark.asyncio
async def test_a_speed_typed_at_the_plan_card_redoes_the_plan_and_is_said_back(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "OK"), ("say", "make it 50 m/s")])
    got = FC.read(world.session.request_txt, world.session.intake_patches)
    # the plan the card shows carries the new speed, once - the old sentence is gone
    assert got["speed_words"] == "50 m/s" and world.session.request_txt.count("Fluid: ") == 1
    assert ap.is_live(world.session.intake_gate.get("approval"))
    text = _last(world)
    assert text.endswith("Got it: air at 20 °C, 50 m/s, with wall functions.\n\n" + ap.PROCEED_ASK), text
    assert "Kept." not in text
    # read in code: no model picked the step
    assert world.model.calls == 0


@pytest.mark.asyncio
async def test_a_wall_treatment_typed_at_the_plan_card_is_the_new_plans(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "OK"), ("say", "resolve the wall please")])
    got = FC.read(world.session.request_txt, world.session.intake_patches)
    assert got["wall"] == "resolved" and got["speed_words"] == "10 m/s"
    assert "with a resolved wall (y⁺ ≈ 1).\n\n" + ap.PROCEED_ASK in _last(world)


@pytest.mark.asyncio
async def test_a_note_that_is_more_than_conditions_stays_a_note(monkeypatch):
    world = await _run(monkeypatch, CAR + [("say", "OK"), ("say", "finer around the air intake")])
    # no fluid was named: the plan's conditions are what they were
    assert FC.read(world.session.request_txt, world.session.intake_patches)["fluid"] == "air"
    assert "Got it:" not in _last(world)


def test_a_summary_redone_with_the_old_sentence_takes_the_typed_value():
    msgs = [{"role": "user", "content": "external aero over this car"}, {"role": "assistant", "content": EXTERNAL_DECL},
            {"role": "assistant", "content": flow.conditions_question([])},
            {"role": "user", "content": "OK"}, {"role": "user", "content": "make it 50 m/s"}]
    old = ("External flow around the car. " + FC.sentence("air at 20 °C", "10 m/s", "functions", internal=False)
           + f" {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment. The user's own words are kept.")
    args = flow.with_conditions_sentence(msgs, {"request_txt": old, "patches": []})
    got = FC.read(args["request_txt"])
    assert got["speed_words"] == "50 m/s" and args["request_txt"].count("Fluid: ") == 1
    assert FC.accepted(args["request_txt"]) == {"the fluid", "the wall treatment"}
    assert "The user's own words are kept." in args["request_txt"]


def test_the_conditions_sentence_is_taken_out_whole():
    s = FC.sentence("oil", "2.5 m/s", "functions", internal=True)
    text = f"Internal flow. {s} {FC.ACCEPTED_LEAD} the wall treatment. Openings: a, b."
    assert FC.without(text) == "Internal flow. Openings: a, b."


# ------------------------------------------------------------------------- 3. through or around ----

@pytest.mark.asyncio
async def test_a_typed_flow_kind_answer_is_said_back_before_the_stage_line(monkeypatch):
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BAR), ("say", "the water flows through it")])
    out = world.answers[-1]
    assert out.stage_flow["flow"] == "internal"
    assert out.reply == f"{gh.DRAWING_MARK} Okay, flow through the part. Checking your part - confirm it on the right " \
                        "and press Proceed."
    assert flow.steps(world.session.messages)[-1] == flow.STAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("button", [THROUGH, AROUND])
async def test_a_flow_kind_button_stays_the_stage_line(monkeypatch, button):
    calls = _model_says(monkeypatch, "Okay, flow through the part.")
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BAR), ("say", button)])
    assert world.answers[-1].reply == gh.HOLD_REPLY
    # (the typed first answer was said back once; the click asked nothing more)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_models_sentence_is_said_when_it_holds_the_stored_flow(monkeypatch):
    calls = _model_says(monkeypatch, "Got it, the flow goes through the part.")
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BAR), ("say", "the water flows through it")])
    assert world.answers[-1].reply.startswith(f"{gh.DRAWING_MARK} Got it, the flow goes through the part. Checking")
    assert calls


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["Okay, flow around the part.", "Okay, flow through the part. Press Proceed.",
                                   "Okay, flow through the part?", "Okay, flow through the part!",
                                   "Okay, water at 20 °C through the part."])
async def test_a_wrong_flow_sentence_is_refused_and_the_plain_one_said(monkeypatch, wrong):
    _model_says(monkeypatch, wrong)
    world = await _run(monkeypatch, [("say", "CFD please"), ("named", BAR), ("say", "the water flows through it")])
    assert world.answers[-1].reply.startswith(f"{gh.DRAWING_MARK} Okay, flow through the part. Checking")


# --------------------------------------------------------------------------- 4. the purpose ----

@pytest.mark.asyncio
@pytest.mark.parametrize("said,ack", [
    ("CFD, external aero over this car", "Okay, a CFD mesh with the flow around the part."),
    ("water flowing through this elbow", "Okay, a CFD mesh with the flow through the part."),
    ("static stress analysis of this bracket", "Okay, an FEA mesh for a static stress analysis."),
    ("FEA", "Okay, an FEA mesh."),
])
async def test_a_typed_purpose_is_said_back_before_the_stage_line(monkeypatch, said, ack):
    world = await _run(monkeypatch, [("say", said)])
    reply = world.answers[-1].reply
    assert reply.startswith(f"{gh.DRAWING_MARK} {ack} "), reply
    assert flow.steps(world.session.messages)[-1] == flow.STAGE


@pytest.mark.asyncio
async def test_a_purpose_button_stays_the_stage_line(monkeypatch):
    from meshpipeline.application import purpose_map as pm
    world = await _run(monkeypatch, [("say", "mesh this please"), ("say", pm.DOMAIN_CHOICES[1][1])])
    # the vague words got the one question (nothing taken: nothing said back); the click got the stage line alone
    assert world.answers[0].reply == pm.DOMAIN_QUESTION
    assert world.answers[-1].reply == f"{gh.DRAWING_MARK} {pm.FEA_HOLD}"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["Okay, an FEA mesh for the car.", "Okay, a CFD mesh with the flow through the part.",
                                   "Okay, a CFD mesh for 4 m of car."])
async def test_a_wrong_purpose_sentence_is_refused(monkeypatch, wrong):
    _model_says(monkeypatch, wrong)
    world = await _run(monkeypatch, [("say", "CFD, external aero over this car")])
    assert world.answers[-1].reply.startswith(f"{gh.DRAWING_MARK} Okay, a CFD mesh with the flow around the part. ")


# ------------------------------------------------------------------------------ 5. the analysis ----

@pytest.mark.asyncio
async def test_a_typed_analysis_is_said_back_before_the_plan_line(monkeypatch):
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "a modal one please")])
    assert "vibration (modal) analysis" in world.session.request_txt
    assert _last(world) == "Okay, a vibration (modal) analysis.\n\n" + ap.PROCEED_ASK
    assert flow.steps(world.session.messages)[-1] == flow.PLAN


@pytest.mark.asyncio
async def test_an_analysis_button_stays_the_plan_line(monkeypatch):
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "Vibration (modal)")])
    assert _last(world) == ap.PROCEED_ASK


@pytest.mark.asyncio
async def test_a_wrong_analysis_sentence_is_refused(monkeypatch):
    _model_says(monkeypatch, "Sure, a buckling analysis.")
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "a modal one please")])
    assert _last(world) == "Okay, a vibration (modal) analysis.\n\n" + ap.PROCEED_ASK


@pytest.mark.asyncio
async def test_a_good_analysis_sentence_is_said(monkeypatch):
    _model_says(monkeypatch, "Sure, a modal analysis then.")
    world = await _run(monkeypatch, [("say", "FEA"), ("stage", FEA_DECL), ("say", "a modal one please")])
    assert _last(world) == "Sure, a modal analysis then.\n\n" + ap.PROCEED_ASK


# ------------------------------------------------------------------------------------ the pieces ----

@pytest.mark.asyncio
async def test_the_model_is_never_waited_on_past_the_time_cap():
    import asyncio

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "Okay, flow through the part."
    f = SA.facts(SA.FLOW_KIND, words="through it", flow="internal")
    got = await SA.written(f, ask_fn=slow, timeout_s=0.05)
    assert got == {"text": "Okay, flow through the part.", "source": SA.SOURCE_FALLBACK, "why": "no answer (TimeoutError)"}
    assert SA.TIMEOUT_S <= 6.0


def test_every_plain_sentence_passes_its_own_guard():
    for f in (SA.facts(SA.FLOW_KIND, words="x", flow="external"),
              SA.facts(SA.PURPOSE, words="x", route="flow", flow="internal"),
              SA.facts(SA.PURPOSE, words="x", route="fea", analysis="impact", analysis_label="Impact or crash"),
              SA.facts(SA.PURPOSE, words="x", route="electronics"),
              SA.facts(SA.ANALYSIS, words="x", analysis="thermal", analysis_label="Thermal or thermal stress")):
        assert f["plain_version_that_is_exactly_right"] and SA.check(f["plain_version_that_is_exactly_right"], f) == "", f


def test_nothing_taken_says_nothing():
    assert SA.facts(SA.PURPOSE, words="hello")["plain_version_that_is_exactly_right"] == ""


def test_the_prompt_blocks_are_the_plan_steps_voice_and_language():
    names = [b[0] for b in SA.STEP_ACK_PROMPT_BLOCKS]
    assert names[0] == "voice" and names[-1] == "language" and "taken" in SA.compose_system()


@pytest.mark.asyncio
async def test_conditions_said_at_the_internal_plan_card_say_the_inlet(monkeypatch):
    world = await _run(monkeypatch, [("say", "water flowing through this elbow"), ("stage", INTERNAL_DECL),
                                     ("say", "OK"), ("say", "make it 3 m/s")])
    assert FC.read(world.session.request_txt, world.session.intake_patches)["speed_words"] == "3 m/s"
    assert _last(world).endswith("Got it: water at 20 °C, 3 m/s at the inlet, with wall functions.\n\n"
                                 + ap.PROCEED_ASK)
