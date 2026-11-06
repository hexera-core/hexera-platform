# Responsibility: Verify NOTE-4 (cad/part_words): the vision model's tying of note phrases to parts is checked by the rules - the quote must be the user's own words, a size's numbers the user's own, the ids real, the sizes sane for each part, the model sure enough - and whatever fails applies nothing, said in plain words, never a failed run; the AI down or off applies nothing and says so.
# Boundaries: no model, no pictures: answers are written here, the model is a stub, the drawing is skipped.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import user_notes as UN

TABLE = [{"id": "Part_1", "name": "", "diag_mm": 45.8}, {"id": "Part_2", "name": "", "diag_mm": 45.8},
         {"id": "Part_3", "name": "", "diag_mm": 533.0}, {"id": "Part_4", "name": "", "diag_mm": 68.4}]
NOTES = ("An element size of around 3 - 4 mm for the frame rail, and 1.5-2 mm for the bolts. Refine near the "
         "bracket. Everything else at 0.5 cm.")


def _phrase(**kw) -> dict:
    out = {"parts": ["Part_3"], "everything_else": False, "ask": "size", "min": 3, "max": 4, "unit": "mm",
           "quote": "An element size of around 3 - 4 mm for the frame rail", "why": "the long hollow beam",
           "confidence": 0.9}
    out.update(kw)
    return out


def _check(*phrases, notes: str = NOTES):
    return PW.check(json.dumps({"phrases": list(phrases), "words": ["read"]}), TABLE, notes)


def test_a_good_answer_gives_each_part_the_users_own_sizes_in_millimetres():
    entries, dropped, words = _check(
        _phrase(), _phrase(parts=["Part_1", "Part_2"], min=1.5, max=2, quote="1.5-2 mm for the bolts"),
        _phrase(parts=["Part_4"], ask="refine", min=0, max=0, quote="Refine near the bracket"))
    assert not dropped and words == ["read"]
    sizes = PW.per_part(entries)
    assert sizes["Part_3"]["min_m"] == pytest.approx(0.003) and sizes["Part_3"]["max_m"] == pytest.approx(0.004)
    assert sizes["Part_1"]["min_m"] == pytest.approx(0.0015) and set(sizes) == {"Part_1", "Part_2", "Part_3"}
    refine = [e for e in entries if e["ask"] == "refine"]
    assert refine and refine[0]["parts"] == ["Part_4"] and "min_mm" not in refine[0]


def test_everything_else_takes_every_part_no_other_phrase_sized_and_units_are_converted():
    entries, dropped, _w = _check(_phrase(), _phrase(parts=[], everything_else=True, min=0.5, max=0.5, unit="cm",
                                                     quote="Everything else at 0.5 cm"))
    sizes = PW.per_part(entries)
    assert sizes["Part_1"]["min_m"] == pytest.approx(0.005) and sizes["Part_1"]["rest"]
    assert sizes["Part_3"]["min_m"] == pytest.approx(0.003)          # sized by its own phrase, kept


@pytest.mark.parametrize(("phrase", "said"), [
    # the quote must be the user's own words
    (_phrase(quote="3-4 mm for the beam"), "not found word for word"),
    # the numbers must be the user's own (here converted by the model)
    (_phrase(min=0.3, max=0.4, unit="cm"), "not the numbers you wrote"),
    (_phrase(min=4, max=3), "the smaller size is over the larger"),
    (_phrase(min=0, max=4), "not positive numbers"),
    (_phrase(min=-3, max=4), "not positive numbers"),
    # unsure: nothing applied, never a guess
    (_phrase(confidence=0.3, why="cannot tell which part is the frame rail"), "not clear which parts"),
    # an id that is not a part
    (_phrase(parts=["Part_99"]), "is not a part of this model"),
    (_phrase(parts=[]), "named no part"),
])
def test_a_wrong_or_unsure_phrase_applies_nothing_and_says_why(phrase, said):
    entries, dropped, _w = _check(phrase)
    assert PW.per_part(entries) == {}
    assert len(dropped) == 1 and said in dropped[0]


