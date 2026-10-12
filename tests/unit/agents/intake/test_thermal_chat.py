# Responsibility: Verify a whole thermal model's chat is an electronics-cooling engineer's: the holding
# line is the model's own headline, the declaration says it was confirmed on the thermal model panel
# and reads back as conjugate heat transfer, the brief is a job sheet, the chat after "Mesh this
# model" asks nothing on its way to the approval, and no thermal text - holding line, declaration,
# brief or reply - says inlet, outlet, sticker, sure, triangle, far field or "flows through".
# Boundaries: the real converter, placement and thermal view on a test model (tests/unit/cad/
# ecxml_models); the model's rounds scripted (as test_engine_never_asked_twice does); the store and
# the repositories stood in for.
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.agents.intake.message as msg
from meshpipeline.agents.intake import turn
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application.geometry_confirmation import (
    CONFIRMED_MARK,
    THERMAL_LEAD,
    confirmation_message,
    declared_case,
    patches_from,
    thermal_declared,
)
from meshpipeline.application.thermal_wording import file_names, forbidden_words, job_sheet

SID = uuid.UUID("abab1111-2222-4222-b222-abababababab")


@pytest.fixture(scope="module")
def view(tmp_path_factory):
    """The set-top box's thermal view, as the check serves it."""
    from tests.unit.cad.ecxml_models import set_top_box

    from meshpipeline.application import thermal_view as tv
    from meshpipeline.cad.ingest import canonicalise
    from meshpipeline.cad.ingest.ecxml import read_ecxml
    from meshpipeline.cad.ingest.ecxml_build import read_ecxml_sidecar
    from meshpipeline.cad.ingest.ecxml_place import place

    work = tmp_path_factory.mktemp("thermal_chat")
    src = work / "set_top_box.ecxml"
    src.write_bytes(set_top_box().xml())
    canonical = canonicalise(src, work / "out", stem="geometry", ecxml_form="placed")
    placement = place(read_ecxml(src))
    return tv.thermal_view(read_ecxml_sidecar(canonical.path), placement, detail=tv.detail_levels(placement))


def _confirmed_body(view, detail="standard"):
    from meshpipeline.api.v1.geometry import ConfirmIn

    regions = [r["name"] for r in view["tree"]] + ["air"]
    return ConfirmIn(input_kind="solid-assembly", flow="internal", part=view["title"], openings=[],
                     size_mm=view["domain"]["size_mm"], unit="mm", unit_fixed=True,
                     unit_fixed_reason=("an ECXML model states every length in metres by definition (JEDEC "
                                        "JEP181A), and it was converted exactly into millimetres"),
                     regions=regions, check_mode="thermal", detail=detail)


# ------------------------------------------------------------------ the vocabulary itself ----
@pytest.mark.parametrize("text,words", [
    ("Six outlet rows, 60% sure", ["outlet", "sure"]),
    ("the fluid flows through it", ["flows through"]),
    ("measured from triangles; sticker 3 is the inlet", ["inlet", "sticker", "triangle"]),
    ("a far-field box", ["far field"]),
    ("the pressure is ensured; 8.7 W in total and 35 °C room air", []),
])
def test_the_thermal_vocabulary_check_finds_the_generic_words(text, words):
    assert forbidden_words(text) == words


def test_the_files_own_names_are_its_words_not_ours():
    assert forbidden_words("Inlet_grille: 50% open") == ["inlet"]
    assert forbidden_words("Inlet_grille: 50% open", ["Inlet_grille"]) == []
    assert forbidden_words("Inlet_grille and the inlet", ["Inlet_grille"]) == ["inlet"]


# ------------------------------------------------------------------- the holding line ----
def test_the_holding_line_is_the_models_own_headline(view):
    line = gh.hold_reply(True, view["headline"])
    assert line == (f"{gh.DRAWING_MARK} Set-top box, vented: a Flotherm thermal model with 8 parts, 8.7 W in "
                    "total and 35 °C room air. Check the parts beside this chat and press Mesh this model.")
    for said in (line, gh.hold_reply(True, ""), gh.wait_reply(True)):
        assert said.startswith(gh.DRAWING_MARK) and "Mesh this model" in said
        assert forbidden_words(said) == [] and "opening" not in said.replace("opening your", "")
    # the generic flow keeps its own lines
    assert gh.hold_reply(False, view["headline"]) == gh.HOLD_REPLY
    assert gh.wait_reply(False) == gh.WAIT_REPLY


