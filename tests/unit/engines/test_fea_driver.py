# Responsibility: Verify the FEA driver end to end on small parts of known geometry: the plan is measured off the staged solid, gmsh meshes it as planned (within its estimate, every planned rule met on the mesh, every face named), the same plan gives the same mesh twice, a plan over its level stops before meshing with its options, a file of two bodies is meshed as an assembly (tests/unit/engines/test_fea_assembly.py), and only a 3D structural solid takes this driver.
# Boundaries: parts are built in the test (tests/fea_fixtures.py) and meshed in-process at a small made-up level, so the suite stays quick; the real levels are the lab's (~/fea4_lab).
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.contracts import decision_record as dr  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """Stage a part as the builder does for gmsh: the upload read in mm, written in metres."""
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


def _plan(ws: Path, **kw) -> dict:
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.make_plan(record, surface, diag, **kw)
    FD.write_plan(ws, plan)
    return plan


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    ws = _stage(fx.plate_with_hole(), tmp_path_factory.mktemp("fea_plate"))
    plan = _plan(ws)
    assert fea_adapter.mesh(ws) == 0
    return ws, plan, json.loads((ws / "quality.json").read_text())


def test_the_plan_reads_the_part_in_metres(plate):
    _ws, plan, _q = plate
    hole = next(z for z in plan["zones"] if z["kind"] == "hole")
    assert hole["radius_m"] == pytest.approx(fx.PLATE["hole_radius"] * 1e-3, rel=1e-3)
    wall = min((z for z in plan["zones"] if z["kind"] == "wall"), key=lambda z: z["thickness_m"])
    assert wall["thickness_m"] == pytest.approx(fx.PLATE["thickness"] * 1e-3, rel=1e-3)
    # PLAN-fea-14: the base is a tenth of the part's diagonal (the 120 x 60 x 4 mm plate)
    assert plan["fits"] and plan["base_size_m"] == pytest.approx((120**2 + 60**2 + 4**2) ** 0.5 / 1e3 / P.MIN_DIVISIONS, rel=1e-3)


def test_the_mesh_comes_out_near_its_estimate(plate):
    _ws, plan, q = plate
    assert 0.65 * plan["estimate"]["elements"] <= q["cells"] <= 1.35 * plan["estimate"]["elements"]
    assert q["cells"] <= plan["limit"]["elements"]


def test_every_planned_rule_is_met_on_the_mesh(plate):
    _ws, _plan_, q = plate
    rules = q["fea"]["rules"]
    assert rules["short"] == []
    assert rules["through_wall"]["min"] >= P.WALL_ELEMENTS * 0.9
    assert q["fea"]["through_wall_min"] == rules["through_wall"]["min"]
    assert q["min_sicn"] >= 0.1 and q["quality_bar"] == "fea"


def test_every_cad_face_is_named_in_the_deck(plate):
    ws, plan, _q = plate
    deck = (ws / "mesh.inp").read_text()
    assert "*ELEMENT, TYPE=C3D10" in deck.upper().replace("TYPE =", "TYPE=")
    for k in range(1, plan["measure"]["faces"] + 1):
        assert f"*SURFACE, NAME=Face_{k}," in deck


def test_the_same_plan_gives_the_same_mesh(plate, tmp_path):
    ws, _plan_, _q = plate
    again = _stage(fx.plate_with_hole(), tmp_path / "again")
    plan2 = _plan(again)
    assert json.loads((ws / P.PLAN_FILE).read_text()) == plan2
    assert fea_adapter.mesh(again) == 0
    digest = [hashlib.sha256((w / "mesh.inp").read_bytes()).hexdigest() for w in (ws, again)]
    assert digest[0] == digest[1]


def test_over_the_limit_a_plan_does_not_fit_and_offers_no_stop(tmp_path):
    ws = _stage(fx.plate_with_hole(), tmp_path / "tiny")
    plan = _plan(ws, limit=300)
    assert not plan["fits"] and "options" not in plan


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws: Path, monkeypatch):
    from meshpipeline.engines import runtime

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    return asyncio.run(FD.drive(ws, {}, job_id=f"t-{ws.name}", publish=None, run=_Run()))


def test_the_driver_meshes_and_cites_every_rule(tmp_path, monkeypatch):
    ws = _stage(fx.bracket(fillet=None), tmp_path / "bracket")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    cited = {e["rule"] for e in dr.cited_for(f"t-{ws.name}")}
    assert "PLAN-fea-17" not in cited                        # one part: no assembly
    assert {"PLAN-fea-1", "PLAN-fea-14", "PLAN-fea-5", "PLAN-fea-8", "PLAN-fea-13", "LAD-fea-5"} <= cited
    passes = json.loads((ws / "fea_passes.json").read_text())
    assert passes["delivered"] == 1 and passes["levers"] == []


