# Responsibility: Prove the snap grid's electronics-cooling resolution rules: what a placed model needs resolved is read from its geometry and physics (never its names), each detail level asks for it, the grid gives it - measured off the grid lines - and a budget that cannot afford it relaxes in order and says so.
from __future__ import annotations

import math

import pytest
from tests.unit.cad.ecxml_models import Ecxml, set_top_box, standard_materials

from meshpipeline.cad.ingest.ecxml import read_ecxml
from meshpipeline.cad.ingest.ecxml_place import place
from meshpipeline.engines.snapgrid import blocks as B
from meshpipeline.engines.snapgrid import grid as G
from meshpipeline.engines.snapgrid import runner as R
from meshpipeline.engines.snapgrid import thermal_rules as TR


def _place(doc: Ecxml):
    return place(read_ecxml(doc.xml()))


def _level(name: str, **over) -> G.GridPlan:
    return R.grid_plan({**(R.plan_for(name) or {}), **over})


def _finned(name: str = "Board with a finned sink", *, fins: int = 5, gap: float = 0.002,
            fin: float = 0.001, renamed: bool = False) -> Ecxml:
    """A powered chip under a plate-fin heat sink on a board, in open air."""
    doc = standard_materials(Ecxml(name))
    doc.domain((-0.02, -0.02, -0.01), (0.08, 0.06, 0.05), ambient=(308.15, 308.15, 0.0))
    doc.pcb("Q" if renamed else "Board", (0.0, 0.0, 0.0), (0.04, 0.02, 0.0016), "+xy",
            "FR4 board")
    top = 0.0016
    doc.block("Zz" if renamed else "Chip", (0.01, 0.005, top), (0.012, 0.01, 0.001), "Silicon",
              4.0)
    pitch = fin + gap
    width = fins * fin + (fins - 1) * gap
    parts = [("Base", (0.01, 0.005, top + 0.001), (width, 0.01, 0.0015), "Aluminium 6063", 0.0)]
    parts += [(f"Fin {i}", (0.01 + i * pitch, 0.005, top + 0.0025), (fin, 0.01, 0.01),
               "Aluminium 6063", 0.0) for i in range(fins)]
    doc.heatsink("Yy" if renamed else "Sink", parts)
    doc.block("Cold part", (0.03, 0.005, top), (0.004, 0.004, 0.001), "Mold compound", 0.0)
    return doc


# ------------------------------------------------------------------------------ the facts -----
def test_heat_sources_and_what_carries_their_heat_are_found_from_physics_not_names():
    for renamed in (False, True):
        pl = _place(_finned(renamed=renamed))
        f = TR.facts(pl)
        kinds = {pl.parts[k].kind: why for k, why in f.heated.items()}
        names = {pl.names[k] for k in f.heated}
        # the chip makes heat; the board under it and the heat sink on it carry it out; the
        # unpowered part on the board is no heated surface
        assert len(f.powered) == 1 and "heatsink" in kinds and "printedCircuitBoard" in kinds
        assert not any("Cold" in n for n in names)
        # every boundary layer is a few millimetres for parts of centimetres in still air
        assert all(0.001 < d < 0.03 for d in f.layer_m.values()), f.layer_m


def test_fin_channels_are_air_gaps_and_a_part_sitting_between_two_faces_is_not():
    pl = _place(_finned(gap=0.002))
    f = TR.facts(pl)
    sink = next(k for k, p in enumerate(pl.parts) if p.kind == "heatsink")
    channels = [g for g in f.gaps if g["parts"] == (sink, sink)]
    assert len(channels) == 4 and all(g["width"] == pytest.approx(0.002) for g in channels)
    # here the base overhangs the chip by 1 mm: the air under the overhang is a real gap
    board = next(k for k, p in enumerate(pl.parts) if p.kind == "printedCircuitBoard")
    assert any(set(g["parts"]) == {board, sink} and g["axis"] == 2 for g in f.gaps)
    # in the set-top box the SoC fills the space between the board and the base whole: no gap
    stb = _place(set_top_box())
    fs = TR.facts(stb)
    pcb = next(k for k, p in enumerate(stb.parts) if p.kind == "printedCircuitBoard")
    hs = next(k for k, p in enumerate(stb.parts) if p.kind == "heatsink")
    assert not any(set(g["parts"]) == {pcb, hs} for g in fs.gaps)


def test_the_fan_flow_sets_the_boundary_layer_and_still_air_its_buoyancy():
    still = TR.boundary_layer(0.02, 0.0)
    fanned = TR.boundary_layer(0.02, 2.0)
    assert fanned < still < 0.02                       # a faster flow, a thinner layer
    # laminar flat plate: 5 L / sqrt(Re) / Pr^(1/3)
    re = 2.0 * 0.02 / TR.AIR_NU
    assert fanned == pytest.approx(5 * 0.02 / math.sqrt(re) / TR.AIR_PR ** (1 / 3))
    pl = _place(set_top_box())
    f = TR.facts(pl)
    assert f.velocity_m_s > 0 and "Exhaust fan" in f.velocity_why


