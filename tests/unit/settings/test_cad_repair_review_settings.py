from __future__ import annotations

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.settings import inventory


def test_review_settings_exist_with_dormant_defaults():
    assert rcfg.CAD_REPAIR_REVIEW_ENABLED is False
    assert rcfg.CAD_REPAIR_REVIEW_SAMPLE_SIZE == 3
    assert rcfg.CAD_REPAIR_REVIEW_LLM_CALLS_MAX == 4
    assert rcfg.CAD_REPAIR_REVIEW_LLM_TIMEOUT_S == 20.0


def test_review_enabled_is_declared_in_cad_repair_group():
    group = next(g for g in inventory.INVENTORY if g.title == "CAD repair")
    var = next(v for v in group.vars if v.name == "CAD_REPAIR_REVIEW_ENABLED")
    assert var.kind == "bool"
    assert var.default == "false"
    assert "mints nothing" in var.help
    assert "parks" in var.help
