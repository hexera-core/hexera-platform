# Responsibility: Verify the notes are read the way the user meant them (NOTE-1, NOTE-7, NOTE-8; Areen, 14 Nov): a click on a button we offered is never a note, the LATEST of two values for one thing wins, "3 layers instead of 8" asks for 3 and nothing else, and a yes or a verdict ("the rest looks good") is no requirement left out - on the flow route and on the FEA and shell routes the same reader serves - with the founder's own transcript as the regression case.
# Boundaries: pure over plain data (contracts/user_notes.capture, engines/mesh_requirements.check); the model's answer is written out as the model gave it. No model is called, no mesh is made.
from __future__ import annotations

import json

import pytest

from meshpipeline.agents.intake.executor import _offered_choices
from meshpipeline.contracts import user_notes as un
from meshpipeline.engines import mesh_requirements as MR

#: THE FOUNDER'S RUN (dev, 14 Nov; external aero on a road car, snappyHexMesh), with a generic part name
FIRST = "External aero on this car at 40 m/s, sitting on the ground"
CELL_QUESTION = ("Full detail needs about 21.0 M cells, over the 8.0 M limit. Keep 8.0 M (about 9 cells across the "
                 "narrowest gaps), or try full detail?")
CLICK = "Keep 8.0 M"
REPLY = "3 prism layers instead of 8, the rest looks good"
TYPED = "Produce a mesh for this car at around 3 million cells"
CONVERSATION = [
    {"role": "assistant", "content": "Got your file. What will you use the mesh for?"},
    {"role": "user", "content": FIRST},
    {"role": "assistant", "content": CELL_QUESTION},
    {"role": "user", "content": CLICK},
    {"role": "assistant", "content": "Here is the plan: snappyHexMesh, 8 prism layers on the body. Does that look right?"},
    {"role": "user", "content": REPLY},
]


def _answer(*, with_click: bool) -> dict:
    """The reading as the model gave it on the founder's run: both counts as targets, both layer numbers as counts,
    the speed, and "the rest looks good" listed as about the mesh."""
    counts = [{"what": "target", "value": 3_000_000, "quote": TYPED}]
    if with_click:
        counts.insert(0, {"what": "target", "value": 8_000_000, "quote": CLICK})
    return {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "local": [],
            "counts": counts,
            "layers": [{"what": "count", "value": 3, "unit": "none", "quote": "3 prism layers instead of 8"},
                       {"what": "count", "value": 8, "unit": "none", "quote": "3 prism layers instead of 8"}],
            "physics": [{"what": "velocity", "value": 40, "unit": "m/s", "quote": "at 40 m/s"},
                        {"what": "air", "value": 0, "unit": "none", "quote": FIRST}],
            "other": [{"quote": FIRST, "kind": "not_mesh"}, {"quote": "the rest looks good", "kind": "mesh"}]}


def _section(notes: list[dict], answer: dict, route: str = MR.FLOW) -> dict:
    text = MR.notes_text({"user_notes": notes})
    req, dropped, not_applied = MR.check(json.dumps(answer), text, route=route)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied)


# ------------------------------------------------------------------ NOTE-1: a click is never a note -------
def test_a_click_on_a_button_we_offered_is_never_kept_as_a_note():
    quotes = [FIRST, CLICK, "3 prism layers instead of 8"]
    notes, unmatched = un.capture(CONVERSATION, quotes, offered=_offered_choices)
    assert [n["text"] for n in notes] == [FIRST, REPLY] and unmatched == []
    # without the offered buttons (the old capture) the click was kept as the user's own words
    old, _ = un.capture(CONVERSATION, quotes)
    assert CLICK in [n["text"] for n in old]
    # the same words TYPED by the user, not under that question, are theirs
    typed = [{"role": "assistant", "content": "Anything else?"}, {"role": "user", "content": CLICK}]
    assert [n["text"] for n in un.capture(typed, [CLICK], offered=_offered_choices)[0]] == [CLICK]


def test_a_click_on_any_application_question_is_never_a_note():
    from meshpipeline.application import purpose_map as pm
    question = pm.ANALYSIS_QUESTION
    send = pm.choices_for(question)[0]["send"]
    msgs = [{"role": "assistant", "content": question}, {"role": "user", "content": send}]
    assert un.capture(msgs, [send], offered=_offered_choices)[0] == []
    assert un.clicked(send, question, pm.choices_for) and not un.clicked("static stress on the bracket", question,
                                                                         pm.choices_for)
    assert not un.clicked(send, question, None)


