# Responsibility: Hold the intake's side of the FEA path and the purpose map: unclear words get ONE
# two-button question before the part is checked, an FEA part is held in its own words, its one
# required question ("What will you analyze?") comes with buttons once the part is confirmed and
# only when the words did not name it, and a reply that invents a load or a material goes back.
# Boundaries: the message authority with the store and the check stood in for; no model.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import message as msg
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import purpose_map as pm
from meshpipeline.application.fea_wording import invented
from meshpipeline.application.geometry_confirmation import CONFIRMED_MARK, FEA_LEAD

SID = uuid.UUID("aaaa1111-2222-4222-b222-aaaaaaaaaaaa")
FEA_DECLARATION = (f"{CONFIRMED_MARK} {FEA_LEAD}, input_kind solid-body: the part itself is meshed, "
                   "with no fluid around it. One body. Clean check: The geometry is clean.")


async def _settle(monkeypatch, messages, *, applies="queue"):
    monkeypatch.setattr(gh, "hold_applies", lambda sid: applies)
    queued: list = []
    monkeypatch.setattr(gh, "queue_naming", lambda *a: queued.append(a[2]) or True)
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
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=last, organization_id="o")
    out = await msg._settle(inbound, AsyncMock(), gate={}, locked=locked, messages=messages,
                            revision="r1", session_repo=repo, logger=MagicMock())
    return out, queued


@pytest.mark.asyncio
async def test_unclear_thermal_words_get_the_one_question_and_nothing_is_checked_yet(monkeypatch):
    out, queued = await _settle(monkeypatch, [{"role": "user", "content": "thermal"}])
    assert out.status is msg.MessageStatus.purpose_question and out.answered
    assert out.reply == pm.THERMAL_QUESTION and queued == []
    assert [c["label"] for c in pm.choices_for(out.reply)] == [lab for lab, _s in pm.THERMAL_CHOICES]


@pytest.mark.asyncio
async def test_the_button_answer_checks_the_part_on_its_path(monkeypatch):
    said = [{"role": "user", "content": "thermal"},
            {"role": "assistant", "content": pm.THERMAL_QUESTION},
            {"role": "user", "content": pm.THERMAL_CHOICES[1][1]}]
    out, queued = await _settle(monkeypatch, said)
    assert out.status is msg.MessageStatus.geometry_hold and len(queued) == 1
    assert out.reply.startswith(gh.DRAWING_MARK) and pm.HEAT_IN_SOLID_NOTE in out.reply
    assert "sticker" not in out.reply and "opening" not in out.reply


@pytest.mark.asyncio
async def test_a_structural_answer_is_held_in_fea_words_with_a_coming_soon_note_when_due(monkeypatch):
    out, _ = await _settle(monkeypatch, [{"role": "user", "content": "stress testing"}])
    assert out.reply == f"{gh.DRAWING_MARK} {pm.FEA_HOLD}"
    out, _ = await _settle(monkeypatch, [{"role": "user", "content": "crash simulation"}])
    assert "Impact or crash is coming soon" in out.reply and "static stress" in out.reply
    out, _ = await _settle(monkeypatch, [{"role": "user", "content": "stress testing"},
                                         {"role": "assistant", "content": f"{gh.DRAWING_MARK} {pm.FEA_HOLD}"},
                                         {"role": "user", "content": "is it ready?"}], applies="wait")
    assert out.reply == gh.FEA_WAIT_REPLY


@pytest.mark.asyncio
async def test_cooling_words_on_an_ordinary_file_point_to_the_ecxml_flow(monkeypatch):
    out, _ = await _settle(monkeypatch, [{"role": "user", "content": "cooling of a chip with a fan"}])
    assert out.reply == f"{gh.HOLD_REPLY} {pm.ELECTRONICS_NOTE}"


@pytest.mark.asyncio
async def test_after_the_fea_stage_the_analysis_type_is_the_one_question(monkeypatch):
    said = [{"role": "user", "content": "FEA"}, {"role": "assistant", "content": FEA_DECLARATION},
            {"role": "user", "content": gh.CONTINUE_TEXT}]
    out, _ = await _settle(monkeypatch, said, applies=None)
    assert out.status is msg.MessageStatus.purpose_question and out.reply == pm.ANALYSIS_QUESTION
    labels = [c["label"] for c in pm.choices_for(out.reply)]
    assert labels[:3] == ["Static stress", "Vibration (modal)", "Buckling"]
    assert all("coming soon" in lab for lab in labels[3:])


@pytest.mark.asyncio
async def test_the_analysis_is_not_asked_when_the_words_said_it_or_it_was_asked(monkeypatch):
    said = [{"role": "user", "content": "modal analysis"}, {"role": "assistant", "content": FEA_DECLARATION},
            {"role": "user", "content": gh.CONTINUE_TEXT}]
    out, _ = await _settle(monkeypatch, said, applies=None)
    assert out.status is msg.MessageStatus.proceed
    asked = [{"role": "user", "content": "FEA"}, {"role": "assistant", "content": FEA_DECLARATION},
             {"role": "user", "content": gh.CONTINUE_TEXT},
             {"role": "assistant", "content": pm.ANALYSIS_QUESTION}, {"role": "user", "content": "not sure"}]
    out, _ = await _settle(monkeypatch, asked, applies=None)
    assert out.status is msg.MessageStatus.proceed
    # the domain button's words ("strength, stress or vibration") are about the path, not the analysis
    via_button = [{"role": "user", "content": "simulation"},
                  {"role": "assistant", "content": pm.DOMAIN_QUESTION},
                  {"role": "user", "content": pm.DOMAIN_CHOICES[1][1]},
                  {"role": "assistant", "content": FEA_DECLARATION},
                  {"role": "user", "content": gh.CONTINUE_TEXT}]
    assert msg.analysis_question_due(via_button)


def test_an_invented_load_support_or_material_is_caught_and_the_users_own_is_not():
    reply = ("I'd apply a 1 kN downward load on the opposite end face, with the near end fixed, "
             "in steel (E = 210 GPa).")
    assert set(invented(reply)) >= {"a force", "a material", "a load"}
    assert invented("Load it with 1 kN on the end face", ["load it with 1 kN on the end face"]) == []
    assert invented("Every face is named, so you set supports and loads in your own solver.") == []
    assert invented(pm.ANALYSIS_QUESTION) == []
