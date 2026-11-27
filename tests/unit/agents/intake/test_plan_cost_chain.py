# Responsibility: Verify NOTE-26 on the C5 knuckle's conversation (26 Nov), replayed on a test part: each reply's
#       "before" is the plan as it stood just before that message, so the costs chain; the costed count is the run's
#       own first plan for the same requirements (the notes read with the team's saved standard, a size on a picked
#       face, a place asked finer); the saved standard's flow lines are listed once.
# Boundaries: a filleted bracket with two holes built in the test (tests/fea_fixtures.py), measured and planned
#       in-process; the model is a fake router that answers the mesh-requirements question only (the standard's
#       reading is the one kept at save time); one real solid run of the same part (gmsh) for the parity test. No live
#       model, no store: the check and the stage's answers are faked.
from __future__ import annotations

import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402
from tests.unit.engines.test_fea_driver import _stage  # noqa: E402
from tests.unit.engines.test_fea_requirements import _drive, _Router  # noqa: E402

from meshpipeline.agents.intake import plan_talk as PT  # noqa: E402
from meshpipeline.application import part_matches as PM  # noqa: E402
from meshpipeline.application import plan_cost as PC  # noqa: E402
from meshpipeline.cad import face_map as FM  # noqa: E402
from meshpipeline.contracts import model_inference as llm  # noqa: E402
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan_options as PO  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402
from meshpipeline.engines import meshing_standard as MS  # noqa: E402

GLOBAL = "3mm global cell size"
FACE = "2 mm cell size on this selected face"
HOLES = "make the mesh finer in/around the holes"
#: the team's saved settings on the knuckle's run, with two flow lines that do not apply to a solid
STD = ("Quadratic elements.\nMax element size 6 mm.\nMin element size 0.5 mm.\nGrowth 1.3.\nAspect ratio under 5.\n"
       "Jacobian above 0.6.\n3 boundary layers, growth 1.2.\ny+ around 30.")


def _lim(measure, direction, target, quote):
    return {"measure": measure, "shape": "any", "direction": direction, "target": target, "allowed_percent": 0,
            "outer": 0, "has_outer": False, "quote": quote}


def _standard() -> dict:
    """The standard as a run is dispatched with it, its reading kept at save time (no model asked for it)."""
    args = {"stated_unit": "mm", "other": [],
            "element_kind": [{"what": "quadratic", "value": 0, "quote": "Quadratic elements"}],
            "sizes": [{"what": "max_edge", "value": 6, "unit": "mm", "quote": "Max element size 6 mm"},
                      {"what": "min_edge", "value": 0.5, "unit": "mm", "quote": "Min element size 0.5 mm"},
                      {"what": "growth", "value": 1.3, "unit": "none", "quote": "Growth 1.3"}],
            "limits": [_lim("aspect_ratio", "max", 5, "Aspect ratio under 5"),
                       _lim("jacobian", "min", 0.6, "Jacobian above 0.6")],
            "layers": [{"what": "count", "value": 3, "unit": "none", "quote": "3 boundary layers, growth 1.2"},
                       {"what": "y_plus", "value": 30, "unit": "none", "quote": "y+ around 30"}],
            "applies_to": {"whole": ["any"], "lines": [{"quote": "3 boundary layers, growth 1.2", "kinds": ["cfd"]},
                                                         {"quote": "y+ around 30", "kinds": ["cfd"]}]}}
    return MS.snapshot(STD, {"key": MS.key_of(STD), "arguments": json.dumps(args)})


class _NotesRouter(_Router):
    """The notes' reader: answers the mesh-requirements question for whichever of the three messages it is shown;
    every other AI step finds the model down."""

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        if not tools or tools[0]["function"]["name"] != MR.TOOL:
            raise TimeoutError()
        shown = json.dumps(messages)
        answer = {"stated_unit": "none", "element_kind": [], "other": [], "limits": [], "sizes": [], "local": []}
        if GLOBAL in shown:
            answer["sizes"].append({"what": "global", "value": 3, "unit": "mm", "quote": GLOBAL})
        if FACE in shown:
            answer["local"].append({"where": "this selected face", "what": "size", "value": 2, "unit": "mm",
                                    "quote": FACE})
        if HOLES.replace("/", "\\/") in shown or HOLES in shown:
            answer["local"].append({"where": "the holes", "what": "finer", "value": 0, "unit": "none",
                                    "quote": HOLES})
        self.calls += 1
        return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=MR.TOOL,
                                                            arguments=json.dumps(answer)),))


def _part():
    """A 60 x 30 x 40 mm L bracket, its inside corner filleted at 4 mm, two 8 mm holes through its base."""
    shape = fx.bracket(4.0)
    for x in (25.0, 45.0):
        shape = fx._cut(shape, fx._cyl(x, 15.0, -1.0, 4.0, 8.0))
    return shape