# ------------------------------------------------------------------ the founder's transcript ---------------
def _notes(*, with_click: bool) -> list[dict]:
    chat = [un.make(FIRST, source=un.SOURCE_CHAT, turn=1)]
    if with_click:
        chat.append(un.make(CLICK, source=un.SOURCE_CHAT, turn=2))
    chat.append(un.make(REPLY, source=un.SOURCE_CHAT, turn=3))
    return un.merge(chat, [un.make(TYPED, source=un.SOURCE_CONSOLE)])


def test_the_founders_run_applies_his_3_million_cells_and_3_layers_with_no_noise():
    sec = _section(_notes(with_click=False), _answer(with_click=False))
    req = sec["requirements"]
    assert req["cells"]["target"] == 3_000_000 and req["layers"]["count"] == 3
    assert MR.words(sec) == ["From your notes: about 3 million cells.", "From your notes: 3 boundary layers."]
    assert req["physics"]["velocity_ms"] == 40.0


def test_the_founders_run_as_it_was_kept_still_applies_his_latest_count():
    # a session captured before the fix still carries the click as a note: the count typed AFTER it wins
    sec = _section(_notes(with_click=True), _answer(with_click=True))
    assert sec["requirements"]["cells"]["target"] == 3_000_000
    said = MR.words(sec)
    assert said == ["From your notes: about 3 million cells.", "From your notes: 3 boundary layers."]
    assert not any(w.startswith("Not applied") for w in said)
    # ... before: "Not applied: ... a second cell count target - the first one written is kept", and the 8 M applied


