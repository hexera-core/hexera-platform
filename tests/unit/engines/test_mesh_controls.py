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
    # one place among several: still applied - to the whole mesh, the nearest there is - and said, never dropped
    req, dropped, _na = _read("3 elements through the ribs", controls=[_ctl("wall_elements", "3 elements through the "
                                                                            "ribs", 3, where="the ribs")])
    assert dropped == [] and req["controls"]["wall_elements"]["place"] == "the ribs"
    assert req["rows"][0]["asked"] == ("3 elements through the thickness of every wall (asked for the ribs: set on "
                                       "the whole mesh, the nearest there is)")
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
    sec = MR.section("used", requirements=req)
    # the AI still reads the count itself (AI-fea-4): only what the rules decide is listed
    assert FA.rules_asks(sec) == ["5 elements through the thickness of every wall"]
    told = FA.brief(record, FA.with_rules_asks(notes, sec), [], diag_m=0.02, limit=1_000_000)
    assert told["notes_decided_by_rules"] == ["5 elements through the thickness of every wall"]
    assert "Leave notes_remark empty when nothing else in the notes was yours to act on" in told["user_notes_fea"]
    assert "treated one as guidance" in told["user_notes_fea"]
    # nothing the rules decide: the brief (and the decision cache's key) is the one it always was
    assert FA.with_rules_asks(notes, None) is notes
    assert "notes_decided_by_rules" not in FA.brief(record, notes, [], diag_m=0.02, limit=1_000_000)
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


@pytest.mark.parametrize(("key", "quote", "value", "where"), [
    ("wall_elements", "4 elements across the thickness of each wall", 4, "each wall"),            # job bfdc60b1
    ("wall_elements", "I need five elements through the wall thickness everywhere", 5,
     "the wall thickness everywhere"),                                                            # job f2c6fc7d
    ("wall_elements", "4 elements through all the walls", 4, "all the walls"),
    ("wall_elements", "3 elements through the plate", 3, "the plate"),
    ("cells_across", "use 36 cells across the inlet passage", 36, "the inlet passage"),
    ("cells_across", "30 cells across the pipe", 30, "the pipe"),
    ("cells_across", "at least 20 cells across the bore", 20, "the bore"),
    ("cells_across", "25 cells across the duct", 25, "the duct"),
    ("surface_level", "surface refinement level 3 on the body", 3, "the body"),
    ("surface_level", "refine the walls to level 2", 2, "the walls"),
])
def test_a_where_that_only_restates_the_controls_subject_is_the_whole_mesh(key, quote, value, where):
    req, dropped, _na = _read(quote, route="fea" if key == "wall_elements" else "flow",
                              controls=[_ctl(key, quote, value, where=where)])
    assert dropped == [], dropped
    got = req["controls"][key]
    assert got["value"] == value and "place" not in got
    assert "asked for" not in req["rows"][0]["asked"]


@pytest.mark.parametrize(("key", "quote", "value", "where"), [
    ("wall_elements", "4 elements through the left flange", 4, "the left flange"),
    ("cells_across", "40 cells across the second branch", 40, "the second branch"),
    ("wall_elements", "3 elements through the bracket's web", 3, "the bracket's web"),
])
def test_one_place_among_several_is_applied_to_the_whole_mesh_and_said(key, quote, value, where):
    req, dropped, _na = _read(quote, route="fea" if key == "wall_elements" else "flow",
                              controls=[_ctl(key, quote, value, where=where)])
    assert dropped == [] and req["controls"][key]["place"] == where
    knobs = {"zones": [], "growth": 1.3, "gentle": False, "internal": True, "cells_across": 24, "surface_level": 2}
    got = CA.apply("fea_solid" if key == "wall_elements" else "snappy", req, knobs)[key]
    assert got["status"] == "nearest"
    assert got["built"].endswith(f"on the whole mesh, not only {where}: a setting for one place is not offered yet")


def test_the_registry_declares_each_controls_own_subject_and_the_reader_is_told():
    for key in ("wall_elements", "cells_across", "surface_level"):
        assert MC.CONTROLS[key].subject, key
    assert MC.place_of(MC.CONTROLS["wall_elements"], "every wall") == ""
    assert MC.place_of(MC.CONTROLS["cells_across"], "the outlet pipe") == ""
    assert MC.place_of(MC.CONTROLS["cells_across"], "the left branch") == "the left branch"
    assert '`where` stays "" when the words only name its own subject (wall, walls, thickness' in MR.SYSTEM
    assert '"through each wall", "across the pipe"' in MR.SYSTEM


