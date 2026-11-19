# Responsibility: Verify NOTE-13 - the controls declared once (contracts/mesh_controls) are read by the notes' one generic output with the usual checks, applied at each plan seam by the registry's appliers or told "not on this mesher" with its reason, and written as rows by each route's report: growth and elements through the thickness on FEA solids, cells across a bore and the wall's level on snappy, the files' unit (told, not offered yet).
# Boundaries: pure over plain data; no model, no mesher.
from __future__ import annotations

import json

import pytest

from meshpipeline.contracts import mesh_controls as MC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import control_appliers as CA
from meshpipeline.engines import mesh_requirements as MR


def _read(notes: str, route: str = "fea", **fields):
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": []}
    base.update(fields)
    return MR.check(json.dumps(base), notes, route=route)


def _ctl(key, quote, value=0, choice="", where=""):
    return {"key": key, "value": value, "choice": choice, "unit": "none", "where": where, "strict": False,
            "quote": quote}


def test_the_reader_is_generated_from_the_registry():
    keys = MR.tool()["function"]["parameters"]["properties"]["controls"]["items"]["properties"]["key"]["enum"]
    assert set(keys) == {c.key for c in MC.read_by_reader()} and "growth" not in keys     # growth has its own field
    assert 'key "wall_elements"' in MR.SYSTEM and "at least 3 elements through the thickness" in MR.SYSTEM


def test_a_control_is_read_with_the_usual_checks():
    req, dropped, _na = _read("At least 3 elements through the thickness. Then 4 elements through every wall.",
                              controls=[_ctl("wall_elements", "At least 3 elements through the thickness", 3),
                                        _ctl("wall_elements", "4 elements through every wall", 4)])
    assert dropped == [] and req["controls"]["wall_elements"]["value"] == 4               # the latest wins
    assert [r["asked"] for r in req["rows"]] == ["4 elements through the thickness of every wall"]
    assert not MR.is_empty(req)
    _req, dropped, _na = _read("3 elements through the thickness", controls=[_ctl("wall_elements", "3 elements through "
                                                                                  "the thickness", 5)])
    assert "not in those words" in dropped[0]
    _req, dropped, _na = _read("3 elements through the ribs", controls=[_ctl("wall_elements", "3 elements through the "
                                                                             "ribs", 3, where="the ribs")])
    assert "whole mesh only" in dropped[0]
    req, dropped, _na = _read("Export in mm.", controls=[_ctl("file_units", "Export in mm.", choice="mm")])
    assert dropped == [] and req["controls"]["file_units"]["choice"] == "mm"


def test_fea_solid_applies_growth_and_elements_through_the_wall():
    req, _d, _na = _read("growth rate 1.2, 3 elements through the thickness",
                         sizes=[{"what": "growth", "value": 1.2, "unit": "none", "quote": "growth rate 1.2"}],
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3)])
    zones = [{"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 0.0005, "thickness_m": 0.001,
              "why": "a 1 mm wall under Face_1: 2 elements through it"},
             {"id": "H1", "kind": "fillet", "faces": ["Face_2"], "size_m": 0.0004, "thickness_m": 0.001,
              "wall_elements": 3}]
    knobs = {"zones": zones, "growth": 1.3, "gentle": False}
    got = CA.apply("fea_solid", req, knobs)
    assert knobs["growth"] == 1.2 and got["growth"]["status"] == "applied"
    assert abs(zones[0]["size_m"] - 0.001 / 3) < 1e-12 and zones[0]["wall_elements"] == 3
    assert zones[1]["size_m"] == 0.0004                                                   # a hot spot keeps its own
    row = CA.report_row("fea_solid", "wall_elements", got)
    assert row["applied"] is True and row["met"] is None and "3 elements through each of 1 wall" in row["delivered"]
    # before NOTE-13 the solid's report said "not applied - on a solid, sizes grow by our own fixed rate"
    assert CA.report_row("fea_solid", "growth", got)["delivered"].startswith("applied as a setting")


