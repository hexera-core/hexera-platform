# Responsibility: Prove the meshing-standard bank runner (devtools/try_meshing_standard.py) with a stand-in model: it reads each line through the product's own save-time call, checks and merges it per route as a run does, and judges OK or MISS against the bank (devtools/settings_bank.json) - fields, strictness, units, told lines, the not-for-this-mesh line and design intent.
# Boundaries: no real model and no key: the model router is a stand-in that answers the reader's tool with fixed arguments. No workspace, no mesher.
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from meshpipeline.contracts import model_inference as llm
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.engines import mesh_requirements as MR

REPO = Path(__file__).resolve().parents[3]


def _runner():
    spec = importlib.util.spec_from_file_location("try_meshing_standard", REPO / "devtools/try_meshing_standard.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": [], "strictness": [],
            "applies_to": {"whole": ["any"], "lines": []}}
    base.update(fields)
    return json.dumps(base)


def _lim(measure, direction, target, quote, pct=0, outer=0):
    return {"measure": measure, "shape": "any", "direction": direction, "target": target, "allowed_percent": pct,
            "outer": outer, "has_outer": bool(outer), "quote": quote}


ANSWERS = {
    "Global element size 5 mm": _answer(sizes=[{"what": "global", "value": 5, "unit": "mm",
                                                "quote": "Global element size 5 mm"}]),
    "Aspect ratio < 5 - this is a hard limit": _answer(
        limits=[_lim("aspect_ratio", "max", 5, "Aspect ratio < 5 - this is a hard limit")],
        strictness=[{"target": "aspect_ratio", "strictness": "hard", "quote": "Aspect ratio < 5 - this is a hard limit"}]),
    "Warpage < 15° (1% between 15° and 30° is acceptable)": _answer(
        limits=[_lim("warpage", "max", 15, "Warpage < 15° (1% between 15° and 30° is acceptable)", 1, 30)]),
    "we care about fatigue at the fillets": _answer(other=[{"quote": "we care about fatigue at the fillets",
                                                            "kind": "mesh"}]),
    "min element size 10 mm\nmax element size 5 mm": _answer(sizes=[
        {"what": "min_edge", "value": 10, "unit": "mm", "quote": "min element size 10 mm"},
        {"what": "max_edge", "value": 5, "unit": "mm", "quote": "max element size 5 mm"}]),
    "Warpage < 15\nQuad min angle > 45\nTria min angle > 20\nJacobian > 0.6\nGlobal size 5 mm": _answer(
        sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "Global size 5 mm"}],
        limits=[_lim("warpage", "max", 15, "Warpage < 15"), _lim("jacobian", "min", 0.6, "Jacobian > 0.6")],
        applies_to={"whole": ["shell", "solid"], "lines": []}),
}


class _StandIn:
    """Answers the reader's tool from ANSWERS by the standard's words; counts the calls."""

    def __init__(self):
        self.calls = 0

    async def call_builder_model(self, messages, tools=None, tool_choice="auto", job_id="", user_id="",
                                 parallel_tool_calls=None, on_reasoning=None):
        self.calls += 1
        assert tools and tools[0]["function"]["name"] == MR.TOOL
        shown = json.loads(messages[1]["content"])["user_notes"]
        text = next(t for t in ANSWERS if t in shown)
        return ModelRoundResult(tool_calls=(ToolCallRequest(id="call_abcdef123", name=MR.TOOL,
                                                            arguments=ANSWERS[text]),))


@pytest.fixture
def ran(monkeypatch):
    mod = _runner()
    stand_in = _StandIn()
    monkeypatch.setattr(llm, "_router", stand_in)
    bank = {e["id"]: e for e in mod.load()}
    said: list[str] = []
    rows = asyncio.run(mod.run_bank([bank[i] for i in ("S01", "T01", "Q02", "D01", "X01", "H04")], wait_s=5.0,
                                    out=said.append))
    return mod, stand_in, {(r["id"], r["route"]): r for r in rows}, said


def test_the_bank_is_big_enough_and_every_line_is_well_formed():
    mod = _runner()
    lines = mod.load()
    assert len(lines) >= 90 and sum(e["category"] == "design_intent" for e in lines) >= 20
    for e in lines:
        assert e["routes"] and set(e["routes"]) <= set(mod.ROUTES), e["id"]
        assert any(e["expect"].get(r) or e["expect"].get("*") for r in e["routes"]), e["id"]
    sheet = next(e for e in lines if e["id"] == "SHEET1")
    assert len(sheet["text"].splitlines()) >= 15


def test_each_line_is_read_once_through_the_products_save_time_call(ran):
    _mod, stand_in, rows, _said = ran
    assert stand_in.calls == 6                          # one reading per line, whatever the routes
    assert {k[1] for k in rows if k[0] == "S01"} == {"solid", "shell"}


def test_a_field_its_strictness_and_its_allowance_are_judged_ok(ran):
    _mod, _s, rows, _said = ran
    assert rows[("S01", "solid")]["ok"] and "global=5.0" in rows[("S01", "solid")]["read"]
    assert rows[("T01", "shell")]["ok"] and "strict:aspect_ratio=hard" in rows[("T01", "shell")]["read"]
    assert rows[("Q02", "shell")]["ok"]


def test_a_contradiction_is_told_and_judged_ok(ran):
    _mod, _s, rows, _said = ran
    row = rows[("X01", "solid")]
    assert row["ok"], row
    assert any(w.startswith("Not applied (your settings)") for w in row["lines"])


def test_design_intent_told_only_as_not_applied_is_a_miss(ran):
    _mod, _s, rows, said = ran
    row = rows[("D01", "solid")]
    assert not row["ok"] and row["missed"] == ["guidance"] and row["told"] == "not applied"
    assert any(s.startswith("MISS D01") for s in said)


def test_a_structural_list_on_a_flow_mesh_is_judged_by_the_not_here_line(ran):
    _mod, _s, rows, _said = ran
    row = rows[("H04", "cfd")]
    assert row["ok"] and row["told"] == "not for this kind of mesh"


def test_totals_count_per_category_and_route(ran):
    mod, _s, rows, _said = ran
    out: list[str] = []
    t = mod.totals(list(rows.values()), out=out.append)
    assert t["rows"] == len(rows) and t["route"]["solid"]["miss"] >= 1
    assert out[-1].startswith("all: ")


def test_with_no_model_configured_nothing_is_called(monkeypatch, capsys):
    mod = _runner()
    monkeypatch.setattr(mod, "_configured", lambda: "no key is set")
    assert mod.main([]) == 0
    assert "Nothing was called" in capsys.readouterr().out
