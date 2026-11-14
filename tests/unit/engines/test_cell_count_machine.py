# Responsibility: Verify THE USER'S CELL COUNT DECIDES (Areen, 14 Nov: "if the person writes 16 M in the notes, move to the big machine; if they say 5 M, we don't; if they say nothing, mesh how we normally mesh") - CFDREQ-3 x RUN-9: a stated count is the plan's budget and its limit, under the standard machine it runs there, past it the plan is written to run on a VM that carries it before anything is meshed, with no question; nothing said changes nothing.
# Boundaries: contracts/mesh_machine (for_cells, cells_carried, route_cells), engines/cfd_requirements.Ask (budget, cell_limit), the snappy driver's seam (_ask_budget, _plan_limit, _route_big), snappy's body-gap plan and cfMesh's machine_for_plan - pure, on tmp workspaces. No VM is started and no mesher runs.
from __future__ import annotations

import json

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts import mesh_machine as MM
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


@pytest.fixture(autouse=True)
def _standard_machine(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)


def _ask(count: float, what: str = "target") -> CR.Ask:
    quote = f"about {count / 1e6:g} million cells"
    req, dropped, _na = MR.check(json.dumps({"stated_unit": "none", "sizes": [], "element_kind": [], "limits": [],
                                             "other": [], "counts": [{"what": what, "value": count,
                                                                      "quote": quote}]}),
                                 quote, route=MR.FLOW)
    assert dropped == []
    return CR.Ask(req, "snappy")


# ------------------------------------------------------------------ RUN-9: the machine a count needs -------
def test_a_count_within_the_standard_machine_stays_there_and_past_it_goes_to_a_vm_that_carries_it():
    assert MM.normal_cells() == 8_000_000
    assert MM.for_cells(5e6)["where"] == "cloud_run" and MM.for_cells(8e6)["where"] == "cloud_run"
    big = MM.for_cells(16e6)
    assert big["where"] == "gce" and big["machine"] == "n2-highmem-8"
    assert "more than the 8 M our standard machine is sized for" in big["words"]
    assert MM.cells_carried(big) >= 16_000_000 > MM.cells_carried(None) == 8_000_000
    # 200 M cells (a semiconductor lead's ask) still fits a machine we run
    huge = MM.for_cells(2e8)
    assert huge["where"] == "gce" and not huge["over_ceiling"] and MM.cells_carried(huge) >= 2e8
    assert MM.ceiling_cells() == MM.cells_carried(MM.for_cells(1e12)) > 2e8
    assert MM.for_cells(1e12)["over_ceiling"] and "largest machine we run" in MM.for_cells(1e12)["words"]


def test_route_cells_writes_the_vm_only_for_a_plan_past_the_standard_machine(tmp_path):
    assert MM.route_cells(tmp_path, 5e6) is None and MM.read(tmp_path) is None     # nothing written: as always
    moved = MM.route_cells(tmp_path, 16e6)
    assert moved["where"] == "gce" and MM.read(tmp_path) == moved
    # a bigger machine already chosen (a run that stepped up after running out of memory) is kept
    up = MM.step_up(moved)
    MM.write(tmp_path, up)
    assert MM.route_cells(tmp_path, 16e6) == up


# ------------------------------------------------------------------ CFDREQ-3: the user's count wins --------
def test_a_count_past_the_standard_machine_is_the_budget_and_is_said_to_run_on_a_bigger_one():
    ask = _ask(16e6)
    assert ask.budget(4_000_000, hard=MM.ceiling_cells()) == 16_000_000
    row = next(iter(ask.rows.values()))
    assert row["status"] == "applied" and "the cell budget is set to 16 million" in row["built"]
    assert "more than our standard machine is sized for, so it is meshed on a bigger one" in row["built"]
    # an engine that cannot move it holds it to the standard machine, and says so plainly
    ask = _ask(16e6)
    assert ask.budget(4_000_000) == 8_000_000
    assert "the most this mesher builds here is 8 million cells" in next(iter(ask.rows.values()))["built"]


def test_a_small_count_is_the_budget_on_the_standard_machine_with_nothing_more_said():
    ask = _ask(3e6)
    assert ask.budget(8_000_000, hard=MM.ceiling_cells()) == 3_000_000
    built = next(iter(ask.rows.values()))["built"]
    assert built == "the cell budget is set to 3 million"


def test_the_users_count_is_the_plans_limit_so_our_gap_boxes_never_raise_past_it():
    assert _ask(3e6).cell_limit(8_000_000) == 3_000_000
    assert _ask(16e6).cell_limit(8_000_000) == 16_000_000
    assert _ask(5e6, "ceiling").cell_limit(8_000_000) == 5_000_000
    assert _ask(2e6, "floor").cell_limit(8_000_000) == 8_000_000
    assert _ask(12e6, "floor").cell_limit(8_000_000) == 12_000_000


# ------------------------------------------------------------------ the snappy seam ------------------------
def test_snappy_plans_the_users_count_and_routes_it_before_meshing(tmp_path):
    from meshpipeline.engines.snappy import drivers as D
    ask = _ask(16e6)
    assert D._ask_budget(ask, 4_000_000, None) == 16_000_000
    assert D._plan_limit(ask) == 16_000_000 and D._plan_limit(None) is None
    moved = D._route_big(tmp_path, 16_000_000, job_id="", attempt=1)
    assert moved["where"] == "gce" and MM.read(tmp_path)["machine"] == moved["machine"]
    # the founder's 3 M: the standard machine, nothing written
    small = _ask(3e6)
    assert D._ask_budget(small, 8_000_000, None) == 3_000_000 and D._plan_limit(small) == 3_000_000
    other = tmp_path / "small"
    other.mkdir()
    assert D._route_big(other, 3_000_000) is None and MM.read(other) is None