async def _settle(monkeypatch, *, applies, stored=(False, ""), mode=""):
    monkeypatch.setattr(gh, "hold_applies", lambda sid: applies)
    monkeypatch.setattr(gh, "queue_naming", lambda *a: True)
    monkeypatch.setattr(gh, "stored_thermal", lambda sid: stored)

    async def _mode(db, session, owner_id, organization_id):
        return mode
    monkeypatch.setattr(gh, "upload_check_mode", _mode)
    repo = MagicMock(); repo.append_message = AsyncMock()
    locked = SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=None,
                             geometry_interpretation_id=None, intake_gate={}, messages=[])
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="mesh it", organization_id="o")
    out = await msg._settle(inbound, AsyncMock(), gate={}, locked=locked,
                            messages=[{"role": "user", "content": "mesh it"}], revision="r1",
                            session_repo=repo, logger=MagicMock())
    return out, repo


@pytest.mark.asyncio
async def test_a_thermal_upload_is_held_with_its_headline(monkeypatch, view):
    out, repo = await _settle(monkeypatch, applies="queue", stored=(True, view["headline"]))
    assert out.status is msg.MessageStatus.geometry_hold
    assert out.reply == gh.hold_reply(True, view["headline"])
    assert repo.append_message.await_args.args[2:] == ("assistant", out.reply)


@pytest.mark.asyncio
async def test_before_the_check_has_read_the_file_the_format_says_it_is_a_thermal_model(monkeypatch):
    out, _ = await _settle(monkeypatch, applies="queue", mode="thermal")
    assert out.reply == gh.THERMAL_OPENING_REPLY
    out, _ = await _settle(monkeypatch, applies="wait", mode="thermal")
    assert out.reply == gh.THERMAL_WAIT_REPLY


@pytest.mark.asyncio
async def test_an_ordinary_upload_keeps_the_generic_holding_line(monkeypatch):
    out, _ = await _settle(monkeypatch, applies="queue")
    assert out.reply == gh.HOLD_REPLY


# --------------------------------------------------------------------- the declaration ----
def test_the_declaration_is_in_thermal_words_and_reads_back_as_heat_transfer(view):
    m = confirmation_message(_confirmed_body(view))
    assert m.startswith(f"{CONFIRMED_MARK} {THERMAL_LEAD} “Set-top box, vented”")
    assert "input_kind solid-assembly, for a conjugate heat-transfer solve." in m
    assert "Detail level: Standard" in m and "Air box: 180 x 150 x 80 mm." in m
    assert "fixed by the conversion of the uploaded file" in m          # the unit is never asked
    assert forbidden_words(m, file_names(view)) == [], m
    assert "the fluid" not in m and "picture" not in m and "the part is" not in m
    convo = [{"role": "user", "content": "mesh it"}, {"role": "assistant", "content": m},
             {"role": "user", "content": gh.CONTINUE_TEXT}]
    assert declared_case(convo) == ("conjugate_heat_transfer", "solid-assembly")
    assert thermal_declared(convo) is True
    assert patches_from(_confirmed_body(view)) == []


def test_a_later_restatement_still_unsettles_a_thermal_declaration(view):
    m = confirmation_message(_confirmed_body(view))
    convo = [{"role": "assistant", "content": m},
             {"role": "user", "content": "actually the air flows through it from the left"}]
    assert declared_case(convo) is None


def test_the_generic_declaration_and_its_parser_are_unchanged():
    from meshpipeline.api.v1.geometry import ConfirmIn

    body = ConfirmIn(input_kind="solid-assembly", flow="internal", part="Pump", openings=[],
                     regions=["housing", "air"])
    m = confirmation_message(body)
    assert m.startswith(f"{CONFIRMED_MARK} the file is an assembly of solid parts, each its own region (Pump)")
    assert "input_kind solid-assembly; the fluid flows through it." in m
    convo = [{"role": "assistant", "content": m}]
    assert declared_case(convo) == ("conjugate_heat_transfer", "solid-assembly")
    assert thermal_declared(convo) is False


