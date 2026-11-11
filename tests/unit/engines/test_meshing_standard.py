# Responsibility: Verify the team's meshing standard (STD-1..3): it is read by the notes' own reader into the same typed set; the reading kept at save time is reused at run time with no model call (a changed question or none kept: read again, with the notes' fallbacks); a run merges rules < settings < notes field by field and says where each value came from; with no standard the notes' reading is returned exactly as before.
# Boundaries: pure over plain data and a fake model router; the driver's reading step with a fake publisher. No mesh is made here (tests/unit/engines/test_meshing_standard_routes.py meshes).
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _blank(**fields) -> dict:
    base = {"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return base


def _limit(measure, shape, direction, target, quote, pct=0, outer=0):
    return {"measure": measure, "shape": shape, "direction": direction, "target": target, "allowed_percent": pct,
            "outer": outer, "has_outer": bool(outer), "quote": quote}


def _std(text: str, answer: dict | None = None, *, key: str | None = None) -> dict:
    """The standard as a run is dispatched with it: the words and, with `answer`, the reading kept at save time."""
    out: dict = {"text": text}
    if answer is not None:
        out["reading"] = {"key": key if key is not None else MS.key_of(text), "arguments": json.dumps(answer)}
    return out


def _notes(text: str) -> list[dict]:
    return [{"text": text, "source": "console"}]


class _Router:
    """Answers the standard's question with `settings` and the notes' with `notes` (None: a model that fails the
    test if it is asked that one)."""

    def __init__(self, settings=None, notes=None, exc=None):
        self.settings, self.notes, self.exc = settings, notes, exc
        self.asked: list[str] = []

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        which = "settings" if "from settings" in messages[1]["content"] else "notes"
        self.asked.append(which)
        if self.exc:
            raise self.exc
        answer = self.settings if which == "settings" else self.notes
        if answer is None:
            raise AssertionError(f"the model was asked about the {which}")
        return ModelRoundResult(tool_calls=(ToolCallRequest(
            id="call_abcdef123", name=MR.TOOL, arguments=json.dumps(answer)),))


@pytest.fixture
def router(monkeypatch):
    from meshpipeline.contracts import decision_cache

    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)

    def install(r):
        monkeypatch.setattr(llm, "_router", r)
        return r
    return install


def _decide(state, **kw):
    return asyncio.run(MS.decide(state, route="fea", job_id="j1", **kw))


def _bare(req: dict) -> dict:
    """A set without where its rows came from and their ids (to compare with the notes' own reading)."""
    out = copy.deepcopy(req)
    for r in out.get("rows") or []:
        r.pop("from", None)
        r.pop("id", None)
    for lim in out.get("limits") or []:
        lim.pop("id", None)
    return out


# ------------------------------------------------------------------ STD-3: nothing in settings, nothing changes ---
def test_with_no_standard_the_run_reads_the_notes_exactly_as_before(router):
    r = router(_Router(notes=criteria_answer()))
    state = {"user_notes": _notes(CRITERIA), "user_id": "u1"}
    before = asyncio.run(MR.decide(state, route="fea", job_id="j1"))
    for std in (None, {}, {"text": ""}, {"text": "   \n "}):
        got = _decide({**state, MS.STATE_KEY: std})
        assert json.dumps(got, sort_keys=True) == json.dumps(before, sort_keys=True)
        assert "lines" not in got and "standard" not in got
    assert r.asked == ["notes"] * 5
    # nothing anywhere: no model, an empty section
    r2 = router(_Router())
    assert _decide({MS.STATE_KEY: None})["status"] == "none" and r2.asked == []
    assert MS.merge(None, before) is before and MS.merge(MR.section("none"), before) is before