def test_the_count_through_the_walls_wins_over_a_size_range():
    """Job 4b1ee697: the saved meshing standard's 5-15 mm range (shortest edge 0.5 x the 10 mm global) held a 1 mm
    wall's elements at 5 mm, so the 5 through it the notes asked became 0.2 - the count is applied after the range."""
    from meshpipeline.engines import fea_plan as P
    req, _d, _na = _read("5 elements through the thickness",
                         controls=[_ctl("wall_elements", "5 elements through the thickness", 5)])
    wall = {"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 0.0005, "thickness_m": 0.001,
            "why": "a 1 mm wall under Face_1: 2 elements through it"}
    notch = {"id": "H1", "kind": "notch", "faces": ["Face_2"], "size_m": 0.0002, "thickness_m": 0.001,
             "radius_m": 0.0005, "spot": "H1"}
    zones = P.user_sized([wall, notch], {"Part_1": {"min_m": 0.005, "max_m": 0.015, "whole": True}})
    assert zones[0]["size_m"] == pytest.approx(0.005) and zones[0]["crossed"] == "PLAN-fea-3"   # the range alone
    knobs = {"zones": zones, "growth": 1.3, "gentle": False}
    got = CA.apply("fea_solid", req, knobs)
    w, h = knobs["zones"]
    assert w["size_m"] == pytest.approx(0.001 / 5) and w["wall_elements"] == 5 and "crossed" not in w
    assert w["count_wins"] == 5
    # the notch beside it: 5 through its wall too, and no longer marked short of its accuracy by the range
    assert h["size_m"] == pytest.approx(0.0002) and h["wall_elements"] == 5 and not h.get("below_accuracy")
    assert got["wall_elements"]["status"] == "applied"
    assert got["wall_elements"]["built"] == ("5 elements through each of 1 wall - all of them finer than the shortest "
                                             "element asked (5 mm): the count wins there")
    words = " ".join(P.user_words({"user_sizes": {"Part_1": {"min_m": 0.005, "max_m": 0.015, "whole": True}},
                                   "zones": knobs["zones"]}))
    assert "to give the 5 elements through them your notes ask for" in words
    assert "fewer than 2 elements through" not in words and "cannot be built valid" not in words, words


def test_a_real_part_takes_the_count_through_its_walls_over_a_size_range(tmp_path):
    """The plan seam itself (fea_driver.make_plan, through choose_plan): a 4 mm plate under a 6-8 mm range and
    "3 elements through the thickness" is planned at 3 through its wall, not at the range's 6 mm."""
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines.test_fea_driver import _stage

    from meshpipeline.engines import fea_driver as FD
    ws = _stage(fx.plate_with_hole(), tmp_path / "plate")
    record, surface, diag = FD.measure_staged(ws)
    req, _d, _na = _read("3 elements through the thickness",
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3)])
    record[CA.RECORD_KEY] = req
    plan = FD.choose_plan(record, surface, diag, sizes={"Part_1": {"min_m": 0.006, "max_m": 0.008}})
    walls = [z for z in plan["zones"] if z["kind"] == "wall"]
    assert walls and all(z["size_m"] == pytest.approx(z["thickness_m"] / 3, rel=1e-3) for z in walls)
    assert plan["controls"]["wall_elements"]["status"] == "applied"
    assert any(w.startswith("Walls: 3 elements through every wall, as your notes ask.") for w in plan["words"])
    assert any("to give the 3 elements through them your notes ask for" in w for w in plan["words"])


def test_the_stages_plans_are_not_reused_when_the_notes_set_a_control(monkeypatch):
    """Job 5656805e (no standard, no sizes): the stage's plan - made before the notes - was reused, so make_plan's
    seam never ran and "5 elements through the thickness" was meshed at the rules' 2."""
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan_options as PO
    monkeypatch.setattr(PO, "kept_plan", lambda kept, key, pick: {"from": "stage"} if kept else None)
    monkeypatch.setattr(FD, "_restate", lambda record, plan: plan)
    monkeypatch.setattr(FD, "choose_plan", lambda *a, **k: {"from": "run"})
    state = {"fea_plan_choice": PO.FULL}
    kw = {"levers": [], "ceiling_s": None, "ai": None, "kept": {"key": "k"}, "key": "k"}
    assert FD.size_choice(state, {}, None, 0.02, **kw)[0] == {"from": "stage"}       # nothing asked: reused
    req, _d, _na = _read("5 elements through the thickness",
                         controls=[_ctl("wall_elements", "5 elements through the thickness", 5)])
    assert FD.size_choice(state, {CA.RECORD_KEY: req}, None, 0.02, **kw)[0] == {"from": "run"}