def test_two_bodies_are_meshed_as_an_assembly_never_stopped(tmp_path, monkeypatch):
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.two_blocks(), tmp_path / "two")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    assert read_refusal(ws) is None
    assert json.loads((ws / "fea_plan.json").read_text())["assembly"]["parts"][1]["id"] == "Part_2"


@pytest.mark.parametrize("state, takes", [
    ({"purpose": "structural", "dimensionality": "3D", "intake_patches": []}, True),
    ({"purpose": "structural"}, True),
    ({"purpose": "structural", "dimensionality": "2D"}, False),
    ({"purpose": "structural", "intake_patches": [{"name": "fixed end", "type": "fixed"}]}, False),
    ({"purpose": "internal_cfd", "flow_topology": "internal"}, False),
    ({"purpose": "external_cfd"}, False),
])
def test_only_a_3d_structural_solid_takes_the_fea_driver(state, takes):
    from meshpipeline.engines.registry import get_spec

    driver = get_spec("gmsh").build_driver_for(state)
    assert (driver is FD.drive) is takes
    if not takes:
        assert driver is None            # the builder loop, as before


def test_no_other_engine_gains_a_driver():
    from meshpipeline.engines.registry import ENGINE_CATALOG

    state = {"purpose": "structural"}
    for name, spec in ENGINE_CATALOG.items():
        if name != "gmsh":
            assert spec.build_driver_for(state) is spec.build_driver


def test_a_guard_trip_pulls_no_lever_and_ends_the_attempt(tmp_path, monkeypatch):
    """The clock only guards (D3): a run past its guard is our infrastructure, never a reason to
    change the plan - no lever is pulled and nothing is delivered from it."""
    from meshpipeline.engines import runtime

    ws = _stage(fx.plate_with_hole(), tmp_path / "trip")
    calls = []
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": -1, "timed_out": True,
                                                                         "stalled": True}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-trip",
                                          publish=None, run=_Run()))
    assert not ok and marker == FD.GUARD_TRIP and len(calls) == 1
    rec = json.loads((ws / "fea_passes.json").read_text())
    assert rec["levers"] == [] and rec["guard_trip"]["unit"] == "elements"
    # the hard ceiling is what the mesher gets - never a guess at how long it should take
    assert calls[0] == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S
    assert not any(e["rule"].startswith("LAD-fea-") and e["decided"] == "lever" for e in dr.cited_for("t-trip"))
    from meshpipeline.engines.preflight import read_refusal

    ref = read_refusal(ws)
    assert ref.gate == FD.GUARD_GATE and ref.cause == "run_infrastructure" and ref.facts["ours"]


def test_a_pass_still_working_at_the_cloud_run_wall_runs_again_on_a_vm(tmp_path, monkeypatch):
    """RUN-5: a pass that met the Cloud Run job's wall STILL WORKING (not stalled) is never ended
    for it: the same plan runs again on a VM, which has no wall - its ceiling is the VM's backstop,
    not the job's limit, and nothing is coarsened or levered."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import runtime

    ws = _stage(fx.plate_with_hole(), tmp_path / "wall")
    calls: list[tuple[int, str]] = []

    def _run(w, timeout):
        where = (MM.read(w) or {}).get("where")
        calls.append((timeout, where))
        if where == "cloud_run":
            return {"rc": -1, "timed_out": True}                  # the wall, still working
        return {"rc": fea_adapter.mesh(w)}                        # on the VM it finishes

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=_run))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-wall", publish=None, run=_Run()))
    assert ok, marker
    assert calls == [(FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S, "cloud_run"), (FD.VM_CEILING_S, "gce")]
    moved = [e for e in dr.cited_for("t-wall") if e["rule"] == "RUN-5"]
    assert moved and moved[0]["value"] == MM.MACHINES[0].name
    assert not any(e["rule"].startswith("LAD-fea-") and e["decided"] == "lever" for e in dr.cited_for("t-wall"))


def test_a_plan_planned_past_the_cloud_run_wall_starts_on_a_vm(tmp_path, monkeypatch):
    """RUN-5 before meshing: a plan whose planned time passes the Cloud Run job's wall never wastes a
    first run there - it starts on a VM with the VM's ceiling."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines import runtime

    ws = _stage(fx.plate_with_hole(), tmp_path / "slow")
    seen: list[tuple[int, str]] = []

    def _run(w, timeout):
        seen.append((timeout, (MM.read(w) or {}).get("where")))
        return {"rc": fea_adapter.mesh(w)}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=_run))
    # the part meshes at one element an hour: hours past the wall
    monkeypatch.setattr(FD, "fill_rate", lambda _r: (1.0 / 3600.0, "complex"))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-slow", publish=None, run=_Run()))
    assert ok, marker
    assert seen[0] == (FD.VM_CEILING_S, "gce")
    assert any(e["rule"] == "RUN-5" for e in dr.cited_for("t-slow"))


