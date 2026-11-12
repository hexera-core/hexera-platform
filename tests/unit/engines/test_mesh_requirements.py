# Responsibility: Verify the reading of the mesh requirements in a user's notes (NOTE-7..9): the model's one answer is checked entry by entry - the quote is the user's own words, every number is in the quote, the unit is the one the user states, directions and ranges hold, contradictions are told - what fits no field is kept as "not applied", no words asks no model, a model that is down replays its saved reading or applies nothing and says so.
# Boundaries: pure over plain data and a fake model router. No provider is called, no mesh is made.
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts import narration
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import mesh_requirements as MR

#: a typical automotive shell criteria list, as a user pastes it into the notes box
CRITERIA = """Global Mesh Size = 10 mm
Min. Element Length (shortest edge) > 0.5 x Global Size
Max. Element Length < 1.5 x Global Size
Warpage < 15° (1% between 15° and 30° is acceptable)
Aspect Ratio < 4.0 (1% between 4.0 and 6.0 is acceptable)
Jacobian > 0.6 (1% between 0.6 and 0.4 is acceptable)
Quad Max. Angle < 135° (1% between 135° and 150° is acceptable)
Quad Min. Angle > 45° (1% between 45° and 30° is acceptable)
Tria Max. Angle < 120° (1% between 120° and 140° is acceptable)
Tria Min. Angle > 30° (1% between 30° and 20° is acceptable)"""
LINES = CRITERIA.split("\n")


def _lim(i, measure, shape, direction, target, outer, text=None):
    return {"measure": measure, "shape": shape, "direction": direction, "target": target, "allowed_percent": 1,
            "outer": outer, "has_outer": True, "quote": text or LINES[i]}


def criteria_answer() -> dict:
    """The criteria list as a model reads it into the tool's fields."""
    return {"stated_unit": "none",
            "sizes": [{"what": "global", "value": 10, "unit": "mm", "quote": LINES[0]},
                      {"what": "min_edge", "value": 0.5, "unit": "x_global", "quote": LINES[1]},
                      {"what": "max_edge", "value": 1.5, "unit": "x_global", "quote": LINES[2]}],
            "element_kind": [],
            "limits": [_lim(3, "warpage", "any", "max", 15, 30), _lim(4, "aspect_ratio", "any", "max", 4.0, 6.0),
                       _lim(5, "jacobian", "any", "min", 0.6, 0.4), _lim(6, "max_angle", "quad", "max", 135, 150),
                       _lim(7, "min_angle", "quad", "min", 45, 30), _lim(8, "max_angle", "tria", "max", 120, 140),
                       _lim(9, "min_angle", "tria", "min", 30, 20)],
            "other": []}


def _check(answer: dict, notes: str):
    return MR.check(json.dumps(answer), notes)


def _one(notes: str, **fields):
    base = {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return _check(base, notes)


# ------------------------------------------------------------------ NOTE-7: the criteria list, checked ----
def test_the_criteria_list_is_read_into_sizes_and_limits():
    req, dropped, not_applied = _check(criteria_answer(), CRITERIA)
    assert dropped == [] and not_applied == []
    assert req["sizes"] == {"global_mm": 10.0, "min_edge_mm": 5.0, "max_edge_mm": 15.0, "growth": None,
                            "thickness_mm": None, "near_wall_mm": None}
    assert req["kind"] == {"shell": None, "tria_percent_max": None, "order": None}
    got = {(lim["measure"], lim["shape"]): (lim["op"], lim["target"], lim["share"], lim["outer"])
           for lim in req["limits"]}
    assert got == {("warpage", "any"): ("<=", 15.0, 0.01, 30.0), ("aspect_ratio", "any"): ("<=", 4.0, 0.01, 6.0),
                   ("jacobian", "any"): (">=", 0.6, 0.01, 0.4), ("max_angle", "quad"): ("<=", 135.0, 0.01, 150.0),
                   ("min_angle", "quad"): (">=", 45.0, 0.01, 30.0), ("max_angle", "tria"): ("<=", 120.0, 0.01, 140.0),
                   ("min_angle", "tria"): (">=", 30.0, 0.01, 20.0)}
    # one plain line per requirement, in the order written, each tied to the user's own line
    assert [r["quote"] for r in req["rows"]] == LINES
    assert [r["asked"] for r in req["rows"]][:5] == [
        "global element size 10 mm",
        "shortest element edge over 5 mm (0.5 x the global size)",
        "longest element edge under 15 mm (1.5 x the global size)",
        "warpage under 15° (up to 1% of elements between 15° and 30°)",
        "aspect ratio under 4 (up to 1% of elements between 4 and 6)"]
    assert req["rows"][5]["asked"] == "Jacobian over 0.6 (up to 1% of elements between 0.4 and 0.6)"
    assert req["rows"][6]["asked"] == "quad largest corner angle under 135° (up to 1% of elements between 135° and 150°)"
    assert not MR.is_empty(req)


def test_a_quote_that_is_not_the_users_words_is_dropped_and_said():
    req, dropped, _na = _one("keep it simple", sizes=[{"what": "global", "value": 5, "unit": "mm",
                                                       "quote": "elements around 5 mm"}])
    assert MR.is_empty(req) and len(dropped) == 1 and "not in your words" in dropped[0]


def test_a_number_that_is_not_in_the_quote_is_dropped():
    # the model "helpfully" rounds 4.75 to 5: not the user's number
    req, dropped, _na = _one("mesh at about 4.75 mm please",
                             sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "about 4.75 mm"}])
    assert MR.is_empty(req) and "is not in those words" in dropped[0]
    req, dropped, _na = _one("aspect ratio under 3",
                             limits=[{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 5,
                                      "allowed_percent": 0, "outer": 0, "has_outer": False,
                                      "quote": "aspect ratio under 3"}])
    assert MR.is_empty(req) and "the limit 5" in dropped[0]


