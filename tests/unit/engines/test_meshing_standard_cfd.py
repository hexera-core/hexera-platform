# Responsibility: Verify the team's meshing standard (STD-1..3) on the flow route (NOTE-10, CFDREQ-1..7): the CFD reading step merges the standard under the notes field by field, a standard alone reaches every CFD engine's set, the report rows and the closing line say where each value came from, the flow fields' cross-field rules hold across the two sources, the standard's unit reaches the flow lengths, and with neither nothing is written.
# Boundaries: pure over plain data and temporary workspaces; the notes' reader is replaced by a fixed reading and the standard's kept reading is used, so no model is asked. No mesher is run.
from __future__ import annotations

import asyncio
import json

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return json.dumps(base)


STANDARD = "Base cell 20 mm\n5 prism layers\nfirst layer 0.02 mm\nMax 8 million cells\nNon-ortho under 65"
STANDARD_ANSWER = _answer(
    sizes=[{"what": "global", "value": 20, "unit": "mm", "quote": "Base cell 20 mm"}],
    counts=[{"what": "ceiling", "value": 8000000, "quote": "Max 8 million cells"}],
    layers=[{"what": "count", "value": 5, "unit": "none", "quote": "5 prism layers"},
            {"what": "first_height", "value": 0.02, "unit": "mm", "quote": "first layer 0.02 mm"}],
    limits=[{"measure": "non_orthogonality", "shape": "any", "direction": "max", "target": 65, "allowed_percent": 0,
             "outer": 0, "has_outer": False, "quote": "Non-ortho under 65"}])


def _state(notes: str | None = None, standard: str | None = STANDARD, answer: str = STANDARD_ANSWER) -> dict:
    state: dict = {"purpose": "external aerodynamics", "user_id": "u1"}
    if notes:
        state["user_notes"] = [{"text": notes, "source": "console"}]
    if standard:
        state[MS.STATE_KEY] = MS.snapshot(standard, {"key": MS.key_of(standard), "arguments": answer})
    return state


def _notes_reading(monkeypatch, notes: str | None, answer: str | None = None):
    """The notes' reader replaced by the reading of `answer` (route "flow"); "none" with no notes, as the reader."""
    async def decide(state, *, route="", job_id="", run=None, enabled=True, wait_s=MR.TIMEOUT_S):
        assert route == CR.ROUTE
        if not notes:
            return MR.section("none")
        req, dropped, not_applied = MR.check(answer or _answer(), notes, route="flow")
        return MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied, key="k", source="model")
    monkeypatch.setattr(MR, "decide", decide)


def _read(ws, state) -> dict:
    return asyncio.run(CR.read_for_attempt(ws, state, job_id="j1"))


def _rows(req) -> dict:
    return {r["field"]: r for r in req["rows"]}


def test_a_standard_alone_reaches_every_cfd_engine_and_says_where_it_came_from(tmp_path, monkeypatch):
    _notes_reading(monkeypatch, None)
    sec = _read(tmp_path, _state())
    assert sec["status"] == "used"
    req = CR.requirements_for(tmp_path)
    assert req["sizes"]["global_mm"] == 20 and req["layers"]["count"] == 5 and req["cells"]["ceiling"] == 8_000_000
    assert {r["from"] for r in req["rows"]} == {"settings"}
    assert all(w.startswith(("From your settings: ", "Not applied")) for w in MR.words(MR.read(tmp_path)))
    # the engine's Ask reads the same set; the report rows carry where each came from; the closing line leads with it
    ask = CR.Ask.of(tmp_path, "snappy")
    assert ask is not None and ask.n_layers == 5 and abs(ask.first_m - 2e-5) < 1e-12
    ask.budget(2_000_000, hard=8_000_000)
    ask.write(tmp_path)
    q = CR.attach(tmp_path, {"cells": 1_000_000, "max_non_ortho": 40.0, "layer_coverage_pct": 95.0}, "snappy")
    assert {r["from"] for r in q["requirements"]["rows"]} == {"settings"}
    from meshpipeline.contracts.user_notes import read_remarks
    said = [str(r) for r in read_remarks(tmp_path)]
    assert any("From your settings: " in s for s in said), said


def test_the_notes_alone_are_read_exactly_as_before(tmp_path, monkeypatch):
    notes = "max 2 million cells"
    answer = _answer(counts=[{"what": "ceiling", "value": 2000000, "quote": "max 2 million cells"}])
    _notes_reading(monkeypatch, notes, answer)
    sec = _read(tmp_path, _state(notes, standard=None))
    req, dropped, not_applied = MR.check(answer, notes, route="flow")
    assert sec == MR.section("used", requirements=req, dropped=dropped, not_applied=not_applied, key="k",
                             source="model")
    assert "standard" not in MR.read(tmp_path) and not any("from" in r for r in CR.requirements_for(tmp_path)["rows"])