# ------------------------------------------------------------------ STD-1: one reader, kept at save time ----------
def test_the_reading_kept_at_save_time_is_used_with_no_model_call(router):
    r = router(_Router())
    sec = _decide({MS.STATE_KEY: _std(CRITERIA, criteria_answer()), "user_id": "u1"})
    assert r.asked == [] and sec["status"] == "used"
    req = MR.requirements_of(sec)
    assert [row["id"] for row in req["rows"]] == [f"s{i}" for i in range(1, 11)]
    assert {row["from"] for row in req["rows"]} == {"settings"}
    words = MR.words(sec)
    assert words[0] == "From your settings: global element size 10 mm."
    assert words[3] == "From your settings: warpage under 15° (up to 1% of elements between 15° and 30°)."
    assert len(words) == 10


def test_the_standard_is_the_same_set_the_same_words_in_the_notes_give(router):
    router(_Router(notes=criteria_answer()))
    from_notes = MR.requirements_of(_decide({"user_notes": _notes(CRITERIA), "user_id": "u1"}))
    from_settings = MR.requirements_of(_decide({MS.STATE_KEY: _std(CRITERIA, criteria_answer())}))
    assert _bare(from_settings) == _bare(from_notes)


def test_the_standard_is_shown_to_the_model_as_the_notes_are_with_no_route(router):
    shown = json.loads(MS.messages(CRITERIA)[1]["content"])
    assert shown["mesh_for"] == "" and CRITERIA in shown["user_notes"]
    assert "the team's meshing standard, from settings" in shown["user_notes"]
    assert MS.messages(CRITERIA)[0]["content"] == MR.SYSTEM
    # the same words are the same question, whoever saves them and whatever route runs them
    assert MS.key_of(CRITERIA) == MS.key_of(CRITERIA) != MS.key_of(CRITERIA + "\nGrowth rate 1.2")


def test_a_changed_question_reads_the_standard_again_through_the_builder_route(router):
    r = router(_Router(settings=criteria_answer()))
    sec = _decide({MS.STATE_KEY: _std(CRITERIA, criteria_answer(), key="an-older-prompt")})
    assert r.asked == ["settings"] and sec["standard"]["source"] == "model"
    assert len(MR.requirements_of(sec)["rows"]) == 10
    # words saved unread (the model was down at save time): read at run time the same way
    r2 = router(_Router(settings=criteria_answer()))
    assert MR.requirements_of(_decide({MS.STATE_KEY: _std(CRITERIA)}))["sizes"]["global_mm"] == 10.0
    assert r2.asked == ["settings"]


def test_a_standard_that_cannot_be_read_never_fails_the_run_and_is_said(router):
    router(_Router(exc=TimeoutError()))
    sec = _decide({MS.STATE_KEY: _std(CRITERIA)})
    assert sec["status"] == "failed" and MR.requirements_of(sec) is None
    assert MR.words(sec) == [MS.UNREAD_WORDS]

    # with notes that were read, the notes apply and the standard's absence is said first
    class _Half(_Router):
        async def call_builder_model(self, messages, **kw):
            if "from settings" in messages[1]["content"]:
                raise TimeoutError()
            return await super().call_builder_model(messages, **kw)

    router(_Half(notes=_blank(sizes=[{"what": "global", "value": 6, "unit": "mm",
                                      "quote": "Global Mesh Size = 6 mm"}])))
    sec = _decide({MS.STATE_KEY: _std(CRITERIA), "user_notes": _notes("Global Mesh Size = 6 mm")})
    assert sec["status"] == "used" and MR.requirements_of(sec)["sizes"]["global_mm"] == 6.0
    assert MR.words(sec) == [MS.UNREAD_WORDS, "From your notes: global element size 6 mm."]


def test_a_saved_round_of_the_same_words_is_replayed_when_the_model_is_down(router, monkeypatch):
    from meshpipeline.contracts import decision_cache

    router(_Router(exc=TimeoutError()))
    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: ModelRoundResult(tool_calls=(
        ToolCallRequest(id="call_abcdef123", name=MR.TOOL, arguments=json.dumps(criteria_answer())),)))
    sec = _decide({MS.STATE_KEY: _std(CRITERIA)})
    assert sec["standard"]["source"] == "saved" and len(MR.requirements_of(sec)["rows"]) == 10
    assert any("saved reading of your team's meshing settings" in w for w in MR.words(sec))


