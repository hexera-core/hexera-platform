# Responsibility: Verify NOTE-12 - every ask in the user's notes is told back: an ask that is no mesh setting (the files' format, units and numbering, a time or cost limit, the user's data, a solver setting) is told with what to do instead, and the floors catch what the model left in no field, around the mesh as well as about it.
# Boundaries: pure over plain data; no model is called.
from __future__ import annotations

import json

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return json.dumps(base)


def _sec(notes: str, route: str = "fea", **fields) -> dict:
    req, dropped, na = MR.check(_answer(**fields), notes, route=route)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na)


AROUND = ("Export to Fluent in millimetres. Nastran IDs starting at 100001. Delete my CAD file after the run. "
          "It must finish within 1 hour. Bracket is steel, fixed at the bolt holes.")


def test_asks_that_are_no_mesh_setting_are_told_with_what_to_do_instead():
    quotes = ["Export to Fluent in millimetres", "Nastran IDs starting at 100001", "Delete my CAD file after the run",
              "It must finish within 1 hour", "Bracket is steel, fixed at the bolt holes"]
    sec = _sec(AROUND, other=[{"quote": q, "kind": "not_mesh"} for q in quotes])
    said = [w for w in MR.words(sec) if w.startswith(MR.NOT_MESH_LEAD)]
    assert len(said) == 5
    assert "every file we write is in metres" in said[0]
    assert "node and element numbers start at 1" in said[1]
    assert "deleting one file on request is not offered yet" in said[2]
    assert "a time limit is not a setting yet" in said[3]
    assert "a setting of your solver" in said[4]
    # they stay with the result, and the reading is kept for the steps that mesh in a process of their own
    assert MR.told_words(sec) == said and MR.not_applied_words(sec) == []


def test_the_floors_catch_what_the_model_left_in_no_field():
    # before NOTE-12 neither line was told: "cells" was no mesh word, and nothing around the mesh had a floor
    sec = _sec("Keep the wake cells nice and uniform.\nExport CGNS.", route="flow")
    words = MR.words(sec)
    assert 'Not applied: "Keep the wake cells nice and uniform" - no mesh setting of ours matches it yet.' in words
    assert any(w.startswith('Not a mesh setting: "Export CGNS"') and "we do not write that format yet" in w
               for w in words)


def test_what_was_read_or_asks_nothing_is_not_told_again():
    notes = "Air at 30 m/s, y+ around 1.\nUnits: mm\nThe rest looks good."
    sec = _sec(notes, route="flow", stated_unit="mm",
               layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ around 1"}],
               physics=[{"what": "velocity", "value": 30, "unit": "m/s", "quote": "Air at 30 m/s"}],
               other=[{"quote": "Air at 30 m/s", "kind": "not_mesh"}, {"quote": "Units: mm", "kind": "not_mesh"},
                      {"quote": "The rest looks good.", "kind": "not_mesh"}])
    assert MR.told_words(sec) == []


def test_a_reading_with_only_such_asks_is_kept_and_said(tmp_path):
    sec = _sec("Write the deck in mm.", other=[{"quote": "Write the deck in mm.", "kind": "not_mesh"}])
    assert MR.requirements_of(sec) is None and MR.write(tmp_path, sec)
    assert MR.told_words(MR.read(tmp_path))[0].startswith('Not a mesh setting: "Write the deck in mm."')
    assert any(d["rule"] == MR.RULE_TOLD for d in MR.decisions(sec))


def test_the_notes_lines_are_said_beside_a_team_standard():
    settings = _sec("Global size 5 mm.", sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "Global size 5 mm"}])
    notes = _sec("Export to Abaqus.", other=[{"quote": "Export to Abaqus.", "kind": "not_mesh"}])
    merged = MS.merge(settings, notes, route="fea")
    assert any(w.startswith('Not a mesh setting: "Export to Abaqus."') and "pick the one you want at download" in w
               for w in MR.words(merged))
