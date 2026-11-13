# Responsibility: Verify a confirmed COOLED SOLID is deterministic in the two places the other
# declared cases already are - the application asks its one required question itself (the coolant
# and its rate, with buttons), and a reply that speaks of something built around the part is sent
# back once for a rewrite.
# Boundaries: the real message authority, the real purpose map, the real loop policy and the real
# agent reader, with the store, the repositories and the model stood in for. Nothing here reads a
# source file for a string.
"""`cooled_solid_declared` reached two card STRINGS and forced no verdict and no question.

It had no production caller at all until the requirements card read it, and the card is
presentation: both consumers were labels. So the one case the user asked to be "hella
deterministic" was the one case whose behaviour came entirely out of a prompt block -
agents/intake/agent._block_cooled_solid - and a prompt block is a hope, not a guarantee.

THE TWO MECHANISMS THIS FILE PINS ARE NOT NEW ONES. Each is the existing mechanism for one of the
other two declared cases, pointed at this one:

  1. An FEA part's one required question is asked by the APPLICATION, with buttons, once the part
     is confirmed on its stage: `fea_declared` -> message.analysis_question_due ->
     message._settle_fea_analysis -> purpose_map.ANALYSIS_QUESTION. A cooled solid's one required
     question is the coolant, and the prompt block said to ask it: "what it is (water, a
     water-glycol mix, a dielectric) and how much of it flows". Nothing made the model ask.
     Mirrored as message.coolant_question_due -> message._settle_coolant ->
     purpose_map.COOLANT_QUESTION.

  2. A whole thermal model's replies are held to its vocabulary by a deterministic send-back:
     `thermal_declared` -> agent._thermal_declared -> IntakeExecutionState.thermal ->
     loop_policy.on_plaintext -> thermal_wording.forbidden_words -> turn.THERMAL_WORDS_NUDGE. The
     prompt block's cold-plate half says "Never write far field, freestream, air box, room air,
     cooling air or 'the fluid around the part'", and nothing enforced it. Mirrored as
     agent._cooled_solid_declared -> IntakeExecutionState.cooled_solid ->
     cooled_solid_wording.forbidden_words -> turn.COOLED_SOLID_WORDS_NUDGE.

WHAT THIS FILE WILL NOT ACCEPT: a test that greps a source file, or one that asserts a reader
returns True with nothing reading it. Every assertion below runs through message._settle, through
the real IntakeLoopPolicy.on_plaintext, or through the real node_intake turn.

AND NOTHING THAT WORKS MOVES. An FEA part, a whole thermal model and an ordinary internal-flow
part are each asserted to be asked no coolant question and sent back for none of these words,
because their declarations are different sentences and `cooled_solid_declared` reads only this
one's.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.message as msg
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import purpose_map as pm
from meshpipeline.application.cooled_solid_wording import forbidden_words
from meshpipeline.application.geometry_confirmation import (
    CONFIRMED_MARK,
    FEA_LEAD,
    body_of,
    confirmation_message,
    thermal_confirmation_message,
    with_declaration,
)
from meshpipeline.contracts import user_notes as un

SID = uuid.UUID("cccc1111-2222-4222-b222-cccccccccccc")

#: The Corintis plate as the check confirmed it - the same dict the card test and the prompt
#: block's test use: two solids brazed into one piece with the coolant cut out of them, two 6 mm
#: ports on the same face (project_corintis_port_roles: port_a is the outlet).
PLATE = {
    "flow": "internal", "input_kind": "solid-assembly", "solids": 2, "coolant_inside": True,
    "part": "ColdPlateAssembly_CLEAN.step", "size_mm": [177.5, 41.84, 101.0],
    "unit": "mm", "scale_to_m": 0.001,
    "openings": [
        {"id": 1, "name": "port_a", "role": "outlet", "diameter_mm": 6.0,
         "centroid_mm": [177.0, 20.0, 50.0]},
        {"id": 2, "name": "port_b", "role": "inlet", "diameter_mm": 6.0,
         "centroid_mm": [177.0, 20.0, 80.0]},
    ],
}
#: ...and a whole thermal model's air box: the SAME declared case, a different declaration.
AIR_BOX = {"check_mode": "thermal", "input_kind": "solid-assembly", "flow": "internal",
           "part": "set_top_box", "regions": ["board", "psu", "air"], "solids": 3,
           "coolant_inside": False, "detail": "standard", "size_mm": [300.0, 60.0, 200.0],
           "unit": "mm", "scale_to_m": 0.001}
#: ...and an ordinary internal-flow part, which is neither.
ELBOW = {"flow": "internal", "input_kind": "solid-body", "solids": 1, "coolant_inside": False,
         "part": "elbow.step", "size_mm": [100.0, 40.0, 40.0], "unit": "mm", "scale_to_m": 0.001,
         "openings": [{"id": 1, "name": "inlet", "role": "inlet", "diameter_mm": 20.0,
                       "centroid_mm": [0.0, 20.0, 20.0]},
                      {"id": 2, "name": "outlet", "role": "outlet", "diameter_mm": 20.0,
                       "centroid_mm": [100.0, 20.0, 20.0]}]}

FEA_DECLARATION = (f"{CONFIRMED_MARK} {FEA_LEAD}, input_kind solid-body: the part itself is "
                   "meshed, with no fluid around it. One body. Clean check: The geometry is clean.")


def _plate_declaration():
    return confirmation_message(body_of(dict(PLATE)))


def _chat(declaration, said="mesh this cold plate", after=()):
    """A conversation that reached the stage, confirmed it, and pressed Proceed."""
    return with_declaration([{"role": "user", "content": said}], declaration) + [
        {"role": "user", "content": gh.CONTINUE_TEXT}, *after]


# #
# 1. the words themselves
# #
@pytest.mark.parametrize("text,words", [
    ("I'll put a far field around it at 5 diameters", ["far field"]),
    ("the farfield patch carries the freestream", ["far field", "freestream"]),
    ("the air box sides are open", ["air box"]),
    ("room air at 25 C with cooling air from the fans", ["room air", "cooling air"]),
    ("and the fluid around the part is meshed too", ["the fluid around the part"]),
    ("air flowing around the plate", ["the fluid around the part"]),
    # ...and a cooled solid's own sentences, which must survive untouched. A cold plate's coolant
    # really does flow AROUND things: the pin fins and the channel ribs are inside the passages.
    ("the coolant around the pin fins is resolved across its narrowest width", []),
    ("The metal and the coolant are the two halves of one mesh, sharing every face.", []),
    ("port_b is the inlet and port_a the outlet, at 2 L/min of water.", []),
])
def test_the_vocabulary_check_finds_what_is_built_around_a_part(text, words):
    assert forbidden_words(text) == words


def test_a_name_given_to_the_check_is_not_policed():
    """`names` takes the shape the thermal half passes: a short string that appears in the reply
    verbatim (an ECXML model's own vent, "Inlet_grille"). Given one, it is left alone."""
    assert forbidden_words("you asked about the far field") == ["far field"]
    assert forbidden_words("you asked about the far field", ["the far field"]) == []


def test_a_whole_user_message_is_not_a_name_and_does_not_exempt_anything():
    """THE SHAPE PRODUCTION ACTUALLY PASSES, which is not the one above. loop_policy hands this
    check `exec_state.user_texts` - whole user MESSAGES - and a sentence does not appear inside a
    reply, so nothing is blanked out. MEASURED 2026-11-12 through the real function.

    Pinned because the test above cannot fail on it: it passes a phrase, and the only caller
    passes messages. The behaviour is recorded in loop_policy beside the call, with the one-line
    change if the exemption is ever wanted."""
    asked = "what about the far field around it?"
    reply = "You asked about the far field; there is no far field here, only the passages."
    assert forbidden_words(reply, [asked]) == ["far field"]
    # ...and the same words, in the shape `names` is for, are exempt - so the difference is the
    # shape and not the check. Every occurrence has to be the named one: this check reads what is
    # left after the names are blanked out, so the second "no far field" in the reply above is
    # still found whatever is passed.
    assert forbidden_words("You asked about the far field.", ["the far field"]) == []
    assert forbidden_words(reply, ["the far field"]) == ["far field"]


# #
# 2. the one required question - the FEA path's mechanism, on this case
# #
async def _settle(monkeypatch, messages, *, applies=None):
    monkeypatch.setattr(gh, "hold_applies", lambda sid: applies)
    monkeypatch.setattr(gh, "queue_naming", lambda *a: True)
    monkeypatch.setattr(gh, "stored_thermal", lambda sid: (False, ""))

    async def _mode(*a):
        return ""
    monkeypatch.setattr(gh, "upload_check_mode", _mode)

    async def _interp(*a):
        return {"unit": "mm", "basis": "file_declared"}
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    locked = SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=None,
                             geometry_interpretation_id="i1", intake_gate={}, messages=[])
    last = [m for m in messages if m["role"] == "user"][-1]["content"]
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=last,
                                 organization_id="o")
    return await msg._settle(inbound, AsyncMock(), gate={}, locked=locked, messages=messages,
                             revision="r1", session_repo=repo, logger=MagicMock())


