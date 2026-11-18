# Responsibility: Verify the notes step before a run (NOTE-5): drawing the card reads nothing; Finalize notes reads the
# parts the user's words name - the first answer, the notes box or both (a sentence in both read once) - and a note
# added after re-arms it while the old rows stay; the run starts only on the user's yes, takes the checked parts (picks
# included), and a yes given before Finalize notes reads the notes first and then runs on the same yes, once - on the
# FEA and the CFD route alike.
# Boundaries: no model, no pictures, no worker, no database: the store is a dict, the model a stub, the enqueue a list.
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import message as msg
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import part_matches as PM
from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import geometry_check as GC
from meshpipeline.contracts import model_inference, object_storage

SID = "5e551011-finalize-notes"
TABLE = [{"id": "Part_1", "name": "Bolt", "diag_mm": 45.8}, {"id": "Part_2", "name": "", "diag_mm": 45.8},
         {"id": "Part_3", "name": "", "diag_mm": 533.0}]
SENTENCE = "Use 3-4 mm on the frame rail."


class _Store:
    def __init__(self, objects):
        self.objects = dict(objects)

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        from pathlib import Path
        self.objects[object_key] = Path(local_path).read_bytes()


class _Router:
    def __init__(self):
        self.seen: list[dict] = []
        self.parts = ["Part_3"]

    async def call_reviewer_with_tools(self, messages, tools, job_id="", user_id="", **_):
        self.seen.append(json.loads(messages[1]["content"][0]["text"]))
        phrase = {"parts": self.parts, "everything_else": False, "ask": "size", "min": 3, "max": 4, "unit": "mm",
                  "quote": "3-4 mm on the frame rail", "why": "the long beam", "confidence": 0.9}
        phrases = [phrase] if self.parts is not None else []
        return SimpleNamespace(failure_marker="", tool_calls=[SimpleNamespace(
            name=tools[0]["function"]["name"], arguments=json.dumps({"phrases": phrases, "words": ["read"]}))])


