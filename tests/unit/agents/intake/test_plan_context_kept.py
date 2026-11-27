# Responsibility: Verify NOTE-18 with NOTE-25 on the live path: a statement of context at the plan step ("the part is
#       cast aluminium", the analysis, the loads, the solver) sent through the same entry the API uses
#       (agents/intake/message.accept) is kept as the user's note and answered as kept - never "Start the mesh now?",
#       never a start - whatever the go reader's model says; "ok", "sure", "yep let's do it" behave as before.
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import consent as C
from meshpipeline.agents.intake import message as M
from meshpipeline.agents.intake import plan_go as PG
from meshpipeline.agents.intake import plan_talk as PT

SID = uuid.UUID("cccc1111-2222-4333-8444-555566667777")
PLAN = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."
#: as on the live site: FEA solid, static stress, the plan step after three requirement messages
TRANSCRIPT = [
    {"role": "user", "content": "static stress analysis of this bracket"},
    {"role": "assistant", "content": PLAN},
    {"role": "user", "content": "use 3 mm overall"},
    {"role": "assistant", "content": "I've set 3 mm elements across the whole part."},
    {"role": "user", "content": "finer on the bore"},
    {"role": "assistant", "content": "I've made the mesh finer at the bore (12 mm across)."},
    {"role": "user", "content": "keep it under 1.5 million elements"},
    {"role": "assistant", "content": "Will do, I'll keep the mesh under 1.5 million elements."},
]
STATEMENTS = ["the part is cast aluminium", "it's for a stress analysis of the bracket",
              "loads are about 5 kN on the bore", "we'll run this in Abaqus"]


class _Repo:
    def __init__(self, messages, gate):
        self.session = SimpleNamespace(id=SID, owner_id="alice", job_id=None, messages=list(messages),
                                       intake_gate=dict(gate), user_notes=[])
        self.said: list[str] = []
        self.notes: list = []
        self.get_for_owner = AsyncMock(return_value=self.session)

    async def get_for_update(self, _db, _sid):
        return self.session

    async def append_message(self, _db, _sid, role, content, extra=None):
        self.session.messages.append({"role": role, "content": content, **(extra or {})})
        if role == "assistant":
            self.said.append(content)

    async def set_intake_gate(self, _db, _sid, gate):
        self.session.intake_gate = gate

    async def set_user_notes(self, _db, _sid, notes):
        self.notes = list(notes)


@asynccontextmanager
async def _db():
    yield SimpleNamespace(commit=AsyncMock())


def _accept(monkeypatch, text, model_says, *, last=None):
    """The API's own entry (message.accept), the plan's approval live, the go reader's model standing in."""
    async def model(_messages, *, job_id, user_id):
        if model_says is None:
            raise TimeoutError
        return model_says
    monkeypatch.setattr(PG, "_ask", model)
    monkeypatch.setattr(ap, "is_live", lambda a: bool(a))
    monkeypatch.setattr(M.uc, "needs_confirmation", lambda _locked: False)
    msgs = TRANSCRIPT if last is None else [*TRANSCRIPT[:-1], {"role": "assistant", "content": last}]
    repo = _Repo(msgs, {"approval": {"status": "awaiting", "expires_at": 4e9, "payload": {"mesh_engine": "gmsh"}}})
    inbound = M.InboundMessage(session_id=SID, owner_id="alice", content=text)
    out = asyncio.run(M.accept(inbound, session_repo=repo, db_factory=_db, logger=logging.getLogger("t")))
    return out, repo


@pytest.mark.parametrize("model_says", ["other", "unsure", "", None, "go"])
@pytest.mark.parametrize("text", STATEMENTS)
def test_a_statement_of_context_is_kept_never_asked_to_start(monkeypatch, text, model_says):
    out, repo = _accept(monkeypatch, text, model_says)
    assert out.status is M.MessageStatus.requirement, (text, model_says, out.status)
    assert PT.ASK_LINE not in repo.said and out.reply != PT.ASK_LINE
    assert [n["text"] for n in repo.notes] == [text]          # kept as the user's note of the turn


@pytest.mark.parametrize("model_says", ["other", "unsure", None])
@pytest.mark.parametrize("text", ["it's a bracket for a mountain bike", "this goes on the rear axle"])
def test_any_other_plain_statement_the_model_is_unsure_of_is_kept(monkeypatch, text, model_says):
    out, repo = _accept(monkeypatch, text, model_says)
    assert out.status is M.MessageStatus.requirement and PT.ASK_LINE not in repo.said


@pytest.mark.parametrize("text,model_says,want", [
    ("ok", "go", M.MessageStatus.approve), ("sure", "go", M.MessageStatus.approve),
    ("yep let's do it", "go", M.MessageStatus.approve), ("yep let's do it", None, M.MessageStatus.approve),
    ("ok", "other", M.MessageStatus.approval_deferred), ("sure", "other", M.MessageStatus.approval_deferred),
    ("maybe", "other", M.MessageStatus.approval_deferred),
])
def test_a_yes_and_a_hedge_behave_as_before(monkeypatch, text, model_says, want):
    out, repo = _accept(monkeypatch, text, model_says)
    assert out.status is want, (text, model_says, out.status)
    if want is M.MessageStatus.approval_deferred:
        assert repo.said[-1] == PT.ASK_LINE


def test_an_analysis_other_than_the_plans_is_still_a_change_to_the_plan():
    assert PT.context_statement("it's for a stress analysis of the bracket", analysis="static")
    assert not PT.context_statement("it's for a modal analysis of the bracket", analysis="static")
    assert not PT.context_statement("it's for a stress analysis of the bracket", analysis="")
    assert not PT.context_statement("use shell elements, it's aluminium", analysis="static")
    assert not PT.context_statement("is it ok for aluminium?", analysis="static")
    assert not PT.context_statement("use cfMesh, it's steel", analysis="static")


@pytest.mark.parametrize("text", ["ok", "sure", "yep let's do it", "maybe", "ready to mesh?", "run it"])
def test_go_ish_words(text):
    assert PT.goish(text, consent=C.reading(text))


@pytest.mark.parametrize("text", STATEMENTS[:1] + ["it's a bracket for a mountain bike"])
def test_statements_are_not_go_ish(text):
    assert not PT.goish(text, consent=C.reading(text))