@pytest.fixture(scope="module")
def part(tmp_path_factory):
    ws = _stage(_part(), tmp_path_factory.mktemp("knuckle_like"))
    record, surface, diag = FD.measure_staged(ws)
    from meshpipeline.cad.scout import read_cad
    fm = FM.from_cad(read_cad(ws / "upload.step"), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, [0.030, 0.015, 0.006], [0.0, 0.0, 1.0])           # the base's top face, between the holes
    assert k is not None
    face = {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}
    return {"ws": ws, "record": record, "surface": surface, "diag": diag, "face": face}


def _notes(face, n: int) -> list[dict]:
    """The first n of the conversation's three messages, as kept (the second with the face picked)."""
    notes = [{"id": "n1", "text": GLOBAL, "source": "chat", "turn": 1},
             {"id": "n2", "text": FACE, "source": "chat", "turn": 2, "faces": [face]},
             {"id": "n3", "text": HOLES, "source": "chat", "turn": 3}]
    return notes[:n]


def _holes(record) -> list[str]:
    return [f for h in record.get("hot_spots") or [] if h.get("type") == "hole" for f in h.get("faces") or []]


def _parts_reading(record, n: int) -> dict:
    """The parts reading of the first n messages, as the vision reader ties them: the global size on everything
    else, the picked face's words not tied to a part (the pick places them), the holes as one area asked finer."""
    entries = [{"phrase": 1, "ask": "size", "min_mm": 3.0, "max_mm": 3.0, "parts": [], "rest_parts": ["Part_1"],
                "everything_else": True, "quote": GLOBAL}]
    table = [{"id": "Part_1", "name": ""}]
    features: dict = {}
    if n >= 3:
        features["Feature_1"] = _holes(record)
        table.append({"id": "Feature_1", "feature": True, "name": "2 holes, 8 mm across", "in_part": "Part_1"})
        entries.append({"phrase": 3, "ask": "refine", "parts": ["Feature_1"], "quote": HOLES})
    return {"status": "used", "entries": entries, "parts": table, "features": features}


def _fea_notes(monkeypatch, state):
    from meshpipeline.contracts import decision_cache
    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_router", _NotesRouter())
    got = PM.read_fea_notes(state, "s1")
    assert got is not None
    return got


def _wire(monkeypatch, part):
    """The check and the stage's answers, faked: the stage sized the part, nothing picked, no plans kept."""
    from meshpipeline.application import fea_check as FC
    from meshpipeline.application import geometry_check as GC
    monkeypatch.setattr(PC, "stage_sized", lambda sid: True)
    monkeypatch.setattr(GC, "read_check", lambda sid: {"source": {"x": 1}, "facts": {}})
    monkeypatch.setattr(PC, "_measured", lambda sid, stored, work: (part["record"], part["surface"], part["diag"], ""))
    monkeypatch.setattr(FC, "confirmed_plan_choice", lambda sid: "")
    monkeypatch.setattr(FC, "_kept_plans", lambda sid: {})


def _conversation(monkeypatch, part) -> list[dict]:
    """The three messages, each read and costed in turn as the worker does, each against the reading before it."""
    _wire(monkeypatch, part)
    costs: list[dict] = []
    previous = None
    for n in (1, 2, 3):
        notes = _notes(part["face"], n)
        state = {"user_notes": notes, "user_id": "u1", "session_id": "s1", MS.STATE_KEY: _standard()}
        asked, base = _fea_notes(monkeypatch, state)
        cost = PC.for_reading("s1", _parts_reading(part["record"], n), notes, asked=asked, base_asked=base,
                              previous=previous)
        assert cost and cost["after"]["elements"] > 0, cost
        costs.append(cost)
        previous = {"status": "done", "cost": cost}
    return costs


def test_each_reply_states_the_change_its_own_message_made(monkeypatch, part):
    """The knuckle's third reply said "from about 0.9 million to about 0.4 million" again - the first reply's numbers
    - though the plan stood at 0.4 million by then and finer holes add elements."""
    c1, c2, c3 = _conversation(monkeypatch, part)
    # the costs chain: each "before" is the plan with every earlier message applied
    assert c1["after"]["elements"] == c2["before"]["elements"]
    assert c2["after"]["elements"] == c3["before"]["elements"]
    assert (c3["before"]["elements"], c3["after"]["elements"]) != (c1["before"]["elements"], c1["after"]["elements"])
    # finer at the holes adds elements; the 2 mm on a picked face too (it is costed, not left out)
    assert c3["after"]["elements"] > c3["before"]["elements"]
    assert c2["after"]["elements"] > c2["before"]["elements"]
    # the first one is against the plan the team's standard alone makes, nothing typed
    asked0 = PM.read_fea_notes({"user_notes": [], "user_id": "u1", MS.STATE_KEY: _standard()}, "s1")[1]
    _rec, plan, _f = PO.requirements_plan(part["record"], part["surface"], part["diag"], asked=asked0, part_sizes=None)
    assert c1["before"]["elements"] == int(plan["estimate"]["elements"])
    # no minutes are kept or said
    for c in (c1, c2, c3):
        assert "minutes" not in c["after"] and "usual_minutes" not in c["after"]
        assert "minute" not in PT.cost_line(c)


