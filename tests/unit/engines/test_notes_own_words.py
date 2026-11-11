# Responsibility: Verify that the notes reader reads ONLY the user's own words (NOTE-7, NOTE-9): what they typed in the notes box and their own chat messages as kept - never the intake's summary of the request, the geometry declaration or any text the application wrote - so a run whose only text is the system's asks no model and says nothing, and a "Not applied" line can only quote the user; and that a shell run's summary never promises a tetrahedral mesh (SHSTAGE-5 / SURF-6).
# Boundaries: pure over plain data, a fake model router and the intake executor with its authorization stubbed. No provider is called, no mesh is made.
from __future__ import annotations

import asyncio
import json

import pytest
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.application import geometry_confirmation as gcf
from meshpipeline.application import shell_check as sc
from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts import user_notes as un
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import mesh_requirements as MR

#: what a surface-file shell run on the console carried besides the user's words: the intake's own
#: summary of the request, and the stage's declaration
SUMMARY = ("Generate a quadratic tetrahedral structural mesh with every face retained as a named group. The three "
           "edges with a gap of 0.037 mm from neighbouring faces are a known geometry issue and are meshed as given.")
DECLARATION = (f"{gcf.CONFIRMED_MARK} {gcf.FEA_LEAD} (panel), input_kind solid-body: the part itself is meshed, with "
               f"no fluid around it. {sc.SURFACE_HEAD} Part size: 400 x 300 x 2 mm.")


class _Router:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls, self.seen = answer, exc, 0, None

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        self.seen = messages
        if self.exc:
            raise self.exc
        return self.answer


def _answer(args: dict) -> ModelRoundResult:
    return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=MR.TOOL,
                                                        arguments=json.dumps(args)),))


# ------------------------------------------------------------------ nothing the user said: nothing asked ----
@pytest.mark.parametrize("state", [
    {"request_txt": SUMMARY},
    {"request_txt": SUMMARY, "review_brief_txt": "The solid meshed whole, every face a named group."},
    {"request_txt": SUMMARY, "messages": [{"role": "assistant", "content": DECLARATION}]},
    # a declaration that reached the notes as a user turn is still the application's text
    {"request_txt": SUMMARY, "user_notes": [{"text": DECLARATION, "source": "chat", "turn": 2}]},
])
def test_a_run_whose_only_text_is_the_systems_asks_no_model_and_says_nothing(state, monkeypatch):
    r = _Router(exc=AssertionError("the model must not be asked"))
    monkeypatch.setattr(llm, "_router", r)
    sec = asyncio.run(MR.decide({**state, "user_id": "u1"}, route="fea", job_id="j1"))
    assert sec["status"] == "none" and r.calls == 0
    assert MR.words(sec) == [] and MR.live_words(sec) == [] and MR.decisions(sec) == []
    assert MR.requirements_of(sec) is None and MR.notes_text(state) == ""


def test_the_summary_never_reaches_the_reader_beside_real_notes(monkeypatch):
    """A real note is read as before; the intake's summary is not shown to the model, and nothing of it can be
    quoted back as a note: a "Not applied" line is the user's own words or nothing."""
    r = _Router(_answer({**criteria_answer(), "other": [
        {"quote": "Generate a quadratic tetrahedral structural mesh with every face retained", "kind": "mesh"},
        {"quote": "The three edges with a gap of 0.037 mm from neighbouring faces are a known geometry issue",
         "kind": "mesh"}]}))
    monkeypatch.setattr(llm, "_router", r)
    state = {"request_txt": SUMMARY, "user_id": "u1",
             "user_notes": [{"text": CRITERIA, "source": "console"}]}
    sec = asyncio.run(MR.decide(state, route="fea", job_id="j1"))
    assert r.calls == 1 and sec["status"] == "used"
    shown = json.loads(r.seen[1]["content"])
    assert set(shown) == {"mesh_for", "user_notes"} and "tetrahedral" not in r.seen[1]["content"]
    assert CRITERIA in shown["user_notes"]
    assert len(MR.requirements_of(sec)["rows"]) == 10
    # the model quoted the summary: not the user's words, so dropped as such - never told as a note not applied
    assert sec["not_applied"] == []
    assert not any(w.startswith("Not applied: \"Generate") or "0.037" in w for w in MR.words(sec))


def test_the_message_shown_is_the_same_with_or_without_the_summary():
    notes = [{"text": "keep elements around 5 mm", "source": "console"}]
    a = MR.messages_for({"user_notes": notes}, route="fea")
    b = MR.messages_for({"user_notes": notes, "request_txt": SUMMARY, "review_brief_txt": "x"}, route="fea")
    assert a == b


def test_a_clause_about_what_to_mesh_as_is_not_told_as_not_applied():
    # the user's own words, but about shell or solid (another reader's): never "Not applied"
    _req, _d, not_applied = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [],
                                                 "other": []}), "Please make an FEA shell mesh of this panel.")
    assert not_applied == []
    _req, _d, not_applied = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [],
                                                 "other": []}), "FEA mesh of this part, quadratic tetrahedral elements")
    assert not_applied == []
    # a setting left out is still told
    _req, _d, not_applied = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [],
                                                 "other": []}), "don't go below 3 elements across any flange")
    assert not_applied == ["don't go below 3 elements across any flange"]