def test_with_the_ai_step_off_a_kept_reading_still_applies_and_none_kept_is_said(router):
    r = router(_Router())
    sec = _decide({MS.STATE_KEY: _std(CRITERIA, criteria_answer())}, enabled=False)
    assert r.asked == [] and len(MR.requirements_of(sec)["rows"]) == 10
    sec = _decide({MS.STATE_KEY: _std(CRITERIA)}, enabled=False)
    assert sec["status"] == "off" and MR.words(sec) == [MS.OFF_WORDS] and r.asked == []


# ------------------------------------------------------------------ STD-2: rules < settings < notes ---------------
def _both(settings_text, settings_answer, notes_text, notes_answer, router):
    router(_Router(notes=notes_answer))
    return _decide({MS.STATE_KEY: _std(settings_text, settings_answer), "user_notes": _notes(notes_text),
                    "user_id": "u1"})


def test_the_notes_win_field_by_field_and_the_standard_keeps_the_rest(router):
    notes = "Global Mesh Size = 5 mm\nAspect Ratio < 3.0"
    sec = _both(CRITERIA, criteria_answer(), notes, _blank(
        sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "Global Mesh Size = 5 mm"}],
        limits=[_limit("aspect_ratio", "any", "max", 3.0, "Aspect Ratio < 3.0")]), router)
    req = MR.requirements_of(sec)
    # the notes' global size; the standard's edge lengths, which are multiples of the global size, follow it
    assert req["sizes"]["global_mm"] == 5.0 and req["sizes"]["min_edge_mm"] == 2.5 and req["sizes"]["max_edge_mm"] == 7.5
    came = {(r["field"], r["from"]) for r in req["rows"]}
    assert ("global", "notes") in came and ("global", "settings") not in came
    assert ("min_edge", "settings") in came and ("limit:aspect_ratio", "notes") in came
    aspect = [lim for lim in req["limits"] if lim["measure"] == "aspect_ratio"]
    assert len(aspect) == 1 and aspect[0]["target"] == 3.0 and aspect[0]["share"] == 0.0
    assert len(req["limits"]) == 7 and len(req["rows"]) == 10
    assert len({r["id"] for r in req["rows"]}) == 10            # no two rows share an id
    words = MR.words(sec)
    assert "From your settings: shortest element edge over 2.5 mm (0.5 x the global size)." in words
    assert "From your notes: global element size 5 mm." in words
    assert "From your notes: aspect ratio under 3." in words
    assert not any("global element size 10 mm" in w for w in words)
    # the merge is kept for the why record: what the notes replaced
    assert {o["field"] for o in sec["standard"]["overridden"]} == {"global", "limit:aspect_ratio"}
    d = MS.decisions(sec, stage="fea_ai")
    assert d[0]["rule"] == "STD-2" and d[0]["value"] == 8 and "your notes win field by field" in d[0]["words"]


def test_a_limit_of_another_element_shape_is_another_field(router):
    sec = _both("Aspect Ratio < 5", _blank(limits=[_limit("aspect_ratio", "any", "max", 5, "Aspect Ratio < 5")]),
                "Quad aspect ratio < 3", _blank(limits=[_limit("aspect_ratio", "quad", "max", 3,
                                                              "Quad aspect ratio < 3")]), router)
    assert {(lim["shape"], lim["target"]) for lim in MR.requirements_of(sec)["limits"]} == {("any", 5.0), ("quad", 3.0)}