@pytest.mark.parametrize("text,value,unit,mm", [
    ("Global mesh size = 0.4 in", 0.4, "in", 10.16), ("element size 1.2 cm", 1.2, "cm", 12.0),
    ("mesh the hull at 0.05 m", 0.05, "m", 50.0), ("elements of 500 um", 500, "um", 0.5),
    ("keep elements around 5 mm", 5, "mm", 5.0), ("element size five mm", 5, "mm", 5.0)])
def test_the_unit_is_the_one_the_user_states(text, value, unit, mm):
    req, dropped, _na = _one(text, sizes=[{"what": "global", "value": value, "unit": unit, "quote": text}])
    assert dropped == [] and req["sizes"]["global_mm"] == pytest.approx(mm)
    assert f"{mm:g} mm" in req["rows"][0]["asked"]


def test_a_deck_in_inches_is_read_in_inches_whatever_the_cad_is_in():
    notes = "All dimensions in inches.\nGlobal Mesh Size = 0.4\nMin. Element Length > 0.2"
    req, dropped, _na = _one(notes, stated_unit="in", sizes=[
        {"what": "global", "value": 0.4, "unit": "none", "quote": "Global Mesh Size = 0.4"},
        {"what": "min_edge", "value": 0.2, "unit": "none", "quote": "Min. Element Length > 0.2"}])
    assert dropped == []
    assert req["sizes"]["global_mm"] == pytest.approx(10.16) and req["sizes"]["min_edge_mm"] == pytest.approx(5.08)
    assert "(0.4 inch)" in req["rows"][0]["asked"]


def test_no_unit_anywhere_is_read_as_millimetres_and_said():
    req, dropped, _na = _one("element size 3", sizes=[{"what": "global", "value": 3, "unit": "none",
                                                       "quote": "element size 3"}])
    assert dropped == [] and req["sizes"]["global_mm"] == 3.0
    assert "no unit was written: read as millimetres" in req["rows"][0]["asked"]


def test_a_multiple_of_the_global_size_needs_a_global_size():
    req, dropped, _na = _one("min element length 0.5 x global size",
                             sizes=[{"what": "min_edge", "value": 0.5, "unit": "x_global",
                                     "quote": "min element length 0.5 x global size"}])
    assert MR.is_empty(req) and "no global size was given" in dropped[0]