def test_cells_across_reaches_vmtk_and_cfmesh_through_the_registry():
    """4d (VMTK, "30 cells across the pipe"): edge_length_factor 0.0667 was set outside the registry and the result
    said "could not be applied"; 4e (cfMesh): "capped at 20+ across" said over 16.8 built. One seam each now."""
    req, _d, _na = _read("30 cells across the pipe", route="flow",
                         controls=[_ctl("cells_across", "30 cells across the pipe", 30, where="the pipe")])
    assert isinstance(MC.lane_of("vmtk", "cells_across"), str) and isinstance(MC.lane_of("cfmesh", "cells_across"), str)
    ask = CR.Ask(req, "vmtk")
    spec = CR.vmtk_spec(ask, {"edge_length_factor": 0.15, "boundary_layers": 5})
    assert spec["edge_length_factor"] == pytest.approx(2 / 30, rel=1e-4)
    rid = req["controls"]["cells_across"]["id"]
    assert ask.rows[rid]["status"] == "applied" and "30 cells across every passage" in ask.rows[rid]["built"]
    CR.vmtk_cells_built(ask, 0.16)                          # the budget fit coarsened it to 12.5 across
    assert ask.rows[rid]["status"] == "nearest" and ask.rows[rid]["built"].startswith("12.5 cells across")
    # cfMesh: the wall band's cell is the passage's width over the count, and the octree's cell is said
    ask = CR.Ask(req, "cfmesh")
    knobs = ask.controls({"internal": True, "passage_d": 0.29, "wall_cell": 0.0172})
    assert knobs["wall_cell"] == pytest.approx(0.29 / 30)
    CR.cfmesh_cells_built(ask, req["controls"]["cells_across"], passage_d=0.29, wall_cell=0.29 / 32)
    assert ask.rows[rid]["status"] == "applied" and ask.rows[rid]["built"].startswith("32 cells across the passage")
    CR.cfmesh_cells_built(ask, req["controls"]["cells_across"], passage_d=0.29, wall_cell=0.0172)
    assert ask.rows[rid]["status"] == "nearest" and ask.rows[rid]["built"].startswith("16.9 cells across")
    # no measured passage: told, never set blind
    ask = CR.Ask(req, "cfmesh")
    ask.controls({"internal": False, "passage_d": 0.0, "wall_cell": 0.0172})
    assert ask.rows[rid]["status"] == "not_applied"


def test_every_ask_the_rules_decide_is_shown_to_the_ai_steps(tmp_path):
    """1b/1c/3c: the AI remark claimed asks the checks had set aside ("limiting element growth to no more than 25%"
    while the plan kept 1.3); the CFD builder's and planner's remarks claimed cells across and y+ the result told
    otherwise. Every ask the reading holds - applied, nearest, set aside, no setting - is listed for them."""
    from meshpipeline.agents.builder.attempt import user_notes_note
    req, dropped, na = _read("growth 25 and smooth transitions please",
                             sizes=[{"what": "growth", "value": 9, "unit": "none", "quote": "growth 25"}],
                             other=[{"quote": "smooth transitions please", "kind": "mesh"}])
    sec = MR.section("used", requirements=req, dropped=dropped, not_applied=na)
    asks = MR.decided_asks(sec)
    assert any("growth 25" in a for a in asks) and any("smooth transitions please" in a for a in asks)
    block = MR.decided_block(sec)
    assert block.startswith("THE ASKS THE RULES DECIDE") and "never say you applied, capped, kept" in block
    MR.write(tmp_path, sec)
    note = user_notes_note(tmp_path, {"user_notes": [{"id": "n1", "text": "growth 25 and smooth transitions please",
                                                      "source": "console"}]})
    assert "THE ASKS THE RULES DECIDE" in note and "growth 25" in note
    assert MR.decided_block(MR.section("none")) == ""


def test_a_growth_written_as_a_percent_is_its_ratio():
    """3c: "elements should grow by no more than 25% from one to the next" was read as 25 and refused."""
    q = "elements should grow by no more than 25% from one to the next"
    for value in (25, 1.25):                                   # the model's raw number, or its ratio
        req, dropped, _na = _read(q, sizes=[{"what": "growth", "value": value, "unit": "none", "quote": q}])
        assert dropped == [] and req["sizes"]["growth"] == pytest.approx(1.25), (value, dropped)
    req, dropped, _na = _read("growth 20 percent", sizes=[{"what": "growth", "value": 20, "unit": "none",
                                                           "quote": "growth 20 percent"}])
    assert req["sizes"]["growth"] == pytest.approx(1.2)
    # no percent written: a 25 is still no growth anyone means
    _req, dropped, _na = _read("growth 25", sizes=[{"what": "growth", "value": 25, "unit": "none",
                                                    "quote": "growth 25"}])
    assert "a growth of 25 is not between 1 and 3" in dropped[0]
    # the layers' growth the same
    req, dropped, _na = _read("each layer 20% thicker", route="flow",
                              layers=[{"what": "growth", "value": 20, "unit": "none", "quote": "each layer 20% thicker"}])
    assert dropped == [] and req["layers"]["growth"] == pytest.approx(1.2)
    assert "A percent is the ratio 1 + percent / 100" in MR.SYSTEM


