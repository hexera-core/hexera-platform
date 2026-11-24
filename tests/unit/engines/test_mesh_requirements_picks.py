# Responsibility: Verify NOTE-17 in the mesh requirements: a size typed in a note sent with faces picked on the model is bound in code to those faces - a "here" place gets them, a global size typed with a pick becomes a size on them (never the global size) unless the words say the whole mesh - and notes with no picks read exactly as before.
from __future__ import annotations

import asyncio
import json

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.contracts.user_notes import own_notes, render
from meshpipeline.engines import mesh_requirements as MR

BORE = {"id": "Face_12", "label": "Face 12 · round Ø15 mm",
        "sig": {"area_m2": 0.0004, "centroid_m": [0.0, 0.0, 0.0], "normal": None}}
TOP = {"id": "Face_3", "label": "Face 3 · flat 120 × 60 mm", "sig": {"area_m2": 0.0072, "centroid_m": [0, 0, 0.01], "normal": [0, 0, 1]}}


def _answer(sizes=(), local=()) -> str:
    return json.dumps({"stated_unit": "none", "sizes": list(sizes), "counts": [], "layers": [], "local": list(local),
                       "physics": [], "element_kind": [], "limits": [], "other": [], "controls": []})


def _read(notes: list[dict], answer: str, route: str = "fea") -> dict:
    kept = own_notes(notes)
    req, _dropped, _na = MR.check(answer, render(kept), route)
    return MR.bind_picks(req, kept, cells=route == MR.FLOW)


def test_a_global_size_typed_with_a_pick_is_a_size_on_that_face():
    notes = [{"text": "2 mm here", "source": "console", "faces": [BORE]}]
    req = _read(notes, _answer(sizes=[{"what": "global", "value": 2, "unit": "mm", "quote": "2 mm here"}]))
    assert req["sizes"]["global_mm"] is None                       # never the global size
    (loc,) = req["local"]
    assert loc["mm"] == 2.0 and loc["what"] == "size" and loc["faces"] == ["Face_12"] and loc["rule"] == "NOTE-17"
    assert loc["where"] == "the face you picked (Face 12 · round Ø15 mm)"
    (row,) = req["rows"]
    assert row["id"] == loc["id"] and row["field"] == "local"
    assert row["asked"] == "elements of 2 mm at the face you picked (Face 12 · round Ø15 mm)"


def test_a_here_place_gets_the_picked_faces_and_the_flow_route_says_cells():
    notes = [{"text": "finer on these please", "source": "console", "faces": [BORE, TOP]}]
    req = _read(notes, _answer(local=[{"where": "these", "what": "finer", "value": 0, "unit": "none",
                                       "quote": "finer on these please"}]), route=MR.FLOW)
    (loc,) = req["local"]
    assert loc["faces"] == ["Face_12", "Face_3"] and loc["where"].startswith("the 2 faces you picked (Face 12")
    assert req["rows"][0]["asked"].startswith("finer cells at the 2 faces you picked")


def test_a_named_place_typed_with_a_pick_is_the_pick_and_the_whole_mesh_is_left_as_written():
    # ONE RULE (NOTE-17, picked_for): the words of a note sent with a face picked are for that face whatever place
    # they name - the card (part_matches.with_faces) shows the same face - unless they say the whole mesh
    notes = [{"text": "3 mm on the bore. Global size 10 mm everywhere", "source": "console", "faces": [BORE]}]
    req = _read(notes, _answer(
        sizes=[{"what": "global", "value": 10, "unit": "mm", "quote": "Global size 10 mm everywhere"}],
        local=[{"where": "the bore", "what": "size", "value": 3, "unit": "mm", "quote": "3 mm on the bore"}]))
    assert req["sizes"]["global_mm"] == 10.0
    (loc,) = req["local"]
    assert loc["faces"] == ["Face_12"] and loc["where"] == "the face you picked (Face 12 · round Ø15 mm)"
    assert MR.picked_for("3 mm on the bore", own_notes(notes)) == [BORE]
    assert MR.picked_for("Global size 10 mm everywhere", own_notes(notes)) is None


def test_only_the_note_that_carries_the_pick_is_bound():
    notes = [{"text": "Global size 10 mm", "source": "console"},
             {"text": "2 mm here", "source": "console", "faces": [BORE]}]
    req = _read(notes, _answer(sizes=[{"what": "global", "value": 10, "unit": "mm", "quote": "Global size 10 mm"}],
                               local=[{"where": "here", "what": "size", "value": 2, "unit": "mm", "quote": "2 mm here"}]))
    assert req["sizes"]["global_mm"] == 10.0
    (loc,) = req["local"]
    assert loc["mm"] == 2.0 and loc["faces"] == ["Face_12"]


def test_notes_with_no_picks_read_exactly_as_before():
    notes = [{"text": "2 mm here", "source": "console"}]
    answer = _answer(sizes=[{"what": "global", "value": 2, "unit": "mm", "quote": "2 mm here"}])
    kept = own_notes(notes)
    before, _d, _n = MR.check(answer, render(kept), "fea")
    assert MR.bind_picks(json.loads(json.dumps(before)), kept) == before and before["sizes"]["global_mm"] == 2.0


def test_snappy_never_spreads_a_picked_size_to_the_whole_wall():
    # a single-body model: a size in a place is the one wall's - but a size typed with a pick is for the face only
    from meshpipeline.engines import cfd_requirements as CR

    notes = [{"text": "2 mm here", "source": "console", "faces": [BORE]}]
    req = _read(notes, _answer(sizes=[{"what": "global", "value": 2, "unit": "mm", "quote": "2 mm here"}]),
                route=MR.FLOW)
    ask = CR.Ask(req, "snappy")
    cap, lo, hi, _f = CR.snappy_levels(ask, 0.02, floor=3, level=3, flevel=4, regions=[], part_levels=False)
    rid = req["local"][0]["id"]
    # (no face map reached this Ask: the pick is told as not re-checked, never spread; the lever itself is in
    # tests/unit/engines/test_snappy_picked_faces.py)
    assert ask.rows[rid]["status"] == "not_applied" and "the face you picked" in ask.rows[rid]["built"]
    assert "no other face was guessed" in ask.rows[rid]["built"]
    assert (lo, hi) == (3, 3)                                       # the walls are as they were


class _Router:
    def __init__(self, args: str):
        self.args = args

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_pick00001", name=MR.TOOL, arguments=self.args),))


def test_the_run_reading_binds_the_pick(monkeypatch):
    monkeypatch.setattr(llm, "_router", _Router(_answer(sizes=[{"what": "global", "value": 2, "unit": "mm",
                                                                 "quote": "2 mm here"}])))
    state = {"user_notes": [{"text": "2 mm here", "source": "console", "faces": [BORE]}], "user_id": "u1"}
    sec = asyncio.run(MR.decide(state, route="fea", job_id="j1"))
    req = MR.requirements_of(sec)
    assert sec["status"] == "used" and req["sizes"]["global_mm"] is None and req["local"][0]["faces"] == ["Face_12"]