def test_a_count_through_the_walls_is_met_when_the_mesh_measures_it():
    """Hinge e0f6a98b: 3 asked, 3.0 measured through the limiting wall, yet "0 met; 1 applied as a setting"."""
    from meshpipeline.engines import fea_requirements as FR
    req, _d, _na = _read("3 elements through the thickness",
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3)])
    applied = {"wall_elements": {"id": req["controls"]["wall_elements"]["id"], "status": "applied",
                                 "built": "3 elements through each of 4 walls"}}
    base = {"applied": True, "met": None, "delivered": "applied as a setting - 3 elements through each of 4 walls"}
    assert CA.report_row("fea_solid", "wall_elements", applied) == base
    got = FR._through_row(req, dict(base), 3.0)
    assert got["met"] is True and got["delivered"] == "3 elements through the thinnest wall"
    got = FR._through_row(req, dict(base), 2.61)
    assert got["met"] is False and got["delivered"] == "2.61 elements through the thinnest wall, not 3"
    rep = MR.finish_report([{"id": "r1", "field": "control:wall_elements", "asked": "3 elements through the thickness "
                             "of every wall", **FR._through_row(req, dict(base), 3.0)}], 10, "")
    assert MR.summary(rep, kind="solid") == "Of the 1 thing you asked for: 1 met."


def test_the_fea_ai_is_told_which_asks_the_rules_apply_and_the_parts_reader_says_nothing_of_none():
    """fea0162, round 3: "Applied your note as guidance for through-thickness resolution." and "I found only an
    instruction for 5 elements through the thickness, ..." beside "Of the 1 thing you asked for: 1 met."."""
    from meshpipeline.engines import fea_ai as FA
    from meshpipeline.engines import fea_part_sizes as PS
    req, _d, _na = _read("5 elements through the thickness, about 500k elements",
                         controls=[_ctl("wall_elements", "5 elements through the thickness", 5)],
                         counts=[{"what": "target", "value": 500000, "quote": "about 500k elements"}])
    notes = {"user_notes": [{"text": "5 elements through the thickness, about 500k elements"}]}
    record = {"faces": {}, "hot_spots": [], "thickness": {"min_mm": 1.0}}
    # the AI still reads the count itself (AI-fea-4): only what the rules apply is listed
    assert FA.rules_asks(req) == ["5 elements through the thickness of every wall"]
    told = FA.brief(record, FA.with_rules_asks(notes, req), [], diag_m=0.02, limit=1_000_000)
    assert told["notes_applied_by_rules"] == ["5 elements through the thickness of every wall"]
    assert "leave notes_remark empty when nothing else in the notes was yours to act on" in told["user_notes_fea"]
    # nothing the rules apply: the brief (and the decision cache's key) is the one it always was
    assert FA.with_rules_asks(notes, None) is notes
    assert "notes_applied_by_rules" not in FA.brief(record, notes, [], diag_m=0.02, limit=1_000_000)
    # the parts reader found no part named: its explanation is not a line of the result
    nothing = {"status": "used", "entries": [], "dropped": [], "unsure": [],
               "words": ["I found only an instruction for 5 elements through the thickness, which is an element count "
                         "rather than a part-specific element size or refinement request."]}
    assert PS.told(nothing, req) == [] and PS.words(nothing) == nothing["words"]
    assert PS.told({**nothing, "dropped": ["the bracket: no such part"]}, req)
    local = {**req, "rows": [*req["rows"], {"id": "r9", "field": "local", "asked": "finer on the bracket"}]}
    assert PS.told(nothing, local) == nothing["words"]          # its row points at these lines
    assert PS.told({"status": "failed", "note": "not read"}, req) == ["not read"]


