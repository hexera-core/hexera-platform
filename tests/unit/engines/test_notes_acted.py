"""NOTE-15: what the notes say that the product acted on outside the requirements reading is never told or counted
as not applied - the engine the notes name above all, whatever the model's reading made of that sentence.

m483 round 12, 5a: "use snappyHexMesh" + "surface refinement level 3" meshed with snappyHexMesh, and the result said
'Not applied: "use snappyHexMesh" - no mesh setting of ours matches it yet.' and counted it as "1 other thing your
notes say about the mesh could not be applied"."""
from __future__ import annotations

import pytest

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import notes_acted as NA

ENGINES = [("snappy", "snappyHexMesh"), ("cfmesh", "cfMesh"), ("vmtk", "VMTK")]
#: the three ways the reading can put the engine's sentence aside
READINGS = ["not_applied", "not_mesh", "dropped"]
LEVEL = {"id": "r1", "field": "control:surface_level", "quote": "surface refinement level 3",
         "asked": "refinement level 3 at the walls"}


def _section(reading: str, quote: str, *, rows=(LEVEL,)) -> dict:
    req = MR.empty()
    req["rows"] = [dict(r) for r in rows]
    sec = MR.section("used", requirements=req)
    if reading == "not_applied":
        sec["not_applied"] = [quote]
    elif reading == "not_mesh":
        req["not_mesh"] = [{"quote": quote, "instead": MR.NOT_MESH_DEFAULT}]
    elif reading == "dropped":
        sec["dropped"] = [f'"{quote}": no such setting: not applied']
    return sec


def _told_not_applied(lines: list[str]) -> list[str]:
    return [w for w in lines if w.startswith(("Not applied", MR.NOT_MESH_LEAD))]


def _tally(sec: dict, engine: str, other_rows=()) -> str:
    rows = [r for r in [NA.report_row(sec, engine), *other_rows] if r is not None]
    others = max(0, len(MR.not_applied_words(sec)) - len(sec.get("dropped") or []))
    return CR.summary({"rows": rows}, not_applied=others)


@pytest.mark.parametrize("engine,word", ENGINES)
@pytest.mark.parametrize("reading", READINGS)
def test_the_engine_the_notes_name_and_the_run_used_is_an_ask_met_whatever_the_reading(engine, word, reading):
    quote = f"use {word}"
    sec = NA.apply(_section(reading, quote), f"{quote}. surface refinement level 3",
                   NA.facts_of({"purpose": "internal_cfd"}, engine))
    lines = MR.words(sec)
    assert f"From your notes: meshed with {word}, as you asked." in lines
    assert lines.count(f"From your notes: meshed with {word}, as you asked.") == 1
    assert _told_not_applied(lines) == []
    assert MR.not_applied_words(sec) == [] and not sec.get("dropped")
    row = NA.report_row(sec, engine)
    assert row is not None and row["met"] is True and row["excess"] == 0
    assert MR.line(row) == f"Meshed with {word}: met."
    met = {"id": "r1", "field": "control:surface_level", "asked": "refinement level 3 at the walls", "met": True}
    tally = _tally(sec, engine, [met])
    assert tally == "Of the 2 things you asked for: 2 met."
    assert "could not be applied" not in tally


@pytest.mark.parametrize("engine,word", ENGINES)
def test_the_engine_ask_stands_when_the_reading_said_nothing_of_it(engine, word):
    sec = NA.apply(_section("none", ""), f"Please mesh it with {word}. surface refinement level 3",
                   {"engine": engine, "topology": "internal"})
    assert f"From your notes: meshed with {word}, as you asked." in MR.words(sec)
    assert NA.report_row(sec, engine)["met"] is True


@pytest.mark.parametrize("asked,word,used,used_word", [("cfmesh", "cfMesh", "snappy", "snappyHexMesh"),
                                                       ("vmtk", "VMTK", "cfmesh", "cfMesh"),
                                                       ("snappy", "snappyHexMesh", "vmtk", "VMTK")])
@pytest.mark.parametrize("reading", READINGS)
def test_an_engine_that_did_not_make_the_mesh_says_why_and_what_did(asked, word, used, used_word, reading):
    quote = f"use {word}"
    facts = {"engine": used, "topology": "internal", "attempts": [{"engine": asked, "kind": "failed"}]}
    sec = NA.apply(_section(reading, quote), f"{quote}. surface refinement level 3", facts)
    lines = MR.words(sec)
    said = [w for w in lines if word in w]
    assert said == [f"From your notes: you asked for {word}; {word} did not make a usable mesh, so this mesh was "
                    f"made with {used_word}."]
    assert _told_not_applied(lines) == []
    row = NA.report_row(sec, used)
    assert row["met"] is False and row["excess"] == 1
    assert MR.line(row) == (f"Meshed with {word}: not fully met - {word} did not make a usable mesh, so it was made "
                            f"with {used_word}.")
    assert "could not be applied" not in _tally(sec, used)


def test_an_engine_that_cannot_make_this_kind_of_mesh_says_so():
    facts = {"engine": "snappy", "topology": "external"}
    sec = NA.apply(_section("not_applied", "use VMTK"), "use VMTK", facts)
    assert "From your notes: you asked for VMTK; VMTK cannot make an external flow mesh, so this mesh was made with " \
           "snappyHexMesh." in MR.words(sec)