@pytest.mark.parametrize(("notes", "lo", "hi", "unit", "said"), [
    ("0.001 mm elements on the bolts", 0.001, 0.001, "mm", "far finer than any mesh"),
    ("10 m elements on the bolts", 10, 10, "m", "coarser than Part_1 itself"),
])
def test_absurd_sizes_are_not_applied(notes, lo, hi, unit, said):
    entries, dropped, _w = _check(_phrase(parts=["Part_1"], min=lo, max=hi, unit=unit, quote=notes), notes=notes)
    assert PW.per_part(entries) == {} and said in " ".join(dropped)


def test_a_part_sized_twice_keeps_the_first_and_a_malformed_answer_applies_nothing():
    entries, dropped, _w = _check(_phrase(), _phrase(min=1.5, max=2, quote="1.5-2 mm for the bolts"))
    assert PW.per_part(entries)["Part_3"]["min_m"] == pytest.approx(0.003) and "already sized" in dropped[0]
    for bad in ("not json", json.dumps([1, 2]), json.dumps({"phrases": [1]}), json.dumps({"phrases": "x"})):
        entries, dropped, _w = PW.check(bad, TABLE, NOTES)
        assert PW.per_part(entries) == {}


# ------------------------------------------------------------------ asking: never a failed run ----------
def _surface():
    xyz = 100.0 * np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    tris = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    return SimpleNamespace(xyz=xyz, tris=tris, label=np.zeros(4, dtype=np.int64), names=["Face_1"])


def _state(text: str = NOTES) -> dict:
    return {"user_notes": UN.clean([UN.make(text, source=UN.SOURCE_CONSOLE)])}


def test_with_no_words_nothing_is_asked(tmp_path):
    out = asyncio.run(PW.decide(tmp_path, {}, _surface(), {}, consumer="fea_part_sizes"))
    assert out["status"] == "none" and PW.words(out) == []


def test_with_the_model_down_nothing_is_applied_and_that_is_said(tmp_path, monkeypatch):
    from meshpipeline.contracts import execution_guard, model_inference

    async def boom(*_a, **_k):
        raise RuntimeError("the provider is down")

    async def owner(_why):
        return None

    monkeypatch.setattr(PW, "draw", lambda *_a, **_k: [])
    monkeypatch.setattr(model_inference, "call_reviewer_with_tools", boom)
    monkeypatch.setattr(execution_guard, "assert_still_owner", owner)
    out = asyncio.run(PW.decide(tmp_path, {}, _surface(), _state(), consumer="fea_part_sizes"))
    assert out["status"] == "failed" and PW.per_part(out["entries"]) == {}
    assert PW.words(out) == [PW.UNREAD_WORDS]
    assert out["parts"] and out["parts"][0]["id"] == "Part_1"        # the table is kept for the record


def test_the_models_answer_is_checked_before_it_is_used(tmp_path, monkeypatch):
    from meshpipeline.contracts import execution_guard, model_inference

    answer = {"phrases": [_phrase(parts=["Part_1"], quote="An element size of around 3 - 4 mm for the frame rail"),
                          _phrase(parts=["Part_1"], quote="words the user never wrote")], "words": ["ok"]}

    async def ask(messages, tools, **_k):
        assert tools[0]["function"]["name"] == PW.TOOL
        assert any(c.get("type") == "text" for c in messages[1]["content"])
        return SimpleNamespace(tool_calls=[SimpleNamespace(name=PW.TOOL, arguments=json.dumps(answer))],
                               failure_marker="")

    async def owner(_why):
        return None

    monkeypatch.setattr(PW, "draw", lambda *_a, **_k: [])
    monkeypatch.setattr(model_inference, "call_reviewer_with_tools", ask)
    monkeypatch.setattr(execution_guard, "assert_still_owner", owner)
    out = asyncio.run(PW.decide(tmp_path, {}, _surface(), _state(), consumer="fea_part_sizes"))
    assert out["status"] == "used" and set(PW.per_part(out["entries"])) == {"Part_1"}
    assert len(out["dropped"]) == 1 and any(w.startswith("Not applied:") for w in PW.words(out))