def test_the_count_said_is_the_runs_own_first_plan(monkeypatch, part, tmp_path):
    """The count the chat says and the count the run plans come from the same steps with the same inputs: the
    notes read with the saved standard, the picked face, the holes asked finer - equal, not near."""
    costs = _conversation(monkeypatch, part)
    notes = _notes(part["face"], 3)
    ws = _stage(_part(), tmp_path / "run")
    state = {"purpose": "structural", "user_id": "u1", "user_notes": notes, MS.STATE_KEY: _standard(),
             "part_matches": _parts_reading(part["record"], 3)}
    planned: list[dict] = []
    real = FD.size_choice

    def first(*a, **k):
        plan, said = real(*a, **k)
        planned.append(plan)
        return plan, said
    monkeypatch.setattr(FD, "size_choice", first)
    ok, marker, _cited, _said = _drive(ws, monkeypatch, state, _NotesRouter())
    assert ok, marker
    assert planned and int(planned[0]["estimate"]["elements"]) == costs[-1]["after"]["elements"]


def test_the_plain_replies_read_right():
    """The three replies as the code's own sentences say them, on the knuckle's numbers."""
    def view(rows, cost):
        return PM.with_cost({"status": "done", "rows": rows, "unsure": []}, cost)
    whole = PM.whole_or_rest([{"note": "n1", "quote": GLOBAL, "size": "3 mm", "parts": ["Part_1"], "rest": True,
                               "parts_text": PM.REST_OF_PART}])
    c1 = {"before": {"elements": 900_000}, "after": {"elements": 400_000}, "bigger_machine": False}
    r1 = PT.reply_for({"text": GLOBAL}, view(whole, c1), note_id="n1")
    assert r1 == "I've set 3 mm elements on the whole part. That takes it from about 0.9 million to about 0.4 million elements."
    # with a picked face beside it, the global size is the rest of the part
    face = {"note": "n2", "quote": FACE, "size": "2 mm", "parts": [], "picked": True,
            "faces": [{"id": "f", "label": "Face 67 · round 92 mm"}], "parts_text": "the face you picked"}
    both = PM.whole_or_rest([*whole, face])
    assert both[0]["parts_text"] == PM.REST_OF_PART
    c2 = {"before": {"elements": 400_000}, "after": {"elements": 450_000}, "bigger_machine": False}
    r2 = PT.reply_for({"text": FACE, "faces": face["faces"]}, view(both, c2), note_id="n2")
    assert r2 == "I've set 2 mm elements on Face 67 (round 92 mm)."            # under a quarter: no cost said
    c3 = {"before": {"elements": 450_000}, "after": {"elements": 690_000}, "bigger_machine": False}
    holes = {"note": "n3", "quote": HOLES, "size": "finer", "parts": ["Feature_1"], "parts_text": "4 areas"}
    r3 = PT.reply_for({"text": HOLES}, view([*both, holes], c3), note_id="n3")
    assert r3.endswith("That takes it from about 0.5 million to about 0.7 million elements.")


def test_the_saved_lines_for_flow_meshes_are_listed_once():
    """The knuckle's result notes: "3 boundary layers, growth 1.2"; "3 boundary layers, growth 1.2."; ... - one
    saved line twice (once as a setting set aside, once as words for flow meshes)."""
    std = _standard()
    settings = MS.checked(std["reading"]["arguments"], STD, route="fea", key="k", source="kept")
    settings["not_applied"] = [*(settings.get("not_applied") or []), "3 boundary layers, growth 1.2."]
    line = MS.merge(settings, MR.section("none"), route="fea")["standard_not_applied"]
    assert line.count("3 boundary layers") == 1 and line.count("y+ around 30") == 1, line
    assert line.startswith("2 lines of your saved meshing standard are written for flow (CFD) meshes")


def test_a_reading_with_nothing_typed_keeps_a_marker_and_the_next_is_against_the_standard_alone():
    assert PC.before_of(None) == ("base", None)
    assert PC.before_of({"status": "done", "cost": {"rule": PC.RULE, "status": PC.NOTHING}}) == ("base", None)
    assert PC.before_of({"status": "done", "cost": {"after": {"elements": 5}}}) == ("chained", {"elements": 5})
    # a reading whose costing failed: the plan before is not known, and nothing is compared
    assert PC.before_of({"status": "done"}) == ("unknown", None)
    assert not PC.worth_saying({"before": None, "after": {"elements": 9}})
