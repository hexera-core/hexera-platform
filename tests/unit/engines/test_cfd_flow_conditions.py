# Responsibility: Verify CFDREQ-8 - the flow conditions the user confirmed (the intake's conditions step: fluid, speed, wall treatment, typed or clicked) reach the mesh: the plan's one sentence is read back at the run, its y+ sizes the first layer on every engine that sizes one, the user's own notes win, and an engine with no layers says so.
# Boundaries: pure over plain data and temporary workspaces; no model, no mesher is run.
from __future__ import annotations

import asyncio
import json
import math

import pytest
from tests.unit.engines.test_cfd_requirements import _read, _snappy, re_num

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

EXT = ("External flow around the car, confirmed by the user on the geometry stage. "
       + FC.sentence("air at 20 °C", "10 m/s", "functions", internal=False) + " The flow travels along +x.")
INT_RESOLVED = ("Internal flow through the elbow, confirmed by the user on the geometry stage. "
                + FC.sentence("water at 20 °C", "1 m/s", "resolved", internal=True) + " Openings: inlet (inlet).")


def test_the_sentence_is_read_back_as_written():
    got = FC.read(EXT)
    assert got["fluid"] == "air" and got["velocity_ms"] == 10.0 and got["wall"] == "functions"
    assert got["y_plus"] == 50.0 and got["nu_m2s"] == FC.NU["air"] and got["why"] == ""
    got = FC.read(INT_RESOLVED)
    assert got["fluid"] == "water" and got["velocity_ms"] == 1.0 and got["y_plus"] == 1.0
    # the plan's words say what the first layer does
    assert "sized for y⁺ ≈ 50, inside 30–300" in EXT and "sized for y⁺ ≈ 1" in INT_RESOLVED
    # a plan the model wrote in its own words has no such sentence: nothing is read
    assert FC.read("External aero over a car at 30 m/s with wall functions.") is None


def test_speeds_are_turned_into_metres_per_second_or_the_reason_is_said():
    assert math.isclose(FC.speed_ms("72 km/h", "air")[0], 20.0)
    assert math.isclose(FC.speed_ms("Mach 0.5", "air")[0], 0.5 * FC.SOUND_AIR)
    assert FC.speed_ms("Mach 0.5", "water")[0] is None
    inlet = [{"name": "in", "type": "inlet", "diameter_mm": 50}, {"name": "out", "type": "outlet"}]
    v, why = FC.speed_ms("6 l/s", "water", inlet)
    assert why == "" and math.isclose(v, 6e-3 / (math.pi * 0.05 ** 2 / 4))
    v, why = FC.speed_ms("6 l/s", "water", [])
    assert v is None and "inlet" in why
    # a fluid we have no viscosity for: the plan says the first layer keeps the mesher's sizing, and why
    said = FC.sentence("oil", "3 m/s", "functions", internal=True)
    assert "keeps the mesher's own sizing, because we have no viscosity for oil" in said
    assert FC.read(said)["why"] == "we have no viscosity for oil"


def test_no_conditions_sentence_leaves_the_reading_untouched():
    sec = MR.section("none")
    assert CR.with_conditions(sec, {"request_txt": "Structural FEA of the bracket."}) is sec
    assert CR.with_conditions(sec, {}) is sec


def test_the_conditions_alone_ask_for_the_first_layer(tmp_path):
    sec = CR.with_conditions(MR.section("none"), {"request_txt": EXT})
    req = MR.requirements_of(sec)
    assert req is not None and req["layers"]["y_plus"] == 50.0
    assert req["physics"]["velocity_ms"] == 10.0 and req["physics"]["fluid"] == "air"
    row = req["rows"][0]
    assert row["field"] == "layers:y_plus" and row["from"] == CR.CONDITIONS_FROM
    assert MR.words(sec)[0].startswith("From your flow conditions: y+ of about 50 at the walls")
    assert "(from your flow conditions)" in MR.line({**row, "met": None, "delivered": "x"})