def test_contradictions_are_told_and_only_the_sound_part_kept():
    notes = "global size 10 mm, min edge 12 mm, max edge 8 mm"
    req, dropped, _na = _one(notes, sizes=[
        {"what": "global", "value": 10, "unit": "mm", "quote": "global size 10 mm"},
        {"what": "min_edge", "value": 12, "unit": "mm", "quote": "min edge 12 mm"},
        {"what": "max_edge", "value": 8, "unit": "mm", "quote": "max edge 8 mm"}])
    assert req["sizes"]["global_mm"] == 10.0 and req["sizes"]["min_edge_mm"] is None and req["sizes"]["max_edge_mm"] is None
    assert [r["field"] for r in req["rows"]] == ["global"]
    assert any("is not under the longest" in d for d in dropped)
    # the shortest edge over the global size alone
    req, dropped, _na = _one("size 10 mm, shortest edge 15 mm", sizes=[
        {"what": "global", "value": 10, "unit": "mm", "quote": "size 10 mm"},
        {"what": "min_edge", "value": 15, "unit": "mm", "quote": "shortest edge 15 mm"}])
    assert req["sizes"]["min_edge_mm"] is None and "over the global size" in dropped[0]
    # a second global size: the first one written is kept
    req, dropped, _na = _one("mesh at 5 mm. actually mesh at 8 mm", sizes=[
        {"what": "global", "value": 5, "unit": "mm", "quote": "mesh at 5 mm"},
        {"what": "global", "value": 8, "unit": "mm", "quote": "mesh at 8 mm"}])
    assert req["sizes"]["global_mm"] == 5.0 and "the first one written is kept" in dropped[0]


def _limit(notes, measure, direction, target, shape="any", pct=0, outer=0, has_outer=False):
    return _one(notes, limits=[{"measure": measure, "shape": shape, "direction": direction, "target": target,
                                "allowed_percent": pct, "outer": outer, "has_outer": has_outer, "quote": notes}])


def test_a_limits_direction_is_its_measures_own():
    req, dropped, _na = _limit("aspect ratio > 5", "aspect_ratio", "min", 5)
    assert MR.is_empty(req) and "aspect ratio can only be held under a value" in dropped[0]
    req, dropped, _na = _limit("Jacobian < 0.6", "jacobian", "max", 0.6)
    assert MR.is_empty(req) and "Jacobian can only be held over a value" in dropped[0]
    # skew is read both ways: a most (0 is ideal) or a least (90 is ideal)
    for direction, op in (("max", "<="), ("min", ">=")):
        req, dropped, _na = _limit("skew 60 degrees", "skew", direction, 60)
        assert dropped == [] and req["limits"][0]["op"] == op


def test_a_limit_outside_any_sane_range_is_dropped():
    assert _limit("jacobian over 6", "jacobian", "min", 6)[1]
    assert _limit("aspect ratio under 0.5", "aspect_ratio", "max", 0.5)[1]
    assert _limit("warpage under 400 degrees", "warpage", "max", 400)[1]
    assert _limit("aspect ratio under 3", "aspect_ratio", "max", 3)[1] == []


def test_an_outer_limit_on_the_wrong_side_keeps_the_target_only():
    req, dropped, _na = _limit("Warpage < 15 (1% between 15 and 10 is ok)", "warpage", "max", 15, pct=1, outer=10,
                               has_outer=True)
    lim = req["limits"][0]
    assert (lim["target"], lim["share"], lim["outer"]) == (15.0, 0.0, None) and "not past the target" in dropped[0]


def test_a_share_with_no_outer_limit_is_kept():
    req, dropped, _na = _limit("aspect ratio below 5, 2% of elements may exceed", "aspect_ratio", "max", 5, pct=2)
    lim = req["limits"][0]
    assert dropped == [] and (lim["share"], lim["outer"]) == (0.02, None)
    assert req["rows"][0]["asked"] == "aspect ratio under 5 (up to 2% of elements may be past it)"


def test_the_element_kind_and_its_contradictions():
    req, dropped, _na = _one("no triangles please", element_kind=[{"what": "quads_only", "value": 0,
                                                                  "quote": "no triangles"}])
    assert req["kind"]["shell"] == "quads" and dropped == [] and req["rows"][0]["asked"] == "shell elements: quads only, no triangles"
    req, dropped, _na = _one("quad mesh only. triangles only.", element_kind=[
        {"what": "quads_only", "value": 0, "quote": "quad mesh only"},
        {"what": "triangles_only", "value": 0, "quote": "triangles only"}])
    assert req["kind"]["shell"] == "quads" and "both quads only and triangles only" in dropped[0]
    req, dropped, _na = _one("at most 5% trias, second order", element_kind=[
        {"what": "max_triangle_percent", "value": 5, "quote": "at most 5% trias"},
        {"what": "quadratic", "value": 0, "quote": "second order"}])
    assert req["kind"] == {"shell": None, "tria_percent_max": 5.0, "order": "quadratic"} and dropped == []


def test_a_wall_thickness_is_kept_in_the_set():
    req, dropped, _na = _one("2 mm sheet steel", sizes=[{"what": "thickness", "value": 2, "unit": "mm",
                                                         "quote": "2 mm sheet"}])
    assert dropped == [] and req["sizes"]["thickness_mm"] == 2.0 and req["rows"][0]["field"] == "thickness"