def test_the_report_row_is_judged_on_the_engine_that_built_this_mesh():
    """The reading is made per attempt; the report is made on the mesh delivered - the engine that built it decides."""
    sec = NA.apply(_section("not_applied", "use cfMesh"), "use cfMesh", {"engine": ""})
    assert "From your notes: meshed with cfMesh, as you asked." in MR.words(sec)
    assert NA.report_row(sec, "cfmesh")["met"] is True
    assert NA.report_row(sec, "snappy")["met"] is False


@pytest.mark.parametrize("notes", ["VMTK failed with this part last time", "do not use VMTK", "use cfMesh or VMTK",
                                   "should I use VMTK?"])
def test_words_that_tell_of_an_engine_or_refuse_it_choose_none(notes):
    assert NA.engine_asked(notes) == ("", "")
    sec = NA.apply(_section("not_applied", notes), notes, {"engine": "vmtk", "topology": "internal"})
    assert NA.report_row(sec, "vmtk") is None
    assert sec["not_applied"] == [notes]


def test_a_sentence_that_asks_more_than_the_engine_stays_not_applied():
    quote = "use snappyHexMesh with a smooth transition into the outlet"
    sec = NA.apply(_section("not_applied", quote), quote, {"engine": "snappy", "topology": "internal"})
    assert "From your notes: meshed with snappyHexMesh, as you asked." in MR.words(sec)
    assert sec["not_applied"] == [quote]


def test_a_sentence_whose_other_words_a_row_holds_is_the_engine_ask_only():
    quote = "use snappyHexMesh with surface refinement level 3"
    sec = NA.apply(_section("not_applied", quote), quote, {"engine": "snappy", "topology": "internal"})
    assert sec["not_applied"] == []
    assert _told_not_applied(MR.words(sec)) == []


def test_the_lines_of_a_reading_merged_with_the_meshing_standard_are_corrected_too():
    sec = _section("not_applied", "use cfMesh")
    sec["lines"] = ["From your notes: refinement level 3 at the walls.",
                    'Not applied: "use cfMesh" - no mesh setting of ours matches it yet.',
                    'Not applied: "a smooth outlet" - no mesh setting of ours matches it yet.']
    sec["not_applied"] = ["use cfMesh", "a smooth outlet"]
    sec = NA.apply(sec, "use cfMesh. a smooth outlet", {"engine": "cfmesh", "topology": "internal"})
    assert MR.words(sec) == ["From your notes: refinement level 3 at the walls.",
                             "From your notes: meshed with cfMesh, as you asked.",
                             'Not applied: "a smooth outlet" - no mesh setting of ours matches it yet.']
    assert len(MR.not_applied_words(sec)) == 1


@pytest.mark.parametrize("quote,said", [
    ("water at 2 m/s", 'From your notes: "water at 2 m/s" - taken as the flow conditions you confirmed (water at '
                       '2 m/s).'),
    ("internal flow mesh", 'From your notes: "internal flow mesh" - meshed as an internal flow mesh, as you said.'),
    ("Units: mm", 'From your notes: "Units: mm" - read as the unit of the lengths in your notes.'),
])
def test_what_the_product_acted_on_is_said_so_never_not_applied(quote, said):
    facts = {"engine": "snappy", "topology": "internal", "conditions": {"fluid_words": "water", "speed_words": "2 m/s"}}
    sec = NA.apply(_section("not_applied", quote), quote, facts)
    lines = MR.words(sec)
    assert said in lines and _told_not_applied(lines) == []
    assert MR.not_applied_words(sec) == []


@pytest.mark.parametrize("quote", ["external flow mesh", "export in mm", "air at 30 m/s"])
def test_what_the_product_did_not_act_on_stays_not_applied(quote):
    facts = {"engine": "snappy", "topology": "internal", "conditions": {"fluid_words": "water", "speed_words": "2 m/s"}}
    sec = NA.apply(_section("not_applied", quote), quote, facts)
    assert MR.not_applied_words(sec) == [f'Not applied: "{quote}" - no mesh setting of ours matches it yet.']


def test_the_closing_line_counts_the_engine_as_met_and_says_the_tally_once(tmp_path):
    """5a end to end on the workspace: the reading kept, the report made on the engine that built the mesh, the remark
    recorded - one tally, the engine an ask met, nothing 'could not be applied'."""
    from meshpipeline.application import final_result as fr
    from meshpipeline.contracts.user_notes import read_remarks
    sec = NA.apply(_section("not_applied", "use snappyHexMesh", rows=()), "use snappyHexMesh",
                   {"engine": "snappy", "topology": "internal"})
    assert MR.write(tmp_path, sec)
    quality = CR.attach(tmp_path, {"cells": 1000}, "snappy")
    assert [r["field"] for r in quality["requirements"]["rows"]] == ["engine"]
    remarks = read_remarks(tmp_path)
    assert remarks == ["Of the 1 thing you asked for: 1 met. Meshed with snappyHexMesh: met."]
    base = fr.build_final_result(
        job_id="j", owner_id="u", status=fr.TerminalStatus.succeeded, engine="snappy",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="", executor_success=True,
        reviewer_verdict="PASS", failed_gate="", api_failure="", attempts=1, attempts_max=3,
        required_ready=True, delivered_types=["mesh"], optional_warnings=[])
    msg = fr.render_message(fr.with_notes_remarks(base, remarks))
    assert msg.count("Of the 1 thing you asked for") == 1
    assert "From your notes: Of the 1 thing you asked for: 1 met." in msg
    assert "could not be applied" not in msg