def test_snappy_sizes_the_first_layer_from_the_conditions(tmp_path):
    sec = CR.with_conditions(MR.section("none"), {"request_txt": INT_RESOLVED})
    ask = CR.Ask(MR.requirements_of(sec), "snappy")
    ask.length_m, ask.length_from = 0.05, "the bore"
    text, _s = _snappy(tmp_path / "c", ask)
    h = re_num(text, "firstLayerThickness")
    expected, missing, _how = CR.y_plus_height(1.0, {"velocity_ms": 1.0, "fluid": "water"}, length_m=0.05)
    assert missing == [] and math.isclose(h, expected, rel_tol=1e-4)
    assert ask.rows["c1"]["status"] == "applied" and "first layer" in ask.rows["c1"]["built"]


def test_cfmesh_and_netgen_take_it_and_the_engines_without_layers_say_so():
    req = MR.requirements_of(CR.with_conditions(MR.section("none"), {"request_txt": EXT}))
    a = CR.Ask(req, "cfmesh")
    out = CR.cfmesh_strategy(a, {"n_layers": 3}, length_m=4.0)
    assert out["first_layer_thickness"] > 0 and a.rows["c1"]["status"] == "applied"
    a = CR.Ask(req, "netgen")
    out = CR.netgen_inputs(a, layers=3, port_diameters=[0.05], soft_budget=1_000_000, cell_limit=8_000_000)
    assert out["first_layer"] > 0 and a.rows["c1"]["status"] == "applied"
    for engine, fn in (("gmsh", lambda x: CR.gmsh_sizes(x, 0.01, external=True)),
                       ("snapgrid", lambda x: CR.snapgrid_plan(x, {"max_cells": 1_000_000})),
                       ("vmtk", lambda x: CR.vmtk_spec(x, {"boundary_layers": 5}))):
        a = CR.Ask(req, engine)
        fn(a)
        assert a.rows["c1"]["status"] == "not_applied", engine