def test_a_ladder_that_runs_out_says_so_for_the_retry_policy(tmp_path, monkeypatch):
    """Every pass the plan allows leaves a degenerate element: the attempt ends with the
    driver's own verdict - deterministic, the ladder exhausted, where the worst elements are - so
    no second attempt builds the same mesh (job ab20566f)."""
    from meshpipeline.contracts.failure_cause import retry_can_help
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.plate_with_hole(), tmp_path / "floor")

    def bad_pass(w, timeout):
        try:
            rc = fea_adapter.mesh(w)
        except Exception:  # noqa: BLE001 - as the mesh runner reports a pass that raised
            # the last rung's pass (LAD-fea-9): no fTetWild outside the mesh image
            return {"rc": 1}
        q = json.loads((Path(w) / "quality.json").read_text())
        q.update(min_sicn=0.0, elements_under_floor=5, fatal=["degenerate elements (SICN <= 0)"])
        q.setdefault("fea", {}).setdefault("quality", {})["worst_at_m"] = {"scaled_jacobian": [0.01, 0.02, 0.003]}
        (Path(w) / "quality.json").write_text(json.dumps(q))
        return {"rc": rc}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=bad_pass))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-floor",
                                          publish=None, run=_Run()))
    assert not ok and marker == FD.BUILD_FAILED
    ref = read_refusal(ws)
    assert ref.gate == FD.QUALITY_GATE and ref.cause == "mesh_quality"
    assert ref.facts["deterministic"] and ref.facts["ladder_exhausted"] and ref.facts["fea"]
    assert ref.facts["passes"] >= 2 and ref.facts["under_floor"] == 5 and ref.facts["worst_at_mm"] == [10.0, 20.0, 3.0]
    assert retry_can_help(ref.cause, ref.facts) is False


def test_a_small_part_is_measured_at_its_true_size(tmp_path):
    """The staged solid's scale is a power of ten: an 8 mm block reads 8 mm, not the 7.5 mm that
    OpenCASCADE's tolerance-padded box once made of a small part (an M5 nut's cusps came out 6% in)."""
    ws = _stage(fx._box(0, 0, 0, 8, 8, 8), tmp_path / "cube")
    record, _surface, diag = FD.measure_staged(ws)
    assert record["thickness"]["min_mm"] == pytest.approx(8.0, rel=0.005)
    assert diag == pytest.approx(8.0 * 3 ** 0.5 / 1000.0, rel=0.01)
    assert all(f["area_mm2"] == pytest.approx(64.0, rel=1e-6) for f in record["faces"].values())
# ------------------------------------------- PLAN-fea-8: under the limit, never a stop before meshing ----
def test_the_rules_own_plan_is_meshed_when_it_fits(tmp_path):
    ws = _stage(fx.plate_with_hole(), tmp_path / "fits")
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.choose_plan(record, surface, diag)
    assert plan["fits"] and not plan.get("coarsened") and "resolution_short" not in plan
    assert plan["limit"]["elements"] == FD.memory_elements(FD._ceiling_gib())


def test_over_the_limit_the_finest_zones_are_coarsened_and_what_is_short_is_named(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "coarse")
    record, surface, diag = FD.measure_staged(ws)
    monkeypatch.setattr(FD, "memory_elements", lambda memory_gib=None: 3_000)
    plan = FD.choose_plan(record, surface, diag)
    assert plan["fits"] and plan["coarsened"]["zone_floor_m"] > 0 and plan["zone_floors_m"]
    assert plan["resolution_short"] and all(isinstance(w, str) for w in plan["resolution_short"])
    assert any("more than the 3,000-element limit allows" in w for w in plan["words"])
    assert not any(w.startswith("Stopped") for w in plan["words"])
    # never past what keeps elements valid
    for z in plan["zones"]:
        keep = P.keep_size(z)
        assert keep is None or z["size_m"] <= keep * (1 + 1e-9) or z["size_m"] == z.get("rule_size_m")


def test_the_driver_never_stops_before_meshing_and_names_what_is_short(tmp_path, monkeypatch):
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal
    from meshpipeline.engines.verdict_limits import fea_limits

    ws = _stage(fx.plate_with_hole(), tmp_path / "never")
    monkeypatch.setattr(FD, "memory_elements", lambda memory_gib=None: 3_000)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-never", publish=None, run=_Run()))
    ref = read_refusal(ws)
    assert ref is None or ref.cause != "cell_budget"
    q = json.loads((ws / "quality.json").read_text())
    assert q["fea"]["coarsened"] and q["fea"]["resolution_short"]
    assert ok, marker                                   # delivered, short where named
    row = next(r for r in fea_limits(q) if r["key"] == "fea_resolution_short")
    assert row["measured"] == q["fea"]["resolution_short"]


