# Responsibility: Verify CAD simplification ships switched OFF with the approved budget and screen margin, and that every switch it reads is declared in the settings catalogue.
from __future__ import annotations

import meshpipeline.settings.cad_simplify as scfg
from meshpipeline.settings import inventory


def _declared() -> dict:
    group = next(g for g in inventory.INVENTORY if g.title == "CAD simplification")
    return {v.name: v for v in group.vars}


def test_the_kill_switch_ships_off_and_is_the_one_template_setting():
    assert scfg.CAD_SIMPLIFY_ENABLED is False
    declared = _declared()
    assert declared["CAD_SIMPLIFY_ENABLED"].default == "false"
    assert declared["CAD_SIMPLIFY_ENABLED"].kind == "bool"
    # like CAD_REPAIR_ENABLED, the one switch an operator is meant to see in .env.example
    assert [n for n, v in declared.items() if v.exposure == "template"] == ["CAD_SIMPLIFY_ENABLED"]


def test_the_approved_budget_and_margin_are_the_defaults():
    # 0.1% of compliance, and a feature kept only when it reads under half of that
    assert scfg.CAD_SIMPLIFY_BUDGET == 0.001
    assert scfg.CAD_SIMPLIFY_SCREEN_MARGIN == 0.5
    declared = _declared()
    assert declared["CAD_SIMPLIFY_BUDGET"].default == "0.001"
    assert declared["CAD_SIMPLIFY_SCREEN_MARGIN"].default == "0.5"


def test_every_module_setting_is_declared_with_the_default_the_code_uses():
    declared = _declared()
    names = [n for n in vars(scfg) if n.startswith("CAD_SIMPLIFY_")]
    assert set(names) == set(declared)
    for name in names:
        assert str(getattr(scfg, name)).lower() == declared[name].default.lower() or \
            float(getattr(scfg, name)) == float(declared[name].default), name
