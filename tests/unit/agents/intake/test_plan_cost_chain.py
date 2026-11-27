# Responsibility: Verify NOTE-26 on the C5 knuckle's conversation (26 Nov), replayed on a test part of its shape (11
#       holes in 4 sizes, a fillet, a team standard): each count line's "before" is the plan as it stood just before
#       that message, so the lines chain; "finer at the holes" is costed on the holes' faces; the count said after the
#       three messages is the count the run's report prints as "Estimated before meshing" - the AI step's decision
#       made when it was costed rides to the run; the saved standard's flow lines are listed once.
# Boundaries: the part is built in the test (tests/fea_fixtures.py), measured and planned in-process; the model is a
#       fake router answering the mesh-requirements question and the AI step's decision (the standard's reading is
#       the one kept at save time); one real solid run of the same part (gmsh) for the parity test. No live model, no
#       store: the check and the stage's answers are faked.
from __future__ import annotations

import json
import time

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
from meshpipeline.cad import part_words as PW  # noqa: E402
from meshpipeline.contracts import model_inference as llm  # noqa: E402
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest  # noqa: E402
from meshpipeline.engines import fea_ai as FA  # noqa: E402
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


def _decision(refine: bool) -> str:
    """The AI step's answer: the fillet (H1) refined for its stress, or nothing refined."""
    return json.dumps({"analysis": "static", "matters": "the fillet carries the load",
                       "zones": [{"id": "H1", "priority": "refine", "reason": "the load path turns there"}] if refine
                       else [], "features": [], "joints": [], "target_elements": 0, "target_min": 0, "target_max": 0,
                       "words": ["Refined the fillet."] if refine else []})


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


class _ModelRouter(_Router):
    """The notes' reader, for whichever of the three messages it is shown, and the AI step (`refine`: whether it
    refines the fillet); the parts reader finds the model down (the stored reading is the one used)."""

    def __init__(self, refine: bool = True):
        super().__init__()
        self.refine = refine

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        name = tools[0]["function"]["name"] if tools else ""
        if name == FA.TOOL_DECISION:
            args = _decision(self.refine)
        elif name == MR.TOOL:
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
            args = json.dumps(answer)
        else:
            raise TimeoutError()
        self.calls += 1
        return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=name, arguments=args),))


def knuckle_like():
    """A 160 x 80 x 90 mm L bracket: its inside corner filleted at 6 mm, 11 holes in 4 sizes (4 of 8 mm and 2 of 20 mm
    through the base, 3 of 12 mm through the upright, 2 of 6 mm) - "4 areas" of holes, as on the C5 knuckle."""
    s = fx._fuse(fx._box(0, 0, 0, 160, 80, 16), fx._box(0, 0, 0, 18, 80, 90))
    inner = fx._edges_where(s, lambda b: abs(b[0] - 18) < 1e-3 and abs(b[3] - 18) < 1e-3
                            and abs(b[2] - 16) < 1e-3 and abs(b[5] - 16) < 1e-3)
    s = fx._fillet(s, inner, 6.0)
    for x, y in [(40, 15), (40, 65), (70, 15), (70, 65)]:
        s = fx._cut(s, fx._cyl(x, y, -1, 4, 20))
    for x, y in [(110, 25), (110, 55)]:
        s = fx._cut(s, fx._cyl(x, y, -1, 10, 20))
    for y, z in [(20, 50), (40, 70), (60, 50)]:
        s = fx._cut(s, fx._cyl(-1, y, z, 6, 22, axis=(1, 0, 0)))
    for x, y in [(145, 20), (145, 60)]:
        s = fx._cut(s, fx._cyl(x, y, -1, 3, 20))
    return s