def test_a_mesh_over_the_limit_after_meshing_is_planned_coarser():
    from meshpipeline.engines import fea_ladder as L

    plan = {"limit": {"elements": 1_000_000}}
    facts = L.pass_facts(1, 0, {"cells": 2_000_000, "min_sicn": 0.3, "fatal": [], "fea": {"rules": {"short": []}}}, plan)
    lever = L.next_lever(facts, [], plan)
    assert lever["lever"] == "LAD-fea-4" and lever["factor"] < 1 and L.coarse_factor([lever]) == lever["factor"]


# ------------------------------------- PLAN-fea-13: no guessed time limit, a progress watchdog ----
def test_the_planned_time_is_for_the_record_and_the_mesher_gets_the_hard_ceiling():
    plan = {"estimate": {"elements": 1_761_000}}
    w = FD.planned_work(plan, rate=587.0, cls="simple", ceiling_s=13800)
    assert w["planned_s"] == pytest.approx(3000.0, rel=1e-3) and w["class"] == "simple"
    assert w["ceiling_s"] == 13800 and w["stall_window_s"] == 900 and "guard_s" not in w


def test_the_watchdog_says_what_it_saw():
    stuck = {"pass": 2, "stalled": True, "stall_window_s": 900, "ceiling_s": 13800,
             "watchdog": {"seconds": 3000.0, "silence_at_stop_s": 905.0}}
    words = FD.watchdog_words(stuck)
    assert "no progress for 15 minutes" in words and "50 minutes of work on pass 2" in words
    ceiling = {"pass": 1, "stalled": False, "ceiling_s": 13800, "watchdog": {"seconds": 13800.0}}
    assert "still working when it reached the hard limit of 230 minutes" in FD.watchdog_words(ceiling)


def test_the_fill_rate_follows_the_parts_measured_complexity():
    simple = {"faces": {f"Face_{k}": {} for k in range(10)}, "hot_spots": [{}] * 3}
    moderate = {"faces": {f"Face_{k}": {} for k in range(90)}, "hot_spots": [{}] * 18}
    complex_ = {"faces": {f"Face_{k}": {} for k in range(10)}, "hot_spots": [], "micro": [{}]}
    rates = [FD.fill_rate(r) for r in (simple, moderate, complex_)]
    assert [c for _r, c in rates] == ["simple", "moderate", "complex"]
    assert rates[0][0] > rates[1][0] > rates[2][0]


def test_no_plan_is_cut_down_for_time():
    """Areen, 24 Oct: a guess at the mesher's speed never lowers the element limit - only memory does."""
    big = {"faces": {f"Face_{k}": {} for k in range(500)}, "hot_spots": [{}] * 300}
    assert FD.element_limit(big, 600.0) == FD.element_limit(big, None)
    assert FD.element_limit(big, 600.0)[0] == FD.memory_elements(FD._ceiling_gib())


def test_the_ceiling_is_the_machines_wall_never_what_the_attempt_has_left():
    """dev-areen 25 Oct: a pass still meshing was cut at "the hard limit of 110 minutes" and its retry
    got 1 minute - the attempt's clock, not the mesh. The ceiling is now only the wall of the machine
    the pass runs on; the pipeline's deadline moves on while the mesher makes progress."""
    import time as _t

    almost_out = SimpleNamespace(deadline_s=400.0, _started=_t.monotonic())
    assert FD.ceiling_for(almost_out) == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S
    assert FD.ceiling_for(SimpleNamespace()) == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S
    assert FD.ceiling_for(almost_out, {"where": "cloud_run"}) == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S
    assert FD.ceiling_for(almost_out, {"where": "gce", "machine": "n2-highmem-8"}) == FD.VM_CEILING_S
    # a VM's backstop stays under the worker task's own 24-hour limit
    src = (Path(__file__).resolve().parents[3] / "src/meshpipeline/adapters/pipeline_execution/celery.py").read_text()
    assert "soft_time_limit=86400" in src and FD.VM_CEILING_S < 86400
    # a MODEL answer is still waited for no longer than the attempt has left
    assert FD.ai_wait_s(almost_out) == 0.0
    assert FD.ai_wait_s(SimpleNamespace()) == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S - FD.AI_RESERVE_S