# ------------------------------------------------------------------------ the job sheet ----
def test_the_job_sheet_reads_like_an_engineers(view):
    assert job_sheet(view, level="standard", engine="snapgrid") == (
        "Mesh “Set-top box, vented” (Flotherm, 8 parts + air, 8.7 W, 35 °C ambient) for a conjugate "
        "heat-transfer solve. Snap-grid hex mesh, Standard: up to 2M cells, at least 2 cells through every "
        "layer. Delivered as an OpenFOAM multi-region case plus the thermal model.")
    assert "Quick look: up to 500k cells, at least 1 cell through" in job_sheet(view, level="draft")
    assert "Fine: up to 6M cells, at least 3 cells through" in job_sheet(view, level="max")
    # an engine the user named instead is said by its name and the level
    other = job_sheet(view, level="standard", engine="snappy_multiregion")
    assert "Snap-grid" not in other and other.endswith("mesh, Standard detail.")


def _session(view, **over):
    m = confirmation_message(_confirmed_body(view, detail="max"))
    base = {"id": SID, "request_txt": "Mesh the set-top box for a conjugate heat-transfer solve " * 3,
            "review_brief_txt": "Every part its own region " * 4, "purpose": "conjugate_heat_transfer",
            "mesh_engine": "snapgrid", "input_kind": "solid-assembly", "dimensionality": "3D",
            "requested_mesh_fidelity": None, "domain": "set-top box thermal mesh", "intake_patches": [],
            "engine_params": {}, "intake_submitted": False,
            "messages": [{"role": "user", "content": "mesh it"}, {"role": "assistant", "content": m}]}
    base.update(over)
    return SimpleNamespace(**base)


def test_the_brief_of_a_thermal_model_is_its_job_sheet(view, monkeypatch):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.intake_brief import build_brief

    monkeypatch.setattr(gc, "read_check", lambda sid: {"check_mode": "thermal", "thermal": view})
    brief = build_brief(_session(view))
    assert brief["check_mode"] == "thermal"
    # the level the user picked on the panel, until the submission records its own
    assert "Snap-grid hex mesh, Fine: up to 6M cells" in brief["job_sheet"]
    assert "Standard" in build_brief(_session(view, requested_mesh_fidelity="standard"))["job_sheet"]
    sides = next(c for c in brief["checks"] if c["id"] == "boundary_roles_valid")
    assert sides["status"] == "pass" and "file" in sides["label"]
    # every word the brief card shows is a thermal model's
    shown = [brief["job_sheet"], *(c["label"] + " " + c["detail"] for c in brief["checks"]),
             *(str(brief.get(k + "_label") or "") for k in ("purpose", "engine", "input_kind", "mesh_fidelity"))]
    assert forbidden_words(" ".join(shown), file_names(view)) == [], shown


def test_an_ordinary_brief_has_no_job_sheet(view, monkeypatch):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.intake_brief import build_brief

    monkeypatch.setattr(gc, "read_check", lambda sid: {"check_mode": "thermal", "thermal": view})
    plain = _session(view, messages=[{"role": "user", "content": "water through this elbow"}])
    brief = build_brief(plain)
    assert "job_sheet" not in brief and "check_mode" not in brief
    sides = next(c for c in brief["checks"] if c["id"] == "boundary_roles_valid")
    assert sides["status"] == "unknown"


# ---------------------------------------------- the chat after "Mesh this model" asks nothing ----
def _tc(name, args):
    return SimpleNamespace(id=f"t-{name}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                         for tc in (tool_calls or [])),
        assistant_text=content, finish_reason="tool_calls" if tool_calls else "stop")


_CASE = {"purpose": "conjugate_heat_transfer", "input_kind": "solid-assembly", "dimensionality": "3D",
         "patches": [], "engine_params": {}}
_PAYLOAD = {**_CASE, "selected_engine": "snapgrid"}
_SUBMIT = {**_CASE, "mesh_engine": "snapgrid", "domain": "set-top box thermal mesh", "engine_source": "suggested_confirmed",
           "mesh_fidelity": "standard",
           "request_txt": ("Mesh the Set-top box, vented (Flotherm ECXML, 8 parts and the air, 8.7 W) for a "
                           "conjugate heat-transfer solve: a snap-grid hex mesh at Standard detail with at least "
                           "2 cells through every layer, delivered as an OpenFOAM multi-region case plus the "
                           "thermal model."),
           "review_brief_txt": ("Every part is present as its own region beside the air; the power stays where "
                                "the file puts it; the thinnest layers have at least 2 cells through them; every "
                                "region checks clean.")}


