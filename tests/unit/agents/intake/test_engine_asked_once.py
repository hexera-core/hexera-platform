# Responsibility: Verify the engine is asked ONCE, as the application's own question carrying a proposal
# and a one-line reason, and that no later reply restates it or carries it as an "assumption".
# Boundaries: engine_selection's question and its readers, loop_policy.engine_nudge, and whole intake
# conversations end to end with the model's rounds scripted (as test_engine_never_asked_twice does).
"""A real session on shared dev (a rocket nozzle, 2026-09-30) opened three replies in a row with:

  "I'll record `snappyHexMesh` as the meshing-engine assumption, since the engine choice was not stated.
   For the CFD case, should I use a steady, single-phase, incompressible air-flow setup ...?"
  "I'll use `snappyHexMesh` as an unstated assumption, with a body-fitted internal fluid mesh and
   approximately 8 prism layers. Should this be a full 3D nozzle mesh ...?"
  "I'll carry forward `snappyHexMesh` as the unconfirmed engine assumption, with a full 3D internal fluid
   mesh. For the mesh, I propose ..."

The engine was never asked - it was assumed, then restated every turn in bookkeeping words, each time in
front of two or three more questions. The soak transcripts (~/geosweep/soak_full.jsonl) show the same:
"I'll carry snappyHexMesh forward as an assumption", "I'll use snappyHexMesh as an assumed toolchain"."""
from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
from meshpipeline.agents.intake import turn
from meshpipeline.agents.intake import vocabulary as _vocab
from meshpipeline.agents.intake.loop_policy import engine_nudge

_ENGINES = {"snappy": "snappyHexMesh", "cfmesh": "cfMesh", "gmsh": "Gmsh", "vmtk": "VMTK"}
_REASON = "it fits the mesh to the nozzle's curved walls and grows thin layers along them"
_QUESTION = ("I'd mesh this with snappyHexMesh: it fits the mesh to the nozzle's curved walls and grows "
             "thin layers along them. OK, or do you use a different mesher?")
_NUDGES = (turn.ENGINE_SETTLED_NUDGE, turn.ENGINE_UNSETTLED_NUDGE)
#: The words a reply used to narrate its bookkeeping with.
_BOOKKEEPING = ("assum", "unconfirmed", "unstated", "carry forward", "carried forward", "not stated")


# ------------------------------------------------------------------ the one engine question ----
def test_the_engine_question_is_one_plain_line_with_the_proposal_and_its_reason():
    assert es.render_selection_statement("snappy", _REASON) == _QUESTION
    # the name a user reads, never the registry key; no bookkeeping around the yes/no
    assert es.render_selection_statement("gmsh") == "I'd mesh this with Gmsh. OK, or do you use a different mesher?"
    for engine in _ENGINES:
        text = es.render_selection_statement(engine, "it suits this part").lower()
        assert text.count("?") == 1 and "selected engine" not in text and "not a selection" not in text
        assert not any(w in text for w in _BOOKKEEPING)


@pytest.mark.parametrize("reason,shown", [
    ("It fits the mesh to the curved walls.", "it fits the mesh to the curved walls"),     # reads on after the colon
    ("because it grows thin layers on the walls", "it grows thin layers on the walls"),
    ("CFD-grade layers on the curved walls", "CFD-grade layers on the curved walls"),      # an acronym stays
    (r"it resolves the wall down to \(y^+ \approx 1\)", "it resolves the wall down to y⁺ ≈ 1"),  # no LaTeX reaches the chat
    ("  it  fits\n the walls ", "it fits the walls"),
])
def test_the_reason_is_printed_as_one_plain_line(reason, shown):
    assert es.proposal_reason("snappy", reason) == shown


@pytest.mark.parametrize("reason", [
    "",
    "it is better than cfMesh here",                        # the question offers ONE engine
    "snappyHexMesh multi-region is overkill, so this one",  # a longer name holding the short one is another engine
    "is that what you use?",                                 # a question of its own
    "it " + "fits the walls and " * 12,                      # not one line any more
])
def test_a_reason_that_cannot_stand_in_front_of_the_user_is_left_out(reason):
    assert es.proposal_reason("snappy", reason) == ""
    assert es.render_selection_statement("snappy", reason) == \
        "I'd mesh this with snappyHexMesh. OK, or do you use a different mesher?"