def test_coarsening_never_goes_past_what_keeps_elements_valid_zone_by_zone():
    wall = {"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 0.0002, "thickness_m": 0.0005}
    hole = {"id": "H1", "kind": "hole", "faces": ["Face_2"], "size_m": 0.0001, "radius_m": 0.002}
    out = {z["id"]: z for z in P.floored([wall, hole], 0.01)}
    assert out["W1"]["size_m"] == pytest.approx(0.0005)                     # 1 element through the wall
    assert out["H1"]["size_m"] == pytest.approx(P.QUARTER * 0.002 / 3)        # 3 per quarter-turn
    assert out["W1"]["kept_valid"] and out["W1"]["short"]
    strict = {z["id"]: z for z in P.floored([wall], 0.01, through=1.5)}
    assert strict["W1"]["size_m"] == pytest.approx(0.0005 / 1.5)


# ------------------------------------------------------------------- fix pack, 22 Oct 2026 ----
def test_an_unfinished_histogram_still_counts_what_it_has_not_resolved(tmp_path, monkeypatch):
    """fea0221: the octree stopped before any leaf and the estimate read 0 elements, so the time
    guard fell to its floor. Stopped short, the cubes not yet resolved are counted at their zones' size."""
    from meshpipeline.engines import fea_size_field as SF

    ws = _stage(fx.plate_with_hole(), tmp_path / "short")
    record, surface, diag = FD.measure_staged(ws)
    full = FD.make_plan(record, surface, diag)
    monkeypatch.setattr(SF, "MAX_CUBES", 20)
    monkeypatch.setattr(FD, "MAX_CUBES_CEILING", 20)
    cut = FD.make_plan(record, surface, diag)
    assert not cut["histogram_complete"] and cut["estimate"]["elements"] > 0
    assert full["histogram_complete"]


def test_a_count_that_ran_out_of_cubes_under_the_limit_is_counted_in_full_and_never_coarsened(tmp_path, monkeypatch):
    """PLAN-fea-8 (27 Oct): an unfinished count can never say a plan fits, so a plan whose count merely ran
    out of cubes was coarsened though it fitted (18 corpus plans; job bf863215's bolt fillets meshed at 3
    elements per quarter-turn instead of 6). Under the limit, it is counted again with more cubes."""
    from meshpipeline.engines import fea_size_field as SF

    ws = _stage(fx.plate_with_hole(), tmp_path / "recount")
    record, surface, diag = FD.measure_staged(ws)
    full = FD.choose_plan(record, surface, diag)
    monkeypatch.setattr(SF, "MAX_CUBES", 20)
    again = FD.choose_plan(record, surface, diag)
    assert again["histogram_complete"] and again["fits"] and not again.get("coarsened")
    assert again["estimate"] == full["estimate"] and again["zones"] == full["zones"]


def _kinds_plan(monkeypatch, need_wall: float, need_fillet: float, limit_share: float):
    """choose_plan over two zones - a wall and a fillet - whose elements fall as their floors rise (each
    floor's own factor), against a limit at `limit_share` of the rules' plan."""
    zones = [{"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 1e-3, "thickness_m": 64e-3},
             {"id": "H1", "kind": "fillet", "faces": ["Face_2"], "size_m": 1e-3, "radius_m": 1.0}]
    monkeypatch.setattr(P, "zones", lambda record, tighten=None: [dict(z) for z in zones])
    monkeypatch.setattr(FD, "_restate", lambda record, plan: plan)

    def fake(record, surface, diag_m, *, levers=None, limit=0, zone_floor_m=None, zone_floors=None, **_k):
        f = zone_floors or {}
        wall = need_wall / (f.get("wall", 1e-3) / 1e-3) ** 3
        fillet = need_fillet / (f.get("fillet", 1e-3) / 1e-3) ** 3
        est = wall + fillet
        return {"fits": est <= P.LIMIT_AIM * limit, "estimate": {"elements": int(est)}, "limit": {}, "zones": [],
                "zone_floors_m": dict(f)}

    monkeypatch.setattr(FD, "make_plan", fake)
    monkeypatch.setattr(FD, "memory_elements",
                        lambda memory_gib=None: int(limit_share * (need_wall + need_fillet) / P.LIMIT_AIM))
    return FD.choose_plan({"faces": {}}, None, 1.0)


def test_over_the_limit_the_bulk_is_coarsened_and_the_stress_raisers_keep_their_sizes(monkeypatch):
    """PLAN-fea-8 (27 Oct): a plan over its limit is coarsened kind by kind - rounds, capped corners, walls -
    and the fillets, holes and notches keep the sizes their rules ask for while that is enough."""
    plan = _kinds_plan(monkeypatch, need_wall=900_000, need_fillet=100_000, limit_share=0.5)
    co = plan["coarsened"]
    assert "wall" in co["floors_m"] and "fillet" not in co["floors_m"]
    assert "fillet" in co["kept"] and not co["at_minimums"]


def test_the_stress_raisers_are_coarsened_only_when_the_bulk_at_its_minimum_is_not_enough(monkeypatch):
    """...and when even the walls at their minimum are not enough, the fillets are coarsened as little as it
    takes - and the walls are then given back as much as still fits."""
    plan = _kinds_plan(monkeypatch, need_wall=100_000, need_fillet=900_000, limit_share=0.3)
    co = plan["coarsened"]
    assert "fillet" in co["floors_m"] and not co["at_minimums"]
    wall = co["floors_m"].get("wall")
    assert wall is None or wall < 64e-3, "the walls were given back once the fillets fitted"


def test_the_coarsening_search_stops_once_every_zone_is_at_its_minimum(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "minimums")
    record, surface, diag = FD.measure_staged(ws)
    monkeypatch.setattr(FD, "memory_elements", lambda memory_gib=None: 10)
    calls = []
    real = FD.make_plan
    monkeypatch.setattr(FD, "make_plan", lambda *a, **k: calls.append(k.get("zone_floor_m")) or real(*a, **k))
    plan = FD.choose_plan(record, surface, diag)
    assert plan["coarsened"]["at_minimums"] and plan["fits"]
    assert len(calls) < FD.FLOOR_STEPS + 1


def test_the_fea_path_reads_gmshs_own_brep_when_the_staging_wrote_one(tmp_path, monkeypatch):
    """PLAN-fea-15: gmsh's STEP writer and reader together lose faces on some parts (fea0080 could
    not be read back; fea0100 left a face with no surface mesh); the B-rep beside it is lossless."""
    from meshpipeline.cad.scout import read_cad

    ws = _stage(fx.plate_with_hole(), tmp_path / "brep")
    assert FD.staged_solid(ws) == ws / "geometry.step"
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    assert FD.staged_solid(ws) == ws / "geometry.brep"
    assert not read_cad(ws / "geometry.brep").IsNull()
    record, _surface, diag = FD.measure_staged(ws)
    by_step = FD.measure_staged(_stage(fx.plate_with_hole(), tmp_path / "step"))
    assert len(record["faces"]) == len(by_step[0]["faces"]) and diag == pytest.approx(by_step[2], rel=1e-6)
    _plan(ws)
    (ws / "geometry.step").unlink()           # the adapter meshes the B-rep, not the STEP
    assert fea_adapter.mesh(ws) == 0
    assert "geometry.brep" in FD.PAYLOAD


def test_a_shape_the_measure_cannot_read_is_healed_and_measured_again(tmp_path, monkeypatch):
    from meshpipeline.cad import fea_measure

    ws = _stage(fx.plate_with_hole(), tmp_path / "heal")
    real = fea_measure.measure_cad_surface
    seen = []

    def once(shape, **kw):
        seen.append(shape)
        if len(seen) == 1:
            raise RuntimeError("Could not fix wire in surface 3")
        return real(shape, **kw)

    monkeypatch.setattr(fea_measure, "measure_cad_surface", once)
    record, _surface, _diag = FD.measure_staged(ws)
    assert len(seen) == 2 and record["faces"]
    assert any("healed" in n for n in record["notes"])


def test_a_plan_the_memory_limit_killed_steps_up_machines_then_is_told_as_memory(tmp_path, monkeypatch):
    """RUN-3: a memory kill runs the SAME plan one machine up, up to the largest; LAD-fea-7: killed on the
    largest too, it is told as memory and not retried (fea0054 and fea0221 spent an hour each on a
    second pass killed the same way)."""
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.contracts.failure_cause import describe, retry_can_help
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.plate_with_hole(), tmp_path / "oom")
    calls = []

    def killed(w, timeout):
        calls.append((MM.read(w) or {}).get("machine"))
        assert (Path(w) / "fea_plan.json").exists()
        return {"rc": -9, "signal": "SIGKILL", "log_tail": "[NATIVE SIGNAL] python was killed by SIGKILL"}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=killed))
    ok, _marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-oom-steps", publish=None, run=_Run()))
    ref = read_refusal(ws)
    assert not ok and calls == [None] + [m.name for m in MM.MACHINES] and ref.facts["out_of_memory"]
    steps = [e for e in dr.cited_for("t-oom-steps") if e["stage"] == "fea_machine" and e["decided"] == "step up"]
    assert [e["value"] for e in steps] == [m.name for m in MM.MACHINES]
    assert "memory" in ref.builder_text and not retry_can_help(ref.cause, ref.facts)
    assert "memory" in " ".join(describe(ref.cause, ref.facts))