@pytest.fixture(scope="module")
def part(tmp_path_factory):
    ws = _stage(knuckle_like(), tmp_path_factory.mktemp("knuckle_like"))
    record, surface, diag = FD.measure_staged(ws)
    from meshpipeline.cad.scout import read_cad
    fm = FM.from_cad(read_cad(ws / "upload.step"), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, [0.120, 0.025, 0.008], [-1.0, 0.0, 0.0])           # a 20 mm bore: a round face picked
    assert k is not None and "round Ø20 mm" in FM.label(fm, k)
    face = {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}
    table, _faces_of, _shown = PW.table_of(record, surface)
    return {"ws": ws, "record": record, "surface": surface, "diag": diag, "face": face, "table": table}


def _notes(face, n: int) -> list[dict]:
    """The first n of the conversation's three messages, as kept (the second with the face picked)."""
    notes = [{"id": "n1", "text": GLOBAL, "source": "chat", "turn": 1},
             {"id": "n2", "text": FACE, "source": "chat", "turn": 2, "faces": [face]},
             {"id": "n3", "text": HOLES, "source": "chat", "turn": 3}]
    return notes[:n]


def _reading(table, n: int) -> dict:
    """The checklist's parts reading of the first n messages, as the vision reader ties them and the check keeps it
    (no faces of its own): the global size on everything else, the picked face's words left to the pick, and the
    holes as their 4 areas asked finer."""
    entries = [{"phrase": 1, "ask": "size", "min_mm": 3.0, "max_mm": 3.0, "parts": [], "rest_parts": ["Part_1"],
                "everything_else": True, "quote": GLOBAL}]
    if n >= 3:
        holes = [str(r["id"]) for r in table if r.get("kind") == "hole group"]
        assert len(holes) == 4
        entries.append({"phrase": 3, "ask": "refine", "parts": holes, "quote": HOLES})
    return {"status": "used", "entries": entries, "parts": [dict(r) for r in table]}


