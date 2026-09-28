# Responsibility: Verify a turn that ends with nothing to say stores one honest sentence, never a blank bubble.
# Boundaries: the sentence and the persistence guard; the loop itself is covered by the canonical loop tests.
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import engine_selection as es
from meshpipeline.agents.intake import message as msg
from meshpipeline.agents.intake.executor import IntakeExecutionState
from meshpipeline.agents.intake.turn import defers_to_default, unsettled_reply
from meshpipeline.contracts.agent_loop import LoopExit


def _state(**over):
    st = IntakeExecutionState(session_id="s1", owner_id="u1", revision="r1", user_msg_count=1)
    for k, v in over.items():
        setattr(st, k, v)
    return st


# the sentence names the one thing the user can say next

# The gate records exactly as the intake writes them: a selection records its confirmation in
# `state` (engine_selection.CONFIRMED), never in a `confirmed` field of its own.

def _proposed(engine="snappy"):
    return es.propose(engine, session_id="s1", owner_id="u1", revision="r1", user_msg_count=1)


def _confirmed(engine="snappy"):
    return es.select_from_structured_input(engine, session_id="s1", owner_id="u1", revision="r1")


def _live_approval():
    return {"id": "a1", "status": ap.AWAITING, "expires_at": time.time() + 600}


def test_the_sentence_asks_about_a_proposed_engine_by_name():
    text = unsettled_reply(_state(selection=_proposed("snappy")), LoopExit.rounds_exhausted)
    assert text.startswith("I could not settle this in one go")
    assert "say yes to use snappy" in text and text.endswith("and I will continue.")


def test_a_confirmed_engine_is_never_asked_for_again():
    text = unsettled_reply(_state(selection=_confirmed("snappy")), LoopExit.rounds_exhausted)
    assert "engine" not in text, "a confirmed engine choice was asked for again"
    assert "use defaults" in text


def test_the_suggested_phrase_is_one_the_intake_reads_as_handing_the_question_back():
    assert defers_to_default("use defaults")


def test_the_sentence_asks_for_the_go_ahead_when_a_summary_awaits():
    text = unsettled_reply(_state(selection=_confirmed(), approval=_live_approval()),
                           LoopExit.no_progress)
    assert "go ahead" in text and "what to change" in text
    assert "engine" not in text
    assert ap.classify("go ahead") == ap.APPROVE_INTENT, "the suggested words would not approve"


def test_an_expired_summary_is_not_a_question_anyone_is_waiting_on():
    stale = {"id": "a1", "status": ap.AWAITING, "expires_at": time.time() - 1}
    text = unsettled_reply(_state(selection=_confirmed(), approval=stale), LoopExit.no_progress)
    assert "go ahead" not in text


def test_an_expired_proposal_is_read_as_no_selection():
    old = {**_proposed(), "expires_at": time.time() - 1}
    assert "what you need meshed" in unsettled_reply(_state(selection=old), None)


def test_the_sentence_asks_for_the_request_when_nothing_is_on_the_table():
    text = unsettled_reply(_state(), LoopExit.deadline_exhausted)
    assert "what you need meshed" in text


def test_every_exit_gets_a_non_empty_sentence():
    for exit_ in LoopExit:
        assert unsettled_reply(_state(), exit_).strip()
    assert unsettled_reply(_state(), None).strip()


def test_the_agent_falls_back_to_the_sentence_only_when_the_turn_is_silent():
    # The wiring in agent.py: `if not assistant_text.strip(): assistant_text = unsettled_reply(...)`.
    # Asserted on the source because the turn runs a whole provider loop; the sentence itself is
    # tested above and the persistence guard below.
    from pathlib import Path
    src = (Path(__file__).parents[4] / "src/meshpipeline/agents/intake/agent.py").read_text(
        encoding="utf-8")
    assert "if not assistant_text.strip():" in src
    assert "turn.unsettled_reply(_exec_state, _loop_result.exit)" in src


# the durable guarantee: a blank reply is never stored

@asynccontextmanager
async def _db():
    yield AsyncMock()          # the turn commits on the session it is handed


def _repo():
    repo = MagicMock()
    for _k, setter in msg._REQUIREMENT_WRITES:
        setattr(repo, setter, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()
    return repo


async def test_a_blank_reply_is_never_persisted():
    repo = _repo()
    inbound = msg.InboundMessage(session_id="s1", owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": ""}]},
                           session_repo=repo, db_factory=_db)
    await msg.persist_turn(inbound, {"messages": []}, session_repo=repo, db_factory=_db)
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "   "}]},
                           session_repo=repo, db_factory=_db)
    repo.append_message.assert_not_awaited()


async def test_the_unsettled_sentence_is_persisted_once_as_the_reply():
    repo = _repo()
    inbound = msg.InboundMessage(session_id="s1", owner_id="alice", content="hi")
    sentence = unsettled_reply(_state(), LoopExit.rounds_exhausted)
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": sentence}]},
                           session_repo=repo, db_factory=_db)
    repo.append_message.assert_awaited_once()
    assert repo.append_message.await_args.args[-1] == sentence