def test_a_wall_thin_only_in_places_is_refined_only_there(tmp_path):
    """PLAN-fea-3, thin only where it is thin (fea0221: a 0.05 mm sliver put the whole 660 mm2 bore at
    0.025 mm elements, 298M). The bore keeps the wall the rest of it reads; the sliver is a spot zone
    of its own, held to 2 elements through it on the mesh - and the plan is far smaller than the whole
    face held to the sliver."""
    ws = _stage(fx.sliver_block(), tmp_path / "sliver")
    record, surface, diag = FD.measure_staged(ws)
    thin = {n: r for n, r in record["faces"].items() if r.get("thin_spots")}
    assert thin, "the sliver is read as a thin spot"
    name, row = min(thin.items(), key=lambda kv: kv[1]["thickness_min_mm"])
    assert row["thickness_min_mm"] < 0.8 and row["thickness_wall_mm"] > 1.0
    plan = FD.make_plan(record, surface, diag)
    spots = [z for z in plan["zones"] if z["kind"] == "wall_spot" and z["faces"] == [name]]
    wall = next(z for z in plan["zones"] if z["kind"] == "wall" and z["faces"] == [name])
    assert spots and wall["thickness_m"] > 5 * min(z["thickness_m"] for z in spots)
    # the whole face held to the sliver, as before: many times the elements
    whole = json.loads(json.dumps(record))
    for r in whole["faces"].values():
        r.pop("thin_spots", None)
        r.pop("thickness_wall_mm", None)
    old = FD.make_plan(whole, surface, diag)
    assert plan["estimate"]["elements"] * 2 < old["estimate"]["elements"]
    FD.write_plan(ws, plan)
    assert fea_adapter.mesh(ws) == 0
    q = json.loads((ws / "quality.json").read_text())
    spot_ids = {z["id"] for z in spots}
    assert not spot_ids & set(q["fea"]["rules"]["short"]), q["fea"]["rules"]["through_wall"]
    assert q["cells"] < 1.5 * plan["estimate"]["elements"] and not q["fatal"]


