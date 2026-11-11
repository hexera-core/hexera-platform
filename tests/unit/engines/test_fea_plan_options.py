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
    # THE TIME, AS IT USUALLY GOES AND ITS UPPER END (Areen, 28 Oct), and that a retry can take longer
    assert "usually about" in full["words"] and ", up to " in full["words"]
    assert view["why"].endswith("A retry can take longer.")
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
    assert not_sized["state"] == "not_sized" and "takes the recommended mesh" in not_sized["words"]
    assert "without waiting for you" in not_sized["words"]


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


def test_the_time_is_said_as_usually_and_up_to():
    assert PO.minutes_words(30, 60) == "usually about 30 minutes, up to 60 minutes"
    assert PO.minutes_words(45, 200) == "usually about 45 minutes, up to 3.3 hours"
    assert PO.minutes_words(None, 20) == "up to about 20 minutes"
    assert PO.minutes_words(20, 20) == "up to about 20 minutes"
    assert PO.minutes_words(5, None) == ""
    # the usual time is the measured whole-run rate; the upper end the part's slow rate (PLAN-fea-13)
    row = PO.row({"faces": {}}, _plan(6_000_000), PO.FULL)
    assert row["usual_minutes"] < row["minutes"]


def _choose_stub(monkeypatch, full: dict, quick: dict, calls: list):
    def choose(record, surface, diag_m, *, ai=None, cap=None, **_kw):
        calls.append(cap)
        return dict(quick if cap is not None else full)
    monkeypatch.setattr(FD, "choose_plan", choose)
    monkeypatch.setattr(FD, "_restate", lambda record, plan: plan)


@pytest.mark.parametrize("quick_short,expected", [(("corner", "round"), "quicker"), (("corner", "fillet"), "full")])
def test_with_nothing_picked_the_run_takes_the_recommended_mesh_and_says_so(monkeypatch, quick_short, expected):
    """THE RUN NEVER WAITS (Areen, 28 Oct): no pick (the stage had not sized it, or no answer came) is the
    recommended mesh, decided by the run from its own plans by the stage's rule, and said."""
    calls: list = []
    full, quick = _plan(25_000_000), _plan(3_000_000, quick_short)
    _choose_stub(monkeypatch, full, quick, calls)
    plan, said = FD.size_choice({}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0, ai=None, kept=None, key="k")
    assert plan["estimate"]["elements"] == (3_000_000 if expected == "quicker" else 25_000_000)
    assert said["value"] == expected and said["inputs"]["picked"] is False and said["rule"] == "PLAN-fea-22"
    assert said["words"].startswith("No size was picked before the run started, so the run took the recommended mesh")
    assert calls == [None, PO.standard_elements()]


def test_nothing_to_choose_is_the_full_plan_and_nothing_said(monkeypatch):
    calls: list = []
    _choose_stub(monkeypatch, _plan(1_000_000), _plan(900_000), calls)
    plan, said = FD.size_choice({}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0, ai=None, kept=None, key="k")
    assert plan["estimate"]["elements"] == 1_000_000 and said == {} and calls == [None]


@pytest.mark.parametrize("pick", ["full", "quicker"])
def test_the_pick_is_planned_and_said(monkeypatch, pick):
    calls: list = []
    _choose_stub(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("corner",)), calls)
    plan, said = FD.size_choice({"fea_plan_choice": pick}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0,
                                ai=None, kept=None, key="k")
    assert said["value"] == pick and said["inputs"]["picked"] is True
    assert calls == ([PO.standard_elements()] if pick == "quicker" else [None])


def test_the_stages_plans_are_reused_only_for_the_same_solid_and_code(monkeypatch):
    """PLANNED ONCE: the run reuses the plans the stage kept when they were made for this staged solid and code (and
    the AI changed nothing); otherwise it plans itself."""
    calls: list = []
    _choose_stub(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("round",)), calls)
    kept = {"key": "same", "full": _plan(24_000_000), "quicker": _plan(2_900_000, ("round",))}
    plan, said = FD.size_choice({}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0, ai=None, kept=kept, key="same")
    assert calls == [] and plan["estimate"]["elements"] == 2_900_000 and said["inputs"]["reused_stage_plan"] is True
    plan, said = FD.size_choice({}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0, ai=None, kept=kept,
                                key="other")
    assert calls == [None, PO.standard_elements()] and said["inputs"]["reused_stage_plan"] is False
    # an AI decision that changes a size: the stage planned without it, so the run plans itself
    calls.clear()
    monkeypatch.setattr(FD, "_ai_changes_nothing", lambda record, ai, diag_m: (False, []))
    used_ai = {"status": "used", "decision": {"zones": {"H1": {"priority": "refine"}}}}
    FD.size_choice({"fea_plan_choice": "full"}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0, ai=used_ai,
                   kept=kept, key="same")
    assert calls == [None]
    # a lever (a later pass) never reuses them
    calls.clear()
    monkeypatch.setattr(FD, "_ai_changes_nothing", lambda record, ai, diag_m: (True, []))
    FD.size_choice({"fea_plan_choice": "full"}, {"faces": {}}, None, 0.1, levers=[{"lever": "x"}], ceiling_s=1.0,
                   ai=None, kept=kept, key="same")
    assert calls == [None]