# ------------------------------------------------------------------ at the source: the notes themselves -----
def test_the_application_text_is_never_kept_as_a_note():
    assert set(un.SYSTEM_LEADS) == {gcf.CONFIRMED_MARK, "GEOMETRY CHECK (drawing your part):"}
    # a user turn the application wrote, answering the notes question or quoted by the intake, is not a note
    conversation = [{"role": "user", "content": "FEA shell mesh"},
                    {"role": "assistant", "content": un.QUESTION},
                    {"role": "user", "content": DECLARATION},
                    {"role": "user", "content": "Global Mesh Size = 10 mm"}]
    notes, unmatched = un.capture(conversation, ["Part size: 400 x 300 x 2 mm", "Global Mesh Size = 10 mm"])
    assert [n["text"] for n in notes] == ["Global Mesh Size = 10 mm"]
    assert unmatched == ["Part size: 400 x 300 x 2 mm"]
    assert un.own_notes([{"text": DECLARATION, "source": "chat"}, {"text": "no triangles", "source": "console"}]) == [
        {"id": "n1", "text": "no triangles", "source": "console", "turn": None, "at": ""}]


def test_with_no_system_text_the_notes_are_kept_as_before():
    conversation = [{"role": "user", "content": "FEA shell mesh"},
                    {"role": "assistant", "content": un.QUESTION},
                    {"role": "user", "content": "Global Mesh Size = 10 mm"}]
    notes, unmatched = un.capture(conversation, ["FEA shell mesh"])
    assert [n["text"] for n in notes] == ["FEA shell mesh", "Global Mesh Size = 10 mm"] and unmatched == []


# ------------------------------------------------------------------ a shell run's summary --------------------
def test_a_shell_is_read_from_the_declaration_and_shell_plus_solid_is_not_one():
    def said(head: str) -> list[dict]:
        return [{"role": "assistant", "content": f"{gcf.CONFIRMED_MARK} {gcf.FEA_LEAD}, {head}"}]

    assert sc.declared_shell(said(sc.SURFACE_HEAD)) and sc.declared_shell(said(sc.SHELL_HEAD))
    assert not sc.declared_shell(said(sc.MIXED_HEAD)) and not sc.declared_shell(said("Part size: 1 x 1 x 1 mm."))
    assert not sc.declared_shell([]) and not sc.declared_shell([{"role": "user", "content": sc.SHELL_HEAD}])
    assert sc.TET_WORDS.findall(SUMMARY) == ["quadratic", "tetrahedral"]
    assert sc.TET_WORDS.findall(sc.SHELL_SUMMARY) == []
    # the card parser's lead is unchanged: the console still reads the declaration (ui/js/render/geometry_form.js)
    assert gcf.FEA_LEAD == "a solid part for structural analysis (FEA)"


def _executor(conversation: list[dict], monkeypatch):
    from meshpipeline.agents.intake import executor as ex

    monkeypatch.setattr(ex, "validate_submission", lambda args: [])
    monkeypatch.setattr(ex.at, "verify_for_submit", lambda *a, **k: (True, ""))
    st = ex.IntakeExecutionState(session_id="s", owner_id="u", revision="1", conversation=tuple(conversation),
                                 declared_case=("structural", "solid-body"))
    return ex, ex.IntakeToolExecutor(state=st, job_id="j", implemented_engines=["gmsh"], search_tool=None)


def _submit(executor, request_txt: str):
    args = {"domain": "FEA", "purpose": "structural", "input_kind": "solid-body", "dimensionality": "3D",
            "mesh_engine": "gmsh", "request_txt": request_txt, "review_brief_txt": "every face a named group",
            "preview_token": "t"}
    return asyncio.run(executor._do_submit_requirements(args))


def test_a_shell_runs_summary_that_promises_tetrahedra_is_sent_back_once(monkeypatch):
    conversation = [{"role": "user", "content": "FEA shell mesh"}, {"role": "assistant", "content": DECLARATION}]
    _ex, executor = _executor(conversation, monkeypatch)
    first = _submit(executor, SUMMARY)
    assert first.accepted is False and "meshed as a SHELL" in first.content and "quadratic, tetrahedral" in first.content
    assert executor.state.submit_args is None
    # once only: a second summary is taken whatever it says (the card's own rows say the shell)
    second = _submit(executor, "A shell mesh of the panel: plate elements on the surface, every face named.")
    assert second.content == "" or "meshed as a SHELL" not in second.content


def test_a_solid_runs_summary_is_never_sent_back_for_saying_tetrahedra(monkeypatch):
    solid = [{"role": "assistant", "content": f"{gcf.CONFIRMED_MARK} {gcf.FEA_LEAD}, input_kind solid-body: the part "
                                              "itself is meshed. Part size: 10 x 10 x 10 mm."}]
    _ex, executor = _executor(solid, monkeypatch)
    got = _submit(executor, SUMMARY)
    assert "meshed as a SHELL" not in (got.content or "") and executor.state.shell_words_sent_back is False


@pytest.mark.parametrize("quote,read,want", [
    ("no triangles", "triangles_only", "quads"), ("no triangles", "quads_only", "quads"),
    ("mesh without any trias please", "triangles_only", "quads"), ("no quads, triangles only", "quads_only", "triangles"),
    ("triangles only", "triangles_only", "triangles"), ("quad mesh only", "quads_only", "quads")])
def test_the_users_words_decide_which_element_kind_is_not_wanted(quote, read, want):
    req, dropped, _na = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "limits": [], "other": [],
                                             "element_kind": [{"what": read, "value": 0, "quote": quote}]}), quote)
    assert dropped == [] and req["kind"]["shell"] == want