# ---------------------------------------------- PLAN-fea-16: the plan fits in the job's memory ----
def test_the_limit_is_what_the_largest_machine_holds_never_the_mesh_jobs_memory():
    """RUN-1/2 (Areen, 24 Oct): a bigger machine replaces shrinking a mesh to fit - the plan is cut down
    only past the largest machine allowed, never for the Cloud Run job's memory or the old 8M cap."""
    from meshpipeline.contracts import mesh_machine as MM

    room = FD.MESH_JOB_MEMORY_GIB * 1024 / FD.MEMORY_MARGIN - FD.MEMORY_BASE_MB
    assert FD.memory_elements() == int(room * 1024 * 1024 / FD.BYTES_PER_ELEMENT)
    assert FD.memory_elements(16.0) < FD.memory_elements(32.0)
    limit, why = FD.element_limit({"faces": {}}, 13800)
    assert limit == FD.memory_elements(MM.ceiling_gib()) and limit > 100_000_000 and MM.CEILING.name in why


def test_the_mesh_job_memory_is_the_deploys_own():
    """PLAN-fea-16 plans against the memory the mesh job is deployed with (dev and prod alike)."""
    import re

    root = Path(__file__).resolve().parents[3]
    for f in ("deploy/gcp/scripts/bootstrap-env.sh", "deploy/gcp/generated.prod.env"):
        m = re.search(r"^MESH_MEMORY=(?:\$\{MESH_MEMORY:-)?(\d+)Gi", (root / f).read_text(), re.M)
        assert m and float(m.group(1)) == FD.MESH_JOB_MEMORY_GIB, f


def test_a_least_valid_mesh_over_the_memory_is_meshed_anyway_and_said(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "overmem")
    record, surface, diag = FD.measure_staged(ws)
    monkeypatch.setattr(FD, "memory_elements", lambda memory_gib=None: 100)
    plan = FD.choose_plan(record, surface, diag)
    assert plan["fits"] and plan["coarsened"]["at_minimums"] and plan["coarsened"]["over_memory"]
    assert any("memory" in w for w in plan["words"])
    row = next(d for d in plan["decisions"] if d["rule"] == "PLAN-fea-16")
    assert row["inputs"]["over_memory"] and row["inputs"]["memory_gib"] == FD.MESH_JOB_MEMORY_GIB


def test_a_springs_round_wire_keeps_its_section(tmp_path):
    """PLAN-fea-7 from the CAD's own curvature (22 Oct: a coil spring's wire, a swept circle no
    cylinder describes, meshed as a 4- to 6-sided ribbon): the wire face reads its own radius and
    gets 4 elements per quarter-turn - about 16 round the wire, and over 3 across it."""
    import math

    ws = _stage(fx.coil_spring(), tmp_path / "spring")
    record, surface, diag = FD.measure_staged(ws)
    wire = [r for r in record["faces"].values() if r.get("radius_basis") == "curvature"]
    assert wire and wire[0]["radius_mm"] == pytest.approx(fx.SPRING["d"] / 2, rel=0.03)
    plan = FD.make_plan(record, surface, diag)
    round_ = next(z for z in plan["zones"] if z["kind"] == "round")
    around = 2 * math.pi * round_["radius_m"] / round_["size_m"]
    assert around >= 16 * 0.999 and fx.SPRING["d"] * 1e-3 / round_["size_m"] >= 3


def test_a_gentle_curve_gets_no_round_zone_of_its_own():
    from meshpipeline.cad import fea_measure as M

    assert M.ROUND_CAP_SHARE <= 0.25       # a bend over a quarter of the part is the base size's