# ------------------------------------------------------------------ NOTE-8: never silently dropped --------
def test_what_fits_no_field_is_kept_as_not_applied():
    notes = "3 elements through the thickness.\nChordal deviation < 0.1 mm.\nLoad 900 daN on the lug.\n3 mm on the bracket."
    req, dropped, not_applied = _one(notes, other=[
        {"quote": "3 elements through the thickness.", "kind": "mesh"},
        {"quote": "Chordal deviation < 0.1 mm.", "kind": "mesh"},
        {"quote": "Load 900 daN on the lug.", "kind": "not_mesh"},
        {"quote": "3 mm on the bracket.", "kind": "elsewhere"}])
    assert MR.is_empty(req) and dropped == []
    assert not_applied == ["3 elements through the thickness.", "Chordal deviation < 0.1 mm."]
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied)
    said = MR.words(sec)
    assert said == ['Not applied: "3 elements through the thickness." - no mesh setting of ours matches it yet.',
                    'Not applied: "Chordal deviation < 0.1 mm." - no mesh setting of ours matches it yet.']
    assert MR.requirements_of(sec) is None        # nothing a route applies: the run is the rules' own


def test_words_about_the_mesh_the_reading_listed_nowhere_are_told_too():
    # the model read the size and listed nothing else: the rest of the sentence is about the mesh, so it is told
    notes = "10 mm mesh, but don't go below 3 elements across any flange. Load 2 kN on the hook."
    req, dropped, not_applied = _one(notes, sizes=[{"what": "global", "value": 10, "unit": "mm", "quote": "10 mm mesh"}])
    assert req["sizes"]["global_mm"] == 10.0 and dropped == []
    assert not_applied == ["don't go below 3 elements across any flange"]
    # words that are covered, short, or not about the mesh are not
    assert _one("mesh the hull at 0.05 m", sizes=[{"what": "global", "value": 0.05, "unit": "m",
                                                   "quote": "at 0.05 m"}])[2] == []
    assert _one("Fixed at the base, 1000 N on the tip of the long arm.")[2] == []


def test_a_fraction_is_its_value_and_the_unit_beside_the_number_is_the_users():
    text = "Average element size: 3/8 inch"
    # the model read the 3 of 3/8, with no unit: not the user's number
    req, dropped, _na = _one(text, sizes=[{"what": "global", "value": 3, "unit": "none", "quote": text}])
    assert MR.is_empty(req) and "is not in those words" in dropped[0]
    # the fraction's value, and the inch written beside it wins over a model that left the unit out
    req, dropped, _na = _one(text, sizes=[{"what": "global", "value": 0.375, "unit": "none", "quote": text}])
    assert dropped == [] and req["sizes"]["global_mm"] == pytest.approx(9.525)
    req, _d, _na = _one('Element length 0.4"', sizes=[{"what": "global", "value": 0.4, "unit": "mm",
                                                          "quote": 'Element length 0.4"'}])
    assert req["sizes"]["global_mm"] == pytest.approx(10.16)
    # "in" is a word too: never read off the quote as inches
    req, _d, _na = _one("mesh size 10 in the flange area", sizes=[{"what": "global", "value": 10, "unit": "none",
                                                                   "quote": "mesh size 10 in the flange area"}])
    assert req["sizes"]["global_mm"] == 10.0


def test_for_99_percent_of_elements_allows_one_percent():
    req, dropped, _na = _limit("Jacobian above 0.7 for 99% of elements", "jacobian", "min", 0.7, pct=1)
    assert dropped == [] and req["limits"][0]["share"] == pytest.approx(0.01)


def test_quads_with_a_triangle_share_is_the_share():
    req, dropped, _na = _one("Use quads, trias <5%", element_kind=[
        {"what": "quads_only", "value": 0, "quote": "Use quads"},
        {"what": "max_triangle_percent", "value": 5, "quote": "trias <5%"}])
    assert req["kind"] == {"shell": None, "tria_percent_max": 5.0, "order": None} and dropped == []
    assert [r["field"] for r in req["rows"]] == ["max_triangle_percent"]