@pytest.mark.asyncio
async def test_after_the_stage_a_cold_plates_coolant_is_the_one_question(monkeypatch):
    out = await _settle(monkeypatch, _chat(_plate_declaration()))
    assert out.status is msg.MessageStatus.purpose_question, out
    assert out.reply == pm.COOLANT_QUESTION
    assert out.reply == "What is the coolant, and how much of it flows?"
    labels = [c["label"] for c in pm.choices_for(out.reply)]
    assert labels == ["Water", "A water-glycol mix", "A dielectric fluid"]
    assert all(c["soon"] is False for c in pm.choices_for(out.reply))


@pytest.mark.asyncio
async def test_the_part_is_still_drawn_before_anything_is_asked(monkeypatch):
    """The question comes AFTER the stage, like the analysis question. A coolant question that
    preempted the geometry hold would mean the plate was never drawn: the first message still
    hands the words to the naming step and holds."""
    out = await _settle(monkeypatch, [{"role": "user", "content": "mesh this cold plate"}],
                        applies="queue")
    assert out.status is msg.MessageStatus.geometry_hold, out
    assert out.reply != pm.COOLANT_QUESTION
    assert not msg.coolant_question_due([{"role": "user", "content": "mesh this cold plate"}])


@pytest.mark.asyncio
async def test_every_coolant_button_reads_back_as_its_own_coolant(monkeypatch):
    """A click must read exactly as typing the words would, or the question comes back."""
    for (_key, label), choice in zip(pm.COOLANTS, pm.choices_for(pm.COOLANT_QUESTION)):
        assert choice["send"] == label
        assert pm.coolant_of(choice["send"]) == _key_for(label)
        out = await _settle(monkeypatch, _chat(
            _plate_declaration(), after=[{"role": "assistant", "content": pm.COOLANT_QUESTION},
                                         {"role": "user", "content": choice["send"]}]))
        assert out.status is msg.MessageStatus.proceed, (label, out)