def test_an_overshoot_of_a_big_count_is_corrected_against_the_count_not_the_standard_machine():
    from meshpipeline.engines.snappy import drivers as D
    # the last pass asked 16 M and made 17 M: inside what the machine for 16 M carries, so nothing to correct
    # (against the standard machine's 8 M it was cut to 12.8 M)
    assert D._ask_budget(_ask(16e6), 4_000_000, (16e6, 17e6)) == 16_000_000
    # ... but a pass that ran away past that machine is brought back
    assert D._ask_budget(_ask(16e6), 4_000_000, (16e6, 40e6)) < 16_000_000
    # a count within the standard machine is corrected against it, as always
    assert D._ask_budget(_ask(3e6), 4_000_000, (3e6, 9e6)) < 3_000_000


def test_the_body_gap_plan_holds_to_the_users_count():
    from tests.unit.engines.test_gap_sizing import _two_blocks

    from meshpipeline.cad.shape_traits import body_gap_field
    from meshpipeline.engines import gap_sizing as G
    from meshpipeline.engines.snappy.snappy_runner import _plan_body_gaps
    field = body_gap_field(_two_blocks(0.002), samples=4000)
    # ours: the gap boxes raise the ceiling past the plan's own budget, within the standard machine
    ours, raised = _plan_body_gaps(field, wall_cell=0.005, budget=2_000, base_cells=1_500.0)
    assert ours.status == G.RAISED and ours.limit == 8_000_000 and raised > 3_000
    # the user asked for about 3,000: the boxes never take the plan past it
    theirs, ceiling = _plan_body_gaps(field, wall_cell=0.005, budget=2_000, base_cells=1_500.0, limit=3_000)
    assert theirs.limit == 3_000 and ceiling <= 3_000 and theirs.status != G.RAISED


def test_cfmesh_runs_a_users_count_past_the_standard_machine_on_a_vm(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    plan = {"estimated_cells": 15_000_000, "estimated_seconds": 60.0}
    d = R.machine_for_plan(tmp_path, plan, user_cells=16_000_000)
    assert d["where"] == "gce" and d["why"] == "the cell count asked for"
    # our own plan of the same size is only moved for time, as before
    other = tmp_path / "ours"
    other.mkdir()
    assert R.machine_for_plan(other, plan)["where"] == "cloud_run"
    assert R.machine_for_plan(other, plan, user_cells=5_000_000)["where"] == "cloud_run"


def test_the_delivered_notes_name_the_users_count_and_how_to_ask_for_more():
    from meshpipeline.engines import resolution_policy as RP
    short = {"kind": RP.RESOLUTION_SHORT, "cells_across": 9.0, "floor": 12.0,
             "plan": {"status": "limited", "route_big": True, "needed": 21e6, "limit": 8e6}}
    said = " ".join(RP.note_lines(short))
    assert "Ask for about 21 M cells in your notes and it is meshed on a bigger machine." in said
    theirs = " ".join(RP.note_lines({**short, "plan": {**short["plan"], "limit": 3e6}}))
    assert "more than the 3 M you asked for" in theirs and "bigger machine" not in theirs
    over = " ".join(RP.note_lines({"kind": RP.OVER_CELL_LIMIT, "cells": 8.6e6, "limit": 8e6}))
    assert "8.6 M cells" in over and "the size your geometry needs" not in over
    # meshed on a bigger machine sized for the user's 16 M: one plain line, no "standard job"
    big = RP.note_lines(RP.caveat("cell_limit", {"cells": 16.2e6, "limit": 8e6, "machine_cells": 23e6}, None))
    assert big == ["  - The mesh has 16.2 M cells. Make sure your solver machine has the memory for it."]


def test_the_cell_limit_gate_still_notes_a_big_mesh_so_the_worker_never_solves_it(tmp_path):
    # a 16 M mesh from the big machine is still over the standard machine's cells: the gate's note is what keeps
    # the worker (8 GiB) from running its trial solve on it (pipeline/executor, LAD-cfd-4)
    from types import SimpleNamespace

    from meshpipeline.engines import resolution_policy as RP
    MM.write(tmp_path, MM.for_cells(16e6))
    ctx = SimpleNamespace(workspace=tmp_path, manifest_or_load=lambda: {"cell_count": 16_200_000})
    ok, fb = RP.gate_cell_limit(ctx)
    from meshpipeline.engines.gates import facts_of
    assert not ok and facts_of(fb)["machine_cells"] >= 16_200_000


def test_nothing_said_changes_nothing(tmp_path):
    # no count in the notes: no Ask, no limit, nothing written - the run is the one it always was
    from meshpipeline.engines.snappy import drivers as D
    assert CR.Ask.of(tmp_path, "snappy") is None and D._plan_limit(None) is None
    assert D._route_big(tmp_path, 8_000_000) is None and not (tmp_path / MM.MACHINE_FILE).exists()