def test_a_quote_off_by_a_character_is_read_as_the_users_own_line():
    notes = "全局网格尺寸 8 毫米，翘曲小于 10 度"
    req, dropped, _na = _one(notes, sizes=[{"what": "global", "value": 8, "unit": "mm",
                                           "quote": "全球网格尺寸 8 毫米，翘曲小于 10 度"}])
    assert dropped == [] and req["sizes"]["global_mm"] == 8.0 and req["rows"][0]["quote"] == notes
    # but a number the user's own line does not hold is still dropped
    req, dropped, _na = _one(notes, sizes=[{"what": "global", "value": 9, "unit": "mm",
                                           "quote": "全球网格尺寸 9 毫米，翘曲小于 10 度"}])
    assert MR.is_empty(req) and dropped


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"sizes": 3}', '{"limits": [{"measure": 1}]}'])
def test_an_answer_of_any_other_shape_is_an_empty_set(raw):
    req, dropped, _not_applied = MR.check(raw, CRITERIA)
    assert MR.is_empty(req)


# ------------------------------------------------------------------ the words --------------------------------
def test_before_meshing_one_short_line_each():
    req, dropped, na = _check(criteria_answer(), CRITERIA)
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=na)
    said = MR.words(sec)
    assert len(said) == 10 and said[0] == "From your notes: global element size 10 mm."
    assert said[3] == "From your notes: warpage under 15° (up to 1% of elements between 15° and 30°)."
    # every line passes the live commentary's guard (no "=", "<", no rule id, no file)
    live = MR.live_words(sec)
    assert len(live) == 10 and all(narration.plain(line) for line in live)
    quoted = MR.section("used", requirements=MR.empty(), not_applied=["Chordal deviation < 0.1 mm"])
    assert narration.plain(MR.live_words(quoted)[0])
    assert MR.words(MR.section("none")) == [] and MR.decisions(MR.section("none")) == []


def test_the_why_record_carries_the_reading():
    req, dropped, na = _check(criteria_answer(), CRITERIA)
    d = MR.decisions(MR.section("used", requirements=req, key="k1", source="model"), stage="fea_ai")
    assert d[0]["rule"] == "NOTE-7" and d[0]["value"] == 10 and d[0]["inputs"]["sizes"]["global_mm"] == 10.0
    assert len(d[0]["inputs"]["limits"]) == 7


# ------------------------------------------------------------------ asking, and NOTE-9 ------------------------
class _Router:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls, self.seen = answer, exc, 0, None

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        self.seen = (messages, tools, tool_choice)
        if self.exc:
            raise self.exc
        return self.answer


def _answer(args: dict | str) -> ModelRoundResult:
    return ModelRoundResult(tool_calls=(ToolCallRequest(
        id="call_abcdef123", name=MR.TOOL, arguments=args if isinstance(args, str) else json.dumps(args)),))


@pytest.fixture
def router(monkeypatch):
    def install(r):
        monkeypatch.setattr(llm, "_router", r)
        return r
    return install


def _notes(text: str) -> dict:
    return {"user_notes": [{"text": text, "source": "console"}], "user_id": "u1"}


def _decide(state, **kw):
    return asyncio.run(MR.decide(state, route="fea", job_id="j1", **kw))


def test_no_words_asks_no_model_and_asks_for_nothing(router):
    r = router(_Router(exc=AssertionError("the model must not be asked")))
    for state in ({}, {"user_notes": []}, {"user_notes": [{"text": "   ", "source": "console"}], "request_txt": ""}):
        sec = _decide(state)
        assert sec["status"] == "none" and MR.requirements_of(sec) is None and MR.words(sec) == []
    assert r.calls == 0


def test_the_notes_are_shown_word_for_word_and_the_answer_is_checked(router):
    r = router(_Router(_answer(criteria_answer())))
    sec = _decide(_notes(CRITERIA))
    assert r.calls == 1 and sec["status"] == "used" and sec["source"] == "model"
    shown = json.loads(r.seen[0][1]["content"])
    assert CRITERIA in shown["user_notes"]
    assert r.seen[2] == {"type": "function", "function": {"name": MR.TOOL}}      # one forced, strict call
    assert len(MR.requirements_of(sec)["rows"]) == 10


def test_notes_with_no_mesh_requirement_give_an_empty_set(router):
    router(_Router(_answer({"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [],
                            "other": [{"quote": "Load 900 daN on the lug.", "kind": "not_mesh"}]})))
    sec = _decide(_notes("Load 900 daN on the lug."))
    assert sec["status"] == "used" and MR.requirements_of(sec) is None and MR.words(sec) == []


@pytest.mark.parametrize("make", [lambda: _Router(exc=TimeoutError()), lambda: _Router(ModelRoundResult()),
                                  lambda: _Router(ModelRoundResult(failure_marker="[PROVIDER_DOWN]"))])
