# Responsibility: Verify SHSTAGE-1, the words that say how an FEA part is meshed: the fixed word table (shell
# words set Shell, solid words set Solid, with no follow-up question), the shell words that are FEA words too,
# and the model read only for paraphrases - its answer kept only when it is shell, solid or none.
# Boundaries: the model is stubbed; nothing is stored.
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meshpipeline.application import mesh_as
from meshpipeline.application import purpose_map as pm

SHELL_WORDS = [
    "shell", "I need a shell mesh of this bracket for Nastran", "2D elements please", "2D mesh",
    "mid-surface model", "a midsurface mesh", "plate model of the deck", "Femap shell model",
    "CQUAD4 elements", "CQUAD4 and CTRIA3", "a sheet-metal shell", "thin shell analysis", "PSHELL properties",
    "SHELL181 for Ansys", "S4R elements in Abaqus", "static stress, meshed as shells",
]
SOLID_WORDS = [
    "solid", "a solid mesh", "3D tets", "tets", "tetrahedral mesh", "hex mesh", "hexahedral elements",
    "C3D10 elements", "CTETRA in Nastran", "SOLID187", "brick elements", "3D elements for stress",
]
NOTHING = [
    "stress in my bracket", "modal analysis", "will it break under 5 kN", "FEA please", "",
    "this is a 3D model of the frame", "a 2D drawing came with it", "solid mechanics problem",
    "hex bolt preload", "shell-and-tube exchanger stress",
]
#: MORE WAYS TO ASK FOR A SHELL (8 Nov): element names and mesh types an FEA engineer says. Only words that ask
#: for elements or a mesh type count.
MORE_SHELL_WORDS = [
    "2D elements", "plate elements", "plate-elements for the deck", "mesh it as plates", "model the walls with plates",
    "idealise the tank as plates", "CQUAD4", "CTRIA3 only", "PSHELL cards", "S4 elements", "S3 elements",
    "S4R elements", "S3/S4R mix", "SHELL181", "SHELL63 in APDL", "shell 63 elements", "a thin-shell model",
    "midplane mesh", "mid-plane mesh", "mid-surface", "midsurface mesh",
]
#: Words that DESCRIBE a thin part (or say S3 for something else) and ask nothing about the mesh.
DESCRIBES_THE_PART = [
    "thin sheet metal bracket", "this is a thin plate part", "a bracket made of two plates", "welded plates",
    "the base plate is 10 mm thick", "a steel plate with holes", "stress in the plates", "plates bolted together",
    "store it in an S3 bucket", "sheet metal part, static stress", "thin plate under load", "a flat plate",
]


@pytest.mark.parametrize("words", MORE_SHELL_WORDS)
def test_more_ways_to_ask_for_a_shell_set_shell(words):
    r = pm.mesh_as_of(words)
    assert (r.value, r.by) == ("shell", "table"), (words, r)


@pytest.mark.parametrize("words", DESCRIBES_THE_PART)
def test_words_that_describe_a_thin_part_never_set_shell(words):
    assert pm.mesh_as_of(words).value == "", words


@pytest.mark.parametrize("words", ["SHELL181 for Ansys", "SHELL63 static", "plate elements on the deck",
                                   "a mid-plane model", "a thin-shell model"])
def test_the_new_shell_words_take_the_fea_path_too(words):
    assert pm.read_purpose(words).route == pm.ROUTE_FEA, words


@pytest.mark.parametrize("words", SHELL_WORDS)
def test_shell_words_set_shell(words):
    r = pm.mesh_as_of(words)
    assert (r.value, r.by) == ("shell", "table"), (words, r)


@pytest.mark.parametrize("words", SOLID_WORDS)
def test_solid_words_set_solid(words):
    r = pm.mesh_as_of(words)
    assert (r.value, r.by) == ("solid", "table"), (words, r)


@pytest.mark.parametrize("words", NOTHING)
def test_words_that_say_neither_say_nothing(words):
    assert pm.mesh_as_of(words).value == "", words


@pytest.mark.parametrize("words", ["shell or solid, not sure", "no tets please, shells", "not a solid mesh",
                                   "rather than tets, use plates"])