def test_lengths_the_notes_give_with_no_unit_take_the_unit_the_standard_names(router):
    std = "All dimensions in inches\nWarpage < 15°"
    sec = _both(std, _blank(stated_unit="in", limits=[_limit("warpage", "any", "max", 15, "Warpage < 15°")]),
                "Global mesh size 0.4", _blank(sizes=[{"what": "global", "value": 0.4, "unit": "none",
                                                       "quote": "Global mesh size 0.4"}]), router)
    req = MR.requirements_of(sec)
    assert req["sizes"]["global_mm"] == pytest.approx(10.16)
    words = MR.words(sec)
    assert "From your settings: lengths with no unit are in inches." in words
    assert ("From your notes: global element size 10.16 mm (0.4 inch) (no unit was written: inches, as your "
            "settings say).") in words
    # notes that name their own unit keep it
    sec = _both(std, _blank(stated_unit="in"), "Global mesh size 8 mm",
                _blank(sizes=[{"what": "global", "value": 8, "unit": "mm", "quote": "Global mesh size 8 mm"}]), router)
    assert MR.requirements_of(sec)["sizes"]["global_mm"] == 8.0


def test_a_unit_the_standard_never_writes_is_not_taken(router):
    sec = _both("Warpage < 15°", _blank(stated_unit="in", limits=[_limit("warpage", "any", "max", 15,
                                                                         "Warpage < 15°")]),
                "Global mesh size 4", _blank(sizes=[{"what": "global", "value": 4, "unit": "none",
                                                     "quote": "Global mesh size 4"}]), router)
    assert MR.requirements_of(sec)["sizes"]["global_mm"] == 4.0
    assert not any("inches" in w for w in MR.words(sec))


def test_a_value_of_the_standard_that_contradicts_the_notes_is_told_and_the_notes_kept(router):
    sec = _both("Min. Element Length > 5 mm", _blank(sizes=[{"what": "min_edge", "value": 5, "unit": "mm",
                                                              "quote": "Min. Element Length > 5 mm"}]),
                "Global Mesh Size = 3 mm", _blank(sizes=[{"what": "global", "value": 3, "unit": "mm",
                                                          "quote": "Global Mesh Size = 3 mm"}]), router)
    req = MR.requirements_of(sec)
    assert req["sizes"]["global_mm"] == 3.0 and req["sizes"]["min_edge_mm"] is None
    assert [r["field"] for r in req["rows"]] == ["global"]
    assert ("Not applied (your settings): shortest element edge over 5 mm - it does not fit the 3 mm global size "
            "your notes ask for.") in MR.words(sec)


def test_quads_only_and_a_share_of_triangles_are_one_choice_and_the_notes_make_it(router):
    sec = _both("quad mesh only", _blank(element_kind=[{"what": "quads_only", "value": 0, "quote": "quad mesh only"}]),
                "at most 5% trias", _blank(element_kind=[{"what": "max_triangle_percent", "value": 5,
                                                          "quote": "at most 5% trias"}]), router)
    req = MR.requirements_of(sec)
    assert req["kind"] == {"shell": None, "tria_percent_max": 5.0, "order": None}
    assert [r["field"] for r in req["rows"]] == ["max_triangle_percent"]
    sec = _both("triangles only", _blank(element_kind=[{"what": "triangles_only", "value": 0,
                                                        "quote": "triangles only"}]),
                "no triangles", _blank(element_kind=[{"what": "quads_only", "value": 0, "quote": "no triangles"}]),
                router)
    assert MR.requirements_of(sec)["kind"]["shell"] == "quads"


def test_what_the_standard_says_of_one_part_or_no_setting_is_told_never_dropped(router):
    std = "3 mm on the bracket\nUse 3 elements through the thickness\nWarpage < 15°"
    sec = _both(std, _blank(limits=[_limit("warpage", "any", "max", 15, "Warpage < 15°")],
                            other=[{"quote": "3 mm on the bracket", "kind": "elsewhere"},
                                   {"quote": "Use 3 elements through the thickness", "kind": "mesh"}]),
                "", None, router)
    words = MR.words(sec)
    assert words[0] == "From your settings: warpage under 15°."
    assert ('Not applied (your settings): "Use 3 elements through the thickness" - no mesh setting of ours matches '
            'it yet.') in words
    assert (f'Not applied (your settings): "3 mm on the bracket" - {MS.ELSEWHERE_WORDS}.') in words
    assert len(MR.not_applied_words(sec)) == 2