def test_a_model_that_is_down_applies_nothing_and_says_so(router, make, monkeypatch):
    from meshpipeline.contracts import decision_cache

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    router(make())
    sec = _decide(_notes(CRITERIA))
    assert sec["status"] in ("failed", "invalid") and MR.requirements_of(sec) is None
    assert MR.words(sec) == [MR.UNREAD_WORDS]
    assert MR.decisions(sec)[0]["rule"] == "NOTE-9"


def test_a_model_that_is_down_replays_its_saved_reading_of_the_same_words(router, monkeypatch):
    from meshpipeline.contracts import decision_cache

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: _answer(criteria_answer()))
    router(_Router(exc=TimeoutError()))
    sec = _decide(_notes(CRITERIA))
    assert sec["status"] == "used" and sec["source"] == "saved" and len(MR.requirements_of(sec)["rows"]) == 10
    assert any("saved reading" in w for w in MR.words(sec))


def test_the_same_words_make_the_same_question(router):
    a = MR.messages_for(_notes(CRITERIA), route="fea")
    b = MR.messages_for(_notes(CRITERIA), route="fea")
    assert a == b and MR._key(a, MR.tool()) == MR._key(b, MR.tool())


def test_the_step_off_asks_nobody(router):
    r = router(_Router(exc=AssertionError("off")))
    assert _decide(_notes(CRITERIA), enabled=False)["status"] == "none" and r.calls == 0


def test_the_reading_is_kept_in_the_workspace_only_when_it_asks_for_something(tmp_path):
    req, dropped, na = _check(criteria_answer(), CRITERIA)
    sec = MR.section("used", requirements=req)
    assert MR.write(tmp_path, sec) is True and MR.requirements_of(MR.read(tmp_path)) == req
    # a run with no requirement removes a carried file: its workspace is exactly as it always was
    assert MR.write(tmp_path, MR.section("none")) is False and not (tmp_path / MR.FILE).exists()
    assert MR.read(tmp_path) == {}


# ------------------------------------------------------------------ the flow route (CFD) ------------------------
def test_flow_limits_are_read_into_the_same_set_and_every_flow_engine_reads_it():
    """The reading is route-neutral: a flow mesh's limits land in the same typed set, and the rows any route's
    result reads are built the same way. Every flow engine reads the set (engines/cfd_requirements, NOTE-10): the
    consumer registry says so."""
    from meshpipeline.contracts import user_notes as un

    notes = "Max non-orthogonality 65 degrees, skewness below 4, cells around 5 mm"
    req, dropped, _na = _one(notes, sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "cells around 5 mm"}],
                             limits=[{"measure": "non_orthogonality", "shape": "cell", "direction": "max", "target": 65,
                                      "allowed_percent": 0, "outer": 0, "has_outer": False,
                                      "quote": "Max non-orthogonality 65 degrees"},
                                     {"measure": "skewness", "shape": "cell", "direction": "max", "target": 4,
                                      "allowed_percent": 0, "outer": 0, "has_outer": False,
                                      "quote": "skewness below 4"}])
    assert dropped == [] and req["sizes"]["global_mm"] == 5.0
    assert [(lim["measure"], lim["shape"], lim["op"], lim["target"]) for lim in req["limits"]] == [
        ("non_orthogonality", "cell", "<=", 65.0), ("skewness", "cell", "<=", 4.0)]
    assert [r["asked"] for r in req["rows"]] == ["global element size 5 mm", "cell non-orthogonality under 65°",
                                                  "cell skewness under 4"]
    # a measured report of them would be rows of the one list every result and verdict reads
    rep = MR.finish_report([{"id": "r2", "field": "limit:non_orthogonality", "asked": req["rows"][1]["asked"],
                             **MR.limit_row(req["limits"][0], np.array([40.0, 62.0, 71.0]), 3)}], 3, "")
    (row,) = MR.asked_rows({"requirements": rep})
    assert row["measured"] == 1 and row["said"] == ("Cell non-orthogonality under 65°: not fully met - 33.3% of "
                                                   "elements (1) are over 65°; the worst is 71°.")
    assert "mesh_requirements" in un.ROUTE_CONSUMERS["fea"] and "mesh_requirements" in un.ENGINE_CONSUMERS["gmsh"]
    assert "mesh_requirements" in un.ROUTE_CONSUMERS["flow"]
    assert all("mesh_requirements" in v for v in un.ENGINE_CONSUMERS.values())
