from __future__ import annotations

import importlib

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.settings import inventory


def test_llm_planner_settings_exist_with_dormant_defaults():
    assert rcfg.CAD_REPAIR_LLM_PLANNER == "off"
    assert rcfg.CAD_REPAIR_LLM_PLANNER_CALLS_MAX == 2
    assert rcfg.CAD_REPAIR_LLM_PLANNER_TIMEOUT_S == 20.0
    assert rcfg.CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE == 0.5

    group = next(g for g in inventory.INVENTORY if g.title == "CAD repair")
    declared = {v.name: v for v in group.vars}
    posture = declared["CAD_REPAIR_LLM_PLANNER"]
    assert posture.default == "off"
    assert "byte-for-byte" in posture.help
    assert declared["CAD_REPAIR_LLM_PLANNER_CALLS_MAX"].default == "2"
    assert declared["CAD_REPAIR_LLM_PLANNER_TIMEOUT_S"].default == "20.0"
    assert declared["CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE"].default == "0.5"


@pytest.mark.parametrize("raw,expected", [
    ("shadow", "shadow"), ("live", "live"), (" LIVE ", "live"), ("off", "off"),
    ("", "off"), ("true", "off"), ("on", "off"), ("lve", "off"),
])
def test_an_unknown_posture_reads_as_off(monkeypatch, raw, expected):
    monkeypatch.setenv("CAD_REPAIR_LLM_PLANNER", raw)
    try:
        reloaded = importlib.reload(rcfg)
        assert reloaded.CAD_REPAIR_LLM_PLANNER == expected
    finally:
        monkeypatch.delenv("CAD_REPAIR_LLM_PLANNER")
        importlib.reload(rcfg)