# ------------------------------------------------------------------ what the page, the result and the record say ---
def test_the_settings_page_shows_each_line_exactly_as_a_run_says_it():
    reading = {"key": MS.key_of(CRITERIA), "arguments": json.dumps(criteria_answer())}
    lines = MS.understood(CRITERIA, reading)
    assert lines[:3] == ["Global element size 10 mm.", "Shortest element edge over 5 mm (0.5 x the global size).",
                         "Longest element edge under 15 mm (1.5 x the global size)."]
    assert lines[4] == "Aspect ratio under 4 (up to 1% of elements between 4 and 6)."
    assert len(lines) == 10
    text = "Units: inches\nGlobal size 0.4\nNastran BDF please, 3 elements through the flange thickness"
    answer = _blank(stated_unit="in", sizes=[{"what": "global", "value": 0.4, "unit": "none", "quote": "Global size 0.4"}],
                    other=[{"quote": "3 elements through the flange thickness", "kind": "mesh"},
                           {"quote": "Units: inches", "kind": "mesh"}])
    assert MS.understood(text, {"key": "k", "arguments": json.dumps(answer)}) == [
        "Global element size 10.16 mm (0.4 inch).", "Lengths with no unit are in inches.",
        'Not applied: "3 elements through the flange thickness" - no mesh setting of ours matches it yet.']
    assert MS.understood("", reading) == [] and MS.understood(CRITERIA, None) == []


def test_the_closing_line_says_where_the_things_asked_for_came_from(router):
    from meshpipeline.contracts.user_notes import result_lines

    only = _decide({MS.STATE_KEY: _std(CRITERIA, criteria_answer())})
    both = _both(CRITERIA, criteria_answer(), "Global Mesh Size = 5 mm", _blank(
        sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "Global Mesh Size = 5 mm"}]), router)
    said = "all 10 things you asked for are met."
    assert MS.remark(only, said) == f"From your settings: {said}"
    assert MS.remark(both, said) == f"From your settings and notes: {said}"
    assert MS.remark(MR.section("used"), said) == said and MS.remark(None, said) == said and MS.remark(only, "") == ""
    assert result_lines([MS.remark(only, said), "refined at the holes"]) == [
        f"From your settings: {said}", "From your notes: refined at the holes"]


def test_each_result_row_says_its_source_and_a_notes_only_row_reads_as_before():
    row = {"id": "s1", "asked": "warpage under 15°", "met": True, "delivered": "every element is inside it",
           "excess": 0}
    assert MR.line(row) == "Warpage under 15°: met."
    assert MR.line({**row, "from": "settings"}) == "Warpage under 15° (from your settings): met."
    q = {"requirements": {"rows": [{**row, "from": "settings"}, {**row, "id": "r1", "from": "notes"}, {**row, "id": "r2"}]}}
    got = MR.asked_rows(q, source="your notes (engines/shell_requirements.py)")
    assert [g["label"] for g in got] == ["Your settings ask: warpage under 15°", "Your notes ask: warpage under 15°",
                                         "You asked: warpage under 15°"]
    assert [g["source"] for g in got] == ["your settings (engines/shell_requirements.py)",
                                          "your notes (engines/shell_requirements.py)",
                                          "your notes (engines/shell_requirements.py)"]


