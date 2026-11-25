# Responsibility: Hold the mesh controls registry (contracts/mesh_controls, NOTE-13) to its promise: every control says what EVERY mesher does with it (an applier that resolves, the older code that reads it, or not here with why), its kind, stage, where and rule are declared, and the lanes cover every engine the notes reach.
# Boundaries: reads the registry and docs/RULES.md; runs nothing.
from __future__ import annotations

import importlib
import re
from pathlib import Path

from meshpipeline.contracts import mesh_controls as MC
from meshpipeline.contracts.user_notes import ENGINE_CONSUMERS

RULES = Path(__file__).resolve().parents[3] / "docs" / "RULES.md"


def _resolves(dotted: str) -> bool:
    mod, _dot, name = dotted.rpartition(".")
    return callable(getattr(importlib.import_module(mod), name, None))


def test_the_lanes_cover_every_engine_the_notes_reach():
    assert {engine for engine, _route in MC.LANES.values()} == set(ENGINE_CONSUMERS)


def test_every_control_says_what_every_mesher_does_with_it():
    for c in MC.CONTROLS.values():
        missing = sorted(set(MC.LANES) - set(c.lanes))
        assert not missing, f"{c.key} does not say what these meshers do with it: {missing}"
        for lane, how in c.lanes.items():
            assert lane in MC.LANES, f"{c.key}: unknown lane {lane}"
            if isinstance(how, MC.NotHere):
                assert how.why.strip(), f"{c.key} on {lane}: not here, but no reason"
            elif isinstance(how, MC.Legacy):
                assert how.where.strip(), f"{c.key} on {lane}: older code, but not named"
            else:
                assert _resolves(how), f"{c.key} on {lane}: applier {how} does not resolve"


def test_every_control_is_fully_declared_and_its_rule_in_force():
    book = RULES.read_text(encoding="utf-8")
    for c in MC.CONTROLS.values():
        assert c.kind in MC.KINDS and c.stage in MC.STAGES and set(c.where) <= set(MC.WHERE), c.key
        assert c.verify == "setting" or _resolves(c.verify), c.key
        assert c.examples and c.prompt and c.asked, c.key
        assert (c.kind == "choice") == bool(c.choices), c.key
        assert re.search(rf"^\| {re.escape(c.rule)} \| in force \|", book, re.M), f"{c.key}: {c.rule} not in force"
        applied_routes = {MC.LANES[lane][1] for lane, how in c.lanes.items() if isinstance(how, str | MC.Legacy)}
        assert applied_routes <= set(c.routes) | {"electronics"}, f"{c.key}: applied on routes it does not name"