# ------------------------------------------------------------------ NOTE-7: the latest one wins ------------
def _one(notes: str, route: str = MR.FLOW, **fields):
    base = {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return MR.check(json.dumps(base), notes, route=route)


def test_two_counts_the_latest_wins_whatever_order_the_model_lists_them():
    notes = MR.notes_text({"user_notes": un.merge([un.make("about 8 million cells", source="chat", turn=1)],
                                                  [un.make("make it 16 million cells", source="console")])})
    for counts in ([{"what": "target", "value": 8e6, "quote": "about 8 million cells"},
                    {"what": "target", "value": 16e6, "quote": "16 million cells"}],
                   [{"what": "target", "value": 16e6, "quote": "16 million cells"},
                    {"what": "target", "value": 8e6, "quote": "about 8 million cells"}]):
        req, dropped, _na = _one(notes, counts=counts)
        assert req["cells"]["target"] == 16e6 and dropped == []
        assert [r["asked"] for r in req["rows"]] == ["about 16 million cells"]


def test_a_later_ceiling_under_an_earlier_target_wins_quietly():
    notes = MR.notes_text({"user_notes": un.merge([un.make("about 8 million cells", source="chat", turn=1)],
                                                  [un.make("no more than 3 million cells", source="console")])})
    req, dropped, _na = _one(notes, counts=[{"what": "target", "value": 8e6, "quote": "about 8 million cells"},
                                            {"what": "ceiling", "value": 3e6, "quote": "no more than 3 million cells"}])
    assert req["cells"] == {"target": None, "ceiling": 3e6, "floor": None} and dropped == []
    # in ONE note it is a contradiction, and told
    req, dropped, _na = _one("at least 5M cells, at most 2M cells", counts=[
        {"what": "floor", "value": 5e6, "quote": "at least 5M cells"},
        {"what": "ceiling", "value": 2e6, "quote": "at most 2M cells"}])
    assert req["cells"]["floor"] is None and req["cells"]["ceiling"] is None and "neither" in dropped[0]


def test_two_layer_counts_and_two_quality_limits_the_latest_wins():
    req, dropped, _na = _one("5 layers. Actually 10 layers.", layers=[
        {"what": "count", "value": 5, "unit": "none", "quote": "5 layers"},
        {"what": "count", "value": 10, "unit": "none", "quote": "Actually 10 layers"}])
    assert req["layers"]["count"] == 10 and dropped == [] and len(req["rows"]) == 1
    lim = {"measure": "non_orthogonality", "shape": "any", "direction": "max", "allowed_percent": 0, "outer": 0,
           "has_outer": False}
    req, dropped, _na = _one("non-ortho under 70. no - non-ortho under 65", limits=[
        {**lim, "target": 70, "quote": "non-ortho under 70"}, {**lim, "target": 65, "quote": "non-ortho under 65"}])
    assert [x["target"] for x in req["limits"]] == [65.0] and dropped == []
    assert [r["id"] for r in req["rows"]] == [req["limits"][0]["id"]]


# ------------------------------------------------------------------ NOTE-7: the value being replaced -------
@pytest.mark.parametrize("quote,ask,old", [
    ("3 prism layers instead of 8", 3, 8), ("3 layers, not 8", 3, 8), ("use 4 layers rather than 6", 4, 6),
    ("change the layers from 8 to 3", 3, 8), ("2 layers in place of the default 5", 2, 5)])
def test_the_value_being_replaced_is_no_second_request(quote, ask, old):
    req, dropped, _na = _one(quote, layers=[{"what": "count", "value": ask, "unit": "none", "quote": quote},
                                            {"what": "count", "value": old, "unit": "none", "quote": quote}])
    assert req["layers"]["count"] == ask and dropped == []
    # listed the other way round, the same
    req, dropped, _na = _one(quote, layers=[{"what": "count", "value": old, "unit": "none", "quote": quote},
                                            {"what": "count", "value": ask, "unit": "none", "quote": quote}])
    assert req["layers"]["count"] == ask and dropped == []


def test_a_count_and_a_size_being_replaced_too_and_a_reading_of_only_the_old_value_is_told():
    q = "about 3 million cells instead of 8 million"
    req, dropped, _na = _one(q, counts=[{"what": "target", "value": 3e6, "quote": q},
                                        {"what": "target", "value": 8e6, "quote": q}])
    assert req["cells"]["target"] == 3e6 and dropped == []
    q = "base cells 20 mm rather than 50 mm"
    req, dropped, _na = _one(q, sizes=[{"what": "global", "value": 20, "unit": "mm", "quote": q},
                                       {"what": "global", "value": 50, "unit": "mm", "quote": q}])
    assert req["sizes"]["global_mm"] == 20.0 and dropped == []
    # the model read only the replaced number: nothing is applied, and the user's line is not silently lost
    req, dropped, _na = _one(REPLY, layers=[{"what": "count", "value": 8, "unit": "none",
                                             "quote": "3 prism layers instead of 8"}])
    assert req["layers"]["count"] is None and len(dropped) == 1 and "the value your words replace" in dropped[0]


# ------------------------------------------------------------------ NOTE-8: a yes asks nothing -------------
@pytest.mark.parametrize("said", ["the rest looks good", "the rest is right", "yes that's right", "looks good",
                                  "Everything else is fine, thanks!", "ok go ahead", "Perfect - that's all"])
def test_a_yes_or_a_verdict_is_no_requirement_left_out(said):
    assert MR.asks_nothing(said)
    req, dropped, not_applied = _one(said, other=[{"quote": said, "kind": "mesh"}])
    assert MR.is_empty(req) and dropped == [] and not_applied == []


@pytest.mark.parametrize("said", ["keep the wheels sharp", "resolve the wake well", "finer at the mirrors",
                                  "no layers on the floor", "good quality mesh", "the rest at 5 mm",
                                  "looks good but refine the wing", "two cells across the slot"])
def test_a_clause_with_a_quantity_a_mesh_term_or_a_named_thing_is_still_a_requirement(said):
    assert not MR.asks_nothing(said)
    _req, _dropped, not_applied = _one(said, other=[{"quote": said, "kind": "mesh"}])
    assert not_applied == [said]


# ------------------------------------------------------------------ the FEA and shell routes ---------------
def test_the_same_rules_hold_on_the_fea_and_shell_routes():
    for route in ("", "fea"):
        req, dropped, not_applied = _one("about 500k elements. make that 800k elements. the rest is right",
                                         route=route,
                                         counts=[{"what": "target", "value": 5e5, "quote": "about 500k elements"},
                                                 {"what": "target", "value": 8e5, "quote": "make that 800k elements"}],
                                         other=[{"quote": "the rest is right", "kind": "mesh"}])
        assert req["cells"]["target"] == 8e5 and dropped == [] and not_applied == []
        assert req["rows"][0]["asked"] == "about 800,000 elements"
        req, dropped, _na = _one("global size 10 mm instead of 15 mm", route=route, sizes=[
            {"what": "global", "value": 10, "unit": "mm", "quote": "global size 10 mm instead of 15 mm"},
            {"what": "global", "value": 15, "unit": "mm", "quote": "global size 10 mm instead of 15 mm"}])
        assert req["sizes"]["global_mm"] == 10.0 and dropped == []


def test_the_reader_is_told_the_latest_wins_and_what_replacing_means():
    assert "the LATEST one is kept" in MR.SYSTEM and "instead of 8" in MR.SYSTEM and "looks" in MR.SYSTEM
    assert "the first one is kept" not in MR.SYSTEM
