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
    assert "PLAN-fea-16" not in cited                        # one part: no assembly
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
        run_cartesian_mesh=lambda w, timeout: calls.append(timeout) or {"rc": -1, "timed_out": True}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-trip",
                                          publish=None, run=_Run()))
    assert not ok and marker == FD.GUARD_TRIP and len(calls) == 1
    rec = json.loads((ws / "fea_passes.json").read_text())
    assert rec["levers"] == [] and rec["guard_trip"]["unit"] == "elements"
    assert not any(e["rule"].startswith("LAD-fea-") and e["decided"] == "lever" for e in dr.cited_for("t-trip"))
    from meshpipeline.engines.preflight import read_refusal

    ref = read_refusal(ws)
    assert ref.gate == FD.GUARD_GATE and ref.cause == "run_infrastructure" and ref.facts["ours"]


def test_a_ladder_that_runs_out_says_so_for_the_retry_policy(tmp_path, monkeypatch):
    """Every pass the plan allows leaves a degenerate element: the attempt ends with the
    driver's own verdict - deterministic, the ladder exhausted, where the worst elements are - so
    no second attempt builds the same mesh (job ab20566f)."""
    from meshpipeline.contracts.failure_cause import retry_can_help
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.plate_with_hole(), tmp_path / "floor")

    def bad_pass(w, timeout):
        rc = fea_adapter.mesh(w)
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
    assert plan["limit"]["elements"] == min(P.ELEMENT_LIMIT, FD.memory_elements())


def test_over_the_limit_the_finest_zones_are_coarsened_and_what_is_short_is_named(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "coarse")
    record, surface, diag = FD.measure_staged(ws)
    monkeypatch.setattr(P, "ELEMENT_LIMIT", 3_000)
    plan = FD.choose_plan(record, surface, diag)
    assert plan["fits"] and plan["coarsened"]["zone_floor_m"] > 0 and plan["zone_floor_m"] > 0
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
    monkeypatch.setattr(P, "ELEMENT_LIMIT", 3_000)
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


# ------------------------------------------------ PLAN-fea-13: the guard grows with the planned work ----
def test_the_guard_is_three_times_the_planned_time_between_the_old_cap_and_the_job_limit():
    assert FD.guard_seconds(None, floor_s=1500, ceiling_s=13800) == 1500
    assert FD.guard_seconds(100.0, floor_s=1500, ceiling_s=13800) == 1500        # never under the old cap
    assert FD.guard_seconds(2000.0, floor_s=1500, ceiling_s=13800) == 6000      # 3 x the planned time
    assert FD.guard_seconds(9000.0, floor_s=1500, ceiling_s=13800) == 13800     # never over the ceiling
    plan = {"estimate": {"elements": 1_761_000}}
    w = FD.planned_work(plan, 1500, rate=587.0, cls="simple", ceiling_s=13800)
    assert w["planned_s"] == pytest.approx(3000.0, rel=1e-3) and w["guard_s"] == 9000 and w["class"] == "simple"


def test_the_fill_rate_follows_the_parts_measured_complexity():
    simple = {"faces": {f"Face_{k}": {} for k in range(10)}, "hot_spots": [{}] * 3}
    moderate = {"faces": {f"Face_{k}": {} for k in range(90)}, "hot_spots": [{}] * 18}
    complex_ = {"faces": {f"Face_{k}": {} for k in range(10)}, "hot_spots": [], "micro": [{}]}
    rates = [FD.fill_rate(r) for r in (simple, moderate, complex_)]
    assert [c for _r, c in rates] == ["simple", "moderate", "complex"]
    assert rates[0][0] > rates[1][0] > rates[2][0]


def test_a_level_is_never_planned_bigger_than_three_guards_fit_under_the_ceiling():
    assert FD.runnable_elements(13800, 587.0) == int(587.0 * 13800 / 3)       # about 2.7M: Fine reachable
    assert FD.runnable_elements(None, 587.0) is None


def test_the_ceiling_is_what_the_attempt_has_left_and_never_more_than_the_job():
    import time as _t

    run = SimpleNamespace(deadline_s=2000.0, _started=_t.monotonic())
    assert FD.ceiling_for(run) == pytest.approx(2000.0 - FD.ATTEMPT_RESERVE_S, abs=5)
    assert FD.ceiling_for(SimpleNamespace()) == FD.MESH_JOB_LIMIT_S - FD.JOB_MARGIN_S


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
    cut = FD.make_plan(record, surface, diag)
    assert not cut["histogram_complete"] and cut["estimate"]["elements"] > 0
    assert full["histogram_complete"]


def test_the_coarsening_search_stops_once_every_zone_is_at_its_minimum(tmp_path, monkeypatch):
    ws = _stage(fx.plate_with_hole(), tmp_path / "minimums")
    record, surface, diag = FD.measure_staged(ws)
    monkeypatch.setattr(P, "ELEMENT_LIMIT", 10)
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


def test_a_plan_the_memory_limit_killed_is_told_as_memory_and_not_retried(tmp_path, monkeypatch):
    """LAD-fea-7: fea0054 and fea0221 spent an hour each on a second pass killed the same way."""
    from meshpipeline.contracts.failure_cause import describe, retry_can_help
    from meshpipeline.engines import runtime
    from meshpipeline.engines.preflight import read_refusal

    ws = _stage(fx.plate_with_hole(), tmp_path / "oom")
    calls = []

    def killed(w, timeout):
        calls.append(w)
        return {"rc": -9, "signal": "SIGKILL", "log_tail": "[NATIVE SIGNAL] python was killed by SIGKILL"}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=killed))
    ok, _marker, _o = asyncio.run(FD.drive(ws, {}, job_id="t-oom", publish=None, run=_Run()))
    ref = read_refusal(ws)
    assert not ok and len(calls) == 1 and ref.facts["out_of_memory"]
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
def test_the_limit_is_what_fits_in_the_mesh_jobs_memory(monkeypatch):
    room = FD.MESH_JOB_MEMORY_GIB * 1024 / FD.MEMORY_MARGIN - FD.MEMORY_BASE_MB
    assert FD.memory_elements() == int(room * 1024 * 1024 / FD.BYTES_PER_ELEMENT)
    assert FD.memory_elements(16.0) < FD.memory_elements(32.0)
    monkeypatch.setattr(FD, "MESH_JOB_MEMORY_GIB", 4.0)
    limit, why = FD.element_limit({"faces": {}}, 13800)
    assert limit == FD.memory_elements() and "memory" in why


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