@pytest.mark.parametrize("fits_from", [None, 0, 1, 5, 9, 13])
def test_the_floor_search_halves_and_finds_the_floor_one_by_one_would(monkeypatch, fits_from):
    """PLAN-fea-8: the first floor that fits, found by halving the list of floors - the same floor the
    floor-by-floor search found, in far fewer histograms (the caldera planned fourteen, 35 minutes)."""
    zones = [{"id": f"Z{k}", "kind": "hole", "faces": [f"Face_{k}"], "size_m": 1e-6 * 1.5 ** k} for k in range(40)]
    monkeypatch.setattr(P, "zones", lambda record, tighten=None: [dict(z) for z in zones])
    monkeypatch.setattr(P, "floored", lambda zl, f, through=None, kinds=None: [
        {**z, "size_m": max(z["size_m"], f)} if kinds is None or z["kind"] in kinds else dict(z) for z in zl])
    monkeypatch.setattr(FD, "_restate", lambda record, plan: plan)
    floors = [1e-6 * 2 ** (k + 1) for k in range(FD.FLOOR_STEPS)]       # what the one-by-one search walks
    first = floors[fits_from] if fits_from is not None else None
    calls = []

    def fake(record, surface, diag_m, *, levers=None, limit=0, zone_floor_m=None, zone_floors=None, **_k):
        f = (zone_floors or {}).get("hole", zone_floor_m)
        calls.append(f)
        ok = f is not None and first is not None and f >= first * (1 - 1e-9)
        return {"fits": ok, "estimate": {"elements": 10 if ok else 10**9}, "limit": {}}

    monkeypatch.setattr(FD, "make_plan", fake)
    plan = FD.choose_plan({"faces": {}}, None, 1.0)
    searched = [f for f in calls[1:] if first is None or abs(f - 0.75 * first) > 1e-18]
    assert len(searched) <= 5                                            # one by one: up to fourteen
    if first is None:
        assert plan["coarsened"]["at_minimums"] and len(searched) == 1
    else:
        assert not plan["coarsened"]["at_minimums"]
        assert plan["coarsened"]["zone_floor_m"] in (pytest.approx(first), pytest.approx(0.75 * first))


# --------------------------------------------- MESH-1/2: mesh first; CAD repair only where it failed ----
def test_a_flagged_part_is_meshed_and_its_note_names_the_spot(tmp_path):
    ws = _stage(fx.wedge(0.2), tmp_path / "flagged")
    record, surface, diag = FD.measure_staged(ws)
    assert record["suspects"] and record["suspects"][0]["kind"] == "fold"
    plan = FD.choose_plan(record, surface, diag)
    assert any("paper-thin fold" in w and "mesh was built" in w for w in plan["words"])


def test_a_mesh_that_fails_at_the_flagged_faces_needs_cad_repair_named(tmp_path, monkeypatch):
    from meshpipeline.contracts.failure_cause import describe, retry_can_help
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh.fea_adapter import BAD_FACES
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.wedge(0.2), tmp_path / "fails_there")
    record, _s, _d = FD.measure_staged(ws)
    faces = record["suspects"][0]["faces"]

    def fill_fails(w, timeout):
        return {"rc": 1, "log_tail": f"{BAD_FACES} {','.join(faces)}\nException: Invalid boundary mesh"}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=fill_fails))
    ok, _m, _o = asyncio.run(FD.drive(ws, {}, job_id="t-mesh2", publish=None, run=_Run()))
    ref = read_refusal(ws)
    assert not ok and ref.gate == FD.CAD_DEFECT_GATE and set(ref.facts["cad_defect"]["faces"]) == set(faces)
    what, nxt = describe(ref.cause, ref.facts)
    assert "needs CAD repair" in what and faces[0] in what and faces[0] in nxt
    assert not retry_can_help(ref.cause, ref.facts)


def test_a_mesh_that_fails_elsewhere_is_the_ordinary_outcome(tmp_path, monkeypatch):
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh.fea_adapter import BAD_FACES
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.wedge(0.2), tmp_path / "fails_elsewhere")
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": 1, "log_tail": f"{BAD_FACES} Face_99\nException: boom"}))
    ok, _m, _o = asyncio.run(FD.drive(ws, {}, job_id="t-mesh2b", publish=None, run=_Run()))
    assert not ok and read_refusal(ws).gate == FD.QUALITY_GATE


def test_a_file_of_closed_shells_is_read_as_its_solid(tmp_path):
    """MESH-1: faces that close, written with no solid entity, are made the solid they bound."""
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.scout import read_cad

    shell = TopExp_Explorer(fx._box(0, 0, 0, 10, 20, 30), TopAbs_SHELL).Current()
    ws = _stage(shell, tmp_path / "shells")
    assert not TopExp_Explorer(read_cad(FD.staged_solid(ws)), TopAbs_SOLID).More()
    assert FD.solids_from_shells(ws) == 1
    assert TopExp_Explorer(read_cad(ws / "geometry.brep"), TopAbs_SOLID).More()
    assert FD.solids_from_shells(ws) == 0                     # a solid already: nothing to do