def test_the_users_own_notes_win_over_the_conditions():
    req, _d, _na = _read("y+ around 5", layers=[{"what": "y_plus", "value": 5, "unit": "none", "quote": "y+ around 5"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": EXT})
    got = MR.requirements_of(sec)
    assert got["layers"]["y_plus"] == 5 and [r["field"] for r in got["rows"]] == ["layers:y_plus"]
    assert "the y+ you gave (5) is used" in MR.words(sec)[0]
    req, _d, _na = _read("first layer 0.1 mm", layers=[{"what": "first_height", "value": 0.1, "unit": "mm",
                                                        "quote": "first layer 0.1 mm"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": EXT})
    assert MR.requirements_of(sec)["layers"]["y_plus"] is None


def test_a_y_plus_typed_as_the_conditions_answer_is_sized_at_the_conditions_speed():
    """Job 63b0fd25: "y+ of 1 at the wall" typed as the answer to the conditions question was read as a note too; the
    note's y+ won, but with no speed or fluid of its own it was never turned into a height ("1 could not be applied")
    and the layers kept our relative sizing. The conditions' water at 1 m/s fills what the note left."""
    req, _d, _na = _read("y+ of 1 at the wall",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 at the wall"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": INT_RESOLVED})
    got = MR.requirements_of(sec)
    assert got["physics"]["velocity_ms"] == 1.0 and got["physics"]["fluid"] == "water"
    assert [r["field"] for r in got["rows"]] == ["layers:y_plus"]          # one ask, the user's own
    ask = CR.Ask(got, "snappy")
    h = ask.first_layer(length_m=1.66, length_from="the bore")
    assert h is not None and 1e-5 < h < 5e-5
    assert ask.rows[ask.rid("layers:y_plus")]["status"] == "applied"
    # a speed of the note's own is kept
    req, _d, _na = _read("y+ of 1 at the wall, 3 m/s",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 at the wall"}],
                         physics=[{"what": "velocity", "value": 3, "unit": "m/s", "quote": "3 m/s"}])
    got = MR.requirements_of(CR.with_conditions(MR.section("used", requirements=req), {"request_txt": INT_RESOLVED}))
    assert got["physics"]["velocity_ms"] == 3.0 and got["physics"]["fluid"] == "water"


def test_the_first_layer_is_checked_on_the_built_mesh(tmp_path):
    """Job 63b0fd25 (round 3): the first layer written exactly as asked and 100% of the wall covered, yet "Of the 1
    thing you asked for: 0 met; 1 applied as a setting"."""
    req, _d, _na = _read("y+ of 1 at the wall",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 at the wall"}])
    got = MR.requirements_of(CR.with_conditions(MR.section("used", requirements=req), {"request_txt": INT_RESOLVED}))
    rid = got["rows"][0]["id"]
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    MR.write(ws, MR.section("used", requirements=got))
    (ws / CR.PLAN_FILE).write_text(json.dumps({"engine": "snappy", "rows": {rid: {
        "status": "applied", "built": "first layer 0.02243 mm high (...); 24 layers (not our usual 5)",
        "first_m": 2.243e-05}}}))
    shm = ws / "system" / "snappyHexMeshDict"
    shm.write_text("addLayersControls { relativeSizes false; firstLayerThickness 2.24346e-05; }")
    rep = CR.report(ws, {"cells": 900_000, "layer_coverage_pct": 100.0}, "snappy")
    row = rep["rows"][0]
    assert row["met"] is True and row["measured"] == 2.243e-05
    assert row["delivered"] == "first layer 0.02243 mm high, as asked; layers on 100% of the wall"
    assert CR.summary(rep) == "Of the 1 thing you asked for: 1 met."
    # the layers did not grow: not met, with the numbers (job f4019933's 0%)
    row = CR.report(ws, {"cells": 900_000, "layer_coverage_pct": 0.0}, "snappy")["rows"][0]
    assert row["met"] is False and row["delivered"].startswith("first layer 0.02243 mm high, as asked; layers on 0% "
                                                                "of the wall - the rest of the wall has none")
    # a different height written: not met, both numbers said
    shm.write_text("addLayersControls { relativeSizes false; firstLayerThickness 5e-05; }")
    row = CR.report(ws, {"cells": 900_000, "layer_coverage_pct": 100.0}, "snappy")["rows"][0]
    assert row["met"] is False and row["delivered"].startswith("first layer 0.05 mm high, not 0.02243 mm")
    # nothing measured: the row stays a setting, as before
    row = CR.report(ws, {"cells": 900_000}, "snappy")["rows"][0]
    assert row["met"] is None


def test_a_summary_redone_in_the_models_words_keeps_the_conditions():
    """E3, job 9a7fcd14: a "Use snappyHexMesh" note had the model submit a new summary in its own words, the conditions
    sentence was gone, and the run sized the layers as if the conditions were never answered."""
    from meshpipeline.agents.intake import flow as F
    msgs = [
        {"role": "user", "content": "internal CFD"},
        {"role": "assistant", "content": "GEOMETRY CHECK (confirmed by the user): the file is the part's wall, hollow "
                                         "inside for the fluid (tapered pipe diffuser), input_kind body-surface; the "
                                         "fluid flows through it. Openings: inlet (inlet), 149 mm across at (0, 0, 0) "
                                         "mm; outlet (outlet), 221 mm across at (693, 0, 124) mm."},
        {"role": "assistant", "content": F.conditions_question([])},
        {"role": "user", "content": "OK"},
        {"role": "assistant", "content": "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."},
        {"role": "user", "content": "Use snappyHexMesh"},
    ]
    patches = [{"name": "inlet", "type": "inlet", "diameter_mm": 149}, {"name": "outlet", "type": "outlet"}]
    args = {"request_txt": "Internal flow through the tapered pipe diffuser, meshed with snappyHexMesh.",
            "patches": patches}
    got = F.with_conditions_sentence(msgs, args)
    c = FC.read(got["request_txt"], patches)
    assert c is not None and c["fluid"] == "water" and c["velocity_ms"] == 1.0 and c["wall"] == "functions"
    assert got["request_txt"].startswith("Internal flow through the tapered pipe diffuser, meshed with snappyHexMesh.")
    # a summary that carries it already, or a run with no conditions step: left as it is
    assert F.with_conditions_sentence(msgs, got) is got
    assert F.with_conditions_sentence(msgs[:2], args) is args


def test_the_cfd_mesh_report_lists_the_users_asks():
    """E2: the CFD mesh report listed only the reviewer's criteria, never "You asked" rows."""
    from meshpipeline.render import mesh_report as RPT
    payload = {"quality": {"criteria": []}, "surface": {"quality": {"cells": 100}},
               "asked": {"rows": [
                   {"asked": "y+ of about 1 at the walls", "delivered": "first layer 0.02243 mm high, as asked; layers "
                    "on 100% of the wall", "met": True, "applied": True, "from": "flow conditions"},
                   {"asked": "30 cells across the passage", "delivered": "applied as a setting - 30 cells across",
                    "met": None, "applied": True},
                   {"asked": "solver files in mm", "delivered": "not applied - not offered yet", "met": None,
                    "applied": False}]}}
    html = RPT._cfd_limits(payload)
    assert "You asked: Y+ of about 1 at the walls (from your flow conditions)" in html
    assert "first layer 0.02243 mm high, as asked; layers on 100% of the wall" in html
    assert html.count('class="ok">Met<') == 1 and "Set as asked" in html and "Not applied" in html


ACCEPTED_FUNCTIONS = ("Internal flow through the diffuser, confirmed by the user on the geometry stage. "
                      + FC.sentence("water at 20 °C", "1 m/s", "functions", internal=True, wall_said=False)
                      + f" {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment.")


@pytest.mark.parametrize(("job", "wall_cell_mm", "y50_first_mm"), [
    ("07fd516d", 1.716, 1.348),       # no-notes external rotor: 5.7% of the wall had layers
    ("8eec5b7b", 1.716, 1.348),       # the same file with a note the checks set aside: 5.59%
    ("c0b45ad1", 2.352, 0.989),       # diffuser plenum: 4.18%
])
def test_proposed_wall_functions_only_accepted_keep_the_meshers_own_layers(job, wall_cell_mm, y50_first_mm):
    """F: pressing OK on the proposed conditions made an absolute y+ 50 first layer about as thick as the wall cells;
    snappyHexMesh grew it on 4-6% of the wall. Accepted, it is no ask: the mesher's own relative layers (5 layers, the
    last 0.3 of the cell, growing 1.2) stay, as on main - a first layer 0.145 of the wall cell, which it grows on the
    whole wall."""
    c = FC.read(ACCEPTED_FUNCTIONS)
    assert c["wall"] == "functions" and c["wall_said"] is False
    assert FC.PROPOSED_WORDS in ACCEPTED_FUNCTIONS and "sized for y⁺ ≈ 50" not in ACCEPTED_FUNCTIONS
    none = MR.section("none")
    assert CR.with_conditions(none, {"request_txt": ACCEPTED_FUNCTIONS}) is none        # no Ask: main's own case
    first = 0.3 * wall_cell_mm / 1.2 ** 4
    assert first / wall_cell_mm == pytest.approx(0.1447, abs=1e-3) and first < y50_first_mm
    # a y+ the user typed is still theirs, sized at the conditions' speed (CFDREQ-8), and made one the mesher keeps
    req, _d, _na = _read("y+ of 1 at the wall",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 at the wall"}])
    got = MR.requirements_of(CR.with_conditions(MR.section("used", requirements=req),
                                                {"request_txt": ACCEPTED_FUNCTIONS}))
    assert got["layers"]["y_plus"] == 1.0 and got["physics"]["velocity_ms"] == 1.0
    # a wall treatment the user chose is an ask, as before
    assert FC.read(INT_RESOLVED)["wall_said"] is True
    assert MR.requirements_of(CR.with_conditions(MR.section("none"), {"request_txt": INT_RESOLVED}))["layers"][
        "y_plus"] == 1.0


def test_an_attempt_whose_only_ask_was_set_aside_is_never_silent(tmp_path, monkeypatch):
    """G: 5b/5c had no tally and no "could not be applied" line - the report returned None with no row."""
    from meshpipeline.contracts import user_notes as UN
    said: list = []
    monkeypatch.setattr(UN, "record_remark", lambda ws, line, by="": said.append(line))
    sec = MR.section("used", requirements=MR.empty(),
                     dropped=['"refine the walls to level 2": this setting is for the whole mesh only: not applied'])
    MR.write(tmp_path, sec)
    out = CR.attach(tmp_path, {"cells": 1000}, "snappy")
    row = out["requirements"]["rows"][0]
    assert row["asked"] == "refine the walls to level 2" and row["applied"] is False
    assert row["delivered"] == "not applied - this setting is for the whole mesh only"
    assert said and said[-1].startswith("Of the 1 thing you asked for: 0 met")


def test_the_walls_level_is_checked_on_the_dictionary(tmp_path):
    """H: "wall refinement level 4 please" - built, or the nearest told with the levels, by the rules."""
    req, _d, _na = _read("wall refinement level 4 please",
                         controls=[{"key": "surface_level", "value": 4, "choice": "", "unit": "none", "where": "",
                                    "strict": False, "quote": "wall refinement level 4 please"}])
    rid = req["controls"]["surface_level"]["id"]
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    MR.write(ws, MR.section("used", requirements=req))
    (ws / CR.PLAN_FILE).write_text(json.dumps({"engine": "snappy", "rows": {rid: {
        "status": "applied", "built": "level 4 at the walls: the background cell is 16 times the wall cell"}}}))
    (ws / "system" / "blockMeshDict").write_text(
        "vertices ((0 0 0)(1 0 0)(1 1 0)(0 1 0)(0 0 1)(1 0 1)(1 1 1)(0 1 1));\n"
        "blocks (hex (0 1 2 3 4 5 6 7) (20 20 20) simpleGrading (1 1 1));")
    shm = ws / "system" / "snappyHexMeshDict"
    shm.write_text("refinementSurfaces { wall { level (4 4); patchInfo { type wall; } } }")
    row = CR.report(ws, {"cells": 1000}, "snappy")["rows"][0]
    assert row["met"] is True and row["delivered"] == "level 4 at the walls (cells of 3.125 mm at the walls)"
    shm.write_text("refinementSurfaces { wall { level (3 3); patchInfo { type wall; } } }")
    row = CR.report(ws, {"cells": 1000}, "snappy")["rows"][0]
    assert row["met"] is False and row["delivered"].startswith("level 3 at the walls (cells of 6.25 mm at the walls), "
                                                                "not 4")


def test_an_engine_that_can_set_the_ask_is_chosen_and_said():
    """ENG-asks: a y+ 1 on an internal flow whose ladder starts with VMTK (which has no absolute first layer) goes to
    snappyHexMesh, said in one line; no ask - the engine as it was; an ask VMTK can set keeps VMTK."""
    req, _d, _na = _read("y+ of 1 at the wall",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ of 1 at the wall"}])
    sec = MR.section("used", requirements=req)
    ladder = ["vmtk", "snappy", "cfmesh", "netgen"]
    engine, why = CR.engine_that_can(sec, "vmtk", ladder)
    assert engine == "snappy" and why.startswith("Meshed with ") and "because it can set the first layer for the " \
                                                                         "y+ you asked for" in why
    assert CR.engine_that_can(None, "vmtk", ladder) == ("vmtk", "")                  # no notes: unchanged
    assert CR.engine_that_can(MR.section("none"), "vmtk", ladder) == ("vmtk", "")
    # a resolved wall the user chose in the conditions is the same ask
    assert CR.engine_that_can(None, "vmtk", ladder, conditions_y_plus=True)[0] == "snappy"
    # a count of layers VMTK sets itself: VMTK stays
    req, _d, _na = _read("8 prism layers", layers=[{"what": "count", "value": 8, "unit": "none",
                                                    "quote": "8 prism layers"}])
    assert CR.engine_that_can(MR.section("used", requirements=req), "vmtk", ladder) == ("vmtk", "")
    # no engine of the ladder can: the engine as it was (its row says not applied, as before)
    assert CR.engine_that_can(sec, "vmtk", ["vmtk", "gmsh"]) == ("vmtk", "")
    # the real internal ladder puts cfMesh before snappyHexMesh: a layer height goes to the engine that delivers it
    # over the whole wall (s_duct_004 y+ 1: cfMesh 78% of the wall, snappy 100%; m483 round 9, 2e), never one the
    # ladder lacks
    assert CR.engine_that_can(sec, "vmtk", ["vmtk", "cfmesh", "snappy"])[0] == "snappy"
    assert CR.engine_that_can(sec, "vmtk", ["vmtk", "cfmesh"])[0] == "cfmesh"


def test_the_run_reads_the_conditions_with_no_notes(tmp_path):
    # before CFDREQ-8 a run with no notes wrote nothing here: the conditions step's answer reached no engine
    asyncio.run(CR.read_for_attempt(tmp_path, {"request_txt": INT_RESOLVED, "purpose": "internal_cfd"}))
    req = CR.requirements_for(tmp_path)
    assert req is not None and req["layers"]["y_plus"] == 1.0 and req["physics"]["velocity_ms"] == 1.0