def test_the_notes_labels_are_never_told_back_as_the_users_words():
    """E1: 'Not a mesh setting: "[n1] (typed in the notes box)"'."""
    notes = "[n1] (typed in the notes box)\n<<<\n4 elements through every wall\n>>>"
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "controls": [_ctl("wall_elements", "4 elements through every wall", 4)],
            "other": [{"quote": "[n1] (typed in the notes box)", "kind": "not_mesh"}]}
    req, dropped, na = MR.check(json.dumps(base), notes, route="fea")
    assert not any("typed in the notes box" in str(x) for x in [*(req.get("not_mesh") or []), *na, *dropped])
    assert req["controls"]["wall_elements"]["value"] == 4


def test_the_parts_reader_is_shown_the_rounded_edges_as_fillets():
    """J: fea0254's 17 rounds were listed nowhere ("No fillet feature is listed") so "finer at the fillets" sized
    nothing - and was still told "applied as a setting"."""
    from types import SimpleNamespace

    from meshpipeline.cad import part_features as PF
    surface = SimpleNamespace(names=[], label=[], tris=[], xyz=[])
    faces = {f"Face_{i}": {"kind": "plane", "thickness_wall_mm": 4.0, "area_mm2": 10.0} for i in range(1, 6)}
    faces.update({"Face_6": {"kind": "cylinder", "radius_mm": 1.5, "concave": True, "thickness_wall_mm": 4.0},
                  "Face_7": {"kind": "cylinder", "radius_mm": 1.5, "concave": True, "thickness_wall_mm": 4.0},
                  "Face_8": {"kind": "torus", "radius_mm": 0.8},
                  "Face_9": {"kind": "cylinder", "radius_mm": 2.0, "concave": False, "thickness_wall_mm": 4.0},
                  "Face_10": {"kind": "cylinder", "radius_mm": 12.0, "concave": False, "thickness_wall_mm": 4.0},
                  # fea0254's own rounds: B-spline faces of radius 0.141 mm (16 of them on the real record)
                  "Face_11": {"kind": "bspline", "radius_mm": 0.141006},
                  "Face_12": {"kind": "bspline", "radius_mm": 0.141006}})
    rows, faces_of = PF.features({"faces": faces, "hot_spots": []}, surface)
    by_name = {r["name"]: faces_of[r["id"]] for r in rows}
    assert by_name["2 rounded edges (fillets), radius 1.5 mm"] == ["Face_6", "Face_7"]
    assert by_name["a rounded edge (fillet), radius 0.8 mm"] == ["Face_8"]
    assert by_name["2 rounded edges (fillets), radius 0.141 mm"] == ["Face_11", "Face_12"]
    # a convex cylinder stays a boss or shaft: the record cannot tell a rounded outer edge from a shaft
    assert by_name["a boss or shaft, 4 mm across"] == ["Face_9"]
    assert by_name["a boss or shaft, 24 mm across"] == ["Face_10"]
    assert all(r["kind"] == "fillets" for r in rows if "rounded edge" in r["name"])


def test_a_place_the_parts_reader_tied_to_nothing_is_not_applied():
    """J: a place ask with no zone placed is "not applied - no such place found", never "applied as a setting"."""
    from meshpipeline.engines import fea_requirements as FR
    req, _d, _na = _read("make the mesh finer at the fillets",
                         local=[{"where": "the fillets", "what": "finer", "value": 0, "unit": "none",
                                 "quote": "make the mesh finer at the fillets"}])
    row = next(r for r in req["rows"] if r["field"] == "local")
    tied = {"status": "used", "entries": [{"quote": "make the mesh finer at the fillets", "parts": ["Feature_3"]}]}
    assert FR._placed(row, req, tied) is True
    assert FR._placed(row, req, {"status": "used", "entries": []}) is False
    assert FR._placed(row, req, {"status": "used", "entries": [{"quote": "make the mesh finer at the fillets",
                                                                "parts": []}]}) is False


def test_a_route_answer_is_no_note_and_a_switch_does_not_turn_layers_off():
    """L1: "FEA shell mesh", the answer to "What are you meshing this for?", was kept as a note; the engine switch's
    summary said "wall layers: on -> none" for VMTK's own switch going with VMTK."""
    from meshpipeline.agents.intake import approval as AP
    from meshpipeline.agents.intake import flow as F
    from meshpipeline.contracts import user_notes as UN
    msgs = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
            {"role": "user", "content": "FEA shell mesh"},
            {"role": "assistant", "content": "Here's the plan. Add any notes, then press Proceed."},
            {"role": "user", "content": "use 3 mm elements on the flanges"}]
    notes, _un = UN.capture(msgs, ["FEA shell mesh", "use 3 mm elements on the flanges"], consumer="intake",
                            route_question=F.route_question)
    assert [n["text"] for n in notes] == ["use 3 mm elements on the flanges"]
    before = {"mesh_engine": "vmtk", "engine_params": {"wall_layers": "on"}}
    after = {"mesh_engine": "snappy", "engine_params": {}}
    assert AP.changes_between(before, after) == ["engine: vmtk -> snappy"]
    # on the same engine a setting turned off is still said
    assert AP.changes_between(before, {"mesh_engine": "vmtk", "engine_params": {"wall_layers": "off"}}) == [
        "wall layers: on -> off"]


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