def _world(monkeypatch, route: str = PM.ROUTE_FEA, *, notes: list | None = None, request_txt: str = ""):
    from meshpipeline.cad.fea_measure import FACT_KEY
    seen = {"route": route, "table": TABLE, "pictures": []}
    facts = {FACT_KEY: {PM.PARTS_KEY: seen}} if route == PM.ROUTE_FEA else {PM.PARTS_KEY: seen}
    store = _Store({gc.check_object_key(SID, "scout.json"):
                    json.dumps({"status": "ready", "session_id": SID, "facts": facts}).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    queued: list[dict] = []
    monkeypatch.setattr(GC, "_part_words_enqueuer", lambda **kw: queued.append(kw))
    router = _Router()
    monkeypatch.setattr(model_inference, "_router", router)
    import meshpipeline.engines.fea_ai as FA
    monkeypatch.setattr(FA, "FEA_AI", True)
    session = SimpleNamespace(id=SID, user_notes=list(notes or []), request_txt=request_txt, owner_id="o1",
                              job_id=None)
    return session, queued, router


def _note(text: str, nid: str = "n1") -> dict:
    return {"id": nid, "text": text, "source": "console"}


@pytest.mark.parametrize("route", [PM.ROUTE_FEA, PM.ROUTE_CFD])
@pytest.mark.parametrize("where", ["notes box", "both"])
def test_finalize_reads_the_parts_from_the_users_notes_and_the_run_takes_them(monkeypatch, route, where):
    notes = [_note(SENTENCE)]
    request = f"Static stress on the bracket assembly. {SENTENCE}" if where == "both" else "Static stress."
    session, queued, router = _world(monkeypatch, route, notes=notes, request_txt=request)
    assert PM.view(session) == {"status": "ready"} and not queued          # the card reads nothing by itself
    assert PM.finalize(session) == {"status": "reading"} and len(queued) == 1
    PM.run_reading(**queued.pop())
    got = PM.view(session)
    assert got["status"] == "done" and [r["parts"] for r in got["rows"]] == [["Part_3"]]   # one row, never two
    asked = router.seen[-1]                                                  # the user's notes only, read once
    assert "user_request" not in asked and json.dumps(asked["user_notes"]).count("frame rail") == 1
    assert "Static stress" not in json.dumps(asked)
    run = PM.for_run(SID, session.user_notes, session.request_txt)
    assert run["status"] == "used" and PW.per_part(run["entries"])["Part_3"]["max_m"] == pytest.approx(0.004)


@pytest.mark.parametrize("route", [PM.ROUTE_FEA, PM.ROUTE_CFD])
def test_the_intake_summary_alone_gives_no_per_part_size(monkeypatch, route):
    """NOTE-4: the intake's summary of the request is a model's rewrite, never the user's words: with no note, a size
    it names is not read, nothing is queued, nothing is shown on the card and the run sizes no part."""
    session, queued, router = _world(monkeypatch, route, notes=[],
                                     request_txt=f"Static stress on the bracket assembly. {SENTENCE}")
    assert PM.view(session) is None and PM.finalize(session) is None and not queued
    assert PM.before_run(session) == "go"
    assert PM.for_run(SID, session.user_notes, session.request_txt) is None


def test_a_pick_on_the_model_reaches_the_run(monkeypatch):
    session, queued, _ = _world(monkeypatch, notes=[_note(SENTENCE)], request_txt="Static stress.")
    PM.finalize(session)
    PM.run_reading(**queued.pop())
    PM.pick(session, 0, ["Part_1"])
    run = PM.for_run(SID, session.user_notes, session.request_txt)
    assert set(PW.per_part(run["entries"])) == {"Part_1"}


def test_a_note_after_finalize_re_arms_it_and_the_old_rows_stay_until_read_again(monkeypatch):
    session, queued, _ = _world(monkeypatch, notes=[_note(SENTENCE)], request_txt="Static stress.")
    PM.finalize(session)
    PM.run_reading(**queued.pop())
    session.user_notes.append(_note("Refine the bolts.", "n2"))
    got = PM.view(session)
    assert got["status"] == "ready" and got["stale"] and got["rows"][0]["parts"] == ["Part_3"]
    got = PM.finalize(session)
    assert got["status"] == "reading" and got["stale"] and got["rows"]
    assert PM.for_run(SID, session.user_notes, session.request_txt) is None   # never the older notes' reading
    PM.run_reading(**queued.pop())
    assert PM.view(session)["status"] == "done" and not PM.view(session).get("stale")


def test_words_naming_no_part_show_nothing_after_finalize(monkeypatch):
    session, queued, router = _world(monkeypatch, notes=[_note("Keep it tidy.")], request_txt="Static stress.")
    router.parts = None
    PM.finalize(session)
    PM.run_reading(**queued.pop())
    assert PM.view(session) == {"status": "done", "rows": [], "unsure": []}


def test_the_words_read_are_the_users_notes_only():
    state = {"user_notes": [_note(SENTENCE)], "request_txt": f"Static stress. {SENTENCE}"}
    assert PW.notes_text(state).count("frame rail") == 1 and "Static stress" not in PW.notes_text(state)
    assert PW.notes_text({"request_txt": f"Static stress. {SENTENCE}"}) == ""
    assert not PW.wanted({"request_txt": f"Static stress. {SENTENCE}"})
    # a declaration that reached the notes as a user turn is the application's text, never read
    from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK
    assert PW.notes_text({"user_notes": [_note(f"{CONFIRMED_MARK} a part. {SENTENCE}")]}) == ""


def test_the_card_lists_only_the_users_own_notes():
    """NOTE-5: the card's notes are the user's words alone - never a sentence of the intake's summary listed as a
    note beside the checklist."""
    from meshpipeline.application.intake_brief import build_brief
    session = SimpleNamespace(request_txt=f"Static stress on the assembly. {SENTENCE}", user_notes=[_note("Keep it tidy.")],
                              messages=[], intake_submitted=False, purpose="structural")
    pm = {"status": "done", "rows": [{"quote": "3-4 mm on the frame rail", "parts": ["Part_3"]}], "unsure": []}
    brief = build_brief(session, part_matches=pm)
    assert [n["text"] for n in brief["user_notes"]] == ["Keep it tidy."]
    assert brief["part_matches"] == pm


# # the yes


async def _settle(monkeypatch, content: str, session, approval=None):
    monkeypatch.setattr(msg.uc, "needs_confirmation", lambda locked: False)
    approval = approval or ap.create(owner_id="o1", session_id=SID, selection_id="s", token_id="t", canonical={},
                                     fingerprint="f", payload={"mesh_engine": "gmsh"}, summary="s",
                                     proposal_revision="r", proposal_msg_count=1)
    gate = {"approval": approval}
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=uuid.UUID(int=7), owner_id="o1", content=content, organization_id="o")
    out = await msg._settle(inbound, AsyncMock(), gate=gate, locked=session,
                            messages=[{"role": "user", "content": content}], revision="r1", session_repo=repo,
                            logger=MagicMock())
    return out, repo


@pytest.mark.asyncio
async def test_nothing_but_the_users_yes_starts_the_run(monkeypatch):
    session, queued, _ = _world(monkeypatch, notes=[_note(SENTENCE)], request_txt="Static stress.")
    PM.finalize(session)
    await asyncio.to_thread(lambda: PM.run_reading(**queued.pop()))
    out, _ = await _settle(monkeypatch, gh.CONTINUE_TEXT, session)      # the geometry check's own turn
    assert out.status is not msg.MessageStatus.approve
    out, _ = await _settle(monkeypatch, "Static stress", session)       # the analysis button
    assert out.status is not msg.MessageStatus.approve
    out, _ = await _settle(monkeypatch, "yes", session)                 # finalized and read: the yes runs
    assert out.status is msg.MessageStatus.approve


@pytest.mark.parametrize("route", [PM.ROUTE_FEA, PM.ROUTE_CFD])
@pytest.mark.asyncio
async def test_a_yes_before_finalize_reads_the_notes_first_then_runs_on_the_same_yes(monkeypatch, route):
    session, queued, _ = _world(monkeypatch, route, notes=[_note(SENTENCE)], request_txt="Static stress.")
    out, repo = await _settle(monkeypatch, "yes", session)
    assert out.status is msg.MessageStatus.reading_first and out.answered and out.awaiting_confirmation
    assert len(queued) == 1                                              # the reading started on the yes
    held = repo.set_intake_gate.await_args.args[2]["approval"]
    assert held["status"] == ap.AWAITING and held["notes_read_first"]
    assert held["expected_confirmation_msg_count"] == 3                  # the console's repeat lands on it
    await asyncio.to_thread(lambda: PM.run_reading(**queued.pop()))
    out, _ = await _settle(monkeypatch, "yes", session, approval=held)   # the same yes, once the rows are shown
    assert out.status is msg.MessageStatus.approve
    assert PM.for_run(SID, session.user_notes, session.request_txt)["status"] == "used"


@pytest.mark.asyncio
async def test_a_reading_that_is_slow_never_traps_the_yes(monkeypatch):
    session, queued, _ = _world(monkeypatch, notes=[_note(SENTENCE)], request_txt="Static stress.")
    _out, repo = await _settle(monkeypatch, "yes", session)
    held = repo.set_intake_gate.await_args.args[2]["approval"]
    out, _ = await _settle(monkeypatch, "yes", session, approval=held)   # still reading: the run starts anyway
    assert out.status is msg.MessageStatus.approve


@pytest.mark.asyncio
async def test_no_words_to_read_runs_on_the_yes_as_before(monkeypatch):
    session, queued, _ = _world(monkeypatch, notes=[], request_txt="")
    out, _ = await _settle(monkeypatch, "yes", session)
    assert out.status is msg.MessageStatus.approve and not queued


@pytest.mark.asyncio
async def test_the_card_carries_the_approval_question_while_the_plan_waits(monkeypatch):
    from meshpipeline.api.v1 import chat
    monkeypatch.setattr(PM, "view", lambda s, owner_id="": None)
    monkeypatch.setattr(chat, "build_brief", lambda s, part_matches=None: {"purpose": "structural"})
    live = ap.create(owner_id="a", session_id=SID, selection_id="s", token_id="t", canonical={}, fingerprint="f",
                     payload={}, summary="s", proposal_revision="r", proposal_msg_count=1)
    waiting = SimpleNamespace(id=SID, request_txt="x", job_id=None, intake_gate={"approval": live})
    assert (await chat._brief(waiting, "a"))["proceed_ask"] == ap.PROCEED_ASK
    started = SimpleNamespace(id=SID, request_txt="x", job_id=uuid.uuid4(), intake_gate={"approval": live})
    assert "proceed_ask" not in await chat._brief(started, "a")


# # NOTES-CFD-1: a flow model with no parts to tie the words to still finalizes its notes

FLOW_WORDS = [{"role": "user", "content": "Internal flow of water through the duct, please."}]
FEA_WORDS = [{"role": "user", "content": "Static stress analysis of the bracket."}]
LAYERS = "5 boundary layers on the wall"


def _single(monkeypatch, words: list, *, notes: list | None = None):
    """A one-part model (the check kept no parts) and a reader that reads one requirement from the notes."""
    store = _Store({gc.check_object_key(SID, "scout.json"):
                    json.dumps({"status": "ready", "session_id": SID, "facts": {}}).encode()})
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    queued: list[dict] = []
    monkeypatch.setattr(GC, "_part_words_enqueuer", lambda **kw: queued.append(kw))
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines import meshing_standard as MS
    asked: list = []

    async def decide(state, *, route="", job_id="", **_):
        asked.append((route, [n["text"] for n in state["user_notes"]]))
        return MR.section("used", requirements={**MR.empty(), "rows": [{"asked": LAYERS}]}, key="k", source="model")

    monkeypatch.setattr(MS, "decide", decide)
    session = SimpleNamespace(id=SID, user_notes=list(notes or []), request_txt="", owner_id="o1", job_id=None,
                              messages=list(words))
    return session, queued, asked


def test_a_flow_part_with_no_parts_to_name_still_finalizes_its_notes(monkeypatch):
    session, queued, asked = _single(monkeypatch, FLOW_WORDS, notes=[_note(f"Put {LAYERS}.")])
    assert PM.view(session) == {"status": "ready"} and not queued          # Finalize notes on the card
    assert PM.finalize(session) == {"status": "reading"} and len(queued) == 1
    assert PM.view(session) == {"status": "reading"}                        # "Reading your notes…"
    assert PM.run_reading(**queued.pop())["status"] == "done"
    # read as the run's CFD reader reads them (the flow route, the user's own notes), and shown as its lines
    assert asked == [("flow", [f"Put {LAYERS}."])]
    assert PM.view(session) == {"status": "done", "rows": [], "unsure": [], "lines": [f"From your notes: {LAYERS}."]}
    assert PM.before_run(session) == "go"                                   # read: the yes runs
    # a note added after re-arms it
    session.user_notes.append(_note("Keep it under 2 million cells.", "n2"))
    assert PM.view(session)["status"] == "ready"


def test_an_fea_part_with_no_parts_to_name_is_unchanged(monkeypatch):
    session, queued, asked = _single(monkeypatch, FEA_WORDS, notes=[_note("Use 3 mm elements.")])
    assert PM.view(session) is None and PM.finalize(session) is None and PM.before_run(session) == "go"
    assert not queued and not asked


def test_a_flow_part_with_no_notes_shows_nothing(monkeypatch):
    session, queued, _ = _single(monkeypatch, FLOW_WORDS, notes=[])
    assert PM.view(session) is None and PM.finalize(session) is None and not queued


@pytest.mark.asyncio
async def test_a_yes_before_finalize_on_a_single_flow_part_reads_the_notes_first(monkeypatch):
    session, queued, _ = _single(monkeypatch, FLOW_WORDS, notes=[_note(f"Put {LAYERS}.")])
    out, repo = await _settle(monkeypatch, "yes", session)
    assert out.status is msg.MessageStatus.reading_first and len(queued) == 1
    held = repo.set_intake_gate.await_args.args[2]["approval"]
    await asyncio.to_thread(lambda: PM.run_reading(**queued.pop()))
    out, _ = await _settle(monkeypatch, "yes", session, approval=held)
    assert out.status is msg.MessageStatus.approve


@pytest.mark.asyncio
async def test_the_finalize_route_reads_a_flow_sessions_notes(monkeypatch):
    """POST /api/v1/geometry/{session}/check/parts/finalize on a CFD session of one part: the reading starts and the
    card's answer is "reading"; once the run has started the notes can no longer change (409)."""
    from fastapi import HTTPException

    from meshpipeline.api.v1 import geometry
    session, queued, _ = _single(monkeypatch, FLOW_WORDS, notes=[_note(f"Put {LAYERS}.")])

    async def owned(_sid, _owner, _org):
        return session

    monkeypatch.setattr(geometry, "_owned_session", owned)
    got = await geometry.finalize_check_parts(uuid.UUID(int=7), owner_id="o1", organization_id="org")
    assert got == {"status": "reading"} and len(queued) == 1
    session.job_id = uuid.uuid4()
    with pytest.raises(HTTPException) as err:
        await geometry.finalize_check_parts(uuid.UUID(int=7), owner_id="o1", organization_id="org")
    assert err.value.status_code == 409