def test_the_report_holds_the_walls_to_the_count_asked():
    """fea0162, round 3: "Elements through the thinnest wall (2 planned; ...)", limit 1.8, and "every wall at least 2
    elements thick" while the notes asked 5."""
    from meshpipeline.engines import verdict_limits as VL
    from meshpipeline.render import mesh_report as RPT
    q = {"element_order": 2, "fea": {"plan": True, "through_wall_min": 5.0, "through_wall_asked": 5}}
    row = next(r for r in VL.fea_limits(q) if r["key"] == "through_wall_min")
    assert row["label"] == "Elements through the thinnest wall (5 planned, as your notes ask; read to within 10%)"
    assert row["limit"] == 4.5
    q["fea"].pop("through_wall_asked")
    row = next(r for r in VL.fea_limits(q) if r["key"] == "through_wall_min")
    assert row["label"] == "Elements through the thinnest wall (2 planned; read to within 10%)" and row["limit"] == 1.8
    assert VL.through_wall_planned({"through_wall_asked": 3}) == (3, 2.7, ", as your notes ask")
    keys = dict(RPT._key_numbers({"surface": {"fea": {"through_wall": {"ok": True, "min": 5.0, "needed": 5}}}}))
    assert keys["Walls"] == "every wall at least 5 elements thick"
    keys = dict(RPT._key_numbers({"surface": {"fea": {"through_wall": {"ok": False, "min": 4.2, "needed": 5}}}}))
    assert keys["Walls"] == "thinnest wall 4.2 elements thick, 5 planned"
    # the plan's control carries the count asked, which the adapter hands the report
    knobs = {"zones": [], "growth": 1.3, "gentle": False}
    req, _d, _na = _read("5 elements through the thickness",
                         controls=[_ctl("wall_elements", "5 elements through the thickness", 5)])
    assert CA.asked_count({"controls": CA.apply("fea_solid", req, knobs)}) == 5
    assert CA.asked_count({}) is None


def test_a_gentler_grading_the_ladder_needed_is_kept_and_said():
    knobs = {"zones": [], "growth": 1.2, "gentle": True}
    status, built = CA.fea_growth(knobs, 1.5)
    assert status == "nearest" and knobs["growth"] == 1.2 and "gentler grading" in built


def test_a_mesher_without_the_lever_says_so_with_the_registrys_reason(tmp_path):
    req, _d, _na = _read("3 elements through the thickness. Export in mm.",
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3),
                                   _ctl("file_units", "Export in mm.", choice="mm")])
    got = CA.apply("shell", req, {})
    assert got["wall_elements"]["status"] == "not_applied" and "one element through its thickness" in \
        got["wall_elements"]["built"]
    assert CA.report_row("fea_solid", "file_units")["delivered"].startswith("not applied - every file is written in "
                                                                             "metres")
    ask = CR.Ask(req, "cfmesh")
    ask.write(tmp_path)
    assert ask.rows[req["controls"]["wall_elements"]["id"]]["built"] == "a flow mesh has cells, not elements through a wall"


def test_snappy_takes_cells_across_the_bore_and_the_walls_level():
    req, _d, _na = _read("30 cells across the pipe, surface refinement level 3", route="flow",
                         controls=[_ctl("cells_across", "30 cells across the pipe", 30),
                                   _ctl("surface_level", "surface refinement level 3", 3)])
    ask = CR.Ask(req, "snappy")
    knobs = ask.controls({"internal": True, "cells_across": 24, "surface_level": 2, "feature_level": 3})
    assert knobs["cells_across"] == 30 and knobs["surface_level"] == 3 and knobs["feature_level"] == 4
    assert ask.rows[req["controls"]["cells_across"]["id"]]["status"] == "applied"
    req, _d, _na = _read("4 cells across the pipe", route="flow", controls=[_ctl("cells_across", "4 cells across the "
                                                                                "pipe", 4)])
    ask = CR.Ask(req, "snappy")
    assert ask.controls({"internal": True, "cells_across": 24, "surface_level": 2})["cells_across"] == 8
    assert ask.rows[req["controls"]["cells_across"]["id"]]["status"] == "nearest"
