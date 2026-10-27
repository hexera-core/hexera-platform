# Responsibility: Pin PLAN-fea-22, the size choice (engines/fea_plan_options): nothing to choose when the full-rules
# mesh fits the standard machine; when it needs a bigger one, the quicker mesh is recommended only when what it
# gives up is shape-only (rounds, relief) or capped sharp corners, and the full one when it would coarsen a wall or
# a stress raiser; the stage's view of it, the answer it checks, and the run's cap (engines/fea_driver).
# Boundaries: pure - the plans are stand-ins with the fields the rows read; nothing is measured or meshed.
from __future__ import annotations

import pytest

from meshpipeline.application import fea_check as F
from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan_options as PO


def _plan(elements: int, short: tuple[str, ...] = (), *, at_minimums: bool = False) -> dict:
    zones = [{"id": f"Z{i}", "kind": k, "short": True, "faces": [f"Face_{i}"]} for i, k in enumerate(short)]
    zones.append({"id": "W99", "kind": "wall", "faces": ["Face_99"]})
    return {"estimate": {"elements": elements}, "zones": zones,
            "resolution_short": [f"{z['id']} short" for z in zones if z.get("short")],
            **({"coarsened": {"at_minimums": True}} if at_minimums else {})}


def _options(monkeypatch, full: dict, quick: dict | None = None) -> dict:
    calls = []

    def choose(record, surface, diag_m, *, ai=None, cap=None, **_kw):
        calls.append(cap)
        return quick if cap is not None else full
    monkeypatch.setattr(FD, "choose_plan", choose)
    out = PO.options({"faces": {}}, None, 0.1)
    # the quicker mesh is planned only when there is a choice to make, capped to the standard machine
    assert calls[0] is None and all(c == PO.standard_elements() for c in calls[1:])
    return out


def test_a_mesh_that_fits_the_standard_machine_has_nothing_to_choose(monkeypatch):
    out = _options(monkeypatch, _plan(1_500_000))
    assert out["choice"] is False and out["quicker"] is None and out["full"]["standard"] is True


def test_the_quicker_mesh_is_recommended_when_it_gives_up_only_corners_and_rounds(monkeypatch):
    out = _options(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("corner", "corner", "round")))
    assert out["choice"] is True and out["recommended"] == PO.QUICKER
    assert out["full"]["standard"] is False and out["quicker"]["standard"] is True
    assert out["quicker"]["short_kinds"] == ["corner", "round"] and out["quicker"]["short_zones"] == 3
    assert "capped sharp corners" in out["why"] and "same" in out["why"]


@pytest.mark.parametrize("kind", ["wall", "fillet", "hole", "notch", "part"])
def test_the_full_mesh_is_recommended_when_the_quicker_one_would_coarsen_a_wall_or_a_stress_raiser(monkeypatch, kind):
    out = _options(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("corner", kind)))
    assert out["choice"] is True and out["recommended"] == PO.FULL
    assert "short of their rules" in out["why"]


def test_no_quicker_mesh_when_even_its_minimums_need_a_bigger_machine(monkeypatch):
    out = _options(monkeypatch, _plan(25_000_000), _plan(20_000_000, ("corner", "wall"), at_minimums=True))
    assert out["choice"] is False and out["recommended"] == PO.FULL


def _facts(options: dict) -> dict:
    return {F.PLAN_FACT_KEY: {"sized": True, **options}}


def test_the_stage_shows_both_meshes_with_the_recommended_one_picked(monkeypatch):
    out = _options(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("corner",)))
    view = F.plan_view(_facts(out))
    assert view["state"] == "sized" and view["choice"] is True and view["recommended"] == "quicker"
    full, quick = view["options"]
    assert (full["key"], quick["key"]) == ("full", "quicker") and quick["recommended"] and not full["recommended"]
    assert "25.0 million elements" in full["words"] and "GiB machine" in full["words"]
    assert "the standard machine" in quick["words"] and "1 zones coarser" in quick["words"]
    # the answer is checked against what was offered: the pick, else the recommended one
    assert F.plan_answer("full", _facts(out)) == "full"
    assert F.plan_answer("", _facts(out)) == "quicker"
    assert F.plan_answer("bogus", _facts(out)) == "quicker"
    assert F.plan_words("full", _facts(out)).startswith("Mesh: full rules (about 25.0 million elements")


def test_no_choice_offered_means_no_answer(monkeypatch):
    out = _options(monkeypatch, _plan(1_000_000))
    view = F.plan_view(_facts(out))
    assert view["choice"] is False and "standard machine" in view["words"]
    assert F.plan_answer("quicker", _facts(out)) == ""
    assert F.plan_view({}) is None
    assert F.plan_view({F.PLAN_FACT_KEY: {"sizing": True, "token": "t"}})["state"] == "sizing"
    assert F.plan_answer("quicker", {F.PLAN_FACT_KEY: {"sizing": True}}) == ""
    not_sized = F.plan_view({F.PLAN_FACT_KEY: {"sized": False, "why": "There was no time left"}})
    assert not_sized["state"] == "not_sized" and "full-rules mesh is made" in not_sized["words"]


def test_the_run_caps_the_plan_only_when_the_quicker_mesh_was_picked():
    assert FD.plan_cap_of({"fea_plan_choice": "quicker"}) == PO.standard_elements()
    assert FD.plan_cap_of({"fea_plan_choice": "full"}) is None
    assert FD.plan_cap_of({}) is None and FD.plan_cap_of(None) is None
    # THE CAP KEEPS A PLAN ON THE STANDARD JOB: a plan at its aim (PLAN-fea-8) is chosen for Cloud Run, mesher and
    # solver files together (contracts/mesh_machine.choose), and one a little over it is not
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import fea_plan as P
    at_aim = P.LIMIT_AIM * PO.standard_elements()
    assert MM.choose(process_gib=FD.memory_need(at_aim)[0], workspace_gib=FD.memory_need(at_aim)[1])["where"] == "cloud_run"
    over = 1.02 * at_aim
    assert MM.choose(process_gib=FD.memory_need(over)[0], workspace_gib=FD.memory_need(over)[1])["where"] == "gce"