# --------------------------------------------------------- answers to the engine question ----
_YES = ["ok", "OK!", "sure", "yes", "yes please", "sounds good", "go with that", "fine", "yep", "that works",
        "perfect", "ok, go with it", "that's fine", "sounds good to me", "fine by me", "go ahead", "👍"]
_HANDED_BACK = ["your call", "you decide", "up to you", "whatever you think is best", "you pick",
                "use your suggestion", "go with your proposal", "I don't know, your call", "no idea, you decide"]
_NOT_YES = ["use cfMesh", "no, cfMesh", "not that one", "no", "I'd rather not", "what's the difference?",
            "cfMesh please", "no, use gmsh", "don't use snappy", "not snappyHexMesh", "wait",
            "is that the best one?", "why snappyHexMesh?", "your call, but not snappy",
            "you decide, but I don't want that", "whatever you think, but forget it",
            "I don't know what snappyHexMesh is"]


def _answer(message: str) -> dict | None:
    sel = es.propose("snappy", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    return es.confirm_by_assent(sel, session_id="s", owner_id="u", revision="r2",
                                latest_user_message=message, user_msg_count=2)


def test_the_fuzz_of_yes_and_hand_backs_confirms_the_proposed_engine_before_the_model_runs():
    # "your call" to a question that named ONE engine is the answer: the engine it named stands. The
    # model used to be left to guess - and re-proposed it, which asked the user a second time.
    rng = random.Random(20260930)
    openers, closers = ["", "great, ", "ok, ", "Alright - "], ["", ".", "!", " thanks", ", thanks!"]
    for _ in range(400):
        said = rng.choice(openers) + rng.choice(_YES + _HANDED_BACK) + rng.choice(closers)
        got = _answer(said)
        assert got is not None and got["state"] == es.CONFIRMED and got["engine"] == "snappy", said


@pytest.mark.parametrize("said", _NOT_YES)
def test_another_engine_a_no_or_a_question_is_never_read_as_the_answer(said):
    assert _answer(said) is None


# ------------------------------------------------ how a reply may speak of the engine ----
#: Replies that carry or restate the engine - the three from the nozzle session, the soak's, and
#: the milder "for the X setup" that names it where nothing asked for it.
_CARRYING = [
    "I'll record `{e}` as the meshing-engine assumption, since the engine choice was not stated. For the "
    "CFD case, should I use a steady, single-phase, incompressible air-flow setup?",
    "I'll use `{e}` as an unstated assumption, with a body-fitted internal fluid mesh and approximately 8 "
    "prism layers. Should this be a full 3D nozzle mesh?",
    "I'll carry forward `{e}` as the unconfirmed engine assumption, with a full 3D internal fluid mesh. "
    "For the mesh, I propose 8 layers - ok?",
    "I'll carry **{e}** forward as an assumption—the toolchain was not explicitly stated. For the mesh "
    "controls, should I use 5 lengths upstream?",
    "I'll assume **{e}** as the meshing toolchain for this external flow; you did not explicitly state an "
    "engine. Should I use air at 15 °C?",
    "For the {e} setup, should I use 10 prism layers?",
    "Using {e}, I'd refine the throat. OK?",
    "The engine is already confirmed as **{e}**. For the wall treatment, is the wall smooth?",
]
_CLEAN = [
    "Flow conditions - I'd go with:\n1. Air at 15 °C and sea-level pressure\n2. 10 m/s at the inlet\n"
    "OK, or tell me what to change.",
    "Near-wall layers: I'd use 8 layers at y⁺ 30–300. OK?",
    "Should the throat get its own refinement zone? I'd say yes.",
]


def _confirmed(engine: str = "snappy", revision: str = "r-earlier") -> dict:
    return {**es.propose(engine, session_id="s", owner_id="u", revision="r0", user_msg_count=0),
            "state": es.CONFIRMED, "confirmed_revision": revision,
            "expires_at": es.time.time() + es.CONFIRMED_TTL_S}


def test_the_fuzz_of_replies_that_carry_the_engine_are_all_sent_back():
    rng = random.Random(930)
    for _ in range(400):
        key = rng.choice(list(_ENGINES))
        spelling = rng.choice((_ENGINES[key], key, _ENGINES[key].lower(), _ENGINES[key].upper()))
        reply = rng.choice(_CARRYING).format(e=spelling)
        user = rng.choice(("yes", "20 m/s", "fine, 8 layers", "ok", "the outlet is on the right"))
        # settled earlier: leave it out; nothing settled: propose it through the one question
        assert engine_nudge(reply, selection=_confirmed(key), latest_user_msg=user,
                            revision="r-now") == turn.ENGINE_SETTLED_NUDGE, reply
        for unsettled in (None, es.propose(key, session_id="s", owner_id="u", revision="r0", user_msg_count=0)):
            assert engine_nudge(reply, selection=unsettled, latest_user_msg=user,
                                revision="r-now") == turn.ENGINE_UNSETTLED_NUDGE, reply


@pytest.mark.parametrize("reply,selection,user,revision", [
    (_CLEAN[0], _confirmed(), "ok", "r-now"),                                  # names no engine
    (_CLEAN[1], None, "ok", "r-now"),
    ("snappyHexMesh fits the cells to the wall; cfMesh staircases it. Keep it?",
     _confirmed(), "why snappyHexMesh and not cfMesh?", "r-now"),              # the user asked about it
    ("It builds the mesh. snappyHexMesh is the usual one for this. OK?",
     _confirmed(), "what does the mesher do?", "r-now"),
    ("Gmsh it is. What is the flow through the duct?", _confirmed("gmsh", "r-now"), "sure", "r-now"),
    # one acknowledgement, in reply to the very message that settled it
    ("Heads-up: snappyHexMesh may not grow layers into the 0.2 mm gap. Proceed anyway?",
     _confirmed(), "ok", "r-now"),                                             # a soft limitation, once
    ("Which do you use - cfMesh, Gmsh or snappyHexMesh?", None, "air, 20 m/s", "r-now"),  # a menu, not carried
])
def test_what_is_left_alone(reply, selection, user, revision):
    assert engine_nudge(reply, selection=selection, latest_user_msg=user, revision=revision) is None


def test_a_reply_to_an_answer_of_the_engine_question_may_talk_about_it():
    # "why?" to "I'd mesh this with snappyHexMesh ... OK?" is about the engine, in words that do not
    # say so; its explanation is the answer. One message later the question is stale, and an engine
    # the model carries in its own words goes back to the one question.
    asked = es.propose("snappy", session_id="s", owner_id="u", revision="r1", user_msg_count=1)
    why = "snappyHexMesh fits the cells to the curved wall, so the layers stay even. OK with it?"
    assert engine_nudge(why, selection=asked, latest_user_msg="why?", user_msg_count=2) is None
    assert engine_nudge(why, selection=asked, latest_user_msg="20 m/s",
                        user_msg_count=3) == turn.ENGINE_UNSETTLED_NUDGE


# ------------------------------------------------------------- whole conversations ----
def _tc(name, args):
    return SimpleNamespace(id="t", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


class _Model:
    """A scripted model for one turn: its first round as given, then what a model does with each note.

    Sent back for an unsettled engine, it proposes the engine through the one question; sent back for a
    settled one, it writes `clean`; after a tool result, it writes `clean`."""

    def __init__(self, first, clean="", engine="snappy", reason=_REASON):
        self.first, self.clean, self.engine, self.reason = first, clean, engine, reason
        self.notes: list[str] = []

    async def __call__(self, **kw):
        messages = kw.get("messages") or []
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and last.get("content") in _NUDGES:
            self.notes.append(last["content"])
            if last["content"] == turn.ENGINE_UNSETTLED_NUDGE:
                return _resp([_tc("propose_engine_selection", {"engine": self.engine, "reason": self.reason})])
            return _resp(content=self.clean)
        if last.get("role") == "tool":
            return _resp(content=self.clean)
        return self.first


class _Conversation:

    def __init__(self):
        self.messages: list[dict] = [
            {"role": "assistant", "content": "Got your file. What will you use the mesh for?"}]
        self.gate: dict | None = None
        self.notes: list[str] = []

    def turn(self, user: str, model: _Model) -> str:
        import meshpipeline.adapters.model_inference.router as llm_router
        self.messages.append({"role": "user", "content": user})
        state = {"job_id": "j", "session_id": "s", "user_id": "u", "messages": list(self.messages)}
        if self.gate is not None:
            state["intake_gate"] = self.gate
        with patch.object(llm_router, "call_intake_model", model):
            out = asyncio.run(intake.node_intake(state))
        self.gate = out["intake_gate"]
        self.notes += model.notes
        reply = out["messages"][-1]["content"]
        self.messages.append({"role": "assistant", "content": reply})
        return reply

    @property
    def selection(self) -> dict:
        return (self.gate or {}).get("selection") or {}

    def assistant(self) -> list[str]:
        return [m["content"] for m in self.messages[1:] if m["role"] == "assistant"]


def _names_an_engine(text: str) -> bool:
    return bool(_vocab.engines_named_in(text))


def test_the_nozzle_conversation_replayed_asks_the_engine_once_and_never_again():
    c = _Conversation()
    # 1. The model carries the engine as an assumption (the real first reply). It is sent back and
    #    proposes instead - the user reads the application's one question.
    reply = c.turn("CFD of the air flow through this rocket nozzle.", _Model(
        _resp(content=_CARRYING[0].format(e="snappyHexMesh"))))
    assert reply == _QUESTION
    assert c.notes == [turn.ENGINE_UNSETTLED_NUDGE]
    assert c.selection["state"] == es.PROPOSED and c.selection["engine"] == "snappy"

    # 2. "ok" answers it - read by the application before the model runs. The model asks one clear thing.
    reply = c.turn("ok", _Model(_resp(content=_CLEAN[0])))
    assert reply == _CLEAN[0]
    assert c.selection["state"] == es.CONFIRMED
    selected = c.selection["id"]

    # 3. The model restates the engine (the real third reply). Sent back; the user reads the clean ask.
    reply = c.turn("yes, but 20 m/s", _Model(
        _resp(content=_CARRYING[2].format(e="snappyHexMesh")), clean=_CLEAN[1]))
    assert reply == _CLEAN[1]
    assert c.notes[-1] == turn.ENGINE_SETTLED_NUDGE

    # 4. The model proposes the engine the user already confirmed. Nothing is asked and nothing moves.
    reply = c.turn("fine", _Model(
        _resp([_tc("propose_engine_selection", {"engine": "snappy", "reason": _REASON})]), clean=_CLEAN[2]))
    assert reply == _CLEAN[2]
    assert c.selection["state"] == es.CONFIRMED and c.selection["id"] == selected

    # Exactly one engine question, and no reply after it names the engine or narrates an assumption.
    said = c.assistant()
    assert [t for t in said if _names_an_engine(t)] == [_QUESTION]
    assert not any(w in t.lower() for t in said for w in _BOOKKEEPING)

    # 5. Asked about it, the model may talk about it.
    answer = "snappyHexMesh fits the cells to the curved wall; cfMesh would staircase it. Keep it?"
    assert c.turn("why snappyHexMesh and not cfMesh?", _Model(_resp(content=answer))) == answer


def test_why_to_the_engine_question_gets_its_answer_end_to_end():
    c = _Conversation()
    c.turn("internal flow through this nozzle, air", _Model(
        _resp([_tc("propose_engine_selection", {"engine": "snappy", "reason": _REASON})])))
    why = "snappyHexMesh fits the cells to the curved wall, so the layers stay even. OK with it?"
    assert c.turn("why?", _Model(_resp(content=why))) == why
    assert c.notes == []


def test_a_hand_back_to_the_engine_question_is_its_answer_end_to_end():
    c = _Conversation()
    c.turn("internal flow through this nozzle, air", _Model(
        _resp([_tc("propose_engine_selection", {"engine": "snappy", "reason": _REASON})])))
    reply = c.turn("I don't know, your call", _Model(_resp(content=_CLEAN[0])))
    assert c.selection["state"] == es.CONFIRMED and reply == _CLEAN[0]


def test_the_soak_over_conversations_asks_the_engine_exactly_once():
    # 60 seeded conversations: a persona that never names the engine answers the one question in any of
    # the ways people do, then goes on; the scripted model restates the engine on about half its turns,
    # and sometimes proposes it again. The user reads the engine exactly once.
    rng = random.Random(20260930)
    for n in range(60):
        c = _Conversation()
        first = rng.choice((_resp(content=rng.choice(_CARRYING).format(e="snappyHexMesh")),
                            _resp([_tc("propose_engine_selection", {"engine": "snappy", "reason": _REASON})])))
        assert c.turn(rng.choice(("CFD through this nozzle", "air flow through it, 20 m/s", "internal CFD")),
                      _Model(first)) == _QUESTION
        c.turn(rng.choice(_YES + _HANDED_BACK), _Model(_resp(content=_CLEAN[0])))
        assert c.selection["state"] == es.CONFIRMED, n
        for i, answer in enumerate(rng.sample(["yes", "20 m/s", "fine", "ok, 8 layers", "no refinement zone"], 3)):
            clean = f"Question {i + 2} of the setup: the {('far-field', 'patch names', 'throat')[i]} - ok?"
            first = rng.choice((_resp(content=rng.choice(_CARRYING).format(e=rng.choice(("snappyHexMesh", "snappy")))),
                                _resp([_tc("propose_engine_selection", {"engine": "snappy"})]),
                                _resp(content=clean)))
            assert c.turn(answer, _Model(first, clean=clean)) == clean, (n, i)
        said = c.assistant()
        assert [t for t in said if _names_an_engine(t)] == [_QUESTION], (n, said)
        assert c.selection["state"] == es.CONFIRMED, n


# ------------------------------------------------------------------------ the prompt ----
def test_the_prompt_asks_the_engine_once_with_a_proposal_and_never_carries_it():
    block = intake._block_engine_first()
    assert "ONE question, asked ONCE, and" in block and "carries your proposal" in block
    assert "`reason`" in block and "I'd mesh this with X: <reason>" in block
    assert "THE ENGINE IS NEVER AN ASSUMPTION" in block
    assert "ONCE SETTLED, LEAVE IT ALONE" in block
    # the old menu-with-no-proposal is gone, and so is the rule that forbade the proposal
    assert "plus 'not sure'" not in block
    assert "Do NOT recommend a specific engine unless" not in block
    propose = intake._block_propose_first()
    assert "Two things are never proposed" not in propose
    assert "never calls it an assumption" in propose
    assert "ENGINE is proposed and confirmed, never defaulted" in propose


def test_the_ask_plainly_block_is_registered_and_composed():
    names = [b[0] for b in intake.INTAKE_PROMPT_BLOCKS]
    assert names.index("ask_plainly") == names.index("propose_first") + 1
    block = intake._block_ask_plainly()
    assert block in intake.compose_intake_system()
    low = block.lower()
    assert "one clear thing" in low and "numbered list" in low and "never two separate questions" in low
    for word in ("'as an assumption'", "'unconfirmed'", "'i'll carry forward'"):
        assert word in low, word


def test_the_propose_tool_takes_a_reason_and_says_it_is_the_one_way_the_engine_is_asked():
    fn = next(t["function"] for t in intake.INTAKE_TOOLS if t["function"]["name"] == "propose_engine_selection")
    assert "reason" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["engine"]
    assert "the ONE way it is ever asked" in fn["description"]
    assert "never proposed again" in fn["description"]


def test_every_nudge_the_application_writes_is_marked_synthetic():
    for note in _NUDGES:
        assert note in turn.SYNTHETIC_NUDGES
        assert not any(w in note.lower() for w in ("snappy", "cfmesh", "gmsh"))