class _Model:
    """The model's rounds, scripted; every tool answer it was handed is kept."""

    def __init__(self, rounds):
        self.rounds, self.tools, self.user_lines = iter(rounds), [], []

    async def __call__(self, **kw):
        messages = kw.get("messages") or []
        self.tools = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        self.user_lines = [m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        return next(self.rounds)


def _turn(view, model):
    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.application import geometry_check as gc

    m = confirmation_message(_confirmed_body(view))
    messages = [{"role": "user", "content": "mesh this for a thermal sim"},
                {"role": "assistant", "content": gh.hold_reply(True, view["headline"])},
                {"role": "assistant", "content": m},
                {"role": "user", "content": gh.CONTINUE_TEXT}]
    ref = {"source_id": "s1", "owner_id": "u", "object_key": "k", "sha256": "a" * 64, "size_bytes": 10,
           "original_filename": "set_top_box.ecxml", "suffix_hint": ".ecxml"}
    state = {"job_id": "j", "session_id": str(SID), "user_id": "u", "messages": messages,
             "geometry": {"ref": ref}}

    async def _measured(state, msgs):
        return None
    with patch.object(llm_router, "call_intake_model", model), \
            patch.object(intake, "_measured_recommendation", _measured), \
            patch.object(gc, "read_check", lambda sid: {"check_mode": "thermal", "thermal": view}):
        return asyncio.run(intake.node_intake(state))


def test_after_mesh_this_model_the_chat_goes_straight_to_the_approval(view):
    model = _Model([
        _resp([_tc("propose_engine_selection", {"engine": "snapgrid", "reason": "it reads the file"})]),
        _resp([_tc("preview_selected_admission", _PAYLOAD)]),
        None,                                    # filled below, once the token is known
    ])
    rounds = list(model.rounds)

    async def _call(**kw):
        n = _call.n; _call.n += 1
        messages = kw.get("messages") or []
        model.tools = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
        if n < 2:
            return rounds[n]
        token = json.loads(model.tools[-1]["content"])["preview_token"]
        return _resp([_tc("submit_requirements", {**_SUBMIT, "preview_token": token})])
    _call.n = 0
    out = _turn(view, _call)
    sel = out["intake_gate"]["selection"]
    # the engine that reads the file, selected with no question; nothing asked on the way
    assert sel["engine"] == "snapgrid" and sel["state"] == es.CONFIRMED and sel["via"] == es.VIA_THE_FILE
    assert "SELECTED" in model.tools[0]["content"]
    reply = out["messages"][-1]["content"]
    assert reply.startswith("Please confirm the requirements above"), reply
    assert "I'd mesh this with" not in reply and forbidden_words(reply) == []
    assert out["intake_gate"]["approval"]["status"] == "awaiting_confirmation"


def test_a_reply_with_generic_words_is_sent_back_once(view):
    model = _Model([
        _resp(content="Before I mesh it: which side is the outlet? I'm 60% sure it is +x."),
        _resp(content="Standard detail it is - 8.7 W over 8 parts."),
    ])
    out = _turn(view, model)
    assert turn.THERMAL_WORDS_NUDGE in model.user_lines
    assert out["messages"][-1]["content"] == "Standard detail it is - 8.7 W over 8 parts."


def test_a_reply_naming_a_part_the_file_calls_an_inlet_is_not_sent_back(view):
    names = file_names(view)
    model = _Model([_resp(content=f"The {names[1]} is meshed at Standard detail.")])
    out = _turn(view, model)
    assert turn.THERMAL_WORDS_NUDGE not in model.user_lines
    assert out["messages"][-1]["content"].startswith("The ")


def test_the_prompt_tells_the_intake_to_ask_nothing_of_a_thermal_model():
    block = intake._block_thermal_model()
    for said in ("ASK NOTHING", "propose_engine_selection", "preview_selected_admission",
                 "submit_requirements", "patches []", "Never ask about the unit"):
        assert said in block, said