def _key_for(label):
    return next(k for k, lab in pm.COOLANTS if lab == label)


@pytest.mark.asyncio
@pytest.mark.parametrize("said", [
    "mesh this cold plate, water at 2 L/min",
    "cold plate with a 50/50 water-glycol mix",
    "immersion-cooled plate, a dielectric fluid at 4 L/min",
    "deionised water through this cold plate",
])
async def test_the_coolant_is_not_asked_when_the_user_already_said_it(monkeypatch, said):
    out = await _settle(monkeypatch, _chat(_plate_declaration(), said=said))
    assert out.status is msg.MessageStatus.proceed, out
    assert pm.coolant_of(said) != ""


@pytest.mark.asyncio
async def test_the_coolant_is_never_asked_twice(monkeypatch):
    """Once, and then the conversation goes on even if the answer settled nothing - the same rule
    the analysis question is asked under."""
    asked = _chat(_plate_declaration(),
                  after=[{"role": "assistant", "content": pm.COOLANT_QUESTION},
                         {"role": "user", "content": "not sure yet"}])
    out = await _settle(monkeypatch, asked)
    assert out.status is msg.MessageStatus.proceed, out


@pytest.mark.asyncio
async def test_a_purpose_button_answer_is_not_read_as_a_coolant(monkeypatch):
    """The words a purpose-question button sends are about the PATH. "Cooling with air or liquid"
    carries no coolant, so the question is still due - the rule analysis_question_due states."""
    via_button = with_declaration(
        [{"role": "user", "content": "thermal"},
         {"role": "assistant", "content": pm.THERMAL_QUESTION},
         {"role": "user", "content": pm.THERMAL_CHOICES[0][1]}],
        _plate_declaration()) + [{"role": "user", "content": gh.CONTINUE_TEXT}]
    assert msg.coolant_question_due(via_button)
    out = await _settle(monkeypatch, via_button)
    assert out.reply == pm.COOLANT_QUESTION