# ------------------------------------------------------------------------------ the grid ------
def test_standard_puts_the_asked_cells_across_fins_gaps_and_heat_sources(tmp_path):
    pl = _place(_finned())
    plan = _level("standard")
    lay = B.build_layout(pl, plan)
    res = TR.audit(pl, lay, lay.plan, plan)
    assert res["measured"] and res["short"] == [], res["short"]
    gaps = res["rows"]["gap"]
    assert gaps and min(r["got"] for r in gaps) >= plan.gap_cells
    assert res["rows"]["band"]          # (the chip is wide enough for the background alone)
    # the old plain plan (no rules) leaves the fin channels with fewer cells
    bare = G.GridPlan(max_cells=plan.max_cells, min_cells_across=2)
    old = B.build_layout(pl, bare)
    measured = TR.audit(pl, old, old.plan, plan)
    assert min(r["got"] for r in measured["rows"]["gap"]) < plan.gap_cells
    assert measured["short"], "the old grid must be measured short, never passed"


def test_every_level_is_finer_than_the_one_below_it():
    pl = _place(_finned())
    cells = [B.build_layout(pl, _level(k)).n_cells for k in ("draft", "standard", "max")]
    assert cells[0] < cells[1] < cells[2]


def test_a_tight_budget_relaxes_the_least_important_rule_first_and_says_so():
    pl = _place(_finned())
    plan = _level("standard")
    full = B.build_layout(pl, plan).n_cells
    lay = B.build_layout(pl, _level("standard", max_cells=int(full * 0.6)))
    assert lay.n_cells <= int(full * 0.6)
    words = " ".join(lay.relaxed)
    assert "to fit the cell budget" in words or "background cell size" in words
    # cells through a part are the last to go: they are kept as asked here
    assert lay.plan.min_cells_across == plan.min_cells_across
    assert lay.rules_budget == 0 or lay.rules_budget >= full * 0.6


def test_the_relaxation_ladder_keeps_layers_and_gaps_longest():
    plan = _level("standard")
    steps = G.relaxations(plan)
    first_layer = next(i for i, p in enumerate(steps) if p.min_cells_across < plan.min_cells_across)
    first_gap = next(i for i, p in enumerate(steps) if p.gap_cells < plan.gap_cells)
    first_band = next(i for i, p in enumerate(steps) if p.band_cells < plan.band_cells)
    first_growth = next(i for i, p in enumerate(steps) if p.growth > plan.growth)
    assert first_band < first_growth < first_gap < first_layer
    assert all(p.min_cells_across >= 1 for p in steps)
    # each step changes one thing, and every change is said in words
    for a, b in zip(steps, steps[1:]):
        assert G._relaxed_words(plan, b), b


def test_the_detail_plans_follow_the_published_practice():
    std, fine = _level("standard"), _level("max")
    assert std.gap_cells >= 3 and fine.gap_cells >= 5     # Flotherm: 3 across a fin gap, 5 for dp
    assert std.opening_cells >= 4                          # Icepak: 4-5 on every fan and vent
    assert std.growth <= 1.3 and fine.growth < std.growth
    assert R.cli_args(R.plan_for("standard"))[:2] == ["--max-cells", "4000000"]
    assert "--gap-cells" in R.cli_args(R.plan_for("standard"))


# ------------------------------------------------------------------------------ the report ----
def test_the_mesh_report_and_quality_carry_the_measured_rules(tmp_path):
    from meshpipeline.engines.snapgrid.mesher import mesh_ecxml

    src = tmp_path / "m.ecxml"
    src.write_bytes(_finned().xml())
    rep = mesh_ecxml(src, tmp_path / "case", _level("standard"), dry_run=True,
                     memory_bytes=16e9).report
    res = rep["resolution"]
    assert res["measured"] and res["short"] == [] and res["summary"]
    assert any("across every air gap" in r for r in res["rules"])
    q = R._resolution_quality(rep)
    assert q["resolution_short"] == [] and q["resolution_checked"] > 0
    # a report that never measured it gives the reviewer nothing to pass
    assert R._resolution_quality({"resolution": {"measured": False}}) == {}
    from meshpipeline.engines.snapgrid.criteria import CRITERIA_ROWS
    crit = next(c for c in CRITERIA_ROWS if c.key == "resolution_short")
    assert crit.gating is False
    assert crit.evaluate({}) is None                       # unmeasured: no pass
    assert crit.evaluate({"resolution_short": ["gap between A and B: 2 of 4 cells"]}) is False
    assert crit.evaluate({"resolution_short": []}) is True