def _router(monkeypatch, router):
    from meshpipeline.contracts import decision_cache
    monkeypatch.setattr(decision_cache, "recorded_answer", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_router", router)


def _wire(monkeypatch, part):
    """The check and the stage's answers, faked: the stage sized the part, nothing picked, no plans kept."""
    from meshpipeline.application import fea_check as FC
    from meshpipeline.application import geometry_check as GC
    monkeypatch.setattr(FA, "FEA_AI", True)
    monkeypatch.setattr(PC, "stage_sized", lambda sid: True)
    monkeypatch.setattr(GC, "read_check", lambda sid: {"source": {"x": 1}, "facts": {}})
    monkeypatch.setattr(PC, "_measured", lambda sid, stored, work: (part["record"], part["surface"], part["diag"], ""))
    monkeypatch.setattr(FC, "confirmed_plan_choice", lambda sid: "")
    monkeypatch.setattr(FC, "_kept_plans", lambda sid: {})


def _state(notes):
    return {"user_notes": notes, "user_id": "u1", "session_id": "s1", MS.STATE_KEY: _standard()}


def _conversation(monkeypatch, part, *, chained: bool = True) -> list[dict]:
    """The three messages, each read and costed in turn as the worker does it, each against the reading before it
    (`chained` False: the reading before had not been costed yet - "before" is planned from what it was read with)."""
    _wire(monkeypatch, part)
    _router(monkeypatch, _ModelRouter(refine=True))
    costs: list[dict] = []
    previous = None
    for n in (1, 2, 3):
        notes = _notes(part["face"], n)
        asked, base = PM.read_fea_notes(_state(notes), "s1")
        section = _reading(part["table"], n)
        t0 = time.time()
        cost = PC.for_reading("s1", section, notes, asked=asked, base_asked=base, previous=previous,
                              ai_state={"user_id": "u1", MS.STATE_KEY: _standard()})
        assert cost and cost["after"]["elements"] > 0, cost
        cost["took_s"] = round(time.time() - t0, 2)
        costs.append(cost)
        previous = {"status": "done", "section": section, "fea_inputs": PC.inputs(section, asked, notes),
                    **({"cost": cost} if chained else {})}
    return costs


def test_each_count_line_states_the_change_its_own_message_made(monkeypatch, part):
    """The knuckle: the third message ("finer in/around the holes") roughly doubled the mesh and got no count line - it
    was costed as a part named Feature_n that does not exist, and changed nothing."""
    c1, c2, c3 = _conversation(monkeypatch, part)
    assert c1["after"]["elements"] == c2["before"]["elements"]
    assert c2["after"]["elements"] == c3["before"]["elements"]
    assert (c3["before"]["elements"], c3["after"]["elements"]) != (c1["before"]["elements"], c1["after"]["elements"])
    # finer at the holes is costed on the holes' faces: worth saying, and the third message has its count line
    assert c3["after"]["elements"] > (1 + PC.WORTH_SAYING) * c3["before"]["elements"], c3
    view = PM.with_cost({"status": "done", "rows": [{"note": "n3", "quote": HOLES, "size": "finer",
                                                     "parts": [], "parts_text": "4 areas"}], "unsure": []}, c3)
    rows = PT.mine({"text": HOLES}, view, note_id="n3")[0]
    assert PT.cost_line(PT.cost_mine(view, rows) or {}).startswith("That takes it from about ")
    # the AI step's decision was made and kept for the run; the count is exact, not a floor
    for c in (c1, c2, c3):
        assert c["ai_args"] and not c["floor"]
        assert "minutes" not in c["after"]
    # the same before when the reading before had not been costed yet (planned from what it was read with)
    d1, d2, d3 = _conversation(monkeypatch, part, chained=False)
    assert [d["before"]["elements"] for d in (d1, d2, d3)] == [c["before"]["elements"] for c in (c1, c2, c3)]


def test_the_old_costing_left_the_holes_out(monkeypatch, part):
    """The gap on this part: the reading as the check keeps it (no faces) against the reading as the run takes it."""
    _wire(monkeypatch, part)
    _router(monkeypatch, _ModelRouter(refine=True))
    notes = _notes(part["face"], 3)
    asked, _base = PM.read_fea_notes(_state(notes), "s1")
    raw = _reading(part["table"], 3)
    _r, old, _f = PO.requirements_plan(part["record"], part["surface"], part["diag"], asked=asked, part_sizes=raw)
    _r, new, _f = PO.requirements_plan(part["record"], part["surface"], part["diag"], asked=asked,
                                       part_sizes=PW.as_run(raw, part["record"], part["surface"]))
    assert new["estimate"]["elements"] > 1.25 * old["estimate"]["elements"]


def test_the_count_said_is_the_runs_estimate_before_meshing(monkeypatch, part, tmp_path):
    """After the three messages the count line's number is the run's own "Estimated before meshing" - the run asked
    its AI step nothing: the decision made when the words were costed rode with the reading (the model would have
    answered differently in the run)."""
    costs = _conversation(monkeypatch, part)
    notes = _notes(part["face"], 3)
    ws = _stage(knuckle_like(), tmp_path / "run")
    reading = {**_reading(part["table"], 3), "ai_args": costs[-1]["ai_args"]}
    state = {"purpose": "structural", "user_id": "u1", "user_notes": notes, MS.STATE_KEY: _standard(),
             "part_matches": reading}
    planned: list[dict] = []
    real = FD.size_choice

    def first(*a, **k):
        plan, said = real(*a, **k)
        planned.append(plan)
        return plan, said
    monkeypatch.setattr(FD, "size_choice", first)
    ok, marker, _cited, _said = _drive(ws, monkeypatch, state, _ModelRouter(refine=False))
    assert planned, marker
    words = [w for w in json.loads((ws / "fea_plan.json").read_text()).get("words") or []
             if w.startswith("Estimated before meshing")]
    said = int(planned[0]["estimate"]["elements"])
    assert said == costs[-1]["after"]["elements"]
    assert words and f"about {said:,} elements" in words[0], words
    # the plan step's decision (the fillet refined), not the one the model would have given the run (nothing)
    ai = planned[0].get("ai") or {}
    assert ai.get("source") == "plan step" and "H1" in ((ai.get("decision") or {}).get("zones") or {})


def test_the_replies_and_count_lines_read_right():
    """The three replies as the code's own sentences say them (no count in them), each count line after it."""
    def view(rows, cost):
        return PM.with_cost({"status": "done", "rows": rows, "unsure": []}, cost)
    whole = PM.whole_or_rest([{"note": "n1", "quote": GLOBAL, "size": "3 mm", "parts": ["Part_1"], "rest": True,
                               "parts_text": PM.REST_OF_PART}])
    c1 = {"before": {"elements": 900_000}, "after": {"elements": 400_000}, "bigger_machine": False}
    v1 = view(whole, c1)
    assert PT.reply_for({"text": GLOBAL}, v1, note_id="n1") == "I've set 3 mm elements on the whole part."
    assert PT.cost_line(c1) == "That takes it from about 0.9 million to about 0.4 million elements."
    face = {"note": "n2", "quote": FACE, "size": "2 mm", "parts": [], "picked": True,
            "faces": [{"id": "f", "label": "Face 67 · round 92 mm"}], "parts_text": "the face you picked"}
    both = PM.whole_or_rest([*whole, face])
    c2 = {"before": {"elements": 400_000}, "after": {"elements": 420_000}, "bigger_machine": False}
    v2 = view(both, c2)
    assert PT.reply_for({"text": FACE, "faces": face["faces"]}, v2, note_id="n2") == (
        "I've set 2 mm elements on Face 67 (round 92 mm).")
    assert PT.cost_mine(v2, [face]) is None                 # under a quarter: no count line
    holes = {"note": "n3", "quote": HOLES, "size": "finer", "parts": ["Feature_1"], "parts_text": "4 areas"}
    c3 = {"before": {"elements": 420_000}, "after": {"elements": 857_000}, "bigger_machine": False}
    v3 = view([*both, holes], c3)
    assert "That takes it" not in PT.reply_for({"text": HOLES}, v3, note_id="n3")
    assert PT.cost_line(PT.cost_mine(v3, [holes]) or {}) == (
        "That takes it from about 0.4 million to about 0.9 million elements.")


def test_a_cost_for_notes_since_changed_is_never_kept(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(PM, "_read", lambda sid, name: store.get(name))
    monkeypatch.setattr(PM, "_write", lambda sid, name, payload: store.__setitem__(name, payload))
    store[PM.MATCHES] = {"key": "k2", "status": "reading"}
    assert not PM.keep_cost("s1", "k1", {"after": {"elements": 5}})
    store[PM.MATCHES] = {"key": "k1", "status": "done", "cost_pending": True}
    assert PM.keep_cost("s1", "k1", {"after": {"elements": 5}})
    assert store[PM.MATCHES] == {"key": "k1", "status": "done", "cost": {"after": {"elements": 5}}}


def test_the_saved_lines_for_flow_meshes_are_listed_once():
    """The knuckle's result notes: "3 boundary layers, growth 1.2"; "3 boundary layers, growth 1.2."; ... - one
    saved line twice (once as a setting set aside, once as words for flow meshes)."""
    std = _standard()
    settings = MS.checked(std["reading"]["arguments"], STD, route="fea", key="k", source="kept")
    settings["not_applied"] = [*(settings.get("not_applied") or []), "3 boundary layers, growth 1.2."]
    line = MS.merge(settings, MR.section("none"), route="fea")["standard_not_applied"]
    assert line.count("3 boundary layers") == 1 and line.count("y+ around 30") == 1, line
    assert line.startswith("2 lines of your saved meshing standard are written for flow (CFD) meshes")


def test_where_before_comes_from():
    assert PC.before_of(None) == ("base", None)
    assert PC.before_of({"status": "done", "cost": {"rule": PC.RULE, "status": PC.NOTHING}}) == ("base", None)
    assert PC.before_of({"status": "done", "cost": {"after": {"elements": 5}}}) == ("chained", {"elements": 5})
    kept = PC.inputs({"status": "used"}, None, [])
    assert PC.before_of({"status": "done", "fea_inputs": kept}) == ("planned", kept)
    assert PC.before_of({"status": "done"}) == ("unknown", None)
    assert not PC.worth_saying({"before": None, "after": {"elements": 9}})