# #
# 3. ...and the three cases that must not move
# #
@pytest.mark.asyncio
async def test_an_fea_part_is_asked_its_analysis_and_no_coolant(monkeypatch):
    out = await _settle(monkeypatch, _chat(FEA_DECLARATION, said="FEA"))
    assert out.status is msg.MessageStatus.purpose_question
    assert out.reply == pm.ANALYSIS_QUESTION, out
    assert not msg.coolant_question_due(_chat(FEA_DECLARATION, said="FEA"))


@pytest.mark.asyncio
async def test_a_thermal_model_is_asked_no_coolant(monkeypatch):
    box = thermal_confirmation_message(body_of(dict(AIR_BOX)))
    said = _chat(box, said="mesh this thermal model")
    assert not msg.coolant_question_due(said)
    out = await _settle(monkeypatch, said)
    assert out.status is msg.MessageStatus.proceed, out


@pytest.mark.asyncio
async def test_an_ordinary_internal_flow_part_is_asked_no_coolant(monkeypatch):
    said = _chat(confirmation_message(body_of(dict(ELBOW))), said="water through this elbow")
    assert not msg.coolant_question_due(said)
    out = await _settle(monkeypatch, said)
    assert out.status is msg.MessageStatus.proceed, out


def test_the_coolant_question_has_no_buttons_under_any_other_reply():
    assert pm.choices_for("What fluid is it?") == []
    assert pm.choices_for(pm.COOLANT_QUESTION.lower()) == []
    assert pm.choices_for("") == []


def test_the_console_gets_the_buttons_back_on_a_reload():
    """The route carries an application question's buttons in two places: on the answer itself
    (api/v1/chat, under `outcome.answered`) and again under the conversation's last message when
    the console reloads. Run through the route's own reader, not a copy of it."""
    from meshpipeline.api.v1.chat import _last_choices

    assert [c["label"] for c in _last_choices(
        [{"role": "user", "content": "x"},
         {"role": "assistant", "content": pm.COOLANT_QUESTION}])] == [
        "Water", "A water-glycol mix", "A dielectric fluid"]
    # ...and the FEA path's question still gets its own, unchanged
    assert len(_last_choices([{"role": "assistant", "content": pm.ANALYSIS_QUESTION}])) == 7
    assert _last_choices([{"role": "assistant", "content": "anything else"}]) == []


