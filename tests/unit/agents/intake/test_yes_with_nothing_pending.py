# Responsibility: Verify a bare yes ("Proceed", "yes", "go", "ok") with no plan awaiting it never reaches the model: a run that did not start is said so and its plan offered again, any other last plan is offered again as it stands, no plan is said plainly, and the questions before a plan keep their own yes; and that a run refused at dispatch is told at that moment with the plan offered again, never a 500.
# Boundaries: pure over plain data and fake repositories; no database, no model.
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import message as msg


def _approval(status=ap.DISPATCHED, **extra):
    now = time.time()
    return {"id": "a1", "status": status, "payload": {"mesh_engine": "snappy"}, "summary": ap.PROCEED_ASK,
            "expires_at": now + 600, "created_at": now, "expected_confirmation_msg_count": 4,
            **({"job_id": str(uuid.uuid4()), "dispatched_at": now} if status == ap.DISPATCHED else {}), **extra}


MESSAGES = [{"role": "user", "content": "internal CFD"}, {"role": "assistant", "content": ap.PROCEED_ASK},
            {"role": "user", "content": "Proceed"}, {"role": "user", "content": "Proceed"}]


def test_a_run_that_did_not_start_is_said_so_and_its_plan_offered_again():
    gate = {"approval": _approval()}
    reply, again = msg.yes_with_nothing_pending("Proceed", gate, SimpleNamespace(status="failed", started=False),
                                                MESSAGES)
    assert reply == "That run did not start: the plan could not be handed to the mesher. Press Proceed to try again."
    a = again["approval"]
    assert ap.is_live(a) and a["id"] != "a1" and "job_id" not in a and "dispatched_at" not in a
    assert a["expected_confirmation_msg_count"] == 3 + 1 and a["payload"] == gate["approval"]["payload"]


@pytest.mark.parametrize("word", ["Proceed", "yes", "go", "ok", "OK please"])
def test_any_bare_yes_after_a_run_offers_the_plan_as_it_stands(word):
    reply, again = msg.yes_with_nothing_pending(word, {"approval": _approval()},
                                                SimpleNamespace(status="succeeded", started=True), MESSAGES)
    assert reply == ap.PROCEED_ASK and ap.is_live(again["approval"])
    # an expired plan too
    old = _approval(status=ap.AWAITING, expires_at=time.time() - 1)
    reply, again = msg.yes_with_nothing_pending(word, {"approval": old}, None, MESSAGES)
    assert reply == ap.PROCEED_ASK and ap.is_live(again["approval"])


def test_what_is_not_a_bare_yes_with_nothing_pending_goes_on_as_before():
    live = {"approval": _approval(status=ap.AWAITING)}
    assert msg.yes_with_nothing_pending("Proceed", live, None, MESSAGES) is None          # the authority's approve
    assert msg.yes_with_nothing_pending("yes but use 2 mm cells", {"approval": _approval()}, None, MESSAGES) is None
    withdrawn = {"approval": _approval(status=ap.INVALIDATED)}                            # a correction's turn
    assert msg.yes_with_nothing_pending("ok", withdrawn, None, MESSAGES) is None
    # before any plan: the conditions' "OK" and the unit's "ok" are the questionnaire's
    asked = [{"role": "assistant", "content": "What flows through it, and how fast? ... - OK, or tell me what to change."}]
    assert msg.yes_with_nothing_pending("OK", {}, None, asked) is None


def test_a_yes_to_a_plan_line_with_no_plan_behind_it_is_said_plainly():
    reply, again = msg.yes_with_nothing_pending("Proceed", {}, None, [{"role": "assistant", "content": ap.PROCEED_ASK}])
    assert reply == "There is no plan to start yet. Tell me what you are meshing, and I'll show the plan." and again is None


class _Repo:
    def __init__(self, session):
        self.session, self.gate, self.said, self.released = session, None, [], False

    async def get_for_update(self, db, sid):
        return self.session

    async def set_intake_gate(self, db, sid, gate):
        self.gate = gate

    async def append_message(self, db, sid, role, text):
        self.said.append((role, text))

    async def release_job(self, db, sid):
        self.released = True


@asynccontextmanager
async def _db():
    class _D:
        async def commit(self):
            return None
    yield _D()


async def test_a_run_refused_at_dispatch_is_told_then_with_the_plan_offered_again():
    session = SimpleNamespace(owner_id="alice", job_id=uuid.uuid4(), intake_gate={"approval": _approval()},
                              messages=MESSAGES)
    repo = _Repo(session)
    reply = await msg.offer_again_after_failed_start(uuid.uuid4(), "alice", session_repo=repo, db_factory=_db)
    assert reply.startswith("That run did not start:") and repo.said == [("assistant", reply)]
    assert ap.is_live(repo.gate["approval"]) and repo.released is True


async def test_the_route_answers_a_run_that_did_not_start_in_words_not_a_500(monkeypatch):
    from meshpipeline.api.v1 import chat

    async def _confirm(*a, **k):
        raise ap.ApprovalTransactionError(ap.ConfirmOutcome(ap.ConfirmStatus.dispatch_failed, "x",
                                                            job_id=uuid.uuid4()))

    async def _offer(*a, **k):
        return msg.DID_NOT_START_REPLY.format(why=msg.DID_NOT_START_WHY)
    monkeypatch.setattr(ap, "confirm_pending_approval", _confirm)
    monkeypatch.setattr(msg, "offer_again_after_failed_start", _offer)
    resp = await chat._confirm_pending_approval(SimpleNamespace(), session_repo=None, owner_id="alice",
                                                session_id=uuid.uuid4())
    assert resp.awaiting_confirmation is True and resp.reply.startswith("That run did not start:")
    assert resp.done is not True