@pytest.mark.parametrize("pick", ["", "full", "quicker"])
@pytest.mark.parametrize("asked", ["count", "sizes"])
def test_a_count_or_sizes_the_user_named_set_the_mesh_over_the_size_choice(monkeypatch, pick, asked):
    """PLAN-fea-27/29 over PLAN-fea-22: the stage's two sizes were made without the user's own count or sizes, so
    neither is offered or reused - the plan is the user's (no cap), and a pick made on the stage is said set aside."""
    seen: list = []

    def choose(record, surface, diag_m, *, ai=None, cap=None, target=None, sizes=None, correction=None, **_kw):
        seen.append({"cap": cap, "target": target, "sizes": sizes, "correction": correction})
        return _plan(7_000_000)
    monkeypatch.setattr(FD, "choose_plan", choose)
    kept = {"key": "same", "full": _plan(24_000_000), "quicker": _plan(2_900_000, ("round",))}
    target = {"elements": 7_000_000, "mode": "about", "source": "ai"} if asked == "count" else None
    sizes = {"Part_1": 0.002} if asked == "sizes" else None
    plan, said = FD.size_choice({"fea_plan_choice": pick} if pick else {}, {"faces": {}}, None, 0.1, levers=[],
                                ceiling_s=1.0, ai=None, kept=kept, key="same", target=target, sizes=sizes,
                                correction=1.1)
    assert plan["estimate"]["elements"] == 7_000_000
    assert seen == [{"cap": None, "target": target, "sizes": sizes, "correction": 1.1}]
    if pick:
        assert said["rule"] == "PLAN-fea-22" and said["inputs"]["set_aside"] == pick
        assert said["words"].endswith("so the size picked on the stage is not used.")
    else:
        assert said == {}


def test_a_count_the_notes_held_but_nobody_read_leaves_the_size_choice_in_force(monkeypatch):
    calls: list = []
    _choose_stub(monkeypatch, _plan(25_000_000), _plan(3_000_000, ("corner",)), calls)
    plan, said = FD.size_choice({"fea_plan_choice": "quicker"}, {"faces": {}}, None, 0.1, levers=[], ceiling_s=1.0,
                                ai=None, kept=None, key="k", target={"unread": True})
    assert said["value"] == "quicker" and calls == [PO.standard_elements()]


def test_an_ai_decision_that_changes_nothing_keeps_the_stages_plans():
    record = {"faces": {}}
    assert FD._ai_changes_nothing(record, None, 0.1) == (True, [])
    assert FD._ai_changes_nothing(record, {"status": "skipped"}, 0.1) == (True, [])
    refine = {"status": "used", "decision": {"zones": {"H1": {"priority": "refine"}}}}
    assert FD._ai_changes_nothing(record, refine, 0.1)[0] is False


def test_the_plans_key_is_the_staged_solid_the_answers_and_the_code(tmp_path):
    (tmp_path / "geometry.brep").write_bytes(b"solid A")
    a = PO.plans_key(tmp_path, connection="bonded", gap_joins=True)
    assert a == PO.plans_key(tmp_path, connection="bonded", gap_joins=True)
    assert a != PO.plans_key(tmp_path, connection="contact", gap_joins=True)
    assert a != PO.plans_key(tmp_path, connection="bonded", gap_joins=False)
    (tmp_path / "geometry.brep").write_bytes(b"solid B")
    assert a != PO.plans_key(tmp_path, connection="bonded", gap_joins=True)
    assert PO.kept_plan({"key": "k", "full": {"x": 1}}, "k", "full") == {"x": 1}
    assert PO.kept_plan({"key": "k", "full": {"x": 1}}, "j", "full") is None
    assert PO.kept_plan(None, "k", "full") is None


def test_the_measured_memory_is_recorded_against_the_model():
    got = FD.memory_measured({"quality": {"peak_rss_mb": 20 * 1024, "cells": 5_000_000}},
                             {"estimate": {"elements": 5_400_000}})
    assert got["peak_gib"] == 20.0 and got["elements"] == 5_000_000
    assert got["bytes_per_element"] == round((20 * 1024 - FD.MEMORY_BASE_MB) * 1024 * 1024 / 5_000_000)
    assert got["model_bytes_per_element"] == FD.BYTES_PER_ELEMENT and got["planned_elements"] == 5_400_000
    assert FD.memory_measured({"quality": {}}, {}) is None


def test_the_sizing_has_its_own_time_beyond_the_naming(monkeypatch):
    from meshpipeline.application import geometry_check as GC
    monkeypatch.setattr(GC, "naming_limits_for", lambda _s: (600, 900))
    soft, hard = GC.naming_task_limits("s")
    assert soft == 600 + int(PO.BUDGET_S + GC.SIZING_MARGIN_S) and hard == 900 + int(PO.BUDGET_S + GC.SIZING_MARGIN_S)