def test_both_the_notes_win_field_by_field_and_the_flow_rules_hold_across_the_two(tmp_path, monkeypatch):
    notes = "no boundary layers\nmax 2 million cells"
    _notes_reading(monkeypatch, notes, _answer(
        counts=[{"what": "ceiling", "value": 2000000, "quote": "max 2 million cells"}],
        layers=[{"what": "count", "value": 0, "unit": "none", "quote": "no boundary layers"}]))
    _read(tmp_path, _state(notes))
    sec = MR.read(tmp_path)
    req = CR.requirements_for(tmp_path)
    rows = _rows(req)
    # the notes' fields are theirs: no layers, 2 million cells; the standard keeps the rest
    assert rows["layers:count"]["from"] == "notes" and req["layers"]["count"] == 0
    assert rows["cells:ceiling"]["from"] == "notes" and req["cells"]["ceiling"] == 2_000_000
    assert rows["global"]["from"] == "settings" and rows["limit:non_orthogonality"]["from"] == "settings"
    # the standard's first layer does not fit "no boundary layers": not applied, and said
    assert "layers:first_height" not in rows and req["layers"]["first_mm"] is None
    assert any("your notes ask for no boundary layers" in w for w in MR.words(sec))
    ask = CR.Ask.of(tmp_path, "snappy")
    assert ask.n_layers == 0 and ask.first_m is None and not any(lim is None for lim in ask.limits)


def test_a_cell_floor_from_the_standard_above_the_notes_ceiling_is_not_applied(tmp_path, monkeypatch):
    standard = "At least 10 million cells"
    s_answer = _answer(counts=[{"what": "floor", "value": 10000000, "quote": "At least 10 million cells"}])
    notes = "max 2 million cells"
    _notes_reading(monkeypatch, notes, _answer(counts=[{"what": "ceiling", "value": 2000000,
                                                          "quote": "max 2 million cells"}]))
    _read(tmp_path, _state(notes, standard, s_answer))
    req = CR.requirements_for(tmp_path)
    assert req["cells"]["floor"] is None and req["cells"]["ceiling"] == 2_000_000
    assert any("it does not fit the cell count your notes ask for" in w for w in MR.words(MR.read(tmp_path)))


def test_the_standards_unit_reaches_the_flow_lengths_written_with_none(tmp_path, monkeypatch):
    standard = "All dimensions in inches\nMax 8 million cells"
    s_answer = _answer(stated_unit="in", counts=[{"what": "ceiling", "value": 8000000, "quote": "Max 8 million cells"}])
    notes = "surface cells 0.1\nfirst layer 0.001\nrefine around the wing to 0.5"
    _notes_reading(monkeypatch, notes, _answer(
        sizes=[{"what": "near_wall", "value": 0.1, "unit": "none", "quote": "surface cells 0.1"}],
        layers=[{"what": "first_height", "value": 0.001, "unit": "none", "quote": "first layer 0.001"}],
        local=[{"where": "the wing", "what": "size", "value": 0.5, "unit": "none",
                "quote": "refine around the wing to 0.5"}]))
    _read(tmp_path, _state(notes, standard, s_answer))
    req = CR.requirements_for(tmp_path)
    assert abs(req["sizes"]["near_wall_mm"] - 2.54) < 1e-9
    assert abs(req["layers"]["first_mm"] - 0.0254) < 1e-12
    assert abs(req["local"][0]["mm"] - 12.7) < 1e-9
    rows = _rows(req)
    assert "cells of 2.54 mm at the walls" in rows["near_wall"]["asked"] and "inches, as your settings say" in \
        rows["near_wall"]["asked"]
    assert "inches, as your settings say" in rows["layers:first_height"]["asked"]
    assert "12.7 mm" in rows["local"]["asked"] and "cells" in rows["local"]["asked"]


def test_nothing_in_either_writes_nothing_and_asks_no_model(tmp_path, monkeypatch):
    from meshpipeline.contracts import model_inference as llm

    class _NoModel:
        async def call_builder_model(self, *a, **k):
            raise AssertionError("the model was asked")
    monkeypatch.setattr(llm, "_router", _NoModel())
    sec = _read(tmp_path, {"purpose": "external aerodynamics", "user_id": "u1"})
    assert sec["status"] == "none" and "standard" not in sec
    assert not (tmp_path / MR.FILE).exists() and CR.requirements_for(tmp_path) is None
    assert CR.Ask.of(tmp_path, "snappy") is None and not (tmp_path / CR.PLAN_FILE).exists()