# #
# 4. the vocabulary send-back, through the real turn
# #
def _tc(name, args):
    return SimpleNamespace(id=f"t-{name}",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name,
                                         arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


class _Model:
    """The model's rounds, scripted; every user line it was handed is kept."""

    def __init__(self, rounds):
        self.rounds, self.user_lines = iter(rounds), []

    async def __call__(self, **kw):
        messages = kw.get("messages") or []
        self.user_lines = [m.get("content") for m in messages
                           if isinstance(m, dict) and m.get("role") == "user"]
        return next(self.rounds)


def _turn(model, declaration, *, said="mesh this cold plate"):
    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.application import geometry_check as gc

    messages = [{"role": "assistant", "content": un.QUESTION}, {"role": "user", "content": "no"},
                {"role": "user", "content": said},
                {"role": "assistant", "content": declaration},
                {"role": "user", "content": gh.CONTINUE_TEXT}]
    ref = {"source_id": "s1", "owner_id": "u", "object_key": "k", "sha256": "a" * 64,
           "size_bytes": 10, "original_filename": "ColdPlateAssembly_CLEAN.step",
           "suffix_hint": ".step"}
    state = {"job_id": "j", "session_id": str(SID), "user_id": "u", "messages": messages,
             "geometry": {"ref": ref}}

    async def _measured(state, msgs):
        return None
    with patch.object(llm_router, "call_intake_model", model), \
            patch.object(intake, "_measured_recommendation", _measured), \
            patch.object(gc, "read_check", lambda sid: {"facts": {"fluid_volume": {"found": True}}}):
        return asyncio.run(intake.node_intake(state))


def test_a_reply_that_builds_something_around_the_plate_is_sent_back_once():
    """THE WHOLE PATH, in one turn: the declaration is read by agent._cooled_solid_declared, the
    flag reaches IntakeExecutionState.cooled_solid, the policy reads the reply through
    cooled_solid_wording and returns turn.COOLED_SOLID_WORDS_NUDGE, and the rewrite is what the
    user is shown. Nothing here asserts on a source file."""
    model = _Model([
        _resp(content="I'll put a far field around the plate and mesh the cooling air in the "
                      "air box."),
        _resp(content="Both regions in one mesh: the metal and the coolant in its passages, "
                      "sharing every face, with port_b as the inlet."),
    ])
    out = _turn(model, _plate_declaration())
    assert turn.COOLED_SOLID_WORDS_NUDGE in model.user_lines
    assert out["messages"][-1]["content"].startswith("Both regions in one mesh")


def test_a_cold_plates_own_reply_is_not_sent_back():
    model = _Model([
        _resp(content="The coolant around the pin fins is resolved across its narrowest width, "
                      "and the metal and the coolant share every face between them."),
    ])
    out = _turn(model, _plate_declaration())
    assert turn.COOLED_SOLID_WORDS_NUDGE not in model.user_lines
    assert out["messages"][-1]["content"].startswith("The coolant around the pin fins")


def test_an_fea_parts_reply_is_not_held_to_a_cold_plates_words():
    """The flag is read off this case's declaration and no other. An FEA part's reply says "with
    no fluid around it" in its own declaration, and nothing about it is a cooled solid."""
    model = _Model([
        _resp(content="Every face of the part is named, so you set supports and loads in your own "
                      "solver. There is no fluid around the part at all."),
    ])
    out = _turn(model, FEA_DECLARATION, said="static stress on this bracket")
    assert turn.COOLED_SOLID_WORDS_NUDGE not in model.user_lines
    assert out["messages"][-1]["content"].startswith("Every face of the part is named")


# #
# 5. the flag itself, on the real state the policy reads
# #
class _Tally:
    """A tally with rounds left, so the send-back is allowed to happen."""

    def remaining_rounds(self, _limits):
        return 3


_TALLY = _Tally()


def _policy(messages, reply):
    state = IntakeExecutionState(
        session_id=str(SID), owner_id="u", revision="r1", user_msg_count=1,
        latest_user_msg="x", user_texts=turn.user_texts(messages),
        conversation=tuple(messages),
        declared_case=intake._declared_case(messages),
        thermal=intake._thermal_declared(messages),
        cooled_solid=intake._cooled_solid_declared(messages))
    policy = IntakeLoopPolicy(
        exec_state=state,
        executor=IntakeToolExecutor(state=state, job_id="j", implemented_engines=("cfmesh",),
                                    search_tool=None, trace=None))
    policy.plaintext_text = reply
    return policy, policy.on_plaintext(_TALLY)


@pytest.mark.parametrize("declaration_of,flag", [("plate", True), ("box", False),
                                                 ("elbow", False), ("fea", False)])
def test_only_this_cases_declaration_sets_the_flag(declaration_of, flag):
    declaration = {"plate": _plate_declaration(),
                   "box": thermal_confirmation_message(body_of(dict(AIR_BOX))),
                   "elbow": confirmation_message(body_of(dict(ELBOW))),
                   "fea": FEA_DECLARATION}[declaration_of]
    messages = _chat(declaration)
    assert intake._cooled_solid_declared(messages) is flag
    _p, decision = _policy(messages, "I'll set up the far field around the part.")
    sent_back = decision is not None and decision.message == turn.COOLED_SOLID_WORDS_NUDGE
    assert sent_back is flag, decision


def test_a_model_that_asks_the_coolant_again_is_caught_by_the_repeat_rule():
    """THE PROMPT BLOCK STILL TELLS THE MODEL TO ASK THE COOLANT, and it is left saying so: the
    application's question goes into the conversation, and the loop's own repeat-question rule
    reads the application's questions as prior questions. So a model that asks it again is sent
    back by a mechanism that already exists, and no third one was added for this.

    MEASURED here by running the real loop_policy.repeats_a_question against the real
    COOLANT_QUESTION - the reply is not hand-matched to the question text.
    """
    from meshpipeline.agents.intake.loop_policy import repeats_a_question

    # THE PRIOR LIST IS COLLECTED, NOT HANDED OVER. agent.node_intake builds it with
    # turn.prior_assistant_texts, which takes every assistant line including the application's
    # own, and _settle_coolant appends the question as an assistant message - so the question is
    # in the list on the next turn because it is in the conversation.
    asked = _chat(_plate_declaration(),
                  after=[{"role": "assistant", "content": pm.COOLANT_QUESTION}])
    prior = turn.prior_assistant_texts(asked)
    assert pm.COOLANT_QUESTION in prior, prior
    assert repeats_a_question("What is the coolant, and how much of it flows?", prior)
    assert repeats_a_question("What coolant is it, and how much of it flows through the plate?",
                              prior)
    # ...and a different question is not a repeat
    assert not repeats_a_question("Which face would you like the inlet on?", prior)
    # ...and before it is asked there is nothing to repeat, so the model is free to ask it once
    assert not repeats_a_question("What is the coolant, and how much of it flows?",
                                  turn.prior_assistant_texts(_chat(_plate_declaration())))


def test_a_user_who_raises_the_word_themselves_still_gets_the_reply_sent_back():
    """THE REAL POLICY, not the function: what `names` is given here is whole user messages, so a
    user who asks about a far field does NOT exempt the model's honest answer about it. MEASURED
    2026-11-12 by running IntakeLoopPolicy.on_plaintext on a conversation whose own first message
    says "far field".

    This is the behaviour, and it is pinned rather than argued: the guard never lets the word
    through, the cost is one send-back per turn on a reply the user invited, and the exemption is
    a change to what a cold-plate owner is shown. loop_policy records the one-line change beside
    the call if it is ever wanted."""
    messages = _chat(_plate_declaration(),
                     said="mesh this cold plate - do I need a far field around it?")
    assert turn.user_texts(messages)[0].endswith("far field around it?")
    _p, decision = _policy(
        messages, "There is no far field here: the coolant is inside the plate's own passages.")
    assert decision is not None and decision.message == turn.COOLED_SOLID_WORDS_NUDGE, decision
    # ...and the reply that says nothing of the kind is delivered on the same conversation, so the
    # send-back above is about the word and not about the user's question
    _p2, ok = _policy(messages, "Both regions in one mesh, sharing every face, port_b the inlet.")
    assert ok is None or ok.message != turn.COOLED_SOLID_WORDS_NUDGE, ok


def test_the_send_back_happens_once_and_then_the_reply_is_delivered():
    """The thermal check's rule exactly: once, so a model that insists still ends the turn and the
    user is never left waiting on a loop."""
    messages = _chat(_plate_declaration())
    policy, first = _policy(messages, "far field around the part")
    assert first.message == turn.COOLED_SOLID_WORDS_NUDGE and first.complete is False
    policy.plaintext_text = "still a far field around the part"
    second = policy.on_plaintext(_TALLY)
    assert second is None or second.message != turn.COOLED_SOLID_WORDS_NUDGE, second