def test_both_kinds_or_a_word_taken_back_go_to_the_paraphrase_reading(words):
    r = pm.mesh_as_of(words)
    assert r.value == "" and r.unsure, (words, r)


@pytest.mark.parametrize("words", ["Femap shell model", "CQUAD4 model for static stress", "a mid-surface mesh",
                                   "plate model of the hull", "shell elements"])
def test_shell_model_words_take_the_fea_path_with_no_question(words):
    reading = pm.read_purpose(words)
    assert reading.route == pm.ROUTE_FEA and not reading.ask, (words, reading)


def test_the_check_of_an_answer_keeps_only_the_allowed_values():
    assert pm.mesh_as_answer("Shell") == "shell" and pm.mesh_as_answer(" solid. ") == "solid"
    for bad in ("none", "beam", "auto", "", None, "shells and solids"):
        assert pm.mesh_as_answer(bad) == ""


def _stub_model(monkeypatch, replies: dict, calls: list):
    from meshpipeline.contracts import model_inference as llm_router

    async def _intake(messages, job_id="", user_id="", name="Intake", tools=None):
        text = messages[-1]["content"]
        calls.append(text)
        for k, v in replies.items():
            if k in text:
                return SimpleNamespace(assistant_text=v)
        return SimpleNamespace(assistant_text='{"mesh_as": "none"}')
    monkeypatch.setattr(llm_router, "call_intake_model", _intake)


PARAPHRASES = [
    ("flat elements on the middle of the walls", '{"mesh_as": "shell"}', "shell"),
    ("mesh the sheet as a membrane-bending surface", '{"mesh_as": "shell"}', "shell"),
    ("elements through the whole volume of the part", '{"mesh_as": "solid"}', "solid"),
    ("no tets please, shells", '{"mesh_as": "shell"}', "shell"),
    ("the thin sheet is fine either way", '{"mesh_as": "none"}', ""),
    # an answer outside the allowed values is nothing said, never a guess
    ("mesh it with beam elements", '{"mesh_as": "beam"}', ""),
    ("quad elements on the plates", "I think shell elements are best here.", ""),
    ("a mesh of the sheet", "solid", "solid"),
]


@pytest.mark.parametrize(("words", "reply", "want"), PARAPHRASES)
def test_the_model_reads_paraphrases_checked_against_the_allowed_values(monkeypatch, words, reply, want):
    calls: list = []
    _stub_model(monkeypatch, {words: reply}, calls)
    r = asyncio.run(mesh_as.read_mesh_as(words, session_id="s", owner_id="o"))
    assert r.value == want, (words, r)
    assert r.by == ("model" if want else "")
    assert len(calls) == 1


def test_the_table_answers_first_and_plain_words_never_ask_the_model(monkeypatch):
    calls: list = []
    _stub_model(monkeypatch, {}, calls)
    assert asyncio.run(mesh_as.read_mesh_as("CQUAD4 model")).value == "shell"
    assert asyncio.run(mesh_as.read_mesh_as("stress in my bracket under 5 kN")).value == ""
    assert calls == []


def test_a_model_that_fails_or_hangs_is_nothing_said(monkeypatch):
    from meshpipeline.contracts import model_inference as llm_router

    async def _broken(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm_router, "call_intake_model", _broken)
    r = asyncio.run(mesh_as.read_mesh_as("flat elements on the middle of the walls"))
    assert r.value == "" and "did not answer" in r.why

    async def _slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(llm_router, "call_intake_model", _slow)
    r = asyncio.run(mesh_as.read_mesh_as("flat elements on the middle of the walls", timeout_s=0.05))
    assert r.value == ""


def test_the_sync_reading_and_the_stored_record(monkeypatch):
    calls: list = []
    _stub_model(monkeypatch, {"middle of the walls": '{"mesh_as": "shell"}'}, calls)
    r = mesh_as.read_mesh_as_sync("flat elements on the middle of the walls")
    assert mesh_as.as_record(r)["value"] == "shell" and mesh_as.as_record(r)["by"] == "model"