def test_the_same_standard_and_notes_twice_give_the_same_reading(router):
    notes = "Global Mesh Size = 5 mm"
    answer = _blank(sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": notes}])
    a = _both(CRITERIA, criteria_answer(), notes, answer, router)
    b = _both(CRITERIA, criteria_answer(), notes, answer, router)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_the_snapshot_carries_the_words_and_only_a_usable_reading():
    reading = {"key": "k1", "arguments": "{}", "read_at": "2026-11-11T10:00:00+00:00"}
    assert MS.snapshot("  Warpage < 15  ", reading, updated_by="a@b.c", updated_at="t") == {
        "text": "Warpage < 15", "reading": {"key": "k1", "arguments": "{}"}, "updated_by": "a@b.c", "updated_at": "t"}
    assert MS.snapshot("Warpage < 15", None) == {"text": "Warpage < 15"}
    assert MS.snapshot("   ", reading) is None
    assert MS.standard_of({MS.STATE_KEY: {"text": "x"}}) == {"text": "x"} and MS.standard_of({}) is None


def test_the_reading_at_save_time_keeps_the_answer_and_the_key_or_nothing(router):
    router(_Router(settings=criteria_answer()))
    got = asyncio.run(MS.read_at_save(CRITERIA, user_id="u1"))
    assert got["key"] == MS.key_of(CRITERIA) and json.loads(got["arguments"]) == criteria_answer()
    assert MS.kept_reading({"text": CRITERIA, "reading": got}) == got["arguments"]
    assert MS.kept_reading({"text": CRITERIA + " ", "reading": got}) == got["arguments"]
    assert MS.kept_reading({"text": CRITERIA + "\nmore", "reading": got}) is None
    router(_Router(exc=TimeoutError()))
    assert asyncio.run(MS.read_at_save(CRITERIA)) is None
    assert asyncio.run(MS.read_at_save("  ")) is None


# ------------------------------------------------------------------ the driver's reading step ---------------------
class _Publish:
    def __init__(self):
        self.said = []

    async def anarrate(self, kind, line, n=None, of=None, unit="", op_id=""):
        self.said.append((kind, line, op_id))


class _Run:
    deadline_s = None


def test_the_driver_reads_both_once_tells_them_before_meshing_and_keeps_the_merge(tmp_path, monkeypatch, router):
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import shell_run

    r = router(_Router(notes=_blank(sizes=[{"what": "global", "value": 6, "unit": "mm",
                                            "quote": "Global Mesh Size = 6 mm"}])))
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    publish = _Publish()
    state = {MS.STATE_KEY: _std(CRITERIA, criteria_answer()), "user_notes": _notes("Global Mesh Size = 6 mm"),
             "user_id": "u1"}
    sec = asyncio.run(FD.read_requirements(tmp_path, state, job_id="j1", run=_Run(), publish=publish))
    assert r.asked == ["notes"] and sec["status"] == "used"
    said = [line for _k, line, _o in publish.said]
    assert said[0] == "From your settings: shortest element edge over 3 mm (0.5 x the global size)."
    assert "From your notes: global element size 6 mm." in said
    assert [c["rule"] for c in cited] == ["NOTE-7", "STD-2"]
    # the shell and mixed runs read the merged set from the workspace
    kept = shell_run.requirements_in(tmp_path)
    assert kept["sizes"]["global_mm"] == 6.0 and len(kept["limits"]) == 7


def test_the_driver_with_a_standard_alone_says_it_and_with_nothing_writes_nothing(tmp_path, monkeypatch, router):
    from meshpipeline.engines import fea_driver as FD

    r = router(_Router())
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    publish = _Publish()
    (tmp_path / "a").mkdir()
    sec = asyncio.run(FD.read_requirements(tmp_path / "a", {MS.STATE_KEY: _std(CRITERIA, criteria_answer())},
                                           job_id="j1", run=_Run(), publish=publish))
    assert r.asked == [] and len(publish.said) == 10 and (tmp_path / "a" / MR.FILE).exists()
    assert all(line.startswith("From your settings: ") for _k, line, _o in publish.said)
    assert MS.remark(sec, "all 10 things you asked for are met.").startswith("From your settings: ")
    (tmp_path / "b").mkdir()
    publish = _Publish()
    sec = asyncio.run(FD.read_requirements(tmp_path / "b", {MS.STATE_KEY: None}, job_id="j1", run=_Run(),
                                           publish=publish))
    assert sec["status"] == "none" and publish.said == [] and list((tmp_path / "b").iterdir()) == []
